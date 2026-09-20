"""The paid tier of classification: Claude Haiku 4.5 over the Batch API.

Only the repos the rule engine could not settle reach this module — roughly the
band where a person would want to read the README before deciding.

Two cost decisions shape the design:

- **Several repos per request.** Haiku 4.5's minimum cacheable prefix is 4,096
  tokens; this system prompt is nowhere near that, so a `cache_control` marker
  would silently do nothing and every request would pay for the instructions
  again. Batching repos into one request amortises them instead, which is the
  saving prompt caching would have bought and does not depend on hitting a
  cache minimum.
- **The Batch API**, for its 50% discount. Classification is not latency
  sensitive: it runs weekly, and the boards are rebuilt afterwards.

Because one response covers several repos, the model echoes each repo's index
and the results are matched on it. Anything that comes back unmatched is
reported rather than quietly dropped — a silent misalignment would mislabel
whole batches.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from airadar.classify.taxonomy import CATEGORIES, is_valid

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
REPOS_PER_REQUEST = 15
MAX_TOKENS = 4000
POLL_INTERVAL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60

# Claude Haiku 4.5 list prices, halved by the Batch API.
INPUT_USD_PER_MTOK = 1.00
OUTPUT_USD_PER_MTOK = 5.00
BATCH_DISCOUNT = 0.5

SYSTEM_PROMPT = f"""You classify GitHub repositories for an AI-ecosystem tracker.

For each repository you are given, decide two things:

1. `is_ai` — does this project touch artificial intelligence in any way? Read
   this broadly: applications, agents, LLM tooling, model weights, training and
   inference infrastructure, RAG and vector search, classic ML and data
   science, computer vision, speech, robotics ML, AI developer tooling, and
   curated lists about any of the above all count as true.

   It is false for projects that merely mention AI in passing, use an AI
   service incidentally without AI being the point, or happen to share a name
   with an AI term. A monitoring `agent`, a web framework that lists an AI
   integration among fifty others, and a personal dotfiles repository are all
   false.

2. `category` — exactly one of: {", ".join(CATEGORIES)}.
   Choose the most specific one that fits. Use `llm-app` only when no narrower
   category applies. Use `awesome-list` for curated link collections. When
   `is_ai` is false, still return your best-guess category; it is ignored.

Also give a `subcategory` of one to three words in your own wording, a
`confidence` between 0 and 1 for the `is_ai` decision, and a `one_liner` of at
most 15 words describing what the project actually does.

