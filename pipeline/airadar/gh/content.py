"""Reading what a repository says about itself.

For a long time only borderline repos got here, on their way to the LLM. That
was the wrong place to stop. Of the forty highest-star repositories created
since July, fourteen scored zero on name, description and topics — and so were
recorded as "not AI" and never escalated at all. `andrewyng/openworker` has
18,090 stars and no description. `browser-use/jev-ultrafast` has 14,310 and
says "i. am. speed."; its README's second line is "A browser agent with a
dynamic, indexed action space", which the rule engine already knows how to
read.

So the excerpt is now evidence in its own right, for every repository we cannot
otherwise place. It stays deliberately small: the opening of a README says what
a project is, while the rest lists integrations — and a tool that merely
mentions ChatGPT is not an AI project.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

from airadar.gh.client import GitHubClient, GitHubError

log = logging.getLogger(__name__)

DEFAULT_EXCERPT_CHARS = 1200

_BADGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WHITESPACE = re.compile(r"[ \t]*\n[ \t]*")
_BLANK_RUN = re.compile(r"\n{3,}")


def clean_readme(markdown: str, *, limit: int = DEFAULT_EXCERPT_CHARS) -> str:
    """Strip the noise that dominates the top of most READMEs.

    Badge rows, HTML banners and link targets are almost pure token cost — the
    prose around them is the part that identifies the project.
    """
    text = markdown or ""
    text = _BADGE.sub("", text)
    text = _HTML_TAG.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _WHITESPACE.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()[:limit]


def decode_content(payload: dict | None) -> str:
    if not payload:
        return ""
    if payload.get("encoding") == "base64" and payload.get("content"):
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""
    return payload.get("content") or ""


@dataclass
class Readme:
    """A fetched README, or the news that it has not changed.

    `unchanged` is what makes this affordable to keep current: a conditional
    request that comes back 304 costs no quota at all, and a README changes far
    less often than a star count.
    """

    excerpt: str = ""
    etag: str | None = None
    unchanged: bool = False
    missing: bool = False


async def fetch_readme(
    client: GitHubClient,
    full_name: str,
    *,
    etag: str | None = None,
    limit: int = DEFAULT_EXCERPT_CHARS,
) -> Readme:
    """The opening of a repo's README, cleaned, fetched conditionally.

    A repo with no README, or one we are not allowed to read, is a normal
    outcome rather than a failure — it is recorded as `missing` so the same
    empty request is not paid for every run.
    """
    try:
        response = await client.get(f"/repos/{full_name}/readme", etag=etag)
    except GitHubError as exc:
        if exc.status in {404, 403, 451}:
            return Readme(missing=True)
        log.warning("readme: %s failed: %s", full_name, exc)
        return Readme(missing=True)

    if response.not_modified:
        return Readme(etag=etag, unchanged=True)
    return Readme(
        excerpt=clean_readme(decode_content(response.data), limit=limit),
        etag=response.etag,
    )


async def fetch_readme_excerpt(
    client: GitHubClient, full_name: str, *, limit: int = DEFAULT_EXCERPT_CHARS
) -> str:
    """The opening of a repo's README, cleaned. Empty string if it has none."""
    return (await fetch_readme(client, full_name, limit=limit)).excerpt
