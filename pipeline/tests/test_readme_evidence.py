"""Reading what a repository says about itself.

The measurement behind this module: of the forty highest-star repositories
created since July 2026, fourteen scored zero on name, description and topics —
and a zero is recorded as "not AI", so they were invisible. `andrewyng/openworker`
has 18,090 stars and no description at all. Every README excerpt quoted here is
the real opening of the real repository, fetched on 2026-09-21.

The second thing under test is the restraint. A README is long and mentions
many things, and buying recall with precision is how this project previously
put 17,849 repositories in permanent limbo.
"""

import sqlite3

import httpx
import pytest

from airadar.classify import rules
from airadar.classify.rules import RepoFacts, classify
from airadar.db import repo as db
from airadar.gh.client import GitHubClient
from airadar.gh.content import fetch_readme

JEV = (
    "Jev Ultrafast. A browser agent with a dynamic, indexed action space. "
    "Give it one goal. TypeSafe's Jev picks an operation and an element. "
    "A small LLM writes text only when the operation is TYPE_TEXT."
)
OPENWORKER = (
    "OpenWorker. AI that gets your everyday tasks done. OpenWorker is an "
    "open-source AI coworker that lives on your desktop and delivers finished "
    "work, not just chat: your code reviewed for vulnerabilities with fixes "
    "ready to go, a polished document, a Slack reply with the numbers."
)
CURL = (
    "curl is a command-line tool and library for transferring data with URLs. "
    "curl supports DICT, FILE, FTP, FTPS, GOPHER, HTTP, HTTPS, IMAP and more. "
    "Note the HTTP user agent string can be set with -A."
)
# The trap. A templating tool that mentions an LLM it does not require.
SHIPIT = (
    "A small CLI for shipping release notes. It can optionally summarise your "
    "changelog using an LLM if you set OPENAI_API_KEY, but that is entirely "
    "optional and off by default. Everything else is plain templating."
)


async def _no_sleep(_seconds):  # pragma: no cover
    raise AssertionError("no throttling expected")


# --- recall ----------------------------------------------------------------


def test_the_readme_rescues_a_repo_its_metadata_could_not_place():
    """14,310 stars in five days. Its description is "i. am. speed."; the
    second line of its README is "A browser agent with a dynamic, indexed
    action space", which the vocabulary already knew how to read."""
    facts = RepoFacts(full_name="browser-use/jev-ultrafast", description="i. am. speed.")

    assert classify(facts).confidence == 0.0
    assert classify(facts).is_ai is False

    with_readme = classify(
        RepoFacts(
            full_name="browser-use/jev-ultrafast",
            description="i. am. speed.",
            readme_excerpt=JEV,
        )
    )
    assert with_readme.is_ai is True
    assert with_readme.category is not None


def test_a_repo_with_no_description_at_all_is_no_longer_invisible():
    """18,090 stars, Andrew Ng, and GitHub's description field is empty. The
    engine had literally nothing to read."""
    bare = classify(RepoFacts(full_name="andrewyng/openworker"))
    assert bare.confidence == 0.0
    assert bare.needs_llm is False  # a zero was settled as "not AI"

    read = classify(RepoFacts(full_name="andrewyng/openworker", readme_excerpt=OPENWORKER))
    assert read.needs_llm is True  # now it reaches a person


def test_short_words_are_matched_whole_and_never_as_substrings():
    """This is why `ai` and `agent` cannot go in the phrase lists: as
    substrings they match email, domain, training, explain and user-agent."""
    assert "ai" in rules.AI_TOKENS
    for innocent in ("email", "domain", "training", "explain", "chain"):
        assert classify(RepoFacts(full_name="acme/tool", readme_excerpt=innocent)).confidence == 0.0


# --- precision -------------------------------------------------------------


def test_one_passing_mention_of_an_llm_does_not_make_a_templating_tool_ai():
    """The trap, and the reason a README phrase is worth less than the same
    phrase in a description. At the first weight tried this scored 0.94 and
    would have put a release-notes CLI on an AI board."""
    verdict = classify(RepoFacts(full_name="releasenotes/shipit", readme_excerpt=SHIPIT))

    assert verdict.is_ai is False
    assert verdict.needs_llm is True  # not dismissed either — a person decides


def test_two_independent_readme_phrases_do_settle_it():
    """Restraint, not refusal. `jev-ultrafast` carries both "browser agent" and
    "LLM" and needs no human."""
    assert (
        classify(RepoFacts(full_name="browser-use/jev-ultrafast", readme_excerpt=JEV)).is_ai is True
    )


