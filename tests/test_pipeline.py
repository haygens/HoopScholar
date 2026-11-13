"""End-to-end test of backfill + nightly logic with the network calls faked.

    python -m pytest tests/        (or just: python tests/test_pipeline.py)

The fake fetchers return demo frames shaped exactly like nba_api's, filtered by
team and date the way stats.nba.com would, so this checks the incremental
logic without touching stats.nba.com.
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_demo_data  # noqa: E402
from hoopscholar import db, fetch, metrics, pipeline  # noqa: E402

SEASON = "2025-26"


def fake_fetchers(cutoff: str):
    """Pretend today is `cutoff`: nothing after it exists yet."""
    teams, index, log, shots, base, adv, track = make_demo_data.build(seed=3)
    shots = shots.assign(_iso=pd_iso(shots["GAME_DATE"]))
    shots = shots[shots["_iso"] <= cutoff]
    log = log[log["GAME_DATE"] <= cutoff]

    def since(df, col, date_from):
        if not date_from:
            return df
        return df[df[col] >= date_from]

    fetch.player_index = lambda season: index
    fetch.team_game_log = lambda season, date_from=None: since(log, "GAME_DATE", date_from)
    fetch.shots_for_team = lambda season, team_id, date_from=None: since(
        shots[shots["TEAM_ID"] == team_id], "_iso", date_from).drop(columns="_iso")
    fetch.player_stats = lambda season, measure="Base", per_mode="PerGame": base if measure == "Base" else adv
    fetch.player_tracking = lambda season, measure="SpeedDistance": track
    plog = make_demo_data.player_game_log(shots.drop(columns="_iso"))
    fetch.player_game_log = lambda season, date_from=None: since(plog, "GAME_DATE", date_from)
    fake_fetchers.player_logs = len(plog)
    return shots


def pd_iso(col):
    return col.str.slice(0, 4) + "-" + col.str.slice(4, 6) + "-" + col.str.slice(6, 8)


def test_backfill_then_nightly():
    path = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)

    # Backfill as of mid-December
    first = fake_fetchers("2025-12-15")
    pipeline.ingest_reference(conn, SEASON)
    pipeline.ingest_games(conn, SEASON)
    pipeline.ingest_player_games(conn, SEASON)
    pipeline.ingest_shots(conn, SEASON)
    pipeline.ingest_season_stats(conn, SEASON)
    pipeline.finish(conn, SEASON, "backfill", "test")
    logs1 = conn.execute("SELECT COUNT(*) FROM player_game_logs").fetchone()[0]
    assert logs1 == fake_fetchers.player_logs, (logs1, fake_fetchers.player_logs)
    n1 = conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    assert n1 == len(first), (n1, len(first))
    assert pipeline.last_game_date(conn, SEASON) <= "2025-12-15"

    # Nightly run a month later only adds the new shots, no duplicates
    later = fake_fetchers("2026-01-15")
    last = pipeline.last_game_date(conn, SEASON)
    pipeline.ingest_games(conn, SEASON, pipeline.day_before(last))
    pipeline.ingest_player_games(conn, SEASON)   # picks its own start date
    pipeline.ingest_shots(conn, SEASON, pipeline.day_before(last))
    pipeline.finish(conn, SEASON, "nightly", "test")
    logs2 = conn.execute("SELECT COUNT(*) FROM player_game_logs").fetchone()[0]
    assert logs2 == fake_fetchers.player_logs > logs1, (logs2, fake_fetchers.player_logs, logs1)
    n2 = conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    assert n2 == len(later), (n2, len(later))
    assert n2 > n1

    # Percentiles: qualified pool spans 0..100, lower-is-better flipped
    frame = metrics.season_frame(conn, SEASON)
    pct = metrics.percentiles(frame).set_index("player_id")
    q = frame[frame["qualified"]].set_index("player_id")
    assert pct.loc[q.index, "pts"].min() == 0 and pct.loc[q.index, "pts"].max() == 100
    best_tov = q["tov_pct"].idxmin()
    assert pct.loc[best_tov, "tov_pct"] == 100
    r = q.loc[best_tov]
    assert abs(r["tov_pct"] - 100 * r["tov"] / (r["fga"] + 0.44 * r["fta"] + r["tov"])) < 1e-9
    # Zone FG% is stored at any sample size; low samples are flagged and kept out of the pool
    rim = metrics.METRIC_BY_KEY["rim_fg_pct"]
    small = frame[(frame["rim_fga"] > 0) & (frame["rim_fga"] < 25)]
    assert len(small) and small["rim_fg_pct"].notna().all()
    assert metrics.low_sample_mask(frame, rim)[small.index].all()
    assert not pct.loc[small["player_id"], "rim_fg_pct"].isna().any()
    # Similar players: 5 per qualified player, never themselves, all qualified
    sim = conn.execute("SELECT player_id, similar_id, score FROM player_similar WHERE season=?", (SEASON,)).fetchall()
    assert len(sim) == 5 * len(q)
    assert all(a != b and -1 <= sc <= 1 for a, b, sc in sim)
    assert {b for _, b, _ in sim} <= set(q.index)
    games = conn.execute("SELECT COUNT(*) FROM games WHERE home_team_id IS NULL OR away_team_id IS NULL").fetchone()[0]
    assert games == 0
    check_summary(conn, frame, metrics.percentiles(frame))
    for backend in ("sqlite", "libsql"):
        check_api(path, int(q.index[0]), backend)
    print(f"ok: backfill {n1} shots, nightly -> {n2}, {len(q)} qualified players")


def check_summary(conn, frame, pct):
    """The precomputed tables hold exactly what the site used to compute on the fly."""
    import pandas as pd
    s = pd.read_sql_query("SELECT * FROM player_season_summary WHERE season=?", conn, params=(SEASON,)).set_index("player_id")
    f, p = frame.set_index("player_id"), pct.set_index("player_id")
    assert set(s.index) == set(f.index)
    for m in metrics.METRICS:
        assert ((s[f"p_{m.key}"] == p.loc[s.index, m.key]) | (s[f"p_{m.key}"].isna() & p.loc[s.index, m.key].isna())).all(), m.key
    assert (s["qualified"].astype(bool) == f.loc[s.index, "qualified"]).all()
    info = conn.execute("SELECT pool_size, max_gp, pool_min_fga FROM season_info WHERE season=?", (SEASON,)).fetchone()
    assert tuple(info) == (int(f["qualified"].sum()), int(f["gp"].max()), metrics.pool_min_fga(frame))
    # zone splits and league zones add up to the shots table
    one = lambda sql: tuple(conn.execute(sql, (SEASON,)).fetchone())
    shots = one("SELECT COUNT(*), SUM(made) FROM shots WHERE season=?")
    assert one("SELECT SUM(fga), SUM(fgm) FROM player_zone_splits WHERE season=?") == shots
    assert one("SELECT SUM(fga), SUM(fgm) FROM league_zone_averages WHERE season=?") == shots
    in_court = conn.execute("SELECT COUNT(*) FROM shots WHERE season=? AND loc_y <= 422.5", (SEASON,)).fetchone()[0]
    assert conn.execute("SELECT SUM(fga) FROM league_hex_bins WHERE season=?", (SEASON,)).fetchone()[0] == in_court
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE is_home IS NULL").fetchone()[0] == 0
    # search columns
    assert conn.execute("""SELECT COUNT(*) FROM players WHERE player_id IN (SELECT player_id FROM player_season_stats)
                           AND (last_season IS NULL OR first_name IS NULL)""").fetchone()[0] == 0


def check_api(path, pid, backend="sqlite"):
    """The site's endpoints against the database the pipeline just built,
    read through `backend` (HOOP_DB_BACKEND)."""
    import os
    import warnings
    from hoopscholar import config
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
    import api.main as site
    config.DB_PATH = path
    old = os.environ.get("HOOP_DB_BACKEND")
    os.environ["HOOP_DB_BACKEND"] = backend
    try:
        site._local.__dict__.clear()   # drop any connection to another database
        site._cache.clear()
        _check_api(TestClient(site.app), pid)
        assert type(site.conn()).__name__ == ("LibsqlConnection" if backend == "libsql" else "Connection")
    finally:
        site._local.__dict__.clear()
        if old is None:
            os.environ.pop("HOOP_DB_BACKEND", None)
        else:
            os.environ["HOOP_DB_BACKEND"] = old


def _check_api(t, pid):
    get = lambda url, **p: t.get(url, params=p).json()

    shots = lambda **f: len(get(f"/api/players/{pid}/shots", **f)["shots"])
    total = shots()
    assert total > 0
    assert shots(loc="home") + shots(loc="away") == total
    assert sum(shots(period=p) for p in "1234") + shots(period="OT") == total
    assert get(f"/api/players/{pid}/shots", last=1)["games"] == 1
    assert sum(z["fga"] for z in get(f"/api/players/{pid}/shots")["zones"]) == total

    g = get(f"/api/players/{pid}/games")
    assert g["available"] and g["games"] and "roll_pts" in g["games"][-1]
    c = get(f"/api/players/{pid}/career")
    assert c["seasons"] and all(len(m["cells"]) == len(c["seasons"]) for m in c["metrics"])
    p = get(f"/api/players/{pid}")
    assert len(p["similar"]) == 5
    assert p["zones"] and sum(z["fga"] for z in p["zones"]) == total
    abbr = p["stats"]["team_abbr"]
    team = get(f"/api/teams/{abbr}")
    assert any(r["player_id"] == pid for r in team["roster"])
    assert any(x["kind"] == "team" for x in get("/api/search", q=abbr))

    # Search: anywhere in the name, case-insensitive, at most 10
    name = p["player"]["full_name"]
    first, _, last = name.partition(" ")
    for q in (f"{first} {last[:2]}".lower(), last.upper(), name, name[1:]):  # name[1:]: starts mid-word
        assert any(x.get("player_id") == pid for x in get("/api/search", q=q)), q
    hits = [x for x in get("/api/search", q=last[:2]) if x["kind"] == "player"]
    starts = [x["full_name"].lower().startswith(last[:2].lower()) for x in hits]
    assert starts == sorted(starts, reverse=True)  # names starting with q come first
    assert len(get("/api/search", q="a")) <= 10
    assert t.get("/api/search", params={"q": "a", "limit": 50}).status_code == 422

    # Leaderboard pages: 50 by default, pages don't overlap, ranks continue
    lb = get("/api/leaderboard", stat="pts", qualified="false")
    assert len(lb["rows"]) <= 50 and lb["offset"] == 0
    if lb["has_more"]:
        nxt = get("/api/leaderboard", stat="pts", qualified="false", offset=50)
        assert nxt["rows"][0]["rank"] == 51
        assert not {r["player_id"] for r in lb["rows"]} & {r["player_id"] for r in nxt["rows"]}
    assert t.get("/api/leaderboard", params={"limit": 500}).status_code == 422
    ts = get("/api/leaderboard", stat="ts_pct")
    assert ts["minimum"]["unit"].startswith("true shooting") and ts["rows"]

    # Cache headers: long on data, short on meta, none on errors
    r = t.get(f"/api/players/{pid}")
    assert r.headers["cache-control"] == "public, s-maxage=86400, stale-while-revalidate=604800"
    assert t.get("/api/meta").headers["cache-control"].startswith("public, s-maxage=300")
    assert "cache-control" not in t.get("/api/leaderboard", params={"stat": "nope"}).headers
    assert get("/api/games/pool", day="2026-01-01")["names"]


def test_past_season_index_keeps_current_teams(tmp_path):
    """Backfilling an old season adds missing players with their bio (no current
    team, inactive) and fills blank bio fields, but never changes a current team."""
    import pandas as pd
    from hoopscholar import load
    conn = db.connect(tmp_path / "p.db")
    db.init_schema(conn)
    person = lambda pid, first, last, team, abbr, **kw: {
        "PERSON_ID": pid, "PLAYER_FIRST_NAME": first, "PLAYER_LAST_NAME": last,
        "TEAM_ID": team, "TEAM_ABBREVIATION": abbr, "ROSTER_STATUS": 1, **kw}
    load.load_players(conn, pd.DataFrame([person(1, "Ann", "Current", 10, "AAA", JERSEY_NUMBER="7")]))
    load.ensure_players(conn, pd.DataFrame({"PLAYER_ID": [3], "PLAYER_NAME": ["Bare Name"]}))
    load.load_players(conn, pd.DataFrame([
        person(1, "Ann", "Current", 20, "BBB", HEIGHT="6-5", JERSEY_NUMBER="99"),   # her old team that season
        person(2, "Old", "Timer", 30, "CCC", HEIGHT="6-9", COLLEGE="State"),        # retired since
        person(3, "Bare", "Name", 30, "CCC", COLLEGE="Tech"),
    ]), past=True)
    get = lambda pid: dict(conn.execute("SELECT * FROM players WHERE player_id=?", (pid,)).fetchone())
    ann, old, bare = get(1), get(2), get(3)
    assert (ann["team_id"], ann["team_abbr"], ann["jersey"], ann["is_active"]) == (10, "AAA", "7", 1)
    assert ann["height"] == "6-5"                                   # blank bio field filled
    assert (old["team_id"], old["team_abbr"], old["is_active"]) == (None, None, 0)
    assert (old["full_name"], old["height"], old["college"]) == ("Old Timer", "6-9", "State")
    assert (bare["first_name"], bare["last_name"], bare["college"]) == ("Bare", "Name", "Tech")


def test_backfill_resumes_after_failure(tmp_path, monkeypatch):
    """A season that stops partway (NBA.com failing on the 12th team) picks up
    where it left off: only the missing teams are fetched, nothing else is
    redone, and the result matches a clean run. --stop-at pauses cleanly."""
    import datetime as dt
    import pytest
    import backfill
    conn = db.connect(tmp_path / "r.db")
    db.init_schema(conn)
    all_shots = fake_fetchers("2026-06-30")
    real_shots, real_index = fetch.shots_for_team, fetch.player_index
    calls = []

    def flaky(season, team_id, date_from=None):
        calls.append(team_id)
        if len(calls) == 12:
            raise RuntimeError("ShotChartDetail failed after 4 attempts")
        return real_shots(season, team_id, date_from)
    monkeypatch.setattr(fetch, "shots_for_team", flaky)
    with pytest.raises(RuntimeError):
        backfill.run_season(conn, SEASON)
    steps = {r[0] for r in conn.execute("SELECT step FROM backfill_progress WHERE season=?", (SEASON,))}
    assert len([s for s in steps if s.startswith("shots:")]) == 11 and "finish" not in steps

    fetched, indexed = [], []
    monkeypatch.setattr(fetch, "shots_for_team", lambda s, t, d=None: fetched.append(t) or real_shots(s, t, d))
    monkeypatch.setattr(fetch, "player_index", lambda s: indexed.append(s) or real_index(s))
    assert backfill.run_season(conn, SEASON) == "done"
    assert len(fetched) == 19 and not set(fetched) & set(calls[:11]) and indexed == []
    assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == len(all_shots)
    assert conn.execute("SELECT COUNT(*) FROM season_info WHERE season=?", (SEASON,)).fetchone()[0] == 1
    assert backfill.run_season(conn, SEASON) == "already done"

    with pytest.raises(backfill.Paused):
        backfill.run_season(conn, SEASON, restart=True, deadline=dt.datetime.now() - dt.timedelta(minutes=1))
    assert backfill.season_list("2024-25..2022-23") == ["2024-25", "2023-24", "2022-23"]
    assert backfill.season_list("2016-17..2017-18") == ["2016-17", "2017-18"]


def test_neutral_site_games_take_home_team_from_shot_data(tmp_path):
    """NBA.com labels both teams '@' at neutral sites (here ATL @ MIL and MIL @ ATL).
    The game keeps both scores, and the shot data's HTM decides who's home."""
    import pandas as pd
    from hoopscholar import load
    conn = db.connect(tmp_path / "n.db")
    db.init_schema(conn)
    conn.executemany("INSERT INTO teams VALUES (?,?,?)", [(10, "ATL", "Atlanta"), (20, "MIL", "Milwaukee")])
    load.load_games(conn, pd.DataFrame([
        {"GAME_ID": "g1", "GAME_DATE": "2024-12-14", "TEAM_ID": 10, "MATCHUP": "ATL @ MIL", "PTS": 102},
        {"GAME_ID": "g1", "GAME_DATE": "2024-12-14", "TEAM_ID": 20, "MATCHUP": "MIL @ ATL", "PTS": 110}]), "2024-25")
    assert tuple(conn.execute("SELECT neutral, home_team_id, home_pts, away_team_id, away_pts FROM games").fetchone()) \
        == (1, 20, 110, 10, 102)                                     # team-id order until oriented
    shot = lambda ev, pid, team: {
        "GAME_ID": "g1", "GAME_EVENT_ID": ev, "GAME_DATE": "20241214", "PLAYER_ID": pid, "PLAYER_NAME": f"P{pid}",
        "TEAM_ID": team, "PERIOD": 1, "MINUTES_REMAINING": 5, "SECONDS_REMAINING": 0, "SHOT_MADE_FLAG": 1,
        "ACTION_TYPE": "Jump Shot", "SHOT_TYPE": "2PT Field Goal", "SHOT_ZONE_BASIC": "Mid-Range",
        "SHOT_ZONE_AREA": "Center(C)", "SHOT_ZONE_RANGE": "16-24 ft.", "SHOT_DISTANCE": 18, "LOC_X": 0, "LOC_Y": 180,
        "HTM": "ATL", "VTM": "MIL"}                                  # NBA.com: Atlanta is the designated home team
    load.load_shots(conn, pd.DataFrame([shot(1, 1, 10), shot(2, 2, 20)]), "2024-25")
    assert pipeline.fill_shot_home(conn) == 0                        # never guesses for neutral games
    assert pipeline.orient_neutral_games(conn, fetch_missing=False) == 1
    assert tuple(conn.execute("SELECT neutral, home_team_id, home_pts, away_team_id, away_pts FROM games").fetchone()) \
        == (1, 10, 102, 20, 110)
    assert dict(conn.execute("SELECT team_id, is_home FROM shots").fetchall()) == {10: 1, 20: 0}
    assert pipeline.orient_neutral_games(conn, fetch_missing=False) == 0   # nothing left to do


def test_push_sends_the_weekly_changes_to_turso(tmp_path):
    """After the one-time upload (a copy of the file), a weekly update plus
    push leaves the remote identical to the local database in every table the
    site reads, while sending only the new games' shots."""
    import shutil
    import sqlite3
    import libsql
    from hoopscholar import turso_sync
    path = tmp_path / "local.db"
    conn = db.connect(path)
    db.init_schema(conn)
    fake_fetchers("2025-12-15")
    pipeline.nightly(conn, SEASON)
    turso_sync.clear_pending(conn)
    conn.close()
    shutil.copy(path, tmp_path / "remote.db")           # the one-time upload

    conn = db.connect(path)
    fake_fetchers("2025-12-29")
    pipeline.nightly(conn, SEASON)                      # two weeks later
    p = turso_sync.get_pending(conn)
    assert p["seasons"] == [SEASON] and list(p["since"]) == [SEASON] and p["since"][SEASON]
    planned = turso_sync.plan(conn)
    remote = libsql.connect(str(tmp_path / "remote.db"))
    first = turso_sync.push(conn, remote)                # also syncs the schema once (no fingerprint yet)
    remote.close()
    assert turso_sync.is_empty(turso_sync.get_pending(conn))
    total_shots = conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    assert first["rows"]["shots"] == planned["rows"]["shots"] and 0 < first["rows"]["shots"] < total_shots / 2

    def same_as_local():
        r = sqlite3.connect(tmp_path / "remote.db")
        skip_meta = (turso_sync.PENDING_KEY, *turso_sync.REMOTE_ONLY_META)
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if t in turso_sync.LOCAL_ONLY:
                continue
            cols = ", ".join(turso_sync._cols(conn, t))
            where = f" WHERE key NOT IN ({','.join(repr(k) for k in skip_meta)})" if t == "meta" else ""
            mine = sorted(map(tuple, conn.execute(f"SELECT {cols} FROM {t}{where}")), key=repr)
            theirs = sorted(map(tuple, r.execute(f"SELECT {cols} FROM {t}{where}")), key=repr)
            assert mine == theirs, t
        r.close()
    same_as_local()

    # Next week: the schema fingerprint matches, so no schema statements, and the
    # request count is exactly what the dry run predicted (a few dozen, not hundreds).
    fake_fetchers("2026-01-05")
    pipeline.nightly(conn, SEASON)
    planned = turso_sync.plan(conn)
    week = turso_sync.push(conn, libsql.connect(str(tmp_path / "remote.db")))
    assert week["requests"] == planned["requests"] <= 40, (week["requests"], planned["requests"])
    same_as_local()

    # Nothing pending: only the small tables and meta go, and rows that didn't
    # change aren't rewritten (a trigger on the stand-in counts real writes).
    side = sqlite3.connect(tmp_path / "remote.db")
    side.executescript("""CREATE TABLE writes (t TEXT);
        CREATE TRIGGER w_teams AFTER UPDATE ON teams BEGIN INSERT INTO writes VALUES ('teams'); END;
        CREATE TRIGGER w_players AFTER UPDATE ON players BEGIN INSERT INTO writes VALUES ('players'); END;
        CREATE TRIGGER d_teams AFTER DELETE ON teams BEGIN INSERT INTO writes VALUES ('teams deleted'); END;""")
    side.close()
    sql_seen = []

    class Spy:
        def __init__(self, c): self.c = c
        def execute(self, sql, params=()): sql_seen.append(sql); return self.c.execute(sql, params)
        def executescript(self, sql): sql_seen.append(sql); return self.c.executescript(sql)
        def commit(self): return self.c.commit()
    idle = turso_sync.push(conn, Spy(libsql.connect(str(tmp_path / "remote.db"))))
    assert idle["rows"].get("shots", 0) == 0 and idle["rows"].get("player_season_summary", 0) == 0
    assert idle["requests"] <= 8
    assert not [s for s in sql_seen if "REPLACE" in s.upper()]      # REPLACE on a parent table = huge FK scans
    side = sqlite3.connect(tmp_path / "remote.db")
    assert side.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    side.close()


