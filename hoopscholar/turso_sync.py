"""Push changes from the local SQLite file to Turso (the weekly job's last step).

The local file stays the source of truth: the weekly update writes it with
sqlite3 as before, and records what changed in meta 'turso_pending':

    seasons  seasons whose derived tables were rebuilt (replaced on Turso)
    since    {season: first game date pulled, or null for the whole season}
             -> those games, their box scores and shots are upserted
    games    extra game ids whose rows changed (neutral-site fixes)

push() sends exactly that in multi-row batches, then the small tables
(teams, players, daily pools) in full, and meta last, so the site only sees
the new last_updated once the data is there. On success the pending list is
cleared; if anything fails it stays, and the next push sends it all again
(every write is an upsert or a per-season replace, so resending is safe).

Connection: TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (a full-access token).
"""
import json
import os

from . import db

PENDING_KEY = "turso_pending"
# Rebuilt per season by pipeline.refresh_season (and the season's stats by the
# update itself): replaced season by season.
SEASON_TABLES = ["player_season_stats", "player_shot_profile", "player_season_summary", "player_zone_splits",
                 "league_zone_averages", "league_hex_bins", "player_similar", "season_info"]
GAME_TABLES = ["games", "player_game_logs", "shots"]      # upserted for the pending games
WHOLE_TABLES = ["teams", "players", "games_daily_pool"]   # small: sent in full every time
LOCAL_ONLY = {"ingest_log", "backfill_progress"}          # never sent; the site doesn't read them
MAX_PARAMS = 30000                                        # per statement (SQLite allows 32766)


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


# ------------------------------------------------------------------ remote

def connect_remote():
    """Full-access libsql connection to Turso from TURSO_DATABASE_URL / TURSO_AUTH_TOKEN."""
    import libsql
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        raise RuntimeError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (full-access token) to push to Turso.")
    return libsql.connect(url, auth_token=token)


def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _send(remote, table: str, cols: list[str], rows: list, verb: str = "INSERT OR REPLACE") -> int:
    """Insert rows with multi-row statements (one round trip per few hundred rows)."""
    per = max(1, min(500, MAX_PARAMS // len(cols)))
    one = "(" + ", ".join("?" * len(cols)) + ")"
    for i in range(0, len(rows), per):
        chunk = rows[i:i + per]
        remote.execute(f"{verb} INTO {table} ({', '.join(cols)}) VALUES {', '.join([one] * len(chunk))}",
                       [v for r in chunk for v in r])
    return len(rows)


def _local_rows(local, sql: str, params=()) -> list[tuple]:
    return [tuple(r) for r in local.execute(sql, tuple(params)).fetchall()]


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


def plan(local) -> dict:
    """What a push would send: {table: rows}, from the pending list."""
    p = get_pending(local)
    out = {}
    for t in SEASON_TABLES:
        out[t] = sum(local.execute(f"SELECT COUNT(*) FROM {t} WHERE season = ?", (s,)).fetchone()[0]
                     for s in p["seasons"])
    ids = pending_games(local, p)
    for t in GAME_TABLES:
        out[t] = sum(local.execute(f"SELECT COUNT(*) FROM {t} WHERE game_id IN ({','.join('?' * len(c))})", c)
                     .fetchone()[0] for c in _chunks(ids, 500))
    for t in WHOLE_TABLES:
        out[t] = local.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    out["meta"] = local.execute("SELECT COUNT(*) FROM meta WHERE key != ?", (PENDING_KEY,)).fetchone()[0]
    return out


def pending_games(local, p: dict) -> list[str]:
    ids = set(p["games"])
    for season, since in p["since"].items():
        if since:
            ids |= {r[0] for r in local.execute("SELECT game_id FROM games WHERE season = ? AND game_date >= ?",
                                                 (season, since))}
        else:
            ids |= {r[0] for r in local.execute("SELECT game_id FROM games WHERE season = ?", (season,))}
    return sorted(ids)


def _chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def push(local, remote, log=print) -> dict:
    """Send the pending changes (see the module docstring). Returns {table: rows sent}."""
    p = get_pending(local)
    sent: dict = {}
    added = sync_schema(local, remote)
    if added:
        log(f"  Turso schema: added {', '.join(added)}")

    for season in p["seasons"]:
        for t in SEASON_TABLES:
            cols = _cols(local, t)
            rows = _local_rows(local, f"SELECT {', '.join(cols)} FROM {t} WHERE season = ?", (season,))
            remote.execute(f"DELETE FROM {t} WHERE season = ?", (season,))
            _send(remote, t, cols, rows, "INSERT")
            remote.commit()   # delete and insert land together
            sent[t] = sent.get(t, 0) + len(rows)
        log(f"  season {season}: derived tables replaced")

    ids = pending_games(local, p)
    for t in GAME_TABLES:
        cols = _cols(local, t)
        for chunk in _chunks(ids, 500):
            rows = _local_rows(local, f"SELECT {', '.join(cols)} FROM {t} WHERE game_id IN ({','.join('?' * len(chunk))})",
                               chunk)
            sent[t] = sent.get(t, 0) + _send(remote, t, cols, rows)
        remote.commit()
    if ids:
        log(f"  {len(ids)} games: {sent.get('games', 0)} game rows, {sent.get('player_game_logs', 0)} box scores, "
            f"{sent.get('shots', 0)} shots")

    for t in WHOLE_TABLES:
        cols = _cols(local, t)
        sent[t] = _send(remote, t, cols, _local_rows(local, f"SELECT {', '.join(cols)} FROM {t}"))
        remote.commit()

    # meta last: last_updated tells the front end a new version is ready
    sent["meta"] = _send(remote, "meta", ["key", "value"],
                         _local_rows(local, "SELECT key, value FROM meta WHERE key != ?", (PENDING_KEY,)))
    remote.commit()
    clear_pending(local)
    return sent
