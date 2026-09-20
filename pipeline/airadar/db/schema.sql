-- AI Radar schema (SQLite).
-- Idempotent: safe to re-run.
--
-- Column types matter here beyond documentation: the connection is opened with
-- PARSE_DECLTYPES, so `DATE`, `TIMESTAMP` and `BOOLEAN` are converted back to
-- Python objects on the way out (see db/repo.py). Declaring a date column TEXT
-- would silently hand callers strings.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY,   -- GitHub numeric id (survives renames)
    full_name       TEXT NOT NULL UNIQUE,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    created_at      TIMESTAMP,
    description     TEXT,
    homepage        TEXT,
    language        TEXT,
    license         TEXT,
    archived        BOOLEAN NOT NULL DEFAULT 0,
    is_fork         BOOLEAN NOT NULL DEFAULT 0,
    stars           INTEGER NOT NULL DEFAULT 0,   -- latest value, for cheap ordering
    discovered_via  TEXT,
    first_seen_at   TIMESTAMP NOT NULL,
    last_checked_at TIMESTAMP,
    etag_repo       TEXT,                          -- 304s cost no quota
    etag_history    TEXT,

    -- Backfill bookkeeping. `history_backfilled_through` is the oldest day ever
    -- pulled; `first_star_date` is the oldest day currently retained. They
    -- diverge once pruning kicks in, and the first is what says whether the
    -- lifetime figures below are trustworthy.
    history_backfilled_through DATE,
    first_star_date DATE,

    -- Lifetime figures computed while the full history was in hand. They are
    -- persisted so that pruning the daily rows does not destroy them.
    fresh_power_tail       REAL NOT NULL DEFAULT 0,  -- decayed weight of pruned days
    fresh_power_tail_asof  DATE,                     -- the day that weight is stated for
    days_to_1k      INTEGER,
    days_to_10k     INTEGER,
    days_to_50k     INTEGER
);

CREATE INDEX IF NOT EXISTS repos_stars_idx ON repos (stars DESC);
CREATE INDEX IF NOT EXISTS repos_last_checked_idx ON repos (last_checked_at);

CREATE TABLE IF NOT EXISTS repo_topics (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    topic   TEXT NOT NULL,
    PRIMARY KEY (repo_id, topic)
);

CREATE INDEX IF NOT EXISTS repo_topics_topic_idx ON repo_topics (topic);

CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    stars       INTEGER NOT NULL,
    forks       INTEGER,
    open_issues INTEGER,
    pushed_at   TIMESTAMP,
    PRIMARY KEY (repo_id, date)
);

-- Per-day star deltas from GET /repos/{o}/{r}/stargazers/history — the backbone
-- of every trend metric. Only a rolling window is retained; anything older is
-- folded into repos.fresh_power_tail before it is dropped.
CREATE TABLE IF NOT EXISTS repo_star_daily (
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date         DATE NOT NULL,
    stars_gained INTEGER NOT NULL,
    PRIMARY KEY (repo_id, date)
);

CREATE INDEX IF NOT EXISTS repo_star_daily_date_idx ON repo_star_daily (date);

-- Weekly roll-up of days that have aged out of `repo_star_daily`. Without it a
-- detail page could only ever draw the retention window, and the lifetime star
-- curve — the thing that shows a five-year climb against a two-week spike — is
-- the most useful chart on the site. A week of resolution is plenty that far
-- back, and it costs a seventh of the rows.
CREATE TABLE IF NOT EXISTS repo_star_weekly (
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    week_start   DATE NOT NULL,
    stars_gained INTEGER NOT NULL,
    PRIMARY KEY (repo_id, week_start)
);

CREATE TABLE IF NOT EXISTS repo_classification (
    repo_id       INTEGER PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
    is_ai         BOOLEAN NOT NULL,
    category      TEXT,
    subcategory   TEXT,
    confidence    REAL NOT NULL,
    method        TEXT NOT NULL,        -- 'rules' | 'llm'
    one_liner     TEXT,
    content_hash  TEXT NOT NULL,        -- re-classify only when inputs change
    classified_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS repo_classification_cat_idx
    ON repo_classification (category) WHERE is_ai;

CREATE TABLE IF NOT EXISTS repo_scores (
    repo_id             INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date                DATE NOT NULL,
    stars_total         INTEGER NOT NULL,
    velocity_7d         REAL NOT NULL DEFAULT 0,
    velocity_14d        REAL NOT NULL DEFAULT 0,
    velocity_28d        REAL NOT NULL DEFAULT 0,
    velocity_90d        REAL NOT NULL DEFAULT 0,
    acceleration        REAL NOT NULL DEFAULT 0,
    relative_growth_14d REAL NOT NULL DEFAULT 0,
    fresh_power         REAL NOT NULL DEFAULT 0,
    momentum_score      REAL NOT NULL DEFAULT 0,
    peak_velocity       REAL,
    days_since_peak     INTEGER,
    days_to_1k          INTEGER,
    days_to_10k         INTEGER,
    days_to_50k         INTEGER,
    breakout            BOOLEAN NOT NULL DEFAULT 0,
    coverage_days       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, date)
);

CREATE INDEX IF NOT EXISTS repo_scores_date_fresh_idx ON repo_scores (date, fresh_power DESC);
CREATE INDEX IF NOT EXISTS repo_scores_date_momentum_idx ON repo_scores (date, momentum_score DESC);

-- A row per board position per day. Keeping the history is what makes "up 40
-- places since last week" answerable.
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    date     DATE NOT NULL,
    board    TEXT NOT NULL,          -- popular | momentum | breakout | fresh
    category TEXT NOT NULL DEFAULT '_all',
    rank     INTEGER NOT NULL,
    repo_id  INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    score    REAL NOT NULL,
    PRIMARY KEY (date, board, category, rank)
);

CREATE INDEX IF NOT EXISTS leaderboard_lookup_idx
    ON leaderboard_snapshots (board, category, date, rank);
CREATE INDEX IF NOT EXISTS leaderboard_repo_idx
    ON leaderboard_snapshots (repo_id, board, date);

-- Repos known only by owner/name (curated lists, dependency graphs, the Hub).
-- They carry no numeric id yet, and repos.id is that id, so they wait here
-- until a resolve pass looks them up.
CREATE TABLE IF NOT EXISTS pending_repos (
    full_name   TEXT PRIMARY KEY,
    source      TEXT,
    added_at    TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    failed      BOOLEAN NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS pending_repos_open_idx ON pending_repos (added_at);

CREATE TABLE IF NOT EXISTS queried_topics (
    topic            TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'seed',   -- seed | snowball
    first_queried_at TIMESTAMP NOT NULL,
    last_queried_at  TIMESTAMP NOT NULL,
    repos_found      INTEGER NOT NULL DEFAULT 0
);

-- Cost and quota bookkeeping, so the estimates in the README can be checked
-- against what actually happened.
CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT NOT NULL,
    started_at   TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    ok           BOOLEAN,
    api_calls    INTEGER NOT NULL DEFAULT 0,
    api_304s     INTEGER NOT NULL DEFAULT 0,
    llm_in_tok   INTEGER NOT NULL DEFAULT 0,
    llm_out_tok  INTEGER NOT NULL DEFAULT 0,
    llm_cost_usd REAL NOT NULL DEFAULT 0,
    notes        TEXT
);
