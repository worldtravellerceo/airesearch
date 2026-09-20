"""Static export tests.

The export is the only thing standing between the database and what people
actually see, so what matters here is that nothing is silently dropped: every
board gets a file, every rank delta is either a number or an honest null, and
the star curve adds up to the repo's headline star count.
"""

import datetime as dt
import json

import pytest

from airadar import export_site
from airadar.collect import score
from airadar.db import repo as db
from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import BOARDS

TODAY = dt.date(2026, 9, 20)


def series(start: dt.date, end: dt.date, per_day: int) -> list[DailyStars]:
    out, day = [], start
    while day <= end:
        out.append(DailyStars(day, per_day))
        day += dt.timedelta(days=1)
    return out


def seed(conn, *, scored_days=(TODAY,)):
    scenarios = [
        (1, "legacy/ml-toolkit", 1826, 50_000, "classic-ml"),
        (2, "newcomer/agent-os", 14, 50_000, "agent-framework"),
        (3, "giant/deep-framework", 3200, 190_000, "classic-ml"),
        (4, "tiny/rag-helper", 900, 2_100, "rag-vectordb"),
    ]
    for repo_id, full_name, age, stars, category in scenarios:
        created = TODAY - dt.timedelta(days=age - 1)
        owner, name = full_name.split("/")
        db.upsert_repos(
            conn,
            [
                db.RepoRecord(
                    id=repo_id,
                    full_name=full_name,
                    owner=owner,
                    name=name,
                    created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
                    description=f"a {category} project",
                    language="Python",
                    stars=stars,
                    discovered_via="topic",
                    topics=("llm", category),
                )
            ],
        )
        db.save_classification(
            conn,
            repo_id,
            is_ai=True,
            category=category,
            subcategory=None,
            confidence=1.0,
            method="rules",
            content_hash=f"h{repo_id}",
            one_liner=f"A {category} project.",
        )
        db.record_star_daily(conn, repo_id, series(created, TODAY, round(stars / age)))
        db.set_backfill_watermark(conn, repo_id, created)
    conn.commit()
    for day in scored_days:
        score(conn, today=day)


def read(out_dir, *parts):
    return json.loads((out_dir.joinpath(*parts)).read_text())


def test_export_writes_a_file_for_every_board(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"

    report = export_site.export(conn, out, date=TODAY)

    manifest = read(out, "manifest.json")
    assert manifest["as_of"] == TODAY.isoformat()
    for board in BOARDS:
        assert f"{board}/_all" in manifest["boards"]
        assert (out / "boards" / board / "_all.json").exists()
    assert report.boards == len(manifest["boards"])


def test_the_boards_still_disagree_after_export(conn, tmp_path):
    """The export must not flatten the whole point of the project."""
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    popular = [e["full_name"] for e in read(out, "boards", "popular", "_all.json")["entries"]]
    fresh = [e["full_name"] for e in read(out, "boards", "fresh", "_all.json")["entries"]]

    assert popular[0] == "giant/deep-framework"
    assert fresh[0] == "newcomer/agent-os"


def test_entries_carry_sparklines_and_honest_rank_deltas(conn, tmp_path):
    seed(conn, scored_days=(TODAY - dt.timedelta(days=1), TODAY))
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    entries = read(out, "boards", "momentum", "_all.json")["entries"]
    assert entries
    for entry in entries:
        assert isinstance(entry["sparkline"], list)
        # Either a number or null — never a fabricated zero for a repo that was
        # not on the board before.
        assert entry["rank_delta"] is None or isinstance(entry["rank_delta"], int)


def test_first_ever_run_reports_every_repo_as_new(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    entries = read(out, "boards", "fresh", "_all.json")["entries"]
    assert all(e["rank_delta"] is None for e in entries)


def test_detail_pages_carry_a_curve_that_ends_at_the_star_count(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    detail = read(out, "repos", "newcomer", "agent-os.json")
    assert detail["full_name"] == "newcomer/agent-os"
    assert detail["topics"] == ["agent-framework", "llm"]
    assert detail["ranks"]["fresh"] == 1

    history = read(out, "repos", "newcomer", "agent-os.history.json")["points"]
    assert history[-1]["cumulative"] == pytest.approx(detail["stars"], rel=0.02)
    assert detail["history_points"] == len(history)


def test_the_curve_is_published_separately_from_the_page(conn, tmp_path):
    """A lifetime history is the largest thing about a repo, and a static build
    inlines whatever a page reads into both its HTML and its client payload.
    Keeping it in its own file is what stops a detail page weighing 125 KB."""
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    detail_file = out / "repos" / "legacy" / "ml-toolkit.json"
    history_file = out / "repos" / "legacy" / "ml-toolkit.history.json"

    assert history_file.exists()
    assert "history" not in read(out, "repos", "legacy", "ml-toolkit.json")
    # The page itself must stay small enough to inline without thought.
    assert detail_file.stat().st_size < 4_000
    assert history_file.stat().st_size > detail_file.stat().st_size


def test_history_spans_the_full_life_even_after_pruning(conn, tmp_path):
    """Pruning rolls old days into weeks; the curve must still reach back to the
    project's beginning, otherwise a five-year climb looks like a four-month one."""
    seed(conn)
    db.prune_star_history(conn, today=TODAY, retain_days=120, half_life_days=180.0)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    history = read(out, "repos", "legacy", "ml-toolkit.history.json")["points"]
    first = dt.date.fromisoformat(history[0]["date"])

    assert (TODAY - first).days > 1_700
    assert history[-1]["cumulative"] == pytest.approx(50_000, rel=0.02)


def test_search_index_covers_every_tracked_repo(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    index = read(out, "index.json")["repos"]
    assert {r["full_name"] for r in index} == {
        "legacy/ml-toolkit",
        "newcomer/agent-os",
        "giant/deep-framework",
        "tiny/rag-helper",
    }


def test_categories_are_aggregated(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)

    by_name = {c["category"]: c for c in read(out, "categories.json")["categories"]}
    assert by_name["classic-ml"]["repos"] == 2


def test_an_empty_database_exports_a_usable_shell(conn, tmp_path):
    """A first deploy happens before any data exists. The site must build and
    say it is empty rather than fail or 404."""
    out = tmp_path / "data"

    report = export_site.export(conn, out, date=None)

    assert report.as_of is None
    assert read(out, "manifest.json")["boards"] == []
    assert read(out, "overview.json")["counts"]["tracked"] == 0


def test_export_is_destructive_so_stale_files_cannot_linger(conn, tmp_path):
    seed(conn)
    out = tmp_path / "data"
    export_site.export(conn, out, date=TODAY)
    (out / "boards" / "fresh" / "gone-category.json").write_text("{}")

    export_site.export(conn, out, date=TODAY)

    assert not (out / "boards" / "fresh" / "gone-category.json").exists()
