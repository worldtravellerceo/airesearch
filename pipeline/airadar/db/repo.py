"""Data access layer (SQLite).

The database is a single file that lives in the repository, which is what
removes the need for a hosted database and an account to go with it. Everything
here is idempotent: collection runs get interrupted by rate limits, Actions
timeouts and transient failures, and re-running must converge rather than
duplicate or double-count.

Types round-trip through `PARSE_DECLTYPES` plus the converters registered
below, so callers hand in and get back `date`, `datetime` and `bool` rather
than the strings and integers SQLite stores.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import Entry
from airadar.scoring.metrics import RepoMetrics

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# How many days of per-day detail to keep. 90 is the longest window any metric
# uses and the sparkline shows 90, so 120 leaves headroom for a few missed runs.
# Everything older is folded into repos.fresh_power_tail before being dropped.
RETAIN_DAYS = 120


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- type plumbing ---------------------------------------------------------


def _adapt_date(value: dt.date) -> str:
    return value.isoformat()


def _adapt_datetime(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _convert_date(raw: bytes) -> dt.date:
    return dt.date.fromisoformat(raw.decode())


def _convert_timestamp(raw: bytes) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.decode())


def _convert_bool(raw: bytes) -> bool:
    return raw not in (b"0", b"", b"false", b"FALSE")


sqlite3.register_adapter(dt.date, _adapt_date)
sqlite3.register_adapter(dt.datetime, _adapt_datetime)
sqlite3.register_converter("DATE", _convert_date)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)
sqlite3.register_converter("BOOLEAN", _convert_bool)


def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


@dataclass
class RepoRecord:
    id: int
    full_name: str
    owner: str
    name: str
    created_at: dt.datetime | None = None
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    license: str | None = None
    archived: bool = False
    is_fork: bool = False
    stars: int = 0
    discovered_via: str | None = None
    topics: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, payload: dict, *, discovered_via: str | None = None) -> RepoRecord:
        licence = payload.get("license") or {}
        return cls(
            id=payload["id"],
            full_name=payload["full_name"],
            owner=payload["owner"]["login"],
            name=payload["name"],
            created_at=parse_ts(payload.get("created_at")),
            description=payload.get("description"),
            homepage=payload.get("homepage") or None,
            language=payload.get("language"),
            license=licence.get("spdx_id") if isinstance(licence, dict) else None,
            archived=bool(payload.get("archived")),
            is_fork=bool(payload.get("fork")),
            stars=int(payload.get("stargazers_count", 0)),
            discovered_via=discovered_via,
            topics=tuple(payload.get("topics") or ()),
        )

    def as_params(self) -> dict:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "owner": self.owner,
            "name": self.name,
            "created_at": self.created_at,
            "description": self.description,
            "homepage": self.homepage,
            "language": self.language,
            "license": self.license,
            "archived": int(self.archived),
            "is_fork": int(self.is_fork),
            "stars": self.stars,
            "discovered_via": self.discovered_via,
            "first_seen_at": _utcnow(),
        }


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the database, creating its directory if needed."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target), detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
    conn.row_factory = _dict_row
    conn.execute("PRAGMA foreign_keys = ON")
    # A collection run is thousands of small writes; WAL and a relaxed sync make
    # that an order of magnitude faster and the file is rebuildable anyway.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


# Columns added after the first database was published. The schema file is
# `CREATE TABLE IF NOT EXISTS` throughout, which is right for a fresh start and
# does nothing at all for the database that already exists — and the only copy
# of ours is a release asset that every run downloads. Without these, a new
# column is silently absent in production and present in every test.
_SCORES_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("acceleration_basis", "TEXT NOT NULL DEFAULT 'measured'"),
)

_REPOS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("etag_readme", "TEXT"),
    ("readme_excerpt", "TEXT"),
    ("readme_hash", "TEXT"),
    ("readme_fetched_at", "TIMESTAMP"),
)


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate_repos(conn)
    _migrate_scores(conn)
    _migrate_companies(conn)
    _migrate_classification_nullable(conn)
    conn.commit()


def _migrate_classification_nullable(conn: sqlite3.Connection) -> bool:
    """Let `repo_classification.is_ai` hold NULL for an unsettled repository.

    SQLite cannot drop a NOT NULL in place, so the table is rebuilt — the
    standard create-copy-drop-rename. It runs once: the second time round the
    column is already nullable and this returns immediately.

    Worth the rebuild because the alternative is worse. Without a third state,
    "we could not decide" has to be written as 0, which reads everywhere else
    as "decided against" and takes the repository off the boards, out of the
    counts and out of the review queue at the same time.
    """
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(repo_classification)")}
    if not info or not info.get("is_ai", {})["notnull"]:
        return False

    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE repo_classification_new (
            repo_id       INTEGER PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
            is_ai         BOOLEAN,
            category      TEXT,
            subcategory   TEXT,
            confidence    REAL NOT NULL,
            method        TEXT NOT NULL,
            one_liner     TEXT,
            content_hash  TEXT NOT NULL,
            classified_at TIMESTAMP NOT NULL
        );
        INSERT INTO repo_classification_new
            SELECT repo_id, is_ai, category, subcategory, confidence, method,
                   one_liner, content_hash, classified_at
              FROM repo_classification;
        DROP TABLE repo_classification;
        ALTER TABLE repo_classification_new RENAME TO repo_classification;
        CREATE INDEX IF NOT EXISTS repo_classification_cat_idx
            ON repo_classification (category) WHERE is_ai = 1;
        PRAGMA foreign_keys = ON;
        """
    )
    log.info("schema: repo_classification.is_ai can now hold NULL")
    return True


def _migrate_companies(conn: sqlite3.Connection) -> list[str]:
    """Add `name_source`, and drop the names that should never have been set.

    Similarweb returns the scraped HTML page title under `title`, which is not
    a company name and was stored as one for 874 companies — 366 of them over
    forty characters long. Those are cleared rather than kept: a name nobody
    can vouch for is worse than none, because `set_name` only ever wrote into
    an empty field, so a page title permanently blocked the real name.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    if not have:
        return []
    added = []
    if "name_source" not in have:
        conn.execute("ALTER TABLE companies ADD COLUMN name_source TEXT")
        conn.execute("UPDATE companies SET name = NULL WHERE name IS NOT NULL")
        added.append("name_source")
        log.info("schema: added name_source to companies and cleared unattributed names")
    return added


def _migrate_scores(conn: sqlite3.Connection) -> list[str]:
    """Add any missing `repo_scores` column.

    `schema.sql` is CREATE TABLE IF NOT EXISTS throughout, which does nothing to
    a table that already exists — and the published release asset is the only
    copy of this database there is."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(repo_scores)")}
    added = []
    for column, decl in _SCORES_MIGRATIONS:
        if column not in have:
            conn.execute(f"ALTER TABLE repo_scores ADD COLUMN {column} {decl}")
            added.append(column)
    if added:
        log.info("schema: added %s to repo_scores", ", ".join(added))
    return added


