"""The ingest steps shared by backfill and nightly runs."""
import datetime as dt
import json

from . import config, db, fetch, games, load, metrics


def ingest_reference(conn, season: str) -> None:
    """Teams, plus the player index. Only the current season's index sets
    current team and roster status. An older season's index adds players we're
    missing (with their bio, no current team, inactive) and fills blank bio
    fields, so backfilling an old season can't put anyone back on an old team."""
    n = load.load_teams(conn, fetch.teams())
    print(f"teams: {n}")
    past = season < config.CURRENT_SEASON
    n = load.load_players(conn, fetch.player_index(season), past=past)
    print(f"players: {n}" + (" new (past season: current teams left as they are)" if past else ""))
    conn.commit()


def ingest_games(conn, season: str, date_from: str | None = None) -> int:
    n = load.load_games(conn, fetch.team_game_log(season, date_from), season)
    conn.commit()
    print(f"games: {n}")
    return n


def ingest_player_games(conn, season: str) -> int:
    """Player game logs, one request. Picks its own start date (the day before the
    last logged game, or the season start if none are stored yet), so the first
    weekly run after this was added fills the whole season by itself."""
    row = conn.execute("""SELECT MAX(g.game_date) FROM player_game_logs l JOIN games g USING (game_id)
                          WHERE l.season = ?""", (season,)).fetchone()
    since = day_before(row[0]) if row[0] else None
    n = load.load_player_games(conn, fetch.player_game_log(season, since), season)
    conn.commit()
    print(f"player game logs: {n} (since {since or 'season start'})")
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
        totals = fetch.player_stats(season, "Base", per_mode="Totals")
    except RuntimeError as e:
        print(f"  season totals unavailable, TOV% from per-game averages: {e}")
        totals = None
    try:
        track = fetch.player_tracking(season, "SpeedDistance")
    except RuntimeError as e:
        print(f"  tracking stats unavailable, skipping: {e}")
        track = None
    n = load.load_season_stats(conn, season, base, adv, track, totals)
    conn.commit()
    print(f"season stat lines: {n}")
    return n


def finish(conn, season: str, kind: str, detail: str, rebuild_all: bool = False,
           today: dt.date | None = None) -> None:
    """Build everything the website reads. Rebuilds `season`, plus any stored
    season that has never been built (so the first run after an upgrade, or
    after backfilling a season, fills it in). rebuild_all=True redoes every
    season, for when the percentile or profile logic changes."""
    today = today or dt.date.today()
    if season == config.CURRENT_SEASON:
        # This run just pulled the current season, so every game before today is loaded.
        db.set_meta(conn, "current_loaded_on", today.isoformat())
    stored = {r[0] for r in conn.execute("SELECT DISTINCT season FROM player_season_stats")}
    built = {r[0] for r in conn.execute("SELECT season FROM season_info")}
    build_derived(conn, stored if rebuild_all else ({season} & stored) | (stored - built))
    db.set_meta(conn, "last_updated", db.now_iso())
    db.set_meta(conn, "data_source", "nba.com")
    db.log_run(conn, kind, season, detail)
    conn.commit()
    db.compact(conn)


def build_derived(conn, seasons, network: bool = True) -> None:
    """Everything the website reads: neutral-site games' home teams, shots.is_home,
    each given season's derived tables, search columns and the stored daily
    pools. Only orient_neutral_games may call NBA.com, and only with network=True."""
    orient_neutral_games(conn, fetch_missing=network)
    fill_shot_home(conn)
    for s in sorted(seasons):
        refresh_season(conn, s)
    refresh_player_search(conn)
    loaded_on = db.get_meta(conn, "current_loaded_on")
    n = refresh_daily_pools(conn, dt.date.fromisoformat(loaded_on)) if loaded_on else 0
    print(f"daily pools: {n} new week(s)")


