"""A coverage canary.

Counting repos is not coverage. The first real corpus held 64,373 repos and
was still missing `karpathy/nanoGPT`, `facebookresearch/faiss` and
`deepseek-ai/DeepSeek-R1`, because 98% of it came from a topic sweep and a
topic sweep cannot see a repo that has no topics. Nobody noticed, because
nothing ever checked.

This is that check: a hand-written list of projects that any index of AI work
must contain, compared against what the database actually holds. It is
deliberately independent of the pipeline — no topic, no search, no
classification — so it cannot fail in the same way discovery fails.

Two honest limits. The list is subjective, and it goes stale as projects are
renamed or transferred, which is why matching is on the repository name rather
than `owner/name` (`jax` moved from `google` to `jax-ml`; `DeepSpeed` from
`microsoft` to `deepspeedai`). It is a smoke test, not a measurement of the
whole universe: it says whether the famous end is intact, and the famous end
is the part that fails loudly when discovery has a blind spot.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Projects an index of AI work is expected to contain, written independently of
# anything the pipeline knows. Grouped only to make the list easier to maintain.
CANARIES: dict[str, tuple[str, ...]] = {
    "foundational": (
        "transformers",
        "pytorch",
        "tensorflow",
        "keras",
        "jax",
        "scikit-learn",
        "xgboost",
        "LightGBM",
    ),
    "serving-inference": (
        "vllm",
        "llama.cpp",
        "ollama",
        "sglang",
        "TensorRT-LLM",
        "text-generation-inference",
        "DeepSpeed",
        "flash-attention",
    ),
    "training": (
        "nanoGPT",
        "minGPT",
        "llm.c",
        "micrograd",
        "LLaMA-Factory",
        "unsloth",
        "peft",
        "trl",
        "ColossalAI",
    ),
    "agents-apps": (
        "langchain",
        "llama_index",
        "AutoGPT",
        "crewAI",
        "dspy",
        "aider",
        "OpenHands",
        "open-webui",
        "dify",
    ),
    "vision-audio": (
        "segment-anything",
        "whisper",
        "ComfyUI",
        "stable-diffusion-webui",
        "diffusers",
        "ultralytics",
        "PaddleOCR",
        "OpenVoice",
        "bark",
    ),
    "retrieval": (
        "faiss",
        "milvus",
        "qdrant",
        "chroma",
        "weaviate",
        "pgvector",
        "sentence-transformers",
    ),
    "models": (
        "DeepSeek-R1",
        "DeepSeek-V3",
        "ChatGLM",
        "Qwen",
        "mamba",
        "grok-1",
    ),
    "ecosystem": (
        "gradio",
        "streamlit",
        "mlflow",
        "ray",
        "pytorch-lightning",
        "litellm",
        "text-generation-webui",
    ),
}

# Below this, a name match is treated as a coincidence rather than the project
# itself — `mamba` matches a package manager, `triton` matches a whale survey.
MIN_STARS_FOR_A_MATCH = 3_000


@dataclass
class CoverageReport:
    found: list[tuple[str, str, int]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.found) + len(self.missing)

    @property
    def rate(self) -> float:
        return len(self.found) / self.total if self.total else 0.0

    def summary(self) -> str:
        return (
            f"{len(self.found)}/{self.total} canaries present ({self.rate:.0%}), "
            f"{len(self.missing)} missing"
        )


def audit_coverage(
    conn: sqlite3.Connection, *, min_stars: int = MIN_STARS_FOR_A_MATCH
) -> CoverageReport:
    """Check the canary list against the database.

    Matches on repository name, case-insensitively, because owners change and
    the project is the thing we care about. A match under `min_stars` is not
    counted: plenty of small repos share a famous name.
    """
    report = CoverageReport()
    for names in CANARIES.values():
        for name in names:
            row = conn.execute(
                """
                SELECT full_name, stars FROM repos
                WHERE lower(name) = lower(:name) AND stars >= :min_stars
                ORDER BY stars DESC LIMIT 1
                """,
                {"name": name, "min_stars": min_stars},
            ).fetchone()
            if row is None:
                # Fall back to a substring match: `ChatGLM` ships as
                # `ChatGLM-6B`, `Qwen` as `Qwen2.5`.
                row = conn.execute(
                    """
                    SELECT full_name, stars FROM repos
                    WHERE lower(name) LIKE lower(:like) AND stars >= :min_stars
                    ORDER BY stars DESC LIMIT 1
                    """,
                    {"like": f"{name}%", "min_stars": min_stars},
                ).fetchone()
            if row is None:
                report.missing.append(name)
            else:
                report.found.append((name, row["full_name"], row["stars"]))
    return report