def test_the_word_agent_once_in_a_networking_readme_settles_nothing():
    """`curl` says "user agent". Repetition is the signal, not presence."""
    verdict = classify(RepoFacts(full_name="curl/curl", readme_excerpt=CURL))

    assert verdict.is_ai is False
    assert verdict.confidence < 0.2


def test_a_repeated_word_is_one_witness_not_six():
    """Noisy-OR assumes independent evidence. One author writing "agent" six
    times is not six independent observations, and counting it that way is how
    routine repos get pushed to the ceiling."""
    once = classify(RepoFacts(full_name="a/b", readme_excerpt="agent ai llm"))
    many = classify(RepoFacts(full_name="a/b", readme_excerpt="agent " * 40))

    assert many.confidence <= once.confidence
    assert len([k for k in many.matched if k.startswith("readme*:")]) == 1


def test_only_the_opening_of_a_readme_is_read():
    """A project says what it is at the top and lists integrations further
    down. `clean_readme` enforces the cut; this pins that classification
    honours it."""
    from airadar.gh.content import DEFAULT_EXCERPT_CHARS, clean_readme

    buried = clean_readme("A CSV parser. " + ("filler " * 500) + "browser agent")

    assert len(buried) <= DEFAULT_EXCERPT_CHARS
    assert classify(RepoFacts(full_name="a/b", readme_excerpt=buried)).is_ai is False


# --- the hashes ------------------------------------------------------------


def test_a_fetched_readme_does_not_invalidate_a_hand_made_verdict():
    """661 verdicts were made by reading repositories. A person who called a
    repo an agent framework did not become wrong when we later fetched its
    README, and `import_verdicts` validates against `inputs_hash`."""
    without = RepoFacts(full_name="a/b", description="x")
    with_readme = RepoFacts(full_name="a/b", description="x", readme_excerpt=JEV)

    assert without.inputs_hash() == with_readme.inputs_hash()


def test_a_fetched_readme_does_re_run_the_rule_engine():
    """The other half: the cache keys on `content_hash`, so new evidence has to
    move it or the repo keeps its old verdict forever."""
    without = RepoFacts(full_name="a/b", description="x")
    with_readme = RepoFacts(full_name="a/b", description="x", readme_excerpt=JEV)

    assert without.content_hash() != with_readme.content_hash()


# --- fetching --------------------------------------------------------------


async def test_an_unchanged_readme_costs_no_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == 'W/"r1"'
        return httpx.Response(304, headers={"x-ratelimit-remaining": "4999"})

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        result = await fetch_readme(client, "a/b", etag='W/"r1"')

    assert result.unchanged is True
    assert client.counters.calls == 0


async def test_a_repo_with_no_readme_is_a_finding_not_a_failure():
    """Recorded, so the same empty request is not paid for every run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"}, headers={})

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep
    ) as client:
        result = await fetch_readme(client, "a/b")

    assert result.missing is True
    assert result.excerpt == ""


def test_storing_an_absent_readme_still_records_that_we_asked(conn):
    db.upsert_repos(
        conn,
        [db.RepoRecord(id=1, full_name="a/b", owner="a", name="b", stars=5_000)],
    )
    db.set_readme(conn, 1, excerpt="", etag=None)
    conn.commit()

    row = conn.execute("SELECT readme_excerpt, readme_fetched_at FROM repos").fetchone()
    assert row["readme_excerpt"] is None
    assert row["readme_fetched_at"] is not None


# --- the migration ---------------------------------------------------------


def test_the_new_columns_reach_a_database_that_already_exists(tmp_path):
    """`schema.sql` is CREATE TABLE IF NOT EXISTS throughout, which does nothing
    for the database we already publish — and that release asset is the only
    copy there is. Without a migration the column is present in every test and
    absent in production."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, full_name TEXT)")
    old.commit()
    old.close()

    with db.connect(path) as conn:
        added = db._migrate_repos(conn)
        assert "readme_excerpt" in added
        assert db._migrate_repos(conn) == []  # idempotent


def test_the_readme_etag_column_is_writable(conn):
    db.upsert_repos(conn, [db.RepoRecord(id=1, full_name="a/b", owner="a", name="b", stars=1)])
    db.set_etag(conn, 1, column="etag_readme", etag='W/"x"')
    conn.commit()

    assert conn.execute("SELECT etag_readme FROM repos").fetchone()["etag_readme"] == 'W/"x"'

    with pytest.raises(ValueError):
        db.set_etag(conn, 1, column="stars", etag="nope")


# --- dependency evidence ---------------------------------------------------


def test_a_repo_that_imports_torch_is_a_machine_learning_project():
    """The one signal that does not care what the description says, or whether
    there is one, or what language it is in."""
    bare = classify(RepoFacts(full_name="acme/fastthing"))
    assert bare.confidence == 0.0

    with_dep = classify(RepoFacts(full_name="acme/fastthing", packages=("torch",)))
    assert with_dep.is_ai is True