def _migrate_repos(conn: sqlite3.Connection) -> list[str]:
    """Add any missing `repos` column. Idempotent, like the schema itself."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(repos)")}
    added = []
    for column, decl in _REPOS_MIGRATIONS:
        if column not in have:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {column} {decl}")
            added.append(column)
    if added:
        log.info("schema: added %s to repos", ", ".join(added))
    return added


# --- repositories ----------------------------------------------------------


#: What a released name is suffixed with. GitHub allows letters, digits, `-`,
#: `_` and `.` in a repository name and letters, digits and `-` in an owner, so
#: an `@` cannot occur in a real `full_name` and this marker cannot collide
#: with one.
STALE_NAME_MARKER = "@"
#: Repositories whose name we can still vouch for. A released name is a
#: placeholder until a later pass learns what the id is really called, and an
#: index must not publish a name it knows is wrong.
CURRENT_NAME_SQL = "r.full_name NOT LIKE '%@%'"

#: How many names to ask about in one statement. SQLite's host-parameter limit
#: is 999 on the builds this has to run on, and a census page is 100 rows.
_NAME_CHUNK = 400


def release_contested_names(conn: sqlite3.Connection, repos: Sequence[RepoRecord]) -> int:
    """Take a name off whichever row is still holding it under a different id.

    `repos` has two unique keys — `id`, and `full_name` — and the upsert below
    can only name one of them in its ON CONFLICT clause. So a repository
    arriving with an id we have never seen, under a name some other row still
    carries, raises `IntegrityError` instead of updating anything. That is not
    an exotic case: a repository is renamed and somebody takes its old name, an
    account changes hands, a project is deleted and recreated.

    It cost the scheduled run of 2026-09-22, which died in the census with
    `UNIQUE constraint failed: repos.full_name` and skipped the eight steps
    after it, so the site was not rebuilt that day. One row in a batch of a
    hundred takes the whole `executemany` with it.

    The stale row keeps its id and everything hanging off it — scores, star
    history, topics and its classification are all `ON DELETE CASCADE`, and
    throwing away the history of a project that was merely renamed is the worst
    available answer. It gives up the name and its `last_checked_at`, which
    puts it at the front of the next collect pass: `repos_due_for_refresh`
    sorts nulls first. That pass asks GitHub what the id is called now and
    writes the real name back, or gets a 404 and removes the row.

    That repair only reaches the tracked universe, which `track_limit` caps at
    the top 12,000 by stars. Measured the first time this fired in production:
    `cortex-docs/cortex` has 127 stars, ranks nowhere near it, and its released
    name went straight onto the site. Below the cap a name is released until
    some later discovery pass happens to see the id again — so the published
    index filters on `CURRENT_NAME_SQL` rather than trusting the repair.
    """
    wanted = {record.full_name: record.id for record in repos}
    names = list(wanted)
    stale: list[tuple[int]] = []
    for start in range(0, len(names), _NAME_CHUNK):
        chunk = names[start : start + _NAME_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        stale.extend(
            (row["id"],)
            for row in conn.execute(
                f"SELECT id, full_name FROM repos WHERE full_name IN ({placeholders})",
                chunk,
            )
            if row["id"] != wanted[row["full_name"]]
        )
    if not stale:
        return 0
    conn.executemany(
        "UPDATE repos SET full_name = full_name || '@' || id, last_checked_at = NULL WHERE id = ?",
        stale,
    )
    log.info("repos: %d name(s) released by a stale row, pending re-check", len(stale))
    return len(stale)


def upsert_repos(conn: sqlite3.Connection, repos: Sequence[RepoRecord]) -> int:
    """Insert or refresh repo rows. `discovered_via` is kept from first sighting."""
    if not repos:
        return 0

    # A batch may not contain two rows claiming one name either, and the last
    # sighting is the one to believe.
    by_name: dict[str, RepoRecord] = {}
    for record in repos:
        by_name[record.full_name] = record
    repos = list(by_name.values())

    release_contested_names(conn, repos)

    conn.executemany(
        """
        INSERT INTO repos (id, full_name, owner, name, created_at, description,
                           homepage, language, license, archived, is_fork, stars,
                           discovered_via, first_seen_at)
        VALUES (:id, :full_name, :owner, :name, :created_at, :description,
                :homepage, :language, :license, :archived, :is_fork, :stars,
                :discovered_via, :first_seen_at)
        ON CONFLICT (id) DO UPDATE SET
            full_name   = excluded.full_name,
            owner       = excluded.owner,
            name        = excluded.name,
            created_at  = COALESCE(excluded.created_at, repos.created_at),
            description = excluded.description,
            homepage    = excluded.homepage,
            language    = excluded.language,
            license     = excluded.license,
            archived    = excluded.archived,
            is_fork     = excluded.is_fork,
            stars       = excluded.stars
        """,
        [record.as_params() for record in repos],
    )
    topic_rows = [(r.id, t) for r in repos for t in r.topics]
    if topic_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO repo_topics (repo_id, topic) VALUES (?, ?)",
            topic_rows,
        )
    conn.commit()
    return len(repos)


def set_etag(conn: sqlite3.Connection, repo_id: int, *, column: str, etag: str | None) -> None:
    if column not in {"etag_repo", "etag_history", "etag_readme"}:
        raise ValueError(f"refusing to write unknown column {column!r}")
    conn.execute(f"UPDATE repos SET {column} = ? WHERE id = ?", (etag, repo_id))


def mark_checked(conn: sqlite3.Connection, repo_id: int, when: dt.datetime) -> None:
    conn.execute("UPDATE repos SET last_checked_at = ? WHERE id = ?", (when, repo_id))


def set_readme(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    excerpt: str,
    etag: str | None,
    now: dt.datetime | None = None,
) -> None:
    """Store a README excerpt, or the fact that there is none.

    An empty excerpt is written deliberately: `readme_fetched_at` is what says
    the question has been asked, and without it a repository with no README
    would be requested again on every run, forever.
    """
    conn.execute(
        """
        UPDATE repos
           SET readme_excerpt = :excerpt,
               readme_hash = :hash,
               etag_readme = :etag,
               readme_fetched_at = :now
         WHERE id = :id
        """,
        {
            "id": repo_id,
            "excerpt": excerpt or None,
            "hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16] if excerpt else None,
            "etag": etag,
            "now": now or dt.datetime.now(dt.UTC),
        },
    )


def record_packages(
    conn: sqlite3.Connection,
    by_repo: dict[str, set[tuple[str, str]]],
    *,
    now: dt.datetime | None = None,
) -> int:
    """Store which bellwether packages each repository depends on."""
    rows = [
        (full_name, ecosystem, package, now or dt.datetime.now(dt.UTC))
        for full_name, pairs in by_repo.items()
        for ecosystem, package in pairs
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO repo_packages (full_name, ecosystem, package, seen_at) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def mark_readme_checked(
    conn: sqlite3.Connection, repo_id: int, now: dt.datetime | None = None
) -> None:
    """A 304: the text we hold is current, and it cost no quota to find out."""
    conn.execute(
        "UPDATE repos SET readme_fetched_at = ? WHERE id = ?",
        (now or dt.datetime.now(dt.UTC), repo_id),
    )


# The tracked universe, ranked by stars. One definition, used both by the
# collect queue and by the freshness audit that checks the collect queue did its
# job. Writing the ranking twice is how the two came to measure different
# populations: the queue was narrowed to AI repos and the audit was not, so it
# reported 24% against a universe that was in fact entirely fresh — the audit
# failing in exactly the way it exists to catch.
#
# Confirmed non-AI repos are excluded because they never reach a board, so
# collecting their metrics spends quota on rows nothing reads — and worse, it
# pushes the star rank of the ones that do matter past `track_limit`. The census
# put 56,105 repos above a thousand stars into the corpus and the tracked floor
# jumped from 1,674 stars to 9,392, cutting off exactly the fast-rising projects
# the Breakout board exists for. Repos not yet judged stay in: they are new, and
# might be AI.
TRACKED_UNIVERSE_SQL = """
    SELECT r.id, r.full_name, r.created_at, r.stars, r.etag_repo, r.etag_history,
           r.last_checked_at, r.history_backfilled_through,
           ROW_NUMBER() OVER (ORDER BY r.stars DESC, r.id) AS star_rank
    FROM repos r
    LEFT JOIN repo_classification c ON c.repo_id = r.id
    WHERE r.is_fork = 0 AND (c.is_ai IS NULL OR c.is_ai = 1)
