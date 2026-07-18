# Unified Jackpot Tracker

Flask app that scrapes jackpot values from **Zitobox** (no login) and **CarnivalCiti** (logged-in sweepstakes casino) on an hourly cycle, stores them in SQLite, and exposes a dark-themed dashboard at `http://127.0.0.1:5050`.

Designed to run 24/7 on a Linux VPS behind Nginx + Let's Encrypt.

## What it does

- Drives two scrapers (Zitobox, CarnivalCiti) from a single dashboard.
- Each source has its own background worker thread (hourly cycle, 8h session cap).
- Cycle retries: every game that didn't produce a reading is re-scraped on the same pass.
- Stale-token detection: Zitobox detects expired tokens via HTTP 401 and re-harvests before falling back to a slow full-browser re-login.
- Diagnostics panel: live status, success rate, last 5 cycle durations, log tail, error-cycles counter.
- **Export & filter panel**: filter readings by date/time range and value range, then copy to clipboard as TSV (paste directly into Excel / Google Sheets).

## Quick start (local dev)

```bash
git clone <this-repo>
cd <this-repo>
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

# Required for CarnivalCiti — your login credentials
cp accessos.txt.example accessos.txt
# edit accessos.txt with your carnivalciti.com email + password

# Optional: links file for Zitobox games
cp links_zitobox.txt.example links_zitobox.txt
# add one line per game: "Display Name | Jackpot Label | https://www.zitobox.com/game/..."

python app.py
# → open http://127.0.0.1:5050
```

## Running 24/7 on a Linux VPS

See [`GUIA-DEPLOY.md`](./GUIA-DEPLOY.md) — copy-pasteable steps for Ubuntu 24.04 + systemd + Nginx + Let's Encrypt. Takes ~30 minutes on a fresh VPS.

## Tests

```bash
python test_regression.py
```

Verifies the three original bug fixes (zombie worker, Carnival listener cleanup, log persistence) plus the new fixes (Zitobox token retry, Carnival retry flag, export endpoint, history filters). 13 tests, runs in <5 seconds with no network and no browser.

## File map

```
app.py            Flask app + HTTP routes + worker thread driver
scrapers.py       Zitobox + CarnivalCiti scrapers (httpx + Playwright)
db.py             SQLite schema + queries + backup + export formatting
templates/        Jinja templates (single index.html)
test_regression.py Unit tests (13 cases)
requirements.txt  flask, playwright, httpx
links_zitobox.txt.example  Template for Zitobox game list
accessos.txt.example       Template for CarnivalCiti credentials
GUIA-DEPLOY.md    VPS deploy guide (systemd + Nginx + Let's Encrypt)
```

## What NOT to commit

`.gitignore` excludes: `accessos.txt`, `carnival_storage.json`, `data/`, `*.vbs`, `*.bat`. The first two contain real credentials / browser session state — never push them.

## Known behaviors

- Zitobox cycles cache tokens between runs; on 401 the cache is invalidated and a new token is harvested (~30-90s per game on first failure, ~instant on retry).
- CarnivalCiti runs up to 3 attempts per game (90s each, 270s total wall-clock cap). Games that fail all attempts are retried in a second pass at the end of the cycle.
- Worker restarts (Start → Stop → Start) always spawn a fresh thread; the previous thread is joined before the new one starts (root-cause fix for the historical "5x loop" bug).