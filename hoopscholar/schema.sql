-- HoopScholar schema (SQLite; written to port to Postgres with minimal changes:
-- swap INTEGER PRIMARY KEY AUTOINCREMENT -> SERIAL and REAL -> DOUBLE PRECISION).
--
-- To add a column, just add it here: db.migrate() (run by init_schema, so by
-- backfill.py and nightly.py) ALTERs it into older databases. New columns must
-- be nullable or have a DEFAULT. Never rename or drop a column here.

CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER PRIMARY KEY,
    abbr        TEXT NOT NULL,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id     INTEGER PRIMARY KEY,
    full_name     TEXT NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    slug          TEXT,
    team_id       INTEGER REFERENCES teams(team_id),
    team_abbr     TEXT,
    jersey        TEXT,
    position      TEXT,
    height        TEXT,
    weight        TEXT,
    country       TEXT,
    college       TEXT,
    draft_year    TEXT,
    draft_round   TEXT,
    draft_number  TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    from_year     TEXT,
    to_year       TEXT,
    updated_at    TEXT,
    -- Latest season with stats and that season's team, for search (set by
    -- pipeline.refresh_player_search). team_id/team_abbr above are the CURRENT
    -- team and only the current season's player index sets them; a player
    -- first seen in a past season has them NULL. Team by season lives in
    -- player_season_stats.team_abbr.
    last_season   TEXT,
    last_team     TEXT
);
-- Search matches anywhere in the name (LIKE '%bron%'), which no index can
-- serve, so it reads the players table once per uncached search. Name indexes
-- would only add writes, so there are none (and older ones are dropped).
DROP INDEX IF EXISTS idx_players_name;
DROP INDEX IF EXISTS idx_players_full_nc;
DROP INDEX IF EXISTS idx_players_first_nc;
DROP INDEX IF EXISTS idx_players_last_nc;

CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,          -- NBA ids are zero-padded strings, e.g. '0022500001'
    season        TEXT NOT NULL,             -- '2025-26'
    season_type   TEXT NOT NULL DEFAULT 'Regular Season',
    game_date     TEXT NOT NULL,             -- ISO 'YYYY-MM-DD'
    home_team_id  INTEGER REFERENCES teams(team_id),
    away_team_id  INTEGER REFERENCES teams(team_id),
    home_pts      INTEGER,
    away_pts      INTEGER,
    -- 1 for neutral-site games (NBA Cup semifinals, Paris, Mexico City...): NBA.com's
    -- game log labels both teams "@", so home/away comes from the shot data's
    -- designated home team instead (pipeline.orient_neutral_games).
    neutral       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_games_season_date ON games(season, game_date);

-- One row per field-goal attempt. Coordinates are NBA.com's: tenths of a foot,
-- hoop at (0,0), x from -250 (left) to 250, y from -47.5 (baseline) upward.
CREATE TABLE IF NOT EXISTS shots (
    game_id            TEXT NOT NULL,
    game_event_id      INTEGER NOT NULL,
    season             TEXT NOT NULL,
    game_date          TEXT NOT NULL,
    player_id          INTEGER NOT NULL REFERENCES players(player_id),
    team_id            INTEGER REFERENCES teams(team_id),
    period             INTEGER,
    minutes_remaining  INTEGER,
    seconds_remaining  INTEGER,
    made               INTEGER NOT NULL,     -- 1 / 0
    action_type        TEXT,                 -- 'Driving Layup Shot', 'Pullup Jump shot', ...
    shot_type          TEXT,                 -- '2PT Field Goal' / '3PT Field Goal'
    zone_basic         TEXT,                 -- 'Restricted Area', 'Mid-Range', 'Left Corner 3', ...
    zone_area          TEXT,
    zone_range         TEXT,
    distance           INTEGER,              -- feet
    loc_x              INTEGER,
    loc_y              INTEGER,
    is_home            INTEGER,              -- 1 / 0 from games, set by pipeline.fill_shot_home (saves a join per shot)
    PRIMARY KEY (game_id, game_event_id)
);
CREATE INDEX IF NOT EXISTS idx_shots_player_season ON shots(player_id, season);
CREATE INDEX IF NOT EXISTS idx_shots_season ON shots(season);

