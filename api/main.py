"""HoopScholar API + static site.

    uvicorn api.main:app --reload                      # uses data/hoopscholar.db
    HOOP_DB=data/demo.db uvicorn api.main:app --reload # demo data

Then open http://127.0.0.1:8000

Where it reads from is set by HOOP_DB_BACKEND (see hoopscholar/db.py): the local
file by default, or Turso with HOOP_DB_BACKEND=turso plus TURSO_DATABASE_URL and
TURSO_AUTH_TOKEN.

Pages read the tables the weekly job precomputes (player_season_summary,
season_info, player_zone_splits, league_zone_averages, league_hex_bins,
games_daily_pool), so a request touches a few dozen rows instead of a season
of shots. Read-only responses carry a Cache-Control header so Vercel's CDN
answers repeat requests without running the function.
"""
import datetime as dt
import json
import sys
import threading
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoopscholar import config, db, games, metrics  # noqa: E402

app = FastAPI(title="HoopScholar")
_local = threading.local()


def conn():
    """One read-only connection per worker thread."""
    if not hasattr(_local, "conn"):
        _local.conn = db.connect(readonly=True)
    return _local.conn


def rows(sql: str, params=()) -> list[dict]:
    return db.rows(conn(), sql, params)


def row(sql: str, params=()) -> dict | None:
    return db.row(conn(), sql, params)


# ------------------------------------------------------------------ CDN caching
# The data only changes with the weekly update, so Vercel's CDN may keep a
# response for a day and serve it stale for a week while it refetches in the
# background. The front end adds ?v=<last_updated> to every request, so after
# an update new URLs miss the cache and old answers are never mixed with new.
# /api/meta (where the front end learns last_updated) is kept for 5 minutes.
CACHE_LONG = "public, s-maxage=86400, stale-while-revalidate=604800"
CACHE_META = "public, s-maxage=300, stale-while-revalidate=3600"


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if request.method in ("GET", "HEAD") and path.startswith("/api/") and response.status_code in (200, 404):
        response.headers["Cache-Control"] = CACHE_META if path == "/api/meta" else CACHE_LONG
    return response


# In-process cache for the games pool, keyed on (name, season, meta.last_updated).
_cache: dict = {}
_cache_lock = threading.Lock()


def _version() -> str:
    return db.get_meta(conn(), "last_updated", "0")


def cached(name: str, season: str, build):
    key = (name, season, _version())
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    value = build()
    with _cache_lock:
        _cache[key] = value
    return value


# ------------------------------------------------------------------ helpers

def seasons() -> list[str]:
    return [r["season"] for r in rows("SELECT season FROM season_info ORDER BY season DESC")]


def resolve_season(season: str | None) -> str:
    all_s = seasons()
    if not all_s:
        raise HTTPException(503, "No data loaded yet. Run scripts/backfill.py or scripts/make_demo_data.py.")
    if season is None:
        return all_s[0]
    if season not in all_s:
        raise HTTPException(404, f"Season {season} not loaded. Have: {', '.join(all_s)}")
    return season


def season_info(season: str) -> dict:
    info = row("""SELECT season, players, pool_size, max_gp, pool_min_fga, min_tov_opps, min_tsa
                  FROM season_info WHERE season = ?""", (season,))
    if not info:
        raise HTTPException(404, f"Season {season} not loaded")
    return info


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def metric_meta(ms):
    return [{"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt,
             "higher_better": m.higher_better} for m in ms]


def low_sample(m: metrics.Metric, value, sample) -> bool:
    return bool(m.sample_col and value is not None and (sample or 0) < config.ZONE_MIN_FGA)


# Column lists for player_season_summary reads (only what each page uses).
SAMPLE_COLS = [m.sample_col for m in metrics.METRICS if m.sample_col]
METRIC_KEYS = [m.key for m in metrics.METRICS]
SLIDER_COLS = list(dict.fromkeys(METRIC_KEYS + SAMPLE_COLS + ["tov_opps"] + metrics.PCT_COLS))
PLAYER_COLS = list(dict.fromkeys(["player_id", "season", *metrics.SUMMARY_STAT_COLS, "qualified", *metrics.PCT_COLS]))
TEAM_COLS = list(dict.fromkeys(["player_id", "full_name", "position", "gp", "min", "pts", "reb", "ast", "ts_pct",
                                "qualified", *SLIDER_COLS]))
