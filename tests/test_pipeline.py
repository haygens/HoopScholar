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
    check_api(path, int(q.index[0]))
    print(f"ok: backfill {n1} shots, nightly -> {n2}, {len(q)} qualified players")




def check_api(path, pid):
    """The site's endpoints against the database the pipeline just built."""
    import warnings
    from hoopscholar import config
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
    import api.main as site
    config.DB_PATH = path
    site._local.__dict__.clear()   # drop any connection to another database
    site._cache.clear()
    t = TestClient(site.app)
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
    abbr = p["stats"]["team_abbr"]
    team = get(f"/api/teams/{abbr}")
    assert any(r["player_id"] == pid for r in team["roster"])
    assert any(x["kind"] == "team" for x in get("/api/search", q=abbr))


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


if __name__ == "__main__":
    test_backfill_then_nightly()
