#!/usr/bin/env python3
"""
gamepass_rss.py — an RSS feed of *new additions* to the Xbox Game Pass catalog.

What it does, once per day:
  1. Fetches the product-ID list for one Game Pass tier + platform + market
     (a single request to catalog.gamepass.com).
  2. Diffs it against a local SQLite database.
  3. Fetches full metadata *only* for product IDs it has never seen before
     (displaycatalog.mp.microsoft.com, batched).
  4. Writes feed.xml — one <item> per newly added game, newest first.

Design rules (see also the "Why" notes throughout):
  * At most one successful catalog fetch per calendar day. Running the script
    more often is safe and pointless; it simply re-renders the feed from the DB.
  * A failed fetch never touches the feed. Last-good state is kept.
  * The first ever run seeds the database silently — otherwise you would get
    600 "new" games at once.
  * A product ID that merely replaces another listing of the same game
    (edition swap, re-published SKU) is NOT announced. Only a game whose title
    is genuinely new to the catalog — or has been gone for a long while — is.

Dependencies: Python 3.9+ standard library only.

Run:
    python3 gamepass_rss.py            # normal daily run
    python3 gamepass_rss.py --force    # ignore the once-a-day cap
    python3 gamepass_rss.py -v         # verbose logging
    python3 gamepass_rss.py --render   # just rewrite feed.xml from the DB
    python3 gamepass_rss.py --backfill 100   # re-announce the 100 newest games from the initial seed
    python3 gamepass_rss.py --dump 9PNJXVCVWD4K  # show stored raw store JSON for one product

Suggested systemd timer (preferred over cron: Persistent=true replays a run
the Pi slept through):

    # /etc/systemd/system/gamepass-rss.service
    [Unit]
    Description=Game Pass additions RSS
    [Service]
    Type=oneshot
    User=pi
    ExecStart=/usr/bin/python3 /home/pi/gamepass_rss.py

    # /etc/systemd/system/gamepass-rss.timer
    [Unit]
    Description=Run gamepass-rss daily
    [Timer]
    OnCalendar=*-*-* 07:00:00
    RandomizedDelaySec=15m
    Persistent=true
    [Install]
    WantedBy=timers.target

    sudo systemctl enable --now gamepass-rss.timer

Then serve DATA_DIR/feed.xml with anything static (nginx, Caddy, or even
`python3 -m http.server` for LAN use).
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these.
# ═══════════════════════════════════════════════════════════════════════════════

TIER = "ultimate"          # "ultimate" | "premium" | "essential"
PLATFORM = "pc"            # "pc" | "console"
MARKET = "CH"              # ISO 3166-1 alpha-2 store market
LANGUAGE = "en-us"         # Language of titles/descriptions in the feed

# Where the database, feed and logs live. Default: a "data" folder next to
# this script. Must be writable by the user running the script.
DATA_DIR = None            # e.g. "/var/lib/gamepass-rss"; None = <script dir>/data

FEED_TITLE = f"New on Game Pass {TIER.title()} ({PLATFORM.upper()}, {MARKET})"
FEED_DESCRIPTION = "Games newly added to the Xbox Game Pass catalog."
FEED_SELF_URL = None       # Public URL of feed.xml, if you serve it (for <atom:link rel="self">)
FEED_MAX_ITEMS = 100       # Newest N additions kept in the feed

# The list endpoint has no "added on" date, so on the very first run the script
# cannot know which games are recent. It infers an added date from each
# product's store availability records (see infer_added_date) and announces the
# newest N. Set to 0 for a silent bootstrap. Can be re-run with --backfill.
BOOTSTRAP_ANNOUNCE = 100

# Rate limiting against Microsoft.
MAX_FETCHES_PER_DAY = 1    # Successful catalog list fetches per local calendar day
FAILED_RETRY_COOLDOWN_HOURS = 6   # After a failed fetch, wait this long before retrying

# Sanity guard: if the returned list is empty, or has shrunk by more than this
# fraction versus the last good run, assume the API glitched and keep last-good.
# Why: a partial response would otherwise be recorded as a mass removal and the
# next good response as a flood of fake "new" games.
MAX_SHRINK_RATIO = 0.10

# A game whose title was in the catalog within the last N days and reappears
# under a new product ID is treated as the same listing (SKU churn), not a new
# addition. A game gone longer than this is a genuine return and IS announced.
RELISTING_WINDOW_DAYS = 30

# ═══════════════════════════════════════════════════════════════════════════════
# END OF CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

import argparse
import gzip
import json
import logging
import mimetypes
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

log = logging.getLogger("gamepass_rss")

# ─── Microsoft endpoints and identifiers ──────────────────────────────────────
# Tier GUIDs and contexts as observed by github.com/nanhopper/catabox after the
# 2025 tier restructure. If Microsoft renames tiers again, these are what change.

SIGLS_ENDPOINT = "https://catalog.gamepass.com/sigls/v3"
DISPLAY_CATALOG_ENDPOINT = "https://displaycatalog.mp.microsoft.com/v7.0/products"
DISPLAY_CATALOG_MS_CV = "DGU1mcuYo0WMMp+F.1"   # Correlation vector the store client sends; harmless but expected

TIERS = {
    "ultimate":  {"sigl_id": "97c6c862-d28a-4907-a3d5-c401f2296a53", "subscription_context": "cfq7ttc0khs0"},
    "premium":   {"sigl_id": "09a72c0d-c466-426a-9580-b78955d8173a", "subscription_context": "cfq7ttc0p85b"},
    "essential": {"sigl_id": "34031711-5a70-4196-bab7-45757dc2294e", "subscription_context": "cfq7ttc0k5dj"},
}
PLATFORMS = {
    "pc": "pc",
    "console": "ConsoleGen8;ConsoleGen9",
}

USER_AGENT = "gamepass-rss/1.0 (+personal RSS generator; one request per day)"
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_ATTEMPTS = 4
HTTP_BACKOFF_BASE_SECONDS = 2.0

# DisplayCatalog accepts many IDs per call; 50 keeps the URL well under proxy limits.
DISPLAY_CATALOG_BATCH_SIZE = 50
MAX_SCREENSHOTS = 6


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP — polite, retrying JSON GET
# ═══════════════════════════════════════════════════════════════════════════════

class FetchError(Exception):
    """A request failed after all retries, or returned something unusable."""


def get_json(url: str) -> Any:
    """GET a JSON document with timeout, retries and exponential backoff.

    Retries on network errors, timeouts, 429 and 5xx. Does not retry other 4xx —
    those mean the request itself is wrong (bad market, bad ID) and won't fix
    themselves by waiting.
    """
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Optional[Exception] = None

    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            last_error = error
            retryable = error.code == 429 or error.code >= 500
            if not retryable:
                raise FetchError(f"GET {url} -> HTTP {error.code} {error.reason}") from error
            delay = _retry_delay(attempt, error.headers.get("Retry-After"))
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            last_error = error
            delay = _retry_delay(attempt, None)

        if attempt < HTTP_MAX_ATTEMPTS:
            log.warning("Attempt %d/%d failed (%s); retrying in %.1fs", attempt, HTTP_MAX_ATTEMPTS, last_error, delay)
            time.sleep(delay)

    raise FetchError(f"GET {url} failed after {HTTP_MAX_ATTEMPTS} attempts: {last_error}") from last_error


def _retry_delay(attempt: int, retry_after_header: Optional[str]) -> float:
    if retry_after_header and retry_after_header.strip().isdigit():
        return float(retry_after_header.strip())
    return HTTP_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Catalog — talking to Microsoft
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CatalogListing:
    """The raw product-ID list for one tier/platform/market, plus its source JSON."""
    product_ids: tuple[str, ...]
    raw_json: bytes
    source_title: str


def fetch_catalog_listing() -> CatalogListing:
    """One request: which product IDs are currently in the configured Game Pass list."""
    tier = TIERS[TIER]
    params = {
        "id": tier["sigl_id"],
        "language": LANGUAGE,
        "market": MARKET,
        "platformContext": PLATFORMS[PLATFORM],
        "subscriptionContext": tier["subscription_context"],
    }
    url = f"{SIGLS_ENDPOINT}?{urlencode(params)}"
    log.info("Fetching catalog list: %s", url)
    payload = get_json(url)

    # Shape: [ {header...}, {"id": "9XXXXXXXXXXX"}, {"id": ...}, ... ]
    if not isinstance(payload, list) or not payload:
        raise FetchError("SIGLS response was not a non-empty JSON array")
    header, *entries = payload
    if not isinstance(header, dict):
        raise FetchError("SIGLS response header missing")
    if header.get("siglId") and header["siglId"].lower() != tier["sigl_id"]:
        # Same check catabox does: make sure Microsoft answered for the list we asked about.
        log.warning("SIGLS header siglId %s != requested %s", header.get("siglId"), tier["sigl_id"])

    ids = _unique_sorted(
        entry["id"] for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip()
    )
    return CatalogListing(
        product_ids=tuple(ids),
        raw_json=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        source_title=str(header.get("title") or ""),
    )


def fetch_products(product_ids: Iterable[str]) -> dict[str, dict]:
    """Full store metadata for the given product IDs, batched. Returns {product_id: raw product}."""
    ids = _unique_sorted(product_ids)
    products: dict[str, dict] = {}
    for start in range(0, len(ids), DISPLAY_CATALOG_BATCH_SIZE):
        batch = ids[start:start + DISPLAY_CATALOG_BATCH_SIZE]
        params = {
            "bigIds": ",".join(batch),
            "market": MARKET,
            "languages": LANGUAGE,
            "MS-CV": DISPLAY_CATALOG_MS_CV,
        }
        url = f"{DISPLAY_CATALOG_ENDPOINT}?{urlencode(params)}"
        log.info("Fetching metadata for %d products", len(batch))
        payload = get_json(url)
        for product in (payload or {}).get("Products") or []:
            product_id = str(product.get("ProductId") or "").upper()
            if product_id:
                products[product_id] = product
    missing = [pid for pid in ids if pid not in products]
    if missing:
        log.warning("DisplayCatalog returned no data for %d product(s): %s", len(missing), ", ".join(missing))
    return products


def _unique_sorted(values: Iterable[str]) -> list[str]:
    return sorted({value.strip().upper() for value in values if value and value.strip()})


# ═══════════════════════════════════════════════════════════════════════════════
# Metadata — turning a raw DisplayCatalog product into what we show in the feed
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class GameInfo:
    product_id: str
    title: str
    short_description: str = ""
    description: str = ""
    publisher: str = ""
    developer: str = ""
    genres: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)   # Raw capability attributes: XPA, 4K, HDR, ...
    player_modes: list[str] = field(default_factory=list)
    content_rating: str = ""
    release_date: str = ""                                # ISO date
    poster_url: str = ""
    hero_url: str = ""
    screenshot_urls: list[str] = field(default_factory=list)
    store_url: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "GameInfo":
        return cls(**json.loads(text))


def extract_game_info(product_id: str, product: dict) -> GameInfo:
    localized = (product.get("LocalizedProperties") or [{}])[0]
    props = product.get("Properties") or {}
    images = localized.get("Images") or []

    title = _clean(localized.get("ProductTitle")) or product_id
    return GameInfo(
        product_id=product_id,
        title=title,
        short_description=_clean(localized.get("ShortDescription")),
        description=_clean(localized.get("ProductDescription")),
        publisher=_clean(localized.get("PublisherName")),
        developer=_clean(localized.get("DeveloperName")),
        genres=_genres(props),
        features=_feature_attributes(props),
        player_modes=_player_modes(product),
        content_rating=_content_rating(product),
        release_date=_release_date(product),
        poster_url=_pick_image(images, ("Poster", "BoxArt", "BrandedKeyArt", "Tile")),
        hero_url=_pick_image(images, ("SuperHeroArt", "TitledHeroArt", "HeroArt", "BrandedKeyArt")),
        screenshot_urls=_screenshots(images),
        store_url=store_url(product_id, title),
    )


def store_url(product_id: str, title: str) -> str:
    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "game"
    return f"https://www.xbox.com/{LANGUAGE}/games/store/{slug}/{product_id}"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _genres(props: dict) -> list[str]:
    raw = list(props.get("Categories") or [])
    if props.get("Category"):
        raw.append(props["Category"])
    return sorted({_clean(genre) for genre in raw if _clean(genre)})


def _feature_attributes(props: dict) -> list[str]:
    names = [_clean(attribute.get("Name")) for attribute in props.get("Attributes") or [] if isinstance(attribute, dict)]
    return sorted({name for name in names if name})


_PLAYER_MODE_PATTERNS = (
    ("Single player",     re.compile(r"single.?player", re.I)),
    ("Online multiplayer", re.compile(r"online.?multi.?player|cross.?platform.?multi.?player", re.I)),
    ("Local multiplayer", re.compile(r"local.?multi.?player|shared.?split.?screen", re.I)),
    ("Online co-op",      re.compile(r"online.?co.?op|coop.?support.?online|cross.?platform.?coop", re.I)),
    ("Local co-op",       re.compile(r"local.?co.?op|couch.?co.?op|split.?screen.?co.?op", re.I)),
)


def _player_modes(product: dict) -> list[str]:
    """Derive human-readable modes from capability attributes, with player counts when given."""
    modes: dict[str, str] = {}
    for attribute in (product.get("Properties") or {}).get("Attributes") or []:
        name = _clean(attribute.get("Name")) if isinstance(attribute, dict) else ""
        for label, pattern in _PLAYER_MODE_PATTERNS:
            if label not in modes and pattern.search(name):
                lo, hi = attribute.get("Minimum"), attribute.get("Maximum")
                count = f" ({lo})" if lo and lo == hi else f" ({lo}-{hi})" if lo and hi else ""
                modes[label] = f"{label}{count}"
    return list(modes.values())


def _content_rating(product: dict) -> str:
    """Prefer PEGI (European markets); fall back to whatever rating system is listed first."""
    ratings: list[dict] = []
    for market_props in product.get("MarketProperties") or []:
        ratings.extend(market_props.get("ContentRatings") or [])
    ratings.extend((product.get("Properties") or {}).get("ContentRatings") or [])
    if not ratings:
        return ""
    rating = next((r for r in ratings if "PEGI" in str(r.get("RatingSystem", "")).upper()), ratings[0])
    system = _clean(rating.get("RatingSystem"))
    value = _clean(rating.get("RatingId") or rating.get("Rating") or rating.get("RatingValue"))
    value = re.sub(r"^[A-Za-z]+:", "", value)   # "PEGI:12" -> "12"
    return f"{system} {value}".strip()


def _release_date(product: dict) -> str:
    candidates: list[Any] = []
    for market_props in product.get("MarketProperties") or []:
        if str(market_props.get("Market", "")).upper() == MARKET:
            candidates.append(market_props.get("OriginalReleaseDate"))
    for market_props in product.get("MarketProperties") or []:
        candidates.append(market_props.get("OriginalReleaseDate"))
    candidates.append((product.get("Properties") or {}).get("OriginalReleaseDate"))
    return _first_plausible_date(candidates)


def _first_plausible_date(candidates: Iterable[Any]) -> str:
    """First value that looks like a real ISO date (Microsoft uses 1601/1753 as 'unset')."""
    for value in candidates:
        if isinstance(value, str) and re.match(r"^(19[5-9]\d|20\d{2})-\d{2}-\d{2}", value):
            return value[:10]
    return ""


def infer_added_date(product: dict) -> tuple[str, str]:
    """Best guess at when this product joined the configured Game Pass tier.

    Returns (iso_date, source). Sources, in order of trust:
      'subscription' — StartDate of the store availability that is fulfilled by
                       the tier's subscription product (the same BigId we send as
                       subscriptionContext). This is the real answer when present.
      'modified'     — the product's LastModifiedDate. Any store edit bumps it,
                       so it's a loose upper bound.
      'release'      — original release date. Wrong for old games added recently.
      ''             — nothing usable.
    """
    subscription_id = TIERS[TIER]["subscription_context"].upper()
    subscription_dates: list[str] = []
    for sku in product.get("DisplaySkuAvailabilities") or []:
        for availability in sku.get("Availabilities") or []:
            remediations = availability.get("Remediations") or []
            fulfilled_by_tier = any(
                isinstance(r, dict) and str(r.get("BigId", "")).upper() == subscription_id for r in remediations
            )
            if fulfilled_by_tier:
                subscription_dates.append(_first_plausible_date([(availability.get("Conditions") or {}).get("StartDate")]))
    subscription_dates = [d for d in subscription_dates if d]
    if subscription_dates:
        return max(subscription_dates), "subscription"       # Latest = most recent (re)join
    modified = _first_plausible_date([product.get("LastModifiedDate")])
    if modified:
        return modified, "modified"
    released = _release_date(product)
    return (released, "release") if released else ("", "")


def _image_url(image: dict) -> str:
    uri = image.get("Uri") if isinstance(image, dict) else None
    if not isinstance(uri, str) or not uri:
        return ""
    if uri.startswith("//"):
        return "https:" + uri
    return uri.replace("http://", "https://", 1)


def _pick_image(images: list, purposes: tuple[str, ...]) -> str:
    """Largest image whose ImagePurpose matches, checking purposes in preference order."""
    for purpose in purposes:
        matches = [img for img in images if isinstance(img, dict) and img.get("ImagePurpose") == purpose and img.get("Uri")]
        if matches:
            best = max(matches, key=lambda img: (img.get("Width") or 0) * (img.get("Height") or 0))
            return _image_url(best)
    return ""


def _screenshots(images: list) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict) or str(image.get("ImagePurpose", "")).lower() != "screenshot":
            continue
        url = _image_url(image)
        content_hash = image.get("UnscaledImageSHA256Hash")
        if not url or url in seen or (content_hash and content_hash in seen):
            continue
        seen.update(filter(None, (url, content_hash)))   # Same image can appear under two URLs or two hashes
        if "store-images.s-microsoft.com" in url:
            # Microsoft's image CDN resizes on request; ask for a modest thumbnail.
            url += ("&" if "?" in url else "?") + "w=640&h=360&q=80&format=jpg"
        urls.append(url)
        if len(urls) == MAX_SCREENSHOTS:
            break
    return urls


# ─── Title families: recognising the same game under a different product ID ──

_PLATFORM_QUALIFIERS = (
    re.compile(r"\s*[\[(]\s*(?:win(?:dows)?(?:\s+(?:10|11|pc))?|pc|xbox(?:\s+(?:one|series\s*x\s*\|?\s*s))?)(?:\s+version)?\s*[\])]\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:for\s+)?windows(?:\s+(?:10|11))?(?:\s*(?:\+|and)\s*launcher)?(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:for\s+)?xbox(?:\s+(?:one|series\s*x\s*\|?\s*s))?(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?pc(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:standard|deluxe|ultimate|gold|premium|digital|special|complete|definitive|anniversary|(?:game\s+of\s+the\s+year|goty)|(?:19|20)\d{2})\s+edition\s*$", re.I),
)
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def family_key(title: str) -> str:
    """Normalise a title so 'Forza Horizon 5 – Windows', 'FORZA HORIZON V' and
    'Forza Horizon 5 Deluxe Edition' all collapse to 'forza horizon 5'.

    Ported from catabox's game-families logic. Deliberately does NOT strip years
    or numbers: 'FC 26' and 'FC 27' are different games.
    """
    text = _clean(title)
    previous = None
    while text != previous:                       # Strip qualifiers repeatedly: "X (PC) - Windows"
        previous = text
        for pattern in _PLATFORM_QUALIFIERS:
            text = pattern.sub("", text).strip()
    text = text or _clean(title)
    text = re.sub(r"[©®℠™]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text, flags=re.I)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text, flags=re.I)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return " ".join(_ROMAN.get(token, token) for token in text.split())


# ═══════════════════════════════════════════════════════════════════════════════
# Storage — SQLite
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id        TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    family_key        TEXT NOT NULL,
    first_seen        TEXT NOT NULL,      -- ISO-8601 UTC, when we first saw it in the list
    last_seen         TEXT NOT NULL,      -- ISO-8601 UTC, last run in which it was present
    removed_at        TEXT,               -- set when it disappears; cleared if it returns
    announced         INTEGER NOT NULL,   -- 1 = shown in the feed
    suppressed_reason TEXT,               -- 'bootstrap' | 'relisting' | NULL
    info_json         TEXT NOT NULL,      -- GameInfo as JSON (what the feed renders)
    raw_product_gz    BLOB                -- gzip'd raw DisplayCatalog product, for future re-rendering
);
CREATE INDEX IF NOT EXISTS products_family_idx ON products(family_key);
CREATE INDEX IF NOT EXISTS products_first_seen_idx ON products(first_seen);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,          -- ISO-8601 UTC
    status        TEXT NOT NULL,          -- 'ok' | 'failed'
    product_count INTEGER,
    added         INTEGER,
    announced     INTEGER,
    removed       INTEGER,
    error         TEXT,
    raw_sigls_gz  BLOB                    -- gzip'd raw list response, for audit / re-diffing
);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs(started_at);
"""


