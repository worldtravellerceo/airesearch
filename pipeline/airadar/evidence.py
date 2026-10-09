"""How a repository relates to AI, read off files it ships — for the explosion boards.

The explosion boards have no classifier gate, so every row needs some other way
to say how it relates to AI. "Not AI" is the one answer it may never give from
absence: on a 40-repository slice of the 2026-10-09 risers, 3 of the 16 rows
the rules had called is_ai=0 were AI products by their own README. So this
module only ever records evidence that something is there, and "nothing found"
stays a separate value from "not looked".

What was measured on the top 250 risers that day, every hit read by hand:

- An agent instruction file at the root (AGENTS.md or CLAUDE.md) is the
  strongest sign a project is being built with coding agents: 81 of 89 were
  instructions for agents working on the code, and all 89 showed a real AI
  relationship. Two token-free GETs per repository; the other seven paths
  probed (.cursorrules, GEMINI.md, llms.txt, ...) added nothing.
  `storytold/photocraft` has one, and its own contributor file attributes 286
  of its 482 commits to models.
- A README sentence saying so ("Built with Claude.", an "AI disclosure"
  section): 6 of 7 right with the strict patterns, 5 of 12 with looser ones —
  so only the strict ones are used.
- "Agent-ready" — the product exposes itself to agents (its own MCP server, an
  agent skill or plugin, "works with AI agents"): 25 of 27 right. Plural
  "MCP servers" is a host configuring other people's servers and does not
  count.
- "Uses AI" for a repository the classifier did not call AI: 23 of 29.

All of it is third-party text. It is matched, never followed, and only the
matched phrase is kept — a short quote, not the paragraph around it — because
it is published on the site and in the feed the reader's agents consume.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import sqlite3
from dataclasses import dataclass, field

import httpx

from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh.content import symlink_target

log = logging.getLogger(__name__)

RAW_ROOT = "https://raw.githubusercontent.com"
AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
#: Re-read a repository's files after this long. Its README and agent files
#: change far more slowly than its stars.
RECHECK_DAYS = 7
#: The longest quote kept from third-party text.
QUOTE_CHARS = 80
#: The most of any one file that is read. The sentences this module looks for
#: sit near the top, and raw.githubusercontent.com serves files up to 100 MB.
MAX_BYTES = 256_000

_I = re.I
#: "Written by Devin Abbott", "Created by Claude Dupont": a bare tool name followed
#: by a capitalised word is a person. Case-sensitive inside a case-blind pattern.
_PERSON = r"(?!\s+(?-i:[A-Z][a-z]))"
_AI_TOOLS = (
    r"(?:(?:claude\s+)?(?:opus|sonnet|haiku)(?:\s*\d(?:\.\d)?)?"
    r"|claude(?:\s*code|\s*\d(?:\.\d)?|" + _PERSON + r")"
    r"|codex(?:\s*cli)?|cursor|chat\s?gpt|gpt-?\s?\d(?:\.\d)?(?:-?codex)?|copilot|gemini(?:\s*cli)?"
    r"|windsurf|aider|devin" + _PERSON + r"|opencode|an?\s+ai(?:\s+(?:agent|assistant))?"
    r"|ai(?:\s+(?:agents?|assistants?|coding\s+(?:agents?|assistants?|tools?)))?|llms?|coding\s+agents?)"
)
_VERBS = r"(?:built|made|written|developed|created|coded|generated|vibe[- ]?coded)"
_ADVERBS = (
    r"(?:almost\s+|nearly\s+)?(?:entirely\s+|mostly\s+|fully\s+|largely\s+|completely\s+|100%\s+)?"
)
_SUBJECT = (
    r"(?:(?:^|\n|[.!>|]\s*)\W{0,4}"
    r"|\b(?:this|the|our|my)\s+(?:entire\s+|whole\s+)?(?:project|repo|repository|codebase|code"
    r"|app|application|game|site|website|tool|library|extension|port|plugin|engine)\s+"
    r"(?:was|is|has\s+been|were)\s+(?:\w+\s+){0,3}"
    r"|\b(?:i|we)\s+(?:\w+\s+){0,2})"
)

#: A sentence about this repository having been built with AI. The strict set
#: only — the loose "made with <tool>" forms were right 5 times in 12, mostly
#: describing what the product does with AI rather than how it was made.
BUILT_PATTERNS = (
    re.compile(
        _SUBJECT
        + _VERBS
        + r"\s+"
        + _ADVERBS
        + r"(?:with|by|using|in)\s+(?:the\s+help\s+of\s+)?"
        + _AI_TOOLS
        + r"\b",
        _I,
    ),
    re.compile(
        r"(?:^|\n)\s*#{1,4}\s*(?:\W\s*)?(?:ai|llm)[- ]"
        r"(?:disclosure|usage|use|assistance|transparency)\b"
        r"|\b(?:ai|llm)\s+(?:coding\s+)?(?:tools?|assistants?|agents?|models?)\s+"
        r"(?:were|was|have\s+been|has\s+been)\s+used\s+(?:in|to|for|during)\b",
        _I,
    ),
)

#: The product exposes itself to agents.
AGENT_READY_PATTERNS = (
    re.compile(
        r"\b(?:an?|its|our|the|built-in|bundled|own|local)\s+mcp[- ]server\b(?!s)"
        r"|\bmcp[- ]server\s+(?:for|that|so|lets|exposes)\b"
        r"|\bmcp\s+(?:api|endpoint|interface|tools?)\b"
        r"|\b(?:over|via|through)\s+mcp\b|\bexposes?\b[^.\n]{0,40}\bmcp\b",
        _I,
    ),
    re.compile(
        r"\bSKILL\.md\b|\bagent[- ]skills?\b|\bclaude[- ](?:code[- ])?(?:skills?|plugins?)\b"
        r"|\bcodex[- ](?:skills?|plugins?)\b|\bnpx\s+skills\s+add\b|\bplugin\s+marketplace\s+add\b",
        _I,
    ),
    re.compile(
        r"\b(?:built|designed|made)\s+for\s+(?:ai\s+|coding\s+)?agents\b"
        r"|\bfor\s+(?:ai|coding)\s+agents\b"
        r"|\bagent[- ](?:ready|friendly|native|first)\b"
        r"|\bworks?\s+with\s+(?:ai\s+|coding\s+)?agents\b"
        r"|\b(?:ai\s+)?agents\s+(?:and\s+scripts\s+)?can\s+"
        r"(?:drive|build|edit|use|control|call|operate|run)\b",
        _I,
    ),
)
AGENT_READY_TOPICS = re.compile(
    r"^(?:mcp|mcp-server|model-context-protocol|agent-skills?|claude-skills?|claude-code-plugin"
    r"|claude-code-skills?|codex-skills?|agentskills)$"
)

#: Uses a model at runtime. `Claude Monet` paints the examples in photocraft's
#: README, which is why the negative lookahead is there.
PROVIDER = re.compile(
    r"\b(?:openai|anthropic|ollama|openrouter|litellm|llama\.cpp|vllm|groq|deepseek|mistral"
    r"|gemini|claude(?!\s+(?:monet|debussy))|gpt-?[345]|qwen|kimi)\b",
    _I,
)
INTEGRATED_STRONG = re.compile(
    r"\b(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE|OPENROUTER|DEEPSEEK|GROQ|MISTRAL|LLM|XAI)_API_KEY\b"
    r"|\b(?:ai|llm)[- ](?:powered|driven|native)\b"
    r"|\bpowered\s+by\s+(?:ai|llms?|gpt|claude|gemini|openai)\b"
    r"|\blarge\s+language\s+models?\b|\bllms?\b(?!\.txt)",
    _I,
)
INTEGRATED_TOPICS = re.compile(
    r"^(?:llm|llms|openai|anthropic|claude|gemini|ollama|chatgpt|gpt|ai|artificial-intelligence"
    r"|generative-ai|genai|ai-agents?|rag|deepseek|qwen|langchain|local-llm|machine-learning"
    r"|deep-learning|computer-vision|whisper|stable-diffusion|text-to-speech|tts|speech-recognition)$"
)

# Each bracket class excludes its own opener, so a scan stops at the next one.
# The obvious `\[([^\]]*)\]` is quadratic on a README of unclosed brackets:
# about an hour of event-loop time for one megabyte, measured in review.
_BADGE = re.compile(r"!\[[^\[\]]*\]\([^()]*\)")
_TAG = re.compile(r"<[^>]{1,300}>")
_LINK = re.compile(r"\[([^\[\]]*)\]\([^()]*\)")
_URL = re.compile(r"https?://\S+")
_SPACE = re.compile(r"\s+")


@dataclass
class Evidence:
    #: "ok" when every file answered, "unreachable" when nothing did (renamed,
    #: deleted, private), "partial" when some request was refused or failed.
    status: str = "ok"
    agent_file: str | None = None
    built: str | None = None
    agent_ready: str | None = None
    integrated: str | None = None
    #: A model or provider is named and nothing stronger is said. On the
    #: 2026-10-09 board this was most often "Claude" alone in a README, which
    #: as often means "I wrote this with Claude" as "this runs on Claude" — so
    #: it is published as "mentions AI", never as "uses AI".
    mentions: str | None = None
    notes: list[str] = field(default_factory=list)


def _quote(match: re.Match[str]) -> str:
    text = _SPACE.sub(" ", match.group(0)).strip(" .,;:!>|#-\n\t")
    return text[:QUOTE_CHARS]


def _first(patterns, text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return _quote(match)
    return None


def _clean(markdown: str) -> str:
    text = _BADGE.sub("", markdown)
    text = _TAG.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    return _URL.sub(" ", text)


def is_scaffold(agents_md: str | None, claude_md: str | None) -> bool:
    """True when the only agent files are a project generator's boilerplate.

    create-next-app writes a short AGENTS.md of Next.js rules with a CLAUDE.md
    that just imports it, and bun init writes a CLAUDE.md about Bun; four of
    the 93 agent files read on 2026-10-09 were one of these. They say which
    template was used, not who wrote the code.
    """
    agents = (agents_md or "").strip()
    claude = (claude_md or "").strip()
    nextjs = agents.startswith("<!-- BEGIN:nextjs-agent-rules -->") and len(agents) < 800
    trivial_claude = claude in ("", "@AGENTS.md", "AGENTS.md")
    bun = claude.startswith("---") and "Use Bun instead of Node.js" in claude[:200]
    if nextjs and trivial_claude:
        return True
    return bun and not agents


def read_evidence(
    *,
    agents_md: str | None,
    claude_md: str | None,
    readme: str | None,
    description: str | None = None,
    topics: tuple[str, ...] | list[str] = (),
) -> Evidence:
    """Every AI relationship the files show. Pure: the fetching is elsewhere."""
    evidence = Evidence()
    present = [
        name
        for name, body in (("AGENTS.md", agents_md), ("CLAUDE.md", claude_md))
        if body is not None and body.strip()
    ]
    if present and not is_scaffold(agents_md, claude_md):
        evidence.agent_file = "+".join(present)

    text = (description or "") + "\n" + _clean(readme or "")
    evidence.built = _first(BUILT_PATTERNS, text)
    evidence.agent_ready = _first(AGENT_READY_PATTERNS, text)
    if evidence.agent_ready is None:
        topic = next((t for t in topics if AGENT_READY_TOPICS.match(t)), None)
        if topic:
            evidence.agent_ready = f"topic: {topic}"

    strong = INTEGRATED_STRONG.search(text)
    if strong:
        evidence.integrated = _quote(strong)
    else:
        topic = next((t for t in topics if INTEGRATED_TOPICS.match(t)), None)
        if topic:
            evidence.integrated = f"topic: {topic}"
    if evidence.integrated is None:
        provider = PROVIDER.search(text)
        # A provider name next to agent-ready text is usually "Claude can drive
        # this over MCP" — about the user's agent, not the product. Measured:
        # without this suppression the label fell from 79% right to 62%.
        if provider and evidence.agent_ready is None:
            evidence.mentions = _quote(provider)
    return evidence


# --- fetching --------------------------------------------------------------


@dataclass
class Fetched:
    status: int | None
    body: str | None = None


async def _get(http: httpx.AsyncClient, full_name: str, path: str) -> Fetched:
    try:
        async with http.stream("GET", f"{RAW_ROOT}/{full_name}/HEAD/{path}") as response:
            if response.status_code != 200:
                return Fetched(status=response.status_code)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BYTES:
                    break
    except httpx.HTTPError as exc:
        log.warning("evidence: %s/%s failed: %s", full_name, path, exc)
        return Fetched(status=None)
    body = b"".join(chunks)[:MAX_BYTES].decode("utf-8", errors="replace")
    return Fetched(status=200, body=body)


async def probe(
    http: httpx.AsyncClient,
    full_name: str,
    *,
    description: str | None = None,
    topics: tuple[str, ...] | list[str] = (),
    stored_excerpt: str | None = None,
) -> Evidence:
    """Fetch the three files and read them.

    A 404 is an answer ("there is no such file"). Anything else that is not a
    200 — a 403, a 429, a timeout — is a refusal or a failure, and a refusal is
    not an empty answer: the result is marked partial and re-read next run
    instead of being stored as "nothing found".
    """
    paths = (*AGENT_FILES, "README.md")
    fetched = await asyncio.gather(*(_get(http, full_name, p) for p in paths))
    by_path = dict(zip(paths, fetched, strict=True))
    readme_item = by_path["README.md"]
    if readme_item.status == 404:
        # Raw paths are case-sensitive and the API's /readme is not: a live
        # repository whose README is readme.md answered 404 three times and
        # was filed as unreachable, re-asked forever.
        lower = await _get(http, full_name, "readme.md")
        if lower.status != 404:
            by_path["README.md"] = readme_item = lower
    target = (
        symlink_target(readme_item.body) if readme_item.status == 200 and readme_item.body else None
    )
    followed = None
    if target and not target.startswith(("/", "..")):
        # The zod case: the root README is a link to packages/zod/README.md.
        linked = await _get(http, full_name, target)
        if linked.status == 200 and linked.body and not symlink_target(linked.body):
            followed = linked.body
    statuses = [by_path[p].status for p in paths]

    def body(path: str) -> str | None:
        item = by_path[path]
        if item.status != 200 or item.body is None:
            return None
        # A symlink is served as its target's path. For an agent file the link
        # is itself the evidence (CLAUDE.md -> AGENTS.md is the common case);
        # for the README the path says nothing, so the stored excerpt — read
        # through the API, which resolves links — stands in for it.
        if path == "README.md" and symlink_target(item.body):
            return None
        return item.body

    readme = body("README.md") or followed or stored_excerpt
    evidence = read_evidence(
        agents_md=body("AGENTS.md"),
        claude_md=body("CLAUDE.md"),
        readme=readme,
        description=description,
        topics=topics,
    )
    if all(s == 404 for s in statuses) and not stored_excerpt:
        evidence.status = "unreachable"
    elif target and not readme:
        # The link was served, not the README. "Read and found nothing" would
        # be a claim about a file nobody read.
        evidence.status = "partial"
        evidence.notes.append(f"README.md -> {target} unread")
    elif any(s not in (200, 404) for s in statuses):
        evidence.status = "partial"
        evidence.notes.append(
            ", ".join(
                f"{p}={s}" for p, s in zip(paths, statuses, strict=True) if s not in (200, 404)
            )
        )
    return evidence


@dataclass
class EvidenceReport:
    considered: int = 0
    probed: int = 0
    found: int = 0
    unreachable: int = 0
    partial: int = 0

    def summary(self) -> str:
        return (
            f"{self.probed} of {self.considered} explosion repos probed: "
            f"{self.found} with AI evidence, {self.unreachable} unreachable, "
            f"{self.partial} partial (re-read next run)"
        )


async def collect_evidence(
    conn: sqlite3.Connection,
    *,
    today: dt.date,
    recheck_days: int = RECHECK_DAYS,
    limit: int | None = None,
    concurrency: int = 8,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EvidenceReport:
    """Read the AI evidence for every repository on today's explosion boards
    that has none yet, or whose evidence is older than `recheck_days`.

    Token-free: raw.githubusercontent.com is not the API and does not spend the
    account's 5,000 an hour. Measured: 2,750 GETs in 68.6 s at concurrency 8,
    no 429s.
    """
    # Three requests a repository: at INFO, httpx would log 2,400 lines a run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    report = EvidenceReport()
    targets = db.explosion_evidence_targets(
        conn, date=today, stale_before=today - dt.timedelta(days=recheck_days), limit=limit
    )
    report.considered = len(targets)
    if not targets:
        return report
    topics = db.repo_topics_map(conn, [t["repo_id"] for t in targets])
    settings = get_settings()
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        transport=transport,
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as http:

        async def one(row: dict) -> tuple[dict, Evidence]:
            async with semaphore:
                found = await probe(
                    http,
                    row["full_name"],
                    description=row["description"],
                    topics=topics.get(row["repo_id"], []),
                    stored_excerpt=row["readme_excerpt"],
                )
            return row, found

        # Saved as each one lands, not after the last: a run cut short keeps
        # what it read.
        for next_done in asyncio.as_completed([one(t) for t in targets]):
            row, found = await next_done
            report.probed += 1
            if found.status == "unreachable":
                report.unreachable += 1
            elif found.status == "partial":
                report.partial += 1
            if found.agent_file or found.built or found.agent_ready or found.integrated:
                report.found += 1
            db.save_ai_evidence(conn, row["repo_id"], found, checked_on=today)
            conn.commit()
    return report
