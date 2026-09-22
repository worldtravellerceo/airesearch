"""Rule-engine tests.

Two failure modes matter, in opposite directions. Missing a real AI project
means it never enters the universe at all. Waving through a non-AI project
pollutes every board. The band in between is what we pay the LLM to read.
"""

import pytest

from airadar.classify import rules as rules_module
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


def test_a_domain_in_a_description_is_an_address_not_a_claim():
    """`geohot/minikeyvalue` is a distributed key-value store. Its description
    ends "used in production at comma.ai", `_WORD` treats the dot as a word
    boundary, and the bare `ai` that fell out scored 0.88 and put it on an AI
    board. Hand-read out of thirty newly-classified repositories."""
    verdict = classify(
        facts(
            "geohot/minikeyvalue",
            "A distributed key value store in under 1000 lines. Used in production at comma.ai",
        )
    )

    assert verdict.is_ai is False


def test_stripping_the_tld_does_not_silence_the_company():
    """The fix takes the suffix, not the name. `openai.com` still speaks as
    `openai`, which is the whole point of keeping the body of the hostname —
    dropping the token wholesale would have cost every repository that names
    a vendor by its domain."""
    verdict = classify(facts("acme/sdk", "A client for openai.com"))

    assert "phrase?:openai" in verdict.matched
    assert verdict.needs_llm is True


# --- the ambiguous middle --------------------------------------------------


def test_ambiguous_repos_are_escalated_rather_than_guessed():
    """'agent' is a monitoring daemon as often as an LLM agent — exactly the
    case a human would want to read the README for."""
    verdict = classify(facts("acme/agent", "A lightweight agent", ["agent"]))

    assert verdict.needs_llm is True
    assert 0.2 < verdict.confidence < 0.8
    assert verdict.category is None  # no category is asserted until it is known


def test_weak_signals_alone_never_reach_certainty():
    """Suggestive hints may add up to a decision, but never to the certainty a
    decisive topic buys — that is what the soft ceiling is for."""
    verdict = classify(facts("acme/thing", "A tool", ["agent", "dataset", "observability"]))
    assert verdict.confidence <= rules_module.SOFT_CEILING
    assert verdict.confidence < classify(facts("a/b", None, ["llm"])).confidence


def test_the_soft_ceiling_is_a_ceiling_and_not_a_bar():
    """The first version set the ceiling at 0.78 against a threshold of 0.80,
    so no amount of weak evidence could ever settle anything. 17,849 repos —
    `openai/codex` and `meta-llama/llama` among them — escalated forever."""
    assert rules_module.SOFT_CEILING > 0.8

    piled_up = classify(facts("acme/thing", "A tool", ["ai", "agent", "mcp"]))
    assert piled_up.is_ai is True
    assert piled_up.needs_llm is False


def test_a_single_decisive_topic_beats_a_pile_of_weak_ones():
    decisive = classify(facts("a/b", None, ["llm"]))
    weak = classify(facts("c/d", None, ["ai", "ml"]))

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


# --- regressions from real data -------------------------------------------


def test_a_stray_topic_does_not_decide_the_category():
    """From the first real run: huggingface/transformers lists
    `speech-recognition` among two dozen topics and was filed under
    audio-speech, while seven of its other topics say classic ML."""
    transformers = facts(
        "huggingface/transformers",
        "State-of-the-art Machine Learning for PyTorch, TensorFlow and JAX",
        [
            "nlp",
            "natural-language-processing",
            "pytorch",
            "tensorflow",
            "jax",
            "machine-learning",
            "deep-learning",
            "speech-recognition",
            "transformer",
            "pretrained-models",
            "llm",
            "python",
        ],
    )

    assert classify(transformers).category == "classic-ml"


def test_a_single_topic_still_decides_when_it_is_the_only_evidence():
    assert classify(facts("a/b", "x", ["speech-recognition"])).category == "audio-speech"
    assert classify(facts("c/d", "x", ["model-context-protocol"])).category == "mcp"


def test_no_category_is_asserted_for_a_repo_we_have_not_judged():
    """`mcp` on its own is suggestive, not decisive — a monitoring agent and an
    MCP server can both carry it. Labelling it anyway would be a guess."""
    verdict = classify(facts("c/d", "x", ["mcp"]))
    assert verdict.needs_llm is True
    assert verdict.category is None


def test_ties_fall_to_the_more_specific_category():
    """Two categories matching equally is what the priority order is for."""
    both = facts("a/b", "an llm agent", ["agent", "agentic-ai", "llm", "gpt"])
    assert classify(both).category == "agent-framework"


def test_changing_the_rules_invalidates_cached_verdicts():
    """A verdict cached under rules that no longer exist is worse than no
    verdict, so the rules version is part of the hash."""
    from airadar.classify import rules as rules_module

    before = facts("a/b", "x", ["llm"]).content_hash()
    original = rules_module.RULES_VERSION
    try:
        rules_module.RULES_VERSION = "999"
        after = facts("a/b", "x", ["llm"]).content_hash()
    finally:
        rules_module.RULES_VERSION = original

    assert before != after


def test_a_framework_topic_is_not_merely_suggestive():
    """A repo topiced `pytorch` is a machine-learning repo; there is no second
    reading. Filing these with `ai` and `agent` as equally ambiguous stranded
    2,744 paper implementations in the escalation band."""
    for topic in ("pytorch", "tensorflow", "keras", "nlp", "ocr"):
        verdict = classify(facts("someone/impl", "An implementation", [topic]))
        assert verdict.is_ai is True, topic
        assert verdict.needs_llm is False, topic


