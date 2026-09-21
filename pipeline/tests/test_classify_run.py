"""Classification orchestration against a real database."""

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

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


# --- reviewed verdicts -----------------------------------------------------


def _verdict_file(path, conn, full_name, *, is_ai, category, stale=False):
    facts, _ = classify_run.load_facts(conn)
    inputs_hash = next(f.inputs_hash() for f in facts if f.full_name == full_name)
    path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "full_name": full_name,
                        "inputs_hash": "0" * 32 if stale else inputs_hash,
                        "is_ai": is_ai,
                        "category": category,
                        "confidence": 0.92,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_verdicts_import_from_a_directory_of_files(conn, tmp_path):
    """The verdicts live in the repository as a directory, so that a judgement
    made once survives the database — which is a release asset, not a file."""
    add_repo(conn, 1, "acme/one", "A tool", ["agent"])
    add_repo(conn, 2, "acme/two", "Another tool", ["mcp"])
    conn.commit()

    folder = tmp_path / "verdicts"
    folder.mkdir()
    _verdict_file(folder / "a.json", conn, "acme/one", is_ai=True, category="agent-framework")
    _verdict_file(folder / "b.json", conn, "acme/two", is_ai=False, category=None)

    report = classify_run.import_verdicts(conn, folder)

    assert report.imported == 2
    rows = {
        row["full_name"]: row
        for row in conn.execute(
            """SELECT r.full_name, c.is_ai, c.category
                 FROM repos r JOIN repo_classification c ON c.repo_id = r.id"""
        )
    }
    assert rows["acme/one"]["is_ai"] == 1
    assert rows["acme/one"]["category"] == "agent-framework"
    assert rows["acme/two"]["is_ai"] == 0


def test_a_verdict_about_a_changed_repo_is_skipped_not_applied(conn, tmp_path):
    """A stale label is worse than no label: no label gets looked at again."""
    add_repo(conn, 1, "acme/one", "A tool", ["agent"])
    conn.commit()

    folder = tmp_path / "verdicts"
    folder.mkdir()
    _verdict_file(
        folder / "a.json", conn, "acme/one", is_ai=True, category="agent-framework", stale=True
    )

    report = classify_run.import_verdicts(conn, folder)

    assert report.imported == 0
    assert report.unmatched == ["acme/one"]
    assert conn.execute("SELECT count(*) AS n FROM repo_classification").fetchone()["n"] == 0


def test_an_empty_verdict_directory_is_not_an_error(conn, tmp_path):
    """The step runs on every classification run, including before anything
    has been judged."""
    folder = tmp_path / "verdicts"
    folder.mkdir()

    report = classify_run.import_verdicts(conn, folder)

    assert report.imported == 0


def test_a_rules_change_does_not_throw_away_a_hand_made_verdict(conn, monkeypatch):
    """Found in the field, the hard way. `RULES_VERSION` was folded into the
    hash a verdict is checked against, so adding one phrase to a list discarded
    all 661 verdicts that had been made by reading the repositories.

    A rule-engine verdict does expire when the rules change. A judgement made by
    reading a description does not: the repository did not move.
    """
    from airadar.classify import rules as rules_module

    add_repo(conn, 1, "acme/one", "A tool", ["agent"])
    conn.commit()

    path = tmp = conn  # placeholder to keep the name obvious below
    del path, tmp

    folder = Path(__import__("tempfile").mkdtemp())
    _verdict_file(folder / "a.json", conn, "acme/one", is_ai=True, category="agent-framework")

    original = rules_module.RULES_VERSION
    try:
        rules_module.RULES_VERSION = "999"
        report = classify_run.import_verdicts(conn, folder)
    finally:
        rules_module.RULES_VERSION = original

    assert report.imported == 1
    assert report.unmatched == []


# --- the weekly review queue -----------------------------------------------


def test_the_review_queue_holds_everything_the_engine_did_not_settle(conn, tmp_path):
    """Both kinds, and this test used to assert only one of them.

    A repo that scored zero was recorded as "not AI" — absence of evidence read
    as evidence of absence. `anomalyco/opencode` sat there at 208,847 stars
    with the description "The open source coding agent."

    A repo in the escalation band was worse off: with the LLM pass disabled,
    which is how the daily run works, nothing is written for it at all, and it
    was filtered out of this queue for scoring above zero. That made partial
    evidence strictly worse than none — a README that moved a repo from 0.00 to
    0.40 hid it from both the boards and the humans.
    """
    add_repo(conn, 1, "acme/silent", "A tool for teams", [], stars=9_000)
    add_repo(conn, 2, "acme/obvious", "An LLM agent framework", ["llm"], stars=8_000)
    add_repo(conn, 3, "acme/ambiguous", "A lightweight agent", ["agent"], stars=7_000)
    conn.commit()

    out = tmp_path / "queue.json"
    classify_run.export_review_queue(conn, out, limit=10, min_stars=1_000)
    names = [r["full_name"] for r in json.loads(out.read_text())["repos"]]

    assert names == ["acme/silent", "acme/ambiguous"]  # settled AI stays out


