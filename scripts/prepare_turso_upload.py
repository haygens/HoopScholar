"""Make the one-time upload file for Turso from the local database.

    python scripts/prepare_turso_upload.py --db data/hoopscholar-full.db --out <path>/hoopscholar-upload.db

Copies the database (SQLite's backup API, so it's consistent) and sets what
Turso's upload endpoint requires: WAL journal mode, 4096-byte pages,
auto_vacuum off, UTF-8. Everything in the file is about to be on Turso, so
the pending-changes list is cleared in both the copy and the source; run the
upload before the next update.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db, turso_sync  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"source database (default {config.DB_PATH})")
    ap.add_argument("--out", required=True, help="upload file to write (outside the repo)")
    args = ap.parse_args()
    src_path, out = Path(args.db or config.DB_PATH), Path(args.out)
    if out.resolve() == src_path.resolve():
        sys.exit("--out must be a different file")
    for f in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        f.unlink(missing_ok=True)

    src = db.connect(src_path)
    turso_sync.clear_pending(src)
    dst = sqlite3.connect(out)
    src.backup(dst)
    src.close()

    dst.execute("DELETE FROM meta WHERE key = ?", (turso_sync.PENDING_KEY,))
    dst.commit()
    checks = {
        "page_size": dst.execute("PRAGMA page_size").fetchone()[0],
        "auto_vacuum": dst.execute("PRAGMA auto_vacuum").fetchone()[0],
        "encoding": dst.execute("PRAGMA encoding").fetchone()[0],
        "journal_mode": dst.execute("PRAGMA journal_mode = WAL").fetchone()[0],
    }
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checks["integrity"] = dst.execute("PRAGMA quick_check").fetchone()[0]
    dst.close()  # last connection closing folds the WAL in; the file keeps WAL mode in its header
    ok = checks == {"page_size": 4096, "auto_vacuum": 0, "encoding": "UTF-8", "journal_mode": "wal", "integrity": "ok"}
    print(f"{out}: {out.stat().st_size:,} bytes; {checks}")
    leftovers = [p.name for p in out.parent.glob(out.name + "-*")]
    if leftovers:
        print(f"Warning: leftover files {leftovers}")
    if not ok:
        sys.exit("The file doesn't meet Turso's upload requirements (see above).")
    print("Ready to upload.")


if __name__ == "__main__":
    main()
