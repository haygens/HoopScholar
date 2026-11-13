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
scripts/rebuild.py              rebuild the website's precomputed tables from loaded data (no NBA.com calls)
scripts/explain.py              EXPLAIN QUERY PLAN for the SQL each main page runs
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
python scripts/backfill.py --season 2025-26           # ~39 requests, 1-3 minutes
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

**Windows:** `powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1` schedules
`scripts\nightly.ps1` weekly (Mondays 5:17 AM). It updates `data\hoopscholar-full.db` (every season, not in git)
and then runs `scripts\push_turso.py`, which sends that week's changes to Turso. The computer has to be awake
(or the job runs at next login).

## Deploying (Vercel + Turso, free plans)

The full history (2016-17 on, ~450 MB) is too big for GitHub, so the site reads a Turso database and the weekly
job on your PC is the only writer:

- **Your PC:** `data\hoopscholar-full.db` is the source of truth. Updates write it with SQLite, record what changed
  (`meta.turso_pending`), and `push_turso.py` sends just that: the new games' box scores and shots, the
  rebuilt season's tables (replaced), the small tables, and `meta` last. A failed push is resent next time.
  Set the user environment variables `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` (a **full-access** token).
- **Vercel:** environment variables `HOOP_DB_BACKEND=turso`, `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`
  (a **read-only** token). Data updates need no redeploy. `pyproject.toml` lists the website's libraries and the
  entrypoint (`api.main:app`); `requirements.txt` is the full local set.
- **First upload:** `python scripts/prepare_turso_upload.py --db data\hoopscholar-full.db --out <file>` makes a
  file in the format Turso's upload API needs (WAL, 4096-byte pages); it's uploaded once into a database created
  with `seed.type = "database_upload"`.
- **Rollback:** remove `HOOP_DB_BACKEND` in Vercel and redeploy; the site reads the committed
  `data/hoopscholar.db` again (as of the last time it was committed).
- `/api/meta` reports which backend a deployment reads (`backend`).

## How the website reads data

- **Precomputed tables.** The weekly update (and `backfill.py`) ends by building, per season, everything the pages
  show: `player_season_summary` (stats, shot profile, qualified flag and percentiles ranked within that season only),
  `season_info`, `player_zone_splits`, `league_zone_averages`, `league_hex_bins` and `games_daily_pool`. Only the
  season just pulled is rebuilt, plus any season never built. After changing how a number is computed, run
  `python scripts/rebuild.py` (every season, no network) or pass `--rebuild-all` to the update.
- **Backends.** `HOOP_DB_BACKEND` picks where the website reads: `sqlite` (default, the local file), `libsql` (the
  same file through the libsql client, to test that path) or `turso` (Turso Cloud; also set `TURSO_DATABASE_URL` and
  `TURSO_AUTH_TOKEN`). Updates always write the local SQLite file.
- **Caching.** Read-only API responses send `Cache-Control: public, s-maxage=86400, stale-while-revalidate=604800`
  (`/api/meta`: 5 minutes), so Vercel's CDN answers repeat requests. The front end adds `?v=<last update>` to every
  request, so after an update no page mixes old and new data, and keeps responses in memory while you browse.
- **Search** matches anywhere in a player's name ("bron" finds LeBron), names starting with the text first, at most
  10 results. No index can serve a match in the middle of a name, so each uncached search reads the players table once.

## How the numbers work

- **Qualified players** have played ≥ 50% of the league-leading games-played total *and* average
  ≥ 15 MPG (`HOOP_QUAL_GP_SHARE`, `HOOP_QUAL_MIN_MPG`). The rule scales with the season, so it
  works in November and in April.
- **Percentiles** rank every player against the qualified pool only, with ties sharing a midpoint
  rank. The worst qualified player is 0 and the best is 100. Lower-is-better stats (TOV%, defensive rating)
  are flipped so red always means good. Players who don't qualify still get a percentile, but the page
  labels them "not qualified".
- **TOV%** is 100 × TOV / (FGA + 0.44 × FTA + TOV) from season totals. It's not NBA.com's `TM_TOV_PCT`,
  which also puts assists in the denominator. Raw turnovers per game stay in the season stats table and leaderboard.
  The TOV% leaderboard and team-page chips only rank players with 400+ turnover opportunities
  (FGA + 0.44 × FTA + TOV) over a full season, scaled by league max GP / 82 (never below 30;
  `HOOP_TOV_MIN_OPPS`, `HOOP_TOV_OPPS_FLOOR`), so a 0-turnover small sample can't top the list.
- **Schema changes** go in `schema.sql` only: `db.migrate()` runs from `backfill.py` and `nightly.py` and adds any
  missing column to an older database. The API works with an older database too (missing columns show as "—" or fall back).
- **Zone FG%** (rim, mid-range, corner 3) is shown for any number of attempts. Under 25 attempts
  (`HOOP_ZONE_MIN_FGA`) the slider gets a "low sample" badge. Each zone's percentile pool is the qualified
  players with at least 25 × (league max GP / 82) attempts there (never below 5, `HOOP_ZONE_POOL_FLOOR`), so
  tiny samples don't distort everyone else's rank but the pool isn't empty in November. Zone leaderboards use the same minimum.