def refresh_season(conn, season: str) -> None:
    """Rebuild one season's derived tables: shot profile, summary (stats +
    percentiles within this season), season_info, zone splits, league zone
    averages, league hex bins and similar players. Local data only."""
    n = metrics.refresh_shot_profiles(conn, season)
    frame = metrics.season_frame(conn, season)   # uses the shot profiles above
    pcts = metrics.percentiles(frame) if not frame.empty else None
    eligible = games.eligible(conn, season)["player_id"]
    rows = metrics.summary_rows(frame, pcts, season, eligible) if pcts is not None else []
    cols = metrics.SUMMARY_COLS
    conn.execute("DELETE FROM player_season_summary WHERE season = ?", (season,))
    conn.executemany(f"INSERT INTO player_season_summary ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)

    conn.execute("DELETE FROM season_info WHERE season = ?", (season,))
    if not frame.empty:
        mins = metrics.minimums(frame)
        conn.execute("""INSERT INTO season_info (season, players, pool_size, max_gp, pool_min_fga,
                        min_tov_opps, min_tsa, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                     (season, len(frame), int(frame["qualified"].sum()), int(frame["gp"].max() or 0),
                      int(mins["pool_min_fga"]), int(mins["min_tov_opps"]), int(mins["min_tsa"]), db.now_iso()))

    conn.execute("DELETE FROM player_zone_splits WHERE season = ?", (season,))
    conn.executemany("INSERT INTO player_zone_splits VALUES (?,?,?,?,?)", metrics.zone_splits(conn, season))
    conn.execute("DELETE FROM league_zone_averages WHERE season = ?", (season,))
    conn.executemany("INSERT INTO league_zone_averages VALUES (?,?,?,?,?)",
                     [(season, z, v["fga"], v["fgm"], v["fg_pct"])
                      for z, v in metrics.league_zone_averages(conn, season).items() if z is not None])
    conn.execute("DELETE FROM league_hex_bins WHERE season = ?", (season,))
    conn.executemany("INSERT INTO league_hex_bins VALUES (?,?,?,?,?)", metrics.league_hex_rows(conn, season))

    sim = metrics.similar_players(frame, pcts, season) if pcts is not None else []
    conn.execute("DELETE FROM player_similar WHERE season = ?", (season,))
    conn.executemany("INSERT INTO player_similar (player_id, season, rank, similar_id, score) VALUES (?,?,?,?,?)",
                     [(r["player_id"], r["season"], r["rank"], r["similar_id"], r["score"]) for r in sim])
    conn.commit()
    print(f"season {season}: {n} shot profiles, {len(rows)} summary rows "
          f"({int(frame['qualified'].sum()) if not frame.empty else 0} qualified, {len(eligible)} in games pool), "
          f"{len(sim)} similar")


def fill_shot_home(conn) -> int:
    """Set shots.is_home from games for shots that don't have it yet. Neutral-site
    games are left to orient_neutral_games: their stored order isn't a real home team."""
    n = conn.execute("""UPDATE shots SET is_home = (shots.team_id = g.home_team_id)
                        FROM games g WHERE g.game_id = shots.game_id AND shots.is_home IS NULL
                          AND g.neutral = 0 AND g.home_team_id IS NOT NULL""").rowcount
    conn.commit()
    return n


def orient_neutral_games(conn, fetch_missing: bool = True) -> int:
    """Give neutral-site games their designated home team. NBA.com's game log
    labels both teams '@' there (and games loaded before this fix have no home
    team at all), but the shot data's HTM column names the home team, and
    load_shots() turns it into shots.is_home. Points come from the box scores.
    Games whose shots predate HTM are fetched once, one call per game, when
    fetch_missing. Returns games fixed."""
    todo = conn.execute("""SELECT game_id, season FROM games g
                           WHERE (neutral = 1 OR home_team_id IS NULL OR away_team_id IS NULL)
                             AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.game_id = g.game_id AND s.is_home = 1
                                             AND s.team_id = g.home_team_id)""").fetchall()
    fixed = 0
    for game_id, season in todo:
        home = conn.execute("SELECT team_id FROM shots WHERE game_id = ? AND is_home = 1 LIMIT 1", (game_id,)).fetchone()
        if home is None and fetch_missing:
            df = fetch.shots_for_game(season, game_id)
            if not df.empty:
                load.load_shots(conn, df, season)  # upsert: same shots, now with is_home from HTM
                home = conn.execute("SELECT team_id FROM shots WHERE game_id = ? AND is_home = 1 LIMIT 1",
                                    (game_id,)).fetchone()
        if home is None:
            continue
        home = home[0]
        teams = {r[0] for r in conn.execute("SELECT DISTINCT team_id FROM player_game_logs WHERE game_id = ?", (game_id,))}
        teams |= {r[0] for r in conn.execute("SELECT DISTINCT team_id FROM shots WHERE game_id = ?", (game_id,))}
        others = teams - {home}
        if len(others) != 1:
            continue
        away = others.pop()
        pts = dict(conn.execute("SELECT team_id, SUM(pts) FROM player_game_logs WHERE game_id = ? GROUP BY team_id",
                                (game_id,)).fetchall())
        old = conn.execute("SELECT home_team_id, home_pts, away_team_id, away_pts FROM games WHERE game_id = ?",
                           (game_id,)).fetchone()
        stored = {old[0]: old[1], old[2]: old[3]}
        conn.execute("""UPDATE games SET home_team_id = ?, away_team_id = ?, home_pts = ?, away_pts = ?, neutral = 1
                        WHERE game_id = ?""",
                     (home, away, pts.get(home, stored.get(home)), pts.get(away, stored.get(away)), game_id))
        conn.execute("UPDATE shots SET is_home = (team_id = ?) WHERE game_id = ?", (home, game_id))
        fixed += 1
    conn.commit()
    if fixed:
        print(f"neutral-site games: {fixed} given their designated home team")
    return fixed


def refresh_player_search(conn) -> None:
    """Fill what search needs: first/last name on bare rows (split from the full
    name), and each player's latest season with stats and that season's team.
    Only rows that change are written."""
    blank = conn.execute("SELECT player_id, full_name FROM players WHERE first_name IS NULL OR last_name IS NULL").fetchall()
    conn.executemany("UPDATE players SET first_name = COALESCE(first_name, ?), last_name = COALESCE(last_name, ?) WHERE player_id = ?",
                     [(*load.split_name(n), pid) for pid, n in blank])
    conn.execute("""
        UPDATE players SET last_season = x.season, last_team = x.team_abbr
        FROM (SELECT player_id, season, team_abbr FROM player_season_stats s
              WHERE season = (SELECT MAX(season) FROM player_season_stats WHERE player_id = s.player_id)) x
        WHERE players.player_id = x.player_id
          AND (players.last_season IS NOT x.season OR players.last_team IS NOT x.team_abbr)""")
    conn.commit()


def refresh_daily_pools(conn, loaded_on: dt.date) -> int:
    """Store Guess the Player's weekly pools from the epoch through the week
    after `loaded_on` (the day the current season's games were last pulled).
    A stored week never changes: its pool only uses games before its cutoff
    (see games.py), and every week stored here has its cutoff on or before
    loaded_on's Monday, so those games are all loaded. Weeks not stored yet
    are computed live by the website. Returns weeks added."""
    have = {r[0] for r in conn.execute("SELECT week FROM games_daily_pool")}
    week = games.week_start(dt.date.fromisoformat(config.GAMES_EPOCH))
    last = games.week_start(loaded_on) + dt.timedelta(days=7)
    added = 0
    while week <= last:
        if week.isoformat() not in have:
            p = games.daily_pool(conn, week)
            conn.execute("INSERT INTO games_daily_pool (week, season, ids) VALUES (?,?,?)",
                         (week.isoformat(), p["season"], json.dumps(p["ids"])))
            added += 1
        week += dt.timedelta(days=7)
    conn.commit()
    return added


def last_game_date(conn, season: str) -> str | None:
    row = conn.execute("SELECT MAX(game_date) FROM shots WHERE season=?", (season,)).fetchone()
    return row[0]


def day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()