"""


def repos_due_for_refresh(
    conn: sqlite3.Connection,
    *,
    tier1_size: int,
    now: dt.datetime,
    limit: int | None = None,
    track_limit: int | None = None,
) -> list[dict]:
    """Repos to collect this run, most-neglected first.

    Two caps, for two different reasons. `track_limit` bounds the universe we
    collect at all, which is what keeps the database small enough to live in the
    repository. `tier1_size` splits what remains into a daily and a weekly tier,
    which is what keeps one run inside an hour of PAT quota.
    """
    params: dict = {
        "tier1": tier1_size,
        "daily_cutoff": now - dt.timedelta(hours=20),
        "weekly_cutoff": now - dt.timedelta(days=7),
        "track_limit": track_limit if track_limit is not None else -1,
    }
    sql = f"""
        WITH ranked AS ({TRACKED_UNIVERSE_SQL})
        SELECT * FROM ranked
        WHERE (:track_limit < 0 OR star_rank <= :track_limit)
          AND (last_checked_at IS NULL
               OR (star_rank <= :tier1 AND last_checked_at < :daily_cutoff)
               OR (star_rank >  :tier1 AND last_checked_at < :weekly_cutoff))
        ORDER BY (last_checked_at IS NOT NULL), last_checked_at, star_rank
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return conn.execute(sql, params).fetchall()


# --- time series -----------------------------------------------------------


def record_snapshot(
    conn: sqlite3.Connection,
    repo_id: int,
    date: dt.date,
    *,
    stars: int,
    forks: int | None = None,
    open_issues: int | None = None,
    pushed_at: dt.datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO repo_snapshots (repo_id, date, stars, forks, open_issues, pushed_at)
        VALUES (:repo_id, :date, :stars, :forks, :open_issues, :pushed_at)
        ON CONFLICT (repo_id, date) DO UPDATE SET
            stars = excluded.stars, forks = excluded.forks,
            open_issues = excluded.open_issues, pushed_at = excluded.pushed_at
        """,
        {
            "repo_id": repo_id,
            "date": date,
            "stars": stars,
            "forks": forks,
            "open_issues": open_issues,
            "pushed_at": pushed_at,
        },
    )


def record_star_daily(conn: sqlite3.Connection, repo_id: int, days: Iterable[DailyStars]) -> int:
    """Upsert per-day star deltas.

    The API restates recent weeks as they fill in, so later values overwrite
    earlier ones rather than accumulating.
    """
    rows = [(repo_id, d.date, d.stars_gained) for d in days]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO repo_star_daily (repo_id, date, stars_gained)
        VALUES (?, ?, ?)
        ON CONFLICT (repo_id, date) DO UPDATE SET stars_gained = excluded.stars_gained
        """,
        rows,
    )
    conn.execute(
        """
        UPDATE repos SET first_star_date = (
            SELECT min(date) FROM repo_star_daily WHERE repo_id = ?
        ) WHERE id = ?
        """,
        (repo_id, repo_id),
    )
    return len(rows)


def set_backfill_watermark(conn: sqlite3.Connection, repo_id: int, oldest: dt.date) -> None:
    conn.execute("UPDATE repos SET history_backfilled_through = ? WHERE id = ?", (oldest, repo_id))


def load_daily_series(
    conn: sqlite3.Connection, repo_id: int, *, since: dt.date | None = None
) -> list[DailyStars]:
    sql = "SELECT date, stars_gained FROM repo_star_daily WHERE repo_id = :repo_id"
    params: dict = {"repo_id": repo_id}
    if since is not None:
        sql += " AND date >= :since"
        params["since"] = since
    sql += " ORDER BY date"
    return [DailyStars(r["date"], r["stars_gained"]) for r in conn.execute(sql, params)]