Return one result per repository, echoing the `id` you were given. Judge only
from the text provided; if it is too thin to tell, say so with a low
confidence rather than guessing."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "is_ai": {"type": "boolean"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "subcategory": {"type": "string"},
                    "confidence": {"type": "number"},
                    "one_liner": {"type": "string"},
                },
                "required": [
                    "id",
                    "is_ai",
                    "category",
                    "subcategory",
                    "confidence",
                    "one_liner",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LLMInput:
    full_name: str
    description: str | None = None
    topics: tuple[str, ...] = ()
    language: str | None = None
    readme_excerpt: str = ""
    content_hash: str = ""

    def render(self, index: int) -> str:
        parts = [f"id: {index}", f"repo: {self.full_name}"]
        if self.language:
            parts.append(f"language: {self.language}")
        if self.topics:
            parts.append(f"topics: {', '.join(self.topics[:15])}")
        parts.append(f"description: {self.description or '(none)'}")
        if self.readme_excerpt:
            parts.append(f"readme:\n{self.readme_excerpt}")
        return "\n".join(parts)


@dataclass(frozen=True)
class LLMVerdict:
    full_name: str
    is_ai: bool
    category: str | None
    subcategory: str | None
    confidence: float
    one_liner: str | None

    @property
    def method(self) -> str:
        return "llm"


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errored: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        raw = (
            self.input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
            + self.output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
        )
        return round(raw * BATCH_DISCOUNT, 4)

    def summary(self) -> str:
        note = f", {len(self.unmatched)} unmatched" if self.unmatched else ""
        return (
            f"{self.requests} batch requests, {self.input_tokens:,} in / "
            f"{self.output_tokens:,} out tokens, ${self.cost_usd:.2f}"
            f"{', ' + str(self.errored) + ' errored' if self.errored else ''}{note}"
        )


def chunk(items: list[LLMInput], size: int = REPOS_PER_REQUEST) -> list[list[LLMInput]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_user_message(batch: list[LLMInput]) -> str:
    rendered = "\n\n---\n\n".join(item.render(i) for i, item in enumerate(batch))
    return f"Classify these {len(batch)} repositories.\n\n{rendered}"


def build_params(batch: list[LLMInput], *, model: str = DEFAULT_MODEL) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_message(batch)}],
        "output_config": {"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    }


def parse_results(text: str, batch: list[LLMInput]) -> tuple[list[LLMVerdict], list[str]]:
    """Match the model's output back to the repos it was asked about.

    Returns the verdicts it could match and the names it could not. An id the
    model invented, repeated or skipped is a misalignment, and mislabelling a
    repo is worse than leaving it for the next run.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("llm: unparseable response for a batch of %d", len(batch))
        return [], [item.full_name for item in batch]

    verdicts: list[LLMVerdict] = []
    claimed: set[int] = set()
    for entry in payload.get("results") or []:
        index = entry.get("id")
        if not isinstance(index, int) or not 0 <= index < len(batch) or index in claimed:
            continue
        claimed.add(index)
        category = entry.get("category")
        verdicts.append(
            LLMVerdict(
                full_name=batch[index].full_name,
                is_ai=bool(entry.get("is_ai")),
                category=category if is_valid(category) else None,
                subcategory=(entry.get("subcategory") or None),
                confidence=_clamp(entry.get("confidence")),
                one_liner=(entry.get("one_liner") or None),
            )
        )

    unmatched = [item.full_name for i, item in enumerate(batch) if i not in claimed]
    if unmatched:
        log.warning("llm: %d of %d repos unmatched in response", len(unmatched), len(batch))
    return verdicts, unmatched


def _clamp(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def estimate_cost_usd(
    repo_count: int,
    *,
    repos_per_request: int = REPOS_PER_REQUEST,
    system_tokens: int = 700,
    input_tokens_per_repo: int = 330,
    output_tokens_per_repo: int = 55,
) -> float:
    """What a sweep will cost before running it.

    The system prompt is charged once per request, not once per repo — which is
    exactly what batching repos together buys.
    """
    if repo_count <= 0:
        return 0.0
    requests = -(-repo_count // max(repos_per_request, 1))
    input_tokens = requests * system_tokens + repo_count * input_tokens_per_repo
    output_tokens = repo_count * output_tokens_per_repo
    raw = (
        input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
    )
    return round(raw * BATCH_DISCOUNT, 4)


class LLMClassifier:
    """Submits one Batch API job and collects its results."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        repos_per_request: int = REPOS_PER_REQUEST,
        client=None,
        sleep=time.sleep,
    ) -> None:
        self.model = model
        self.repos_per_request = repos_per_request
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key or None)

    def classify(
        self, inputs: list[LLMInput], *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> tuple[list[LLMVerdict], Usage]:
        usage = Usage()
        if not inputs:
            return [], usage

        batches = chunk(inputs, self.repos_per_request)
        batch_id = self._submit(batches)
        log.info("llm: submitted %d requests as batch %s", len(batches), batch_id)

        if not self._await_completion(batch_id, timeout_seconds):
            usage.unmatched = [item.full_name for item in inputs]
            log.error("llm: batch %s did not finish within the timeout", batch_id)
            return [], usage

        return self._collect(batch_id, batches, usage)

    def _submit(self, batches: list[list[LLMInput]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=f"batch-{i}",
                params=MessageCreateParamsNonStreaming(**build_params(batch, model=self.model)),
            )
            for i, batch in enumerate(batches)
        ]
        return self._client.messages.batches.create(requests=requests).id

    def _await_completion(self, batch_id: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            batch = self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return True
            self._sleep(POLL_INTERVAL_SECONDS)
        return False

    def _collect(
        self, batch_id: str, batches: list[list[LLMInput]], usage: Usage
    ) -> tuple[list[LLMVerdict], Usage]:
        by_custom_id = {f"batch-{i}": batch for i, batch in enumerate(batches)}
        verdicts: list[LLMVerdict] = []
        seen: set[str] = set()

        # Results arrive in any order, so they are keyed by custom_id, never by
        # position.
        for result in self._client.messages.batches.results(batch_id):
            batch = by_custom_id.get(result.custom_id)
            if batch is None:
                log.warning("llm: unknown custom_id %s in results", result.custom_id)
                continue
            seen.add(result.custom_id)
            usage.requests += 1

            if getattr(result.result, "type", None) != "succeeded":
                usage.errored += 1
                usage.unmatched.extend(item.full_name for item in batch)
                continue

            message = result.result.message
            usage.input_tokens += getattr(message.usage, "input_tokens", 0) or 0
            usage.output_tokens += getattr(message.usage, "output_tokens", 0) or 0

            text = next((b.text for b in message.content if b.type == "text"), "")
            matched, unmatched = parse_results(text, batch)
            verdicts.extend(matched)
            usage.unmatched.extend(unmatched)

        for custom_id, batch in by_custom_id.items():
            if custom_id not in seen:
                usage.unmatched.extend(item.full_name for item in batch)

        return verdicts, usage
