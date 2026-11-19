# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A single-file Python 3.9+ script (`gamepass_rss.py`) that generates an RSS feed of
**newly added** games in the Xbox Game Pass catalog for one tier + platform + market.
It runs once per day as a GitHub Action, diffs Microsoft's catalog against plain-text
state files in `data/`, writes `data/feed.xml`, and commits `data/` back to the repo. It fetches full store metadata only for never-before-seen
product IDs.

There is no package, no build step, no tests, and no third-party dependencies. Standard
library only — keep it that way unless asked otherwise.

## Layout

| Path | Purpose |
| --- | --- |
| `gamepass_rss.py` | Entire program. Config at top, entry point at bottom. |
| `.github/workflows/update-feed.yml` | Production runner: daily 03:00 UTC, runs the script, commits `data/`. |
| `data/products.jsonl` | State: one JSON line per product ID, sorted. |
| `data/runs.jsonl` | Append-only run audit log. |
| `data/catalog.json` | Latest raw SIGLS list response (indented: one ID per line). |
| `data/raw/<ID>.json` | Raw DisplayCatalog product; write-once. |
| `data/feed.xml` | Generated output, served via raw.githubusercontent.com. Rewritten atomically on each run. |
| `data/glober/` | Unrelated Godot/HTML game export. Do not touch; ignore it. |

## Running

```bash
python3 gamepass_rss.py            # normal daily run (respects once-per-day + cooldown)
python3 gamepass_rss.py --force    # ignore the once-a-day cap
python3 gamepass_rss.py -v         # debug logging
python3 gamepass_rss.py --render   # rewrite feed.xml from stored state only; no network
python3 gamepass_rss.py --backfill 100   # re-announce newest N bootstrap games; no network
python3 gamepass_rss.py --dump 9PNJXVCVWD4K  # print stored raw store JSON for one product
```

Use `--render`, `--backfill`, or `--dump` when testing locally to avoid hitting Microsoft.
A network run mutates the real state in `data/` — prefer `--render` for UI/feed-formatting
work, or point `DATA_DIR` at a scratch copy.

## Architecture (in file order)

1. **Configuration** (top of file): `TIER`, `PLATFORM`, `MARKET`, `LANGUAGE`, `DATA_DIR`,
   `FEED_*`, `BOOTSTRAP_ANNOUNCE`, rate-limit and heuristic constants. Edit here for
   behavior changes; don't hard-code values elsewhere.
2. **HTTP** (`get_json`, `_retry_delay`): retrying JSON GET. Retries network errors,
   timeouts, 429, 5xx with exponential backoff. Non-retryable 4xx raise `FetchError`.
3. **Catalog** (`fetch_catalog_listing`, `fetch_products`): one SIGLS request returns the
   product-ID list; DisplayCatalog is called in batches of 50 for metadata.
4. **Metadata** (`extract_game_info` + `_*` helpers): turns a raw product into the
   `GameInfo` dataclass that the feed renders.
5. **Storage** (`Store`): loads the text files into memory; mutations stay in memory
   until `commit()` writes them (temp + `os.replace`); `rollback()` reloads from disk.
   Text rather than SQLite because git stores every daily version — keep files sorted
   and stable so diffs stay small. There is no per-product `last_seen` for the same
   reason (see `family_last_seen`).
6. **Sync** (`sync_catalog`, `validate_listing`, `decide_announcement`): the daily job.
7. **Feed** (`render_feed`, `render_item`, `render_description_html`, `write_feed`):
   builds RSS 2.0 XML with media RSS extensions; writes via temp file + `os.replace`.
8. **Entry point** (`parse_args`, `main`): CLI dispatch and error handling.

## Invariants — do not break these

These exist for good reasons documented in code comments; preserve them.

- **At most one successful catalog fetch per local calendar day** (`MAX_FETCHES_PER_DAY`),
  plus a cooldown after a failure (`FAILED_RETRY_COOLDOWN_HOURS`). Only `--force` bypasses.
- **A failed fetch never touches the feed.** On `FetchError`, pending changes are rolled
  back and only a `failed` run row is recorded. Last-good state stays.
- **Sanity guard** (`MAX_SHRINK_RATIO`): an empty or implausibly shrunken list is treated
  as an API glitch, not mass removal.
- **Bootstrap is silent by default.** The first run seeds the state without announcing;
  `backfill_bootstrap` infers dates and announces the newest N.
- **Relisting suppression** (`RELISTING_WINDOW_DAYS`): a title that reappears under a new
  product ID within the window is SKU churn, not a new addition.
- **Feed writes are atomic** (`tmp` + `os.replace`) — readers never see a half-written file.
- **`guid` is `isPermaLink="false"` and is the product ID** — readers use it to dedupe.

## Conventions

- Standard library only; Python 3.9+ syntax (`from __future__ import annotations` is used).
- Type hints throughout; dataclasses for structured records.
- Log via the module-level `log = logging.getLogger("gamepass_rss")`, not `print`.
- Comments explain *why*, especially around Microsoft API quirks and heuristics. Match that.
- Configuration constants are UPPER_SNAKE at the top of the file.
- Raw store JSON is kept in `data/raw/` so feed items can be re-rendered without re-fetching.
- Don't rewrite unchanged lines in the state files (no daily-touched fields, no unstable
  key order) — every byte that changes lands in git history.

## Gotchas

- Microsoft endpoints, tier GUIDs (`TIERS`), and platform contexts change when Game Pass
  restructures. If the catalog comes back wrong, check `TIERS`/`PLATFORMS` first.
- `started_at` is stored in UTC; "once per day" is evaluated in the **local** timezone via
  `_to_local_date`.
- Listings with no store page are announced as a stub titled with the product ID rather
  than dropped.
- `data/glober/` is unrelated to this project.
