"""Push changes from the local SQLite file to Turso (the weekly job's last step).

The local file stays the source of truth: the weekly update writes it with
sqlite3 as before, and records what changed in meta 'turso_pending':

    seasons  seasons whose derived tables were rebuilt (replaced on Turso)
    since    {season: first game date pulled, or null for the whole season}
             -> those games, their box scores and shots are upserted
    games    extra game ids whose rows changed (neutral-site fixes)

push() sends exactly that, then the small tables (teams, players, daily
pools) in full, and meta last, so the site only sees the new last_updated
once the data is there. On success the pending list is cleared; if anything
fails it stays, and the next push sends it all again (every write is an
upsert or a per-season replace, so resending is safe).

Cost on Turso (it bills every row a statement scans or writes):
- nothing is read back except one meta row (the schema fingerprint); the
  schema is only synced when schema.sql changed;
- a season replace deletes through a season index, so it reads only that
  season's rows;
- rows go in a few large statements (integers and text as literals, floats as
  exact parameters; up to MAX_ROWS rows, MAX_SQL_CHARS characters or
  MAX_PARAMS parameters each), so a weekly push is a few dozen requests.

Connection: TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (a full-access token).
"""
import hashlib
import json
import math
import os

from . import db

PENDING_KEY = "turso_pending"
SCHEMA_KEY = "schema_hash"          # on Turso only: fingerprint of the schema.sql it has
# Rebuilt per season by pipeline.refresh_season (and the season's stats by the
# update itself): replaced season by season.
SEASON_TABLES = ["player_season_stats", "player_shot_profile", "player_season_summary", "player_zone_splits",
                 "league_zone_averages", "league_hex_bins", "player_similar", "season_info"]
GAME_TABLES = ["games", "player_game_logs", "shots"]      # upserted for the pending games
WHOLE_TABLES = ["teams", "players", "games_daily_pool"]   # small: sent in full every time
LOCAL_ONLY = {"ingest_log", "backfill_progress"}          # never sent; the site doesn't read them
REMOTE_ONLY_META = {SCHEMA_KEY}
MAX_ROWS = 5000                                           # rows per INSERT statement
MAX_SQL_CHARS = 1_000_000                                 # characters per INSERT statement
MAX_PARAMS = 30000                                        # float parameters per statement (SQLite allows 32766)


# ------------------------------------------------------------------ pending changes

def get_pending(conn) -> dict:
    raw = db.get_meta(conn, PENDING_KEY)
    p = json.loads(raw) if raw else {}
    return {"seasons": sorted(set(p.get("seasons", []))), "since": dict(p.get("since", {})),
            "games": sorted(set(p.get("games", [])))}


def add_pending(conn, seasons=(), since: dict | None = None, games=()) -> None:
    """Record changes for the next push. For `since`, the earlier date wins and
    None (the whole season) beats any date."""
    p = get_pending(conn)
    p["seasons"] = sorted(set(p["seasons"]) | set(seasons))
    for s, d in (since or {}).items():
        if s in p["since"]:
            old = p["since"][s]
            p["since"][s] = None if old is None or d is None else min(old, d)
        else:
            p["since"][s] = d
    p["games"] = sorted(set(p["games"]) | {str(g) for g in games})
    db.set_meta(conn, PENDING_KEY, json.dumps(p))
    conn.commit()


def clear_pending(conn) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (PENDING_KEY,))
    conn.commit()


def is_empty(p: dict) -> bool:
    return not (p["seasons"] or p["since"] or p["games"])


def schema_hash() -> str:
    return hashlib.sha256(db.SCHEMA.read_bytes()).hexdigest()[:16]


# ------------------------------------------------------------------ remote

def connect_remote():
    """Full-access libsql connection to Turso from TURSO_DATABASE_URL / TURSO_AUTH_TOKEN."""
    import libsql
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        raise RuntimeError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (full-access token) to push to Turso.")
    return libsql.connect(url, auth_token=token)


class Counted:
    """Wraps a remote connection and counts the requests sent through it."""

    def __init__(self, conn):
        self.conn, self.requests = conn, 0

    def execute(self, sql, params=()):
        self.requests += 1
        return self.conn.execute(sql, tuple(params))

    def executescript(self, sql):
        self.requests += 1
        return self.conn.executescript(sql)

    def commit(self):
        self.requests += 1
        return self.conn.commit()


