"""What the boards get sliced by.

Categories are deliberately shallow and mutually exclusive: a repo gets exactly
one, chosen by the first matching rule in priority order. Deeper nuance lives in
`subcategory`, which is free text from the classifier and is never used for
ranking.

Order matters, and it runs most specific first. `mcp` sits above
`agent-framework` because an MCP server is almost always also agent tooling and
the narrower label is the useful one. `llm-app` sits near the bottom for the
opposite reason: the `llm` topic is on practically every project in this space,
so giving it high priority would let it swallow the RAG engines, the inference
servers and the fine-tuning toolkits alike. It is the fallback, not the default.
`awesome-list` leads because a curated list of LLM tools is a list, whatever
else it is topiced.
"""

from __future__ import annotations

from typing import Final

CATEGORIES: Final[tuple[str, ...]] = (
    "awesome-list",
    "mcp",
    "agent-framework",
    "rag-vectordb",
    "inference-serving",
    "training-finetuning",
    "prompt-eval-observability",
    "ai-devtools",
    "multimodal-vision",
    "audio-speech",
    "robotics-embodied",
    "model-weights",
    "data-tooling",
    "llm-app",
    "classic-ml",
)

LABELS_TR: Final[dict[str, str]] = {
    "mcp": "MCP / araç protokolü",
    "agent-framework": "Agent framework",
    "llm-app": "LLM uygulaması",
    "ai-devtools": "AI geliştirici aracı",
    "inference-serving": "Çıkarım / servis",
    "training-finetuning": "Eğitim / fine-tuning",
    "model-weights": "Model ağırlıkları",
    "rag-vectordb": "RAG / vektör DB",
    "prompt-eval-observability": "Prompt / değerlendirme / gözlemlenebilirlik",
    "multimodal-vision": "Görüntü / multimodal",
    "audio-speech": "Ses / konuşma",
    "robotics-embodied": "Robotik / gömülü",
    "data-tooling": "Veri araçları",
    "classic-ml": "Klasik ML",
    "awesome-list": "Kaynak listesi",
}

# Topics that place a repo in a category, in the priority order above. A repo
# matching several lands in whichever comes first.
CATEGORY_TOPICS: Final[dict[str, frozenset[str]]] = {
    "mcp": frozenset({"mcp", "model-context-protocol", "mcp-server", "mcp-client"}),
    "agent-framework": frozenset(
        {
            "ai-agents",
            "agents",
            "agent",
            "autonomous-agents",
            "multi-agent",
            "agentic",
            "agentic-ai",
            "ai-agent-framework",
            "tool-use",
            "function-calling",
            "agent-framework",
        }
    ),
    "ai-devtools": frozenset(
        {
            "copilot",
            "ai-coding",
            "code-generation",
            "coding-agent",
            "code-completion",
            "ai-code-review",
            "developer-tools",
        }
    ),
    "inference-serving": frozenset(
        {
            "inference",
            "inference-engine",
            "llm-inference",
            "llm-serving",
            "vllm",
            "quantization",
            "gguf",
            "onnx",
            "tensorrt",
            "model-serving",
            "edge-ai",
            "on-device-ai",
            "llmops",
            "ai-infrastructure",
        }
    ),
    "training-finetuning": frozenset(
        {
            "fine-tuning",
            "lora",
            "peft",
            "rlhf",
            "transfer-learning",
            "pretraining",
            "model-training",
            "dpo",
            "distributed-training",
        }
    ),
    "model-weights": frozenset(
        {"foundation-models", "language-model", "pretrained-models", "model-zoo"}
    ),
    "rag-vectordb": frozenset(
        {
            "rag",
            "retrieval-augmented-generation",
            "vector-database",
            "vector-search",
            "embeddings",
            "semantic-search",
            "hybrid-search",
            "knowledge-graph",
        }
    ),
    "prompt-eval-observability": frozenset(
        {
            "prompt-engineering",
            "prompting",
            "prompt-injection",
            "llm-evaluation",
            "evaluation",
            "observability",
            "guardrails",
            "ai-safety",
            "alignment",
            "red-teaming",
            "hallucination",
        }
    ),
    "multimodal-vision": frozenset(
        {
            "computer-vision",
            "image-generation",
            "text-to-image",
            "diffusion",
            "diffusion-models",
            "stable-diffusion",
            "image-segmentation",
            "object-detection",
            "ocr",
            "multimodal",
            "vision-language-model",
            "text-to-video",
            "video-generation",
            "3d-generation",
            "ai-art",
        }
    ),
    "audio-speech": frozenset(
        {
            "speech-recognition",
            "text-to-speech",
            "speech-to-text",
            "tts",
            "asr",
            "voice-cloning",
            "audio-generation",
            "music-generation",
        }
    ),
    "robotics-embodied": frozenset({"robotics", "self-driving", "embodied-ai", "drone"}),
    "data-tooling": frozenset(
        {
            "dataset",
            "datasets",
            "synthetic-data",
            "feature-store",
            "data-labeling",
            "annotation-tool",
            "data-science",
            "etl",
        }
    ),
    "llm-app": frozenset(
        {
            "chatbot",
            "conversational-ai",
            "chatgpt",
            "ai-assistant",
            "character-ai",
            "digital-human",
            "gpt",
            "llm",
        }
    ),
    "classic-ml": frozenset(
        {
            "machine-learning",
            "deep-learning",
            "neural-network",
            "ml",
            "pytorch",
            "tensorflow",
            "jax",
            "keras",
            "scikit-learn",
            "nlp",
            "natural-language-processing",
            "reinforcement-learning",
            "time-series",
            "anomaly-detection",
            "forecasting",
            "recommendation-system",
        }
    ),
    "awesome-list": frozenset({"awesome", "awesome-list", "list", "resources", "curated-list"}),
}

DEFAULT_CATEGORY: Final[str] = "llm-app"


def categorise(topics: set[str]) -> str | None:
    """The category the repo's topics point at most strongly.

    Weight of evidence, not first match. Real repositories carry a dozen topics
    and a first-match rule lets one stray tag decide: `huggingface/transformers`
    lists `speech-recognition` among its topics and landed in `audio-speech`,
    while seven of its other topics say classic ML. Counting fixes that, and
    priority order still breaks ties — which is what it was really for.
    """
    lowered = {t.lower() for t in topics}
    best: str | None = None
    best_score = 0
    for category in CATEGORIES:
        score = len(lowered & CATEGORY_TOPICS[category])
        # Strictly greater, so the earlier (more specific) category wins a tie.
        if score > best_score:
            best, best_score = category, score
    return best


def is_valid(category: str | None) -> bool:
    return category in CATEGORIES
