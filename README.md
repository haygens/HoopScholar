# HoopScholar

A free, Baseball-Savant-style analytics site for NBA players: shot charts,
hot zones, percentile sliders, leaderboards and search, built on NBA.com
data via [`nba_api`](https://github.com/swar/nba_api).

```
notebooks/01_shot_chart.ipynb   step 0: pull one player's shots, plot on a half court
hoopscholar/schema.sql                 step 1: players, teams, games, shots, season stats, shot profiles
hoopscholar/fetch.py                   nba_api calls (rate limited, retried, proxy-aware)
hoopscholar/load.py                    DataFrame -> table rows (shared by real + demo data)
hoopscholar/metrics.py                 shot profiles, qualification, percentiles, hex bins
hoopscholar/pipeline.py                ingest steps shared by backfill and nightly
scripts/backfill.py             step 2: load one full season
api/main.py + web/              steps 3-4: FastAPI + the player page, leaderboards, search
scripts/nightly.py, nightly.sh  step 5: incremental daily update + cron wrapper
scripts/make_demo_data.py       fake season for offline development
tests/test_pipeline.py          backfill -> nightly test with the network faked
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 1. Try it right away with demo data

```bash
python scripts/make_demo_data.py                      # ~10 s, writes data/demo.db
HOOP_DB=data/demo.db uvicorn api.main:app --reload    # Windows: set HOOP_DB=data\demo.db
```

Open http://127.0.0.1:8000. Every player in the demo is invented, and the site shows a banner saying so.

## 2. Load the real season

```bash
python scripts/backfill.py --season 2025-26           # ~36 requests, 1-3 minutes
uvicorn api.main:app --reload                          # uses data/hoopscholar.db
```

Re-running is safe because every write is an upsert. To add more seasons, run it again with another `--season`;
the player page gets a season dropdown and the career table fills in.

**Run ingestion from your own computer.** stats.nba.com throttles fast clients and doesn't
answer most cloud IPs (AWS, GCP, Azure, Colab, GitHub Actions). If it hangs anyway:
- raise the delay: `HOOP_REQUEST_DELAY=3 python scripts/backfill.py ...`
- or route through a proxy: `NBA_API_PROXY=http://host:port`

## 3. Nightly updates

`scripts/nightly.py` pulls only the games and shots since the last stored date, then
refreshes season-to-date stats and shot profiles. It targets the current season
(2026-27 right now; override with `--season`).

Before the first nightly run of a new season, backfill it once:
`python scripts/backfill.py --season 2026-27`.

**macOS / Linux:** `chmod +x scripts/nightly.sh`, then `crontab -e` and add
```
17 5 * * *  /full/path/to/hoopscholar/scripts/nightly.sh
```
Logs go to `data/logs/`. On macOS, give your terminal (or `cron`) Full Disk Access if the job can't read the folder.

**Windows:** Task Scheduler → Create Basic Task → Daily → Start a program:
`C:\path\to\hoopscholar\.venv\Scripts\python.exe` with argument `scripts\nightly.py`
and "Start in" set to the project folder.

The computer has to be awake when the job runs. When this outgrows a laptop, move the database to
hosted Postgres (Supabase/Neon free tiers) and run the job from any always-on machine
whose IP NBA.com accepts, or through a proxy.

## Deploying to Vercel (free, personal use)

The site only reads data, so Vercel serves a read-only copy of `data/hoopscholar.db`
committed to the repo. `pyproject.toml` tells Vercel where the app is (`api.main:app`) and
lists only the libraries the website needs; `requirements.txt` stays the full local set.

1. Import the GitHub repo at vercel.com/new (no settings to change).
2. After each nightly run on your PC:
   ```
   python scripts/prepare_deploy.py
   git add data/hoopscholar.db
   git commit -m "Data update"
   git push
   ```
   Vercel redeploys automatically.

## How the numbers work

- **Qualified players** have played ≥ 50% of the league-leading games-played total *and* average
  ≥ 15 MPG (`HOOP_QUAL_GP_SHARE`, `HOOP_QUAL_MIN_MPG`). The rule scales with the season, so it
  works in November and in April.
- **Percentiles** rank every player against the qualified pool only, with ties sharing a midpoint
  rank. The worst qualified player is 0 and the best is 100. Lower-is-better stats (turnovers, defensive rating)
  are flipped so red always means good. Players who don't qualify still get a percentile, but the page
  labels them "not qualified".
- **League zone FG%** is total made / total attempted over every league shot in that zone. Backcourt has no league average: NBA.com's official FGA leaves out most missed end-of-period heaves, so the few that remain are mostly makes.
- **Zone FG%** (rim, mid-range, corner 3) is shown for any number of attempts. Under 25 attempts
  (`HOOP_ZONE_MIN_FGA`) the slider gets a "low sample" badge. Each zone's percentile pool is the qualified
  players with at least 25 × (league max GP / 82) attempts there (never below 5, `HOOP_ZONE_POOL_FLOOR`), so
  tiny samples don't distort everyone else's rank but the pool isn't empty in November. Zone leaderboards use the same minimum.
- **League zone FG%** is total made / total attempted over every league shot in that zone. Backcourt has no
  league average: NBA.com's official FGA leaves out most missed end-of-period heaves, so the few left are mostly makes.
- **Hot zones** bin shots into 2 ft hexagons and color each by the player's FG% minus the
  league's FG% *from that same hexagon*. Small samples are pulled toward league average
  (8 phantom league-average attempts) so a 2-for-3 hex doesn't light up.

## API

| Endpoint | What |
|---|---|
| `GET /api/meta` | seasons, metric definitions, data source, last update |
| `GET /api/search?q=jok` | player search |
| `GET /api/leaderboard?stat=ts_pct&season=2025-26&qualified=true&limit=50` | sorted leaderboard |
| `GET /api/players/{id}?season=` | bio, season stats, percentile sliders, zone splits, career |
| `GET /api/players/{id}/shots?season=` | every shot + hex bins vs league |

Interactive docs at http://127.0.0.1:8000/docs.

## Tests

```bash
python -m pytest tests/
```

## Ideas for next steps

- More tracking data: `fetch.player_tracking` already accepts other `pt_measure_type`s
  (`Drives`, `CatchShoot`, `PullUpShot`, `Defense`, `Rebounding`). Add columns and sliders for them.
- Year-over-year percentile history on the player page.
- Player vs player comparison page.
- Export the API responses to static JSON and host the whole site free on GitHub Pages/Vercel.
- Swap the vanilla JS front end for React/Next.js once the pages settle.

## Terms

Data comes from NBA.com and is subject to the NBA.com Terms of Use. Keep the site free and
non-commercial, credit the source, cache what you fetch rather than hammering the API, and don't use
NBA or team logos or player headshots.
