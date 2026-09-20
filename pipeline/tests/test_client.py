import time

import httpx
import pytest

from airadar.gh.client import GitHubClient, GitHubError, rate_limit_floor


def _headers(
    remaining: int = 4999,
    reset: float | None = None,
    limit: int = 5000,
    **extra: str,
) -> dict[str, str]:
    headers = {
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-reset": str(int(reset if reset is not None else time.time() + 3600)),
    }
    headers.update(extra)
    return headers


async def test_etag_304_does_not_count_as_a_call(recorded_sleeps):
    """304s are free on GitHub's side, so they must not inflate our call counter."""
    _, fake_sleep = recorded_sleeps

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == 'W/"abc"'
        return httpx.Response(304, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        response = await client.get("/repos/a/b", etag='W/"abc"')

    assert response.not_modified is True
    assert response.etag == 'W/"abc"'
    assert client.counters.calls == 0
    assert client.counters.not_modified == 1


async def test_api_version_header_is_sent(recorded_sleeps):
    """stargazers/history only exists under the 2026-03-10 API version."""
    _, fake_sleep = recorded_sleeps
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True}, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        await client.get("/repos/a/b/stargazers/history")

    assert seen["x-github-api-version"] == "2026-03-10"
    assert seen["authorization"] == "Bearer t"


async def test_secondary_rate_limit_is_retried_with_retry_after(recorded_sleeps):
    """A 403 carrying Retry-After is a throttle, not a permission error."""
    sleeps, fake_sleep = recorded_sleeps
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                403,
                text="You have exceeded a secondary rate limit",
                headers=_headers(**{"retry-after": "7"}),
            )
        return httpx.Response(200, json={"ok": True}, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        response = await client.get("/search/repositories", resource="search")

    assert response.data == {"ok": True}
    assert sleeps == [7.0]
    assert client.counters.retries == 1


async def test_real_403_is_not_retried(recorded_sleeps):
    """A genuine permission failure must surface immediately, not burn 5 retries."""
    sleeps, fake_sleep = recorded_sleeps

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Must have admin rights", headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        with pytest.raises(GitHubError) as excinfo:
            await client.get("/repos/a/b/stargazers")

    assert excinfo.value.status == 403
    assert sleeps == []


def test_the_reserve_scales_with_the_bucket():
    """A fixed reserve is wrong at one end or the other. Keeping 25 requests in
    hand is prudent against the 5,000-per-hour core bucket and ruinous against
    the 30-per-minute search bucket — it would cap a sweep at five requests a
    minute."""
    assert rate_limit_floor(5000) == 25
    assert rate_limit_floor(30) == 3
    assert rate_limit_floor(10) == 2  # never so small that a 403 is likely
    assert rate_limit_floor(None) == 25  # unknown bucket: be careful


async def test_search_sweeps_are_not_throttled_by_a_core_sized_reserve(recorded_sleeps):
    """Regression: with a fixed reserve of 25, five requests drained the
    30-per-minute search bucket below the floor and every sixth request waited a
    full minute."""
    sleeps, fake_sleep = recorded_sleeps

    def handler(request: httpx.Request) -> httpx.Response:
        # A realistic search bucket: 30 per minute, counting down.
        handler.remaining -= 1
        return httpx.Response(
            200,
            json={"items": []},
            headers=_headers(remaining=handler.remaining, limit=30, reset=time.time() + 60),
        )

    handler.remaining = 30

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        for _ in range(25):
            await client.search_repositories("topic:llm", page=1)

    assert sleeps == [], "25 of 30 search requests should not need to wait"


async def test_governor_waits_before_draining_a_bucket(recorded_sleeps):
    """When a bucket is nearly empty we pause proactively rather than eat a 403."""
    sleeps, fake_sleep = recorded_sleeps
    reset_at = time.time() + 120

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers=_headers(remaining=rate_limit_floor(5000) - 1, reset=reset_at),
        )

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        await client.get("/repos/a/b")  # drains the bucket
        await client.get("/repos/c/d")  # must wait first

    assert len(sleeps) == 1
    assert 110 < sleeps[0] < 125
    assert client.counters.rate_limit_waits == 1


async def test_search_and_core_buckets_are_independent(recorded_sleeps):
    """Exhausting search must not stall core requests (they are separate quotas)."""
    sleeps, fake_sleep = recorded_sleeps

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search"):
            return httpx.Response(
                200,
                json={"items": []},
                headers=_headers(remaining=0, limit=30, reset=time.time() + 60),
            )
        return httpx.Response(200, json={"ok": True}, headers=_headers(remaining=4000))

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        await client.search_repositories("topic:llm")
        await client.get("/repos/a/b")

    assert sleeps == []  # core was never blocked by the drained search bucket


async def test_paginate_follows_link_header(recorded_sleeps):
    _, fake_sleep = recorded_sleeps
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        link = '<https://api.github.com/x?page=2>; rel="next"' if pages["n"] == 1 else None
        headers = _headers()
        if link:
            headers["link"] = link
        return httpx.Response(200, json=[pages["n"]], headers=headers)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=fake_sleep
    ) as client:
        collected = [r.data async for r in client.paginate("/x")]

    assert collected == [[1], [2]]


async def test_missing_token_fails_loudly():
    with pytest.raises(ValueError, match="GH_PAT"):
        GitHubClient(token="")