CAREER_COLS = ["season", "team_abbr", "gp", "min", "pts", "reb", "ast", "stl", "blk", "tov",
               "fg_pct", "fg3_pct", "ft_pct", "ts_pct"]
BIO_COLS = ["player_id", "full_name", "first_name", "last_name", "team_abbr", "jersey", "position", "height",
            "weight", "country", "college", "draft_year", "draft_round", "draft_number", "is_active",
            "from_year", "to_year"]


def cols(names) -> str:
    return ", ".join(names)


# ------------------------------------------------------------------ routes

@app.get("/api/meta")
def meta():
    c = conn()
    return {
        "seasons": seasons(),
        "data_source": db.get_meta(c, "data_source", "unknown"),
        "last_updated": db.get_meta(c, "last_updated"),
        "metrics": metric_meta(metrics.METRICS),
        "sortable": metric_meta(metrics.SORTABLE.values()),
        "qualification": {"gp_share": config.QUAL_GP_SHARE, "min_mpg": config.QUAL_MIN_MPG},
        "zone_min_fga": config.ZONE_MIN_FGA,
        "glossary": metrics.GLOSSARY,
    }


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, max_length=60), limit: int = Query(10, ge=1, le=10)):
    """Teams (by abbreviation or name) first, then players with stats whose
    name contains q anywhere ("bron" finds LeBron), case-insensitive. Names
    that start with q come first, then the most recent seasons."""
    text = q.strip().replace("%", "").replace("_", "")
    if not text:
        return []
    prefix = text + "%"
    teams = [{"kind": "team", **r} for r in rows("""
        SELECT abbr, name FROM teams
        WHERE abbr LIKE ? OR name LIKE ?
        ORDER BY (abbr LIKE ?) DESC, name LIMIT 3""", (prefix, f"%{text}%", text))]
    players = [{"kind": "player", **r} for r in rows("""
        SELECT player_id, full_name, last_team AS team_abbr, position, last_season
        FROM players
        WHERE full_name LIKE ? AND last_season IS NOT NULL
        ORDER BY (full_name LIKE ?) DESC, last_season DESC, full_name
        LIMIT ?""", (f"%{text}%", prefix, limit))]
    return (teams + players)[:limit]


@app.get("/api/teams/{abbr}")
def team(abbr: str, season: str | None = None):
    """Roster for one team-season: everyone whose season stat line is with this
    team, by minutes, with each player's top 3 percentiles."""
    t = row("SELECT team_id, abbr, name FROM teams WHERE abbr = ? COLLATE NOCASE", (abbr,))
    if not t:
        raise HTTPException(404, "Team not found")
    team_seasons = [r["season"] for r in rows(
        "SELECT DISTINCT season FROM player_season_summary WHERE team_abbr = ? ORDER BY season DESC", (t["abbr"],))]
    if not team_seasons:
        raise HTTPException(404, "No stats for this team")
    season = season if season in team_seasons else team_seasons[0]
    info = season_info(season)
    roster = rows(f"""SELECT {cols(TEAM_COLS)} FROM player_season_summary
                      WHERE season = ? AND team_abbr = ? ORDER BY min DESC, player_id""", (season, t["abbr"]))
    out = [{**{k: r[k] for k in ("player_id", "full_name", "position", "gp", "min", "pts", "reb", "ast", "ts_pct")},
            "qualified": bool(r["qualified"]),
            "top": metrics.top_percentiles(r, metrics.summary_pct(r), tov_min=info["min_tov_opps"])}
           for r in roster]
    return {"team": t, "season": season, "seasons": team_seasons, "roster": out}


