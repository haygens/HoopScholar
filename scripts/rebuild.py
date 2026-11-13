"""Rebuild the website's precomputed tables from data already in the database.
No NBA.com calls.

    python scripts/rebuild.py                      # every season, data/hoopscholar.db
    python scripts/rebuild.py --season 2025-26     # one season
    python scripts/rebuild.py --db data/hoopscholar-full.db

Run it after changing how percentiles, shot profiles or similar players are
computed, or once on a database from before these tables existed. The weekly
job keeps them current by itself afterwards. last_updated is left alone, since
no new games come in.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"database path (default {config.DB_PATH})")
    ap.add_argument("--season", default=None, help="only this season (default: every season with stats)")
    args = ap.parse_args()

    t0 = time.time()
    conn = db.connect(args.db)
    db.init_schema(conn)
    # Databases from before this script record when the current season was last
    # pulled only in ingest_log; daily pools need that date (see refresh_daily_pools).
    if not db.get_meta(conn, "current_loaded_on"):
        r = conn.execute("SELECT MAX(run_at) FROM ingest_log WHERE season = ? AND kind IN ('nightly', 'backfill')",
                         (config.CURRENT_SEASON,)).fetchone()
        if r[0]:
            db.set_meta(conn, "current_loaded_on", r[0][:10])
            print(f"current season last pulled on {r[0][:10]} (from ingest_log)")
    stored = [r[0] for r in conn.execute("SELECT DISTINCT season FROM player_season_stats ORDER BY season")]
    seasons = [args.season] if args.season else stored
    print(f"Rebuilding {', '.join(seasons) or 'nothing'} in {args.db or config.DB_PATH}")
    pipeline.build_derived(conn, seasons, network=False)
    db.log_run(conn, "rebuild", args.season or "all", f"seasons={len(seasons)}")
    conn.commit()
    db.compact(conn)
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
