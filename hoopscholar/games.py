"""Player pools for the games pages (Guess the Player, Higher or Lower).

A player is in the pool when their total minutes this season are at least
config.GAMES_MIN_PER_TEAM_GAME times the number of games their team has played.
Minutes come from player_game_logs, so the pool can be computed as of any past
date. The daily puzzle depends on that:

Each Guess the Player day draws from a pool frozen at a cutoff: games before
the Monday one week before that day's week (Monday-Sunday). The weekly data
pull always covers games through the Sunday before it runs, so the cutoff is
always loaded and those games don't change afterwards. A refresh mid-week,
or on the Monday itself, can't change a day's pool, so it can't change that
day's answer or the answers before it. In the first weeks of a season the
day falls back to last season's pool (see daily_pool()).
"""
import datetime as dt

import pandas as pd

from . import config


def eligible(conn, season: str, before: str | None = None) -> pd.DataFrame:
    """Players at or above the minutes threshold in `season`, counting only games
    before `before` (ISO date) if given. Columns: player_id, team_id, minutes,
    team_gp. The team is the one from the player's latest game in range, so a
    traded player is measured against their current team. Sorted by player_id."""
    cols = ["player_id", "team_id", "minutes", "team_gp"]
    cond, params = ("AND g.game_date < ?", [season, before]) if before else ("", [season])
    logs = pd.read_sql_query(f"""
        SELECT l.player_id, l.team_id, l.min, g.game_date, l.game_id
        FROM player_game_logs l JOIN games g USING (game_id)
        WHERE l.season = ? {cond}""", conn, params=params)
    if logs.empty:
        return pd.DataFrame(columns=cols)
    team_gp = pd.read_sql_query(f"""
        SELECT team_id, COUNT(*) AS team_gp FROM (
            SELECT home_team_id AS team_id, game_date FROM games g WHERE season = ? {cond}
            UNION ALL
            SELECT away_team_id, game_date FROM games g WHERE season = ? {cond})
        GROUP BY team_id""", conn, params=params + params)
    latest = logs.sort_values(["game_date", "game_id"]).groupby("player_id").tail(1)[["player_id", "team_id"]]
    minutes = logs.groupby("player_id")["min"].sum().rename("minutes").reset_index()
    df = minutes.merge(latest, on="player_id").merge(team_gp, on="team_id", how="left")
    df["team_gp"] = df["team_gp"].fillna(0)
    df = df[(df["team_gp"] > 0) & (df["minutes"] >= config.GAMES_MIN_PER_TEAM_GAME * df["team_gp"])]
    return df[cols].sort_values("player_id").reset_index(drop=True)


def week_start(d: dt.date) -> dt.date:
    """The Monday of d's week."""
    return d - dt.timedelta(days=d.weekday())


def pool_cutoff(week: dt.date) -> dt.date:
    """Daily pools for the week starting `week` use games before this date."""
    return week - dt.timedelta(days=7)


def previous_season(season: str) -> str:
    """'2026-27' -> '2025-26'."""
    start = int(season[:4]) - 1
    return f"{start}-{str(start + 1)[-2:]}"


def daily_pool(conn, week: dt.date) -> dict:
    """{season, ids} for the week starting `week`. Early in a season (before
    the cutoff has GAMES_MIN_DAILY_POOL players) it falls back to last season's
    full pool, so there's always a puzzle. Both inputs are fixed once the week
    starts (games before the cutoff, and a finished season), so the choice
    between them can't change either."""
    season = config.season_for_date(week)
    cutoff = pool_cutoff(week).isoformat()
    ids = eligible(conn, season, cutoff)["player_id"]
    if len(ids) < config.GAMES_MIN_DAILY_POOL:
        prev = previous_season(season)
        prev_ids = eligible(conn, prev, cutoff)["player_id"]
        if len(prev_ids) > len(ids):
            season, ids = prev, prev_ids
    return {"season": season, "ids": [int(x) for x in ids]}


def daily_pools(conn, through: dt.date) -> dict[str, dict]:
    """{week start (ISO Monday): {season, ids}} for every week from the epoch's
    week through `through`'s week. See daily_pool()."""
    out = {}
    week = week_start(dt.date.fromisoformat(config.GAMES_EPOCH))
    last = week_start(through)
    while week <= last:
        out[week.isoformat()] = daily_pool(conn, week)
        week += dt.timedelta(days=7)
    return out
