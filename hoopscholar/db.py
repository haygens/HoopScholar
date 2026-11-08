"""SQLite connection and upsert helpers."""
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path=None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # bulk loads arrive in any order
    return conn


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
