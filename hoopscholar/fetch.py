"""Thin wrappers around nba_api. Each returns a pandas DataFrame.

Every call pauses REQUEST_DELAY seconds first and retries with backoff, since
stats.nba.com throttles quick clients and sometimes just hangs.
"""
import random
import time

import pandas as pd
from nba_api.stats.endpoints import (
    leaguedashplayerstats,
    leaguedashptstats,
    leaguegamelog,
    playerindex,
    shotchartdetail,
)
from nba_api.stats.static import teams as static_teams

from . import config


def _call(endpoint_cls, frame_index=0, retries=None, **params):
    params.setdefault("timeout", config.REQUEST_TIMEOUT)
    if config.PROXY:
        params.setdefault("proxy", config.PROXY)
    retries = retries or config.REQUEST_RETRIES
    last_err = None
    for attempt in range(retries):
        # Pause before every call; after a failure wait twice as long each
        # time, plus up to one delay of jitter so retries don't line up.
        wait = config.REQUEST_DELAY * (1 if attempt == 0 else 2 ** attempt)
        time.sleep(wait + (random.uniform(0, config.REQUEST_DELAY) if attempt else 0))
        try:
            return endpoint_cls(**params).get_data_frames()[frame_index]
        except Exception as e:  # timeouts, JSON decode errors, connection resets
            last_err = e
            print(f"  ! {endpoint_cls.__name__} failed (attempt {attempt + 1}/{retries}): {e}")
    raise RuntimeError(f"{endpoint_cls.__name__} failed after {retries} attempts") from last_err


def _nba_date(iso: str) -> str:
    """'2025-11-03' -> '11/03/2025' (the format stats.nba.com date filters expect)."""
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def teams() -> pd.DataFrame:
    """Bundled with nba_api, no network needed."""
    return pd.DataFrame(static_teams.get_teams())


def player_index(season: str) -> pd.DataFrame:
    return _call(playerindex.PlayerIndex, season=season, league_id="00")


def team_game_log(season: str, date_from: str | None = None) -> pd.DataFrame:
    """Two rows per game (one per team)."""
    return _call(
        leaguegamelog.LeagueGameLog,
        season=season,
        season_type_all_star=config.SEASON_TYPE,
        player_or_team_abbreviation="T",
        date_from_nullable=_nba_date(date_from) if date_from else "",
    )


def player_game_log(season: str, date_from: str | None = None) -> pd.DataFrame:
    """Every player's box score in every game, one row per player-game, in one
    request (playergamelog would need one request per player)."""
    return _call(
        leaguegamelog.LeagueGameLog,
        season=season,
        season_type_all_star=config.SEASON_TYPE,
        player_or_team_abbreviation="P",
        date_from_nullable=_nba_date(date_from) if date_from else "",
    )


def shots_for_team(season: str, team_id: int, date_from: str | None = None) -> pd.DataFrame:
    """All field-goal attempts by one team's players. player_id=0 means every player."""
    return _call(
        shotchartdetail.ShotChartDetail,
        team_id=team_id,
        player_id=0,
        season_nullable=season,
        season_type_all_star=config.SEASON_TYPE,
        context_measure_simple="FGA",
        date_from_nullable=_nba_date(date_from) if date_from else "",
    )


def shots_for_player(season: str, player_id: int) -> pd.DataFrame:
    return _call(
        shotchartdetail.ShotChartDetail,
        team_id=0,
        player_id=player_id,
        season_nullable=season,
        season_type_all_star=config.SEASON_TYPE,
        context_measure_simple="FGA",
    )


def player_stats(season: str, measure: str = "Base", per_mode: str = "PerGame") -> pd.DataFrame:
    """measure: 'Base' (box score) or 'Advanced' (TS%, USG%, ratings...).
    per_mode: 'PerGame' or 'Totals' (unrounded, used for TOV%)."""
    return _call(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        season_type_all_star=config.SEASON_TYPE,
        per_mode_detailed=per_mode,
        measure_type_detailed_defense=measure,
    )


def player_tracking(season: str, measure: str = "SpeedDistance") -> pd.DataFrame:
    return _call(
        leaguedashptstats.LeagueDashPtStats,
        season=season,
        season_type_all_star=config.SEASON_TYPE,
        per_mode_simple="PerGame",
        player_or_team="Player",
        pt_measure_type=measure,
    )
