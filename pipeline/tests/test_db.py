"""Data access layer tests.

The store is a SQLite file, so every one of these runs everywhere — there is
no "skipped because no database was reachable" hole for a regression to hide
in.
"""

import datetime as dt

import pytest

from airadar.db import repo as db
from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import Entry
from airadar.scoring.metrics import RepoMetrics

TODAY = dt.date(2026, 9, 20)
NOW = dt.datetime(2026, 9, 20, 6, 0, tzinfo=dt.UTC)


def api_payload(repo_id: int, full_name: str, stars: int = 100, **overrides) -> dict:
    owner, name = full_name.split("/")
    payload = {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "created_at": "2024-01-15T10:00:00Z",
        "description": "an ai thing",
        "homepage": "",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "archived": False,
        "fork": False,
        "stargazers_count": stars,
        "topics": ["llm", "agents"],
    }
    payload.update(overrides)
    return payload


def test_schema_is_reapplicable(conn):
    db.apply_schema(conn)  # must not raise on a populated database


def test_upsert_is_idempotent_and_refreshes_mutable_fields(conn):
    record = db.RepoRecord.from_api(api_payload(1, "acme/agent"), discovered_via="topic-bucket")
    db.upsert_repos(conn, [record])

    grown = db.RepoRecord.from_api(api_payload(1, "acme/agent", stars=999, archived=True))
    db.upsert_repos(conn, [grown])

    rows = conn.execute("SELECT * FROM repos").fetchall()
    assert len(rows) == 1
    assert rows[0]["stars"] == 999
    assert rows[0]["archived"] is True
    # First-sighting provenance survives later refreshes.
    assert rows[0]["discovered_via"] == "topic-bucket"
    topics = {r["topic"] for r in conn.execute("SELECT topic FROM repo_topics")}
    assert topics == {"llm", "agents"}


def test_renamed_repo_follows_its_numeric_id(conn):
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(7, "old/name"))])
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(7, "new/name"))])

    rows = conn.execute("SELECT full_name FROM repos").fetchall()
    assert [r["full_name"] for r in rows] == ["new/name"]


def test_star_daily_restates_rather_than_accumulates(conn):
    """The API refines the current week as it fills in; re-collecting the same
    day must overwrite, not double-count."""
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "acme/agent"))])

    db.record_star_daily(conn, 1, [DailyStars(TODAY, 5)])
    db.record_star_daily(conn, 1, [DailyStars(TODAY, 9)])
    conn.commit()

    series = db.load_daily_series(conn, 1)
    assert series == [DailyStars(TODAY, 9)]


def test_load_daily_series_respects_since(conn):
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "acme/agent"))])
    db.record_star_daily(conn, 1, [DailyStars(TODAY - dt.timedelta(days=i), i) for i in range(10)])
    conn.commit()

    recent = db.load_daily_series(conn, 1, since=TODAY - dt.timedelta(days=2))
    assert [d.date for d in recent] == [
        TODAY - dt.timedelta(days=2),
        TODAY - dt.timedelta(days=1),
        TODAY,
    ]


def test_refresh_queue_puts_never_checked_repos_first(conn):
    db.upsert_repos(
        conn,
        [
            db.RepoRecord.from_api(api_payload(1, "a/big", stars=90_000)),
            db.RepoRecord.from_api(api_payload(2, "b/small", stars=60)),
        ],
    )
    due = db.repos_due_for_refresh(conn, tier1_size=1, now=NOW)
    assert {r["id"] for r in due} == {1, 2}


def test_tier2_repos_are_refreshed_weekly_not_daily(conn):
    db.upsert_repos(
        conn,
        [
            db.RepoRecord.from_api(api_payload(1, "a/big", stars=90_000)),
            db.RepoRecord.from_api(api_payload(2, "b/small", stars=60)),
        ],
    )
    yesterday = NOW - dt.timedelta(days=1)
    db.mark_checked(conn, 1, yesterday)
    db.mark_checked(conn, 2, yesterday)
    conn.commit()

    due = {r["id"] for r in db.repos_due_for_refresh(conn, tier1_size=1, now=NOW)}
    assert due == {1}  # tier-1 is due again, tier-2 is not

    db.mark_checked(conn, 2, NOW - dt.timedelta(days=8))
    conn.commit()
    due = {r["id"] for r in db.repos_due_for_refresh(conn, tier1_size=1, now=NOW)}
    assert due == {1, 2}


def test_forks_are_never_queued(conn):
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "a/fork", fork=True))])
    assert db.repos_due_for_refresh(conn, tier1_size=10, now=NOW) == []


