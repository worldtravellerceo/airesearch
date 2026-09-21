"""A polite, rate-limit-aware GitHub REST client.

Three things matter here and nothing else does:

1. **Conditional requests.** A `304 Not Modified` does not count against the
   primary rate limit, so caching ETags is the single biggest lever we have.
2. **Rate-limit governance.** GitHub exposes separate buckets ("core", "search",
   ...). We read the response headers and pause *before* running a bucket dry,
   instead of burning retries on 403s.
3. **Honest counters.** Every run reports how many calls it actually spent so the
   cost estimates in the plan can be checked against reality.
4. **Throughput.** Requests are not serialised — several are in flight at once,
   because a sequential client is bounded by the round trip rather than by
   quota. Several tokens can be driven at once too, each in its own lane with
   its own buckets, but note what GitHub actually says about that: "All of
   these requests count towards your personal rate limit of 5,000 requests per
   hour." The limit belongs to the **account**, not the token, so a second
   token from the same account raises nothing. Lanes only multiply the ceiling
   when the tokens belong to different accounts. `check_lanes` measures which
   of the two is true rather than assuming.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from airadar.config import GITHUB_API_ROOT, GITHUB_API_VERSION, get_settings

log = logging.getLogger(__name__)

# We stop and wait for the reset once a bucket drops below a reserve. The
# reserve has to scale with the bucket, not be a constant: "leave 25 in hand" is
# prudent against the 5,000-per-hour core bucket and ruinous against the
# 30-per-minute search bucket, where it would cap us at five requests a minute
# and make a discovery sweep six times longer than it needs to be.
RATE_LIMIT_RESERVE_FRACTION = 0.1
MAX_RATE_LIMIT_FLOOR = 25
MIN_RATE_LIMIT_FLOOR = 2
MAX_ATTEMPTS = 5


def rate_limit_floor(limit: int | None) -> int:
    """How many requests to keep in reserve for a bucket of this size."""
    if not limit or limit <= 0:
        return MAX_RATE_LIMIT_FLOOR
    scaled = int(limit * RATE_LIMIT_RESERVE_FRACTION)
    return max(MIN_RATE_LIMIT_FLOOR, min(MAX_RATE_LIMIT_FLOOR, scaled))


class GitHubError(RuntimeError):
    """A non-retryable GitHub API failure."""

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"GitHub {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


@dataclass
class Response:
    """What callers actually need: the payload, the ETag, and whether it changed."""

    status: int
    data: Any
    etag: str | None
    not_modified: bool
    link: str | None = None

    @property
    def has_next_page(self) -> bool:
        return bool(self.link and 'rel="next"' in self.link)


@dataclass
class Counters:
    calls: int = 0
    not_modified: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    seconds_waiting: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "calls": self.calls,
            "not_modified": self.not_modified,
            "retries": self.retries,
            "rate_limit_waits": self.rate_limit_waits,
            "seconds_waiting": round(self.seconds_waiting, 1),
        }


@dataclass
class _Bucket:
    """Mirrors GitHub's view of one rate-limit resource, for one token."""

    remaining: int = 5000
    limit: int | None = None
    reset_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def floor(self) -> int:
        return rate_limit_floor(self.limit)

    @property
    def spare(self) -> int:
        """Requests available above the reserve, treating a passed reset as refilled."""
        if self.remaining > self.floor:
            return self.remaining - self.floor
        if self.reset_at <= time.time():
            return self.limit or 5000
        return 0