def test_literal_values_round_trip(tmp_path):
    """Rows sent as SQL literals read back exactly as the local values."""
    import libsql
    from hoopscholar import turso_sync
    rows = [(1, "O'Neal", 0.1, None), (2, "Dončić\n\"x\"", 1 / 3, b"\x00\xff"), (3, "", 1e-300, 2 ** 62),
            (4, "a,b);DROP", -0.0, 123456789.123456789), (5, "ok", float("nan"), -7)]
    conn = libsql.connect(str(tmp_path / "lit.db"))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT, f REAL, x)")
    for sql, params in turso_sync.insert_statements("t", ["id", "s", "f", "x"], rows):
        conn.execute(sql, params)
    conn.commit()
    got = conn.execute("SELECT id, s, f, x FROM t ORDER BY id").fetchall()
    assert [tuple(r) for r in got[:4]] == rows[:4]               # floats exact, including 1e-300
    assert tuple(got[4]) == (5, "ok", None, -7)                 # NaN is stored as NULL, as sqlite3 does
    assert len(list(turso_sync.insert_statements("t", ["id"], [(i,) for i in range(12001)]))) == 3  # 5000 rows max
    floats = list(turso_sync.insert_statements("t", ["f"], [(0.5,)] * 70000))
    assert len(floats) == 14 and max(len(p) for _, p in floats) <= turso_sync.MAX_PARAMS


