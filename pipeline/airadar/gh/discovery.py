"""Building the universe of AI-adjacent repositories.

GitHub's search API caps every query at 1,000 returned results, so no single
query can enumerate a population this size. The way through is partitioning:
slice each query by star range, and split any slice that hits the cap until
every slice fits underneath it. Star counts are power-law distributed, so the
splits are geometric — an arithmetic midpoint would put almost everything in
the lower half and barely make progress.

Four channels feed the universe, because no single one has good recall:

1. topic x star-bucket — broad and systematic
2. free-text keywords  — catches projects that never set topics
3. topic snowballing   — topics seen on confirmed AI repos become new queries
4. curated lists       — `awesome-*` READMEs, already human-filtered
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from airadar.config import get_settings
from airadar.gh.client import GitHubClient, GitHubError
from airadar.gh.vocabulary import AWESOME_LISTS, SEED_KEYWORDS, SEED_TOPICS

log = logging.getLogger(__name__)

SEARCH_RESULT_CAP = 1000
SEARCH_PAGE_SIZE = 100
# The upper bound of the initial star range. It has to be finite so the
# recursion terminates, and far above the largest repository that could ever
# exist so nothing falls off the top — a repo above this ceiling would simply
# never be queried, and the omission would be silent. Geometric splitting means
# the generous headroom costs one or two extra requests, not more.
MAX_STARS = 10_000_000
MAX_SPLIT_DEPTH = 24
GITHUB_EPOCH = dt.date(2008, 1, 1)

RepoSink = Callable[[list[dict], str], Awaitable[None] | None]

_REPO_LINK = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})",
    re.IGNORECASE,
)
# Paths under github.com that are not repositories.
_RESERVED_OWNERS = {
    "sponsors",
    "orgs",
    "topics",
    "collections",
    "features",
    "about",
    "pricing",
    "marketplace",
    "apps",
    "settings",
    "notifications",
    "explore",
    "search",
    "login",
    "join",
    "readme",
    "site",
    "security",
    "enterprise",
    "customer-stories",
}


@dataclass
class DiscoveryStats:
    """Counters, plus the search budget for this run.

    A full topic sweep is thousands of search requests at 30 per minute, which
    is more than one Actions job should hold. The budget lets a run stop
    cleanly and the next one pick up where it left off — `queried_topics`
    records what has already been swept, so nothing is redone.
    """

    queries: int = 0
    splits: int = 0
    pages: int = 0
    repos_seen: int = 0
    capped_queries: int = 0
    errors: int = 0
    budget: int | None = None
    channels: dict[str, int] = field(default_factory=dict)

    @property
    def exhausted(self) -> bool:
        # Pages, not queries: every page is one request against the 30-per-minute
        # search limit, and a single wide query can be ten of them.
        return self.budget is not None and self.pages >= self.budget

    def note(self, channel: str, count: int) -> None:
        self.channels[channel] = self.channels.get(channel, 0) + count

    def summary(self) -> str:
        channels = ", ".join(f"{k}={v}" for k, v in sorted(self.channels.items()))
        return (
            f"{self.repos_seen} repo sightings from {self.queries} queries "
            f"({self.splits} splits, {self.pages} pages, {self.errors} errors) — {channels}"
        )


async def _emit(sink: RepoSink, items: list[dict], channel: str) -> None:
    result = sink(items, channel)
    if asyncio.iscoroutine(result):
        await result


# --- partitioned search ----------------------------------------------------


async def search_partitioned(
    client: GitHubClient,
    base_query: str,
    *,
    channel: str,
    sink: RepoSink,
    min_stars: int,
    stats: DiscoveryStats | None = None,
) -> DiscoveryStats:
    """Enumerate every repo matching `base_query` above `min_stars`.

    Splits the star range recursively until each slice fits under the API's
    1,000-result ceiling, so the population is enumerated rather than merely
    sampled from the top.
    """
    stats = stats or DiscoveryStats()
    if stats.exhausted:
        return stats
    await _partition_by_stars(
        client, base_query, min_stars, MAX_STARS, channel, sink, stats, depth=0
    )
    return stats


async def _partition_by_stars(
    client: GitHubClient,
    base_query: str,
    low: int,
    high: int,
    channel: str,
    sink: RepoSink,
    stats: DiscoveryStats,
    *,
    depth: int,
) -> None:
    if stats.exhausted:
        return
    query = f"{base_query} stars:{low}..{high}"
    total = await _drain(client, query, channel, sink, stats)
    if total is None or total <= SEARCH_RESULT_CAP:
        return

    if low >= high or depth >= MAX_SPLIT_DEPTH:
        # A single star value still over the cap. Stars cannot separate these
        # repos, so cut on creation date instead.
        stats.capped_queries += 1
        await _partition_by_date(
            client, query, GITHUB_EPOCH, dt.date.today(), channel, sink, stats, depth=0
        )
        return

    midpoint = _geometric_midpoint(low, high)
    stats.splits += 1
    await _partition_by_stars(
        client, base_query, low, midpoint, channel, sink, stats, depth=depth + 1
    )
    await _partition_by_stars(
        client, base_query, midpoint + 1, high, channel, sink, stats, depth=depth + 1
    )


async def _partition_by_date(
    client: GitHubClient,
    base_query: str,
    start: dt.date,
    end: dt.date,
    channel: str,
    sink: RepoSink,
    stats: DiscoveryStats,
    *,
    depth: int,
) -> None:
    """Secondary split dimension for slices stars cannot separate."""
    if stats.exhausted:
        return
    query = f"{base_query} created:{start.isoformat()}..{end.isoformat()}"
    total = await _drain(client, query, channel, sink, stats)
    if total is None or total <= SEARCH_RESULT_CAP:
        return
    if (end - start).days <= 1 or depth >= MAX_SPLIT_DEPTH:
        # Genuinely un-splittable. We keep the top 1,000, which for a slice this
        # narrow is the best the API can offer.
        log.warning("discovery: %s stays over the cap at %d results", query, total)
        stats.capped_queries += 1
        return
    middle = start + (end - start) / 2
    stats.splits += 1
    await _partition_by_date(
        client, base_query, start, middle, channel, sink, stats, depth=depth + 1
    )
    await _partition_by_date(
        client,
        base_query,
        middle + dt.timedelta(days=1),
        end,
        channel,
        sink,
        stats,
        depth=depth + 1,
    )


async def _drain(
    client: GitHubClient, query: str, channel: str, sink: RepoSink, stats: DiscoveryStats
) -> int | None:
    """Page through one query, up to the API's hard ceiling. Returns total_count."""
    try:
        first = await client.search_repositories(query, page=1)
    except GitHubError as exc:
        log.warning("discovery: query failed (%s): %s", query, exc)
        stats.errors += 1
        return None

    stats.queries += 1
    stats.pages += 1
    payload = first.data or {}
    total = int(payload.get("total_count", 0))
    items = payload.get("items") or []
    if not items:
        return total

    await _emit(sink, items, channel)
    stats.repos_seen += len(items)
    stats.note(channel, len(items))

    if total > SEARCH_RESULT_CAP:
        # Caller will split; draining the first 1,000 now would be wasted quota.
        return total

    last_page = math.ceil(min(total, SEARCH_RESULT_CAP) / SEARCH_PAGE_SIZE)
    if last_page < 2:
        return total

    # Pages of one query are independent, so they are fetched together rather
    # than one round trip at a time. With several tokens the search limit is no
    # longer what governs a sweep — latency is, and a search response is slow.
    # A ten-page drain was ten waits in a row; it is now one.
    remaining = last_page - 1
    if stats.budget is not None:
        # Never overshoot the budget: the whole point of it is that the run
        # stops cleanly rather than being killed part-way.
        remaining = min(remaining, max(0, stats.budget - stats.pages))
    if remaining <= 0:
        return total

    pages = range(2, 2 + remaining)
    semaphore = asyncio.Semaphore(get_settings().concurrency)

    async def fetch(page: int):
        async with semaphore:
            try:
                return page, await client.search_repositories(query, page=page)
            except GitHubError as exc:
                log.warning("discovery: page %d failed (%s): %s", page, query, exc)
                return page, exc

    results = await asyncio.gather(*(fetch(page) for page in pages))

    # Applied in page order so a short page still means the end of the results,
    # the way it did when they arrived one at a time.
    for _, outcome in sorted(results, key=lambda pair: pair[0]):
        if isinstance(outcome, GitHubError):
            stats.errors += 1
            continue
        stats.pages += 1
        page_items = (outcome.data or {}).get("items") or []
        if not page_items:
            break
        await _emit(sink, page_items, channel)
        stats.repos_seen += len(page_items)
        stats.note(channel, len(page_items))

    return total