@app.get("/api/leaderboard")
def leaderboard(stat: str = "pts", season: str | None = None, qualified: bool = True,
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=2000),
                asc: bool | None = None):
    """One page (limit rows from offset) of a season's leaders on `stat`.
    has_more says whether another page follows."""
    if stat not in metrics.SORTABLE:
        raise HTTPException(400, f"Unknown stat {stat}")
    season = resolve_season(season)
    info = season_info(season)
    m = metrics.SORTABLE[stat]
    where, params = ["season = ?", f"{stat} IS NOT NULL"], [season]
    if qualified:
        where.append("qualified = 1")
    rule = metrics.rank_rule(m, info)  # zone FG%, TOV% and TS%: sample minimums
    if rule:
        where.append(f"COALESCE({rule[0]}, 0) >= ?")
        params.append(rule[1])
    ascending = (not m.higher_better) if asc is None else asc
    show = list(dict.fromkeys(["player_id", "full_name", "team_abbr", "position", "gp", "min", "pts", "reb", "ast",
                               "ts_pct", stat]))
    pct = f"p_{stat}" if stat in metrics.METRIC_BY_KEY else "NULL"
    got = rows(f"""SELECT {cols(show)}, {pct} AS percentile FROM player_season_summary
                   WHERE {' AND '.join(where)}
                   ORDER BY {stat} {'ASC' if ascending else 'DESC'}, player_id
                   LIMIT ? OFFSET ?""", params + [limit + 1, offset])
    out = [{**r, "rank": rank, "value": r[stat]} for rank, r in enumerate(got[:limit], offset + 1)]
    return {"season": season, "stat": stat, "qualified_only": qualified, "rows": out,
            "offset": offset, "has_more": len(got) > limit,
            "minimum": metrics.rank_minimum(m, mins=info)}


@app.get("/api/players/{player_id}")
def player(player_id: int, season: str | None = None):
    bio = row(f"SELECT {cols(BIO_COLS)} FROM players WHERE player_id = ?", (player_id,))
    if not bio:
        raise HTTPException(404, "Player not found")
    career = rows(f"SELECT {cols(CAREER_COLS)} FROM player_season_summary WHERE player_id = ? ORDER BY season",
                  (player_id,))
    if not career:
        raise HTTPException(404, "No stats for this player")
    player_seasons = [c["season"] for c in reversed(career)]
    season = season if season in player_seasons else player_seasons[0]
    s = row(f"SELECT {cols(PLAYER_COLS)} FROM player_season_summary WHERE player_id = ? AND season = ?",
            (player_id, season))
    info = season_info(season)
    pcts = metrics.summary_pct(s)

    sliders = []
    for m in metrics.METRICS:
        sample = s.get(m.sample_col) if m.sample_col else None
        sample = int(sample) if sample is not None else None
        value = s.get(m.key)
        sliders.append({"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt,
                        "value": value, "percentile": pcts[m.key], "sample": sample,
                        "low_sample": low_sample(m, value, sample)})

    league = {z["zone_basic"]: z["fg_pct"] for z in rows(
        "SELECT zone_basic, fg_pct FROM league_zone_averages WHERE season = ?", (season,))}
    zones = [{**z, "fg_pct": z["fgm"] / z["fga"] if z["fga"] else None,
              "league_fg_pct": league.get(z["zone_basic"]),
              "no_league_avg": z["zone_basic"] in metrics.NO_LEAGUE_AVG_ZONES}
             for z in rows("""SELECT zone_basic, fga, fgm FROM player_zone_splits
                              WHERE player_id = ? AND season = ? ORDER BY fga DESC""", (player_id, season))]

    similar = rows("""
        SELECT ps.similar_id AS player_id, ps.score, p.full_name, s.team_abbr, p.position
        FROM player_similar ps
        JOIN players p ON p.player_id = ps.similar_id
        LEFT JOIN player_season_summary s ON s.player_id = ps.similar_id AND s.season = ps.season
        WHERE ps.player_id = ? AND ps.season = ? ORDER BY ps.rank""", (player_id, season))

    return {
        "player": bio,
        "season": season,
        "seasons": player_seasons,
        "qualified": bool(s["qualified"]),
        "stats": {k: v for k, v in s.items() if not k.startswith("p_")},
        "sliders": sliders,
        "zones": zones,
        "career": career,
        "similar": similar,
        "pool_size": info["pool_size"],
        "league_max_gp": info["max_gp"],
        "early_season": (info["max_gp"] or 0) < config.EARLY_SEASON_GP,
    }


