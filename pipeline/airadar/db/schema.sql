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
    etag_readme     TEXT,

    -- The opening of the README, cleaned and truncated. Not decoration: it is
    -- classification evidence, and often the only evidence there is. Measured
    -- on the forty highest-star repositories created since July, fourteen
    -- scored zero on name, description and topics alone — among them
    -- `andrewyng/openworker` with no description at all and
    -- `browser-use/jev-ultrafast`, whose README's second line reads "A browser
    -- agent with a dynamic, indexed action space".
    readme_excerpt  TEXT,
    readme_hash     TEXT,                          -- changes when the text does
    readme_fetched_at TIMESTAMP,

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
    -- NULL means the engine could not settle it. A third state, not a synonym
    -- for 0: a repository nobody could decide about has to stay
    -- distinguishable from one decided against, or it leaves the index in
    -- silence. 1,842 repos above a thousand stars — `karpathy/nanoGPT` among
    -- them — had no row here at all for exactly this reason.
    is_ai         BOOLEAN,
    category      TEXT,
    subcategory   TEXT,
    confidence    REAL NOT NULL,
    method        TEXT NOT NULL,        -- 'rules' | 'llm'
    one_liner     TEXT,
    content_hash  TEXT NOT NULL,        -- re-classify only when inputs change
    classified_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS repo_classification_cat_idx
    ON repo_classification (category) WHERE is_ai = 1;

