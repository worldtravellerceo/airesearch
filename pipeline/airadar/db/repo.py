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
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import Entry
from airadar.scoring.metrics import RepoMetrics

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


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


# --- repositories ----------------------------------------------------------


def upsert_repos(conn: sqlite3.Connection, repos: Sequence[RepoRecord]) -> int:
    """Insert or refresh repo rows. `discovered_via` is kept from first sighting."""
    if not repos:
        return 0

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
    if column not in {"etag_repo", "etag_history"}:
        raise ValueError(f"refusing to write unknown column {column!r}")
    conn.execute(f"UPDATE repos SET {column} = ? WHERE id = ?", (etag, repo_id))


def mark_checked(conn: sqlite3.Connection, repo_id: int, when: dt.datetime) -> None:
    conn.execute("UPDATE repos SET last_checked_at = ? WHERE id = ?", (when, repo_id))


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
    sql = """
        WITH ranked AS (
            SELECT id, full_name, created_at, stars, etag_repo, etag_history,
                   last_checked_at, history_backfilled_through,
                   ROW_NUMBER() OVER (ORDER BY stars DESC, id) AS star_rank
            FROM repos
            WHERE is_fork = 0
        )
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
    """Drop day rows older than the retention window, folding their decayed
    weight into each repo's carried tail first.

    Deleting them outright would silently shrink every long-lived project's
    `fresh_power`; folding them in keeps the number exact.
    """
    import math

    cutoff = today - dt.timedelta(days=retain_days)
    decay = math.log(2) / half_life_days

    doomed = conn.execute(
        "SELECT repo_id, date, stars_gained FROM repo_star_daily WHERE date < :cutoff",
        {"cutoff": cutoff},
    ).fetchall()
    if not doomed:
        return 0

    # Everything is restated as of `today`, so a later run only has to decay the
    # single carried number forward.
    additions: dict[int, float] = {}
    for row in doomed:
        age = (today - row["date"]).days
        additions[row["repo_id"]] = additions.get(row["repo_id"], 0.0) + row[
            "stars_gained"
        ] * math.exp(-decay * age)

    for repo_id, added in additions.items():
        current = conn.execute(
            "SELECT fresh_power_tail, fresh_power_tail_asof FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
        existing = _tail_at(current, today, decay) if current else 0.0
        conn.execute(
            "UPDATE repos SET fresh_power_tail = ?, fresh_power_tail_asof = ? WHERE id = ?",
            (existing + added, today, repo_id),
        )

    conn.execute("DELETE FROM repo_star_daily WHERE date < :cutoff", {"cutoff": cutoff})
    conn.commit()
    return len(doomed)


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
    is_ai: bool,
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
            "is_ai": int(is_ai),
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


# --- scores and boards -----------------------------------------------------


SCORE_COLUMNS = (
    "velocity_7d velocity_14d velocity_28d velocity_90d acceleration "
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
               s.velocity_14d, s.acceleration, s.relative_growth_14d,
               s.fresh_power, s.momentum_score, s.breakout,
               s.days_to_1k, s.days_to_10k, s.days_to_50k, s.coverage_days
        FROM leaderboard_snapshots l
        JOIN repos r ON r.id = l.repo_id
        LEFT JOIN repo_classification c ON c.repo_id = l.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = l.repo_id AND s.date = l.date
        WHERE l.date = :date AND l.board = :board AND l.category = :category
        ORDER BY l.rank
        LIMIT :limit
        """,
        {"date": date, "board": board, "category": category, "limit": limit},
    ).fetchall()


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
        WHERE r.is_fork = 0
        ORDER BY r.stars DESC
        """
    ).fetchall()

    for row in rows:
        row["carried_tail"] = _tail_at(row, today, decay)
    return rows
