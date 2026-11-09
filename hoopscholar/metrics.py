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
    fmt: str = "num1"            # 'num1' 12.3 | 'pct' 0.583 -> 58.3% | 'int'  | 'pct100' 9.9 -> 9.9%
    higher_better: bool = True
    sample_col: str | None = None  # attempts behind a rate stat (low-sample handling)


# What shows up as Savant-style percentile sliders, in display order.
METRICS: list[Metric] = [
    Metric("pts", "Points", "Scoring"),
    Metric("ts_pct", "True Shooting %", "Scoring", "pct"),
    Metric("usg_pct", "Usage %", "Scoring", "pct"),
    Metric("rim_fg_pct", "Rim FG%", "Shooting", "pct", sample_col="rim_fga"),
    Metric("mid_fg_pct", "Mid-Range FG%", "Shooting", "pct", sample_col="mid_fga"),
    Metric("fg3_pct", "3PT%", "Shooting", "pct"),
    Metric("corner3_pct", "Corner 3 %", "Shooting", "pct", sample_col="corner3_fga"),
    Metric("ft_pct", "FT%", "Shooting", "pct"),
    Metric("ast", "Assists", "Playmaking"),
    Metric("ast_pct", "Assist %", "Playmaking", "pct"),
    Metric("tov_pct", "TOV%", "Playmaking", "pct100", higher_better=False),
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
    Metric("tov", "Turnovers", "Box", higher_better=False),
    Metric("fga", "FGA", "Box"),
    Metric("efg_pct", "eFG%", "Box", "pct"),
    Metric("rim_rate", "Rim Rate", "Shot Profile", "pct"),
    Metric("three_rate", "3PA Rate", "Shot Profile", "pct"),
    Metric("avg_distance", "Avg Shot Dist (ft)", "Shot Profile"),
]
SORTABLE = {m.key: m for m in METRICS + LEADERBOARD_EXTRA}


# ---------------------------------------------------------------- shot profiles