class _Lane:
    """One token: its own HTTP client, its own rate-limit buckets.

    Buckets belong to the token rather than the process. Sharing one set across
    tokens would mean the client believed it had spent quota it had not, and
    would idle waiting for a reset that had already happened on the other lane.
    """

    def __init__(self, name: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self.client = client
        self.issued = 0
        self.buckets: dict[str, _Bucket] = {}

    def bucket(self, resource: str) -> _Bucket:
        if resource not in self.buckets:
            self.buckets[resource] = _Bucket()
        return self.buckets[resource]


class GitHubClient:
    """Async GitHub REST client. Use as an async context manager."""

    def __init__(
        self,
        token: str | None = None,
        *,
        tokens: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        settings = get_settings()
        if tokens is None:
            tokens = [token] if token is not None else settings.github_tokens
        tokens = [t for t in tokens if t]
        if not tokens:
            raise ValueError(
                "No GitHub token. Set GH_PAT — the Actions GITHUB_TOKEN is capped at "
                "1,000 req/hour per repository and is not usable for this workload."
            )
        self._sleep = sleep
        self.counters = Counters()
        self._lanes = [
            _Lane(
                # The name is an ordinal, never the token: this string reaches
                # the log, and the log reaches a public Actions run.
                name=f"token-{index + 1}",
                client=httpx.AsyncClient(
                    base_url=GITHUB_API_ROOT,
                    transport=transport,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": GITHUB_API_VERSION,
                        "Authorization": f"Bearer {value}",
                        "User-Agent": settings.user_agent,
                    },
                    follow_redirects=True,
                ),
            )
            for index, value in enumerate(tokens)
        ]

    @property
    def lanes(self) -> int:
        """How many tokens this client is driving."""
        return len(self._lanes)

    async def check_lanes(self) -> list[dict[str, Any]]:
        """Read every lane's quota, and find out whether the lanes are real.

        `/rate_limit` is free and does not count against anything, so reading
        each lane costs nothing. What is not free is believing the sum of them.
        GitHub applies the primary limit to the **account**, not the token, so
        three tokens belonging to one user are three views of one allowance —
        and a client that added them up would think it had 45,000 requests an
        hour when it had 15,000, then wonder why the run took three times as
        long as predicted. `shared_quota` below is that question, measured.
        """
        report = []
        for lane in list(self._lanes):
            response = await lane.client.get("/rate_limit")
            if response.status_code == 401:
                log.error("%s was rejected (401) and is being dropped", lane.name)
                self._lanes.remove(lane)
                await lane.client.aclose()
                report.append({"lane": lane.name, "ok": False})
                continue
            if response.status_code >= 300:
                # Anything else is not evidence the token is bad. Saying so
                # would fail a run over a blip on a call that is only here to
                # print a number.
                log.warning(
                    "%s: /rate_limit returned %d, assuming the lane is fine",
                    lane.name,
                    response.status_code,
                )
                report.append({"lane": lane.name, "ok": True, "unknown": True})
                continue
            resources = response.json()["resources"]
            core, search = resources["core"], resources["search"]
            lane.bucket("core").remaining = core["remaining"]
            lane.bucket("core").limit = core["limit"]
            lane.bucket("core").reset_at = float(core["reset"])
            lane.bucket("search").remaining = search["remaining"]
            lane.bucket("search").limit = search["limit"]
            lane.bucket("search").reset_at = float(search["reset"])
            report.append(
                {
                    "lane": lane.name,
                    "ok": True,
                    "core": core["remaining"],
                    "core_limit": core["limit"],
                    "search": search["remaining"],
                    "search_limit": search["limit"],
                }
            )
        if not self._lanes:
            raise GitHubError(401, "/rate_limit", "every configured token was rejected")
        if len(self._lanes) > 1:
            shared = await self._lanes_share_a_bucket()
            for entry in report:
                entry["shared_quota"] = shared
        return report

    async def _lanes_share_a_bucket(self) -> bool:
        """Spend one request on the first lane and see whether the second paid.

        Two tokens from one account share a bucket; two from different accounts
        do not. Nothing in a token says which, and the difference is the whole
        value of having more than one — so it is measured, once, at the start
        of a run, for the price of a single request.
        """
        first, second = self._lanes[0], self._lanes[1]
        before = await self._core_remaining(second)
        if before is None:
            return False
        # `/user` is the cheapest endpoint that actually counts.
        await first.client.get("/user")
        after = await self._core_remaining(second)
        if after is None:
            return False
        shared = after < before
        log.info(
            "rate limit: the lanes %s a bucket (%s -> %s on the second while the first spent one)",
            "share" if shared else "do not share",
            before,
            after,
        )
        return shared

    async def _core_remaining(self, lane: _Lane) -> int | None:
        response = await lane.client.get("/rate_limit")
        if response.status_code >= 300:
            return None
        return int(response.json()["resources"]["core"]["remaining"])

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for lane in self._lanes:
            await lane.client.aclose()

    # -- rate limiting ------------------------------------------------------

    async def _claim_lane(self, resource: str) -> _Lane:
        """Pick the lane with the most quota left for `resource`, waiting if none has any.

        With one token this is the old behaviour exactly: the single lane is
        always the best one, and running it dry means sleeping to its reset.
        With three, a lane that has run out costs nothing — the work moves to
        another token instead of the run stopping for up to an hour.
        """
        while True:
            # Most quota first, and between lanes that look alike, whichever has
            # been asked to do least. The tie-break is not cosmetic: fresh lanes
            # report identical counters, so without it every request goes down
            # lane one until its allowance visibly falls behind — and the
            # secondary rate limit, which is also per token, would see a single
            # token carrying the whole run.
            best = max(self._lanes, key=lambda lane: (lane.bucket(resource).spare, -lane.issued))
            if best.bucket(resource).spare > 0:
                # Spend the request against the lane now rather than when its
                # response lands, so a burst of requests issued back to back
                # cannot all read the same untouched counter.
                best.bucket(resource).remaining -= 1
                best.issued += 1
                return best

            soonest = min(self._lanes, key=lambda lane: lane.bucket(resource).reset_at)
            bucket = soonest.bucket(resource)
            async with bucket.lock:
                wait = bucket.reset_at - time.time()
                if wait <= 0:
                    # Reset has passed; assume it refilled and let the next
                    # response tell us the truth.
                    bucket.remaining = bucket.floor + 1
                    return soonest
                wait += 1.0
                log.info(
                    "rate limit: every lane is out of %s quota, sleeping %.0fs for %s",
                    resource,
                    wait,
                    soonest.name,
                )
                self.counters.rate_limit_waits += 1
                self.counters.seconds_waiting += wait
                await self._sleep(wait)
                bucket.remaining = bucket.floor + 1
                return soonest

    def _record_limits(self, lane: _Lane, resource: str, headers: httpx.Headers) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        limit = headers.get("x-ratelimit-limit")
        if remaining is None:
            return
        bucket = lane.bucket(resource)
        try:
            bucket.remaining = int(remaining)
            if reset is not None:
                bucket.reset_at = float(reset)
            if limit is not None:
                bucket.limit = int(limit)
        except ValueError:
            pass

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Honour Retry-After / rate-limit reset, else exponential backoff."""
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset")
            if reset:
                try:
                    return max(1.0, float(reset) - time.time() + 1.0)
                except ValueError:
                    pass
        return min(60.0, (2**attempt)) + random.uniform(0, 1)

    # -- requests -----------------------------------------------------------

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        etag: str | None = None,
        resource: str = "core",
    ) -> Response:
        """GET `path`, transparently retrying throttles and 5xx.

        Pass `etag` from a previous call to make the request conditional: an
        unchanged resource comes back as `not_modified` and costs no quota.
        """
        headers = {"If-None-Match": etag} if etag else None

        for attempt in range(MAX_ATTEMPTS):
            lane = await self._claim_lane(resource)
            try:
                response = await lane.client.get(path, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = min(60.0, 2**attempt) + random.uniform(0, 1)
                log.warning("transport error on %s (%s), retrying in %.1fs", path, exc, delay)
                self.counters.retries += 1
                await self._sleep(delay)
                continue

            self._record_limits(lane, resource, response.headers)

            if response.status_code == 304:
                self.counters.not_modified += 1
                return Response(304, None, etag, True, response.headers.get("link"))

            self.counters.calls += 1

            if response.status_code < 300:
                return Response(
                    response.status_code,
                    response.json() if response.content else None,
                    response.headers.get("etag"),
                    False,
                    response.headers.get("link"),
                )

            if response.status_code == 401 and len(self._lanes) > 1:
                # A revoked or expired extra token would otherwise fail a third
                # of every run's repos, quietly and forever. Drop the lane and
                # carry on with the tokens that work.
                log.error(
                    "%s was rejected (401) and is being dropped; %d lane(s) left",
                    lane.name,
                    len(self._lanes) - 1,
                )
                self._lanes.remove(lane)
                await lane.client.aclose()
                continue

            if self._is_throttled(response) or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GitHubError(response.status_code, path, response.text)
                delay = self._retry_delay(response, attempt)
                log.warning(
                    "throttled/5xx %s on %s, sleeping %.0fs",
                    response.status_code,
                    path,
                    delay,
                )
                self.counters.retries += 1
                self.counters.seconds_waiting += delay
                await self._sleep(delay)
                continue

            raise GitHubError(response.status_code, path, response.text)

        raise GitHubError(0, path, "exhausted retries")

    @staticmethod
    def _is_throttled(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        # 403 is overloaded: it means "forbidden" *and* "secondary rate limit".
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        if response.headers.get("retry-after"):
            return True
        return "secondary rate limit" in response.text.lower()

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str = "core",
        max_pages: int = 100,
    ):
        """Yield successive pages, stopping at `max_pages` or the last Link page."""
        page = 1
        while page <= max_pages:
            page_params = dict(params or {})
            page_params["page"] = page
            response = await self.get(path, params=page_params, resource=resource)
            yield response
            if not response.has_next_page:
                return
            page += 1

    async def search_repositories(
        self,
        query: str,
        *,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 100,
        page: int = 1,
    ) -> Response:
        """Repository search. Separate 30 req/min bucket, 1,000 results per query."""
        return await self.get(
            "/search/repositories",
            params={
                "q": query,
                "sort": sort,
                "order": order,
                "per_page": per_page,
                "page": page,
            },
            resource="search",
        )