def set_lifetime_stats(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    tail: float,
    tail_asof: dt.date,
    days_to_1k: int | None,
    days_to_10k: int | None,
    days_to_50k: int | None,
) -> None:
    """Persist the figures that a later prune would otherwise destroy."""
    conn.execute(
        """
        UPDATE repos SET fresh_power_tail = :tail, fresh_power_tail_asof = :asof,
                         days_to_1k = :d1k, days_to_10k = :d10k, days_to_50k = :d50k
        WHERE id = :repo_id
        """,
        {
            "tail": tail,
            "asof": tail_asof,
            "d1k": days_to_1k,
            "d10k": days_to_10k,
            "d50k": days_to_50k,
            "repo_id": repo_id,
        },
    )


def set_milestones(
    conn: sqlite3.Connection,
    repo_id: int,
    milestones: tuple[int | None, int | None, int | None],
) -> None:
    """Persist days-to-1k/10k/50k without disturbing the carried tail."""
    conn.execute(
        "UPDATE repos SET days_to_1k = ?, days_to_10k = ?, days_to_50k = ? WHERE id = ?",
        (*milestones, repo_id),
    )


def prune_star_history(
    conn: sqlite3.Connection,
    *,
    today: dt.date,
    retain_days: int = RETAIN_DAYS,
    half_life_days: float,
) -> int:
    """Drop day rows older than the retention window, keeping what they mean.

    Two things happen before the delete, and both are done in SQL rather than by
    reading the rows into Python. The first prune after a full backfill spans
    every tracked repo's entire history — millions of rows — and adding those up
    in memory would exhaust the runner.

    1. Their decayed weight is folded into each repo's carried tail. Deleting
       them outright would silently shrink every long-lived project's
       `fresh_power`, which is the opposite of what the metric is for: it would
       make old projects look newer.
    2. They are rolled up into Sunday-aligned weekly buckets, so the lifetime
       star curve on a detail page still reaches back to the beginning.
    """
    import math

    cutoff = today - dt.timedelta(days=retain_days)
    decay = math.log(2) / half_life_days
    # Registered rather than relying on SQLite's math functions, which are only
    # present when the library was compiled with them.
    conn.create_function("airadar_decay", 1, lambda age: math.exp(-decay * (age or 0.0)))

    doomed = conn.execute(
        "SELECT count(*) AS n FROM repo_star_daily WHERE date < :cutoff",
        {"cutoff": cutoff},
    ).fetchone()["n"]
    if not doomed:
        return 0

    # The roll-up reads the same rows the tail calculation needs, and both have
    # to happen before the delete.
    conn.execute(
        """
        INSERT INTO repo_star_weekly (repo_id, week_start, stars_gained)
        SELECT repo_id,
               date(date, '-' || strftime('%w', date) || ' days') AS week_start,
               sum(stars_gained)
        FROM repo_star_daily
        WHERE date < :cutoff
        GROUP BY repo_id, week_start
        ON CONFLICT (repo_id, week_start)
        DO UPDATE SET stars_gained = stars_gained + excluded.stars_gained
        """,
        {"cutoff": cutoff},
    )

    # Everything is restated as of `today`, so a later run only has to decay the
    # single carried number forward.
    conn.execute(
        """
        UPDATE repos SET
            fresh_power_tail =
                COALESCE(fresh_power_tail, 0) * airadar_decay(
                    julianday(:today) - julianday(COALESCE(fresh_power_tail_asof, :today))
                )
                + COALESCE((
                    SELECT sum(d.stars_gained
                               * airadar_decay(julianday(:today) - julianday(d.date)))
                    FROM repo_star_daily d
                    WHERE d.repo_id = repos.id AND d.date < :cutoff
                ), 0),
            fresh_power_tail_asof = :today
        WHERE id IN (SELECT DISTINCT repo_id FROM repo_star_daily WHERE date < :cutoff)
        """,
        {"today": today, "cutoff": cutoff},
    )

    conn.execute("DELETE FROM repo_star_daily WHERE date < :cutoff", {"cutoff": cutoff})
    conn.commit()
    return doomed


def load_weekly_series(conn: sqlite3.Connection, repo_id: int) -> list[tuple[dt.date, int]]:
    return [
        (row["week_start"], row["stars_gained"])
        for row in conn.execute(
            "SELECT week_start, stars_gained FROM repo_star_weekly "
            "WHERE repo_id = ? ORDER BY week_start",
            (repo_id,),
        )
    ]


def prune_derived_tables(
    conn: sqlite3.Connection,
    *,
    today: dt.date,
    keep_days: int = 90,
    snapshot_keep_days: int | None = None,
) -> dict[str, int]:
    """Drop derived rows the site no longer shows.

    Scores and board snapshots accumulate a full set of rows every single day;
    left alone they would outgrow the star history they are derived from.

    This function had no callers for its whole life, so none of these three
    tables was ever pruned. That was survivable while `repo_snapshots` grew by
    the 2,956 repositories `collect` reaches in a day. The census now writes one
    row per repository per day for about 74,000 of them, which is 5 MB a day
    against a release asset that is downloaded in full by every workflow run —
    so the sweep has to actually run, and snapshots get their own, shorter
    horizon than the scores and board history beside them.
    """
    cutoff = today - dt.timedelta(days=keep_days)
    snapshot_cutoff = (
        today - dt.timedelta(days=snapshot_keep_days) if snapshot_keep_days is not None else cutoff
    )
    removed = {}
    for table in ("repo_scores", "leaderboard_snapshots", "explosion_board"):
        cursor = conn.execute(f"DELETE FROM {table} WHERE date < ?", (cutoff,))
        removed[table] = cursor.rowcount
    cursor = conn.execute("DELETE FROM repo_snapshots WHERE date < ?", (snapshot_cutoff,))
    removed["repo_snapshots"] = cursor.rowcount
    conn.commit()
    return removed


def _tail_at(row: dict, today: dt.date, decay: float) -> float:
    """A stored tail, decayed forward to `today`."""
    import math

    tail = row.get("fresh_power_tail") or 0.0
    asof = row.get("fresh_power_tail_asof")
    if not tail or asof is None:
        return 0.0
    age = (today - asof).days
    if age <= 0:
        return float(tail)
    return float(tail) * math.exp(-decay * age)


# --- classification --------------------------------------------------------


