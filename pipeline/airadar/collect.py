"""Collection and scoring orchestration.

Every function here is restartable. A daily run that dies halfway through — rate
limits, an Actions timeout, a flaky network — must be able to pick up where it
left off, which is why progress is committed per repo and the queue is ordered
by `last_checked_at`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import psycopg

from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh.client import GitHubClient, GitHubError
from airadar.gh.metrics import (
    HISTORY_PATH,
    MAX_HISTORY_PAGES,
    WEEKS_PER_PAGE,
    StarHistoryFormatError,
    parse_star_history,
)
from airadar.scoring.leaderboards import build_all
from airadar.scoring.metrics import compute_repo_metrics, score_cohort

log = logging.getLogger(__name__)

# Repos that 404 or 451 are gone (deleted, renamed away, DMCA'd). Drop them from
# the queue rather than retrying every run.
GONE_STATUSES = {404, 451}


@dataclass
class CollectReport:
    considered: int = 0
    refreshed: int = 0
    unchanged: int = 0
    gone: int = 0
    failed: int = 0
    days_written: int = 0

    def summary(self) -> str:
        return (
            f"{self.refreshed} refreshed, {self.unchanged} unchanged, "
            f"{self.gone} gone, {self.failed} failed, {self.days_written} day-rows"
        )


async def collect(
    conn: psycopg.Connection,
    client: GitHubClient,
    *,
    today: dt.date | None = None,
    limit: int | None = None,
) -> CollectReport:
    """Refresh metadata and recent star history for every repo that is due.

    Two requests per repo: the repo object, and page 1 of its star history —
    which covers ~210 days and so satisfies every bounded window (7/14/28/90d)
    without a backfill.
    """
    settings = get_settings()
    today = today or dt.date.today()
    now = dt.datetime.now(dt.UTC)
    report = CollectReport()

    queue = db.repos_due_for_refresh(conn, tier1_size=settings.tier1_size, now=now, limit=limit)
    report.considered = len(queue)
    log.info("collect: %d repos due", len(queue))

    for row in queue:
        try:
            report.days_written += await _collect_one(conn, client, row, today, report)
        except GitHubError as exc:
            if exc.status in GONE_STATUSES:
                log.info("collect: %s is gone (%s), removing", row["full_name"], exc.status)
                conn.execute("DELETE FROM repos WHERE id = %s", (row["id"],))
                conn.commit()
                report.gone += 1
                continue
            log.warning("collect: %s failed: %s", row["full_name"], exc)
            report.failed += 1
        except StarHistoryFormatError as exc:
            # A shape change in the endpoint is worth surfacing loudly rather
            # than silently writing nothing for every repo.
            log.error("collect: unexpected history shape for %s: %s", row["full_name"], exc)
            report.failed += 1

    return report


async def _collect_one(
    conn: psycopg.Connection,
    client: GitHubClient,
    row: dict,
    today: dt.date,
    report: CollectReport,
) -> int:
    full_name = row["full_name"]

    meta = await client.get(f"/repos/{full_name}", etag=row.get("etag_repo"))
    if meta.not_modified:
        report.unchanged += 1
        stars = row["stars"]
    else:
        record = db.RepoRecord.from_api(meta.data)
        db.upsert_repos(conn, [record])
        db.set_etag(conn, row["id"], column="etag_repo", etag=meta.etag)
        stars = record.stars
        db.record_snapshot(
            conn,
            row["id"],
            today,
            stars=stars,
            forks=meta.data.get("forks_count"),
            open_issues=meta.data.get("open_issues_count"),
            pushed_at=db.parse_ts(meta.data.get("pushed_at")),
        )
        report.refreshed += 1

    history = await client.get(
        HISTORY_PATH.format(full_name=full_name),
        params={"per_page": WEEKS_PER_PAGE, "page": 1},
        etag=row.get("etag_history"),
    )
    written = 0
    if not history.not_modified:
        days = parse_star_history(history.data)
        written = db.record_star_daily(conn, row["id"], days)
        db.set_etag(conn, row["id"], column="etag_history", etag=history.etag)

    db.mark_checked(conn, row["id"], dt.datetime.now(dt.UTC))
    conn.commit()
    return written


async def backfill(
    conn: psycopg.Connection,
    client: GitHubClient,
    *,
    full_name: str | None = None,
    limit: int | None = None,
) -> int:
    """Walk star history back to each repo's creation.

    Required for the Fresh Power board: `fresh_power` integrates a repo's whole
    life, and one page recovers only about 55% of it at a 180-day half-life.
    Costs roughly one request per 30 weeks of age, once per repo.
    """
    if full_name:
        rows = conn.execute(
            "SELECT id, full_name, created_at, history_backfilled_through "
            "FROM repos WHERE full_name = %s",
            (full_name,),
        ).fetchall()
    else:
        sql = """
            SELECT id, full_name, created_at, history_backfilled_through
            FROM repos
            WHERE NOT is_fork
              AND (history_backfilled_through IS NULL
                   OR history_backfilled_through > created_at::date + 7)
            ORDER BY stars DESC
        """
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT %s"
            params = (limit,)
        rows = conn.execute(sql, params).fetchall()

    total = 0
    for row in rows:
        total += await _backfill_one(conn, client, row)
    return total


async def _backfill_one(conn: psycopg.Connection, client: GitHubClient, row: dict) -> int:
    """Page backwards until the series reaches the repo's creation date.

    The stop conditions are data-driven rather than Link-header-driven: this
    endpoint is two weeks old, and a missing or unusual `Link` header must not
    silently truncate a repo's history to one page. An empty page, a page that
    fails to move the window further back, or reaching the creation date all
    end the walk; `MAX_HISTORY_PAGES` is the backstop.
    """
    created = row["created_at"].date() if row["created_at"] else None
    oldest_seen: dt.date | None = None
    written = 0

    for page in range(1, MAX_HISTORY_PAGES + 1):
        response = await client.get(
            HISTORY_PATH.format(full_name=row["full_name"]),
            params={"per_page": WEEKS_PER_PAGE, "page": page},
        )
        days = parse_star_history(response.data)
        if not days:
            break

        written += db.record_star_daily(conn, row["id"], days)
        page_oldest = days[0].date
        conn.commit()

        if oldest_seen is not None and page_oldest >= oldest_seen:
            # Not advancing — we have hit the end of the available history.
            break
        oldest_seen = page_oldest

        if created and oldest_seen <= created:
            break

    if oldest_seen:
        db.set_backfill_watermark(conn, row["id"], oldest_seen)
        conn.commit()
    log.info("backfill: %s -> %d day-rows", row["full_name"], written)
    return written


def score(conn: psycopg.Connection, *, today: dt.date | None = None) -> tuple[int, int]:
    """Recompute every metric and rebuild all boards for `today`.

    Pure database work — no API calls — so it is cheap to re-run after a
    reclassification or a scoring change.
    """
    settings = get_settings()
    today = today or dt.date.today()

    rows = conn.execute(
        """
        SELECT r.id, r.created_at, r.stars, c.category
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai
        WHERE NOT r.is_fork
        """
    ).fetchall()

    metrics = []
    for row in rows:
        series = db.load_daily_series(conn, row["id"])
        created = row["created_at"].date() if row["created_at"] else today
        metrics.append(
            compute_repo_metrics(
                days=series,
                stars_total=row["stars"],
                created_at=created,
                today=today,
                half_life_days=settings.fresh_power_half_life_days,
                repo_id=row["id"],
                category=row["category"],
            )
        )

    score_cohort(metrics)
    saved = db.save_scores(conn, today, metrics)
    entries = build_all(metrics)
    db.save_leaderboards(conn, today, entries)
    log.info("score: %d repos scored, %d board rows", saved, len(entries))
    return saved, len(entries)
