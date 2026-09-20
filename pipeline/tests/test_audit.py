"""The coverage canary.

This check exists because counting repos was mistaken for coverage. A corpus
of 64,373 repos was missing `karpathy/nanoGPT` and `facebookresearch/faiss`,
and nothing noticed for a full sweep.
"""

import datetime as dt

from airadar import audit
from airadar.db import repo as db


def add(conn, repo_id, full_name, stars=50_000):
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
                description="x",
                stars=stars,
            )
        ],
    )


def test_an_empty_database_fails_every_canary(conn):
    report = audit.audit_coverage(conn)
    assert report.found == []
    assert len(report.missing) == report.total
    assert report.rate == 0.0


def test_a_project_is_matched_even_after_its_owner_changes(conn):
    """`jax` moved from `google` to `jax-ml`, `DeepSpeed` from `microsoft` to
    `deepspeedai`. Matching on `owner/name` would report both as missing and
    send someone hunting a discovery bug that does not exist."""
    add(conn, 1, "jax-ml/jax")
    add(conn, 2, "deepspeedai/DeepSpeed")
    conn.commit()

    found = {name for name, _, _ in audit.audit_coverage(conn).found}
    assert "jax" in found
    assert "DeepSpeed" in found


def test_a_suffixed_release_still_counts(conn):
    """`ChatGLM` ships as `ChatGLM-6B` and `Qwen` as `Qwen2.5`."""
    add(conn, 1, "zai-org/ChatGLM-6B")
    conn.commit()

    assert "ChatGLM" in {name for name, _, _ in audit.audit_coverage(conn).found}


def test_a_small_namesake_does_not_count_as_the_real_project(conn):
    """`mamba` is also a package manager and `triton` is also a whale survey.
    Counting those would report coverage the index does not have."""
    add(conn, 1, "someone/mamba", stars=40)
    conn.commit()

    report = audit.audit_coverage(conn)
    assert "mamba" in report.missing


def test_the_report_counts_what_it_found(conn):
    add(conn, 1, "karpathy/nanoGPT")
    conn.commit()

    report = audit.audit_coverage(conn)
    assert len(report.found) == 1
    assert report.total == len(report.found) + len(report.missing)
    assert "nanoGPT" in {name for name, _, _ in report.found}
    assert "canaries present" in report.summary()


# --- freshness -------------------------------------------------------------

NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC)


def add_checked(conn, repo_id, full_name, stars, checked_at):
    add(conn, repo_id, full_name, stars=stars)
    if checked_at is not None:
        db.mark_checked(conn, repo_id, checked_at)


def test_a_universe_that_was_never_collected_reads_as_entirely_stale(conn):
    """The state this check was written for. `collect` had never completed a
    cycle — no run_log entry, no snapshots — and every board was being built
    from the 1,200 repos a separate backfill had filled in. Nothing said so."""
    for i in range(1, 4):
        add_checked(conn, i, f"acme/repo{i}", 1000 - i, None)
    conn.commit()

    report = audit.audit_freshness(conn, tier1_size=2, track_limit=3, now=NOW)

    assert report.never_checked == 3
    assert report.stale == 3
    assert "3 never checked" in report.summary()


def test_a_fully_refreshed_universe_reads_as_fresh(conn):
    add_checked(conn, 1, "acme/a", 900, NOW - dt.timedelta(hours=2))
    add_checked(conn, 2, "acme/b", 800, NOW - dt.timedelta(days=2))
    conn.commit()

    report = audit.audit_freshness(conn, tier1_size=1, track_limit=2, now=NOW)

    assert report.stale == 0
    assert report.never_checked == 0


def test_the_two_tiers_are_held_to_their_own_deadlines(conn):
    """Tier 1 is refreshed daily and tier 2 weekly, so three days old is stale
    for one and perfectly fine for the other."""
    add_checked(conn, 1, "acme/top", 900, NOW - dt.timedelta(days=3))
    add_checked(conn, 2, "acme/tail", 800, NOW - dt.timedelta(days=3))
    conn.commit()

    report = audit.audit_freshness(conn, tier1_size=1, track_limit=2, now=NOW)

    assert report.tier1_stale == 1
    assert report.tier2_stale == 0


def test_repos_outside_the_tracked_universe_are_not_counted(conn):
    """They are not collected by design, so counting them stale would make the
    number meaningless."""
    add_checked(conn, 1, "acme/tracked", 900, NOW - dt.timedelta(hours=1))
    add_checked(conn, 2, "acme/untracked", 10, None)
    conn.commit()

    report = audit.audit_freshness(conn, tier1_size=1, track_limit=1, now=NOW)

    assert report.total == 1
    assert report.stale == 0


def test_the_audit_ignores_confirmed_non_ai_repos_just_as_collect_does(conn):
    """Same setup as `test_confirmed_non_ai_repos_are_not_collected` in
    test_db.py, because these two functions have to give the same answer.

    They did not. The collect queue was narrowed to AI repos and this audit was
    not, so 9,153 non-AI repos it had never been asked to collect were counted
    as stale and a fully refreshed universe was reported as 24%.
    """
    add_checked(conn, 1, "acme/big-not-ai", 900, None)
    add_checked(conn, 2, "acme/small-ai", 100, NOW - dt.timedelta(hours=2))
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

    report = audit.audit_freshness(conn, tier1_size=10, track_limit=10, now=NOW)

    assert report.total == 1
    assert report.stale == 0
    assert report.never_checked == 0


def test_the_queue_and_the_audit_agree_on_which_repos_are_tracked(conn):
    """The test that keeps the two definitions from drifting again.

    Every repo the collect queue hands out must be one the audit is counting;
    otherwise the audit is grading a different population than the one being
    collected, and its number means nothing.
    """
    add_checked(conn, 1, "acme/ai-fresh", 5_000, NOW - dt.timedelta(hours=2))
    add_checked(conn, 2, "acme/ai-stale", 4_000, NOW - dt.timedelta(days=30))
    add_checked(conn, 3, "acme/ai-never", 3_000, None)
    add_checked(conn, 4, "acme/not-ai", 9_000, None)
    for repo_id, is_ai in ((1, True), (2, True), (3, True), (4, False)):
        db.save_classification(
            conn,
            repo_id,
            is_ai=is_ai,
            category="llm-app" if is_ai else None,
            subcategory=None,
            confidence=0.99,
            method="rules",
            content_hash=f"h{repo_id}",
        )
    conn.commit()

    queued = {
        r["full_name"]
        for r in db.repos_due_for_refresh(conn, tier1_size=2, now=NOW, track_limit=10)
    }
    report = audit.audit_freshness(conn, tier1_size=2, track_limit=10, now=NOW)

    # The non-AI repo is in neither.
    assert "acme/not-ai" not in queued
    assert report.total == 3

    # Everything the queue hands out is inside the population the audit grades,
    # and the audit's stale count is the part of it that has fallen behind.
    assert queued == {"acme/ai-stale", "acme/ai-never"}
    assert report.stale == 2
    assert report.never_checked == 1
