"""Classification orchestration against a real database."""

import datetime as dt
import json
from dataclasses import dataclass, field

import httpx

from airadar import classify_run
from airadar.db import repo as db
from airadar.gh.client import GitHubClient

HEADERS = {
    "x-ratelimit-remaining": "4999",
    "x-ratelimit-reset": str(int(dt.datetime.now().timestamp()) + 3600),
}


async def _no_sleep(_seconds):
    return None


def add_repo(conn, repo_id, full_name, description, topics=(), stars=500):
    owner, name = full_name.split("/")
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=repo_id,
                full_name=full_name,
                owner=owner,
                name=name,
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
                description=description,
                language="Python",
                stars=stars,
                topics=tuple(topics),
            )
        ],
    )
    conn.commit()


def readme_handler(text="An internal tool for something."):
    def handler(request: httpx.Request) -> httpx.Response:
        import base64

        return httpx.Response(
            200,
            json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()},
            headers=HEADERS,
        )

    return handler


# --- fake Anthropic --------------------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int = 5_000
    output_tokens: int = 500


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
    id: str = "batch_1"
    processing_status: str = "ended"


class FakeBatches:
    def __init__(self, entries_by_custom_id):
        self.entries = entries_by_custom_id
        self.submitted_messages: list[str] = []

    def create(self, *, requests):
        for request in requests:
            self.submitted_messages.append(request["params"]["messages"][0]["content"])
        return FakeBatch()

    def retrieve(self, batch_id):
        return FakeBatch(id=batch_id)

    def results(self, batch_id):
        return iter(
            FakeRow(
                custom_id,
                FakeResult(
                    "succeeded",
                    FakeMessage(content=[FakeBlock(json.dumps({"results": entries}))]),
                ),
            )
            for custom_id, entries in self.entries.items()
        )


class FakeAnthropic:
    def __init__(self, batches):
        self.messages = type("M", (), {"batches": batches})()


def patch_classifier(monkeypatch, batches):
    real_init = classify_run.LLMClassifier.__init__

    def fake_init(self, *, api_key=None, model="m", repos_per_request=15, client=None, sleep=None):
        real_init(
            self,
            model=model,
            repos_per_request=repos_per_request,
            client=FakeAnthropic(batches),
            sleep=lambda s: None,
        )

    monkeypatch.setattr(classify_run.LLMClassifier, "__init__", fake_init)


# --- tests -----------------------------------------------------------------


async def test_rule_settled_repos_never_reach_the_model(conn, monkeypatch):
    add_repo(conn, 1, "langchain-ai/langchain", "Build LLM applications", ["llm", "agents"])
    add_repo(conn, 2, "expressjs/express", "Minimalist web framework", ["nodejs"])
    batches = FakeBatches({})
    patch_classifier(monkeypatch, batches)

    def handler(request):  # pragma: no cover - no README should be fetched
        raise AssertionError("no repo should need its README read")

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        report = await classify_run.classify_all(conn, client)

    assert report.settled_by_rules == 2
    assert report.escalated == 0
    assert batches.submitted_messages == []

    rows = {r["repo_id"]: r for r in conn.execute("SELECT * FROM repo_classification").fetchall()}
    assert rows[1]["is_ai"] is True
    assert rows[1]["method"] == "rules"
    assert rows[2]["is_ai"] is False


async def test_ambiguous_repos_get_their_readme_read_and_go_to_the_model(conn, monkeypatch):
    add_repo(conn, 1, "acme/agent", "A lightweight agent", ["agent"])
    batches = FakeBatches(
        {
            "batch-0": [
                {
                    "id": 0,
                    "is_ai": False,
                    "category": "llm-app",
                    "subcategory": "monitoring",
                    "confidence": 0.9,
                    "one_liner": "A server monitoring daemon.",
                }
            ]
        }
    )
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t",
        transport=httpx.MockTransport(readme_handler("A daemon that reports server metrics.")),
        sleep=_no_sleep,
    ) as client:
        report = await classify_run.classify_all(conn, client)

    assert report.escalated == 1
    assert report.classified_by_llm == 1
    # The README made it into the prompt — that is the whole reason to fetch it.
    assert "reports server metrics" in batches.submitted_messages[0]

    row = conn.execute("SELECT * FROM repo_classification").fetchone()
    assert row["is_ai"] is False
    assert row["method"] == "llm"
    assert row["one_liner"] == "A server monitoring daemon."


