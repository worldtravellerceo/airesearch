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
        "LlamaFactory",
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
        "textgen",
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


@dataclass
class FreshnessReport:
    """How much of the tracked universe actually got refreshed."""

    tier1_total: int = 0
    tier1_stale: int = 0
    tier2_total: int = 0
    tier2_stale: int = 0
    never_checked: int = 0

    @property
    def stale(self) -> int:
        return self.tier1_stale + self.tier2_stale

    @property
    def total(self) -> int:
        return self.tier1_total + self.tier2_total

    def summary(self) -> str:
        fresh = self.total - self.stale
        rate = fresh / self.total if self.total else 0.0
        return (
            f"{fresh}/{self.total} tracked repos fresh ({rate:.0%}); "
            f"tier1 {self.tier1_stale} stale, tier2 {self.tier2_stale} stale, "
            f"{self.never_checked} never checked"
        )


def audit_freshness(
    conn: sqlite3.Connection, *, tier1_size: int, track_limit: int, now
) -> FreshnessReport:
    """Count tracked repos whose metrics are older than their tier allows.

    The companion to the coverage canary, for the same class of failure. A
    collect run that cannot finish does not fail — it walks the star order
    until the job is killed and reports the part it managed as though it were
    the whole. The tail simply stops being refreshed, and nothing says so.
    Stale counts make that visible in one number.
    """
    import datetime as _dt

    from airadar.db.repo import TRACKED_UNIVERSE_SQL

    row = conn.execute(
        f"""
        WITH ranked AS ({TRACKED_UNIVERSE_SQL})
        SELECT
          sum(star_rank <= :tier1) AS t1,
          sum(star_rank <= :tier1 AND (last_checked_at IS NULL
              OR last_checked_at < :daily_cutoff)) AS t1_stale,
          sum(star_rank >  :tier1) AS t2,
          sum(star_rank >  :tier1 AND (last_checked_at IS NULL
              OR last_checked_at < :weekly_cutoff)) AS t2_stale,
          sum(last_checked_at IS NULL) AS never
        FROM ranked WHERE star_rank <= :track_limit
        """,
        {
            "tier1": tier1_size,
            "track_limit": track_limit,
            # Deliberately looser than the collect queue's 20 hours and 7 days.
            # A repo becomes *due* before it becomes *stale*; without the grace
            # period every repo waiting its turn in a run that is still going
            # would be counted as a failure.
            "daily_cutoff": now - _dt.timedelta(hours=28),
            "weekly_cutoff": now - _dt.timedelta(days=8),
        },
    ).fetchone()

    return FreshnessReport(
        tier1_total=row["t1"] or 0,
        tier1_stale=row["t1_stale"] or 0,
        tier2_total=row["t2"] or 0,
        tier2_stale=row["t2_stale"] or 0,
        never_checked=row["never"] or 0,
    )


# --- completeness against ground truth -------------------------------------


@dataclass
class BucketCoverage:
    low: int
    high: int
    on_github: int
    in_corpus: int

    @property
    def rate(self) -> float:
        return self.in_corpus / self.on_github if self.on_github else 1.0

    @property
    def missing(self) -> int:
        return max(0, self.on_github - self.in_corpus)


@dataclass
class CompletenessReport:
    buckets: list[BucketCoverage] = field(default_factory=list)

    @property
    def on_github(self) -> int:
        return sum(b.on_github for b in self.buckets)

    @property
    def in_corpus(self) -> int:
        return sum(b.in_corpus for b in self.buckets)

    @property
    def rate(self) -> float:
        return self.in_corpus / self.on_github if self.on_github else 1.0

    def summary(self) -> str:
        return (
            f"{self.in_corpus:,}/{self.on_github:,} of GitHub above the census "
            f"floor ({self.rate:.1%}), {self.on_github - self.in_corpus:,} missing"
        )


# Star ranges to check. Chosen so each is small enough that a miss shows up as a
# visible percentage rather than being diluted by the size of the band.
COMPLETENESS_BUCKETS: tuple[tuple[int, int], ...] = (
    (1_000, 1_500),
    (1_500, 2_500),
    (2_500, 5_000),
    (5_000, 10_000),
    (10_000, 25_000),
    (25_000, 100_000),
    (100_000, 100_000_000),
)


async def audit_completeness(
    conn: sqlite3.Connection, client, *, buckets=COMPLETENESS_BUCKETS
) -> CompletenessReport:
    """Compare the corpus against GitHub's own count, bucket by bucket.

    This is the only check that can answer "is anything missing", because it is
    the only one that asks something outside the pipeline. The canary list says
    the famous projects are present and the census says it enumerated the star
    range, but both are the pipeline grading its own homework. GitHub's search
    endpoint returns `total_count` for any query, which is ground truth for how
    many public non-fork repos exist in a star range — one request per bucket.

    It only covers the range the census guarantees. Below that floor discovery
    is heuristic and this would report a gap that is expected rather than a
    fault, which would train everyone to ignore the number.
    """
    report = CompletenessReport()
    for low, high in buckets:
        query = f"fork:false stars:{low}..{high}"
        response = await client.search_repositories(query, page=1, per_page=1)
        on_github = int((response.data or {}).get("total_count", 0))
        in_corpus = conn.execute(
            "SELECT count(*) AS n FROM repos WHERE is_fork = 0 AND stars >= ? AND stars <= ?",
            (low, high),
        ).fetchone()["n"]
        report.buckets.append(BucketCoverage(low, high, on_github, in_corpus))
    return report
