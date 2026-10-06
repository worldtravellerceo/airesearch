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


def test_every_repo_on_a_board_gets_a_page(conn, tmp_path):
    """The selection used to be the top 1,200 by stars, which broke the two
    boards this project exists for. Breakout ranks repos exploding *now*, and
    a repo exploding now has not had time to accumulate stars: 114 of its 132
    live rows pointed at a page that was never written, and Momentum lost 64
    of 200."""
    from airadar.collect import score

    # A giant nobody would miss, and a newcomer that only a board would surface.
    for repo_id, name, stars, created in [
        (1, "giant/model", 200_000, dt.datetime(2020, 1, 1, tzinfo=dt.UTC)),
        (2, "tiny/breakout", 900, dt.datetime(2026, 9, 1, tzinfo=dt.UTC)),
    ]:
        db.upsert_repos(
            conn,
            [
                db.RepoRecord(
                    id=repo_id,
                    full_name=name,
                    owner=name.split("/")[0],
                    name=name.split("/")[1],
                    created_at=created,
                    description="an llm agent framework",
                    stars=stars,
                )
            ],
        )
        db.save_classification(
            conn,
            repo_id,
            is_ai=True,
            category="agent-framework",
            subcategory=None,
            confidence=1.0,
            method="rules",
            content_hash=f"h{repo_id}",
        )
    db.record_star_daily(
        conn,
        2,
        [
            DailyStars(date=dt.date(2026, 9, 20) - dt.timedelta(days=n), stars_gained=60)
            for n in range(14)
        ],
    )
    conn.commit()
    score(conn, today=dt.date(2026, 9, 20))

    export_site.export(conn, tmp_path, date=dt.date(2026, 9, 20))

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    on_a_board = {
        row["full_name"]
        for row in conn.execute(
            "SELECT DISTINCT r.full_name FROM leaderboard_snapshots l "
            "JOIN repos r ON r.id = l.repo_id WHERE l.date = ?",
            (dt.date(2026, 9, 20),),
        )
    }
    assert on_a_board, "the fixture should put something on a board"
    assert on_a_board <= set(manifest["repos"])
    for name in on_a_board:
        assert (tmp_path / "repos" / f"{name}.json").exists(), name


def test_the_star_curve_stops_at_the_export_date(conn, tmp_path):
    """GitHub's star-history endpoint returns whole weeks, including the days
    of the current week that have not happened yet, each with a gain of zero.
    Every chart on the live site ran five days past the site's own "last
    updated" stamp as a flat line into the future."""
    today = dt.date(2026, 9, 21)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/b",
                owner="a",
                name="b",
                stars=1_000,
                description="an llm agent",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            )
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="llm-app",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    db.record_star_daily(
        conn,
        1,
        [DailyStars(date=today + dt.timedelta(days=n - 3), stars_gained=10) for n in range(7)],
    )
    conn.commit()
    from airadar.collect import score

    score(conn, today=today)
    export_site.export(conn, tmp_path, date=today)

    points = json.loads((tmp_path / "repos" / "a" / "b.history.json").read_text())["points"]
    assert points, "the fixture should produce a curve"
    assert max(p["date"] for p in points) <= today.isoformat()


def test_the_star_curve_ends_at_the_number_printed_beside_it(conn, tmp_path):
    """The running total used to start from max(stars - sum(gains), 0), which
    anchors correctly only while the recorded history is smaller than the star
    count. `openclaw/openclaw` ended at 390,203 against a tile reading
    390,187."""
    today = dt.date(2026, 9, 21)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/b",
                owner="a",
                name="b",
                stars=500,
                description="an llm agent",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            )
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="llm-app",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    # More history than the repo has stars — the state that broke the anchor.
    db.record_star_daily(
        conn,
        1,
        [DailyStars(date=today - dt.timedelta(days=n), stars_gained=100) for n in range(10)],
    )
    conn.commit()
    from airadar.collect import score

    score(conn, today=today)
    export_site.export(conn, tmp_path, date=today)

    points = json.loads((tmp_path / "repos" / "a" / "b.history.json").read_text())["points"]
    assert points[-1]["cumulative"] == 500
    assert all(p["cumulative"] >= 0 for p in points)


