"""Database connections and upsert helpers.

Writers (backfill, the weekly job, tests) always use a local SQLite file
through Python's sqlite3. The website reads through connect(readonly=True),
whose backend HOOP_DB_BACKEND picks:

    sqlite  (default)  the local file, read-only, via sqlite3
    libsql             the same local file via the libsql client (to test that path)
    turso              Turso Cloud via libsql; needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN
"""
import datetime as dt
import os
import sqlite3
import threading
from pathlib import Path
from typing import Sequence

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")
BACKENDS = ("sqlite", "libsql", "turso")


def backend() -> str:
    b = os.environ.get("HOOP_DB_BACKEND", "sqlite").strip().lower() or "sqlite"
    if b not in BACKENDS:
        raise ValueError(f"HOOP_DB_BACKEND={b!r}; use one of {', '.join(BACKENDS)}")
    return b


# ------------------------------------------------------------------ libsql wrapper
# libsql's Python client is close to sqlite3 but has no row_factory, cursors
# that can't be iterated, and executemany() that wants a list. This wraps it
# just enough for the website's read-only queries: rows come back as Row,
# which works like sqlite3.Row (row["col"], row[0], dict(row), row.keys()).

class Row(tuple):
    __slots__ = ()
    _index: dict  # set per query on a subclass

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._index[k])
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._index)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        desc = cur.description or ()
        self.description = desc
        self._row = type("Row", (Row,), {"_index": {d[0]: i for i, d in enumerate(desc)}})

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else self._row(r)

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class LibsqlConnection:
    """Read-only libsql connection with a sqlite3-like surface. A failed query
    reconnects and runs once more (a warm serverless function can hold a
    connection the server has since closed); safe because nothing here writes."""

    def __init__(self, open_fn):
        self._open = open_fn
        self._conn = open_fn()
        self._lock = threading.Lock()

    def execute(self, sql: str, params: Sequence = ()):
        params = tuple(params)
        with self._lock:
            try:
                return _Cursor(self._conn.execute(sql, params))
            except Exception:
                self._conn = self._open()
                return _Cursor(self._conn.execute(sql, params))

    def close(self):
        self._conn.close()

    def commit(self):  # nothing to commit on a read-only connection
        pass


def _libsql_connect(database: str, **kw) -> LibsqlConnection:
    import libsql  # only the website's libsql/turso backends need it
    return LibsqlConnection(lambda: libsql.connect(database, **kw))


def connect(path=None, readonly: bool = False):
    """Open the database.

    readonly=True is what the website uses; HOOP_DB_BACKEND picks where it
    reads from (see the module docstring). Writers always get a local sqlite3
    connection. On Vercel (where the VERCEL env var is set) the file system is
    read-only, so a local file is also opened as 'immutable', which tells
    SQLite not to look for lock or journal files.
    """
    path = Path(path or config.DB_PATH)
    if readonly and backend() == "turso":
        url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
        if not url or not token:
            raise RuntimeError("HOOP_DB_BACKEND=turso needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        return _libsql_connect(url, auth_token=token)
    if readonly and backend() == "libsql":
        if not path.exists():
            raise FileNotFoundError(f"Database not found: {path}")
        return _libsql_connect(str(path))
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


def rows(conn, sql: str, params: Sequence = ()) -> list[dict]:
    """Run a query and return plain dicts (works on every backend)."""
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(conn, sql: str, params: Sequence = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r is not None else None


def frame(conn, sql: str, params: Sequence = ()):
    """Query -> pandas DataFrame, on every backend (pandas.read_sql_query only
    knows sqlite3 connections)."""
    import pandas as pd
    cur = conn.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description or ()]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


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