def _geometric_midpoint(low: int, high: int) -> int:
    """Split a star range so both halves hold comparable populations.

    Stars follow a power law: an arithmetic midpoint of 50..1,000,000 would put
    essentially every repository in the lower half and the recursion would crawl.
    """
    if high <= low:
        return low
    midpoint = int(math.sqrt(max(low, 1) * high))
    return min(max(midpoint, low), high - 1)


# --- channels --------------------------------------------------------------


async def discover_by_topics(
    client: GitHubClient,
    *,
    sink: RepoSink,
    min_stars: int,
    topics: Iterable[str] = SEED_TOPICS,
    stats: DiscoveryStats | None = None,
) -> DiscoveryStats:
    stats = stats or DiscoveryStats()
    for topic in topics:
        await search_partitioned(
            client,
            f"topic:{topic} fork:false",
            channel="topic",
            sink=sink,
            min_stars=min_stars,
            stats=stats,
        )
    return stats


async def discover_by_keywords(
    client: GitHubClient,
    *,
    sink: RepoSink,
    min_stars: int,
    keywords: Iterable[str] = SEED_KEYWORDS,
    stats: DiscoveryStats | None = None,
) -> DiscoveryStats:
    stats = stats or DiscoveryStats()
    for keyword in keywords:
        quoted = f'"{keyword}"' if " " in keyword else keyword
        await search_partitioned(
            client,
            f"{quoted} in:name,description,readme fork:false",
            channel="keyword",
            sink=sink,
            min_stars=min_stars,
            stats=stats,
        )
    return stats