CREATE TABLE IF NOT EXISTS repo_scores (
    repo_id             INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date                DATE NOT NULL,
    stars_total         INTEGER NOT NULL,
    velocity_7d         REAL NOT NULL DEFAULT 0,
    velocity_14d        REAL NOT NULL DEFAULT 0,
    velocity_28d        REAL NOT NULL DEFAULT 0,
    velocity_90d        REAL NOT NULL DEFAULT 0,
    acceleration        REAL NOT NULL DEFAULT 0,
    -- measured | too_young | no_baseline. Two of acceleration's values are
    -- produced by the metric code rather than observed, and the Breakout board
    -- is exactly where that difference matters.
    acceleration_basis  TEXT NOT NULL DEFAULT 'measured',
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

-- How many repositories each board was allowed to rank on a given day. The
-- boards themselves are truncated to a limit, so their length says nothing
-- about the size of the question they answer — and the Fresh Power tile was
-- reading a backfill count instead, which is a different number again.
CREATE TABLE IF NOT EXISTS board_pool (
    date     DATE NOT NULL,
    board    TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '_all',
    eligible INTEGER NOT NULL,
    PRIMARY KEY (date, board, category)
);

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

-- ---------------------------------------------------------------------------
-- The company universe.
--
-- A second universe alongside the repositories, keyed on the registrable
-- domain. The domain is the primary key on purpose: a company is called
-- something different in every source — "OpenAI" on Wikidata, "OpenAI, Inc."
-- in a SEC filing, `openai` on GitHub, `openai.com` everywhere that matters —
-- and the domain is the only one of those that is unambiguous and that every
-- source can be joined on.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS companies (
    domain        TEXT PRIMARY KEY,          -- registrable domain, eTLD+1
    name          TEXT,                      -- best-known name, once a source gives one
    -- Which source supplied it. Needed because they are not equally good and
    -- the first writer used to win: Similarweb's `title` is the scraped HTML
    -- page title, so `openclaw.ai` was named "The ClawCast Episode 1",
    -- `claude.com` "Kundensupport | Claude" and `opencode.ai` "References" —
    -- and those names then blocked Crunchbase's real ones.
    name_source   TEXT,
    first_seen_at TIMESTAMP NOT NULL,
    seed_source   TEXT NOT NULL,             -- where it first arrived: corpus, wikidata, ...
    -- What the repository corpus knows, carried over so the boards can rank on
    -- it before any paid source has run.
    repo_stars    INTEGER NOT NULL DEFAULT 0,
    repo_count    INTEGER NOT NULL DEFAULT 0,
    top_repo      TEXT,
    gh_owners     TEXT                       -- comma-separated GitHub owners
);

CREATE INDEX IF NOT EXISTS companies_stars_idx ON companies(repo_stars DESC);

-- Which bellwether packages a repository depends on.
--
-- Keyed on `full_name` rather than `repo_id` on purpose: dependency evidence
-- arrives during discovery, before the repo has been resolved and given an id,
-- and the package that produced the sighting was previously dropped at exactly
-- that boundary — every one of the 29 bellwethers collapsed into the single
-- string 'ecosystems' in `pending_repos.source`.
--
-- This is the evidence a README cannot give. A repository that imports `torch`
-- is a machine-learning project whatever its description says, in whatever
-- language it says it.
CREATE TABLE IF NOT EXISTS repo_packages (
    full_name  TEXT NOT NULL,
    ecosystem  TEXT NOT NULL,
    package    TEXT NOT NULL,
    seen_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (full_name, ecosystem, package)
);

CREATE INDEX IF NOT EXISTS repo_packages_name_idx ON repo_packages(full_name);

-- Crunchbase's own company list, as a dictionary.
--
-- The point is the `domain` column. Everything else in this universe had to
-- guess: we hold `langchain.com` and Crunchbase wants `langchain`, and the
-- guess was right 19% of the time — 140 of 498 lookups came back as a real
-- company that was not ours. This table is bought rather than guessed, from
-- the actor's instant database, which serves the same clean company row and
-- therefore carries `website`.
--
-- It also rescues the funding rounds. A round row is flat: it carries
-- `companyPermalink` and no website at all, which is why all 400 of the first
-- run's rounds had a NULL domain and none could be tied to a company we track.
-- With this dictionary the permalink resolves.
CREATE TABLE IF NOT EXISTS crunchbase_directory (
    permalink   TEXT PRIMARY KEY,
    name        TEXT,
    website     TEXT,
    domain      TEXT,                    -- registrable domain of `website`
    categories  TEXT,
    country     TEXT,
    fetched_at  TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS crunchbase_directory_domain_idx
    ON crunchbase_directory(domain);

-- One row per company per month of Similarweb data. Monthly, not daily: the
-- source is a monthly estimate, and storing it per day would invent precision
-- that is not there.
CREATE TABLE IF NOT EXISTS company_traffic (
    domain         TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    month          DATE NOT NULL,            -- first day of the month it describes
    visits         INTEGER,
    global_rank    INTEGER,
    category       TEXT,
    category_rank  INTEGER,
    bounce_rate    REAL,
    traffic_genai  REAL,                     -- share of visits arriving from AI assistants
    collected_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (domain, month)
);

-- Every paid call, with what it cost. A budget that is not written down after
-- the fact is not a budget: this table is what `companies spend` reads, and
-- what stops a run that would take the month over its cap.
CREATE TABLE IF NOT EXISTS apify_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    run_id      TEXT,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status      TEXT,
    items       INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS apify_run_started_idx ON apify_run(started_at);

-- ---------------------------------------------------------------------------
-- Money: rounds, what a company has raised, who bought whom.
--
-- Kept in tables of its own rather than as columns on `companies`, because a
-- company can exist here with no funding record at all and that is not a gap
-- to be filled — most open-source AI projects are not venture-funded, and a
-- NULL that means "never raised" must not look like a NULL that means "not
-- looked up yet".
-- ---------------------------------------------------------------------------

-- One row per announced funding round. `round_key` is the source's own id, so
-- re-running a search replaces rather than duplicates.
CREATE TABLE IF NOT EXISTS funding_round (
    round_key      TEXT PRIMARY KEY,
    company_name   TEXT NOT NULL,
    company_domain TEXT,                     -- resolved where possible; NULL is honest
    cb_permalink   TEXT,
    round_type     TEXT,                     -- seed, series_a, grant, ...
    amount_usd     INTEGER,                  -- NULL when undisclosed, never guessed
    announced_on   DATE,
    investors      TEXT,
    source         TEXT NOT NULL,            -- crunchbase | sec-form-d
    source_url     TEXT,
    collected_at   TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS funding_round_date_idx ON funding_round(announced_on DESC);
CREATE INDEX IF NOT EXISTS funding_round_domain_idx ON funding_round(company_domain);

-- What a company has raised in total, as its profile states it. One row per
-- company, replaced on each refresh.
CREATE TABLE IF NOT EXISTS company_funding (
    domain         TEXT PRIMARY KEY REFERENCES companies(domain) ON DELETE CASCADE,
    cb_permalink   TEXT,
    total_usd      INTEGER,
    rounds         INTEGER,
    investors      INTEGER,
    last_round     TEXT,
    last_round_on  DATE,
    employee_range TEXT,
    country        TEXT,
    ipo_status     TEXT,
    -- Valuation is press-reported, not a field any of these sources exposes.
    -- It is stored with the article that said it so the site can show the
    -- source, and left NULL rather than inferred from the money raised.
    valuation_usd  INTEGER,
    valuation_src  TEXT,
    valuation_on   DATE,
    collected_at   TIMESTAMP NOT NULL
);

-- Acquisitions, in both directions: a company that was bought and companies it
-- bought. `acquirer` and `target` are names because the other side is often
-- not in our universe at all.
CREATE TABLE IF NOT EXISTS acquisition (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    acquirer      TEXT NOT NULL,
    target        TEXT NOT NULL,
    domain        TEXT,                      -- whichever side is ours
    announced_on  DATE,
    amount_usd    INTEGER,
    source        TEXT NOT NULL,
    collected_at  TIMESTAMP NOT NULL
);

-- Not a UNIQUE constraint on the columns, because SQLite treats two NULLs as
-- distinct: an acquisition with no announced date inserted a fresh row on
-- every single run, and an acquisition with no announced date is the common
-- case. COALESCE gives the three of them one identity.
CREATE UNIQUE INDEX IF NOT EXISTS acquisition_identity_idx
    ON acquisition (acquirer, target, COALESCE(announced_on, ''));

-- How a company in our universe maps onto Crunchbase. Separate from
-- `companies` so that a failed match is a recorded state rather than a missing
-- row: we asked, and the answer was no.
CREATE TABLE IF NOT EXISTS company_crunchbase (
    domain       TEXT PRIMARY KEY REFERENCES companies(domain) ON DELETE CASCADE,
    permalink    TEXT,
    name         TEXT,
    website      TEXT,
    -- matched: the profile's own website is this domain.
    -- mismatched: a profile came back pointing somewhere else — kept, because
    --   knowing the guess was wrong is what stops it being guessed again.
    -- missing: Crunchbase has nothing under that name.
    match_state  TEXT NOT NULL,
    asked_as     TEXT,
    checked_at   TIMESTAMP NOT NULL
);

-- G2 ratings. One row per company per snapshot; G2 covers a minority of this
-- universe by design (it indexes software buyers review, not model weights),
-- so absence here is the normal case and not a coverage failure.
CREATE TABLE IF NOT EXISTS company_g2 (
    domain        TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    product_slug  TEXT NOT NULL,
    collected_on  DATE NOT NULL,
    reviews       INTEGER,
    avg_rating    REAL,
    rating_1      INTEGER NOT NULL DEFAULT 0,
    rating_2      INTEGER NOT NULL DEFAULT 0,
    rating_3      INTEGER NOT NULL DEFAULT 0,
    rating_4      INTEGER NOT NULL DEFAULT 0,
    rating_5      INTEGER NOT NULL DEFAULT 0,
    collected_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (domain, product_slug, collected_on)
);
