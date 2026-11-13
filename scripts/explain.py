"""Print EXPLAIN QUERY PLAN for the SQL each main page actually runs.

    python scripts/explain.py                    # data/hoopscholar.db
    python scripts/explain.py --db data/hoopscholar-full.db --season 2025-26

Calls the real API endpoints in-process (read-only), records every statement
they execute, and explains each one. "SCAN <table>" reads the whole table (or
a whole season); "SEARCH ... USING INDEX" reads only matching rows, which is
what keeps Turso's rows-read count low. Rows returned are shown for scale.
"""
import argparse
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"database path (default {config.DB_PATH})")
    ap.add_argument("--season", default=None, help="season to use (default: latest)")
    ap.add_argument("--player", type=int, default=None, help="player id (default: most shots that season)")
    args = ap.parse_args()
    if args.db:
        config.DB_PATH = Path(args.db)
    os.environ["HOOP_DB_BACKEND"] = "sqlite"
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
    import api.main as site

    conn = db.connect(readonly=True)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)  # receives SQL with parameters filled in
    site.conn = lambda: conn  # endpoints run on worker threads; make them all use this traced connection
    t = TestClient(site.app)

    season = args.season or conn.execute("SELECT MAX(season) FROM season_info").fetchone()[0]
    pid = args.player or conn.execute("""SELECT player_id FROM player_zone_splits WHERE season = ?
                                         GROUP BY player_id ORDER BY SUM(fga) DESC LIMIT 1""", (season,)).fetchone()[0]
    team = conn.execute("SELECT team_abbr FROM player_season_summary WHERE player_id=? AND season=?",
                        (pid, season)).fetchone()[0]
    name = conn.execute("SELECT full_name FROM players WHERE player_id=?", (pid,)).fetchone()[0]
    pages = [
        ("player page", f"/api/players/{pid}?season={season}"),
        ("shot chart", f"/api/players/{pid}/shots?season={season}"),
        ("shot chart, last 10 games, away", f"/api/players/{pid}/shots?season={season}&last=10&loc=away"),
        ("game log", f"/api/players/{pid}/games?season={season}"),
        ("career percentiles", f"/api/players/{pid}/career"),
        ("leaderboard (points)", f"/api/leaderboard?stat=pts&season={season}"),
        ("leaderboard (TS%, with minimum)", f"/api/leaderboard?stat=ts_pct&season={season}"),
        ("team page", f"/api/teams/{team}?season={season}"),
        (f"search '{name[:3]}'", f"/api/search?q={name[:3]}"),
        ("games pool", "/api/games/pool"),
    ]
    sizes = {t_: conn.execute(f"SELECT COUNT(*) FROM {t_}").fetchone()[0]
             for t_ in ("shots", "players", "player_game_logs", "player_season_summary")}
    print(f"database: {config.DB_PATH}\nplayer {pid} ({name}), season {season}\ntable rows: {sizes}\n")
    for label, url in pages:
        statements.clear()
        r = t.get(url)
        ran = [s for s in statements if not s.lstrip().upper().startswith(("EXPLAIN", "PRAGMA"))]
        print(f"=== {label}: GET {url} -> {r.status_code}, {len(ran)} queries")
        conn.set_trace_callback(None)
        for sql in dict.fromkeys(ran):
            short = " ".join(sql.split())
            print(f"  {short[:150]}{'...' if len(short) > 150 else ''}")
            for step in conn.execute("EXPLAIN QUERY PLAN " + sql):
                print(f"      {step[3]}")
            print(f"      -> {len(conn.execute(sql).fetchall())} rows returned")
        conn.set_trace_callback(statements.append)
        print()


if __name__ == "__main__":
    main()