def test_every_stat_has_a_tooltip():
    keys = [m.key for m in metrics.METRICS + metrics.LEADERBOARD_EXTRA]
    assert not [k for k in keys if not metrics.GLOSSARY.get(k)]



def test_top_percentiles_skips_low_sample_zones():
    import pandas as pd
    row = pd.Series({"pts": 20.0, "ast": 5.0, "stl": 1.0, "corner3_pct": 1.0, "corner3_fga": 3})
    prow = pd.Series({"pts": 80.0, "ast": 60.0, "stl": 60.0, "corner3_pct": 100.0})
    top = metrics.top_percentiles(row, prow)
    assert [t["key"] for t in top] == ["pts", "ast", "stl"]  # 3-attempt corner 3 left out; ties keep slider order
    row["corner3_fga"] = 40
    assert metrics.top_percentiles(row, prow)[0]["key"] == "corner3_pct"



def test_rolling_trends():
    import pandas as pd
    g = pd.DataFrame({"pts": [10, 20, 30, 40, 50, 0], "fga": [10, 10, 10, 10, 10, 0], "fta": [0, 0, 0, 0, 0, 0]})
    out = metrics.rolling_trends(g)
    assert out["roll_pts"].isna().sum() == 4 and out["roll_pts"].iloc[4] == 30
    assert abs(out["roll_ts"].iloc[4] - 150 / 100) < 1e-9      # window totals, not mean of game TS%
    assert pd.isna(out["ts_pct"].iloc[5])                       # 0 attempts -> no TS%
    assert metrics.rolling_trends(g.iloc[:0]).empty



