# Game Pass RSS

An RSS feed of **new additions** to the Xbox Game Pass catalog.

`gamepass_rss.py` checks Microsoft's Game Pass catalog once per day, diffs it against a
set of plain-text state files in `data/`, and writes an RSS feed containing one item per newly added game.
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
python3 gamepass_rss.py --render      # rewrite feed.xml from the stored state only
python3 gamepass_rss.py --backfill 50 # re-announce the newest 50 seeded games
```

In production it runs as a GitHub Action (see [Scheduling](#scheduling)), and feed readers
subscribe to the raw file on GitHub.

## Configuration

Edit the constants at the top of `gamepass_rss.py`:

| Setting | Default | Notes |
| --- | --- | --- |
| `TIER` | `"ultimate"` | `"ultimate"` \| `"premium"` \| `"essential"` |
| `PLATFORM` | `"pc"` | `"pc"` \| `"console"` |
| `MARKET` | `"CH"` | ISO 3166-1 alpha-2 store market |
| `LANGUAGE` | `"en-us"` | Language of titles/descriptions |
| `DATA_DIR` | `None` | State and feed location; `None` = `<script dir>/data` (what the workflow commits) |
| `FEED_TITLE` / `FEED_DESCRIPTION` | derived | Feed metadata |
| `FEED_SELF_URL` | env `FEED_SELF_URL` | Public URL of `feed.xml`, for `<atom:link rel="self">`; set by the workflow |
| `FEED_MAX_ITEMS` | `100` | Newest N additions kept in the feed |
| `BOOTSTRAP_ANNOUNCE` | `100` | Newest N seeded games announced on first run; `0` = fully silent |

Advanced knobs (rate limiting, relisting window, sanity guard) are documented inline.

## Scheduling

The feed is updated by the GitHub Actions workflow `.github/workflows/update-feed.yml`,
daily at **03:00 UTC**. Each run:

1. checks out the repo (which contains all state in `data/`),
2. runs `python3 gamepass_rss.py`,
3. commits the changed files in `data/` back to `main` as `github-actions[bot]`.

Subscribe your feed reader to:

```
https://raw.githubusercontent.com/<owner>/<repo>/main/data/feed.xml
```

Setup:

- Push the repo to GitHub. It must be **public** for the raw URL to work without a token.
- Make sure Actions are enabled. The workflow asks for `contents: write` itself; if your
  account or org restricts `GITHUB_TOKEN` to read-only, allow write under
  *Settings → Actions → General → Workflow permissions*.
- To run it by hand, open *Actions → Update feed → Run workflow*. Tick **force** to fetch
  again on a day that already had a successful fetch.

Notes:

- GitHub can start scheduled runs late (sometimes by tens of minutes) when it is busy.
- raw.githubusercontent.com caches files for a few minutes.
- If a fetch fails, the job goes red (GitHub emails you), the feed and product state are
  left as they were, and only a `failed` line is committed to `runs.jsonl`.
- Running the script more often is safe: until the next day's fetch is allowed, it just
  re-renders the feed from the stored state.

## Command-line reference

| Command | Description |
| --- | --- |
| `python3 gamepass_rss.py` | Normal daily run. Respects the once-per-day cap and failure cooldown. |
| `--force` | Fetch even if already fetched successfully today. |
| `-v`, `--verbose` | Debug-level logging. |
| `--render` | Rewrite `feed.xml` from the stored state only; no network. |
| `--backfill [N]` | (Re)announce the N newest bootstrap-seeded games using inferred added dates; no network. Defaults to `BOOTSTRAP_ANNOUNCE`. |
| `--dump PRODUCT_ID` | Print the stored raw store JSON for one product and exit. |

Exit codes: `0` on success, `1` on fetch failure or missing product for `--dump`,
`2` on invalid configuration.

## How it works

1. **Fetch the list.** One request to `catalog.gamepass.com` returns the product IDs for
   the configured tier, platform, and market.
2. **Diff.** Compare against `data/products.jsonl` to find new, still-present, and removed IDs.
3. **Fetch metadata.** Request full store data from `displaycatalog.mp.microsoft.com`,
   batched 50 IDs at a time, only for IDs never seen before.
4. **Decide what to announce.** New IDs are announced unless this is the first run
   (bootstrap) or the same title recently appeared under a different product ID
   (a re-listing / edition swap, not a genuine addition).
5. **Render.** Write `feed.xml` atomically — one `<item>` per announced game, newest first.

## Safety and courtesy

- **At most one catalog fetch per local calendar day**, with a cooldown after a failure.
- **A failed fetch never touches the feed.** Pending changes are discarded (nothing is
  written until the run succeeds) and the last good state is preserved.
- **Sanity guard.** An empty or implausibly shrunken response is treated as an API glitch,
  not a mass removal, preventing a flood of fake "new" games the next day.
- **Polite HTTP.** Retries on network errors, timeouts, 429 and 5xx with exponential
  backoff, honoring `Retry-After` when present.

## Data

| File | Description |
| --- | --- |
| `data/products.jsonl` | One JSON line per product ID, sorted: title, first seen, removed at, announced, why suppressed, and the rendered game info. |
| `data/runs.jsonl` | Append-only audit log, one line per run. |
| `data/catalog.json` | The latest raw catalog list response. Its git history shows each day's additions and removals. |
| `data/raw/<ID>.json` | Raw store JSON for each product, written once, so feed items can be re-rendered without re-fetching. |
| `data/feed.xml` | The generated RSS feed. |

Everything is plain text so the daily commit is a small diff instead of a rewritten binary
database. All files are written atomically (temp file + rename).

## License

No license specified. Treat as private/personal use unless a `LICENSE` file is added.