# --- saying which numbers are measurements ---------------------------------


def test_the_overview_separates_what_is_classified_from_what_is_measured(conn):
    """58,367 repositories were classified as AI and 19,248 of them have so
    much as one day of star history. The rest carry a velocity of zero because
    the column is NOT NULL DEFAULT 0, not because they are standing still — and
    a single tile reading "İzlenen AI projesi 58.367" claims all of it was
    observed. Measured on the live database on 2026-09-22."""
    seed(conn)

    counts = export_site._overview(conn, TODAY)["counts"]

    assert counts["ai_repos"] >= counts["ai_measured"]
    assert counts["ai_measured"] > 0


def test_the_fresh_tile_reads_the_board_pool_and_not_a_backfill_count(conn, tmp_path):
    """`backfilled` counts every repository with a completed history, AI or
    not, scoring above zero or not. The Fresh Power tile printed it as though
    it were the pool the board ranks. They are different questions with
    different answers."""
    seed(conn)
    export_site.export(conn, out := tmp_path / "data", date=TODAY)

    counts = read(out, "overview.json")["counts"]
    pools = counts["pools"]

    assert set(pools) <= set(BOARDS)
    assert pools["fresh"] == len(read(out, "boards", "fresh", "_all.json")["entries"])


def test_a_sparkline_says_where_in_the_window_it_starts(conn, tmp_path):
    """Every row on every board is labelled "90 gün" and stretched to the same
    96 pixels. A repo with two recorded days and one with ninety drew the same
    width, which reads as two shapes of the same thing."""
    seed(conn, scored_days=(TODAY,))
    export_site.export(conn, out := tmp_path / "data", date=TODAY)

    for entry in read(out, "boards", "momentum", "_all.json")["entries"]:
        if not entry["sparkline"]:
            continue
        start = dt.date.fromisoformat(entry["sparkline_from"])
        # Dense from its own first day to the export date, gaps included as
        # nulls, so the caller can place it on a fixed grid.
        assert len(entry["sparkline"]) == (TODAY - start).days + 1


def test_acceleration_says_whether_it_was_measured():
    """`_acceleration` invents two of its values: 1.0 for a repository too
    young to have a baseline, and a cap for one that was dormant and suddenly
    woke. Both reached the Breakout board — the one board whose entire subject
    is acceleration — looking exactly like a reading."""
    from airadar.scoring.metrics import compute_repo_metrics

    young = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=5), TODAY, 40),
        stars_total=240,
        created_at=TODAY - dt.timedelta(days=5),
        today=TODAY,
    )
    assert young.acceleration == 1.0
    assert young.acceleration_basis == "too_young"

    steady = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=120), TODAY, 10),
        stars_total=1_210,
        created_at=TODAY - dt.timedelta(days=120),
        today=TODAY,
    )
    assert steady.acceleration_basis == "measured"


def test_a_released_name_never_reaches_the_published_index(conn, tmp_path):
    """The first time the collision fix fired in production it put
    `cortex-docs/cortex@1331965172` straight onto the live site.

    The released name was supposed to be repaired by the next collect pass, and
    it is — for the tracked universe, which `track_limit` caps at the top
    12,000 by stars. That repository has 127 stars. Below the cap the
    placeholder stands until some later discovery pass happens to see the id
    again, so the export filters on the name rather than trusting the repair.
    """
    seed(conn)
    # The shape `release_contested_names` leaves behind: a real row whose name
    # now belongs to somebody else.
    conn.execute("UPDATE repos SET full_name = full_name || '@' || id WHERE id = 2")
    conn.commit()

    export_site.export(conn, out := tmp_path / "data", date=TODAY)

    index = read(out, "index.json")["repos"]
    assert index
    assert not [r for r in index if "@" in r["full_name"]]
    # Not on a board either, so nothing links to a page that was not written.
    for board in BOARDS:
        entries = read(out, "boards", board, "_all.json")["entries"]
        assert not [e for e in entries if "@" in e["full_name"]]
    assert not [slug for slug in read(out, "manifest.json")["repos"] if "@" in slug]
    # And it is not counted as something this index tracks.
    assert read(out, "overview.json")["counts"]["tracked"] == 3