def test_tov_pct_needs_enough_plays():
    import pandas as pd
    from hoopscholar import config
    frame = pd.DataFrame({"gp": [11, 11, 6], "tov_pct": [0.0, 9.0, 0.0], "tov_opps": [50.0, 120.0, 26.0]})
    m = metrics.METRIC_BY_KEY["tov_pct"]
    need = metrics.min_tov_opps(frame)
    assert need == max(config.TOV_OPPS_FLOOR, round(config.TOV_MIN_OPPS * 11 / 82))
    assert metrics.rank_mask(frame, m, frame).tolist() == [False, True, False]
    full = frame.assign(gp=[82, 82, 82])
    assert metrics.min_tov_opps(full) == config.TOV_MIN_OPPS        # full season: the full minimum
    row = pd.Series({"tov_pct": 0.0, "tov_opps": 26.0, "pts": 5.0})
    prow = pd.Series({"tov_pct": 100.0, "pts": 40.0})
    assert [t["key"] for t in metrics.top_percentiles(row, prow, tov_min=need)] == ["pts"]
    assert metrics.top_percentiles(row, prow)[0]["key"] == "tov_pct"    # no minimum given: unchanged


def test_ts_pct_needs_enough_attempts():
    import pandas as pd
    from hoopscholar import config
    # tsa = gp * (fga + 0.44 * fta): 11 * 3 = 33, 11 * 9 = 99, 6 * 4 = 24
    frame = pd.DataFrame({"gp": [11, 11, 6], "fga": [3.0, 8.0, 4.0], "fta": [0.0, 2.5, 0.0],
                          "ts_pct": [1.0, 0.6, 0.9]})
    frame["tsa"] = frame["gp"] * (frame["fga"] + 0.44 * frame["fta"])
    m = metrics.METRIC_BY_KEY["ts_pct"]
    need = metrics.min_tsa(frame)
    assert need == max(config.TS_TSA_FLOOR, round(config.TS_MIN_TSA * 11 / 82))  # 54 at 11 games
    assert metrics.rank_mask(frame, m, frame).tolist() == [False, True, False]
    assert metrics.rank_minimum(m, frame)["value"] == need
    assert metrics.min_tsa(frame.assign(gp=[82, 82, 82])) == config.TS_MIN_TSA


if __name__ == "__main__":
    test_backfill_then_nightly()
