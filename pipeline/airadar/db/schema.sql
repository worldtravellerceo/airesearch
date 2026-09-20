-- AI Radar schema (PostgreSQL / Neon).
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS repos (
    id              BIGINT PRIMARY KEY,           -- GitHub numeric repo id (stable across renames)
    full_name       TEXT NOT NULL UNIQUE,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ,
    description     TEXT,
    homepage        TEXT,
    language        TEXT,
    license         TEXT,
    archived        BOOLEAN NOT NULL DEFAULT FALSE,
    is_fork         BOOLEAN NOT NULL DEFAULT FALSE,
    stars           INTEGER NOT NULL DEFAULT 0,   -- denormalised latest value, for cheap ordering
    discovered_via  TEXT,                         -- topic-bucket | keyword | snowball | awesome | ecosystems | hf
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    history_backfilled_through DATE,              -- oldest week already pulled from stargazers/history
    etag_repo       TEXT,                         -- conditional-request cache: 304s are free
    etag_history    TEXT
);

CREATE INDEX IF NOT EXISTS repos_stars_idx ON repos (stars DESC);
CREATE INDEX IF NOT EXISTS repos_last_checked_idx ON repos (last_checked_at NULLS FIRST);

CREATE TABLE IF NOT EXISTS repo_topics (
    repo_id BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    topic   TEXT NOT NULL,
    PRIMARY KEY (repo_id, topic)
);

CREATE INDEX IF NOT EXISTS repo_topics_topic_idx ON repo_topics (topic);

-- Point-in-time totals, written once per collection run.
CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id     BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    stars       INTEGER NOT NULL,
    forks       INTEGER,
    open_issues INTEGER,
    pushed_at   TIMESTAMPTZ,
    PRIMARY KEY (repo_id, date)
);

-- Per-day star deltas, sourced from GET /repos/{o}/{r}/stargazers/history.
-- This is the backbone of every trend metric.
CREATE TABLE IF NOT EXISTS repo_star_daily (
    repo_id      BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date         DATE NOT NULL,
    stars_gained INTEGER NOT NULL,
    PRIMARY KEY (repo_id, date)
);

CREATE INDEX IF NOT EXISTS repo_star_daily_date_idx ON repo_star_daily (date);

CREATE TABLE IF NOT EXISTS repo_classification (
    repo_id       BIGINT PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
    is_ai         BOOLEAN NOT NULL,
    category      TEXT,
    subcategory   TEXT,
    confidence    REAL NOT NULL,
    method        TEXT NOT NULL,        -- 'rules' | 'llm'
    one_liner     TEXT,
    content_hash  TEXT NOT NULL,        -- re-classify only when the inputs actually change
    classified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS repo_classification_cat_idx
    ON repo_classification (category) WHERE is_ai;

-- One row per repo per scoring run.
CREATE TABLE IF NOT EXISTS repo_scores (
    repo_id             BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
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
    breakout            BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_days       INTEGER NOT NULL DEFAULT 0,  -- how much daily history backs these numbers
    PRIMARY KEY (repo_id, date)
);

CREATE INDEX IF NOT EXISTS repo_scores_date_fresh_idx ON repo_scores (date, fresh_power DESC);
CREATE INDEX IF NOT EXISTS repo_scores_date_momentum_idx ON repo_scores (date, momentum_score DESC);

-- Materialised rankings. Keeping a row per day is what makes "up 40 places since
-- last week" answerable.
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    date     DATE NOT NULL,
    board    TEXT NOT NULL,          -- popular | momentum | breakout | fresh
    category TEXT NOT NULL DEFAULT '_all',
    rank     INTEGER NOT NULL,
    repo_id  BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    score    REAL NOT NULL,
    PRIMARY KEY (date, board, category, rank)
);

CREATE INDEX IF NOT EXISTS leaderboard_lookup_idx
    ON leaderboard_snapshots (board, category, date, rank);
CREATE INDEX IF NOT EXISTS leaderboard_repo_idx
    ON leaderboard_snapshots (repo_id, board, date);

-- Bookkeeping for cost/rate-limit reporting.
CREATE TABLE IF NOT EXISTS run_log (
    id           BIGSERIAL PRIMARY KEY,
    command      TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    ok           BOOLEAN,
    api_calls    INTEGER NOT NULL DEFAULT 0,
    api_304s     INTEGER NOT NULL DEFAULT 0,
    llm_in_tok   BIGINT NOT NULL DEFAULT 0,
    llm_out_tok  BIGINT NOT NULL DEFAULT 0,
    llm_cost_usd REAL NOT NULL DEFAULT 0,
    notes        TEXT
);

-- Repositories discovered by name only (curated lists, dependency graphs, the
-- Hugging Face Hub). They carry no numeric id yet, and `repos.id` is that id,
-- so they wait here until a resolve pass looks them up.
CREATE TABLE IF NOT EXISTS pending_repos (
    full_name   TEXT PRIMARY KEY,
    source      TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    failed      BOOLEAN NOT NULL DEFAULT FALSE,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS pending_repos_open_idx
    ON pending_repos (added_at) WHERE resolved_at IS NULL AND NOT failed;

-- Which topics discovery has already swept, so snowballed topics are queried
-- once rather than every run.
CREATE TABLE IF NOT EXISTS queried_topics (
    topic            TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'seed',   -- seed | snowball
    first_queried_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_queried_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    repos_found      INTEGER NOT NULL DEFAULT 0
);