def test_dependency_evidence_works_through_a_language_barrier():
    """Our vocabulary is English. A Chinese project describing itself perfectly
    well scores zero on every phrase list, and its imports do not."""
    facts = RepoFacts(full_name="acme/mox", description="一个快速的推理工具")

    assert classify(facts).confidence == 0.0
    assert (
        classify(
            RepoFacts(
                full_name="acme/mox", description="一个快速的推理工具", packages=("transformers",)
            )
        ).is_ai
        is True
    )


def test_the_same_package_seen_twice_is_counted_once():
    once = classify(RepoFacts(full_name="a/b", packages=("torch",)))
    twice = classify(RepoFacts(full_name="a/b", packages=("torch", "TORCH")))

    assert once.confidence == twice.confidence


def test_packages_re_run_the_engine_without_invalidating_a_hand_verdict():
    plain = RepoFacts(full_name="a/b", description="x")
    with_pkg = RepoFacts(full_name="a/b", description="x", packages=("torch",))

    assert plain.inputs_hash() == with_pkg.inputs_hash()
    assert plain.content_hash() != with_pkg.content_hash()


def test_package_links_round_trip_through_the_database(conn):
    from airadar.classify_run import load_facts

    db.upsert_repos(
        conn, [db.RepoRecord(id=1, full_name="acme/mox", owner="acme", name="mox", stars=4_000)]
    )
    db.record_packages(conn, {"acme/mox": {("pypi", "torch"), ("npm", "openai")}})
    conn.commit()

    facts, _ = load_facts(conn)

    assert sorted(facts[0].packages) == ["openai", "torch"]
    assert classify(facts[0]).is_ai is True


# --- the vocabulary that had to be split -----------------------------------


def test_the_agent_ecosystem_is_recognised():
    """A 160-repository sample, labelled by hand across four star bands and
    three age cohorts, said the engine caught 66% of the AI projects in it.
    Eighteen of the thirty-four misses — 53% — were one category: the words
    this ecosystem started using after the vocabulary was written."""
    for name, description in [
        ("msitarzewski/agency-agents", "A complete AI agency at your fingertips"),
        ("karpathy/nanoGPT", "The simplest, fastest repository for training/finetuning GPT"),
        ("mattpocock/skills", "A collection of agent skills"),
        ("anysphere/priompt", "Prompt design library"),
    ]:
        assert classify(RepoFacts(full_name=name, description=description)).is_ai, name


def test_a_monitoring_daemon_is_not_an_ai_agent():
    """The measurement that produced the tier above said putting `agent` at
    decisive weight cost no precision. The measurement was wrong — the sample
    simply contained no monitoring daemon. Checked afterwards against ten
    well-known non-AI projects, `DataDog/datadog-agent` and
    `newrelic/newrelic-java-agent` both came back as AI.

    The genuinely ambiguous words now settle nothing on their own and land in
    review, while any corroborating signal still carries a real one over."""
    for name, description in [
        ("DataDog/datadog-agent", "Main repository for Datadog Agent"),
        ("newrelic/newrelic-java-agent", "The New Relic Java agent"),
        ("harness/harness", "An end-to-end developer platform with CI/CD"),
    ]:
        verdict = classify(RepoFacts(full_name=name, description=description))
        assert verdict.is_ai is False, name
        assert verdict.needs_llm is True, name  # a person decides, not silence


def test_short_vendor_names_are_matched_whole():
    """Measured: as a substring, `grok` matches `ngrok`, and a tunnelling tool
    became an AI project."""
    assert (
        classify(RepoFacts(full_name="outray/outray", description="ngrok alternative")).is_ai
        is False
    )
    assert classify(RepoFacts(full_name="acme/tool", description="A Grok client")).is_ai is True


def test_a_project_that_describes_itself_in_chinese_is_not_invisible():
    """Our phrase lists are English. Tokenising on [a-z0-9] deletes these
    characters entirely, so they are matched against the raw description."""
    assert (
        classify(RepoFacts(full_name="acme/tool", description="一个基于大模型的智能体框架")).is_ai
        is True
    )


def test_the_field_had_a_vocabulary_before_it_was_called_ai():
    """The second-largest category of misses: projects whose subject is
    unmistakable to a reader and invisible to a keyword list."""
    for description in [
        "Open-source vector similarity search for Postgres",
        "A world model trained on driving footage",
        "Knowledge distillation toolkit",
    ]:
        assert classify(RepoFacts(full_name="acme/thing", description=description)).is_ai, (
            description
        )
