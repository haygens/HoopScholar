"""HoopScholar API + static site.

    uvicorn api.main:app --reload                      # uses data/hoopscholar.db
    HOOP_DB=data/demo.db uvicorn api.main:app --reload # demo data

Then open http://127.0.0.1:8000
"""
import datetime as dt
import sqlite3
import sys
import threading
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoopscholar import config, db, games, metrics  # noqa: E402

app = FastAPI(title="HoopScholar")
_local = threading.local()


def conn():
    """One read-only-ish SQLite connection per worker thread."""
    if not hasattr(_local, "conn"):
        _local.conn = db.connect(readonly=True)
    return _local.conn


# ------------------------------------------------------------------ caching
# Percentiles and league hex bins only change when ingest runs, so cache them
# keyed on (season, meta.last_updated).
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


def season_data(season: str):
    def build():
        frame = metrics.season_frame(conn(), season)
        pcts = metrics.percentiles(frame) if not frame.empty else pd.DataFrame()
        return frame, pcts
    return cached("season", season, build)


def league_hexes(season: str) -> pd.DataFrame:
    def build():
        shots = pd.read_sql_query("SELECT loc_x, loc_y, made FROM shots WHERE season=?", conn(), params=(season,))
        return metrics.hexbin(shots).set_index(["q", "r"]) if not shots.empty else pd.DataFrame()
    return cached("hex", season, build)


def league_zones(season: str) -> dict:
    return cached("zones", season, lambda: metrics.league_zone_averages(conn(), season))


# ------------------------------------------------------------------ helpers

def seasons() -> list[str]:
    return [r[0] for r in conn().execute(
        "SELECT DISTINCT season FROM player_season_stats ORDER BY season DESC")]


def resolve_season(season: str | None) -> str:
    all_s = seasons()
    if not all_s:
        raise HTTPException(503, "No data loaded yet. Run scripts/backfill.py or scripts/make_demo_data.py.")
    if season is None:
        return all_s[0]
    if season not in all_s:
        raise HTTPException(404, f"Season {season} not loaded. Have: {', '.join(all_s)}")
    return season


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def metric_meta(ms):
    return [{"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt,
             "higher_better": m.higher_better} for m in ms]


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
def search(q: str = Query(..., min_length=1), limit: int = 10):
    """Teams (by abbreviation or name) first, then players with stats."""
    c = conn()
    teams = [{"kind": "team", **dict(r)} for r in c.execute("""
        SELECT abbr, name FROM teams
        WHERE abbr LIKE ? OR name LIKE ?
        ORDER BY (abbr LIKE ?) DESC, name LIMIT 3""", (f"{q}%", f"%{q}%", q))]
    players = [{"kind": "player", **dict(r)} for r in c.execute("""
        SELECT * FROM (
            SELECT p.player_id, p.full_name, p.team_abbr, p.position,
                   (SELECT MAX(season) FROM player_season_stats s WHERE s.player_id = p.player_id) AS last_season
            FROM players p
            WHERE p.full_name LIKE ? OR p.last_name LIKE ?)
        WHERE last_season IS NOT NULL
        ORDER BY (full_name LIKE ?) DESC, last_season DESC, full_name
        LIMIT ?""", (f"%{q}%", f"{q}%", f"{q}%", limit))]
    return (teams + players)[:limit]


@app.get("/api/teams/{abbr}")
def team(abbr: str, season: str | None = None):
    """Roster for one team-season: everyone whose season stat line is with this
    team, by minutes, with each player's top 3 percentiles."""
    c = conn()
    t = c.execute("SELECT team_id, abbr, name FROM teams WHERE abbr = ? COLLATE NOCASE", (abbr,)).fetchone()
    if not t:
        raise HTTPException(404, "Team not found")
    team_seasons = [r[0] for r in c.execute(
        "SELECT DISTINCT season FROM player_season_stats WHERE team_abbr = ? ORDER BY season DESC", (t["abbr"],))]
    if not team_seasons:
        raise HTTPException(404, "No stats for this team")
    season = season if season in team_seasons else team_seasons[0]

    frame, pcts = season_data(season)
    roster = frame[frame["team_abbr"] == t["abbr"]].sort_values("min", ascending=False)
    pidx = pcts.set_index("player_id")
    tov_min = metrics.min_tov_opps(frame)
    rows = []
    for _, r in roster.iterrows():
        rows.append({
            **{k: clean(r.get(k)) for k in ("player_id", "full_name", "position", "gp", "min",
                                             "pts", "reb", "ast", "ts_pct")},
            "qualified": bool(r["qualified"]),
            "top": metrics.top_percentiles(r, pidx.loc[r["player_id"]], tov_min=tov_min),
        })
    return {"team": dict(t), "season": season, "seasons": team_seasons, "roster": rows}


