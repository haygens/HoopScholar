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
    for col in migrate(conn):
        print(f"migrated: added column {col}")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any column schema.sql declares that this (older) database lacks.

    Builds schema.sql in memory and diffs PRAGMA table_info, so columns that
    already exist are skipped and running it again is a no-op."""
    ref = sqlite3.connect(":memory:")
    ref.executescript(SCHEMA.read_text())
    added = []
    tables = [r[0] for r in ref.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for _cid, name, ctype, _notnull, default, _pk in ref.execute(f"PRAGMA table_info({table})"):
            if name not in have:
                ddl = f"ALTER TABLE {table} ADD COLUMN {name} {ctype}"
                if default is not None:
                    ddl += f" DEFAULT {default}"
                conn.execute(ddl)
                added.append(f"{table}.{name}")
    ref.close()
    conn.commit()
    return added


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