def literal(v) -> str:
    """A non-float Python value as an SQL literal. Floats are never written as
    text: SQLite's decimal-to-double conversion isn't exact for every value
    (1e-300 reads back as 9.999999999999999e-301), so they go as parameters."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "X'" + bytes(v).hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def upsert_clause(cols: list[str], key: list[str]) -> str:
    """ON CONFLICT ... DO UPDATE that rewrites a row only when something changed.

    Never INSERT OR REPLACE here: REPLACE deletes the old row and inserts a new
    one, and Turso enforces foreign keys, so deleting a parent row (a team, a
    player) makes SQLite scan every child table for references, and again on
    the re-insert. With no index on shots.team_id that was ~2 million rows per
    team: pushing the 30 teams read ~130 million rows. An in-place update that
    doesn't touch the key does no foreign-key scan."""
    rest = [c for c in cols if c not in key]
    if not rest:
        return f" ON CONFLICT ({', '.join(key)}) DO NOTHING"
    sets = ", ".join(f"{c} = excluded.{c}" for c in rest)
    changed = " OR ".join(f"{c} IS NOT excluded.{c}" for c in rest)
    return f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {sets} WHERE {changed}"


def insert_statements(table: str, cols: list[str], rows, key: list[str] | None = None):
    """Yield (sql, params) multi-row INSERT statements: integers, text and NULLs
    as literals, floats as exact bound parameters (NaN -> NULL, as sqlite3
    stores it). With `key`, rows already there are updated in place (see
    upsert_clause). Each statement holds at most MAX_ROWS rows, about
    MAX_SQL_CHARS characters and MAX_PARAMS parameters."""
    head = f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
    tail = upsert_clause(cols, key) if key else ""
    parts, params, size = [], [], len(head) + len(tail)
    for r in rows:
        cells, mine = [], []
        for v in r:
            if isinstance(v, float):
                if math.isnan(v):
                    cells.append("NULL")
                else:
                    cells.append("?")
                    mine.append(v)
            else:
                cells.append(literal(v))
        lit = "(" + ",".join(cells) + ")"
        if parts and (len(parts) >= MAX_ROWS or size + len(lit) + 1 > MAX_SQL_CHARS
                      or len(params) + len(mine) > MAX_PARAMS):
            yield head + ",".join(parts) + tail, params
            parts, params, size = [], [], len(head) + len(tail)
        parts.append(lit)
        params += mine
        size += len(lit) + 1
    if parts:
        yield head + ",".join(parts) + tail, params


def _send(remote, table: str, cols: list[str], rows: list, key: list[str] | None = None) -> int:
    for sql, params in insert_statements(table, cols, rows, key):
        remote.execute(sql, params)
    return len(rows)