@app.get("/api/leaderboard")
def leaderboard(stat: str = "pts", season: str | None = None, qualified: bool = True,
                limit: int = Query(50, le=500), asc: bool | None = None):
    if stat not in metrics.SORTABLE:
        raise HTTPException(400, f"Unknown stat {stat}")
    season = resolve_season(season)
    frame, pcts = season_data(season)
    m = metrics.SORTABLE[stat]
    df = frame[frame["qualified"]] if qualified else frame
    df = df.dropna(subset=[stat])
    df = df[metrics.rank_mask(df, m, frame)]  # zone FG%, TOV% and TS%: sample minimums
    ascending = (not m.higher_better) if asc is None else asc
    df = df.sort_values(stat, ascending=ascending).head(limit)
    pct_lookup = pcts.set_index("player_id")[stat] if stat in pcts else None
    cols = ["player_id", "full_name", "team_abbr", "position", "gp", "min", "pts", "reb", "ast", "ts_pct", stat]
    out = []
    for rank, r in enumerate(df[list(dict.fromkeys(cols))].to_dict("records"), 1):
        row = {k: clean(v) for k, v in r.items()}
        row["rank"] = rank
        row["value"] = row[stat]
        row["percentile"] = clean(pct_lookup.get(r["player_id"])) if pct_lookup is not None else None
        out.append(row)
    return {"season": season, "stat": stat, "qualified_only": qualified, "rows": out,
            "minimum": metrics.rank_minimum(m, frame)}


@app.get("/api/players/{player_id}")
def player(player_id: int, season: str | None = None):
    c = conn()
    bio = c.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
    if not bio:
        raise HTTPException(404, "Player not found")
    player_seasons = [r[0] for r in c.execute(
        "SELECT season FROM player_season_stats WHERE player_id=? ORDER BY season DESC", (player_id,))]
    if not player_seasons:
        raise HTTPException(404, "No stats for this player")
    season = season if season in player_seasons else player_seasons[0]

    frame, pcts = season_data(season)
    row = frame[frame["player_id"] == player_id].iloc[0]
    prow = pcts[pcts["player_id"] == player_id].iloc[0]

    sliders = []
    for m in metrics.METRICS:
        sample = clean(row.get(m.sample_col)) if m.sample_col else None
        sample = int(sample) if sample is not None else None
        value = clean(row.get(m.key))
        sliders.append({"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt,
                        "value": value, "percentile": clean(prow.get(m.key)),
                        "sample": sample,
                        "low_sample": bool(m.sample_col and value is not None
                                           and (sample or 0) < config.ZONE_MIN_FGA)})

    zones_league = league_zones(season)
    zones = c.execute("""SELECT zone_basic, COUNT(*) fga, SUM(made) fgm, AVG(made) fg_pct
                         FROM shots WHERE player_id=? AND season=? GROUP BY zone_basic
                         ORDER BY COUNT(*) DESC""", (player_id, season)).fetchall()
    zone_rows = [{**dict(z),
                  "league_fg_pct": zones_league.get(z["zone_basic"], {}).get("fg_pct"),
                  "no_league_avg": z["zone_basic"] in metrics.NO_LEAGUE_AVG_ZONES} for z in zones]

    career = [dict(r) for r in c.execute("""
        SELECT season, team_abbr, gp, min, pts, reb, ast, stl, blk, tov, fg_pct, fg3_pct, ft_pct, ts_pct
        FROM player_season_stats WHERE player_id=? ORDER BY season""", (player_id,))]

    try:
        similar = [dict(r) for r in c.execute("""
            SELECT ps.similar_id AS player_id, ps.score, p.full_name, s.team_abbr, p.position
            FROM player_similar ps
            JOIN players p ON p.player_id = ps.similar_id
            LEFT JOIN player_season_stats s ON s.player_id = ps.similar_id AND s.season = ps.season
            WHERE ps.player_id = ? AND ps.season = ? ORDER BY ps.rank""", (player_id, season))]
    except sqlite3.OperationalError:  # older database without the table
        similar = []

    return {
        "player": dict(bio),
        "season": season,
        "seasons": player_seasons,
        "qualified": bool(row["qualified"]),
        "stats": {k: clean(v) for k, v in row.items()},
        "sliders": sliders,
        "zones": zone_rows,
        "career": career,
        "similar": similar,
        "pool_size": int(frame["qualified"].sum()),
        "league_max_gp": int(frame["gp"].max()),
        "early_season": bool(frame["gp"].max() < config.EARLY_SEASON_GP),
    }


