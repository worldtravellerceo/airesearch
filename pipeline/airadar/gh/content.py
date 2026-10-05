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


#: A README body that is nothing but a path to another README. Git stores a
#: symbolic link as a blob holding its target, and raw.githubusercontent.com
#: serves that blob verbatim — so fetching `colinhacks/zod`'s root README.md
#: returns the 22 bytes `packages/zod/README.md`, with status 200 and no hint
#: that anything is wrong. The API's `/repos/{repo}/readme` endpoint resolves
#: the link properly; measured over the 55,534 stored excerpts, none is a
#: symlink body. The raw path is the one that needs this, and it is the path
#: the packet builders use because it needs no token.
_SYMLINK_BODY = re.compile(
    # The leading directory is optional: a root `README.md` symlinked to a
    # sibling `readme.md` has a body with no slash in it at all.
    r"^(?:[\w.\-/@]+/)?(?:readme|README)[\w.\-]*\.(?:md|rst|markdown)$",
    re.I,
)


def symlink_target(body: str) -> str | None:
    """The path this README points at, if it is a symlink rather than a README.

    What it cost: three of the 1,324 repositories on the boards had a paragraph
    written from 22 bytes of path instead of their README, `vercel/ai` among
    them — the AI SDK, on an AI board, described from nothing. Three of the 200
    repositories in the 2026-10-05 review slice had the same, and two of those
    went back to the queue undecided because the reader could see there was no
    text to read. That is the better failure of the two, and it is still a
    wasted slot.

    The check is deliberately narrow: a single line, under 200 characters, no
    whitespace, ending in a README filename. A real README that is one line
    long does not match, because a real README does not end in `.md`.
    """
    text = (body or "").strip()
    if not text or "\n" in text or len(text) > 200:
        return None
    return text if _SYMLINK_BODY.match(text) else None


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
