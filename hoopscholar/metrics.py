"""Derived numbers: shot profiles, qualification, percentiles, hex bins."""
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    fmt: str = "num1"            # 'num1' 12.3 | 'pct' 0.583 -> 58.3% | 'int'
    higher_better: bool = True


# What shows up as Savant-style percentile sliders, in display order.
METRICS: list[Metric] = [
    Metric("pts", "Points", "Scoring"),
    Metric("ts_pct", "True Shooting %", "Scoring", "pct"),
    Metric("usg_pct", "Usage %", "Scoring", "pct"),
    Metric("rim_fg_pct", "Rim FG%", "Shooting", "pct"),
    Metric("mid_fg_pct", "Mid-Range FG%", "Shooting", "pct"),
    Metric("fg3_pct", "3PT%", "Shooting", "pct"),
    Metric("corner3_pct", "Corner 3 %", "Shooting", "pct"),
    Metric("ft_pct", "FT%", "Shooting", "pct"),
    Metric("ast", "Assists", "Playmaking"),
    Metric("ast_pct", "Assist %", "Playmaking", "pct"),
    Metric("tov", "Turnovers", "Playmaking", higher_better=False),
    Metric("reb_pct", "Rebound %", "Defense & Boards", "pct"),
    Metric("stl", "Steals", "Defense & Boards"),
    Metric("blk", "Blocks", "Defense & Boards"),
    Metric("def_rating", "Defensive Rating", "Defense & Boards", higher_better=False),
    Metric("net_rating", "Net Rating", "Impact"),
    Metric("pie", "PIE", "Impact", "pct"),
    Metric("dist_miles", "Miles Run / Game", "Movement", "num2"),
    Metric("avg_speed", "Avg Speed (mph)", "Movement", "num2"),
]
METRIC_BY_KEY = {m.key: m for m in METRICS}

# Extra columns that can be sorted on the leaderboard but aren't sliders.
LEADERBOARD_EXTRA = [
    Metric("reb", "Rebounds", "Box"),
    Metric("min", "Minutes", "Box"),
    Metric("fga", "FGA", "Box"),
    Metric("efg_pct", "eFG%", "Box", "pct"),
    Metric("rim_rate", "Rim Rate", "Shot Profile", "pct"),
    Metric("three_rate", "3PA Rate", "Shot Profile", "pct"),
    Metric("avg_distance", "Avg Shot Dist (ft)", "Shot Profile"),
]
SORTABLE = {m.key: m for m in METRICS + LEADERBOARD_EXTRA}


# ---------------------------------------------------------------- shot profiles

def refresh_shot_profiles(conn, season: str) -> int:
    """Rebuild player_shot_profile for a season from the shots table."""
    m = config.ZONE_MIN_FGA

    def pct(zone_cond: str) -> str:
        return (f"CASE WHEN SUM(CASE WHEN {zone_cond} THEN 1 ELSE 0 END) >= {m} "
                f"THEN 1.0 * SUM(CASE WHEN {zone_cond} THEN made ELSE 0 END) / "
                f"SUM(CASE WHEN {zone_cond} THEN 1 ELSE 0 END) END")

    def cnt(zone_cond: str) -> str:
        return f"SUM(CASE WHEN {zone_cond} THEN 1 ELSE 0 END)"

    rim = "zone_basic = 'Restricted Area'"
    paint = "zone_basic = 'In The Paint (Non-RA)'"
    mid = "zone_basic = 'Mid-Range'"
    corner = "zone_basic IN ('Left Corner 3', 'Right Corner 3')"
    atb = "zone_basic = 'Above the Break 3'"
    three = "shot_type = '3PT Field Goal'"
    conn.execute("DELETE FROM player_shot_profile WHERE season = ?", (season,))
    conn.execute(f"""
        INSERT INTO player_shot_profile
        SELECT player_id, season, COUNT(*), AVG(distance),
               {cnt(rim)}, {pct(rim)}, {cnt(paint)}, {pct(paint)},
               {cnt(mid)}, {pct(mid)}, {cnt(corner)}, {pct(corner)},
               {cnt(atb)}, {pct(atb)},
               1.0 * {cnt(rim)} / COUNT(*), 1.0 * {cnt(three)} / COUNT(*)
        FROM shots WHERE season = ? GROUP BY player_id, season
    """, (season,))
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM player_shot_profile WHERE season=?", (season,)).fetchone()[0]


