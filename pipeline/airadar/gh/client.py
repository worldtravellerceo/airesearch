"""A polite, rate-limit-aware GitHub REST client.

Three things matter here and nothing else does:

1. **Conditional requests.** A `304 Not Modified` does not count against the
   primary rate limit, so caching ETags is the single biggest lever we have.
2. **Rate-limit governance.** GitHub exposes separate buckets ("core", "search",
   ...). We read the response headers and pause *before* running a bucket dry,
   instead of burning retries on 403s.
3. **Honest counters.** Every run reports how many calls it actually spent so the
   cost estimates in the plan can be checked against reality.
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
    """Mirrors GitHub's view of one rate-limit resource."""

    remaining: int = 5000
    limit: int | None = None
    reset_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def floor(self) -> int:
        return rate_limit_floor(self.limit)


class GitHubClient:
    """Async GitHub REST client. Use as an async context manager."""

    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        settings = get_settings()
        self._token = token if token is not None else settings.github_token
        if not self._token:
            raise ValueError(
                "No GitHub token. Set GH_PAT — the Actions GITHUB_TOKEN is capped at "
                "1,000 req/hour per repository and is not usable for this workload."
            )
        self._sleep = sleep
        self.counters = Counters()
        self._buckets: dict[str, _Bucket] = {}
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_ROOT,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "Authorization": f"Bearer {self._token}",
                "User-Agent": settings.user_agent,
            },
            follow_redirects=True,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- rate limiting ------------------------------------------------------

    def _bucket(self, name: str) -> _Bucket:
        if name not in self._buckets:
            self._buckets[name] = _Bucket()
        return self._buckets[name]

    async def _await_capacity(self, resource: str) -> None:
        bucket = self._bucket(resource)
        async with bucket.lock:
            floor = bucket.floor
            if bucket.remaining > floor:
                return
            wait = bucket.reset_at - time.time()
            if wait <= 0:
                # Reset has passed; assume the bucket refilled and let the next
                # response tell us the truth.
                bucket.remaining = floor + 1
                return
            wait += 1.0
            log.info(
                "rate limit: %s bucket down to %d of %s, sleeping %.0fs",
                resource,
                bucket.remaining,
                bucket.limit or "?",
                wait,
            )
            self.counters.rate_limit_waits += 1
            self.counters.seconds_waiting += wait
            await self._sleep(wait)
            bucket.remaining = floor + 1

    def _record_limits(self, resource: str, headers: httpx.Headers) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        limit = headers.get("x-ratelimit-limit")
        if remaining is None:
            return
        bucket = self._bucket(resource)
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
            await self._await_capacity(resource)
            try:
                response = await self._client.get(path, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = min(60.0, 2**attempt) + random.uniform(0, 1)
                log.warning("transport error on %s (%s), retrying in %.1fs", path, exc, delay)
                self.counters.retries += 1
                await self._sleep(delay)
                continue

            self._record_limits(resource, response.headers)

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
