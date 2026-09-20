"""Discovery tests.

The thing worth protecting here is enumeration: the search API silently
truncates at 1,000 results, so a partitioning bug does not raise — it just
quietly returns a fraction of the population and every downstream ranking is
built on a biased sample.
"""

import datetime as dt

import httpx
import pytest

from airadar.gh.client import GitHubClient
from airadar.gh.discovery import (
    SEARCH_RESULT_CAP,
    DiscoveryStats,
    _geometric_midpoint,
    discover_by_topics,
    extract_repo_links,
    mine_awesome_lists,
    search_partitioned,
    snowball_topics,
)

HEADERS = {
    "x-ratelimit-remaining": "999",
    "x-ratelimit-reset": str(int(dt.datetime.now().timestamp()) + 3600),
}


async def _no_sleep(_seconds):  # pragma: no cover
    raise AssertionError("no throttling expected")


class FakeGitHub:
    """A synthetic repo population that enforces the real API's 1,000 cap.

    It honours `stars:` and `created:` qualifiers, so a partitioning bug shows
    up as missing repos rather than as an assertion about query strings.
    """

    def __init__(self, repos: dict[str, tuple[int, dt.date]]):
        self.repos = repos
        self.queries: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = params.get("q", "")
        self.queries.append(query)
        page = int(params.get("page", 1))

        matches = sorted(
            (
                name
                for name, (stars, created) in self.repos.items()
                if self._matches(query, stars, created)
            ),
            key=lambda n: -self.repos[n][0],
        )
        total = len(matches)
        # The API only ever serves the first 1,000 results, however many match.
        servable = matches[:SEARCH_RESULT_CAP]
        start = (page - 1) * 100
        items = [
            {
                "id": abs(hash(n)) % 10**9,
                "full_name": n,
                "stargazers_count": self.repos[n][0],
            }
            for n in servable[start : start + 100]
        ]
        return httpx.Response(
            200,
            json={"total_count": total, "incomplete_results": False, "items": items},
            headers=HEADERS,
        )

    @staticmethod
    def _matches(query: str, stars: int, created: dt.date) -> bool:
        for token in query.split():
            if token.startswith("stars:"):
                low, high = token.removeprefix("stars:").split("..")
                if not (int(low) <= stars <= int(high)):
                    return False
            elif token.startswith("created:"):
                low, high = token.removeprefix("created:").split("..")
                if not (dt.date.fromisoformat(low) <= created <= dt.date.fromisoformat(high)):
                    return False
        return True


def power_law_population(count: int, *, max_stars: int = 400_000) -> dict:
    """`count` repos spread geometrically from 50 stars up to `max_stars`."""
    growth = (max_stars / 50) ** (1 / max(count - 1, 1))
    return {
        f"o/r{i}": (int(50 * growth**i), dt.date(2015, 1, 1) + dt.timedelta(days=i))
        for i in range(count)
    }


def test_geometric_midpoint_splits_a_power_law_range_usefully():
    """An arithmetic midpoint of 50..1,000,000 is 500,025 — which leaves
    essentially the entire population in the lower half."""
    assert _geometric_midpoint(50, 1_000_000) == pytest.approx(7071, rel=0.01)
    assert _geometric_midpoint(50, 100) == 70
    assert _geometric_midpoint(50, 51) == 50  # always makes progress
    assert _geometric_midpoint(50, 50) == 50
    assert _geometric_midpoint(1, 4) == 2


async def test_partitioning_enumerates_a_population_larger_than_the_cap():
    # 2,500 repos across a realistic star range. A single query could only ever
    # return 1,000 of them.
    population = power_law_population(2_500)
    fake = FakeGitHub(population)
    seen: set[str] = set()

    async def sink(items, channel):
        seen.update(item["full_name"] for item in items)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        stats = await search_partitioned(
            client, "topic:llm fork:false", channel="topic", sink=sink, min_stars=50
        )

    assert len(seen) == len(population)  # nothing was silently dropped
    assert stats.splits > 0
    assert stats.capped_queries == 0


async def test_the_biggest_repositories_are_not_lost_off_the_top_of_the_range():
    """A finite upper bound on the initial star range means anything above it is
    never queried, and nothing would report the omission."""
    population = power_law_population(1_200, max_stars=900_000)
    fake = FakeGitHub(population)
    seen: set[str] = set()

    async def sink(items, channel):
        seen.update(i["full_name"] for i in items)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        await search_partitioned(
            client, "topic:ai fork:false", channel="topic", sink=sink, min_stars=50
        )

    biggest = max(population, key=lambda n: population[n][0])
    assert biggest in seen
    assert len(seen) == len(population)


async def test_no_split_needed_for_a_small_population():
    fake = FakeGitHub({f"o/r{i}": (100 + i, dt.date(2020, 1, 1)) for i in range(150)})
    seen: set[str] = set()

    async def sink(items, channel):
        seen.update(i["full_name"] for i in items)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        stats = await search_partitioned(
            client, "topic:mcp fork:false", channel="topic", sink=sink, min_stars=50
        )

    assert len(seen) == 150
    assert stats.splits == 0
    assert stats.pages == 2  # 150 results = two pages, no wasted requests


