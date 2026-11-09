"""Load one full season from stats.nba.com into the database.

    python scripts/backfill.py --season 2025-26

About 39 requests (players, games, player game logs, 30 teams of shots, 4 stat tables), so expect
1-3 minutes. Safe to re-run: every write is an upsert.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default="2025-26", help="e.g. 2025-26")
    ap.add_argument("--db", default=None, help=f"database path (default {config.DB_PATH})")
    ap.add_argument("--skip-shots", action="store_true", help="skip the 30 shot-chart calls")
    args = ap.parse_args()

    t0 = time.time()
    conn = db.connect(args.db)
    db.init_schema(conn)
    if db.get_meta(conn, "data_source") == "demo":
        sys.exit("This database holds demo data. Use a different --db or delete it first.")

    print(f"Backfilling {args.season} into {args.db or config.DB_PATH}")
    pipeline.ingest_reference(conn, args.season)
    pipeline.ingest_games(conn, args.season)
    pipeline.ingest_player_games(conn, args.season)
    shots = 0 if args.skip_shots else pipeline.ingest_shots(conn, args.season)
    pipeline.ingest_season_stats(conn, args.season)
    pipeline.finish(conn, args.season, "backfill", f"shots={shots}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