def test_classification_cache_keys_on_content_hash(conn):
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "acme/agent"))])
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="agent-framework",
        subcategory=None,
        confidence=0.9,
        method="llm",
        content_hash="hash-v1",
    )
    conn.commit()

    assert db.cached_classification_hashes(conn) == {1: "hash-v1"}

    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="llm-app",
        subcategory=None,
        confidence=0.95,
        method="llm",
        content_hash="hash-v2",
    )
    conn.commit()
    row = conn.execute("SELECT category, content_hash FROM repo_classification").fetchone()
    assert row["category"] == "llm-app"
    assert row["content_hash"] == "hash-v2"


def test_saving_leaderboards_replaces_the_day_wholesale(conn):
    db.upsert_repos(
        conn,
        [
            db.RepoRecord.from_api(api_payload(1, "a/one")),
            db.RepoRecord.from_api(api_payload(2, "b/two")),
        ],
    )
    db.save_leaderboards(
        conn,
        TODAY,
        [Entry("momentum", "_all", 1, 1, 10.0), Entry("momentum", "_all", 2, 2, 5.0)],
    )
    # A re-run that finds only one eligible repo must not leave the other behind.
    db.save_leaderboards(conn, TODAY, [Entry("momentum", "_all", 1, 2, 7.0)])

    rows = conn.execute("SELECT * FROM leaderboard_snapshots WHERE date = ?", (TODAY,)).fetchall()
    assert [(r["rank"], r["repo_id"]) for r in rows] == [(1, 2)]


def test_previous_board_ranks_reads_the_last_snapshot_before_today(conn):
    db.upsert_repos(
        conn,
        [db.RepoRecord.from_api(api_payload(i, f"o/r{i}")) for i in (1, 2)],
    )
    db.save_leaderboards(
        conn,
        TODAY - dt.timedelta(days=7),
        [Entry("momentum", "_all", 1, 1, 9.0), Entry("momentum", "_all", 2, 2, 8.0)],
    )
    db.save_leaderboards(
        conn,
        TODAY - dt.timedelta(days=1),
        [Entry("momentum", "_all", 1, 2, 9.0), Entry("momentum", "_all", 2, 1, 8.0)],
    )

    ranks = db.previous_board_ranks(conn, before=TODAY, board="momentum")
    assert ranks == {2: 1, 1: 2}  # yesterday's, not last week's

    assert db.previous_board_ranks(conn, before=TODAY, board="fresh") == {}


def test_scores_and_board_join_round_trip(conn):
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "acme/agent", stars=4_200))])
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="agent-framework",
        subcategory="autonomous",
        confidence=0.97,
        method="rules",
        content_hash="h",
        one_liner="An autonomous agent framework.",
    )
    conn.commit()
    metrics = RepoMetrics(
        repo_id=1,
        stars_total=4_200,
        velocity_14d=120.5,
        acceleration=4.2,
        fresh_power=9_000.0,
        momentum_score=88.0,
        breakout=True,
        coverage_days=210,
        days_to_1k=30,
    )
    db.save_scores(conn, TODAY, [metrics])
    db.save_leaderboards(conn, TODAY, [Entry("momentum", "_all", 1, 1, 120.5)])

    rows = db.load_leaderboard(conn, date=TODAY, board="momentum")
    assert len(rows) == 1
    row = rows[0]
    assert row["full_name"] == "acme/agent"
    assert row["category"] == "agent-framework"
    assert row["one_liner"] == "An autonomous agent framework."
    assert row["velocity_14d"] == pytest.approx(120.5)
    assert row["breakout"] is True
    assert row["days_to_1k"] == 30


def test_run_log_records_cost(conn):
    run_id = db.start_run(conn, "collect")
    db.finish_run(conn, run_id, ok=True, api_calls=4_812, api_304s=901, llm_cost_usd=1.62)

    row = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
    assert row["ok"] is True
    assert row["api_calls"] == 4_812
    assert row["llm_cost_usd"] == pytest.approx(1.62)
    assert row["finished_at"] is not None


def test_topics_lookup_works_beyond_the_sql_variable_limit(conn):
    """SQLite caps bound variables per statement — 999 on older builds. The
    site export asks for topics for well over a thousand repos at once."""
    count = 2_500
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(id=i, full_name=f"o/r{i}", owner="o", name=f"r{i}", topics=("llm",))
            for i in range(1, count + 1)
        ],
    )

    topics = db.repo_topics_map(conn, list(range(1, count + 1)))

    assert len(topics) == count
    assert topics[1] == ["llm"]
    assert topics[count] == ["llm"]


