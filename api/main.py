"""HoopScholar API + static site.

    uvicorn api.main:app --reload                      # uses data/hoopscholar.db
    HOOP_DB=data/demo.db uvicorn api.main:app --reload # demo data

Then open http://127.0.0.1:8000
"""
import sys
import threading
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoopscholar import config, db, metrics  # noqa: E402

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
    rows = conn().execute("""
        SELECT p.player_id, p.full_name, p.team_abbr, p.position,
               (SELECT MAX(season) FROM player_season_stats s WHERE s.player_id = p.player_id) AS last_season
        FROM players p
        WHERE p.full_name LIKE ? OR p.last_name LIKE ?
        ORDER BY (p.full_name LIKE ?) DESC, last_season DESC, p.full_name
        LIMIT ?""", (f"%{q}%", f"{q}%", f"{q}%", limit)).fetchall()
    return [dict(r) for r in rows if r["last_season"]]


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
    if m.sample_col:  # zone FG%: same attempt minimum as the percentile pool
        df = df[metrics.sample_ok(df, m, metrics.pool_min_fga(frame))]
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
    return {"season": season, "stat": stat, "qualified_only": qualified, "rows": out}


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

    return {
        "player": dict(bio),
        "season": season,
        "seasons": player_seasons,
        "qualified": bool(row["qualified"]),
        "stats": {k: clean(v) for k, v in row.items()},
        "sliders": sliders,
        "zones": zone_rows,
        "career": career,
        "pool_size": int(frame["qualified"].sum()),
    }


@app.get("/api/players/{player_id}/shots")
def player_shots(player_id: int, season: str | None = None):
    season = resolve_season(season)
    rows = conn().execute("""SELECT loc_x x, loc_y y, made m, zone_basic z, distance d, action_type a, game_date g
                             FROM shots WHERE player_id=? AND season=?""", (player_id, season)).fetchall()
    shots = [dict(r) for r in rows]

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
    return {"season": season, "hex_radius": metrics.HEX_R, "shots": shots, "hexes": hexes}


# ------------------------------------------------------------------ static site

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