def save_classification(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    is_ai: bool | None,
    category: str | None,
    subcategory: str | None,
    confidence: float,
    method: str,
    content_hash: str,
    one_liner: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO repo_classification (repo_id, is_ai, category, subcategory,
                                         confidence, method, one_liner, content_hash,
                                         classified_at)
        VALUES (:repo_id, :is_ai, :category, :subcategory, :confidence, :method,
                :one_liner, :content_hash, :classified_at)
        ON CONFLICT (repo_id) DO UPDATE SET
            is_ai = excluded.is_ai, category = excluded.category,
            subcategory = excluded.subcategory, confidence = excluded.confidence,
            method = excluded.method, one_liner = excluded.one_liner,
            content_hash = excluded.content_hash, classified_at = excluded.classified_at
        """,
        {
            "repo_id": repo_id,
            # NULL, not 0: a repo the engine could not settle is not a repo it
            # decided against. The tracked universe keeps NULL rows, the boards
            # take only 1, and the review queue takes everything else.
            "is_ai": None if is_ai is None else int(is_ai),
            "category": category,
            "subcategory": subcategory,
            "confidence": confidence,
            "method": method,
            "one_liner": one_liner,
            "content_hash": content_hash,
            "classified_at": _utcnow(),
        },
    )


def cached_classification_hashes(conn: sqlite3.Connection) -> dict[int, str]:
    """repo_id -> content_hash, so unchanged repos are never re-classified."""
    return {
        row["repo_id"]: row["content_hash"]
        for row in conn.execute("SELECT repo_id, content_hash FROM repo_classification")
    }


# --- summaries -------------------------------------------------------------


def save_summary(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    description_tr: str,
    usage_tr: str,
    matched_project: str | None,
    relevance: int,
    investment_note: str | None,
    model: str,
    content_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO repo_summary (repo_id, description_tr, usage_tr, matched_project,
                                  relevance, investment_note, model, content_hash,
                                  created_at)
        VALUES (:repo_id, :description_tr, :usage_tr, :matched_project, :relevance,
                :investment_note, :model, :content_hash, :created_at)
        ON CONFLICT (repo_id) DO UPDATE SET
            description_tr = excluded.description_tr, usage_tr = excluded.usage_tr,
            matched_project = excluded.matched_project, relevance = excluded.relevance,
            investment_note = excluded.investment_note, model = excluded.model,
            content_hash = excluded.content_hash, created_at = excluded.created_at
        """,
        {
            "repo_id": repo_id,
            "description_tr": description_tr,
            "usage_tr": usage_tr,
            # NULL means the summariser found no genuine use, which is a real
            # answer and not a missing one.
            "matched_project": matched_project,
            "relevance": relevance,
            "investment_note": investment_note,
            "model": model,
            "content_hash": content_hash,
            "created_at": _utcnow(),
        },
    )


#: The morning digest's star floor and cap. They live here rather than in
#: `export_site` because `visible_repo_ids` has to select the same rows — a
#: repository on the digest is a repository on a page, and the two selections
#: drifting is how a page ends up listing repos nobody wrote a paragraph for.
DIGEST_ARRIVAL_MIN_STARS = 100
DIGEST_ARRIVAL_LIMIT = 30
#: Arrivals under this age are reported as new projects rather than as repos
#: that merely crossed the census threshold.
DIGEST_NEW_PROJECT_MAX_AGE_DAYS = 90


def digest_arrival_ids(conn: sqlite3.Connection, *, date) -> list[int]:
    """The repos the morning digest lists, biggest first."""
    rows = conn.execute(
        """
        SELECT r.id
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        WHERE date(r.first_seen_at) = :date
          AND r.is_fork = 0
          AND r.stars >= :floor
        ORDER BY r.stars DESC
        LIMIT :limit
        """,
        {"date": date, "floor": DIGEST_ARRIVAL_MIN_STARS, "limit": DIGEST_ARRIVAL_LIMIT},
    ).fetchall()
    return [row["id"] for row in rows]


def visible_repo_ids(conn: sqlite3.Connection, *, date, detail_limit: int) -> list[int]:
    """Every repo the site actually renders, biggest first.

    The same selection `export_site._details` uses — the top N by stars plus
    everyone on a board, whatever their rank — and it lives here so the two
    cannot drift.

    Today's arrivals are in here too, and they were the gap. The old reasoning
    was sound while it was true: a repo outside the boards appeared on no page,
    so writing paragraphs for it would be paying for text nobody could reach.
    Then `/bugun/` put the arrivals on a page and the premise stopped holding.
    Measured on the first digest: 1 of its 12 arrivals had a Turkish paragraph,
    because the other 11 sat between 100 and 1,031 stars and no board had
    reached them yet. So the page that exists to say "this is new" was the one
    page with nothing written about its rows.
    """
    rows = conn.execute(
        """
        SELECT r.id
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        WHERE """
        + CURRENT_NAME_SQL
        + """
          AND (r.id IN (
                  SELECT id FROM repos WHERE is_fork = 0 ORDER BY stars DESC LIMIT :limit
                )
               OR r.id IN (SELECT repo_id FROM leaderboard_snapshots WHERE date = :date)
               OR (date(r.first_seen_at) = :date
                   AND r.is_fork = 0
                   AND r.stars >= :arrival_floor))
        ORDER BY r.stars DESC
        """,
        {
            "date": date,
            "limit": detail_limit,
            "arrival_floor": DIGEST_ARRIVAL_MIN_STARS,
        },
    ).fetchall()
    return [row["id"] for row in rows]


def cached_summary_hashes(conn: sqlite3.Connection) -> dict[int, str]:
    """repo_id -> content_hash, so an unchanged repo is never paid for twice."""
    return {
        row["repo_id"]: row["content_hash"]
        for row in conn.execute("SELECT repo_id, content_hash FROM repo_summary")
    }


