import datetime as dt

from airadar.gh.metrics import DailyStars
from airadar.scoring.leaderboards import (
    ALL_CATEGORIES,
    Entry,
    build_all,
    rank_board,
    rank_deltas,
)
from airadar.scoring.metrics import compute_repo_metrics, score_cohort

TODAY = dt.date(2026, 9, 20)


def make(repo_id, *, per_day, days_of_history, stars_total, category=None, complete=True):
    created = TODAY - dt.timedelta(days=days_of_history - 1)
    series = [DailyStars(created + dt.timedelta(days=i), per_day) for i in range(days_of_history)]
    return compute_repo_metrics(
        days=series,
        stars_total=stars_total,
        # An incomplete history is one that starts long after the repo did.
        created_at=created if complete else created - dt.timedelta(days=2000),
        today=TODAY,
        repo_id=repo_id,
        category=category,
    )


def test_popular_and_fresh_disagree_which_is_the_whole_point():
    incumbent = make(1, per_day=27, days_of_history=1826, stars_total=50_000)
    rocket = make(2, per_day=3571, days_of_history=14, stars_total=50_000)
    cohort = score_cohort([incumbent, rocket])

    popular = rank_board(cohort, "popular")
    fresh = rank_board(cohort, "fresh")

    assert [e.repo_id for e in popular] == [1, 2]  # tie on stars, lower id first
    assert [e.repo_id for e in fresh] == [2, 1]  # the newcomer wins on fresh power


def test_fresh_board_excludes_repos_without_a_full_backfill():
    backfilled = make(1, per_day=10, days_of_history=400, stars_total=4_000)
    partial = make(2, per_day=1000, days_of_history=200, stars_total=900_000, complete=False)
    cohort = score_cohort([backfilled, partial])

    fresh_ids = [e.repo_id for e in rank_board(cohort, "fresh")]
    momentum_ids = [e.repo_id for e in rank_board(cohort, "momentum")]

    assert fresh_ids == [1]  # partial history cannot be ranked fairly
    assert momentum_ids[0] == 2  # but bounded windows are fine without backfill


def test_breakout_board_only_contains_flagged_repos():
    surging = compute_repo_metrics(
        days=(
            [DailyStars(TODAY - dt.timedelta(days=d), 5) for d in range(200, 14, -1)]
            + [DailyStars(TODAY - dt.timedelta(days=d), 400) for d in range(14, -1, -1)]
        ),
        stars_total=7_000,
        created_at=TODAY - dt.timedelta(days=200),
        today=TODAY,
        repo_id=1,
    )
    steady = make(2, per_day=300, days_of_history=200, stars_total=60_000)
    quiet = [make(10 + i, per_day=1, days_of_history=200, stars_total=200) for i in range(30)]
    cohort = score_cohort([surging, steady, *quiet])

    ids = [e.repo_id for e in rank_board(cohort, "breakout")]

    assert ids == [1]


def test_limit_is_respected():
    cohort = score_cohort(
        [make(i, per_day=i, days_of_history=100, stars_total=i * 100) for i in range(1, 60)]
    )
    assert len(rank_board(cohort, "momentum", limit=200)) == 59
    assert len(rank_board(cohort, "momentum", limit=10)) == 10
    assert [e.rank for e in rank_board(cohort, "momentum", limit=3)] == [1, 2, 3]


def test_build_all_produces_global_and_per_category_boards():
    cohort = score_cohort(
        [
            make(
                1, per_day=100, days_of_history=200, stars_total=20_000, category="agent-framework"
            ),
            make(
                2, per_day=50, days_of_history=200, stars_total=10_000, category="agent-framework"
            ),
            make(3, per_day=10, days_of_history=200, stars_total=2_000, category="rag-vectordb"),
        ]
    )

    entries = build_all(cohort)
    categories = {e.category for e in entries}
    boards = {e.board for e in entries}

    assert categories == {ALL_CATEGORIES, "agent-framework", "rag-vectordb"}
    # Nothing in this cohort is surging, so the breakout board is legitimately
    # empty — an empty board is a real answer, not a missing one.
    assert boards == {"popular", "momentum", "fresh"}
    agent_popular = [e for e in entries if e.board == "popular" and e.category == "agent-framework"]
    assert [e.repo_id for e in agent_popular] == [1, 2]


def test_rank_deltas_distinguish_new_entries_from_static_ones():
    previous = [
        Entry("momentum", ALL_CATEGORIES, 1, 100, 5.0),
        Entry("momentum", ALL_CATEGORIES, 2, 200, 4.0),
    ]
    current = [
        Entry("momentum", ALL_CATEGORIES, 1, 200, 9.0),  # climbed one place
        Entry("momentum", ALL_CATEGORIES, 2, 100, 5.0),  # dropped one place
        Entry("momentum", ALL_CATEGORIES, 3, 300, 1.0),  # brand new
    ]

    deltas = rank_deltas(current, previous)

    assert deltas[("momentum", ALL_CATEGORIES, 200)] == 1
    assert deltas[("momentum", ALL_CATEGORIES, 100)] == -1
    assert deltas[("momentum", ALL_CATEGORIES, 300)] is None