def refresh_shot_profiles(conn, season: str) -> int:
    """Rebuild player_shot_profile for a season from the shots table.

    Zone percentages are stored for any number of attempts (NULL only at 0);
    small samples are flagged when read, see low_sample_mask()."""
    def pct(zone_cond: str) -> str:
        return (f"1.0 * SUM(CASE WHEN {zone_cond} THEN made ELSE 0 END) / "
                f"NULLIF(SUM(CASE WHEN {zone_cond} THEN 1 ELSE 0 END), 0)")

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
            (player_id, season, fga, avg_distance, rim_fga, rim_fg_pct, paint_fga, paint_fg_pct,
             mid_fga, mid_fg_pct, corner3_fga, corner3_pct, atb3_fga, atb3_pct, rim_rate, three_rate)
        SELECT player_id, season, COUNT(*), AVG(distance),
               {cnt(rim)}, {pct(rim)}, {cnt(paint)}, {pct(paint)},
               {cnt(mid)}, {pct(mid)}, {cnt(corner)}, {pct(corner)},
               {cnt(atb)}, {pct(atb)},
               1.0 * {cnt(rim)} / COUNT(*), 1.0 * {cnt(three)} / COUNT(*)
        FROM shots WHERE season = ? GROUP BY player_id, season
    """, (season,))
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM player_shot_profile WHERE season=?", (season,)).fetchone()[0]


# ---------------------------------------------------------------- league zone averages

# Zones where NBA.com's data can't support a league average. Official FGA leaves
# out most missed end-of-period heaves, so the Backcourt shots that remain are a
# few dozen a season and mostly makes (2026-27 at launch: 6 of 8).
NO_LEAGUE_AVG_ZONES = {"Backcourt"}


def league_zone_averages(conn, season: str) -> dict:
    """{zone_basic: {fga, fgm, fg_pct}} over every league shot in the season.
    fg_pct is total made / total attempted (not an average of player averages),
    and None for zones in NO_LEAGUE_AVG_ZONES."""
    rows = conn.execute("""SELECT zone_basic, COUNT(*) AS fga, SUM(made) AS fgm
                           FROM shots WHERE season = ? GROUP BY zone_basic""", (season,)).fetchall()
    out = {}
    for zone, fga, fgm in rows:
        pct = None if zone in NO_LEAGUE_AVG_ZONES or not fga else fgm / fga
        out[zone] = {"fga": fga, "fgm": fgm, "fg_pct": pct}
    return out


# ---------------------------------------------------------------- season frame + percentiles

def season_frame(conn, season: str) -> pd.DataFrame:
    """Every player-season with stats, shot profile, name, and a qualified flag."""
    df = pd.read_sql_query("""
        SELECT s.*, p.full_name, p.position,
               sp.avg_distance, sp.rim_fg_pct, sp.mid_fg_pct, sp.corner3_pct, sp.atb3_pct,
               sp.paint_fg_pct, sp.rim_rate, sp.three_rate,
               sp.rim_fga, sp.mid_fga, sp.corner3_fga
        FROM player_season_stats s
        JOIN players p USING (player_id)
        LEFT JOIN player_shot_profile sp USING (player_id, season)
        WHERE s.season = ?
    """, conn, params=(season,))
    if df.empty:
        df["qualified"] = []
        return df
    # Older databases (deployed before tov_pct existed) or rows loaded without it:
    # fall back to the per-game line so the slider never goes blank.
    if "tov_pct" not in df:
        df["tov_pct"] = np.nan
    plays = df["fga"] + 0.44 * df["fta"] + df["tov"]
    df["tov_pct"] = df["tov_pct"].fillna(100 * df["tov"] / plays.where(plays > 0))
    max_gp = df["gp"].max() or 0
    df["qualified"] = (df["gp"] >= config.QUAL_GP_SHARE * max_gp) & (df["min"] >= config.QUAL_MIN_MPG)
    return df


def pool_min_fga(frame: pd.DataFrame) -> int:
    """Attempts a player needs in a zone to be in that zone's percentile pool:
    ZONE_MIN_FGA scaled by how far the season is (league max GP / 82), with a floor."""
    max_gp = frame["gp"].max() if len(frame) else 0
    progress = min(1.0, (max_gp or 0) / config.SEASON_GAMES)
    return max(config.ZONE_POOL_FLOOR, min(config.ZONE_MIN_FGA, round(config.ZONE_MIN_FGA * progress)))


def sample_ok(frame: pd.DataFrame, m: Metric, min_fga: int) -> pd.Series:
    """True where the player has enough attempts behind metric m (always True
    for metrics without a sample column)."""
    if m.sample_col is None or m.sample_col not in frame:
        return pd.Series(True, index=frame.index)
    return frame[m.sample_col].fillna(0) >= min_fga


def low_sample_mask(frame: pd.DataFrame, m: Metric) -> pd.Series:
    """Rate stats with a value but fewer than ZONE_MIN_FGA attempts (shown with a badge)."""
    return frame[m.key].notna() & ~sample_ok(frame, m, config.ZONE_MIN_FGA) if m.key in frame         else pd.Series(False, index=frame.index)


def percentiles(frame: pd.DataFrame) -> pd.DataFrame:
    """Percentile (0-100) of every player on every METRIC, ranked against
    qualified players only. Lower-is-better metrics are flipped so 100 is always good.

    Zone rate stats rank against qualified players with at least pool_min_fga()
    attempts there, so a 3-for-3 corner shooter can't distort everyone else's
    rank. Players below that still get a percentile against that pool."""
    out = pd.DataFrame({"player_id": frame["player_id"]})
    min_fga = pool_min_fga(frame)
    for m in METRICS:
        if m.key not in frame:
            continue
        pool = frame[frame["qualified"] & sample_ok(frame, m, min_fga)]
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