def start_llm_run(
    conn: sqlite3.Connection,
    *,
    command: str,
    model: str,
    repos: int,
    estimate_usd: float,
) -> int:
    """Record a paid run before it is submitted, so a crash still leaves a trace."""
    cursor = conn.execute(
        """
        INSERT INTO llm_run (command, model, repos, estimate_usd, started_at, status)
        VALUES (?, ?, ?, ?, ?, 'running')
        """,
        (command, model, repos, estimate_usd, _utcnow()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_llm_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    batch_id: str | None = None,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE llm_run SET finished_at = ?, status = ?, batch_id = ?, cost_usd = ?,
               input_tokens = ?, output_tokens = ?, notes = ?
         WHERE id = ?
        """,
        (_utcnow(), status, batch_id, cost_usd, input_tokens, output_tokens, notes, run_id),
    )
    conn.commit()


# --- scores and boards -----------------------------------------------------


SCORE_COLUMNS = (
    "velocity_7d velocity_14d velocity_28d velocity_90d acceleration acceleration_basis "
    "relative_growth_14d fresh_power momentum_score peak_velocity days_since_peak "
    "days_to_1k days_to_10k days_to_50k breakout coverage_days"
).split()


def save_scores(conn: sqlite3.Connection, date: dt.date, metrics: Sequence[RepoMetrics]) -> int:
    rows = []
    for metric in metrics:
        if metric.repo_id is None:
            continue
        row = [metric.repo_id, date, metric.stars_total]
        for column in SCORE_COLUMNS:
            value = getattr(metric, column)
            row.append(int(value) if isinstance(value, bool) else value)
        rows.append(tuple(row))
    if not rows:
        return 0

    placeholders = ", ".join(["?"] * (3 + len(SCORE_COLUMNS)))
    updates = ", ".join(f"{c} = excluded.{c}" for c in ("stars_total", *SCORE_COLUMNS))
    conn.executemany(
        f"""
        INSERT INTO repo_scores (repo_id, date, stars_total, {", ".join(SCORE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT (repo_id, date) DO UPDATE SET {updates}
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def save_board_pools(conn: sqlite3.Connection, date: dt.date, pools: Mapping[tuple, int]) -> int:
    """Record how many repositories each board could have ranked today.

    The board file itself is truncated to its limit, so its length answers
    "how many did we show", never "out of how many". The Fresh Power tile was
    answering the second question with a third number — the count of repos with
    a completed backfill, which includes every repo we have never classified as
    AI and excludes nothing that scored zero."""
    conn.execute("DELETE FROM board_pool WHERE date = ?", (date,))
    rows = [(date, board, category, n) for (board, category), n in pools.items()]
    if rows:
        conn.executemany(
            "INSERT INTO board_pool (date, board, category, eligible) VALUES (?, ?, ?, ?)", rows
        )
    conn.commit()
    return len(rows)


def load_board_pools(conn: sqlite3.Connection, date: dt.date) -> dict[tuple[str, str], int]:
    return {
        (row["board"], row["category"]): row["eligible"]
        for row in conn.execute(
            "SELECT board, category, eligible FROM board_pool WHERE date = ?", (date,)
        )
    }


def save_leaderboards(conn: sqlite3.Connection, date: dt.date, entries: Sequence[Entry]) -> int:
    """Replace the day's boards wholesale so a re-run cannot leave stale ranks."""
    conn.execute("DELETE FROM leaderboard_snapshots WHERE date = ?", (date,))
    if entries:
        conn.executemany(
            """
            INSERT INTO leaderboard_snapshots (date, board, category, rank, repo_id, score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(date, e.board, e.category, e.rank, e.repo_id, e.score) for e in entries],
        )
    conn.commit()
    return len(entries)


def latest_board_date(conn: sqlite3.Connection) -> dt.date | None:
    row = conn.execute("SELECT max(date) AS d FROM leaderboard_snapshots").fetchone()
    # An aggregate loses its declared type, so this one is converted by hand.
    return dt.date.fromisoformat(row["d"]) if row and row["d"] else None


def load_leaderboard(
    conn: sqlite3.Connection,
    *,
    date: dt.date,
    board: str,
    category: str = "_all",
    limit: int = 200,
) -> list[dict]:
    return conn.execute(
        """
        SELECT l.rank, l.score, r.id AS repo_id, r.full_name, r.description,
               r.language, r.stars, r.archived, c.category, c.one_liner,
               s.velocity_14d, s.acceleration, s.acceleration_basis,
               s.relative_growth_14d,
               s.fresh_power, s.momentum_score, s.breakout,
               s.days_to_1k, s.days_to_10k, s.days_to_50k, s.coverage_days,
               r.history_backfilled_through,
               m.description_tr, m.usage_tr, m.matched_project, m.relevance
        FROM leaderboard_snapshots l
        JOIN repos r ON r.id = l.repo_id
        LEFT JOIN repo_classification c ON c.repo_id = l.repo_id
        LEFT JOIN repo_summary m ON m.repo_id = l.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = l.repo_id AND s.date = l.date
        WHERE l.date = :date AND l.board = :board AND l.category = :category
          AND """
        + CURRENT_NAME_SQL
        + """
        ORDER BY l.rank
        LIMIT :limit
        """,
        {"date": date, "board": board, "category": category, "limit": limit},
    ).fetchall()


def previous_board_date(
    conn: sqlite3.Connection, *, before: dt.date, board: str, category: str = "_all"
) -> dt.date | None:
    """The date `previous_board_ranks` compares against.

    A board's movement line has to say what it moved *since*. Yesterday is the
    usual answer and not a safe assumption: a day the scheduled run failed
    leaves a two-day gap, and reporting that as "since yesterday" would make a
    two-day shift look like a one-day one.
    """
    row = conn.execute(
        """
        SELECT max(date) AS d FROM leaderboard_snapshots
        WHERE date < :before AND board = :board AND category = :category
        """,
        {"before": before, "board": board, "category": category},
    ).fetchone()
    if not row or not row["d"]:
        return None
    value = row["d"]
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def previous_board_ranks(
    conn: sqlite3.Connection, *, before: dt.date, board: str, category: str = "_all"
) -> dict[int, int]:
    """Ranks from the most recent snapshot strictly before `before`."""
    row = conn.execute(
        """
        SELECT max(date) AS d FROM leaderboard_snapshots
        WHERE date < :before AND board = :board AND category = :category
        """,
        {"before": before, "board": board, "category": category},
    ).fetchone()
    if not row or not row["d"]:
        return {}
    return {
        r["repo_id"]: r["rank"]
        for r in conn.execute(
            """
            SELECT repo_id, rank FROM leaderboard_snapshots
            WHERE date = :date AND board = :board AND category = :category
            """,
            {"date": row["d"], "board": board, "category": category},
        )
    }


# --- run bookkeeping -------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str) -> int:
    cursor = conn.execute(
        "INSERT INTO run_log (command, started_at) VALUES (?, ?)",
        (command, _utcnow()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ok: bool,
    api_calls: int = 0,
    api_304s: int = 0,
    llm_in_tok: int = 0,
    llm_out_tok: int = 0,
    llm_cost_usd: float = 0.0,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE run_log SET finished_at = :finished_at, ok = :ok, api_calls = :api_calls,
                           api_304s = :api_304s, llm_in_tok = :llm_in_tok,
                           llm_out_tok = :llm_out_tok, llm_cost_usd = :llm_cost_usd,
                           notes = :notes
        WHERE id = :run_id
        """,
        {
            "finished_at": _utcnow(),
            "ok": int(ok),
            "api_calls": api_calls,
            "api_304s": api_304s,
            "llm_in_tok": llm_in_tok,
            "llm_out_tok": llm_out_tok,
            "llm_cost_usd": llm_cost_usd,
            "notes": notes,
            "run_id": run_id,
        },
    )
    conn.commit()


# --- discovery bookkeeping -------------------------------------------------


def add_pending(conn: sqlite3.Connection, names: Iterable[str], *, source: str) -> int:
    """Queue repos known only by `owner/name` for later resolution."""
    rows = [(name, source, _utcnow()) for name in {n for n in names if n and "/" in n}]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO pending_repos (full_name, source, added_at) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def unresolved_pending(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT p.full_name, p.source
        FROM pending_repos p
        LEFT JOIN repos r ON lower(r.full_name) = lower(p.full_name)
        WHERE p.resolved_at IS NULL AND p.failed = 0 AND r.id IS NULL
        ORDER BY p.added_at
    """
    params: dict = {}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return conn.execute(sql, params).fetchall()


def mark_pending_resolved(conn: sqlite3.Connection, full_name: str) -> None:
    conn.execute(
        "UPDATE pending_repos SET resolved_at = ? WHERE full_name = ?",
        (_utcnow(), full_name),
    )


def mark_pending_failed(conn: sqlite3.Connection, full_name: str, note: str) -> None:
    conn.execute(
        "UPDATE pending_repos SET failed = 1, note = ? WHERE full_name = ?",
        (note[:500], full_name),
    )


def record_topic_query(
    conn: sqlite3.Connection, topic: str, *, source: str, repos_found: int
) -> None:
    now = _utcnow()
    conn.execute(
        """
        INSERT INTO queried_topics (topic, source, first_queried_at, last_queried_at,
                                    repos_found)
        VALUES (:topic, :source, :now, :now, :found)
        ON CONFLICT (topic) DO UPDATE SET
            last_queried_at = excluded.last_queried_at,
            repos_found = excluded.repos_found
        """,
        {"topic": topic.lower(), "source": source, "now": now, "found": repos_found},
    )
    conn.commit()


def queried_topics(conn: sqlite3.Connection) -> set[str]:
    return {r["topic"] for r in conn.execute("SELECT topic FROM queried_topics")}


def topics_of_ai_repos(conn: sqlite3.Connection) -> list[str]:
    """Every topic appearing on a repo the classifier confirmed as AI.

    Feeding these back into discovery is what keeps the seed vocabulary from
    going stale — the next `mcp` arrives on its own.
    """
    return [
        row["topic"]
        for row in conn.execute(
            """
            SELECT t.topic
            FROM repo_topics t
            JOIN repo_classification c ON c.repo_id = t.repo_id AND c.is_ai = 1
            """
        )
    ]


def repo_topics_map(conn: sqlite3.Connection, repo_ids: Sequence[int] | None = None):
    """repo_id -> sorted topics. SQLite has no ordered array aggregate, so the
    ordering is done in the subquery and the result comes back as JSON."""
    sql = """
        SELECT r.id AS repo_id,
               (SELECT json_group_array(topic)
                  FROM (SELECT topic FROM repo_topics
                        WHERE repo_id = r.id ORDER BY topic)) AS topics
        FROM repos r
    """
    params: dict = {}
    if repo_ids:
        sql += f" WHERE r.id IN ({', '.join('?' * len(repo_ids))})"
        return {
            row["repo_id"]: json.loads(row["topics"] or "[]")
            for row in conn.execute(sql, tuple(repo_ids))
        }
    return {row["repo_id"]: json.loads(row["topics"] or "[]") for row in conn.execute(sql, params)}


def load_scoring_rows(
    conn: sqlite3.Connection, *, today: dt.date, half_life_days: float
) -> list[dict]:
    """Everything scoring needs about each AI repo, in one query.

    The carried tail comes back already decayed to `today`, so the caller never
    has to know the decay constant — and a missed run simply means a larger age
    in that one calculation rather than a silently wrong score.
    """
    import math

    decay = math.log(2) / half_life_days
    rows = conn.execute(
        """
        SELECT r.id, r.created_at, r.stars, r.fresh_power_tail, r.fresh_power_tail_asof,
               r.history_backfilled_through, r.days_to_1k, r.days_to_10k, r.days_to_50k,
               c.category
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        WHERE r.is_fork = 0 AND """
        + CURRENT_NAME_SQL
        + """
        ORDER BY r.stars DESC
        """
    ).fetchall()

    for row in rows:
        row["carried_tail"] = _tail_at(row, today, decay)
    return rows


def load_explosion_inputs(
    conn: sqlite3.Connection, *, today: dt.date, lookback_days: int
) -> list[dict]:
    """Every repository captured today, with its captures since `lookback_days`.

    No classification join: the explosion boards are the one place where AI is
    a label and not a gate. Read as plain tuples — 14 days of census is about
    1.1M rows, and building a dict per row is most of the cost.
    """
    since = today - dt.timedelta(days=lookback_days)
    cursor = conn.cursor()
    cursor.row_factory = None
    points: dict[int, list[tuple[dt.date, int]]] = {}
    forks: dict[int, int | None] = {}
    for repo_id, date, stars, fork_count in cursor.execute(
        "SELECT repo_id, date, stars, forks FROM repo_snapshots WHERE date >= ? AND date <= ?",
        (since, today),
    ):
        points.setdefault(repo_id, []).append((date, stars))
        if date == today:
            forks[repo_id] = fork_count
    out = []
    for repo_id, created_at in cursor.execute(
        "SELECT r.id, r.created_at FROM repos r WHERE r.is_fork = 0 AND r.archived = 0 AND "
        + CURRENT_NAME_SQL
    ):
        if repo_id not in forks:
            continue
        out.append(
            {
                "repo_id": repo_id,
                "created_at": created_at.date() if created_at else None,
                "points": points[repo_id],
                "forks": forks[repo_id],
            }
        )
    return out


def snapshot_count(conn: sqlite3.Connection, *, date: dt.date) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM repo_snapshots WHERE date = ?", (date,)
    ).fetchone()
    return int(row["n"]) if row else 0


def save_explosions(conn: sqlite3.Connection, date: dt.date, items: Sequence) -> int:
    """Replace the day's explosion boards. `score` runs several times a day."""
    conn.execute("DELETE FROM explosion_board WHERE date = ?", (date,))
    conn.executemany(
        """
        INSERT INTO explosion_board (date, board, rank, repo_id, stars, forks, age_days,
                                     window_days, gain_window, gain_1d, score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                date,
                e.board,
                e.rank,
                e.repo_id,
                e.stars,
                e.forks,
                e.age_days,
                e.window_days,
                e.gain_window,
                e.gain_1d,
                e.score,
            )
            for e in items
        ],
    )
    conn.commit()
    return len(items)


def latest_explosion_date(conn: sqlite3.Connection, *, on_or_before: dt.date) -> dt.date | None:
    row = conn.execute(
        "SELECT max(date) AS d FROM explosion_board WHERE date <= ?", (on_or_before,)
    ).fetchone()
    return _as_date(row["d"]) if row and row["d"] else None


def previous_explosion_date(
    conn: sqlite3.Connection, *, before: dt.date, board: str
) -> dt.date | None:
    """The day an explosion board's movement is measured against — not
    "yesterday" by assumption, for the same reason as `previous_board_date`."""
    row = conn.execute(
        "SELECT max(date) AS d FROM explosion_board WHERE date < ? AND board = ?",
        (before, board),
    ).fetchone()
    return _as_date(row["d"]) if row and row["d"] else None


def explosion_ranks(conn: sqlite3.Connection, *, date: dt.date, board: str) -> dict[int, int]:
    return {
        row["repo_id"]: row["rank"]
        for row in conn.execute(
            "SELECT repo_id, rank FROM explosion_board WHERE date = ? AND board = ?",
            (date, board),
        )
    }


def load_explosion_board(conn: sqlite3.Connection, *, date: dt.date, board: str) -> list[dict]:
    """One explosion board with everything a row shows, classification as a label.

    `is_ai` keeps its three values apart: 1, 0 and NULL ("not settled") are
    different statements, and the site must not print the last two alike.
    """
    return conn.execute(
        """
        SELECT e.rank, e.score, e.stars, e.forks, e.age_days, e.window_days,
               e.gain_window, e.gain_1d,
               r.id AS repo_id, r.full_name, r.description, r.language, r.license,
               r.homepage, r.created_at, r.first_seen_at, r.discovered_via,
               r.readme_excerpt,
               c.is_ai, c.category, c.confidence,
               m.description_tr, m.usage_tr, m.matched_project, m.relevance,
               v.status AS evidence_status, v.checked_on AS evidence_checked_on,
               v.agent_file, v.built, v.agent_ready, v.integrated, v.mentions
        FROM explosion_board e
        JOIN repos r ON r.id = e.repo_id
        LEFT JOIN repo_classification c ON c.repo_id = e.repo_id
        LEFT JOIN repo_summary m ON m.repo_id = e.repo_id
        LEFT JOIN repo_ai_evidence v ON v.repo_id = e.repo_id
        WHERE e.date = ? AND e.board = ?
        ORDER BY e.rank
        """,
        (date, board),
    ).fetchall()


def explosion_evidence_targets(
    conn: sqlite3.Connection, *, date: dt.date, stale_before: dt.date, limit: int | None = None
) -> list[dict]:
    """Explosion-board repositories whose AI evidence is missing, old, or was
    not a clean read last time (a refusal is re-asked, not remembered)."""
    return conn.execute(
        """
        SELECT r.id AS repo_id, r.full_name, r.description, r.readme_excerpt,
               min(e.rank) AS best_rank
        FROM explosion_board e
        JOIN repos r ON r.id = e.repo_id
        LEFT JOIN repo_ai_evidence v ON v.repo_id = e.repo_id
        WHERE e.date = :date
          AND (v.repo_id IS NULL OR v.checked_on < :stale OR v.status != 'ok')
        GROUP BY r.id
        ORDER BY best_rank, r.id
        LIMIT :limit
        """,
        {"date": date, "stale": stale_before, "limit": -1 if limit is None else limit},
    ).fetchall()


def save_ai_evidence(
    conn: sqlite3.Connection, repo_id: int, evidence, *, checked_on: dt.date
) -> None:
    conn.execute(
        """
        INSERT INTO repo_ai_evidence (repo_id, checked_on, status, agent_file, built,
                                      agent_ready, integrated, mentions, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (repo_id) DO UPDATE SET
            checked_on = excluded.checked_on, status = excluded.status,
            agent_file = excluded.agent_file, built = excluded.built,
            agent_ready = excluded.agent_ready, integrated = excluded.integrated,
            mentions = excluded.mentions, notes = excluded.notes
        """,
        (
            repo_id,
            checked_on,
            evidence.status,
            evidence.agent_file,
            evidence.built,
            evidence.agent_ready,
            evidence.integrated,
            evidence.mentions,
            "; ".join(evidence.notes) or None,
        ),
    )


def _as_date(value) -> dt.date:
    # An aggregate loses its declared type, so max(date) comes back as text.
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def reset_history(conn: sqlite3.Connection, *, only_inflated: bool = True) -> int:
    """Clear the derived star history so a backfill can rebuild it.

    The weekly buckets and `fresh_power_tail` are one-way: the day rows they
    were folded from are deleted, so a wrong total cannot be recomputed from
    what is left. Re-fetching is the only repair.

    `only_inflated` limits the work to repos whose recorded history already
    exceeds their actual star count, which cannot happen legitimately — a repo
    cannot have gained more stars than it has.
    """
    condition = ""
    if only_inflated:
        condition = """
            AND (
                COALESCE((SELECT sum(stars_gained) FROM repo_star_daily
                          WHERE repo_id = repos.id), 0)
              + COALESCE((SELECT sum(stars_gained) FROM repo_star_weekly
                          WHERE repo_id = repos.id), 0)
            ) > repos.stars
        """

    targets = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM repos WHERE history_backfilled_through IS NOT NULL {condition}"
        )
    ]
    if not targets:
        return 0

    marks = ",".join("?" * len(targets))
    conn.execute(f"DELETE FROM repo_star_weekly WHERE repo_id IN ({marks})", targets)
    conn.execute(f"DELETE FROM repo_star_daily WHERE repo_id IN ({marks})", targets)
    conn.execute(
        f"""
        UPDATE repos SET history_backfilled_through = NULL,
                         fresh_power_tail = 0,
                         fresh_power_tail_asof = NULL,
                         etag_history = NULL
        WHERE id IN ({marks})
        """,
        targets,
    )
    conn.commit()
    return len(targets)
