"""Rule-engine tests.

Two failure modes matter, in opposite directions. Missing a real AI project
means it never enters the universe at all. Waving through a non-AI project
pollutes every board. The band in between is what we pay the LLM to read.
"""

import pytest

from airadar.classify.rules import RepoFacts, classify, escalation_rate, partition


def facts(name, description=None, topics=(), language=None):
    return RepoFacts(
        full_name=name, description=description, topics=tuple(topics), language=language
    )


# --- obvious yes -----------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        facts(
            "langchain-ai/langchain",
            "Build context-aware reasoning applications",
            ["llm", "agents"],
        ),
        facts(
            "vllm-project/vllm", "A high-throughput inference engine for LLMs", ["llm-inference"]
        ),
        facts("acme/thing", "A retrieval augmented generation pipeline", []),
        facts("someone/whisper-x", "Automatic speech recognition with word-level timestamps", []),
        facts("org/repo", None, ["machine-learning"]),
    ],
)
def test_clear_ai_projects_are_settled_for_free(repo):
    verdict = classify(repo)
    assert verdict.is_ai is True
    assert verdict.needs_llm is False
    assert verdict.category is not None


# --- obvious no ------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        facts(
            "expressjs/express",
            "Fast, unopinionated, minimalist web framework for Node.js",
            ["nodejs", "web"],
        ),
        facts("acme/maintenance-scripts", "Scripts for server maintenance", ["devops"]),
        facts("someone/blockchain-wallet", "A cryptocurrency wallet", ["crypto"]),
        facts("org/certain-things", "Nothing to see here"),
    ],
)
def test_clearly_unrelated_projects_are_rejected_for_free(repo):
    verdict = classify(repo)
    assert verdict.is_ai is False
    assert verdict.needs_llm is False


def test_ai_is_not_matched_as_a_substring():
    """'maintenance', 'chain' and 'certain' all contain 'ai'. Substring matching
    here would quietly fill the universe with unrelated repositories."""
    for name in ("acme/maintenance", "acme/chain-tools", "acme/certainty"):
        assert classify(facts(name, "A utility")).confidence == 0.0


# --- the ambiguous middle --------------------------------------------------


def test_ambiguous_repos_are_escalated_rather_than_guessed():
    """'agent' is a monitoring daemon as often as an LLM agent — exactly the
    case a human would want to read the README for."""
    verdict = classify(facts("acme/agent", "A lightweight agent", ["agent"]))

    assert verdict.needs_llm is True
    assert 0.2 < verdict.confidence < 0.8
    assert verdict.category is None  # no category is asserted until it is known


def test_weak_signals_alone_never_reach_certainty():
    """Several suggestive hints should escalate, not settle: that is what the
    soft ceiling is for."""
    verdict = classify(facts("acme/gpt-bot", "A bot using openai", ["gpt", "openai", "chatbot"]))
    assert verdict.confidence <= 0.78
    assert verdict.needs_llm is True


def test_a_single_decisive_topic_beats_a_pile_of_weak_ones():
    decisive = classify(facts("a/b", None, ["llm"]))
    weak = classify(facts("c/d", None, ["ai", "ml", "gpt", "openai"]))

    assert decisive.is_ai is True
    assert weak.needs_llm is True
    assert decisive.confidence > weak.confidence


# --- categories ------------------------------------------------------------


@pytest.mark.parametrize(
    "topics,expected",
    [
        (["mcp", "ai-agents"], "mcp"),  # the more specific label wins
        (["ai-agents", "llm"], "agent-framework"),
        (["rag", "llm"], "rag-vectordb"),
        (["stable-diffusion"], "multimodal-vision"),
        (["text-to-speech"], "audio-speech"),
        (["machine-learning"], "classic-ml"),
    ],
)
def test_category_priority(topics, expected):
    assert classify(facts("a/b", "an llm project", topics)).category == expected


def test_category_falls_back_to_description_when_topics_are_absent():
    verdict = classify(facts("a/b", "An inference engine for large language models"))
    assert verdict.is_ai is True
    assert verdict.category == "inference-serving"


# --- caching ---------------------------------------------------------------


def test_content_hash_changes_only_when_the_inputs_do():
    base = facts("a/b", "An LLM agent", ["llm"], "Python")

    assert base.content_hash() == facts("a/b", "An LLM agent", ["llm"], "Python").content_hash()
    # Topic order is not information.
    assert base.content_hash() == facts("a/b", "An LLM agent", ["llm"], "Python").content_hash()
    assert base.content_hash() != facts("a/b", "A RAG tool", ["llm"], "Python").content_hash()
    assert (
        base.content_hash() != facts("a/b", "An LLM agent", ["llm", "rag"], "Python").content_hash()
    )


def test_topic_order_and_case_do_not_change_the_hash():
    a = facts("a/b", "x", ["LLM", "Agents"])
    b = facts("a/b", "x", ["agents", "llm"])
    assert a.content_hash() == b.content_hash()


# --- budgeting -------------------------------------------------------------


def test_partition_splits_settled_from_escalated():
    batch = [
        facts("a/langchain", "LLM framework", ["llm"]),  # settled yes
        facts("b/express", "web framework", ["nodejs"]),  # settled no
        facts("c/agent", "an agent", ["agent"]),  # escalate
    ]

    settled, escalate = partition(batch)

    assert {f.full_name for f, _ in settled} == {"a/langchain", "b/express"}
    assert {f.full_name for f, _ in escalate} == {"c/agent"}
    assert escalation_rate(batch) == pytest.approx(1 / 3)
    assert escalation_rate([]) == 0.0