@app.get("/api/players/{player_id}/shots")
def player_shots(player_id: int, season: str | None = None,
                 last: int | None = Query(None, ge=1, le=100, description="only the player's last N games with a shot"),
                 period: str | None = Query(None, pattern="^(1|2|3|4|OT)$"),
                 loc: str | None = Query(None, pattern="^(home|away)$")):
    """Shots, hot-zone hexes and zone splits for one player-season, optionally
    filtered. No extra columns are needed: period is stored on each shot and
    home/away comes from games. Hexes and zones still compare against the
    full-season league average."""
    season = resolve_season(season)
    where, params = ["s.player_id = ?", "s.season = ?"], [player_id, season]
    if period == "OT":
        where.append("s.period >= 5")
    elif period:
        where.append("s.period = ?")
        params.append(int(period))
    if loc:
        where.append(f"g.home_team_id {'=' if loc == 'home' else '!='} s.team_id")
    if last:
        where.append("""s.game_id IN (SELECT game_id FROM shots WHERE player_id = ? AND season = ?
                                      GROUP BY game_id ORDER BY MAX(game_date) DESC, game_id DESC LIMIT ?)""")
        params += [player_id, season, last]
    rows = conn().execute(f"""SELECT s.loc_x x, s.loc_y y, s.made m, s.zone_basic z, s.distance d,
                                     s.action_type a, s.game_date g, s.period q,
                                     CASE WHEN g.home_team_id = s.team_id THEN 1 ELSE 0 END h
                              FROM shots s LEFT JOIN games g USING (game_id)
                              WHERE {' AND '.join(where)}""", params).fetchall()
    shots = [dict(r) for r in rows]

    league_z = league_zones(season)
    zones = []
    if shots:
        z = pd.DataFrame(shots).groupby("z")["m"].agg(fga="size", fgm="sum").reset_index()
        for r in z.sort_values("fga", ascending=False).to_dict("records"):
            zones.append({"zone_basic": r["z"], "fga": int(r["fga"]), "fgm": int(r["fgm"]),
                          "fg_pct": r["fgm"] / r["fga"],
                          "league_fg_pct": league_z.get(r["z"], {}).get("fg_pct"),
                          "no_league_avg": r["z"] in metrics.NO_LEAGUE_AVG_ZONES})

    hexes = []
    if shots:
        mine = metrics.hexbin(pd.DataFrame(shots).rename(columns={"x": "loc_x", "y": "loc_y", "m": "made"}))
        league = league_hexes(season)
        for h in mine.to_dict("records"):
            lg = league.loc[(h["q"], h["r"])] if (h["q"], h["r"]) in league.index else None
            hexes.append({
                "x": round(h["x"], 1), "y": round(h["y"], 1),
                "fga": int(h["fga"]), "fgm": int(h["fgm"]),
                "league_pct": round(float(lg["fgm"] / lg["fga"]), 4) if lg is not None else None,
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
    try:
        df = pd.read_sql_query("""
            SELECT l.game_id, g.game_date, l.team_id,
                   CASE WHEN g.home_team_id = l.team_id THEN 1 ELSE 0 END AS home,
                   CASE WHEN g.home_team_id = l.team_id THEN ta.abbr ELSE th.abbr END AS opp,
                   CASE WHEN g.home_team_id = l.team_id THEN g.home_pts - g.away_pts
                        ELSE g.away_pts - g.home_pts END AS margin,
                   l.min, l.pts, l.reb, l.ast, l.stl, l.blk, l.tov,
                   l.fgm, l.fga, l.fg3m, l.fg3a, l.ftm, l.fta, l.plus_minus
            FROM player_game_logs l
            JOIN games g USING (game_id)
            LEFT JOIN teams th ON th.team_id = g.home_team_id
            LEFT JOIN teams ta ON ta.team_id = g.away_team_id
            WHERE l.player_id = ? AND l.season = ?
            ORDER BY g.game_date, l.game_id""", conn(), params=(player_id, season))
    except pd.errors.DatabaseError:  # older database without player_game_logs
        return {"season": season, "available": False, "window": metrics.TREND_WINDOW, "games": []}
    df = metrics.rolling_trends(df)
    games = [{k: clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    return {"season": season, "available": True, "window": metrics.TREND_WINDOW, "games": games}


@app.get("/api/players/{player_id}/career")
def player_career(player_id: int):
    """Every slider's percentile in every season the player has stats, each
    ranked against that season's qualified pool."""
    player_seasons = [r[0] for r in conn().execute(
        "SELECT season FROM player_season_stats WHERE player_id=? ORDER BY season", (player_id,))]
    if not player_seasons:
        raise HTTPException(404, "No stats for this player")
    by_season, qualified = {}, {}
    for s in player_seasons:
        frame, pcts = season_data(s)
        row = frame[frame["player_id"] == player_id].iloc[0]
        prow = pcts[pcts["player_id"] == player_id].iloc[0]
        qualified[s] = bool(row["qualified"])
        by_season[s] = (row, prow)
    out = []
    for m in metrics.METRICS:
        cells = []
        for s in player_seasons:
            row, prow = by_season[s]
            value = clean(row.get(m.key))
            sample = clean(row.get(m.sample_col)) if m.sample_col else None
            cells.append({"season": s, "value": value, "percentile": clean(prow.get(m.key)),
                          "low_sample": bool(m.sample_col and value is not None
                                             and (sample or 0) < config.ZONE_MIN_FGA)})
        out.append({"key": m.key, "label": m.label, "group": m.group, "fmt": m.fmt, "cells": cells})
    return {"seasons": player_seasons, "qualified": qualified, "metrics": out}


GAME_STATS = ["pts", "ast", "reb", "ts_pct", "usg_pct", "ast_pct", "reb_pct", "def_rating"]


@app.get("/api/games/pool")
def games_pool():
    """Everything the games pages need, in one request:
    - players: this season's pool (games.eligible) with bio and Higher or Lower stats
    - names: every player with stats this season (Guess the Player's search list)
    - daily_pools: frozen weekly pools for the daily puzzle (see hoopscholar/games.py)"""
    season = resolve_season(None)

    def build():
        c = conn()
        pool = games.eligible(c, season)
        frame, _ = season_data(season)
        bio = pd.read_sql_query("SELECT player_id, team_abbr AS bio_team, jersey, height FROM players", c)
        df = (frame[["player_id", "full_name", "team_abbr", "position", *GAME_STATS]]
              .merge(bio, on="player_id", how="left"))
        df["team_abbr"] = df["team_abbr"].fillna(df["bio_team"])
        cols = ["player_id", "full_name", "team_abbr", "position", "height", "jersey", *GAME_STATS]
        players = [{k: clean(v) for k, v in r.items()}
                   for r in df[df["player_id"].isin(pool["player_id"])][cols].to_dict("records")]
        # Through tomorrow in UTC, so every time zone's "today" is covered.
        through = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
        daily = games.daily_pools(c, through)
        # Guess list: everyone with stats this season, plus any season a daily
        # pool falls back to, so the answer is always guessable. Each name shows
        # the team from their latest of those seasons.
        name_seasons = sorted({season, *(w["season"] for w in daily.values())})
        names = pd.read_sql_query(f"""
            SELECT s.player_id, p.full_name, s.team_abbr FROM player_season_stats s JOIN players p USING (player_id)
            WHERE s.season IN ({",".join("?" * len(name_seasons))}) ORDER BY s.season""", c, params=name_seasons)
        names = names.drop_duplicates("player_id", keep="last").sort_values("full_name")
        names = [{"player_id": int(r.player_id), "full_name": r.full_name, "team_abbr": clean(r.team_abbr)}
                 for r in names.itertuples()]
        return {"season": season, "min_per_team_game": config.GAMES_MIN_PER_TEAM_GAME,
                "epoch": config.GAMES_EPOCH, "no_repeat_days": config.GAMES_NO_REPEAT_DAYS,
                "stats": GAME_STATS, "players": players, "names": names, "daily_pools": daily}
    # The cache key includes the day, so daily_pools picks up a new week on its own.
    return cached("games_pool:" + dt.date.today().isoformat(), season, build)


# ------------------------------------------------------------------ static site

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
