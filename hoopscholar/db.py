"""SQLite connection and upsert helpers."""
import datetime as dt
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path=None, readonly: bool = False) -> sqlite3.Connection:
    """Open the database.

    readonly=True is what the website uses. On Vercel (where the VERCEL env var
    is set) the file system is read-only, so the file is also opened as
    'immutable', which tells SQLite not to look for lock or journal files.
    """
    path = Path(path or config.DB_PATH)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"Database not found: {path}")
        uri = f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if os.environ.get("VERCEL") else "")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        # Plain rollback journal (not WAL) so the single .db file is always
        # complete and can be committed to Git and deployed as-is.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=OFF")  # bulk loads arrive in any order
    conn.row_factory = sqlite3.Row
    return conn


def compact(conn: sqlite3.Connection) -> None:
    """Fold any leftover journal into the main file and shrink it."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, rows: Sequence[dict], key: Sequence[str]) -> int:
    """INSERT ... ON CONFLICT(key) DO UPDATE for a list of dicts sharing the same keys."""
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updates = [c for c in cols if c not in key]
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT ({', '.join(key)}) DO "
        + (f"UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in updates)}" if updates else "NOTHING")
    )
    conn.executemany(sql, ([r.get(c) for c in cols] for r in rows))
    return len(rows)


def set_meta(conn, key: str, value: str) -> None:
    upsert(conn, "meta", [{"key": key, "value": value}], ["key"])


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def log_run(conn, kind: str, season: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ingest_log (run_at, kind, season, detail) VALUES (?,?,?,?)",
        (now_iso(), kind, season, detail),
    )


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