-- Per-game season averages plus advanced and tracking numbers, one row per player-season.
CREATE TABLE IF NOT EXISTS player_season_stats (
    player_id   INTEGER NOT NULL REFERENCES players(player_id),
    season      TEXT NOT NULL,
    team_abbr   TEXT,
    age         REAL,
    gp          INTEGER,
    min         REAL,      -- minutes per game
    pts REAL, reb REAL, ast REAL, stl REAL, blk REAL, tov REAL,
    oreb REAL, dreb REAL, pf REAL, plus_minus REAL,
    fgm REAL, fga REAL, fg_pct REAL,
    fg3m REAL, fg3a REAL, fg3_pct REAL,
    ftm REAL, fta REAL, ft_pct REAL,
    -- Advanced
    ts_pct REAL, efg_pct REAL, usg_pct REAL, ast_pct REAL, ast_to REAL,
    reb_pct REAL, oreb_pct REAL, dreb_pct REAL,
    off_rating REAL, def_rating REAL, net_rating REAL, pie REAL,
    tov_pct REAL,      -- 100 * TOV / (FGA + 0.44*FTA + TOV), from season totals
    -- Tracking (Second Spectrum aggregates published on NBA.com)
    dist_miles REAL, avg_speed REAL,
    updated_at  TEXT,
    PRIMARY KEY (player_id, season)
);
-- Season indexes on the per-season tables keyed by player first: the weekly
-- Turso push replaces one season ("DELETE ... WHERE season = ?"), and without
-- them that delete scans every season's rows (Turso bills each scanned row).
CREATE INDEX IF NOT EXISTS idx_season_stats_season ON player_season_stats(season);

-- Derived from the shots table by hoopscholar.metrics.refresh_shot_profiles().
CREATE TABLE IF NOT EXISTS player_shot_profile (
    player_id     INTEGER NOT NULL,
    season        TEXT NOT NULL,
    fga           INTEGER,
    avg_distance  REAL,
    rim_fga       INTEGER, rim_fg_pct REAL,   -- Restricted Area
    paint_fga     INTEGER, paint_fg_pct REAL, -- In The Paint (Non-RA)
    mid_fga       INTEGER, mid_fg_pct REAL,
    corner3_fga   INTEGER, corner3_pct REAL,
    atb3_fga      INTEGER, atb3_pct REAL,     -- above the break
    rim_rate      REAL,                       -- share of FGA at the rim
    three_rate    REAL,                       -- share of FGA from three
    PRIMARY KEY (player_id, season)
);
CREATE INDEX IF NOT EXISTS idx_shot_profile_season ON player_shot_profile(season);