def _key(conn, table: str) -> list[str]:
    """Primary-key columns, in key order."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _local_rows(local, sql: str, params=()) -> list[tuple]:
    return [tuple(r) for r in local.execute(sql, tuple(params)).fetchall()]


def _chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def sync_schema(local, remote) -> list[str]:
    """Bring Turso's schema up to the local one: add columns the local tables
    have (the same rule as db.migrate: new columns are nullable or have a
    default), then run schema.sql (CREATE ... IF NOT EXISTS) for new tables and
    indexes. Returns the columns added."""
    remote_tables = {r[0] for r in remote.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    added = []
    for (table,) in local.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        if table not in remote_tables or table in LOCAL_ONLY:
            continue
        have = {r[1] for r in remote.execute(f"PRAGMA table_info({table})").fetchall()}
        for _cid, name, ctype, _notnull, default, _pk in local.execute(f"PRAGMA table_info({table})"):
            if name not in have:
                remote.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}"
                               + (f" DEFAULT {default}" if default is not None else ""))
                added.append(f"{table}.{name}")
    remote.executescript(db.SCHEMA.read_text())
    remote.commit()
    return added


def ensure_schema(local, remote) -> list[str] | None:
    """Sync the schema only when Turso's stored fingerprint differs from
    schema.sql's (one 1-row read otherwise). Returns columns added, or None
    when nothing needed doing."""
    want = schema_hash()
    have = remote.execute("SELECT value FROM meta WHERE key = ?", (SCHEMA_KEY,)).fetchone()
    if have and have[0] == want:
        return None
    added = sync_schema(local, remote)
    remote.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   (SCHEMA_KEY, want))
    remote.commit()
    return added


def pending_games(local, p: dict) -> list[str]:
    ids = set(p["games"])
    for season, since in p["since"].items():
        if since:
            ids |= {r[0] for r in local.execute("SELECT game_id FROM games WHERE season = ? AND game_date >= ?",
                                                 (season, since))}
        else:
            ids |= {r[0] for r in local.execute("SELECT game_id FROM games WHERE season = ?", (season,))}
    return sorted(ids)


def _work(local) -> list[tuple]:
    """Everything a push sends, in order: (group, table, cols, rows, key, delete_season).
    key None = plain INSERT after the season's DELETE; otherwise upsert on key."""
    p = get_pending(local)
    out = []
    for season in p["seasons"]:
        for t in SEASON_TABLES:
            cols = _cols(local, t)
            out.append(("season", t, cols, _local_rows(local, f"SELECT {', '.join(cols)} FROM {t} WHERE season = ?",
                                                        (season,)), None, season))
    ids = pending_games(local, p)
    for t in GAME_TABLES:
        cols = _cols(local, t)
        rows = []
        for chunk in _chunks(ids, 500):
            rows += _local_rows(local, f"SELECT {', '.join(cols)} FROM {t} WHERE game_id IN ({','.join('?' * len(chunk))})",
                                chunk)
        out.append(("games", t, cols, rows, _key(local, t), None))
    for t in WHOLE_TABLES:
        cols = _cols(local, t)
        out.append(("final", t, cols, _local_rows(local, f"SELECT {', '.join(cols)} FROM {t}"), _key(local, t), None))
    out.append(("final", "meta", ["key", "value"],
                _local_rows(local, "SELECT key, value FROM meta WHERE key != ?", (PENDING_KEY,)), ["key"], None))
    return out


def plan(local) -> dict:
    """What a push would do, without connecting: rows per table, requests, and
    an estimate of Turso rows read (a season delete reads that season's rows
    through its index; each upserted row is one key lookup)."""
    rows, requests, reads = {}, 1, 1          # the schema fingerprint check
    for group, t, cols, data, key, season in _work(local):
        rows[t] = rows.get(t, 0) + len(data)
        stmts = sum(1 for _ in insert_statements(t, cols, data, key))
        if season is not None:
            requests += 2 + stmts             # delete, inserts, commit
            reads += len(data)                # rows the delete removes (about this many)
        else:
            requests += stmts
            reads += len(data)                # one key lookup per upserted row
    requests += 2                             # commits for the game rows and the final group
    return {"rows": rows, "requests": requests, "est_rows_read": reads}


def push(local, remote, log=print) -> dict:
    """Send the pending changes (see the module docstring).
    Returns {"rows": {table: rows sent}, "requests": requests made}."""
    r = Counted(remote)
    added = ensure_schema(local, r)
    if added:
        log(f"  Turso schema: added {', '.join(added)}")
    elif added is not None:
        log("  Turso schema: synced (no new columns)")

    sent: dict = {}
    work = _work(local)
    for group, t, cols, data, key, season in work:
        if group != "season":
            continue
        r.execute(f"DELETE FROM {t} WHERE season = ?", (season,))   # through the season index
        _send(r, t, cols, data, key)
        r.commit()   # this table's delete and insert land together
        sent[t] = sent.get(t, 0) + len(data)
    for season in get_pending(local)["seasons"]:
        log(f"  season {season}: derived tables replaced")

    for group, t, cols, data, key, _ in work:
        if group == "games":
            sent[t] = sent.get(t, 0) + _send(r, t, cols, data, key)
    r.commit()
    if sent.get("games"):
        log(f"  {sent['games']} games: {sent.get('player_game_logs', 0)} box scores, {sent.get('shots', 0)} shots")

    # small tables, then meta (last_updated tells the front end a new version is ready), one transaction
    for group, t, cols, data, key, _ in work:
        if group == "final":
            sent[t] = _send(r, t, cols, data, key)
    r.commit()
    clear_pending(local)
    return {"rows": sent, "requests": r.requests}