def test_the_queue_is_ordered_by_stars_and_reports_what_is_left(conn, tmp_path):
    """Biggest first, because that is the order in which a miss costs
    something, and capped because this is worked through a slice at a time."""
    for repo_id, stars in enumerate([500, 5_000, 90_000, 20_000], start=1):
        add_repo(conn, repo_id, f"acme/r{repo_id}", "A tool for teams", [], stars=stars)
    conn.commit()

    out = tmp_path / "queue.json"
    classify_run.export_review_queue(conn, out, limit=2, min_stars=1_000)
    payload = json.loads(out.read_text())

    assert [r["full_name"] for r in payload["repos"]] == ["acme/r3", "acme/r4"]
    assert payload["remaining_after_this_slice"] == 1  # r2; r1 is under the floor


def test_repos_already_judged_are_not_handed_back_next_week(conn, tmp_path):
    """The verdict files are the record of what has been looked at, not the
    database: a rules re-run rewrites the stored method, so the database
    forgets and the same repos come round again every week."""
    add_repo(conn, 1, "acme/seen", "A tool for teams", [], stars=9_000)
    add_repo(conn, 2, "acme/unseen", "Another tool for teams", [], stars=8_000)
    conn.commit()

    verdicts = tmp_path / "verdicts"
    verdicts.mkdir()
    (verdicts / "old.json").write_text(
        json.dumps({"repos": [{"full_name": "acme/seen", "is_ai": False}]}),
        encoding="utf-8",
    )

    out = tmp_path / "queue.json"
    classify_run.export_review_queue(conn, out, limit=10, min_stars=1_000, verdicts_dir=verdicts)
    names = [r["full_name"] for r in json.loads(out.read_text())["repos"]]

    assert names == ["acme/unseen"]


def test_the_queue_carries_the_hash_a_verdict_is_validated_against(conn, tmp_path):
    """So a slice round-trips: export, judge, import, without a second lookup."""
    add_repo(conn, 1, "acme/silent", "A tool for teams", [], stars=9_000)
    conn.commit()

    out = tmp_path / "queue.json"
    classify_run.export_review_queue(conn, out, limit=10, min_stars=1_000)
    entry = json.loads(out.read_text())["repos"][0]

    facts, _ = classify_run.load_facts(conn)
    expected = next(f.inputs_hash() for f in facts if f.full_name == "acme/silent")
    assert entry["inputs_hash"] == expected


def test_the_escalation_band_reaches_the_review_queue(conn, tmp_path):
    """Partial evidence must not be worse than none. With the LLM pass off —
    which is how the daily run works — nothing is written for an escalated
    repo: it is on no board, and it used to be filtered out of this queue for
    scoring above zero. So a README that moved a repo from 0.00 to 0.40 hid it
    completely."""
    from airadar.classify import rules
    from airadar.classify_run import export_review_queue

    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="andrewyng/openworker",
                owner="andrewyng",
                name="openworker",
                stars=18_090,
            ),
            db.RepoRecord(
                id=2, full_name="acme/nothing", owner="acme", name="nothing", stars=9_000
            ),
        ],
    )
    conn.execute(
        "UPDATE repos SET readme_excerpt = ? WHERE id = 1",
        ("OpenWorker. AI that gets your everyday tasks done, an open-source AI coworker.",),
    )
    conn.commit()

    facts, _ = __import__("airadar.classify_run", fromlist=["load_facts"]).load_facts(conn)
    scored = {f.full_name: rules.classify(f) for f in facts}
    assert 0 < scored["andrewyng/openworker"].confidence < 0.8  # the escalation band
    assert scored["acme/nothing"].confidence == 0.0

    out = tmp_path / "queue.json"
    report = export_review_queue(conn, out, limit=10, min_stars=1_000)

    names = [row["full_name"] for row in json.loads(out.read_text())["repos"]]
    assert "andrewyng/openworker" in names
    assert "acme/nothing" in names
    assert report.considered == 2


def test_a_settled_ai_repo_is_not_queued_for_review(conn, tmp_path):
    from airadar.classify_run import export_review_queue

    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="acme/agent",
                owner="acme",
                name="agent",
                description="an agent framework for llm applications",
                stars=9_000,
            )
        ],
    )
    conn.commit()

    out = tmp_path / "queue.json"
    export_review_queue(conn, out, limit=10, min_stars=1_000)

    assert json.loads(out.read_text())["repos"] == []
