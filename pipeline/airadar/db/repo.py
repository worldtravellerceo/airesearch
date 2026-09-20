"""Data access layer.

Everything here is idempotent: collection runs get interrupted by rate limits,
Actions timeouts and transient failures, and re-running must converge rather
than duplicate or double-count.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import Entry
from airadar.scoring.metrics import RepoMetrics

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


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


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


@contextmanager
def connect(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text())
    conn.commit()


# --- repositories ----------------------------------------------------------


def upsert_repos(conn: psycopg.Connection, repos: Sequence[RepoRecord]) -> int:
    """Insert or refresh repo rows. `discovered_via` is kept from first sighting."""
    if not repos:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO repos (id, full_name, owner, name, created_at, description,
                               homepage, language, license, archived, is_fork, stars,
                               discovered_via)
            VALUES (%(id)s, %(full_name)s, %(owner)s, %(name)s, %(created_at)s,
                    %(description)s, %(homepage)s, %(language)s, %(license)s,
                    %(archived)s, %(is_fork)s, %(stars)s, %(discovered_via)s)
            ON CONFLICT (id) DO UPDATE SET
                full_name   = EXCLUDED.full_name,
                owner       = EXCLUDED.owner,
                name        = EXCLUDED.name,
                created_at  = COALESCE(EXCLUDED.created_at, repos.created_at),
                description = EXCLUDED.description,
                homepage    = EXCLUDED.homepage,
                language    = EXCLUDED.language,
                license     = EXCLUDED.license,
                archived    = EXCLUDED.archived,
                is_fork     = EXCLUDED.is_fork,
                stars       = EXCLUDED.stars
            """,
            [r.__dict__ | {"topics": None} for r in repos],
        )
        topic_rows = [(r.id, t) for r in repos for t in r.topics]
        if topic_rows:
            cur.executemany(
                "INSERT INTO repo_topics (repo_id, topic) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                topic_rows,
            )
    conn.commit()
    return len(repos)


def set_etag(conn: psycopg.Connection, repo_id: int, *, column: str, etag: str | None) -> None:
    if column not in {"etag_repo", "etag_history"}:
        raise ValueError(f"refusing to write unknown column {column!r}")
    conn.execute(f"UPDATE repos SET {column} = %s WHERE id = %s", (etag, repo_id))


def mark_checked(conn: psycopg.Connection, repo_id: int, when: dt.datetime) -> None:
    conn.execute("UPDATE repos SET last_checked_at = %s WHERE id = %s", (when, repo_id))


def repos_due_for_refresh(
    conn: psycopg.Connection,
    *,
    tier1_size: int,
    now: dt.datetime,
    limit: int | None = None,
) -> list[dict]:
    """Repos to collect this run, most-neglected first.

    Tiering keeps the daily job inside one hour of PAT quota: the top `tier1_size`
    repos by stars are refreshed every day, everything else weekly.
    """
    sql = """
        WITH ranked AS (
            SELECT id, full_name, created_at, stars, etag_repo, etag_history,
                   last_checked_at, history_backfilled_through,
                   ROW_NUMBER() OVER (ORDER BY stars DESC) AS star_rank
            FROM repos
            WHERE NOT is_fork
        )
        SELECT * FROM ranked
        WHERE last_checked_at IS NULL
           OR (star_rank <= %(tier1)s AND last_checked_at < %(daily_cutoff)s)
           OR (star_rank >  %(tier1)s AND last_checked_at < %(weekly_cutoff)s)
        ORDER BY last_checked_at NULLS FIRST, star_rank
    """
    params: dict = {
        "tier1": tier1_size,
        "daily_cutoff": now - dt.timedelta(hours=20),
        "weekly_cutoff": now - dt.timedelta(days=7),
    }
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    return conn.execute(sql, params).fetchall()


# --- time series -----------------------------------------------------------


def record_snapshot(
    conn: psycopg.Connection,
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
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo_id, date) DO UPDATE SET
            stars = EXCLUDED.stars, forks = EXCLUDED.forks,
            open_issues = EXCLUDED.open_issues, pushed_at = EXCLUDED.pushed_at
        """,
        (repo_id, date, stars, forks, open_issues, pushed_at),
    )


def record_star_daily(conn: psycopg.Connection, repo_id: int, days: Iterable[DailyStars]) -> int:
    """Upsert per-day star deltas.

    The API restates recent weeks as they fill in, so later values overwrite
    earlier ones rather than accumulating.
    """
    rows = [(repo_id, d.date, d.stars_gained) for d in days]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO repo_star_daily (repo_id, date, stars_gained)
            VALUES (%s, %s, %s)
            ON CONFLICT (repo_id, date) DO UPDATE SET stars_gained = EXCLUDED.stars_gained
            """,
            rows,
        )
    return len(rows)


def set_backfill_watermark(conn: psycopg.Connection, repo_id: int, oldest: dt.date) -> None:
    conn.execute(
        "UPDATE repos SET history_backfilled_through = %s WHERE id = %s", (oldest, repo_id)
    )


def load_daily_series(
    conn: psycopg.Connection, repo_id: int, *, since: dt.date | None = None
) -> list[DailyStars]:
    sql = "SELECT date, stars_gained FROM repo_star_daily WHERE repo_id = %s"
    params: list = [repo_id]
    if since is not None:
        sql += " AND date >= %s"
        params.append(since)
    sql += " ORDER BY date"
    return [DailyStars(r["date"], r["stars_gained"]) for r in conn.execute(sql, params)]


