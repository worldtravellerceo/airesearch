"""Discovery orchestration against a real database."""

import base64
import datetime as dt
import os

import httpx
import psycopg
import pytest

from airadar.db import repo as db
from airadar.discover import discover, discovery_overview, resolve_pending
from airadar.gh.client import GitHubClient

TEST_DSN = os.environ.get(
    "AIRADAR_TEST_DSN", "postgresql://postgres@/airadar_test?host=/tmp&port=55432"
)
HEADERS = {
    "x-ratelimit-remaining": "4999",
    "x-ratelimit-reset": str(int(dt.datetime.now().timestamp()) + 3600),
}


def _reachable() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test PostgreSQL available")


@pytest.fixture
def conn():
    with db.connect(TEST_DSN) as connection:
        connection.execute(
            "DROP TABLE IF EXISTS leaderboard_snapshots, repo_scores, "
            "repo_classification, repo_star_daily, repo_snapshots, repo_topics, "
            "queried_topics, pending_repos, run_log, repos CASCADE"
        )
        connection.commit()
        db.apply_schema(connection)
        yield connection


async def _no_sleep(_seconds):
    return None


def repo_json(repo_id, full_name, stars=500, fork=False, topics=("llm",)):
    owner, name = full_name.split("/")
    return {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "created_at": "2024-01-01T00:00:00Z",
        "description": "a thing",
        "homepage": "",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "archived": False,
        "fork": fork,
        "stargazers_count": stars,
        "topics": list(topics),
    }


async def test_topic_sweep_persists_repos_and_records_the_topic(conn):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/repositories":
            return httpx.Response(
                200,
                json={"total_count": 1, "items": [repo_json(1, "acme/agent")]},
                headers=HEADERS,
            )
        return httpx.Response(404, json={}, headers=HEADERS)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        report = await discover(
            conn,
            client,
            keywords=False,
            awesome=False,
            ecosystems=False,
            huggingface=False,
            snowball=False,
            resolve=False,
        )

    assert report.repos_upserted > 0
    assert conn.execute("SELECT count(*) AS n FROM repos").fetchone()["n"] == 1
    assert "llm" in db.queried_topics(conn)


async def test_search_results_below_the_star_floor_are_not_tracked(conn):
    """The floor exists to keep the universe and the daily quota finite."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    repo_json(1, "acme/tiny", stars=3),
                    repo_json(2, "acme/real", stars=500),
                    repo_json(3, "acme/forked", stars=900, fork=True),
                ],
            },
            headers=HEADERS,
        )

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        await discover(
            conn,
            client,
            keywords=False,
            awesome=False,
            ecosystems=False,
            huggingface=False,
            snowball=False,
            resolve=False,
        )

    names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
    assert names == {"acme/real"}


async def test_awesome_list_names_queue_as_pending_then_resolve(conn):
    readme = "[a](https://github.com/acme/agent) [b](https://github.com/acme/gone)"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/readme"):
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(readme.encode()).decode()},
                headers=HEADERS,
            )
        if path == "/repos/acme/agent":
            return httpx.Response(200, json=repo_json(11, "acme/agent"), headers=HEADERS)
        if path == "/repos/acme/gone":
            return httpx.Response(404, json={"message": "Not Found"}, headers=HEADERS)
        return httpx.Response(200, json={"total_count": 0, "items": []}, headers=HEADERS)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        report = await discover(
            conn,
            client,
            topics=False,
            keywords=False,
            ecosystems=False,
            huggingface=False,
            snowball=False,
            awesome=True,
        )

    assert report.pending_queued == 2
    assert report.pending_resolved == 1
    assert report.pending_failed == 1

    row = conn.execute("SELECT full_name, discovered_via FROM repos").fetchone()
    assert row["full_name"] == "acme/agent"
    assert row["discovered_via"] == "awesome"

    gone = conn.execute(
        "SELECT failed, note FROM pending_repos WHERE full_name = 'acme/gone'"
    ).fetchone()
    assert gone["failed"] is True
    assert "gone" in gone["note"]


async def test_a_resolved_pending_repo_is_never_looked_up_twice(conn):
    """Quota spent rediscovering the same too-small repo every week is quota
    not spent on the repos we actually track."""
    db.add_pending(conn, ["acme/tiny"], source="ecosystems")
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lookups.append(request.url.path)
        return httpx.Response(200, json=repo_json(5, "acme/tiny", stars=2), headers=HEADERS)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        await resolve_pending(conn, client)
        await resolve_pending(conn, client)

    assert lookups == ["/repos/acme/tiny"]  # below the floor, but settled
    assert conn.execute("SELECT count(*) AS n FROM repos").fetchone()["n"] == 0


async def test_snowball_queries_topics_learned_from_confirmed_ai_repos(conn):
    db.upsert_repos(
        conn,
        [db.RepoRecord.from_api(repo_json(1, "acme/agent", topics=("llm", "brand-new-topic")))],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="agent-framework",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    db.record_topic_query(conn, "llm", source="seed", repos_found=1)
    conn.commit()

    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params.get("q", ""))
        return httpx.Response(200, json={"total_count": 0, "items": []}, headers=HEADERS)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        report = await discover(
            conn,
            client,
            topics=False,
            keywords=False,
            awesome=False,
            ecosystems=False,
            huggingface=False,
            snowball=True,
            resolve=False,
        )

    assert report.snowballed_topics == ["brand-new-topic"]
    assert any("topic:brand-new-topic" in q for q in queries)
    # And it is recorded, so next week's run does not sweep it again.
    assert "brand-new-topic" in db.queried_topics(conn)


async def test_discovery_overview_reports_provenance(conn):
    db.upsert_repos(
        conn,
        [
            db.RepoRecord.from_api(repo_json(1, "a/one"), discovered_via="topic"),
            db.RepoRecord.from_api(repo_json(2, "b/two"), discovered_via="awesome"),
        ],
    )
    db.add_pending(conn, ["c/three"], source="huggingface")

    overview = discovery_overview(conn)

    assert overview["total"] == 2
    assert overview["by_channel"] == {"topic": 1, "awesome": 1}
    assert overview["pending"] == 1
