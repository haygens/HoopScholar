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
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_players_name ON players(full_name);

CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,          -- NBA ids are zero-padded strings, e.g. '0022500001'
    season        TEXT NOT NULL,             -- '2025-26'
    season_type   TEXT NOT NULL DEFAULT 'Regular Season',
    game_date     TEXT NOT NULL,             -- ISO 'YYYY-MM-DD'
    home_team_id  INTEGER REFERENCES teams(team_id),
    away_team_id  INTEGER REFERENCES teams(team_id),
    home_pts      INTEGER,
    away_pts      INTEGER
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