@app.get("/api/players/{player_id}/shots")
def player_shots(player_id: int, season: str | None = None,
                 last: int | None = Query(None, ge=1, le=100, description="only the player's last N games with a shot"),
                 period: str | None = Query(None, pattern="^(1|2|3|4|OT)$"),
                 loc: str | None = Query(None, pattern="^(home|away)$")):
    """Shots, hot-zone hexes and zone splits for one player-season, optionally
    filtered by last N games, quarter and home/away. Hexes and zones always
    compare against the full-season league average."""
    season = resolve_season(season)
    where, params = ["player_id = ?", "season = ?"], [player_id, season]
    if period == "OT":
        where.append("period >= 5")
    elif period:
        where.append("period = ?")
        params.append(int(period))
    if loc:
        where.append("is_home = ?")
        params.append(1 if loc == "home" else 0)
    if last:
        where.append("""game_id IN (SELECT game_id FROM shots WHERE player_id = ? AND season = ?
                                    GROUP BY game_id ORDER BY MAX(game_date) DESC, game_id DESC LIMIT ?)""")
        params += [player_id, season, last]
    shots = rows(f"""SELECT loc_x x, loc_y y, made m, zone_basic z, distance d, action_type a,
                            game_date g, period q, COALESCE(is_home, 0) h
                     FROM shots WHERE {' AND '.join(where)}""", params)

    zones, hexes = [], []
    if shots:
        league_z = {z["zone_basic"]: z["fg_pct"] for z in rows(
            "SELECT zone_basic, fg_pct FROM league_zone_averages WHERE season = ?", (season,))}
        agg: dict = {}
        for s in shots:
            if s["z"] is not None:
                a = agg.setdefault(s["z"], [0, 0])
                a[0] += 1
                a[1] += s["m"]
        for z, (fga, fgm) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            zones.append({"zone_basic": z, "fga": fga, "fgm": fgm, "fg_pct": fgm / fga,
                          "league_fg_pct": league_z.get(z), "no_league_avg": z in metrics.NO_LEAGUE_AVG_ZONES})

        mine = metrics.hexbin(pd.DataFrame(shots).rename(columns={"x": "loc_x", "y": "loc_y", "m": "made"}))
        # The whole season's league grid is a few hundred hexes; one indexed range read.
        league = {(r["q"], r["r"]): r for r in rows(
            "SELECT q, r, fga, fgm FROM league_hex_bins WHERE season = ?", (season,))}
        for h in mine.to_dict("records"):
            lg = league.get((int(h["q"]), int(h["r"])))
            hexes.append({
                "x": round(h["x"], 1), "y": round(h["y"], 1),
                "fga": int(h["fga"]), "fgm": int(h["fgm"]),
                "league_pct": round(lg["fgm"] / lg["fga"], 4) if lg and lg["fga"] else None,
            })
    return {"season": season, "hex_radius": metrics.HEX_R, "shots": shots, "hexes": hexes, "zones": zones,
            "games": len({s["g"] for s in shots}),
            "filters": {"last": last, "period": period, "loc": loc}}