class Store:
    """Thin, explicit wrapper around the SQLite database. No ORM, no magic."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ── runs ──────────────────────────────────────────────────────────────────

    def last_run(self, status: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE status = ? ORDER BY started_at DESC LIMIT 1", (status,)
        ).fetchone()

    def successful_runs_on(self, local_date: str) -> int:
        # started_at is UTC; compare on the local date by checking each row in Python.
        rows = self.conn.execute("SELECT started_at FROM runs WHERE status = 'ok'").fetchall()
        return sum(1 for row in rows if _to_local_date(row["started_at"]) == local_date)

    def record_run(self, started_at: datetime, status: str, **fields: Any) -> None:
        columns = ["started_at", "status", *fields]
        values = [started_at.isoformat(), status, *fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        self.conn.execute(f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders})", values)

    # ── products ──────────────────────────────────────────────────────────────

    def known_product_ids(self) -> set[str]:
        return {row["product_id"] for row in self.conn.execute("SELECT product_id FROM products")}

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0

    def family_last_seen(self, key: str) -> Optional[datetime]:
        row = self.conn.execute("SELECT MAX(last_seen) AS ts FROM products WHERE family_key = ?", (key,)).fetchone()
        return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None

    def insert_product(self, info: GameInfo, raw_product: Optional[dict], seen_at: datetime,
                       announced: bool, suppressed_reason: Optional[str]) -> None:
        self.conn.execute(
            """INSERT INTO products (product_id, title, family_key, first_seen, last_seen, removed_at,
                                     announced, suppressed_reason, info_json, raw_product_gz)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (info.product_id, info.title, family_key(info.title), seen_at.isoformat(), seen_at.isoformat(),
             int(announced), suppressed_reason, info.to_json(),
             gzip.compress(json.dumps(raw_product, separators=(",", ":")).encode()) if raw_product else None),
        )

    def mark_present(self, product_ids: Iterable[str], seen_at: datetime) -> None:
        self.conn.executemany(
            "UPDATE products SET last_seen = ?, removed_at = NULL WHERE product_id = ?",
            [(seen_at.isoformat(), pid) for pid in product_ids],
        )

    def mark_removed(self, product_ids: Iterable[str], seen_at: datetime) -> int:
        cursor = self.conn.executemany(
            "UPDATE products SET removed_at = ? WHERE product_id = ? AND removed_at IS NULL",
            [(seen_at.isoformat(), pid) for pid in product_ids],
        )
        return cursor.rowcount

    def announced_products(self, limit: int) -> list[tuple[GameInfo, datetime]]:
        rows = self.conn.execute(
            "SELECT info_json, first_seen FROM products WHERE announced = 1 ORDER BY first_seen DESC, title LIMIT ?",
            (limit,),
        ).fetchall()
        return [(GameInfo.from_json(row["info_json"]), datetime.fromisoformat(row["first_seen"])) for row in rows]

    def bootstrap_products(self) -> list[tuple[str, str, Optional[dict]]]:
        """(product_id, title, raw product or None) for every row seeded at bootstrap."""
        rows = self.conn.execute(
            "SELECT product_id, title, raw_product_gz FROM products WHERE suppressed_reason = 'bootstrap'"
        ).fetchall()
        return [
            (row["product_id"], row["title"],
             json.loads(gzip.decompress(row["raw_product_gz"])) if row["raw_product_gz"] else None)
            for row in rows
        ]

    def raw_product(self, product_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT raw_product_gz FROM products WHERE product_id = ?", (product_id.upper(),)).fetchone()
        return json.loads(gzip.decompress(row["raw_product_gz"])) if row and row["raw_product_gz"] else None

    def set_first_seen(self, product_id: str, first_seen: datetime) -> None:
        self.conn.execute("UPDATE products SET first_seen = ? WHERE product_id = ?", (first_seen.isoformat(), product_id))

    def set_announced(self, product_ids: Iterable[str], announced: bool) -> None:
        self.conn.executemany(
            "UPDATE products SET announced = ? WHERE product_id = ?", [(int(announced), pid) for pid in product_ids]
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _to_local_date(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone().date().isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# Sync — the daily job
# ═══════════════════════════════════════════════════════════════════════════════

class SkipFetch(Exception):
    """Not an error: we chose not to hit Microsoft this run."""


def check_fetch_allowed(store: Store, now: datetime, force: bool) -> None:
    """Enforce the once-per-day rule and the post-failure cooldown."""
    if force:
        return
    today = now.astimezone().date().isoformat()
    if store.successful_runs_on(today) >= MAX_FETCHES_PER_DAY:
        raise SkipFetch(f"already fetched successfully today ({today})")
    last_failed = store.last_run("failed")
    if last_failed:
        failed_at = datetime.fromisoformat(last_failed["started_at"])
        cooldown = timedelta(hours=FAILED_RETRY_COOLDOWN_HOURS)
        last_ok = store.last_run("ok")
        ok_after_failure = last_ok and datetime.fromisoformat(last_ok["started_at"]) > failed_at
        if not ok_after_failure and now - failed_at < cooldown:
            raise SkipFetch(f"last fetch failed at {failed_at:%Y-%m-%d %H:%M} UTC; cooling down for {cooldown}")


def validate_listing(listing: CatalogListing, store: Store) -> None:
    """Refuse to believe an implausible response. Raises FetchError."""
    if not listing.product_ids:
        raise FetchError("catalog list came back empty")
    last_ok = store.last_run("ok")
    if last_ok and last_ok["product_count"]:
        floor = int(last_ok["product_count"] * (1 - MAX_SHRINK_RATIO))
        if len(listing.product_ids) < floor:
            raise FetchError(
                f"catalog shrank from {last_ok['product_count']} to {len(listing.product_ids)} products "
                f"(below {floor}); treating as an API glitch"
            )


@dataclass
class SyncResult:
    product_count: int
    added: int
    announced: int
    removed: int


def sync_catalog(store: Store, now: datetime) -> SyncResult:
    """Fetch the list, diff it, fetch metadata for newcomers, persist. Raises FetchError on failure."""
    listing = fetch_catalog_listing()
    validate_listing(listing, store)

    is_bootstrap = store.is_empty()
    current = set(listing.product_ids)
    known = store.known_product_ids()
    new_ids = sorted(current - known)
    gone_ids = sorted(known - current)
    log.info("Catalog has %d products: %d new, %d no longer listed", len(current), len(new_ids), len(gone_ids))

    products = fetch_products(new_ids) if new_ids else {}

    # Update presence first, so relisting detection below sees accurate last_seen
    # for everything *except* the newcomers we're about to insert.
    store.mark_present(current & known, now)
    removed = store.mark_removed(gone_ids, now)

    announced_count = 0
    for product_id in new_ids:
        raw = products.get(product_id)
        if raw is None:
            # Listed in Game Pass but the store has no page for it. Announce anyway
            # with a bare title so it isn't silently lost; store a stub so we don't
            # re-request it every day.
            info = GameInfo(product_id=product_id, title=product_id, store_url=store_url(product_id, product_id))
        else:
            info = extract_game_info(product_id, raw)

        announced, reason = decide_announcement(store, info, now, is_bootstrap)
        announced_count += int(announced)
        store.insert_product(info, raw, now, announced, reason)
        log.info("%-12s %s%s", "NEW" if announced else "SUPPRESSED", info.title, f"  [{reason}]" if reason else "")

    store.record_run(now, "ok", product_count=len(current), added=len(new_ids), announced=announced_count,
                     removed=removed, raw_sigls_gz=gzip.compress(listing.raw_json))
    if is_bootstrap and BOOTSTRAP_ANNOUNCE > 0:
        announced_count = backfill_bootstrap(store, BOOTSTRAP_ANNOUNCE)
    store.commit()
    return SyncResult(len(current), len(new_ids), announced_count, removed)


def backfill_bootstrap(store: Store, announce_newest: int) -> int:
    """Give bootstrap-seeded products a real 'added' date and announce the newest N.

    Idempotent: only touches rows whose suppressed_reason is 'bootstrap'. Rows
    whose date can't be inferred keep their original first_seen and stay silent.
    """
    dated: list[tuple[datetime, str, str]] = []       # (added, product_id, title)
    sources: dict[str, int] = {}
    for product_id, title, raw in store.bootstrap_products():
        if raw is None:
            sources["no raw product"] = sources.get("no raw product", 0) + 1
            continue
        iso_date, source = infer_added_date(raw)
        sources[source or "none"] = sources.get(source or "none", 0) + 1
        if not iso_date:
            continue
        added = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
        store.set_first_seen(product_id, added)
        dated.append((added, product_id, title))

    dated.sort(reverse=True)
    chosen = dated[:announce_newest]
    store.set_announced([pid for _, pid, _ in dated], False)      # Reset, so re-running with a smaller N works
    store.set_announced([pid for _, pid, _ in chosen], True)

    log.info("Backfill: dated %d bootstrap products (sources: %s); announcing newest %d",
             len(dated), ", ".join(f"{k}={v}" for k, v in sorted(sources.items())), len(chosen))
    for added, _, title in chosen[:10]:
        log.info("  %s  %s", added.date(), title)
    if len(chosen) > 10:
        log.info("  ... and %d more", len(chosen) - 10)
    return len(chosen)


def decide_announcement(store: Store, info: GameInfo, now: datetime, is_bootstrap: bool) -> tuple[bool, Optional[str]]:
    """Should this newly seen product ID appear in the feed?

    * First run ever: no — we're just learning what's already there.
    * Same title seen recently under another product ID: no — it's a relisting.
    * Otherwise: yes.
    """
    if is_bootstrap:
        return False, "bootstrap"
    seen = store.family_last_seen(family_key(info.title))
    if seen and now - seen < timedelta(days=RELISTING_WINDOW_DAYS):
        return False, "relisting"
    return True, None


# ═══════════════════════════════════════════════════════════════════════════════
# Feed — rendering RSS 2.0
# ═══════════════════════════════════════════════════════════════════════════════

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_MEDIA = "http://search.yahoo.com/mrss/"
ET.register_namespace("atom", NS_ATOM)
ET.register_namespace("media", NS_MEDIA)


def render_feed(items: list[tuple[GameInfo, datetime]], built_at: datetime) -> bytes:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", FEED_TITLE)
    _text(channel, "link", f"https://www.xbox.com/{LANGUAGE}/xbox-game-pass")
    _text(channel, "description", FEED_DESCRIPTION)
    _text(channel, "language", LANGUAGE)
    _text(channel, "lastBuildDate", format_datetime(built_at))
    _text(channel, "generator", "gamepass_rss.py")
    if FEED_SELF_URL:
        ET.SubElement(channel, f"{{{NS_ATOM}}}link", href=FEED_SELF_URL, rel="self", type="application/rss+xml")

    for info, first_seen in items:
        channel.append(render_item(info, first_seen))

    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def render_item(info: GameInfo, first_seen: datetime) -> ET.Element:
    item = ET.Element("item")
    _text(item, "title", info.title)
    _text(item, "link", info.store_url)
    guid = _text(item, "guid", info.product_id)
    guid.set("isPermaLink", "false")            # Readers key on this: never re-show the same game
    _text(item, "pubDate", format_datetime(first_seen))
    for genre in info.genres:
        _text(item, "category", genre)
    if info.poster_url:
        mime = mimetypes.guess_type(info.poster_url.split("?")[0])[0] or "image/jpeg"
        ET.SubElement(item, "enclosure", url=info.poster_url, type=mime, length="0")
        ET.SubElement(item, f"{{{NS_MEDIA}}}thumbnail", url=info.poster_url)
    for url in info.screenshot_urls:
        ET.SubElement(item, f"{{{NS_MEDIA}}}content", url=url, medium="image")
    _text(item, "description", render_description_html(info))
    return item


def render_description_html(info: GameInfo) -> str:
    """The body of the feed item: cover art, blurb, and every useful fact MS gave us."""
    from html import escape as h

    parts: list[str] = []
    if info.poster_url:
        parts.append(f'<p><a href="{h(info.store_url)}"><img src="{h(info.poster_url)}" alt="{h(info.title)}" style="max-width:300px"></a></p>')
    if info.short_description:
        parts.append(f"<p><strong>{h(info.short_description)}</strong></p>")

    facts = [
        ("Developer", info.developer),
        ("Publisher", info.publisher),
        ("Genre", ", ".join(info.genres)),
        ("Players", ", ".join(info.player_modes)),
        ("Rating", info.content_rating),
        ("Released", info.release_date),
        ("Features", ", ".join(info.features)),
        ("Product ID", info.product_id),
    ]
    rows = "".join(f"<li><b>{h(label)}:</b> {h(value)}</li>" for label, value in facts if value)
    if rows:
        parts.append(f"<ul>{rows}</ul>")

    if info.description and info.description != info.short_description:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}|\r\n\r\n", info.description) if p.strip()]
        parts.extend(f"<p>{h(p)}</p>" for p in (paragraphs or [info.description]))

    if info.screenshot_urls:
        shots = "".join(f'<a href="{h(u)}"><img src="{h(u)}" style="max-width:100%;margin:2px 0"></a>' for u in info.screenshot_urls)
        parts.append(f"<p>{shots}</p>")
    if info.hero_url:
        parts.append(f'<p><img src="{h(info.hero_url)}" style="max-width:100%"></p>')

    parts.append(f'<p><a href="{h(info.store_url)}">Open in the Xbox store</a></p>')
    return "\n".join(parts)


def _text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = value
    return element


def write_feed(store: Store, feed_path: Path, now: datetime) -> int:
    items = store.announced_products(FEED_MAX_ITEMS)
    xml = render_feed(items, now)
    tmp = feed_path.with_suffix(".xml.tmp")
    tmp.write_bytes(xml)
    os.replace(tmp, feed_path)                  # Atomic: a reader never sees a half-written file
    return len(items)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="fetch even if already fetched today")
    parser.add_argument("--render", action="store_true", help="only rewrite feed.xml from the database; no network")
    parser.add_argument("--backfill", type=int, metavar="N", nargs="?", const=BOOTSTRAP_ANNOUNCE,
                        help="(re)announce the N newest bootstrap-seeded games using inferred added dates; no network")
    parser.add_argument("--dump", metavar="PRODUCT_ID", help="print the stored raw store JSON for one product and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if TIER not in TIERS or PLATFORM not in PLATFORMS:
        log.error("TIER must be one of %s and PLATFORM one of %s", list(TIERS), list(PLATFORMS))
        return 2

    data_dir = Path(DATA_DIR) if DATA_DIR else Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path, feed_path = data_dir / "gamepass.sqlite3", data_dir / "feed.xml"

    now = datetime.now(timezone.utc)
    store = Store(db_path)
    exit_code = 0
    try:
        if args.dump:
            raw = store.raw_product(args.dump)
            print(json.dumps(raw, indent=2, ensure_ascii=False) if raw else f"No stored product {args.dump}")
            return 0 if raw else 1

        if args.backfill is not None:
            backfill_bootstrap(store, args.backfill)
            store.commit()
        elif not args.render:
            try:
                check_fetch_allowed(store, now, args.force)
                result = sync_catalog(store, now)
                log.info("Sync OK: %d in catalog, %d new (%d announced), %d removed",
                         result.product_count, result.added, result.announced, result.removed)
            except SkipFetch as why:
                log.info("Not fetching: %s", why)
            except FetchError as error:
                # Nothing has been committed; last-good state is intact.
                store.conn.rollback()
                store.record_run(now, "failed", error=str(error))
                store.commit()
                log.error("Fetch failed, feed left unchanged: %s", error)
                exit_code = 1

        count = write_feed(store, feed_path, now)
        log.info("Wrote %s with %d item(s)", feed_path, count)
    finally:
        store.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

