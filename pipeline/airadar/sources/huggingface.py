"""Hugging Face Hub as a discovery channel.

Models and Spaces frequently link the GitHub repository that produced them, and
those repositories are often absent from GitHub search results entirely: a
research codebase with no topics and a two-line README is invisible to a topic
query but sits at the top of the Hub by downloads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from airadar.sources.ecosystems import repo_from_url

log = logging.getLogger(__name__)

API_ROOT = "https://huggingface.co/api"
PAGE_SIZE = 100
REQUEST_PAUSE_SECONDS = 0.2


@dataclass
class HuggingFaceStats:
    requests: int = 0
    items_seen: int = 0
    repos_found: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"{self.repos_found} repos from {self.items_seen} Hub items "
            f"({self.requests} requests, {self.errors} errors)"
        )


class HuggingFaceClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = None,
        user_agent: str = "airadar/0.1",
        sleep=asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self.stats = HuggingFaceStats()
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=headers,
            follow_redirects=True,
        )

    async def __aenter__(self) -> HuggingFaceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict):
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            log.warning("huggingface: %s failed: %s", path, exc)
            self.stats.errors += 1
            return None
        self.stats.requests += 1
        if response.status_code >= 400:
            log.warning("huggingface: %s -> %s", path, response.status_code)
            self.stats.errors += 1
            return None
        await self._sleep(REQUEST_PAUSE_SECONDS)
        return response.json()

    async def linked_repos(self, kind: str = "models", *, max_pages: int = 20) -> set[str]:
        """GitHub repositories linked from the most-downloaded models or spaces."""
        if kind not in {"models", "spaces"}:
            raise ValueError(f"unknown Hub kind {kind!r}")

        found: set[str] = set()
        for page in range(max_pages):
            payload = await self._get(
                f"/{kind}",
                {
                    "sort": "downloads",
                    "direction": -1,
                    "limit": PAGE_SIZE,
                    "skip": page * PAGE_SIZE,
                    "full": "true",
                },
            )
            if not payload:
                break
            self.stats.items_seen += len(payload)
            for item in payload:
                found |= _links_from_item(item)
            if len(payload) < PAGE_SIZE:
                break

        self.stats.repos_found += len(found)
        return found


def _links_from_item(item: dict) -> set[str]:
    """Pull GitHub links out of a Hub item's card metadata."""
    found: set[str] = set()
    card = item.get("cardData") or {}
    candidates = [card.get("repository"), card.get("github"), item.get("repository")]
    for key in ("source", "code", "homepage"):
        value = card.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for candidate in candidates:
        repo = repo_from_url(candidate) if isinstance(candidate, str) else None
        if repo:
            found.add(repo)
    return found


async def discover_via_huggingface(
    *, token: str | None = None, max_pages: int = 20, client: HuggingFaceClient | None = None
) -> tuple[set[str], HuggingFaceStats]:
    owned = client is None
    client = client or HuggingFaceClient(token=token)
    try:
        found = await client.linked_repos("models", max_pages=max_pages)
        found |= await client.linked_repos("spaces", max_pages=max_pages)
    finally:
        if owned:
            await client.__aexit__()
    return found, client.stats
