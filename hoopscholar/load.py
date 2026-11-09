"""Turn nba_api DataFrames into rows for our tables and write them.

Kept separate from fetch.py so the same code loads real data and demo data.
"""
import math

import pandas as pd

from . import db


def _clean(v):
    """pandas/numpy scalars -> plain Python, NaN -> None."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _iso(d) -> str:
    """Accepts '20251103', '2025-11-03', or '2025-11-03T00:00:00'."""
    s = str(d)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def load_teams(conn, df: pd.DataFrame) -> int:
    rows = [
        {"team_id": int(r.id), "abbr": r.abbreviation, "name": r.full_name}
        for r in df.itertuples()
    ]
    return db.upsert(conn, "teams", rows, ["team_id"])


def load_players(conn, df: pd.DataFrame, insert_only: bool = False) -> int:
    """insert_only=True adds players we don't have yet and leaves existing rows
    alone. Use it for past seasons, whose index has last season's teams."""
    now = db.now_iso()
    known = {r[0] for r in conn.execute("SELECT player_id FROM players")} if insert_only else set()
    rows = []
    for r in df.to_dict("records"):
        first, last = r.get("PLAYER_FIRST_NAME") or "", r.get("PLAYER_LAST_NAME") or ""
        rows.append({
            "player_id": int(r["PERSON_ID"]),
            "full_name": f"{first} {last}".strip(),
            "first_name": first,
            "last_name": last,
            "slug": r.get("PLAYER_SLUG"),
            "team_id": _clean(r.get("TEAM_ID")) or None,
            "team_abbr": r.get("TEAM_ABBREVIATION"),
            "jersey": _clean(r.get("JERSEY_NUMBER")),
            "position": r.get("POSITION"),
            "height": r.get("HEIGHT"),
            "weight": _clean(r.get("WEIGHT")),
            "country": r.get("COUNTRY"),
            "college": r.get("COLLEGE"),
            "draft_year": _clean(r.get("DRAFT_YEAR")),
            "draft_round": _clean(r.get("DRAFT_ROUND")),
            "draft_number": _clean(r.get("DRAFT_NUMBER")),
            "is_active": 1 if _clean(r.get("ROSTER_STATUS")) else 0,
            "from_year": _clean(r.get("FROM_YEAR")),
            "to_year": _clean(r.get("TO_YEAR")),
            "updated_at": now,
        })
    rows = [r for r in rows if r["player_id"] not in known]
    return db.upsert(conn, "players", rows, ["player_id"])


def ensure_players(conn, df: pd.DataFrame) -> None:
    """Add bare player rows for anyone in a stats/shots frame we haven't seen
    (e.g. waived players missing from the current PlayerIndex)."""
    if df is None or df.empty or "PLAYER_ID" not in df:
        return
    pairs = df[["PLAYER_ID", "PLAYER_NAME"]].drop_duplicates("PLAYER_ID")
    conn.executemany(
        "INSERT OR IGNORE INTO players (player_id, full_name, is_active) VALUES (?, ?, 0)",
        [(int(p), str(n)) for p, n in pairs.itertuples(index=False)],
    )


def load_games(conn, df: pd.DataFrame, season: str) -> int:
    """Collapse the two-rows-per-game team log into one row per game.

    MATCHUP looks like 'BOS vs. NYK' for the home team and 'NYK @ BOS' for the away team.
    """
    games: dict[str, dict] = {}
    for r in df.to_dict("records"):
        g = games.setdefault(str(r["GAME_ID"]), {
            "game_id": str(r["GAME_ID"]),
            "season": season,
            "season_type": "Regular Season",
            "game_date": _iso(r["GAME_DATE"]),
            "home_team_id": None, "away_team_id": None,
            "home_pts": None, "away_pts": None,
        })
        side = "home" if " vs. " in r["MATCHUP"] else "away"
        g[f"{side}_team_id"] = int(r["TEAM_ID"])
        g[f"{side}_pts"] = _clean(r["PTS"])
    return db.upsert(conn, "games", list(games.values()), ["game_id"])