def test_an_ai_lab_owner_decides_a_repo_with_no_other_evidence():
    """`deepseek-ai/DeepSeek-V3`: no topics, no description, 104k stars, and
    so no evidence at all until you read the owner."""
    verdict = classify(facts("deepseek-ai/DeepSeek-V3", None, []))
    assert verdict.is_ai is True
    assert verdict.needs_llm is False

    # The signal is the owner, not the word appearing anywhere in the name.
    assert classify(facts("someone/openai-is-not-the-owner", "A tool", [])).is_ai is False


def test_the_same_word_twice_is_one_piece_of_evidence():
    """Noisy-OR assumes independent signals. `pytorch` as a topic and "PyTorch"
    in the description are one fact seen twice, and counting both inflated
    routine repos toward the ceiling."""
    once = classify(facts("a/b", "An implementation", ["pytorch"]))
    twice = classify(facts("c/d", "A PyTorch implementation", ["pytorch"]))

    assert once.confidence == twice.confidence


def test_generic_lists_are_still_rejected():
    """From real data: `public-apis/public-apis` and `awesome-public-datasets`
    carry `dataset`/`datasets` and are not AI projects."""
    for repo in (
        facts("public-apis/public-apis", "A collective list of free APIs", ["api", "dataset"]),
        facts(
            "awesomedata/awesome-public-datasets",
            "A topic-centric list of HQ open datasets",
            ["datasets"],
        ),
        facts("redis/redis", "The preferred in-memory data store", ["cache", "database"]),
        facts(
            "prometheus/prometheus", "The Prometheus monitoring system", ["monitoring", "metrics"]
        ),
    ):
        assert classify(repo).is_ai is False, repo.full_name


def test_the_2026_agent_vocabulary_is_recognised():
    """From the live corpus: `anomalyco/opencode` (208k stars, "The open source
    coding agent.") and `cline/cline` (68k, "Autonomous coding agent") scored
    0.00 and were filed as not-AI. No topics to fall back on, and 0.00 is below
    the escalation band, so they were not even flagged as uncertain — the
    engine was confidently wrong about two of the best-known coding agents."""
    for description in (
        "The open source coding agent.",
        "Autonomous coding agent as an SDK, IDE extension, or CLI assistant.",
        "The open agent skills tool",
        "An autonomous agent for deep financial research",
        "On-device computer use agent that runs fully in the background",
        "Open Multi-Agent Interactive Classroom",
    ):
        verdict = classify(facts("someone/thing", description, []))
        assert verdict.is_ai is True, description
        assert verdict.needs_llm is False, description


def test_a_passing_mention_of_a_harness_is_not_a_decision():
    """`getumbrel/umbrel` is a home server OS whose description happens to say
    you can run OpenClaw on it. Naming a harness is weaker evidence than being
    one, so it escalates rather than settling."""
    verdict = classify(
        facts("getumbrel/umbrel", "An elegant home server OS. Run OpenClaw, store your files", [])
    )
    assert verdict.is_ai is False


# --- negative evidence -----------------------------------------------------


def test_a_search_engine_that_supports_ai_is_not_an_ai_project():
    """`meilisearch/meilisearch` declares twenty topics, fifteen of them search
    and storage and one of them `ai`, and reached an AI board at 0.99. Noisy-OR
    cannot tell "supports AI" from "is AI" on its own; the author's own topic
    list can."""
    verdict = classify(
        facts(
            "meilisearch/meilisearch",
            "A lightning-fast search engine API bringing AI-powered hybrid search "
            "to your sites and applications.",
            ["ai", "database", "enterprise-search", "full-text-search", "search", "search-engine"],
        )
    )

    assert verdict.is_ai is False
    assert verdict.needs_llm is True  # to a person, not to silence


def test_the_product_reading_is_a_ratio_and_not_a_flag():
    """Capping on the mere presence of a product topic scored beautifully on
    the 160-repo sample — which holds eight such repositories — and then took
    `FlowiseAI/Flowise`, `qdrant/qdrant` and 1,390 others off the boards when
    run over the whole index. An AI project that also stores things declares
    mostly AI topics."""
    ai_first = classify(
        facts(
            "FlowiseAI/Flowise",
            "Build AI Agents, Visually",
            ["agents", "artificial-intelligence", "chatbot", "llm", "workflow-automation"],
        )
    )

    assert ai_first.is_ai is True


def test_a_repository_that_carries_the_vocabulary_in_its_own_name_is_exempt():
    """Naming a thing is not describing it. This is what keeps
    `Untrivial-ai/agent-orchestrator` on the boards while `conductor-oss/conductor`,
    whose description also says "agentic", goes to review."""
    named = classify(
        facts("Untrivial-ai/agent-orchestrator", "Run and supervise teams of coding agents", [])
    )
    unnamed = classify(
        facts(
            "conductor-oss/conductor",
            "Conductor is an event driven agentic workflow engine providing durable execution",
            ["orchestration-engine", "orchestrator", "workflow-engine", "workflow-management"],
        )
    )

    assert named.is_ai is True
    assert unnamed.is_ai is False


def test_identity_evidence_overrides_the_product_reading():
    """`pgvector/pgvector` is a database by every topic it carries, and it is
    also unambiguously part of this field. A term from before any of this was
    called AI is not something a marketing line produces."""
    assert (
        classify(
            facts(
                "pgvector/pgvector",
                "Open-source vector similarity search for Postgres",
                ["database", "postgres"],
            )
        ).is_ai
        is True
    )