def test_the_digest_separates_a_new_project_from_a_newly_tracked_one(conn, tmp_path):
    """ "What is new since I last looked" had no file.

    `first_seen_at` was written for every repository from the first day and read
    by nothing, so an arrival at 10,351 stars — `Vincentwei1021/video-shotcraft`,
    first seen on 5 October — landed somewhere in the middle of a board with
    nothing marking it as absent the day before.

    The two kinds of arrival are different news and measured as such: of the 12
    arrivals above the star floor on 5 October, 10 were genuinely young projects
    and 2 were long-lived repos that had only now crossed the census threshold.
    One list would bury the first kind.
    """
    seed(conn)
    # Both first seen today; one is a fortnight old, the other predates us.
    conn.execute(
        "UPDATE repos SET first_seen_at = ? WHERE id IN (2, 1)",
        (dt.datetime.combine(TODAY, dt.time(), dt.UTC),),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    digest = read(tmp_path / "site", "digest.json")

    assert [row["full_name"] for row in digest["new_projects"]] == ["newcomer/agent-os"]
    assert [row["full_name"] for row in digest["newly_tracked"]] == ["legacy/ml-toolkit"]
    assert digest["date"] == TODAY.isoformat()


def test_an_arrival_under_the_star_floor_is_counted_but_not_listed(conn, tmp_path):
    """Negative control, and the reason the floor exists.

    With no floor an ordinary day brings 51-522 new AI repositories, which
    nobody reads; at 1,000 stars it is 2-3, which is not worth opening. The
    count still reports the whole truth, so the page can say how much it is not
    showing rather than implying the day was quiet.
    """
    seed(conn)
    conn.execute(
        "UPDATE repos SET first_seen_at = ?, stars = 40 WHERE id = 4",
        (dt.datetime.combine(TODAY, dt.time(), dt.UTC),),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    digest = read(tmp_path / "site", "digest.json")

    listed = [row["full_name"] for row in digest["new_projects"] + digest["newly_tracked"]]
    assert "tiny/rag-helper" not in listed
    assert digest["counts"]["arrivals_total"] == 1
    assert digest["counts"]["arrivals_shown"] == 0


def test_the_digest_reports_a_quiet_day_as_quiet(conn, tmp_path):
    """An empty list is the honest answer when nothing arrived.

    The same rule the company boards follow: a page with no rows says the
    question has not been answered yet, which is better than a page padded with
    yesterday's news.
    """
    seed(conn)
    conn.execute("UPDATE repos SET first_seen_at = ?", ("2020-01-01T00:00:00+00:00",))
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    digest = read(tmp_path / "site", "digest.json")

    assert digest["new_projects"] == []
    assert digest["newly_tracked"] == []
    assert digest["counts"]["arrivals_total"] == 0


def test_digest_rows_carry_the_topics_the_hash_is_built_from(conn, tmp_path):
    """Without them a paragraph is written, validates, and does nothing.

    `inputs_hash` is full name, description, topics and language, and the
    summary import checks every written row against it. The first arrivals
    round left topics out of the digest, so the packet hashed an empty tuple
    and 6 of its 11 rows — precisely the ones that had topics, 4 to 12 each —
    were skipped as stale on import. The 5 that matched were the repos with no
    topics at all, which is the worst possible failure shape: it looks like it
    works, on exactly the rows where there was nothing to get wrong.

    A digest row has no detail file to fall back on, because detail pages are
    written for the boards and an arrival is not on one. So the digest is the
    only place the packet builder can read them from.
    """
    seed(conn)
    conn.execute(
        "UPDATE repos SET first_seen_at = ? WHERE id = 2",
        (dt.datetime.combine(TODAY, dt.time(), dt.UTC),),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    row = read(tmp_path / "site", "digest.json")["new_projects"][0]

    assert row["full_name"] == "newcomer/agent-os"
    # `seed` gives every repo the topics ("llm", <category>).
    assert row["topics"] == ["agent-framework", "llm"]


def test_a_digest_hash_matches_what_the_import_will_check(conn, tmp_path):
    """The end-to-end version of the above, against the real hash function.

    This is the check that would have caught it: build the hash the way the
    packet builder does, from the digest row alone, and compare it with the one
    the database computes. They have to be equal or the paragraph is written
    against a repository the index does not recognise.
    """
    from airadar.summarise import SummaryInput

    seed(conn)
    conn.execute(
        "UPDATE repos SET first_seen_at = ? WHERE id = 2",
        (dt.datetime.combine(TODAY, dt.time(), dt.UTC),),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    row = read(tmp_path / "site", "digest.json")["new_projects"][0]

    from_digest = SummaryInput(
        repo_id=0,
        full_name=row["full_name"],
        description=row["description"],
        topics=tuple(row["topics"]),
        language=row["language"],
        license=None,
        stars=0,
        readme_excerpt="",
    ).inputs_hash()

    db_row = conn.execute("SELECT description, language FROM repos WHERE id = 2").fetchone()
    from_db = SummaryInput(
        repo_id=0,
        full_name="newcomer/agent-os",
        description=db_row["description"],
        topics=tuple(sorted(db.repo_topics_map(conn, [2])[2])),
        language=db_row["language"],
        license=None,
        stars=0,
        readme_excerpt="",
    ).inputs_hash()

    assert from_digest == from_db


def test_the_warming_band_stops_where_breakout_starts(conn, tmp_path):
    """The gap between "holding its pace" and a 3x breakout had no page.

    Measured on 2026-10-06 over the 1,097 AI repositories with a measured
    acceleration and at least 10 stars a day: 611 slowing, 248 steady, 68 at
    1.2-1.5x, 41 at 1.5-2x, 49 at 2-3x, 80 clearing 3x. So 90 repositories were
    accelerating meaningfully and appeared on no board —
    `rohitg00/ai-engineering-from-scratch` among them at 65,135 stars and 2.97x.

    The ceiling is Breakout's own constant rather than a second copy of 3.0, so
    the two bands cannot drift apart and double-count a repository.
    """
    seed(conn)
    conn.execute(
        """
        UPDATE repo_scores
           SET acceleration = 2.0, acceleration_basis = 'measured', velocity_14d = 50
         WHERE repo_id = 1 AND date = ?
        """,
        (TODAY,),
    )
    conn.execute(
        """
        UPDATE repo_scores
           SET acceleration = 4.0, acceleration_basis = 'measured', velocity_14d = 50
         WHERE repo_id = 2 AND date = ?
        """,
        (TODAY,),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    warming = read(tmp_path / "site", "digest.json")["warming"]
    names = [row["full_name"] for row in warming]

    assert "legacy/ml-toolkit" in names, "2x should be in the band"
    assert "newcomer/agent-os" not in names, "4x belongs to Breakout, not here"


def test_a_guessed_acceleration_never_reaches_the_warming_band(conn, tmp_path):
    """Negative control, and the one that would flood it.

    A repo with no baseline is handed `BREAKOUT_ACCELERATION * 10` with basis
    `no_baseline`, and one too young to have a pace gets a flat 1.0 marked
    `too_young`. Both are placeholders, not readings. Filtering on `measured`
    is what keeps the band a list of repositories that actually sped up.
    """
    seed(conn)
    conn.execute(
        """
        UPDATE repo_scores
           SET acceleration = 2.0, acceleration_basis = 'no_baseline', velocity_14d = 50
         WHERE repo_id = 1 AND date = ?
        """,
        (TODAY,),
    )
    conn.commit()

    export_site.export(conn, tmp_path / "site", date=TODAY)
    warming = read(tmp_path / "site", "digest.json")["warming"]

    assert "legacy/ml-toolkit" not in [row["full_name"] for row in warming]
