"""The ingest steps shared by backfill and nightly runs."""
import datetime as dt

from . import db, fetch, load, metrics


def ingest_reference(conn, season: str) -> None:
    n = load.load_teams(conn, fetch.teams())
    print(f"teams: {n}")
    n = load.load_players(conn, fetch.player_index(season))
    print(f"players: {n}")
    conn.commit()


def ingest_games(conn, season: str, date_from: str | None = None) -> int:
    n = load.load_games(conn, fetch.team_game_log(season, date_from), season)
    conn.commit()
    print(f"games: {n}")
    return n


def ingest_shots(conn, season: str, date_from: str | None = None) -> int:
    """30 calls, one per team. Each commits on its own so a crash loses little."""
    total = 0
    team_ids = [r["team_id"] for r in conn.execute("SELECT team_id, abbr FROM teams ORDER BY abbr")]
    for i, tid in enumerate(team_ids, 1):
        df = fetch.shots_for_team(season, tid, date_from)
        n = load.load_shots(conn, df, season)
        conn.commit()
        total += n
        print(f"shots: team {i:>2}/{len(team_ids)} -> {n}")
    return total


def ingest_season_stats(conn, season: str) -> int:
    base = fetch.player_stats(season, "Base")
    adv = fetch.player_stats(season, "Advanced")
    try:
        track = fetch.player_tracking(season, "SpeedDistance")
    except RuntimeError as e:
        print(f"  tracking stats unavailable, skipping: {e}")
        track = None
    n = load.load_season_stats(conn, season, base, adv, track)
    conn.commit()
    print(f"season stat lines: {n}")
    return n


def finish(conn, season: str, kind: str, detail: str) -> None:
    n = metrics.refresh_shot_profiles(conn, season)
    print(f"shot profiles: {n}")
    db.set_meta(conn, "last_updated", db.now_iso())
    db.set_meta(conn, "data_source", "nba.com")
    db.log_run(conn, kind, season, detail)
    conn.commit()


def last_game_date(conn, season: str) -> str | None:
    row = conn.execute("SELECT MAX(game_date) FROM shots WHERE season=?", (season,)).fetchone()
    return row[0]


def day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()
