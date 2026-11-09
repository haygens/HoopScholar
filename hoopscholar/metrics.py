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

# One plain-English sentence per stat, shown as a tooltip wherever the stat is
# labeled (sliders, table headers). Keys are column names; the front end gets
# this whole dictionary from /api/meta.
GLOSSARY: dict[str, str] = {
    "gp": "Games played this season.",
    "min": "Minutes played per game.",
    "pts": "Points scored per game.",
    "reb": "Rebounds per game, offensive and defensive.",
    "ast": "Assists per game: passes that lead directly to a teammate's made basket.",
    "stl": "Steals per game.",
    "blk": "Blocked shots per game.",
    "tov": "Turnovers per game: possessions lost to a bad pass, a travel, an offensive foul and so on.",
    "fga": "Field-goal attempts per game.",
    "fg_pct": "Share of field-goal attempts that go in.",
    "fg3_pct": "Share of three-point attempts that go in.",
    "ft_pct": "Share of free throws that go in.",
    "ts_pct": "True Shooting %: scoring efficiency that counts threes and free throws, points / (2 × (FGA + 0.44 × FTA)).",
    "efg_pct": "Effective FG%: field-goal percentage with made threes counted as 1.5 makes, since they're worth 50% more.",
    "usg_pct": "Usage %: share of the team's plays a player finishes (with a shot, free throws or a turnover) while on the floor.",
    "ast_pct": "Assist %: share of teammates' made baskets a player assisted while on the floor.",
    "reb_pct": "Rebound %: share of all available rebounds a player grabbed while on the floor.",
    "tov_pct": "TOV%: turnovers per 100 plays that end in a shot, a trip to the line or a turnover, 100 × TOV / (FGA + 0.44 × FTA + TOV). Lower is better.",
    "def_rating": "Defensive Rating: points the team allows per 100 possessions while the player is on the floor. Lower is better.",
    "net_rating": "Net Rating: the team's point margin per 100 possessions while the player is on the floor (offensive minus defensive rating).",
    "pie": "PIE (Player Impact Estimate): NBA.com's estimate of the share of all box-score events in a player's games (points, rebounds, assists and more) that they produced.",
    "rim_fg_pct": "Rim FG%: shooting percentage in the restricted area, within 4 feet of the hoop.",
    "mid_fg_pct": "Mid-Range FG%: shooting percentage on two-pointers from outside the paint.",
    "corner3_pct": "Corner 3 %: three-point percentage from the corners, the shortest three on the floor (22 ft).",
    "rim_rate": "Rim Rate: share of a player's field-goal attempts taken in the restricted area.",
    "three_rate": "3PA Rate: share of a player's field-goal attempts that are threes.",
    "avg_distance": "Average distance of a player's field-goal attempts from the hoop, in feet.",
    "dist_miles": "Miles a player runs per game, measured by NBA.com's optical tracking cameras.",
    "avg_speed": "Average speed while on the floor in miles per hour, from optical tracking.",
    "zone_league": "League FG% from this zone: every made shot there divided by every attempt, across the whole league.",
    "zone_diff": "The player's FG% from this zone minus the league's, in percentage points.",
    "percentile": "Percentile vs qualified players: 100 is the best, 50 is average, 0 is the worst.",
    "similar": "Qualified players whose percentile profile points the same way, by cosine similarity of their slider percentiles (100 = identical shape).",
}


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


def top_percentiles(row: pd.Series, prow: pd.Series, n: int = 3) -> list[dict]:
    """A player's n best slider percentiles (row from season_frame, prow from
    percentiles()). Zone stats under ZONE_MIN_FGA attempts are skipped so a
    3-for-3 corner shooter's '100' isn't billed as a strength. Ties keep
    METRICS order."""
    found = []
    for m in METRICS:
        p = prow.get(m.key)
        if p is None or pd.isna(p):
            continue
        if m.sample_col:
            attempts = row.get(m.sample_col)
            if attempts is None or pd.isna(attempts) or attempts < config.ZONE_MIN_FGA:
                continue
        found.append({"key": m.key, "label": m.label, "fmt": m.fmt,
                      "value": float(row[m.key]), "percentile": int(p)})
    return sorted(found, key=lambda x: -x["percentile"])[:n]


# ---------------------------------------------------------------- game-log trends

TREND_WINDOW = 5


def rolling_trends(games: pd.DataFrame, window: int = TREND_WINDOW) -> pd.DataFrame:
    """Add ts_pct (single game) and roll_pts / roll_ts (last `window` games,
    starting once there are that many) to a date-ordered game log.

    Rolling TS% comes from the window's totals, PTS / (2 × (FGA + 0.44 × FTA)),
    so a 2-shot game doesn't count as much as a 20-shot game."""
    g = games.copy()
    if g.empty:
        for c in ("ts_pct", "roll_pts", "roll_ts"):
            g[c] = pd.Series(dtype=float)
        return g
    tsa = 2 * (g["fga"] + 0.44 * g["fta"])
    g["ts_pct"] = g["pts"] / tsa.where(tsa > 0)
    roll = lambda s: s.rolling(window, min_periods=window).sum()
    g["roll_pts"] = roll(g["pts"]) / window
    roll_tsa = roll(tsa)
    g["roll_ts"] = roll(g["pts"]) / roll_tsa.where(roll_tsa > 0)
    return g


# ---------------------------------------------------------------- similar players

SIMILAR_N = 5


def similar_players(frame: pd.DataFrame, pcts: pd.DataFrame, season: str, n: int = SIMILAR_N) -> list[dict]:
    """Top-n most similar qualified players for each qualified player, by cosine
    similarity of their slider percentiles.

    Percentiles are centered, (p - 50) / 50, so the league median is 0. On raw
    0-100 values every pair looks similar, because all vectors point the same
    general way. A missing stat, or a zone stat under ZONE_MIN_FGA attempts,
    counts as 0 (median), so it neither adds to nor subtracts from a match."""
    q = frame[frame["qualified"]]
    keys = [m.key for m in METRICS if m.key in pcts]
    if len(q) < 2 or not keys:
        return []
    X = (pcts.set_index("player_id").loc[q["player_id"], keys].to_numpy(float) - 50) / 50
    for j, key in enumerate(keys):
        m = METRIC_BY_KEY[key]
        if m.sample_col:
            X[~sample_ok(q, m, config.ZONE_MIN_FGA).to_numpy(), j] = np.nan
    X = np.nan_to_num(X, nan=0.0)
    norms = np.linalg.norm(X, axis=1)
    U = X / np.where(norms == 0, 1, norms)[:, None]
    S = U @ U.T
    np.fill_diagonal(S, -np.inf)
    ids = q["player_id"].to_numpy()
    out = []
    for i, pid in enumerate(ids):
        for rank, j in enumerate(np.argsort(-S[i], kind="stable")[:n], 1):
            out.append({"player_id": int(pid), "season": season, "rank": rank,
                        "similar_id": int(ids[j]), "score": round(float(S[i, j]), 4)})
    return out


def refresh_similar(conn, season: str) -> int:
    """Rebuild player_similar for a season (local data only, no network)."""
    frame = season_frame(conn, season)
    rows = similar_players(frame, percentiles(frame), season) if not frame.empty else []
    conn.execute("DELETE FROM player_similar WHERE season = ?", (season,))
    conn.executemany(
        "INSERT INTO player_similar (player_id, season, rank, similar_id, score) VALUES (?,?,?,?,?)",
        [(r["player_id"], r["season"], r["rank"], r["similar_id"], r["score"]) for r in rows])
    conn.commit()
    return len(rows)


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