async def mine_awesome_lists(
    client: GitHubClient,
    *,
    lists: Iterable[str] = AWESOME_LISTS,
    stats: DiscoveryStats | None = None,
) -> set[str]:
    """Extract repository references from curated list READMEs.

    These lists are maintained by people who already decided what counts, which
    makes them the cheapest recall we can buy — one request per list.
    """
    stats = stats or DiscoveryStats()
    found: set[str] = set()
    for list_repo in lists:
        try:
            response = await client.get(f"/repos/{list_repo}/readme")
        except GitHubError as exc:
            log.warning("discovery: cannot read %s: %s", list_repo, exc)
            stats.errors += 1
            continue
        stats.queries += 1
        found |= extract_repo_links(_decode_readme(response.data))
    found -= set(lists)
    stats.note("awesome", len(found))
    return found


def _decode_readme(payload: dict | None) -> str:
    if not payload:
        return ""
    if payload.get("encoding") == "base64" and payload.get("content"):
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    return payload.get("content") or ""


def extract_repo_links(markdown: str) -> set[str]:
    """Pull `owner/name` pairs out of README prose, ignoring non-repo URLs."""
    found: set[str] = set()
    for owner, name in _REPO_LINK.findall(markdown or ""):
        if owner.lower() in _RESERVED_OWNERS:
            continue
        name = name.rstrip(".,);:'\"")
        # Strip the trailing path segment of deep links (/tree/main, .git, ...).
        if name.endswith(".git"):
            name = name[:-4]
        if not name or name in {".", ".."}:
            continue
        found.add(f"{owner}/{name}")
    return found


def snowball_topics(
    seen_topics: Iterable[str], *, already_queried: Iterable[str], limit: int = 100
) -> list[str]:
    """Topics observed on confirmed AI repos that are not yet in the vocabulary.

    This is what keeps the seed list from going stale: a topic that did not
    exist when the vocabulary was written (the next `mcp`) arrives here on its
    own, ranked by how often it co-occurs with known AI projects.
    """
    queried = {t.lower() for t in already_queried}
    counts: dict[str, int] = {}
    for topic in seen_topics:
        slug = topic.lower().strip()
        if not slug or slug in queried:
            continue
        counts[slug] = counts.get(slug, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [topic for topic, _ in ranked[:limit]]
