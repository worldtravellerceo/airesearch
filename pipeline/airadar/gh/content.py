"""Fetching the extra context the LLM classifier needs.

Only borderline repos get here, and each one costs a request, so the excerpt is
deliberately small: the opening of a README says what a project is far more
reliably than the installation instructions and badge wall further down.
"""

from __future__ import annotations

import base64
import logging
import re

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


async def fetch_readme_excerpt(
    client: GitHubClient, full_name: str, *, limit: int = DEFAULT_EXCERPT_CHARS
) -> str:
    """The opening of a repo's README, cleaned. Empty string if it has none."""
    try:
        response = await client.get(f"/repos/{full_name}/readme")
    except GitHubError as exc:
        if exc.status in {404, 403, 451}:
            return ""
        log.warning("readme: %s failed: %s", full_name, exc)
        return ""
    return clean_readme(decode_content(response.data), limit=limit)