# ---------------------------------------------------------------- season frame + percentiles

def season_frame(conn, season: str) -> pd.DataFrame:
    """Every player-season with stats, shot profile, name, and a qualified flag."""
    df = pd.read_sql_query("""
        SELECT s.*, p.full_name, p.position,
               sp.avg_distance, sp.rim_fg_pct, sp.mid_fg_pct, sp.corner3_pct, sp.atb3_pct,
               sp.paint_fg_pct, sp.rim_rate, sp.three_rate
        FROM player_season_stats s
        JOIN players p USING (player_id)
        LEFT JOIN player_shot_profile sp USING (player_id, season)
        WHERE s.season = ?
    """, conn, params=(season,))
    if df.empty:
        df["qualified"] = []
        return df
    max_gp = df["gp"].max() or 0
    df["qualified"] = (df["gp"] >= config.QUAL_GP_SHARE * max_gp) & (df["min"] >= config.QUAL_MIN_MPG)
    return df


def percentiles(frame: pd.DataFrame) -> pd.DataFrame:
    """Percentile (0-100) of every player on every METRIC, ranked against
    qualified players only. Lower-is-better metrics are flipped so 100 is always good."""
    pool = frame[frame["qualified"]]
    out = pd.DataFrame({"player_id": frame["player_id"]})
    for m in METRICS:
        if m.key not in frame:
            continue
        ref = np.sort(pool[m.key].dropna().to_numpy())
        vals = frame[m.key].to_numpy(dtype=float)
        if len(ref) < 2:
            out[m.key] = np.nan
            continue
        below = np.searchsorted(ref, vals, side="left")
        at_or_below = np.searchsorted(ref, vals, side="right")
        # midpoint rank handles ties; scale so the worst qualified player is 0 and the best is 100
        rank = (below + at_or_below - 1) / 2
        pct = np.clip(100 * rank / (len(ref) - 1), 0, 100)
        if not m.higher_better:
            pct = 100 - pct
        pct[np.isnan(vals)] = np.nan
        out[m.key] = np.round(pct)
    return out


# ---------------------------------------------------------------- hex bins

HEX_R = 20.0  # 2 ft hexagons
_SQRT3 = math.sqrt(3)


def hex_ids(x: np.ndarray, y: np.ndarray, r: float = HEX_R):
    """Pointy-top axial hex coordinates (q, r) for each point."""
    q = (_SQRT3 / 3 * x - y / 3) / r
    rr = (2 / 3 * y) / r
    # cube rounding
    cx, cz = q, rr
    cy = -cx - cz
    rx, ry, rz = np.round(cx), np.round(cy), np.round(cz)
    dx, dy, dz = np.abs(rx - cx), np.abs(ry - cy), np.abs(rz - cz)
    fix_x = (dx > dy) & (dx > dz)
    fix_z = ~fix_x & (dz >= dy)
    rx = np.where(fix_x, -ry - rz, rx)
    rz = np.where(fix_z, -rx - ry, rz)
    return rx.astype(int), rz.astype(int)


def hex_center(q, r, size: float = HEX_R):
    return size * _SQRT3 * (q + r / 2), size * 1.5 * r


def hexbin(shots: pd.DataFrame) -> pd.DataFrame:
    """shots with loc_x, loc_y, made -> per-hex fga/fgm."""
    s = shots[shots["loc_y"] <= 422.5]
    q, r = hex_ids(s["loc_x"].to_numpy(float), s["loc_y"].to_numpy(float))
    g = pd.DataFrame({"q": q, "r": r, "made": s["made"].to_numpy()}).groupby(["q", "r"])["made"]
    out = pd.DataFrame({"fga": g.size(), "fgm": g.sum()}).reset_index()
    out["x"], out["y"] = hex_center(out["q"], out["r"])
    return out
