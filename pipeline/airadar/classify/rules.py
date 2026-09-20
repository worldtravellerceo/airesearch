"""The free tier of classification.

Most repositories can be decided without asking a model anything: a repo topiced
`llm` and described as an "LLM agent framework" is not a judgement call. This
module settles those, and hands only the genuinely ambiguous middle to the LLM —
which is what keeps a full sweep of the universe inside a couple of dollars.

Evidence is combined with a noisy-OR rather than a sum, because the signals
overlap heavily (a repo topiced `llm` usually also says "LLM" in its
description) and adding them would push routine repos straight past the ceiling.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from airadar.classify.taxonomy import DEFAULT_CATEGORY, categorise

# Topics that settle the question on their own.
DECISIVE_TOPICS: frozenset[str] = frozenset(
    {
        "llm",
        "llms",
        "large-language-models",
        "artificial-intelligence",
        "machine-learning",
        "deep-learning",
        "neural-network",
        "generative-ai",
        "genai",
        "rag",
        "retrieval-augmented-generation",
        "ai-agents",
        "agentic-ai",
        "ai-agent-framework",
        "model-context-protocol",
        "llm-inference",
        "llm-serving",
        "llmops",
        "fine-tuning",
        "rlhf",
        "foundation-models",
        "diffusion-models",
        "stable-diffusion",
        "vision-language-model",
        "speech-recognition",
        "text-to-speech",
        "natural-language-processing",
        "computer-vision",
        "transformers",
        "prompt-engineering",
        "llm-evaluation",
        "multimodal",
        "embeddings",
        "reinforcement-learning",
        "text-to-image",
        "vector-database",
    }
)

# Topics that point at AI but also have entirely unrelated uses. "agent" is a
# monitoring daemon as often as an LLM agent; "ai" appears on joke repos.
SUGGESTIVE_TOPICS: frozenset[str] = frozenset(
    {
        "ai",
        "ml",
        "agent",
        "agents",
        "mcp",
        "gpt",
        "chatgpt",
        "openai",
        "anthropic",
        "claude",
        "gemini",
        "llama",
        "mistral",
        "transformer",
        "chatbot",
        "inference",
        "embeddings",
        "dataset",
        "datasets",
        "nlp",
        "pytorch",
        "tensorflow",
        "jax",
        "keras",
        "scikit-learn",
        "huggingface",
        "langchain",
        "llamaindex",
        "quantization",
        "vllm",
        "ocr",
        "tts",
        "asr",
        "evaluation",
        "observability",
        "robotics",
        "time-series",
        "recommendation-system",
        "anomaly-detection",
        "copilot",
        "diffusion",
    }
)

# Phrases in a description that are hard to write about anything but AI.
DECISIVE_PHRASES: tuple[str, ...] = (
    "large language model",
    "language model",
    "llm",
    "genai",
    "generative ai",
    "ai agent",
    "agentic",
    "retrieval augmented generation",
    "retrieval-augmented",
    "vector database",
    "vector store",
    "embedding model",
    "fine-tune",
    "fine-tuning",
    "prompt engineering",
    "text-to-image",
    "text to image",
    "text-to-speech",
    "speech recognition",
    "diffusion model",
    "transformer model",
    "neural network",
    "machine learning",
    "deep learning",
    "model context protocol",
    "inference engine",
    "inference server",
    "multimodal model",
    "foundation model",
    "reinforcement learning",
    "computer vision",
    "natural language processing",
)

SUGGESTIVE_PHRASES: tuple[str, ...] = (
    "gpt",
    "chatgpt",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "llama",
    "mistral",
    "deepseek",
    "qwen",
    "huggingface",
    "hugging face",
    "pytorch",
    "tensorflow",
    "chatbot",
    "copilot",
    "ai-powered",
    "ai powered",
    "powered by ai",
    "semantic search",
    "knowledge base",
    "rag ",
    "mcp server",
    "stable diffusion",
    "whisper",
    "ollama",
    "langchain",
    "embeddings",
)

# Words that, standing alone in a name, mean nothing. Matching "ai" inside
# "maintenance" or "chain" is the classic way to poison a corpus like this.
NAME_TOKENS: frozenset[str] = frozenset(
    {
        "ai",
        "llm",
        "gpt",
        "ml",
        "rag",
        "agent",
        "agents",
        "mcp",
        "chat",
        "nlp",
        "vision",
        "diffusion",
        "prompt",
        "embed",
        "embeddings",
        "transformer",
        "neural",
        "bot",
        "copilot",
        "assistant",
    }
)

WEIGHT_DECISIVE_TOPIC = 0.90
WEIGHT_SUGGESTIVE_TOPIC = 0.45
WEIGHT_DECISIVE_PHRASE = 0.85
WEIGHT_SUGGESTIVE_PHRASE = 0.30
WEIGHT_NAME_TOKEN = 0.25
# Several weak signals should be able to add up to a decision, but never to the
# certainty that a decisive topic buys.
SOFT_CEILING = 0.78

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Verdict:
    is_ai: bool
    confidence: float
    category: str | None
    matched: tuple[str, ...]
    needs_llm: bool

    @property
    def method(self) -> str:
        return "rules"


@dataclass(frozen=True)
class RepoFacts:
    """The zero-cost inputs — everything here comes from the repo object we
    already fetch during collection."""

    full_name: str
    description: str | None = None
    topics: tuple[str, ...] = ()
    language: str | None = None
    homepage: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> RepoFacts:
        return cls(
            full_name=row["full_name"],
            description=row.get("description"),
            topics=tuple(row.get("topics") or ()),
            language=row.get("language"),
            homepage=row.get("homepage"),
        )

    def content_hash(self) -> str:
        """Identity of the *inputs*, so classification is only ever redone when
        something that could change the answer actually changed."""
        payload = "\x1f".join(
            [
                self.full_name.lower(),
                (self.description or "").strip().lower(),
                ",".join(sorted(t.lower() for t in self.topics)),
                (self.language or "").lower(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def tokenise(text: str | None) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def classify(facts: RepoFacts, *, low: float = 0.2, high: float = 0.8) -> Verdict:
    """Score how likely this repo is AI-related, from free signals only.

    Returns `needs_llm=True` for anything landing between `low` and `high` — the
    band where a human would want to read the README before deciding.
    """
    topics = {t.lower() for t in facts.topics}
    haystack = " ".join(
        filter(
            None,
            [
                facts.full_name.replace("/", " ").replace("-", " ").lower(),
                (facts.description or "").lower(),
            ],
        )
    )
    name_tokens = tokenise(facts.full_name.replace("/", " ").replace("-", " ").replace("_", " "))

    signals: list[tuple[str, float]] = []

    for topic in sorted(topics & DECISIVE_TOPICS):
        signals.append((f"topic:{topic}", WEIGHT_DECISIVE_TOPIC))
    for topic in sorted(topics & SUGGESTIVE_TOPICS):
        signals.append((f"topic?:{topic}", WEIGHT_SUGGESTIVE_TOPIC))
    for phrase in DECISIVE_PHRASES:
        if phrase in haystack:
            signals.append((f"phrase:{phrase}", WEIGHT_DECISIVE_PHRASE))
    for phrase in SUGGESTIVE_PHRASES:
        if phrase in haystack:
            signals.append((f"phrase?:{phrase}", WEIGHT_SUGGESTIVE_PHRASE))
    for token in sorted(name_tokens & NAME_TOKENS):
        signals.append((f"name:{token}", WEIGHT_NAME_TOKEN))

    confidence = _noisy_or(weight for _, weight in signals)
    has_decisive = any(key.startswith("topic:") or key.startswith("phrase:") for key, _ in signals)
    if not has_decisive:
        # Weak evidence only. Cap it below certainty so these still get read.
        confidence = min(confidence, SOFT_CEILING)

    category = categorise(topics)
    if category is None and confidence >= high:
        category = _category_from_text(haystack) or DEFAULT_CATEGORY

    return Verdict(
        is_ai=confidence >= high,
        confidence=round(confidence, 4),
        category=category if confidence >= high else None,
        matched=tuple(key for key, _ in signals[:12]),
        needs_llm=low < confidence < high,
    )


def _noisy_or(weights) -> float:
    """P(at least one signal is right), assuming rough independence."""
    product = 1.0
    for weight in weights:
        product *= 1.0 - min(max(weight, 0.0), 0.99)
    return 1.0 - product


_TEXT_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mcp", ("model context protocol", "mcp server")),
    ("agent-framework", ("ai agent", "agentic", "multi-agent", "autonomous agent")),
    (
        "rag-vectordb",
        (
            "retrieval augmented",
            "retrieval-augmented",
            "vector database",
            "vector store",
            "semantic search",
            "embedding",
        ),
    ),
    (
        "inference-serving",
        ("inference engine", "inference server", "serving", "quantization", "quantized"),
    ),
    ("training-finetuning", ("fine-tune", "fine-tuning", "training framework", "rlhf")),
    (
        "multimodal-vision",
        (
            "text-to-image",
            "text to image",
            "diffusion",
            "computer vision",
            "image generation",
            "ocr",
        ),
    ),
    ("audio-speech", ("text-to-speech", "speech recognition", "voice", "audio generation")),
    (
        "prompt-eval-observability",
        ("prompt engineering", "evaluation", "observability", "guardrail"),
    ),
    ("ai-devtools", ("code generation", "coding assistant", "copilot", "code review")),
    (
        "classic-ml",
        ("machine learning", "deep learning", "neural network", "reinforcement learning"),
    ),
)


def _category_from_text(haystack: str) -> str | None:
    for category, needles in _TEXT_CATEGORY_HINTS:
        if any(needle in haystack for needle in needles):
            return category
    return None


def partition(
    facts: list[RepoFacts], *, low: float = 0.2, high: float = 0.8
) -> tuple[list[tuple[RepoFacts, Verdict]], list[tuple[RepoFacts, Verdict]]]:
    """Split a batch into (settled, needs_llm)."""
    settled: list[tuple[RepoFacts, Verdict]] = []
    escalate: list[tuple[RepoFacts, Verdict]] = []
    for item in facts:
        verdict = classify(item, low=low, high=high)
        (escalate if verdict.needs_llm else settled).append((item, verdict))
    return settled, escalate


def escalation_rate(facts: list[RepoFacts], *, low: float = 0.2, high: float = 0.8) -> float:
    """Fraction of a batch that would cost money. Useful for budgeting a sweep
    before running it."""
    if not facts:
        return 0.0
    _, escalate = partition(facts, low=low, high=high)
    return len(escalate) / len(facts)
