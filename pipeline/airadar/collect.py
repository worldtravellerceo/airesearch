"""Collection and scoring orchestration.

Every function here is restartable. A daily run that dies halfway through — rate
limits, an Actions timeout, a flaky network — must be able to pick up where it
left off, which is why progress is committed per repo and the queue is ordered
by `last_checked_at`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh.client import GitHubClient, GitHubError
from airadar.gh.content import fetch_readme
from airadar.gh.metrics import (
    HISTORY_PATH,
    MAX_HISTORY_PAGES,
    WEEKS_PER_PAGE,
    StarHistoryFormatError,
    parse_star_history,
)
from airadar.scoring.leaderboards import build_all, pool_sizes
from airadar.scoring.metrics import (
    compute_repo_metrics,
    milestone_days,
    score_cohort,
)

log = logging.getLogger(__name__)

# Repos that 404 or 451 are gone (deleted, renamed away, DMCA'd). Drop them from
# the queue rather than retrying every run.
GONE_STATUSES = {404, 451}


async def _for_each(
    rows: Sequence[dict],
    worker: Callable[[dict], Awaitable[None]],
    *,
    concurrency: int,
    deadline: dt.datetime | None = None,
) -> int:
    """Run `worker` over every row with `concurrency` requests in flight.

    Sequential collection is bounded by the round trip, not by quota: one
    request at ~250ms is four a second however much allowance is left. The
    workers share one queue rather than taking a slice each, so a repo with
    eight pages of history does not leave a worker idle at the end.

    This is safe against the SQLite connection because nothing here is
    threaded. Coroutines only interleave at an `await`, and every write in the
    workers below is separated from its neighbours by none — so no worker can
    observe another's half-written repo.

    `deadline` stops the workers cleanly, which is what makes a long job land.
    A README pass over the 53,475 repositories with no signal ran 322 minutes
    against a 330-minute job limit and was killed mid-flight — the work it had
    done survived only because the publish step runs on `always()`. A job that
    stops itself with time to spare publishes on purpose rather than by luck.
    Returns how many rows went unprocessed.
    """
    if not rows:
        return 0
    queue: asyncio.Queue[dict] = asyncio.Queue()
    for row in rows:
        queue.put_nowait(row)

    async def drain() -> None:
        while True:
            if deadline is not None and dt.datetime.now(dt.UTC) >= deadline:
                return
            try:
                row = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await worker(row)

    await asyncio.gather(*(drain() for _ in range(min(concurrency, len(rows)))))
    return queue.qsize()


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
    conn: sqlite3.Connection,
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

    # `track_limit` bounds the universe we collect at all. Leaving it off is not
    # a bigger index, it is a run that cannot finish: the corpus is 64,373 repos
    # and two requests each is 25 hours of quota against a job that is killed at
    # five and a half. Every daily run was being truncated part-way through the
    # star-rank order, so the tail was never refreshed at all.
    queue = db.repos_due_for_refresh(
        conn,
        tier1_size=settings.tier1_size,
        now=now,
        limit=limit,
        track_limit=settings.track_limit,
    )
    report.considered = len(queue)
    log.info(
        "collect: %d repos due, %d in flight across %d token(s)",
        len(queue),
        settings.concurrency,
        client.lanes,
    )

    async def one(row: dict) -> None:
        try:
            report.days_written += await _collect_one(conn, client, row, today, report)
        except GitHubError as exc:
            if exc.status in GONE_STATUSES:
                log.info("collect: %s is gone (%s), removing", row["full_name"], exc.status)
                conn.execute("DELETE FROM repos WHERE id = ?", (row["id"],))
                conn.commit()
                report.gone += 1
                return
            log.warning("collect: %s failed: %s", row["full_name"], exc)
            report.failed += 1
        except StarHistoryFormatError as exc:
            # A shape change in the endpoint is worth surfacing loudly rather
            # than silently writing nothing for every repo.
            log.error("collect: unexpected history shape for %s: %s", row["full_name"], exc)
            report.failed += 1

    await _for_each(queue, one, concurrency=settings.concurrency)
    return report


async def _collect_one(
    conn: sqlite3.Connection,
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
        # One page is thirty weeks, but only `retain_days` of it is kept as day
        # rows — the rest already lives in the weekly buckets, folded there by a
        # previous prune. Writing those days back would hand the next prune the
        # same days a second time, and it adds rather than replaces: the weekly
        # total and `fresh_power_tail` both grow on every cycle. That is how
        # `openclaw/openclaw` came to show 527k stars of history against 390k
        # actual, and `karpathy/autoresearch` 1.85x its real count.
        cutoff = today - dt.timedelta(days=get_settings().retain_days)
        days = [day for day in parse_star_history(history.data) if day.date >= cutoff]
        written = db.record_star_daily(conn, row["id"], days)
        db.set_etag(conn, row["id"], column="etag_history", etag=history.etag)

    db.mark_checked(conn, row["id"], dt.datetime.now(dt.UTC))
    conn.commit()
    return written


async def backfill(
    conn: sqlite3.Connection,
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
            "FROM repos WHERE lower(full_name) = lower(?)",
            (full_name,),
        ).fetchall()
    else:
        # A backfill is complete once its history reaches the creation date. The
        # week of slack absorbs the API's Sunday-aligned week buckets.
        sql = """
            SELECT id, full_name, created_at, history_backfilled_through
            FROM repos
            WHERE is_fork = 0
              AND (history_backfilled_through IS NULL
                   OR history_backfilled_through > date(created_at, '+7 days'))
            ORDER BY stars DESC
        """
        params: dict = {}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        rows = conn.execute(sql, params).fetchall()

    total = 0

    async def one(row: dict) -> None:
        nonlocal total
        try:
            total += await _backfill_one(conn, client, row)
        except GitHubError as exc:
            if exc.status in GONE_STATUSES:
                log.info("backfill: %s is gone (%s)", row["full_name"], exc.status)
                return
            # One repo failing must not abandon the other 17,798. The watermark
            # is not moved, so the next run picks this one up again.
            log.warning("backfill: %s failed: %s", row["full_name"], exc)
        except StarHistoryFormatError as exc:
            log.error("backfill: unexpected history shape for %s: %s", row["full_name"], exc)

    await _for_each(rows, one, concurrency=get_settings().concurrency)
    return total


async def _backfill_one(conn: sqlite3.Connection, client: GitHubClient, row: dict) -> int:
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
        # The milestones can only be read off a complete series, and that series
        # is about to be pruned down to a rolling window — so they are computed
        # and stored now, while the days are still here.
        if created and oldest_seen <= created + dt.timedelta(days=7):
            series = db.load_daily_series(conn, row["id"])
            db.set_milestones(conn, row["id"], milestone_days(series, created))
        conn.commit()
    log.info("backfill: %s -> %d day-rows", row["full_name"], written)
    return written


def score(conn: sqlite3.Connection, *, today: dt.date | None = None) -> tuple[int, int]:
    """Recompute every metric and rebuild all boards for `today`.

    Pure database work — no API calls — so it is cheap to re-run after a
    reclassification or a scoring change.
    """
    settings = get_settings()
    today = today or dt.date.today()

    rows = db.load_scoring_rows(
        conn, today=today, half_life_days=settings.fresh_power_half_life_days
    )

    metrics = []
    for row in rows:
        series = db.load_daily_series(conn, row["id"])
        created = row["created_at"].date() if row["created_at"] else today
        stored = (row["days_to_1k"], row["days_to_10k"], row["days_to_50k"])
        metrics.append(
            compute_repo_metrics(
                days=series,
                stars_total=row["stars"],
                created_at=created,
                today=today,
                half_life_days=settings.fresh_power_half_life_days,
                repo_id=row["id"],
                category=row["category"],
                carried_tail=row["carried_tail"],
                backfilled_through=row["history_backfilled_through"],
                # Milestones are computed once, during backfill, and persisted —
                # the days they were derived from are long pruned by now.
                milestones=stored if any(v is not None for v in stored) else None,
            )
        )

    score_cohort(metrics)
    saved = db.save_scores(conn, today, metrics)
    entries = build_all(metrics)
    db.save_leaderboards(conn, today, entries)
    db.save_board_pools(conn, today, pool_sizes(metrics))

    # Pruning happens after scoring, not before: today's numbers are computed
    # from the full retained window, and only then does the window slide.
    pruned = db.prune_star_history(
        conn,
        today=today,
        retain_days=settings.retain_days,
        half_life_days=settings.fresh_power_half_life_days,
    )
    log.info(
        "score: %d repos scored, %d board rows, %d day-rows pruned",
        saved,
        len(entries),
        pruned,
    )
    return saved, len(entries)


@dataclass
class ReadmeReport:
    considered: int = 0
    fetched: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0
    left: int = 0

    def summary(self) -> str:
        tail = f", {self.left} left for the next run" if self.left else ""
        return (
            f"{self.fetched} fetched, {self.unchanged} unchanged, "
            f"{self.missing} have none, {self.failed} failed "
            f"(of {self.considered} considered){tail}"
        )


async def fetch_readmes(
    conn: sqlite3.Connection,
    client: GitHubClient,
    *,
    limit: int | None = None,
    min_stars: int = 1000,
    refetch: bool = False,
    max_minutes: float | None = None,
) -> ReadmeReport:
    """Read the README of every repository the metadata could not place.

    This is the recall fix. A repository is invisible to the index when its
    name, description and topics say nothing about AI — and the engine records
    that as "not AI", which is the one reading a zero score does not support.
    Fourteen of the forty highest-star repositories created since July are in
    exactly that state.

    Ordered biggest first and bounded by `limit`, so an interrupted run has
    spent its requests on the repositories it would have hurt most to miss, and
    the next run carries on from where it stopped.
    """
    report = ReadmeReport()
    sql = """
        SELECT r.id, r.full_name, r.etag_readme
        FROM repos r
        LEFT JOIN repo_classification c ON c.repo_id = r.id
        WHERE r.is_fork = 0
          AND r.stars >= :min_stars
          AND COALESCE(c.confidence, 0) = 0
    """
    if not refetch:
        # A README we have already read, or established does not exist, is not
        # worth another request until the repo itself changes.
        sql += " AND r.readme_fetched_at IS NULL"
    sql += " ORDER BY r.stars DESC"
    params: dict = {"min_stars": min_stars}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit

    queue = conn.execute(sql, params).fetchall()
    report.considered = len(queue)
    settings = get_settings()
    log.info(
        "readme: %d repos with no signal, %d in flight",
        len(queue),
        settings.concurrency,
    )

    async def one(row: dict) -> None:
        try:
            result = await fetch_readme(client, row["full_name"], etag=row["etag_readme"])
        except GitHubError as exc:
            log.warning("readme: %s failed: %s", row["full_name"], exc)
            report.failed += 1
            return

        if result.unchanged:
            report.unchanged += 1
            db.mark_readme_checked(conn, row["id"])
        elif result.missing:
            report.missing += 1
            db.set_readme(conn, row["id"], excerpt="", etag=None)
        else:
            report.fetched += 1
            db.set_readme(conn, row["id"], excerpt=result.excerpt, etag=result.etag)
        conn.commit()

    deadline = (
        dt.datetime.now(dt.UTC) + dt.timedelta(minutes=max_minutes)
        if max_minutes is not None
        else None
    )
    report.left = await _for_each(queue, one, concurrency=settings.concurrency, deadline=deadline)
    log.info("readme: %s", report.summary())
    return report