- **League zone FG%** is total made / total attempted over every league shot in that zone. Backcourt has no
  league average: NBA.com's official FGA leaves out most missed end-of-period heaves, so the few left are mostly makes.
- **Similar players** compare each qualified player's slider percentiles with every other qualified player's,
  by cosine similarity after centering at 50 (so the league median is 0). A missing stat, or a zone stat under
  25 attempts, counts as median. The top 5 are stored in `player_similar` and rebuilt for every season at the end
  of each update. Volume stats are part of the profile, so stars tend to match stars.
- **Game logs** come from one `LeagueGameLog` request per season (player mode), not one request per player.
  The weekly update picks up from the last logged game by itself. Rolling 5-game TS% uses the five games'
  combined points and attempts, so a 2-shot game doesn't count as much as a 20-shot game.
- **Hot zones** bin shots into 2 ft hexagons and color each by the player's FG% minus the
  league's FG% *from that same hexagon*. Small samples are pulled toward league average
  (8 phantom league-average attempts) so a 2-for-3 hex doesn't light up.

## Games (`#/games`, code in `web/games.js` and `hoopscholar/games.py`)

- **Pool:** players whose total minutes this season are at least `HOOP_GAMES_MIN_PER_TEAM_GAME` (12) × their
  team's games played. Both games use the pool and this season's numbers.
- **Guess the Player** has two modes. `#/games/guess` opens Daily, or Unlimited once today's puzzle is finished.
- **Daily** (`#/games/guess/daily`): each day's answer is the player with the lowest hash of `date:player_id`
  from that day's pool, skipping the last `HOOP_GAMES_NO_REPEAT_DAYS` (30) answers. Puzzle #1 is
  `HOOP_GAMES_EPOCH` (2026-11-09) and days run on Pacific time. A day's pool is frozen from game logs before
  the Monday a week before that day's week, so a data refresh, even mid-week, can't change an answer.
  Early in a season, when that pool has fewer than `HOOP_GAMES_MIN_DAILY_POOL` (100) players, the day uses
  last season's pool and shows last season's card and team, so there's always a puzzle.
  Position is shown from the start. Each miss reveals the next hint: conference, team, height, jersey, then
  headshot, so the headshot is up before the 6th and last guess. The page counts down to the next puzzle.
- **Unlimited** (`#/games/guess/unlimited`): a random player from today's daily pool (same rules and
  fallback), never today's daily answer and never one of your last 20 unlimited answers. A puzzle in progress
  survives a refresh, and "New player" appears once it's finished. Its streak counts wins in a row.
- **Higher or Lower** (endless): two pool players and one of PPG, APG, RPG, TS%, USG%, AST%, REB% or
  Defensive Rating (lower is better). There are no ties and no repeated pairs within a run.
- Progress and streaks stay in the browser, in localStorage: `hoopscholar:guess:daily:*`,
  `hoopscholar:guess:unlimited:*` and `hoopscholar:hilo:*`. Nothing is stored on the server. Daily progress
  saved under the older `hoopscholar:guess:stats` and `hoopscholar:guess:day:*` keys moves to the daily keys
  automatically the first time a games page opens.

## API

| Endpoint | What |
|---|---|
| `GET /api/meta` | seasons, metric definitions, data source, last update |
| `GET /api/search?q=jok` | player and team search (teams first), anywhere in the name, at most 10 |
| `GET /api/teams/LAL?season=` | team roster by minutes, each player's top 3 percentiles |
| `GET /api/leaderboard?stat=ts_pct&season=2025-26&qualified=true&limit=50&offset=0` | sorted leaderboard, 50 a page (max 100), `has_more` for the next page |
| `GET /api/players/{id}?season=` | bio, season stats, percentile sliders, zone splits, career |
| `GET /api/players/{id}/shots?season=&last=10&period=4&loc=away` | shots, hex bins and zone splits, optionally filtered by last N games, quarter (1-4, OT) and home/away; always compared with the full-season league |
| `GET /api/players/{id}/games?season=` | game log with rolling 5-game PTS and TS% |
| `GET /api/players/{id}/career` | every slider's percentile in every season |
| `GET /api/games/pool?day=YYYY-MM-DD` | games pool with bio and stats, every player's name for this season, frozen weekly pools for the daily puzzle |

Interactive docs at http://127.0.0.1:8000/docs.

## Tests

```bash
python -m pytest tests/
node --test tests/games.test.mjs     # games storage and picking rules (Node 20+, no packages)
```

## Ideas for next steps

- More tracking data: `fetch.player_tracking` already accepts other `pt_measure_type`s
  (`Drives`, `CatchShoot`, `PullUpShot`, `Defense`, `Rebounding`). Add columns and sliders for them.
- Export the API responses to static JSON and host the whole site free on GitHub Pages/Vercel.
- Swap the vanilla JS front end for React/Next.js once the pages settle.

## Terms

Data comes from NBA.com and is subject to the NBA.com Terms of Use. Keep the site free and
non-commercial, credit the source, cache what you fetch rather than hammering the API, and don't use
NBA or team logos or player headshots.
