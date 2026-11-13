"""Push the changes recorded by the last update(s) from the local database to Turso.

    python scripts/push_turso.py --db data/hoopscholar-full.db              # push
    python scripts/push_turso.py --db data/hoopscholar-full.db --dry-run    # show what would be sent
    python scripts/push_turso.py --db data/hoopscholar-full.db --mark-synced

Needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (the full-access token) in the
environment; when they're only stored as Windows user variables (set in
another window), they're read from there. --mark-synced forgets the pending
changes without sending them (only right after uploading the whole file).
See hoopscholar/turso_sync.py for what is sent.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, turso_sync  # noqa: E402


def load_user_env(*names):
    """On Windows, pick up user environment variables set after this process started."""
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                if not os.environ.get(name):
                    try:
                        os.environ[name] = winreg.QueryValueEx(key, name)[0]
                    except FileNotFoundError:
                        pass
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"local database (default {config.DB_PATH})")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent and stop")
    ap.add_argument("--mark-synced", action="store_true", help="clear the pending list without sending")
    args = ap.parse_args()

    local = db.connect(args.db)
    db.init_schema(local)
    pending = turso_sync.get_pending(local)
    print(f"Pending: seasons {pending['seasons'] or '-'}, games since {pending['since'] or '-'}, "
          f"extra games {len(pending['games'])}")
    if args.mark_synced:
        turso_sync.clear_pending(local)
        print("Pending list cleared.")
        return
    if args.dry_run:  # local only: never connects to Turso
        p = turso_sync.plan(local)
        for t, n in p["rows"].items():
            print(f"  {t}: {n:,} rows")
        print(f"Would make about {p['requests']} requests; estimated Turso rows read ~{p['est_rows_read']:,}.")
        return
    if turso_sync.is_empty(pending):
        print("Nothing new to push (small tables and meta are sent anyway).")

    load_user_env("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")
    t0 = time.time()
    remote = turso_sync.connect_remote()
    res = turso_sync.push(local, remote)
    sent = res["rows"]
    print(f"Pushed {sum(sent.values()):,} rows to Turso in {time.time() - t0:.0f}s with {res['requests']} requests: "
          + ", ".join(f"{t} {n:,}" for t, n in sent.items() if n))


if __name__ == "__main__":
    main()
