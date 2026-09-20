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
