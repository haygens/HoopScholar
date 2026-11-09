# HoopScholar Log

My personal log of what I planned and what changed while building HoopScholar.

## Nov 7, 2026

### Planned
- Build a free, Baseball Savant-style site for NBA players.
- I learned the NBA doesn't publish raw player-tracking data the way MLB publishes Statcast. So I built the site around shot locations and the stats NBA.com does publish, pulled with the `nba_api` Python library.
- Steps: shot chart notebook → database schema → one-season backfill → player page with Savant-style percentile sliders → leaderboards and search → automatic updates.

### What changed
- Built the starter project (`af7b49d`):
  - Jupyter notebook (`notebooks/01_shot_chart.ipynb`)
  - SQLite schema with players, teams, games, shots, season stats and shot profiles (`hoopscholar/schema.sql`)
  - Backfill script (`scripts/backfill.py`)
  - FastAPI backend (`api/main.py`)
  - Vanilla JS frontend with SVG shot charts and hot zones, leaderboards and search (`web/`)
  - Nightly update script (`scripts/nightly.py`), demo data generator (`scripts/make_demo_data.py`) and a test (`tests/test_pipeline.py`)
- Renamed the project from BasketballSvnt to HoopScholar.
- winget couldn't install Python (error 1601), so I installed it through the Microsoft Store. That gave me Python 3.15, which is too new for some libraries, so I used Python 3.13 for the virtual environment.
- Ran the site on demo data, then backfilled the real 2025-26 season.
- Created the GitHub repo on the website, since the `gh` CLI wasn't installed, and pushed the project.
- Prepared for Vercel (`f8f5c9a`):
  - The website now opens the database read-only (`hoopscholar/db.py`).
  - Added `pyproject.toml` with the entrypoint (`api.main:app`) and only the runtime dependencies.
  - Committed the database file (`data/hoopscholar.db`).
  - Deployed at hoop-scholar.vercel.app.
- Backfilled the current 2026-27 season (`ad602c8`).
- Switched the automatic update from nightly to weekly, Mondays at 5:17 AM, using Windows Task Scheduler (`cf9e381`). PowerShell scripts update the data and push it so Vercel redeploys (`scripts/nightly.ps1`, `scripts/register_nightly.ps1`).
- The GitHub repo was renamed to HoopScholar, so I updated the git remote URL.

## Nov 8, 2026

### What changed
- Added HoopScholar to my portfolio as Artifact 4, with a reflection.

## Next
- Add the features my friends suggested.
- Move to a hosted database before the database file gets close to GitHub's 100 MB file limit. It's about 49 MB now, with two seasons in it.
