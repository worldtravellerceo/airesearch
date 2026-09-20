"""LLM classifier tests with a fake Anthropic client.

The failure worth guarding against is misalignment: one response covers many
repositories, so a wrong id mapping mislabels a whole batch silently. Every
path that cannot confidently match a result must report the repo as unmatched
instead of assigning it something.
"""

import json
from dataclasses import dataclass, field

import pytest

from airadar.classify.llm import (
    LLMClassifier,
    LLMInput,
    build_params,
    build_user_message,
    chunk,
    estimate_cost_usd,
    parse_results,
)
from airadar.classify.taxonomy import CATEGORIES


def make_input(name, **kwargs):
    return LLMInput(full_name=name, **kwargs)


# --- request construction --------------------------------------------------


def test_chunking_amortises_the_system_prompt():
    items = [make_input(f"o/r{i}") for i in range(35)]
    batches = chunk(items, 15)
    assert [len(b) for b in batches] == [15, 15, 5]


def test_user_message_numbers_every_repo():
    batch = [make_input("a/one", description="first"), make_input("b/two", description="second")]
    message = build_user_message(batch)

    assert "id: 0" in message and "a/one" in message
    assert "id: 1" in message and "b/two" in message


def test_params_constrain_the_output_to_the_taxonomy():
    params = build_params([make_input("a/b")], model="claude-haiku-4-5")
    schema = params["output_config"]["format"]["schema"]
    category = schema["properties"]["results"]["items"]["properties"]["category"]

    assert params["model"] == "claude-haiku-4-5"
    assert category["enum"] == list(CATEGORIES)
    # No thinking configuration: classification does not need it and it is billed.
    assert "thinking" not in params


def test_readme_excerpt_is_included_when_present():
    with_readme = make_input("a/b", readme_excerpt="An agent runtime for tools.")
    without = make_input("a/b")

    assert "readme:" in with_readme.render(0)
    assert "readme:" not in without.render(0)


# --- response parsing ------------------------------------------------------


def _response(entries) -> str:
    return json.dumps({"results": entries})


def test_results_are_matched_back_by_id():
    batch = [make_input("a/one"), make_input("b/two")]
    text = _response(
        [
            {
                "id": 1,
                "is_ai": True,
                "category": "mcp",
                "subcategory": "server",
                "confidence": 0.9,
                "one_liner": "An MCP server.",
            },
            {
                "id": 0,
                "is_ai": False,
                "category": "llm-app",
                "subcategory": "none",
                "confidence": 0.1,
                "one_liner": "A web framework.",
            },
        ]
    )

    verdicts, unmatched = parse_results(text, batch)

    by_name = {v.full_name: v for v in verdicts}
    assert unmatched == []
    assert by_name["b/two"].is_ai is True
    assert by_name["b/two"].category == "mcp"
    assert by_name["a/one"].is_ai is False


def test_a_skipped_repo_is_reported_not_invented():
    batch = [make_input("a/one"), make_input("b/two"), make_input("c/three")]
    text = _response(
        [
            {
                "id": 0,
                "is_ai": True,
                "category": "mcp",
                "subcategory": "s",
                "confidence": 0.9,
                "one_liner": "x",
            }
        ]
    )

    verdicts, unmatched = parse_results(text, batch)

    assert [v.full_name for v in verdicts] == ["a/one"]
    assert sorted(unmatched) == ["b/two", "c/three"]


def test_duplicate_and_out_of_range_ids_are_discarded():
    batch = [make_input("a/one")]
    text = _response(
        [
            {
                "id": 0,
                "is_ai": True,
                "category": "mcp",
                "subcategory": "s",
                "confidence": 0.9,
                "one_liner": "first",
            },
            {
                "id": 0,
                "is_ai": False,
                "category": "llm-app",
                "subcategory": "s",
                "confidence": 0.1,
                "one_liner": "contradiction",
            },
            {
                "id": 7,
                "is_ai": True,
                "category": "mcp",
                "subcategory": "s",
                "confidence": 0.9,
                "one_liner": "invented",
            },
        ]
    )

    verdicts, unmatched = parse_results(text, batch)

    assert len(verdicts) == 1
    assert verdicts[0].one_liner == "first"  # the first claim wins, the rest are dropped
    assert unmatched == []


def test_unparseable_response_loses_nothing_silently():
    batch = [make_input("a/one"), make_input("b/two")]
    verdicts, unmatched = parse_results("not json at all", batch)

    assert verdicts == []
    assert sorted(unmatched) == ["a/one", "b/two"]


def test_an_invalid_category_does_not_become_a_label():
    batch = [make_input("a/one")]
    text = _response(
        [
            {
                "id": 0,
                "is_ai": True,
                "category": "something-made-up",
                "subcategory": "s",
                "confidence": 0.9,
                "one_liner": "x",
            }
        ]
    )

    verdicts, _ = parse_results(text, batch)
    assert verdicts[0].category is None
    assert verdicts[0].is_ai is True


@pytest.mark.parametrize("value,expected", [(1.5, 1.0), (-2, 0.0), ("x", 0.0), (None, 0.0)])
def test_confidence_is_clamped(value, expected):
    batch = [make_input("a/one")]
    text = _response(
        [
            {
                "id": 0,
                "is_ai": True,
                "category": "mcp",
                "subcategory": "s",
                "confidence": value,
                "one_liner": "x",
            }
        ]
    )
    verdicts, _ = parse_results(text, batch)
    assert verdicts[0].confidence == expected


# --- batch lifecycle -------------------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 200


@dataclass
class FakeBlock:
    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    content: list
    usage: FakeUsage = field(default_factory=FakeUsage)