def test_confirmed_non_ai_repos_are_not_collected(conn):
    """The census put 56,105 repos above a thousand stars into the corpus and
    the tracked floor jumped from 1,674 stars to 9,392 — because the star rank
    was taken over every repo, including the ones no board will ever show. A
    non-AI repo crowding out a rising AI one is quota spent to make the product
    worse."""
    db.upsert_repos(
        conn,
        [
            db.RepoRecord.from_api(api_payload(1, "acme/big-not-ai", stars=900)),
            db.RepoRecord.from_api(api_payload(2, "acme/small-ai", stars=100)),
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=False,
        category=None,
        subcategory=None,
        confidence=0.99,
        method="rules",
        content_hash="a",
    )
    db.save_classification(
        conn,
        2,
        is_ai=True,
        category="llm-app",
        subcategory=None,
        confidence=0.99,
        method="rules",
        content_hash="b",
    )
    conn.commit()

    due = {r["full_name"] for r in db.repos_due_for_refresh(conn, tier1_size=10, now=NOW)}

    assert due == {"acme/small-ai"}


def test_repos_not_yet_judged_are_still_collected(conn):
    """A repo discovered this run has no verdict yet. Dropping it would mean a
    new project waits a full classification cycle before anyone measures it —
    and new projects are the ones this index exists to catch."""
    db.upsert_repos(conn, [db.RepoRecord.from_api(api_payload(1, "acme/brand-new", stars=500))])
    conn.commit()

    due = {r["full_name"] for r in db.repos_due_for_refresh(conn, tier1_size=10, now=NOW)}

    assert due == {"acme/brand-new"}


# --- two unique keys, one ON CONFLICT clause -------------------------------


def test_a_new_id_may_claim_a_name_another_row_is_holding(conn):
    """`repos` has two unique keys and the upsert names only one of them, so a
    repository arriving with an unseen id under a name some other row still
    carries raised `UNIQUE constraint failed: repos.full_name`.

    It cost the scheduled run of 2026-09-22: the census died on it and the
    eight steps after it were skipped, so the site was not rebuilt that day.
    One row in a batch of a hundred takes the whole `executemany` with it.
    """
    db.upsert_repos(conn, [db.RepoRecord(id=111, full_name="a/one", owner="a", name="one")])
    conn.commit()

    # The same name, a different repository. GitHub does this whenever a
    # project is renamed and somebody takes the handle it left behind.
    db.upsert_repos(conn, [db.RepoRecord(id=222, full_name="a/one", owner="a", name="one")])
    conn.commit()

    rows = {r["id"]: r["full_name"] for r in conn.execute("SELECT id, full_name FROM repos")}

    assert rows[222] == "a/one"  # the newcomer owns the name
    assert rows[111] == "a/one@111"  # the old row kept its id and gave up the name
    assert 111 in rows  # and was not deleted


def test_the_row_that_gave_up_its_name_keeps_its_history(conn):
    """Everything hanging off a repository is `ON DELETE CASCADE`. Throwing
    away the star history of a project that was merely renamed would be a
    worse answer than the crash."""
    db.upsert_repos(
        conn, [db.RepoRecord(id=111, full_name="a/one", owner="a", name="one", topics=("llm",))]
    )
    db.record_star_daily(conn, 111, [DailyStars(dt.date(2026, 9, 1), 40)])
    conn.commit()

    db.upsert_repos(conn, [db.RepoRecord(id=222, full_name="a/one", owner="a", name="one")])
    conn.commit()

    def count(table: str) -> int:
        return conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE repo_id = 111"  # noqa: S608
        ).fetchone()["n"]

    assert count("repo_star_daily") == 1
    assert count("repo_topics") == 1


def test_the_released_row_goes_to_the_front_of_the_next_collect_pass(conn):
    """The tombstone is meant to be short-lived: the next pass asks GitHub what
    the id is called now and writes the real name back, or gets a 404 and
    removes the row. `repos_due_for_refresh` sorts nulls first, so that happens
    in the same run, before the export."""
    db.upsert_repos(
        conn,
        [db.RepoRecord(id=111, full_name="a/one", owner="a", name="one", stars=5_000)],
    )
    db.mark_checked(conn, 111, NOW)
    conn.commit()

    db.upsert_repos(
        conn, [db.RepoRecord(id=222, full_name="a/one", owner="a", name="one", stars=5_000)]
    )
    conn.commit()

    due = [r["id"] for r in db.repos_due_for_refresh(conn, tier1_size=10, now=NOW)]

    assert due and due[0] == 111

    # And the name repairs itself when the id turns up under its real one.
    db.upsert_repos(conn, [db.RepoRecord(id=111, full_name="a/renamed", owner="a", name="renamed")])
    conn.commit()
    names = {r["id"]: r["full_name"] for r in conn.execute("SELECT id, full_name FROM repos")}
    assert names == {111: "a/renamed", 222: "a/one"}


def test_one_batch_may_not_contain_two_rows_claiming_one_name(conn):
    """The same collision inside a single page would fail the same way, and the
    last sighting is the one to believe."""
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(id=111, full_name="a/one", owner="a", name="one"),
            db.RepoRecord(id=222, full_name="a/one", owner="a", name="one"),
        ],
    )
    conn.commit()

    rows = {r["id"]: r["full_name"] for r in conn.execute("SELECT id, full_name FROM repos")}

    assert rows == {222: "a/one"}