-- One box score per player per game, from LeagueGameLog (player mode), for the
-- game-log tab and rolling trends. Date, opponent and home/away come from
-- joining games, so they aren't repeated here. WITHOUT ROWID keeps the table in
-- its primary-key b-tree (no separate index), which saves space.
CREATE TABLE IF NOT EXISTS player_game_logs (
    player_id   INTEGER NOT NULL,
    game_id     TEXT NOT NULL,
    season      TEXT NOT NULL,
    team_id     INTEGER,
    min REAL, pts INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    plus_minus INTEGER,
    PRIMARY KEY (player_id, game_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_game_logs_player_season ON player_game_logs(player_id, season);

-- Top 5 most similar qualified players per qualified player-season, by cosine
-- similarity of centered slider percentiles. Built by metrics.refresh_similar().
CREATE TABLE IF NOT EXISTS player_similar (
    player_id   INTEGER NOT NULL,
    season      TEXT NOT NULL,
    rank        INTEGER NOT NULL,     -- 1 = most similar
    similar_id  INTEGER NOT NULL,
    score       REAL,                 -- cosine similarity, -1..1
    PRIMARY KEY (player_id, season, rank)
);
CREATE INDEX IF NOT EXISTS idx_similar_season ON player_similar(season);

-- ---------------------------------------------------------------------------
-- Precomputed for the website, rebuilt per season by pipeline.refresh_season()
-- (the weekly job does only the current season). The API reads these instead
-- of scanning shots or rebuilding percentiles, so a page is a few dozen rows.

-- One row per player-season: per-game stats, shot profile, qualification and
-- percentiles (p_<metric>, ranked within that season's qualified pool; see
-- metrics.percentiles). Columns must match metrics.SUMMARY_COLS.
CREATE TABLE IF NOT EXISTS player_season_summary (
    player_id   INTEGER NOT NULL,
    season      TEXT NOT NULL,
    full_name   TEXT, position TEXT, team_abbr TEXT, age REAL,
    gp INTEGER, min REAL,
    pts REAL, reb REAL, ast REAL, stl REAL, blk REAL, tov REAL, fga REAL, fta REAL,
    fg_pct REAL, fg3_pct REAL, ft_pct REAL,
    ts_pct REAL, efg_pct REAL, usg_pct REAL, ast_pct REAL, reb_pct REAL, tov_pct REAL,
    def_rating REAL, net_rating REAL, pie REAL, dist_miles REAL, avg_speed REAL,
    avg_distance REAL, rim_fga INTEGER, rim_fg_pct REAL, mid_fga INTEGER, mid_fg_pct REAL,
    corner3_fga INTEGER, corner3_pct REAL, rim_rate REAL, three_rate REAL,
    tov_opps REAL,             -- season FGA + 0.44*FTA + TOV, for the TOV% minimum
    tsa REAL,                  -- season FGA + 0.44*FTA, for the TS% minimum
    qualified INTEGER NOT NULL DEFAULT 0,
    games_eligible INTEGER NOT NULL DEFAULT 0,   -- in the games pool (games.eligible)
    p_pts REAL, p_ts_pct REAL, p_usg_pct REAL, p_rim_fg_pct REAL, p_mid_fg_pct REAL,
    p_fg3_pct REAL, p_corner3_pct REAL, p_ft_pct REAL, p_ast REAL, p_ast_pct REAL,
    p_tov_pct REAL, p_reb_pct REAL, p_stl REAL, p_blk REAL, p_def_rating REAL,
    p_net_rating REAL, p_pie REAL, p_dist_miles REAL, p_avg_speed REAL,
    PRIMARY KEY (player_id, season)
);
-- Leaderboards and the games pool read one season; team pages one team-season.
CREATE INDEX IF NOT EXISTS idx_summary_season_team ON player_season_summary(season, team_abbr);
CREATE INDEX IF NOT EXISTS idx_summary_team_season ON player_season_summary(team_abbr, season);

-- Season-wide numbers the pages show next to a player-season.
CREATE TABLE IF NOT EXISTS season_info (
    season        TEXT PRIMARY KEY,
    players       INTEGER,     -- player-seasons with stats
    pool_size     INTEGER,     -- qualified players
    max_gp        INTEGER,     -- league-leading games played
    pool_min_fga  INTEGER,     -- metrics.pool_min_fga
    min_tov_opps  INTEGER,     -- metrics.min_tov_opps
    min_tsa       INTEGER,     -- metrics.min_tsa
    updated_at    TEXT
);

-- League FG% by zone per season (fg_pct NULL where metrics.NO_LEAGUE_AVG_ZONES).
CREATE TABLE IF NOT EXISTS league_zone_averages (
    season      TEXT NOT NULL,
    zone_basic  TEXT NOT NULL,
    fga INTEGER, fgm INTEGER, fg_pct REAL,
    PRIMARY KEY (season, zone_basic)
) WITHOUT ROWID;

-- Each player's attempts and makes by zone per season.
CREATE TABLE IF NOT EXISTS player_zone_splits (
    player_id   INTEGER NOT NULL,
    season      TEXT NOT NULL,
    zone_basic  TEXT NOT NULL,
    fga INTEGER, fgm INTEGER,
    PRIMARY KEY (player_id, season, zone_basic)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_zone_splits_season ON player_zone_splits(season);

-- League attempts and makes per 2 ft hexagon per season (metrics.hexbin), for hot zones.
CREATE TABLE IF NOT EXISTS league_hex_bins (
    season  TEXT NOT NULL,
    q INTEGER NOT NULL, r INTEGER NOT NULL,
    fga INTEGER, fgm INTEGER,
    PRIMARY KEY (season, q, r)
) WITHOUT ROWID;

-- Guess the Player's frozen weekly pools (games.daily_pool), one row per week.
CREATE TABLE IF NOT EXISTS games_daily_pool (
    week    TEXT PRIMARY KEY,      -- ISO Monday
    season  TEXT NOT NULL,
    ids     TEXT NOT NULL          -- JSON array of player ids
);

-- Steps scripts/backfill.py has finished, so a stopped run resumes where it left off.
CREATE TABLE IF NOT EXISTS backfill_progress (
    season   TEXT NOT NULL,
    step     TEXT NOT NULL,     -- 'reference', 'games', 'player_games', 'shots:<team_id>', 'season_stats', 'finish'
    done_at  TEXT NOT NULL,
    detail   TEXT,
    PRIMARY KEY (season, step)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at  TEXT NOT NULL,
    kind    TEXT NOT NULL,      -- 'backfill' | 'nightly' | 'demo'
    season  TEXT,
    detail  TEXT
);