@dataclass
class FakeResult:
    type: str
    message: FakeMessage | None = None


@dataclass
class FakeRow:
    custom_id: str
    result: FakeResult


@dataclass
class FakeBatch:
    id: str = "batch_123"
    processing_status: str = "ended"


class FakeBatches:
    def __init__(self, rows, statuses=("ended",)):
        self.rows = rows
        self.statuses = list(statuses)
        self.created = None
        self.polls = 0

    def create(self, *, requests):
        self.created = requests
        return FakeBatch()

    def retrieve(self, batch_id):
        self.polls += 1
        status = self.statuses[min(self.polls - 1, len(self.statuses) - 1)]
        return FakeBatch(id=batch_id, processing_status=status)

    def results(self, batch_id):
        return iter(self.rows)


class FakeAnthropic:
    def __init__(self, batches):
        self.messages = type("M", (), {"batches": batches})()


def _ok_row(custom_id, entries, *, in_tok=1000, out_tok=200):
    return FakeRow(
        custom_id=custom_id,
        result=FakeResult(
            "succeeded",
            FakeMessage(
                content=[FakeBlock(_response(entries))],
                usage=FakeUsage(in_tok, out_tok),
            ),
        ),
    )


def test_classify_end_to_end_tracks_cost():
    inputs = [make_input("a/one"), make_input("b/two")]
    rows = [
        _ok_row(
            "batch-0",
            [
                {
                    "id": 0,
                    "is_ai": True,
                    "category": "agent-framework",
                    "subcategory": "s",
                    "confidence": 0.95,
                    "one_liner": "An agent framework.",
                },
                {
                    "id": 1,
                    "is_ai": False,
                    "category": "llm-app",
                    "subcategory": "s",
                    "confidence": 0.05,
                    "one_liner": "A web server.",
                },
            ],
            in_tok=2_000_000,
            out_tok=400_000,
        )
    ]
    batches = FakeBatches(rows)
    classifier = LLMClassifier(client=FakeAnthropic(batches), sleep=lambda s: None)

    verdicts, usage = classifier.classify(inputs)

    assert len(batches.created) == 1  # both repos fit in one request
    assert {v.full_name for v in verdicts} == {"a/one", "b/two"}
    assert usage.requests == 1
    # 2M in @ $1/MTok + 0.4M out @ $5/MTok = $4.00, halved by the Batch API.
    assert usage.cost_usd == pytest.approx(2.00)
    assert usage.unmatched == []


def test_an_errored_request_reports_its_repos_rather_than_dropping_them():
    inputs = [make_input("a/one")]
    rows = [FakeRow("batch-0", FakeResult("errored"))]
    classifier = LLMClassifier(client=FakeAnthropic(FakeBatches(rows)), sleep=lambda s: None)

    verdicts, usage = classifier.classify(inputs)

    assert verdicts == []
    assert usage.errored == 1
    assert usage.unmatched == ["a/one"]


def test_a_request_missing_from_the_results_is_also_reported():
    inputs = [make_input(f"o/r{i}") for i in range(20)]  # two requests
    rows = [
        _ok_row(
            "batch-0",
            [
                {
                    "id": i,
                    "is_ai": True,
                    "category": "mcp",
                    "subcategory": "s",
                    "confidence": 0.9,
                    "one_liner": "x",
                }
                for i in range(15)
            ],
        )
    ]
    classifier = LLMClassifier(client=FakeAnthropic(FakeBatches(rows)), sleep=lambda s: None)

    verdicts, usage = classifier.classify(inputs)

    assert len(verdicts) == 15
    assert sorted(usage.unmatched) == sorted(f"o/r{i}" for i in range(15, 20))


def test_classify_polls_until_the_batch_ends():
    inputs = [make_input("a/one")]
    rows = [
        _ok_row(
            "batch-0",
            [
                {
                    "id": 0,
                    "is_ai": True,
                    "category": "mcp",
                    "subcategory": "s",
                    "confidence": 0.9,
                    "one_liner": "x",
                }
            ],
        )
    ]
    batches = FakeBatches(rows, statuses=("in_progress", "in_progress", "ended"))
    classifier = LLMClassifier(client=FakeAnthropic(batches), sleep=lambda s: None)

    verdicts, _ = classifier.classify(inputs)

    assert batches.polls == 3
    assert len(verdicts) == 1


def test_a_batch_that_never_finishes_does_not_hang_forever():
    inputs = [make_input("a/one")]
    batches = FakeBatches([], statuses=("in_progress",))
    classifier = LLMClassifier(client=FakeAnthropic(batches), sleep=lambda s: None)

    verdicts, usage = classifier.classify(inputs, timeout_seconds=0)

    assert verdicts == []
    assert usage.unmatched == ["a/one"]


def test_empty_input_costs_nothing_and_calls_nothing():
    batches = FakeBatches([])
    classifier = LLMClassifier(client=FakeAnthropic(batches), sleep=lambda s: None)

    verdicts, usage = classifier.classify([])

    assert verdicts == []
    assert usage.cost_usd == 0.0
    assert batches.created is None


# --- budgeting -------------------------------------------------------------


def test_cost_estimate_reflects_batching():
    """The system prompt is charged per request, so batching is the saving."""
    unbatched = estimate_cost_usd(5_000, repos_per_request=1)
    batched = estimate_cost_usd(5_000, repos_per_request=15)

    assert batched < unbatched
    assert batched == pytest.approx(1.63, abs=0.05)  # the budget in the plan
    assert estimate_cost_usd(0) == 0.0


def test_cost_scales_linearly_with_repo_count():
    assert estimate_cost_usd(10_000) == pytest.approx(2 * estimate_cost_usd(5_000), rel=0.01)
