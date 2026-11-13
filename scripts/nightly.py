"""Incremental update for the current season. Run once a day (see README).

    python scripts/nightly.py                 # current season
    python scripts/nightly.py --season 2026-27

Pulls only games and shots since the last stored game date (minus a day of
overlap, which upserts absorb), then refreshes season stats and shot profiles,
since those are season-to-date totals that change every night.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=config.CURRENT_SEASON)
    ap.add_argument("--db", default=None)
    ap.add_argument("--rebuild-all", action="store_true",
                    help="rebuild every season's precomputed tables, not just this one (after a metrics change)")
    args = ap.parse_args()

    t0 = time.time()
    conn = db.connect(args.db)
    db.init_schema(conn)
    if db.get_meta(conn, "data_source") == "demo":
        sys.exit("This database holds demo data. Point --db at your real database.")

    pipeline.nightly(conn, args.season, rebuild_all=args.rebuild_all)
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