async def test_falls_back_to_date_slicing_when_stars_cannot_separate():
    """1,500 repos all sitting on exactly 50 stars cannot be split by stars, so
    the partitioner must cut on another dimension — and must still enumerate
    every one of them, not just the first 1,000."""
    population = {
        f"o/r{i}": (50, dt.date(2018, 1, 1) + dt.timedelta(days=i * 2)) for i in range(1_500)
    }
    fake = FakeGitHub(population)
    seen: set[str] = set()

    async def sink(items, channel):
        seen.update(i["full_name"] for i in items)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        await search_partitioned(
            client, "topic:llm fork:false", channel="topic", sink=sink, min_stars=50
        )

    assert any("created:" in q for q in fake.queries)
    assert len(seen) == len(population)


async def test_topic_channel_queries_every_topic():
    fake = FakeGitHub({"o/only": (500, dt.date(2020, 1, 1))})

    async def sink(items, channel):
        assert channel == "topic"

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        await discover_by_topics(client, sink=sink, min_stars=50, topics=["llm", "rag", "mcp"])

    assert sum("topic:llm " in q for q in fake.queries) >= 1
    assert sum("topic:rag " in q for q in fake.queries) >= 1
    assert sum("topic:mcp " in q for q in fake.queries) >= 1


# --- awesome-list mining ---------------------------------------------------


def test_extract_repo_links_ignores_non_repository_urls():
    markdown = """
    - [LangChain](https://github.com/langchain-ai/langchain) - framework
    - [vLLM](https://github.com/vllm-project/vllm/tree/main/docs) - serving
    - [Sponsor](https://github.com/sponsors/someone)
    - [Topic](https://github.com/topics/llm)
    - [Docs](https://example.com/not-github)
    - clone: https://github.com/acme/thing.git
    - trailing punctuation: https://github.com/acme/other.
    """

    found = extract_repo_links(markdown)

    assert "langchain-ai/langchain" in found
    assert "vllm-project/vllm" in found
    assert "acme/thing" in found
    assert "acme/other" in found
    assert not any(r.startswith("sponsors/") for r in found)
    assert not any(r.startswith("topics/") for r in found)


def test_extract_repo_links_on_empty_input():
    assert extract_repo_links("") == set()
    assert extract_repo_links(None) == set()


async def test_mine_awesome_lists_decodes_base64_readmes():
    import base64

    readme = "See [x](https://github.com/acme/agent) and [y](https://github.com/acme/rag)"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "encoding": "base64",
                "content": base64.b64encode(readme.encode()).decode(),
            },
            headers=HEADERS,
        )

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        found = await mine_awesome_lists(client, lists=["someone/awesome-ai"])

    assert found == {"acme/agent", "acme/rag"}


async def test_mine_awesome_lists_survives_a_missing_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"}, headers=HEADERS)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        assert await mine_awesome_lists(client, lists=["gone/list"]) == set()


# --- snowballing -----------------------------------------------------------


def test_snowball_ranks_new_topics_by_how_often_they_co_occur():
    seen = ["mcp", "mcp", "mcp", "llm", "webdev", "webdev", "agents"]

    fresh = snowball_topics(seen, already_queried=["llm", "agents"])

    assert fresh == ["mcp", "webdev"]  # already-queried topics excluded, sorted by count


def test_snowball_respects_the_limit_and_normalises_case():
    seen = ["MCP", "mcp", "RAG"]
    assert snowball_topics(seen, already_queried=[], limit=1) == ["mcp"]


async def test_a_search_budget_stops_a_sweep_cleanly():
    """A full topic sweep is thousands of requests at 30 a minute — more than
    one CI job holds. The run has to be able to stop and be resumed."""
    population = power_law_population(3_000)
    fake = FakeGitHub(population)
    stats = DiscoveryStats(budget=12)

    async def sink(items, channel):
        pass

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        await search_partitioned(
            client,
            "topic:llm fork:false",
            channel="topic",
            sink=sink,
            min_stars=50,
            stats=stats,
        )

    assert stats.exhausted is True
    # The budget counts pages, because every page is one request against the
    # 30-per-minute search limit. It is checked between requests rather than
    # enforced mid-flight, so a single-page overshoot is expected.
    assert 12 <= stats.pages <= 14
    assert stats.repos_seen < len(population)  # genuinely stopped early


async def test_no_budget_means_no_limit():
    fake = FakeGitHub(power_law_population(150))
    stats = DiscoveryStats()

    async def sink(items, channel):
        pass

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(fake.handler), sleep=_no_sleep
    ) as client:
        await search_partitioned(
            client,
            "topic:llm fork:false",
            channel="topic",
            sink=sink,
            min_stars=50,
            stats=stats,
        )

    assert stats.exhausted is False


def test_snowball_is_empty_until_something_has_been_classified():
    """The ordering bug this pins, found in the field.

    `discover.yml` ran discovery before classification, and snowball reads the
    classification table to find topics the seed vocabulary never had. On the
    first run that table was empty, so snowball returned nothing — no error, no
    warning — and the entire Agent Skills ecosystem went unswept. `claude-code`
    alone had 3,199 repos in the corpus, every one of them found incidentally
    through some other topic.

    Snowball needs a previous generation to learn from. That is inherent; what
    is not inherent is running it when no generation exists yet.
    """
    assert snowball_topics([], already_queried=["llm"]) == []

    fed = snowball_topics(
        ["claude-code", "claude-code", "agent-skills", "llm"],
        already_queried=["llm"],
    )
    assert fed[0] == "claude-code"  # ranked by how often it co-occurs with AI repos
    assert "agent-skills" in fed
    assert "llm" not in fed  # already swept