def load_shots(conn, df: pd.DataFrame, season: str) -> int:
    if df.empty:
        return 0
    ensure_players(conn, df)
    rows = [{
        "game_id": str(r["GAME_ID"]),
        "game_event_id": int(r["GAME_EVENT_ID"]),
        "season": season,
        "game_date": _iso(r["GAME_DATE"]),
        "player_id": int(r["PLAYER_ID"]),
        "team_id": int(r["TEAM_ID"]),
        "period": _clean(r["PERIOD"]),
        "minutes_remaining": _clean(r["MINUTES_REMAINING"]),
        "seconds_remaining": _clean(r["SECONDS_REMAINING"]),
        "made": int(r["SHOT_MADE_FLAG"]),
        "action_type": r["ACTION_TYPE"],
        "shot_type": r["SHOT_TYPE"],
        "zone_basic": r["SHOT_ZONE_BASIC"],
        "zone_area": r["SHOT_ZONE_AREA"],
        "zone_range": r["SHOT_ZONE_RANGE"],
        "distance": _clean(r["SHOT_DISTANCE"]),
        "loc_x": _clean(r["LOC_X"]),
        "loc_y": _clean(r["LOC_Y"]),
    } for r in df.to_dict("records")]
    return db.upsert(conn, "shots", rows, ["game_id", "game_event_id"])


# nba_api column -> our column, per source frame
BASE_COLS = {
    "TEAM_ABBREVIATION": "team_abbr", "AGE": "age", "GP": "gp", "MIN": "min",
    "PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "BLK": "blk", "TOV": "tov",
    "OREB": "oreb", "DREB": "dreb", "PF": "pf", "PLUS_MINUS": "plus_minus",
    "FGM": "fgm", "FGA": "fga", "FG_PCT": "fg_pct",
    "FG3M": "fg3m", "FG3A": "fg3a", "FG3_PCT": "fg3_pct",
    "FTM": "ftm", "FTA": "fta", "FT_PCT": "ft_pct",
}
ADV_COLS = {
    "TS_PCT": "ts_pct", "EFG_PCT": "efg_pct", "USG_PCT": "usg_pct", "AST_PCT": "ast_pct",
    "AST_TO": "ast_to", "REB_PCT": "reb_pct", "OREB_PCT": "oreb_pct", "DREB_PCT": "dreb_pct",
    "OFF_RATING": "off_rating", "DEF_RATING": "def_rating", "NET_RATING": "net_rating",
    "PIE": "pie",
}
TRACK_COLS = {"DIST_MILES": "dist_miles", "AVG_SPEED": "avg_speed"}


def tov_pct(tov, fga, fta):
    """100 * TOV / (FGA + 0.44*FTA + TOV): turnovers per 100 plays that end in a
    shot, a trip to the line, or a turnover. (NBA.com's TM_TOV_PCT also counts
    assists in the denominator, so it's a different stat.)"""
    if tov is None or fga is None or fta is None:
        return None
    plays = fga + 0.44 * fta + tov
    return 100 * tov / plays if plays > 0 else None


def load_season_stats(conn, season: str, base: pd.DataFrame,
                      adv: pd.DataFrame | None = None,
                      track: pd.DataFrame | None = None,
                      totals: pd.DataFrame | None = None) -> int:
    """totals (Base, per_mode='Totals') is optional: it's used for TOV% because
    per-game averages arrive rounded to one decimal. Without it, TOV% comes
    from the per-game line."""
    ensure_players(conn, base)
    rows: dict[int, dict] = {}
    for frame, mapping in ((base, BASE_COLS), (adv, ADV_COLS), (track, TRACK_COLS)):
        if frame is None or frame.empty:
            continue
        for r in frame.to_dict("records"):
            pid = int(r["PLAYER_ID"])
            if frame is not base and pid not in rows:
                continue  # only keep players that have a base line
            row = rows.setdefault(pid, {"player_id": pid, "season": season})
            for src, dst in mapping.items():
                if src in r:
                    row[dst] = _clean(r[src])
    for frame in (totals, base):  # totals win; the per-game line fills anyone missing
        if frame is None or frame.empty:
            continue
        for r in frame.to_dict("records"):
            row = rows.get(int(r["PLAYER_ID"]))
            if row is not None and row.get("tov_pct") is None:
                row["tov_pct"] = tov_pct(_clean(r.get("TOV")), _clean(r.get("FGA")), _clean(r.get("FTA")))
    now = db.now_iso()
    # every row needs the same columns for executemany
    all_cols = {c for r in rows.values() for c in r}
    out = [{c: r.get(c) for c in sorted(all_cols)} | {"updated_at": now} for r in rows.values()]
    return db.upsert(conn, "player_season_stats", out, ["player_id", "season"])
