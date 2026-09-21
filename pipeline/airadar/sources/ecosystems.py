"""Dependency-graph discovery via ecosyste.ms.

GitHub search cannot answer "which repositories import torch". ecosyste.ms can:
it indexes package metadata and dependency graphs across registries, for free,
at 5,000 requests an hour.

This channel finds the projects that are unmistakably AI by what they build on
rather than by what they call themselves — the ones with no topics, a terse
README, and a `requirements.txt` full of transformers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from airadar.gh.vocabulary import BELLWETHER_PACKAGES

log = logging.getLogger(__name__)

API_ROOT = "https://packages.ecosyste.ms/api/v1"
REGISTRY_FOR_ECOSYSTEM = {"pypi": "pypi.org", "npm": "npmjs.org"}
PAGE_SIZE = 100
# Courtesy pause between requests. The documented limit is 5,000/hour; this
# keeps us an order of magnitude under it on a free community service.
REQUEST_PAUSE_SECONDS = 0.2

_GITHUB_REPO = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.IGNORECASE
)


@dataclass
class EcosystemsStats:
    requests: int = 0
    packages_seen: int = 0
    repos_found: int = 0
    errors: int = 0
    per_package: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.repos_found} repos from {self.packages_seen} dependent packages "
            f"({self.requests} requests, {self.errors} errors)"
        )


def repo_from_url(url: str | None) -> str | None:
    """Normalise a repository URL to `owner/name`, or None if it is not GitHub."""
    if not url:
        return None
    match = _GITHUB_REPO.match(url.strip())
    if not match:
        return None
    owner, name = match.group(1), match.group(2)
    if not owner or not name:
        return None
    return f"{owner}/{name}"


class EcosystemsClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "airadar/0.1",
        sleep=asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self.stats = EcosystemsStats()
        self._client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            # A contact address puts us in the "polite pool", which gets more
            # consistent response times than anonymous traffic.
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def __aenter__(self) -> EcosystemsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None):
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            log.warning("ecosystems: %s failed: %s", path, exc)
            self.stats.errors += 1
            return None
        self.stats.requests += 1
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            log.warning("ecosystems: %s -> %s", path, response.status_code)
            self.stats.errors += 1
            return None
        await self._sleep(REQUEST_PAUSE_SECONDS)
        return response.json()

    async def dependent_repos(
        self, ecosystem: str, package: str, *, max_pages: int = 10
    ) -> set[str]:
        """GitHub repos of packages that depend on `package`.

        The caller gets a bare set, which is all discovery needs. Which package
        produced each sighting is the interesting part for classification, and
        `discover_via_dependencies` keeps it.
        """
        registry = REGISTRY_FOR_ECOSYSTEM.get(ecosystem)
        if registry is None:
            raise ValueError(f"unknown ecosystem {ecosystem!r}")

        found: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = await self._get(
                f"/registries/{registry}/packages/{package}/dependent_packages",
                {"per_page": PAGE_SIZE, "page": page},
            )
            if not payload:
                break
            self.stats.packages_seen += len(payload)
            for entry in payload:
                repo = repo_from_url(entry.get("repository_url"))
                if repo:
                    found.add(repo)
            if len(payload) < PAGE_SIZE:
                break

        self.stats.per_package[f"{ecosystem}:{package}"] = len(found)
        self.stats.repos_found += len(found)
        return found


async def discover_via_dependencies(
    *,
    packages: dict[str, tuple[str, ...]] | None = None,
    max_pages: int = 10,
    client: EcosystemsClient | None = None,
) -> tuple[dict[str, set[tuple[str, str]]], EcosystemsStats]:
    """Repos depending on any bellwether AI package, and which ones.

    Returns `{repo_full_name: {(ecosystem, package), ...}}` rather than a bare
    set. The package is the whole value of this channel for classification — a
    repository that imports `torch` is a machine-learning project whatever its
    description says — and it used to be discarded here, with all 29 bellwethers
    collapsing into the single string "ecosystems" as the discovery source.
    """
    packages = packages or BELLWETHER_PACKAGES
    owned = client is None
    client = client or EcosystemsClient()
    found: dict[str, set[tuple[str, str]]] = {}
    try:
        for ecosystem, names in packages.items():
            for name in names:
                for repo in await client.dependent_repos(ecosystem, name, max_pages=max_pages):
                    found.setdefault(repo, set()).add((ecosystem, name))
                log.info("ecosystems: %s:%s -> %d repos so far", ecosystem, name, len(found))
    finally:
        if owned:
            await client.__aexit__()
    return found, client.stats