# --- classification --------------------------------------------------------


def save_classification(
    conn: psycopg.Connection,
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (repo_id) DO UPDATE SET
            is_ai = EXCLUDED.is_ai, category = EXCLUDED.category,
            subcategory = EXCLUDED.subcategory, confidence = EXCLUDED.confidence,
            method = EXCLUDED.method, one_liner = EXCLUDED.one_liner,
            content_hash = EXCLUDED.content_hash, classified_at = now()
        """,
        (repo_id, is_ai, category, subcategory, confidence, method, one_liner, content_hash),
    )


def cached_classification_hashes(conn: psycopg.Connection) -> dict[int, str]:
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


def save_scores(conn: psycopg.Connection, date: dt.date, metrics: Sequence[RepoMetrics]) -> int:
    rows = [
        (m.repo_id, date, m.stars_total, *[getattr(m, c) for c in SCORE_COLUMNS])
        for m in metrics
        if m.repo_id is not None
    ]
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * (3 + len(SCORE_COLUMNS)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ("stars_total", *SCORE_COLUMNS))
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO repo_scores (repo_id, date, stars_total, {", ".join(SCORE_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT (repo_id, date) DO UPDATE SET {updates}
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def save_leaderboards(conn: psycopg.Connection, date: dt.date, entries: Sequence[Entry]) -> int:
    """Replace the day's boards wholesale so a re-run cannot leave stale ranks."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM leaderboard_snapshots WHERE date = %s", (date,))
        if entries:
            cur.executemany(
                """
                INSERT INTO leaderboard_snapshots (date, board, category, rank, repo_id, score)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [(date, e.board, e.category, e.rank, e.repo_id, e.score) for e in entries],
            )
    conn.commit()
    return len(entries)


def load_leaderboard(
    conn: psycopg.Connection,
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
        WHERE l.date = %s AND l.board = %s AND l.category = %s
        ORDER BY l.rank
        LIMIT %s
        """,
        (date, board, category, limit),
    ).fetchall()


def previous_board_ranks(
    conn: psycopg.Connection, *, before: dt.date, board: str, category: str = "_all"
) -> dict[int, int]:
    """Ranks from the most recent snapshot strictly before `before`."""
    row = conn.execute(
        """
        SELECT max(date) AS date FROM leaderboard_snapshots
        WHERE date < %s AND board = %s AND category = %s
        """,
        (before, board, category),
    ).fetchone()
    if not row or row["date"] is None:
        return {}
    return {
        r["repo_id"]: r["rank"]
        for r in conn.execute(
            """
            SELECT repo_id, rank FROM leaderboard_snapshots
            WHERE date = %s AND board = %s AND category = %s
            """,
            (row["date"], board, category),
        )
    }


# --- run bookkeeping -------------------------------------------------------


def start_run(conn: psycopg.Connection, command: str) -> int:
    row = conn.execute(
        "INSERT INTO run_log (command) VALUES (%s) RETURNING id", (command,)
    ).fetchone()
    conn.commit()
    return row["id"]


def finish_run(
    conn: psycopg.Connection,
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
        UPDATE run_log SET finished_at = now(), ok = %s, api_calls = %s, api_304s = %s,
                           llm_in_tok = %s, llm_out_tok = %s, llm_cost_usd = %s, notes = %s
        WHERE id = %s
        """,
        (ok, api_calls, api_304s, llm_in_tok, llm_out_tok, llm_cost_usd, notes, run_id),
    )
    conn.commit()


# --- discovery bookkeeping -------------------------------------------------


def add_pending(conn: psycopg.Connection, names: Iterable[str], *, source: str) -> int:
    """Queue repos known only by `owner/name` for later resolution."""
    rows = [(name, source) for name in {n for n in names if n and "/" in n}]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO pending_repos (full_name, source) VALUES (%s, %s) "
            "ON CONFLICT (full_name) DO NOTHING",
            rows,
        )
    conn.commit()
    return len(rows)


def unresolved_pending(conn: psycopg.Connection, *, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT p.full_name, p.source
        FROM pending_repos p
        LEFT JOIN repos r ON lower(r.full_name) = lower(p.full_name)
        WHERE p.resolved_at IS NULL AND NOT p.failed AND r.id IS NULL
        ORDER BY p.added_at
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def mark_pending_resolved(conn: psycopg.Connection, full_name: str) -> None:
    conn.execute("UPDATE pending_repos SET resolved_at = now() WHERE full_name = %s", (full_name,))


def mark_pending_failed(conn: psycopg.Connection, full_name: str, note: str) -> None:
    conn.execute(
        "UPDATE pending_repos SET failed = TRUE, note = %s WHERE full_name = %s",
        (note[:500], full_name),
    )


def record_topic_query(
    conn: psycopg.Connection, topic: str, *, source: str, repos_found: int
) -> None:
    conn.execute(
        """
        INSERT INTO queried_topics (topic, source, repos_found)
        VALUES (%s, %s, %s)
        ON CONFLICT (topic) DO UPDATE SET
            last_queried_at = now(),
            repos_found = EXCLUDED.repos_found
        """,
        (topic.lower(), source, repos_found),
    )
    conn.commit()


def queried_topics(conn: psycopg.Connection) -> set[str]:
    return {r["topic"] for r in conn.execute("SELECT topic FROM queried_topics")}


def topics_of_ai_repos(conn: psycopg.Connection) -> list[str]:
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
            JOIN repo_classification c ON c.repo_id = t.repo_id AND c.is_ai
            """
        )
    ]
