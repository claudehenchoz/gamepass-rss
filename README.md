# Game Pass RSS

An RSS feed of **new additions** to the Xbox Game Pass catalog.

`gamepass_rss.py` checks Microsoft's Game Pass catalog once per day, diffs it against a
local SQLite database, and writes an RSS feed containing one item per newly added game.
It only fetches full store metadata for games it has never seen before, so the daily run
is a couple of HTTP requests, not hundreds.

- Standard library only — no `pip install`, no virtualenv, no build step.
- Python 3.9+.
- One configurable tier / platform / market combination per instance.
- Failed runs never touch the feed; the last good state is always kept.
- Covers art, description, developer, publisher, genre, players, rating, release date,
  features, screenshots, and a link into the Xbox store.

## Quick start

```bash
python3 gamepass_rss.py          # fetch, diff, and write data/feed.xml
```

On the very first run the script **seeds silently**: it records everything currently in
the catalog without announcing 600 games at once. To publish the most recent additions
from that initial seed, run:

```bash
python3 gamepass_rss.py --backfill 100
```

To test locally without hitting Microsoft, use the offline flags:

```bash
python3 gamepass_rss.py --render      # rewrite feed.xml from the database only
python3 gamepass_rss.py --backfill 50 # re-announce the newest 50 seeded games
```

Then serve `data/feed.xml` with anything static — nginx, Caddy, or even
`python3 -m http.server` for LAN use.

## Configuration

Edit the constants at the top of `gamepass_rss.py`:

| Setting | Default | Notes |
| --- | --- | --- |
| `TIER` | `"ultimate"` | `"ultimate"` \| `"premium"` \| `"essential"` |
| `PLATFORM` | `"pc"` | `"pc"` \| `"console"` |
| `MARKET` | `"CH"` | ISO 3166-1 alpha-2 store market |
| `LANGUAGE` | `"en-us"` | Language of titles/descriptions |
| `DATA_DIR` | `None` | Database/feed/log location; `None` = `<script dir>/data` |
| `FEED_TITLE` / `FEED_DESCRIPTION` | derived | Feed metadata |
| `FEED_SELF_URL` | `None` | Public URL of `feed.xml`, for `<atom:link rel="self">` |
| `FEED_MAX_ITEMS` | `100` | Newest N additions kept in the feed |
| `BOOTSTRAP_ANNOUNCE` | `100` | Newest N seeded games announced on first run; `0` = fully silent |

Advanced knobs (rate limiting, relisting window, sanity guard) are documented inline.

## Scheduling

The script is designed to run once a day. Running it more often is safe — it simply
re-renders the feed from the database until the next day's fetch is allowed.

A systemd timer is preferred over cron because `Persistent=true` replays a run the
machine slept through:

```ini
# /etc/systemd/system/gamepass-rss.service
[Unit]
Description=Game Pass additions RSS
[Service]
Type=oneshot
User=pi
ExecStart=/usr/bin/python3 /home/pi/gamepass_rss.py
```

```ini
# /etc/systemd/system/gamepass-rss.timer
[Unit]
Description=Run gamepass-rss daily
[Timer]
OnCalendar=*-*-* 07:00:00
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now gamepass-rss.timer
```

## Command-line reference

| Command | Description |
| --- | --- |
| `python3 gamepass_rss.py` | Normal daily run. Respects the once-per-day cap and failure cooldown. |
| `--force` | Fetch even if already fetched successfully today. |
| `-v`, `--verbose` | Debug-level logging. |
| `--render` | Rewrite `feed.xml` from the database only; no network. |
| `--backfill [N]` | (Re)announce the N newest bootstrap-seeded games using inferred added dates; no network. Defaults to `BOOTSTRAP_ANNOUNCE`. |
| `--dump PRODUCT_ID` | Print the stored raw store JSON for one product and exit. |

Exit codes: `0` on success, `1` on fetch failure or missing product for `--dump`,
`2` on invalid configuration.

## How it works

1. **Fetch the list.** One request to `catalog.gamepass.com` returns the product IDs for
   the configured tier, platform, and market.
2. **Diff.** Compare against the SQLite database to find new, still-present, and removed IDs.
3. **Fetch metadata.** Request full store data from `displaycatalog.mp.microsoft.com`,
   batched 50 IDs at a time, only for IDs never seen before.
4. **Decide what to announce.** New IDs are announced unless this is the first run
   (bootstrap) or the same title recently appeared under a different product ID
   (a re-listing / edition swap, not a genuine addition).
5. **Render.** Write `feed.xml` atomically — one `<item>` per announced game, newest first.

## Safety and courtesy

- **At most one catalog fetch per local calendar day**, with a cooldown after a failure.
- **A failed fetch never touches the feed.** The database transaction is rolled back and
  the last good state is preserved.
- **Sanity guard.** An empty or implausibly shrunken response is treated as an API glitch,
  not a mass removal, preventing a flood of fake "new" games the next day.
- **Polite HTTP.** Retries on network errors, timeouts, 429 and 5xx with exponential
  backoff, honoring `Retry-After` when present.

## Data

| File | Description |
| --- | --- |
| `data/gamepass.sqlite3` | State: `products` (one row per product ID) and `runs` (audit log). |
| `data/feed.xml` | The generated RSS feed. |

`products` keeps the raw store JSON (gzipped) so feed items can be re-rendered without
re-fetching, and `runs` records the raw catalog list response for each successful run for
auditing and re-diffing.

## License

No license specified. Treat as private/personal use unless a `LICENSE` file is added.