async def test_unchanged_repos_are_not_reclassified(conn, monkeypatch):
    add_repo(conn, 1, "acme/agent", "A lightweight agent", ["agent"])
    batches = FakeBatches(
        {
            "batch-0": [
                {
                    "id": 0,
                    "is_ai": True,
                    "category": "agent-framework",
                    "subcategory": "runtime",
                    "confidence": 0.8,
                    "one_liner": "An agent runtime.",
                }
            ]
        }
    )
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        first = await classify_run.classify_all(conn, client)
        second = await classify_run.classify_all(conn, client)

    assert first.classified_by_llm == 1
    assert second.cached == 1
    assert second.escalated == 0
    assert len(batches.submitted_messages) == 1  # the second run spent nothing


async def test_a_changed_description_invalidates_the_cache(conn, monkeypatch):
    add_repo(conn, 1, "acme/agent", "A lightweight agent", ["agent"])
    batches = FakeBatches(
        {
            "batch-0": [
                {
                    "id": 0,
                    "is_ai": True,
                    "category": "agent-framework",
                    "subcategory": "runtime",
                    "confidence": 0.8,
                    "one_liner": "x",
                }
            ]
        }
    )
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        await classify_run.classify_all(conn, client)
        conn.execute("UPDATE repos SET description = 'Now an LLM agent runtime' WHERE id = 1")
        conn.commit()
        second = await classify_run.classify_all(conn, client)

    assert second.cached == 0
    assert second.settled_by_rules == 1  # the new description settles it for free


async def test_unmatched_repos_are_left_uncached_so_the_next_run_retries(conn, monkeypatch):
    """Caching a repo the model never answered for would strand it forever."""
    add_repo(conn, 1, "acme/agent", "A lightweight agent", ["agent"])
    batches = FakeBatches({"batch-0": []})  # model answered about nothing
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        report = await classify_run.classify_all(conn, client)

    assert report.unmatched == ["acme/agent"]
    assert conn.execute("SELECT count(*) AS n FROM repo_classification").fetchone()["n"] == 0

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        again = await classify_run.classify_all(conn, client)
    assert again.escalated == 1  # picked up again rather than stranded


async def test_dry_run_reports_the_cost_without_spending_it(conn, monkeypatch):
    for i in range(30):
        add_repo(conn, i + 1, f"acme/agent{i}", "A lightweight agent", ["agent"])
    batches = FakeBatches({})
    patch_classifier(monkeypatch, batches)

    def handler(request):  # pragma: no cover
        raise AssertionError("a dry run must not fetch anything")

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        report = await classify_run.classify_all(conn, client, dry_run=True)

    assert report.escalated == 30
    assert report.estimated_cost_usd > 0
    assert batches.submitted_messages == []
    assert conn.execute("SELECT count(*) AS n FROM repo_classification").fetchone()["n"] == 0


async def test_no_llm_mode_writes_only_what_rules_decided(conn, monkeypatch):
    add_repo(conn, 1, "langchain-ai/langchain", "Build LLM applications", ["llm"])
    add_repo(conn, 2, "acme/agent", "A lightweight agent", ["agent"])
    batches = FakeBatches({})
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        report = await classify_run.classify_all(conn, client, use_llm=False)

    assert report.settled_by_rules == 1
    assert report.escalated == 1
    assert report.classified_by_llm == 0
    names = {
        r["full_name"]
        for r in conn.execute(
            "SELECT r.full_name FROM repos r JOIN repo_classification c ON c.repo_id = r.id"
        )
    }
    assert names == {"langchain-ai/langchain"}


async def test_max_llm_caps_spend(conn, monkeypatch):
    for i in range(40):
        add_repo(conn, i + 1, f"acme/agent{i}", "A lightweight agent", ["agent"])
    batches = FakeBatches({})
    patch_classifier(monkeypatch, batches)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(readme_handler()), sleep=_no_sleep
    ) as client:
        report = await classify_run.classify_all(conn, client, max_llm_repos=5)

    # One request per 15 repos, so five escalations is a single request.
    assert len(batches.submitted_messages) == 1
    assert report.escalated == 40  # reported honestly, even though only 5 were sent