@app.get("/api/players/{player_id}/games")
def player_games(player_id: int, season: str | None = None):
    """Game log for one season, oldest first, with each game's TS% and the
    rolling 5-game PTS and TS% (TS% from 5-game totals, not an average of
    single-game percentages)."""
    season = resolve_season(season)
    df = db.frame(conn(), """
        SELECT l.game_id, g.game_date, l.team_id,
               CASE WHEN g.home_team_id = l.team_id THEN 1 ELSE 0 END AS home,
               CASE WHEN g.home_team_id = l.team_id THEN ta.abbr ELSE th.abbr END AS opp,
               CASE WHEN g.home_team_id = l.team_id THEN g.home_pts - g.away_pts
                    ELSE g.away_pts - g.home_pts END AS margin,
               l.min, l.pts, l.reb, l.ast, l.stl, l.blk, l.tov,
               l.fgm, l.fga, l.fg3m, l.fg3a, l.ftm, l.fta, l.plus_minus
        FROM player_game_logs l INDEXED BY idx_game_logs_player_season  -- one season, not the whole career
        JOIN games g USING (game_id)
        LEFT JOIN teams th ON th.team_id = g.home_team_id
        LEFT JOIN teams ta ON ta.team_id = g.away_team_id
        WHERE l.player_id = ? AND l.season = ?
        ORDER BY g.game_date, l.game_id""", (player_id, season))
    df = metrics.rolling_trends(df)
    out = [{k: clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    return {"season": season, "available": True, "window": metrics.TREND_WINDOW, "games": out}


@app.get("/api/players/{player_id}/career")
def player_career(player_id: int):
    """Every slider's percentile in every season the player has stats, each
    ranked against that season's qualified pool."""
    by_season = rows(f"""SELECT season, qualified, {cols(SLIDER_COLS)} FROM player_season_summary
                         WHERE player_id = ? ORDER BY season""", (player_id,))
    if not by_season:
        raise HTTPException(404, "No stats for this player")
    out = []
    for m in metrics.METRICS:
        cells = []
        for r in by_season:
            value = r[m.key]
            sample = r[m.sample_col] if m.sample_col else None
            cells.append({"season": r["season"], "value": value, "percentile": r[f"p_{m.key}"],
                          "low_sample": low_sample(m, value, sample)})
        out.append({"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt, "cells": cells})
    return {"seasons": [r["season"] for r in by_season],
            "qualified": {r["season"]: bool(r["qualified"]) for r in by_season}, "metrics": out}


GAME_STATS = ["pts", "ast", "reb", "ts_pct", "usg_pct", "ast_pct", "reb_pct", "def_rating"]


def daily_pools(through: dt.date) -> dict:
    """{week start: {season, ids}} from the epoch's week through `through`'s.
    Stored weeks come from games_daily_pool; any week the weekly job hasn't
    stored yet is computed from game logs (same function, same answer)."""
    first = games.week_start(dt.date.fromisoformat(config.GAMES_EPOCH))
    last = games.week_start(through)
    stored = {r["week"]: {"season": r["season"], "ids": json.loads(r["ids"])} for r in rows(
        "SELECT week, season, ids FROM games_daily_pool WHERE week >= ? AND week <= ?",
        (first.isoformat(), last.isoformat()))}
    out, week = {}, first
    while week <= last:
        out[week.isoformat()] = stored.get(week.isoformat()) or games.daily_pool(conn(), week)
        week += dt.timedelta(days=7)
    return out


@app.get("/api/games/pool")
def games_pool(day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                       description="the caller's UTC date; only makes each day's URL distinct for caches")):
    """Everything the games pages need, in one request:
    - players: this season's pool (games.eligible) with bio and Higher or Lower stats
    - names: every player with stats this season (Guess the Player's search list)
    - daily_pools: frozen weekly pools for the daily puzzle (see hoopscholar/games.py)"""
    season = resolve_season(None)

    def build():
        players = rows(f"""
            SELECT s.player_id, s.full_name, COALESCE(s.team_abbr, p.team_abbr) AS team_abbr, s.position,
                   p.height, p.jersey, {cols('s.' + k for k in GAME_STATS)}
            FROM player_season_summary s JOIN players p USING (player_id)
            WHERE s.season = ? AND s.games_eligible = 1 ORDER BY s.player_id""", (season,))
        # Through tomorrow in UTC, so every time zone's "today" is covered.
        through = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
        daily = daily_pools(through)
        # Guess list: everyone with stats this season, plus any season a daily
        # pool falls back to, so the answer is always guessable. Each name shows
        # the team from their latest of those seasons.
        name_seasons = sorted({season, *(w["season"] for w in daily.values())})
        latest: dict = {}
        for r in rows(f"""SELECT player_id, full_name, team_abbr FROM player_season_summary
                          WHERE season IN ({",".join("?" * len(name_seasons))}) ORDER BY season""", name_seasons):
            latest[r["player_id"]] = r
        names = sorted(latest.values(), key=lambda r: r["full_name"] or "")
        return {"season": season, "min_per_team_game": config.GAMES_MIN_PER_TEAM_GAME,
                "epoch": config.GAMES_EPOCH, "no_repeat_days": config.GAMES_NO_REPEAT_DAYS,
                "stats": GAME_STATS, "players": players, "names": names, "daily_pools": daily}
    # The cache key includes the day, so daily_pools picks up a new week on its own.
    return cached("games_pool:" + dt.datetime.now(dt.timezone.utc).date().isoformat(), season, build)


# ------------------------------------------------------------------ static site

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
