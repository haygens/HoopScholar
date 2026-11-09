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
    args = ap.parse_args()

    t0 = time.time()
    conn = db.connect(args.db)
    db.init_schema(conn)
    if db.get_meta(conn, "data_source") == "demo":
        sys.exit("This database holds demo data. Point --db at your real database.")

    last = pipeline.last_game_date(conn, args.season)
    since = pipeline.day_before(last) if last else None
    print(f"Nightly update for {args.season}; pulling since {since or 'season start'}")

    pipeline.ingest_reference(conn, args.season)   # rosters change (trades, signings)
    pipeline.ingest_games(conn, args.season, since)
    pipeline.ingest_player_games(conn, args.season)
    shots = pipeline.ingest_shots(conn, args.season, since)
    pipeline.ingest_season_stats(conn, args.season)
    pipeline.finish(conn, args.season, "nightly", f"since={since} shots={shots}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
