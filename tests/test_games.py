"""Games pool: the minutes-per-team-game rule and frozen daily pools.

    python -m pytest tests/
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from hoopscholar import config, db, games, pipeline  # noqa: E402

SEASON = "2026-27"


def make_db(tmp_path, days):
    """Two teams playing each other daily from Oct 20. Player 1 plays 30 min a
    game, player 2 plays 5, player 3 plays 20 for team 10 then is traded to 20."""
    conn = db.connect(tmp_path / "g.db")
    db.init_schema(conn)
    conn.executemany("INSERT INTO teams VALUES (?,?,?)", [(10, "AAA", "A"), (20, "BBB", "B")])
    start = dt.date(2026, 10, 20)
    for i in range(days):
        gid, date = f"g{i:03d}", (start + dt.timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO games (game_id, season, game_date, home_team_id, away_team_id) VALUES (?,?,?,?,?)",
                     (gid, SEASON, date, 10, 20))
        rows = [(1, gid, SEASON, 10, 30.0), (2, gid, SEASON, 20, 5.0), (3, gid, SEASON, 10 if i < 5 else 20, 20.0)]
        conn.executemany("INSERT INTO player_game_logs (player_id, game_id, season, team_id, min) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_threshold_and_team(tmp_path):
    conn = make_db(tmp_path, 10)
    pool = games.eligible(conn, SEASON)
    assert list(pool["player_id"]) == [1, 3]                # 5 min/game is under 12
    assert pool.set_index("player_id").loc[3, "team_id"] == 20  # measured against the new team
    early = games.eligible(conn, SEASON, before="2026-10-23")  # only the first 3 games
    assert list(early["player_id"]) == [1, 3]
    assert games.eligible(conn, SEASON, before="2026-10-20").empty


def test_daily_pools_ignore_later_games(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GAMES_EPOCH", "2026-11-09")
    week = dt.date(2026, 11, 9)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(config, "GAMES_MIN_DAILY_POOL", 1)
    before = games.daily_pools(make_db(tmp_path / "a", 14), week)
    after = games.daily_pools(make_db(tmp_path / "b", 30), week)
    # Two weeks vs a month of games loaded: the week's pool (cutoff Nov 2) is identical.
    assert before == after == {"2026-11-09": {"season": SEASON, "ids": [1, 3]}}
    assert games.pool_cutoff(games.week_start(dt.date(2026, 11, 15))) == dt.date(2026, 11, 2)


def add_last_season(conn):
    """A finished 2025-26: player 4 played 30 min a game, player 1 played 2."""
    for i in range(20):
        gid, date = f"p{i:03d}", (dt.date(2025, 10, 22) + dt.timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO games (game_id, season, game_date, home_team_id, away_team_id) VALUES (?,?,?,?,?)",
                     (gid, "2025-26", date, 10, 20))
        conn.executemany("INSERT INTO player_game_logs (player_id, game_id, season, team_id, min) VALUES (?,?,?,?,?)",
                         [(4, gid, "2025-26", 20, 30.0), (1, gid, "2025-26", 10, 2.0)])
    conn.commit()


def test_early_season_falls_back_to_last_season(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GAMES_EPOCH", "2026-10-19")
    monkeypatch.setattr(config, "GAMES_MIN_DAILY_POOL", 2)
    pools = {}
    for days in (14, 30):  # more games loaded later must not change any week's choice
        (tmp_path / str(days)).mkdir()
        conn = make_db(tmp_path / str(days), days)
        add_last_season(conn)
        pools[days] = games.daily_pools(conn, dt.date(2026, 11, 9))
    assert pools[14] == pools[30]
    p = pools[30]
    # Oct 19 and Oct 26 weeks: no 2026-27 games before the cutoff -> last season's pool
    assert p["2026-10-19"] == p["2026-10-26"] == {"season": "2025-26", "ids": [4]}
    # Nov 2 week (cutoff Oct 26): players 1 and 3 qualify, 2 >= the minimum -> this season
    assert p["2026-11-02"] == {"season": SEASON, "ids": [1, 3]}
    assert games.previous_season("2026-27") == "2025-26"


def test_stored_pools_match_live(tmp_path, monkeypatch):
    """The weekly job stores pools through the week after the day games were
    last pulled; they equal the live computation and are never recomputed."""
    monkeypatch.setattr(config, "GAMES_EPOCH", "2026-10-19")
    monkeypatch.setattr(config, "GAMES_MIN_DAILY_POOL", 2)
    conn = make_db(tmp_path, 30)
    add_last_season(conn)
    loaded_on = dt.date(2026, 11, 4)  # a Wednesday: games before Monday Nov 2 are complete
    assert pipeline.refresh_daily_pools(conn, loaded_on) == 4
    stored = {w: {"season": s, "ids": json.loads(i)} for w, s, i in conn.execute("SELECT week, season, ids FROM games_daily_pool")}
    assert sorted(stored) == ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]
    assert stored == games.daily_pools(conn, dt.date(2026, 11, 9))
    assert pipeline.refresh_daily_pools(conn, loaded_on) == 0
