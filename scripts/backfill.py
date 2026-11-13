"""Load full seasons from stats.nba.com into the database.

    python scripts/backfill.py --season 2025-26
    python scripts/backfill.py --db data/hoopscholar-full.db --seasons 2024-25..2016-17 --delay 2.5 --stop-at "2026-11-15 22:00"

Per season: player index, games, player game logs, 30 teams of shots and 4
stat tables (about 37 requests), then the website's tables for that season.
Seasons run one at a time in the order given, and the file size is printed
after each.

Resuming: every step (and each team's shots) is recorded in backfill_progress
when it finishes. After an error, Ctrl+C, or --stop-at, run the same command
again and it skips what's done. A season that finished is skipped entirely;
pass --restart to load it again. Writes are upserts, so redoing a step is safe.

Pacing: --delay seconds before every call (default HOOP_REQUEST_DELAY, 1.2),
and --retries attempts per call with doubling waits plus jitter.
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, fetch, load, pipeline  # noqa: E402


class Paused(Exception):
    """--stop-at reached; progress so far is saved."""


def season_list(text: str) -> list[str]:
    """'2024-25..2016-17' (either direction) or '2024-25,2022-23' -> seasons in that order."""
    def start(s):
        if len(s) != 7 or s[4] != "-" or not s[:4].isdigit() or s[5:] != str(int(s[:4]) + 1)[-2:]:
            raise argparse.ArgumentTypeError(f"bad season {s!r}; use e.g. 2024-25")
        return int(s[:4])
    if ".." in text:
        a, b = (start(x.strip()) for x in text.split(".."))
        step = -1 if a > b else 1
        return [f"{y}-{str(y + 1)[-2:]}" for y in range(a, b + step, step)]
    out = [x.strip() for x in text.split(",") if x.strip()]
    for s in out:
        start(s)
    return out


def run_season(conn, season: str, *, skip_shots=False, deadline: dt.datetime | None = None,
               rebuild_all=False, restart=False) -> str:
    """Load one season, skipping steps already recorded. Returns 'done',
    'already done', or raises Paused (saved, rerun to resume) / RuntimeError
    (an NBA call kept failing; saved, rerun to resume)."""
    if restart:
        conn.execute("DELETE FROM backfill_progress WHERE season = ?", (season,))
        conn.commit()
    done = {r[0] for r in conn.execute("SELECT step FROM backfill_progress WHERE season = ?", (season,))}
    if "finish" in done:
        return "already done"

    def check_time(next_step):
        if deadline and dt.datetime.now() >= deadline:
            raise Paused(f"stop time {deadline:%Y-%m-%d %H:%M} reached before {season} {next_step}")

    def mark(step, detail=None):
        conn.execute("INSERT OR REPLACE INTO backfill_progress (season, step, done_at, detail) VALUES (?,?,?,?)",
                     (season, step, db.now_iso(), detail))
        conn.commit()

    def step(name, fn):
        if name in done:
            print(f"  {name}: done earlier, skipping")
            return
        check_time(name)
        detail = fn()
        mark(name, None if detail is None else str(detail))

    step("reference", lambda: pipeline.ingest_reference(conn, season))
    step("games", lambda: pipeline.ingest_games(conn, season))
    step("player_games", lambda: pipeline.ingest_player_games(conn, season))
    if not skip_shots:
        teams = [r[0] for r in conn.execute("SELECT team_id FROM teams ORDER BY abbr")]
        for i, tid in enumerate(teams, 1):
            name = f"shots:{tid}"
            if name in done:
                continue
            check_time(name)
            n = load.load_shots(conn, fetch.shots_for_team(season, tid), season)  # one executemany per team
            mark(name, n)
            print(f"shots: team {i:>2}/{len(teams)} -> {n}")
    step("season_stats", lambda: pipeline.ingest_season_stats(conn, season))
    shots = conn.execute("SELECT COUNT(*) FROM shots WHERE season = ?", (season,)).fetchone()[0]
    step("finish", lambda: pipeline.finish(conn, season, "backfill", f"shots={shots}", rebuild_all=rebuild_all))
    return "done"


def season_report(conn, path: Path, season: str) -> dict:
    one = lambda sql: conn.execute(sql, (season,)).fetchone()[0]
    return {
        "mb": path.stat().st_size / 1_000_000,
        "shots": one("SELECT COUNT(*) FROM shots WHERE season = ?"),
        "games": one("SELECT COUNT(*) FROM games WHERE season = ?"),
        "game_logs": one("SELECT COUNT(*) FROM player_game_logs WHERE season = ?"),
        "players": one("SELECT COUNT(*) FROM player_season_stats WHERE season = ?"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default=None, help="one season, e.g. 2025-26")
    ap.add_argument("--seasons", type=season_list, default=None,
                    help="several, run in this order: 2024-25..2016-17 or 2024-25,2023-24")
    ap.add_argument("--db", default=None, help=f"database path (default {config.DB_PATH})")
    ap.add_argument("--skip-shots", action="store_true", help="skip the 30 shot-chart calls")
    ap.add_argument("--delay", type=float, default=None, help=f"seconds before each NBA call (default {config.REQUEST_DELAY})")
    ap.add_argument("--retries", type=int, default=None, help=f"attempts per NBA call (default {config.REQUEST_RETRIES})")
    ap.add_argument("--stop-at", default=None, help="local time 'YYYY-MM-DD HH:MM': stop before the next step after it (rerun to resume)")
    ap.add_argument("--restart", action="store_true", help="forget saved progress for these seasons and load them again")
    ap.add_argument("--rebuild-all", action="store_true",
                    help="rebuild every season's precomputed tables, not just this one (after a metrics change)")
    args = ap.parse_args()

    seasons = args.seasons or [args.season or config.CURRENT_SEASON]
    if args.delay is not None:
        config.REQUEST_DELAY = args.delay
    if args.retries is not None:
        config.REQUEST_RETRIES = args.retries
    deadline = dt.datetime.strptime(args.stop_at, "%Y-%m-%d %H:%M") if args.stop_at else None
    path = Path(args.db or config.DB_PATH)

    conn = db.connect(path)
    db.init_schema(conn)
    if db.get_meta(conn, "data_source") == "demo":
        sys.exit("This database holds demo data. Use a different --db or delete it first.")

    print(f"Backfilling {', '.join(seasons)} into {path} (delay {config.REQUEST_DELAY}s, "
          f"{config.REQUEST_RETRIES} attempts per call{f', stop at {deadline:%Y-%m-%d %H:%M}' if deadline else ''})")
    start_mb = path.stat().st_size / 1_000_000
    finished = []
    for season in seasons:
        t0 = time.time()
        print(f"\n=== {season} ===")
        try:
            status = run_season(conn, season, skip_shots=args.skip_shots, deadline=deadline,
                                rebuild_all=args.rebuild_all, restart=args.restart)
        except Paused as e:
            print(f"\nPAUSED: {e}. Progress is saved; run the same command to resume.")
            sys.exit(3)
        except RuntimeError as e:
            print(f"\nSTOPPED: {e} (NBA.com kept failing). Progress is saved; run the same command to resume, "
                  f"maybe with a longer --delay.")
            sys.exit(2)
        r = season_report(conn, path, season)
        if status == "done":
            finished.append(r["mb"])
        print(f"--- {season} {status} in {time.time() - t0:.0f}s: file {r['mb']:.1f} MB | shots {r['shots']:,} | "
              f"games {r['games']:,} | game logs {r['game_logs']:,} | player-seasons {r['players']:,}")
    if finished:
        per = (finished[-1] - start_mb) / len(finished)
        print(f"\nAll done. {len(finished)} season(s) added {finished[-1] - start_mb:.1f} MB (~{per:.0f} MB each).")


if __name__ == "__main__":
    main()
