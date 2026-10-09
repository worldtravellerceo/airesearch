"""How an explosion-board repository relates to AI, read off its own files.

Every rule here was measured on the 2026-10-09 risers with each hit read by
hand. The tests pin the cases that decided the rules: the false positives that
made a looser pattern lose, and the refusal that must not become "nothing found".
"""

import datetime as dt

import httpx
import pytest

from airadar import evidence
from airadar.collect import score_explosions
from airadar.db import repo as db

TODAY = dt.date(2026, 10, 9)


# --- reading -------------------------------------------------------------------


def test_an_agent_file_is_evidence_of_being_built_with_agents():
    """photocraft's AGENTS.md is 'a guide for AI agents and contributors', and
    its contributor file attributes 286 of 482 commits to models."""
    found = evidence.read_evidence(
        agents_md="# Guide for AI agents and contributors\n...", claude_md=None, readme=""
    )
    assert found.agent_file == "AGENTS.md"


def test_a_claude_md_that_is_a_symlink_to_agents_md_still_counts():
    found = evidence.read_evidence(agents_md="# Agents", claude_md="AGENTS.md", readme="")
    assert found.agent_file == "AGENTS.md+CLAUDE.md"


@pytest.mark.parametrize(
    ("agents", "claude"),
    [
        # create-next-app: four of the 93 agent files read were generator output.
        ("<!-- BEGIN:nextjs-agent-rules -->\nUse the App Router.\n", "@AGENTS.md"),
        (None, "---\ndescription: Use Bun instead of Node.js, npm, pnpm, or vite.\n---\n"),
    ],
)
def test_a_project_generator_template_is_not_evidence(agents, claude):
    assert evidence.read_evidence(agents_md=agents, claude_md=claude, readme="").agent_file is None


def test_a_strict_build_statement_counts():
    found = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="# Agentcraft\n\nBuilt with Claude.\n"
    )
    assert found.built == "Built with Claude"


def test_an_ai_disclosure_section_counts():
    """LoreanXavier/pt-pc: 'AI coding tools were used in developing ... this port'."""
    found = evidence.read_evidence(
        agents_md=None,
        claude_md=None,
        readme="## AI disclosure\nAI coding tools were used in developing and debugging this port.",
    )
    assert found.built is not None


def test_describing_what_the_product_does_with_ai_is_not_a_build_statement():
    """guillaumemeyer/watermarks-remover: 'check if a file was made with Claude'
    is the product's feature, not how it was written — one of the 7 of 12 the
    loose pattern got wrong."""
    found = evidence.read_evidence(
        agents_md=None,
        claude_md=None,
        readme="Check if a file was made with Claude, then strip the watermark.",
    )
    assert found.built is None


def test_exposing_an_mcp_server_is_agent_ready_and_configuring_them_is_not():
    """Plural 'MCP servers' is a host adding other people's servers
    (xai-org/grok-build), not a product exposing itself."""
    ready = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="Drive it from an agent over MCP: photocraft-cli mcp"
    )
    host = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="Add MCP servers from the settings panel."
    )
    assert ready.agent_ready == "over MCP"
    assert host.agent_ready is None


def test_a_bare_model_name_is_a_mention_not_use():
    """On the 2026-10-09 board, "Claude" alone in a README was as often "I wrote
    this with Claude" as "this runs on Claude" (aardappel/goose, a programming
    language, was labelled "uses AI" on it)."""
    found = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="The Goose language. Thanks, Claude."
    )
    assert found.integrated is None
    assert found.mentions == "Claude"


def test_claude_monet_is_a_painter():
    """photocraft's README credits its sample images: 'Impression, Sunrise, Claude Monet'."""
    found = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="Impression, Sunrise — Claude Monet, 1872"
    )
    assert found.integrated is None
    assert found.mentions is None


