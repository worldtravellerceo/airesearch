"""Discovery orchestration: run every channel and persist what it finds.

Search-based channels hand back full repo objects, so those land in `repos`
directly. The other channels (curated lists, dependency graphs, the Hub) only
know a project by `owner/name`, and `repos.id` is GitHub's numeric id — the one
thing that survives a rename. Those names queue in `pending_repos` until a
resolve pass looks them up.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field

from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh import discovery
from airadar.gh.client import GitHubClient, GitHubError
from airadar.gh.vocabulary import SEED_KEYWORDS, SEED_TOPICS
from airadar.sources.ecosystems import discover_via_dependencies
from airadar.sources.huggingface import discover_via_huggingface

log = logging.getLogger(__name__)

GONE_STATUSES = {404, 451}


class CensusError(RuntimeError):
    """The census came back empty, which cannot be true of GitHub."""


@dataclass
class DiscoverReport:
    repos_upserted: int = 0
    pending_queued: int = 0
    pending_resolved: int = 0
    pending_failed: int = 0
    snowballed_topics: list[str] = field(default_factory=list)
    search: discovery.DiscoveryStats = field(default_factory=discovery.DiscoveryStats)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.repos_upserted} repos upserted",
            f"{self.pending_queued} queued",
            f"{self.pending_resolved} resolved",
        ]
        if self.pending_failed:
            parts.append(f"{self.pending_failed} unresolvable")
        if self.snowballed_topics:
            parts.append(f"snowball: {', '.join(self.snowballed_topics[:8])}")
        parts.extend(self.notes)
        return "; ".join(parts)


def _make_sink(conn: sqlite3.Connection, report: DiscoverReport, min_stars: int):
    """Persist search hits as they arrive, so an interrupted sweep keeps its work."""

    def sink(items: list[dict], channel: str) -> None:
        records = [
            db.RepoRecord.from_api(item, discovered_via=channel)
            for item in items
            if item.get("stargazers_count", 0) >= min_stars and not item.get("fork")
        ]
        if records:
            report.repos_upserted += db.upsert_repos(conn, records)

    return sink


async def discover(
    conn: sqlite3.Connection,
    client: GitHubClient,
    *,
    census: bool = True,
    topics: bool = True,
    keywords: bool = True,
    awesome: bool = True,
    ecosystems: bool = True,
    huggingface: bool = True,
    snowball: bool = True,
    snowball_limit: int = 40,
    resolve: bool = True,
    resolve_limit: int | None = None,
    query_budget: int | None = None,
) -> DiscoverReport:
    """Sweep every channel. Each is independently skippable so a long run can be
    split across several Actions jobs."""
    settings = get_settings()
    min_stars = settings.min_stars
    report = DiscoverReport()
    report.search.budget = query_budget
    sink = _make_sink(conn, report, min_stars)

    # First, and deliberately: this is the only channel that cannot miss a
    # popular project, so it must never be the one the query budget runs out
    # on. The topic sweep below is breadth; this is the guarantee.
    if census:
        errors_before = report.search.errors
        await discovery.search_partitioned(
            client,
            "fork:false",
            channel="census",
            sink=sink,
            min_stars=settings.census_min_stars,
            stats=report.search,
        )
        seen = report.search.channels.get("census", 0)
        report.notes.append(f"census: >={settings.census_min_stars} stars, {seen} sightings")
        # A census that sees nothing is a broken query, not an empty GitHub:
        # there are tens of thousands of repos above a thousand stars. Without
        # this the failure is a logged warning inside `_drain` and the run goes
        # green having quietly stopped guaranteeing anything.
        if seen == 0 and not report.search.exhausted:
            raise CensusError(
                f"census found no repos above {settings.census_min_stars} stars "
                f"({report.search.errors - errors_before} search errors). "
                "The query shape is almost certainly rejected, not the population empty."
            )

    if topics:
        already = db.queried_topics(conn)
        todo = [t for t in SEED_TOPICS if t.lower() not in already] or list(SEED_TOPICS)
        log.info("discover: %d topics to sweep", len(todo))
        for topic in todo:
            if report.search.exhausted:
                log.info("discover: search budget spent, stopping the topic sweep")
                break
            before = report.repos_upserted
            await discovery.search_partitioned(
                client,
                f"topic:{topic} fork:false",
                channel="topic",
                sink=sink,
                min_stars=min_stars,
                stats=report.search,
            )
            # Only record it as swept if the budget did not cut it short —
            # otherwise the next run would skip a topic it never finished.
            if not report.search.exhausted:
                db.record_topic_query(
                    conn, topic, source="seed", repos_found=report.repos_upserted - before
                )

    if keywords:
        for keyword in SEED_KEYWORDS:
            if report.search.exhausted:
                break
            quoted = f'"{keyword}"' if " " in keyword else keyword
            await discovery.search_partitioned(
                client,
                f"{quoted} in:name,description,readme fork:false",
                channel="keyword",
                sink=sink,
                min_stars=min_stars,
                stats=report.search,
            )

    if snowball:
        fresh = discovery.snowball_topics(
            db.topics_of_ai_repos(conn),
            already_queried=db.queried_topics(conn),
            limit=snowball_limit,
        )
        report.snowballed_topics = fresh
        for topic in fresh:
            if report.search.exhausted:
                break
            before = report.repos_upserted
            await discovery.search_partitioned(
                client,
                f"topic:{topic} fork:false",
                channel="snowball",
                sink=sink,
                min_stars=min_stars,
                stats=report.search,
            )
            db.record_topic_query(
                conn, topic, source="snowball", repos_found=report.repos_upserted - before
            )

    if awesome:
        names = await discovery.mine_awesome_lists(client, stats=report.search)
        report.pending_queued += db.add_pending(conn, names, source="awesome")
        report.notes.append(f"awesome: {len(names)} names")

    if ecosystems:
        names, stats = await discover_via_dependencies()
        report.pending_queued += db.add_pending(conn, names, source="ecosystems")
        report.notes.append(f"ecosystems: {stats.summary()}")

    if huggingface:
        names, stats = await discover_via_huggingface()
        report.pending_queued += db.add_pending(conn, names, source="huggingface")
        report.notes.append(f"huggingface: {stats.summary()}")

    if resolve:
        resolved, failed = await resolve_pending(conn, client, limit=resolve_limit)
        report.pending_resolved += resolved
        report.pending_failed += failed

    return report


async def resolve_pending(
    conn: sqlite3.Connection, client: GitHubClient, *, limit: int | None = None
) -> tuple[int, int]:
    """Look up queued `owner/name` entries and promote them into `repos`.

    One request each, and repos below the star floor are recorded as resolved
    rather than retried: they exist, they are simply too small to track, and a
    later run must not spend quota rediscovering that.
    """
    settings = get_settings()
    resolved = failed = 0

    for row in db.unresolved_pending(conn, limit=limit):
        full_name = row["full_name"]
        try:
            response = await client.get(f"/repos/{full_name}")
        except GitHubError as exc:
            if exc.status in GONE_STATUSES:
                db.mark_pending_failed(conn, full_name, f"gone ({exc.status})")
                conn.commit()
                failed += 1
                continue
            log.warning("resolve: %s failed: %s", full_name, exc)
            failed += 1
            continue

        payload = response.data or {}
        if payload.get("fork") or payload.get("stargazers_count", 0) < settings.min_stars:
            db.mark_pending_resolved(conn, full_name)
            conn.commit()
            continue

        db.upsert_repos(conn, [db.RepoRecord.from_api(payload, discovered_via=row["source"])])
        db.mark_pending_resolved(conn, full_name)
        conn.commit()
        resolved += 1

    return resolved, failed


def seed_topic_log(conn: sqlite3.Connection) -> int:
    """Record the seed vocabulary as queried without sweeping it (for tests and
    for resuming a run that already covered the seeds)."""
    for topic in SEED_TOPICS:
        db.record_topic_query(conn, topic, source="seed", repos_found=0)
    return len(SEED_TOPICS)


def discovery_overview(conn: sqlite3.Connection) -> dict:
    """Where the tracked universe came from — useful for judging recall."""
    rows = conn.execute(
        "SELECT COALESCE(discovered_via, 'unknown') AS channel, count(*) AS n "
        "FROM repos GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    pending = conn.execute(
        "SELECT count(*) AS n FROM pending_repos WHERE resolved_at IS NULL AND failed = 0"
    ).fetchone()
    return {
        "by_channel": {r["channel"]: r["n"] for r in rows},
        "total": sum(r["n"] for r in rows),
        "pending": pending["n"],
        "as_of": dt.date.today().isoformat(),
    }


def health(conn: sqlite3.Connection) -> dict:
    """A compact readout of what the database actually contains.

    Printed into every workflow summary. A run's logs are tens of megabytes and
    have to be downloaded to read; this is the thing that says whether the run
    did what it was supposed to, at a glance.
    """
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM repos) AS tracked,
            (SELECT count(*) FROM repos WHERE is_fork = 0) AS not_forks,
            (SELECT count(*) FROM repo_classification) AS classified,
            (SELECT count(*) FROM repo_classification WHERE is_ai = 1) AS ai_repos,
            (SELECT count(*) FROM repos WHERE history_backfilled_through IS NOT NULL)
                AS backfilled,
            (SELECT count(*) FROM repos WHERE last_checked_at IS NOT NULL) AS collected,
            (SELECT count(*) FROM repo_star_daily) AS day_rows,
            (SELECT count(*) FROM repo_star_weekly) AS week_rows,
            (SELECT count(*) FROM pending_repos
              WHERE resolved_at IS NULL AND failed = 0) AS pending,
            (SELECT count(*) FROM queried_topics) AS topics_swept
        """
    ).fetchone()

    # The gap between "classified" and "tracked" is the band the rule engine
    # could not settle — those repos are on no board until they are judged.
    row["unjudged"] = row["not_forks"] - row["classified"]
    row["boards_as_of"] = db.latest_board_date(conn)
    row["by_channel"] = {
        r["channel"]: r["n"]
        for r in conn.execute(
            "SELECT COALESCE(discovered_via, 'unknown') AS channel, count(*) AS n "
            "FROM repos GROUP BY 1 ORDER BY n DESC"
        )
    }
    row["by_category"] = {
        r["category"]: r["n"]
        for r in conn.execute(
            "SELECT COALESCE(category, 'yok') AS category, count(*) AS n "
            "FROM repo_classification WHERE is_ai = 1 GROUP BY 1 ORDER BY n DESC"
        )
    }
    row["recent_runs"] = conn.execute(
        "SELECT command, ok, api_calls, api_304s, notes, finished_at "
        "FROM run_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return row