def test_provider_names_next_to_agent_ready_text_are_about_the_agent():
    """'Claude can drive this over MCP' says who drives it, not what it runs on.
    Without this the label fell from 79% right to 62%."""
    found = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="Claude can drive every command over MCP."
    )
    assert found.agent_ready is not None
    assert found.integrated is None
    assert found.mentions is None


def test_an_api_key_variable_is_strong_evidence_of_using_a_model():
    found = evidence.read_evidence(
        agents_md=None, claude_md=None, readme="Set OPENROUTER_API_KEY and run it."
    )
    assert found.integrated == "OPENROUTER_API_KEY"


def test_quotes_are_short():
    """Third-party text is published as evidence; only the matched phrase goes."""
    found = evidence.read_evidence(
        agents_md=None,
        claude_md=None,
        readme="This project was built entirely with Claude Code " + "and more " * 50,
    )
    assert found.built is not None
    assert len(found.built) <= evidence.QUOTE_CHARS


# --- fetching ------------------------------------------------------------------


def transport(files: dict[str, tuple[int, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/HEAD/", 1)[1]
        status, body = files.get(path, (404, ""))
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


async def probe_with(files, **kwargs):
    async with httpx.AsyncClient(transport=transport(files)) as http:
        return await evidence.probe(http, "owner/repo", **kwargs)


async def test_nothing_found_is_a_clean_answer():
    found = await probe_with({"README.md": (200, "# A photo editor\nLayers and masks.")})
    assert found.status == "ok"
    assert not (found.agent_file or found.built or found.agent_ready or found.integrated)


async def test_a_refusal_is_not_an_empty_answer():
    """news.crunchbase.com answered 403 to the default user agent and the
    channel logged a clean finish for its whole life. A 403 here is "partial",
    re-asked next run, and never stored as "nothing found"."""
    found = await probe_with(
        {"AGENTS.md": (403, "rate limited"), "README.md": (200, "# A photo editor")}
    )
    assert found.status == "partial"


async def test_a_repository_that_answers_nothing_is_unreachable():
    found = await probe_with({})
    assert found.status == "unreachable"


async def test_a_readme_that_is_a_symlink_falls_back_to_the_stored_excerpt():
    """raw.githubusercontent serves a symlinked README as its target path —
    `colinhacks/zod` returns 22 bytes of `packages/zod/README.md`."""
    found = await probe_with(
        {"README.md": (200, "packages/zod/README.md")},
        stored_excerpt="Built with Claude.",
    )
    assert found.built == "Built with Claude"


# --- collecting ----------------------------------------------------------------


def seed_board(conn):
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=10,
                full_name="storytold/photocraft",
                owner="storytold",
                name="photocraft",
                created_at=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
                stars=31_865,
                discovered_via="nursery",
            )
        ],
    )
    db.record_snapshot(conn, 10, TODAY, stars=31_865)
    conn.commit()
    score_explosions(conn, today=TODAY)


async def test_collect_reads_today_s_board_and_does_not_repeat_a_clean_read(conn):
    seed_board(conn)
    files = {
        "AGENTS.md": (200, "# Guide for AI agents and contributors"),
        "README.md": (200, "Every action is a command, reachable over MCP."),
    }
    first = await evidence.collect_evidence(conn, today=TODAY, transport=transport(files))
    assert (first.probed, first.found) == (1, 1)
    row = conn.execute("SELECT * FROM repo_ai_evidence WHERE repo_id = 10").fetchone()
    assert (row["status"], row["agent_file"], row["agent_ready"]) == ("ok", "AGENTS.md", "over MCP")

    again = await evidence.collect_evidence(conn, today=TODAY, transport=transport(files))
    assert again.considered == 0


async def test_collect_re_asks_after_a_refusal(conn):
    seed_board(conn)
    refused = {"AGENTS.md": (429, ""), "README.md": (200, "# x")}
    first = await evidence.collect_evidence(conn, today=TODAY, transport=transport(refused))
    assert first.partial == 1
    second = await evidence.collect_evidence(conn, today=TODAY, transport=transport(refused))
    assert second.considered == 1
