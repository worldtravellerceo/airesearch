"""The behavioural contract for the whole project.

The brief was concrete: a project that took five years to reach 50k stars must
not outrank one that took two weeks to reach the same number. Every metric here
exists to make that true, so these tests are written before the implementation
and are the thing to protect during refactors.
"""

import datetime as dt

import pytest

from airadar.gh.metrics import DailyStars
from airadar.scoring.metrics import compute_repo_metrics, score_cohort

TODAY = dt.date(2026, 9, 20)


def series(start: dt.date, end: dt.date, per_day: float) -> list[DailyStars]:
    """A flat daily star series over [start, end]."""
    out, day = [], start
    while day <= end:
        out.append(DailyStars(day, round(per_day)))
        day += dt.timedelta(days=1)
    return out


def slow_burner() -> dict:
    """50k stars accumulated evenly over five years — the incumbent."""
    created = TODAY - dt.timedelta(days=5 * 365)
    return {
        "days": series(created, TODAY, 50_000 / (5 * 365)),
        "stars_total": 50_000,
        "created_at": created,
    }


def overnight_hit() -> dict:
    """50k stars in fourteen days — the newcomer that actually beat the market."""
    created = TODAY - dt.timedelta(days=14)
    return {
        "days": series(created, TODAY, 50_000 / 14),
        "stars_total": 50_000,
        "created_at": created,
    }


# --- the headline contract -------------------------------------------------


def test_two_week_rocket_outranks_five_year_incumbent_at_equal_stars():
    old = compute_repo_metrics(**slow_burner(), today=TODAY)
    new = compute_repo_metrics(**overnight_hit(), today=TODAY)

    assert old.stars_total == new.stars_total == 50_000  # identical on the classic board
    assert new.fresh_power > old.fresh_power * 5  # decisively ahead on fresh power
    assert new.velocity_14d > old.velocity_14d * 50
    assert new.days_to_10k is not None and new.days_to_10k < 5
    assert old.days_to_10k is not None and old.days_to_10k > 300


def test_fresh_power_discounts_old_stars_by_the_half_life():
    """Stars one half-life old must count for about half as much."""
    day = TODAY - dt.timedelta(days=180)
    recent = compute_repo_metrics(
        days=[DailyStars(TODAY, 1000)], stars_total=1000, created_at=TODAY, today=TODAY
    )
    aged = compute_repo_metrics(
        days=[DailyStars(day, 1000)], stars_total=1000, created_at=day, today=TODAY
    )

    assert aged.fresh_power == pytest.approx(recent.fresh_power / 2, rel=0.02)


def test_momentum_metrics_are_correct_from_a_single_history_page():
    """One API page covers ~210 days, so every bounded window (7/14/28/90d) is
    exact without any backfill. This is what makes a daily refresh cost one
    request per repo."""
    full = slow_burner()
    one_page = {
        **full,
        "days": [d for d in full["days"] if d.date >= TODAY - dt.timedelta(days=209)],
    }

    complete = compute_repo_metrics(**full, today=TODAY)
    partial = compute_repo_metrics(**one_page, today=TODAY)

    assert partial.velocity_14d == complete.velocity_14d
    assert partial.velocity_90d == complete.velocity_90d
    assert partial.acceleration == pytest.approx(complete.acceleration)
    assert partial.relative_growth_14d == pytest.approx(complete.relative_growth_14d)


def test_fresh_power_needs_a_full_backfill_and_flags_when_it_lacks_one():
    """fresh_power integrates the repo's whole life, so a truncated series
    understates it — at a 180-day half-life, 210 days of history recovers only
    about 55% of the true value. Repos are therefore only comparable on this
    metric once backfilled, and `history_complete` is what the leaderboard gates
    on."""
    full = slow_burner()
    one_page = {
        **full,
        "days": [d for d in full["days"] if d.date >= TODAY - dt.timedelta(days=209)],
    }

    complete = compute_repo_metrics(**full, today=TODAY)
    partial = compute_repo_metrics(**one_page, today=TODAY)

    assert complete.history_complete is True
    assert partial.history_complete is False
    assert partial.fresh_power == pytest.approx(complete.fresh_power * 0.55, rel=0.05)
    assert partial.fresh_power < complete.fresh_power


def test_the_verdict_survives_incomplete_history():
    """Even understated, a five-year incumbent must not beat a two-week rocket —
    truncation only ever pushes the old project further down."""
    full = slow_burner()
    one_page = {
        **full,
        "days": [d for d in full["days"] if d.date >= TODAY - dt.timedelta(days=209)],
    }

    partial_old = compute_repo_metrics(**one_page, today=TODAY)
    new = compute_repo_metrics(**overnight_hit(), today=TODAY)

    assert new.fresh_power > partial_old.fresh_power


# --- individual metrics ----------------------------------------------------


def test_velocity_windows_average_over_the_window_length():
    days = series(TODAY - dt.timedelta(days=29), TODAY, 10)
    m = compute_repo_metrics(days=days, stars_total=300, created_at=days[0].date, today=TODAY)

    assert m.velocity_7d == pytest.approx(10.0)
    assert m.velocity_14d == pytest.approx(10.0)
    assert m.velocity_28d == pytest.approx(10.0)


def test_acceleration_compares_the_repo_against_its_own_baseline():
    """A project running 10x its 90-day norm is accelerating, whatever its size."""
    old = series(TODAY - dt.timedelta(days=89), TODAY - dt.timedelta(days=15), 10)
    recent = series(TODAY - dt.timedelta(days=14), TODAY, 100)
    m = compute_repo_metrics(
        days=old + recent, stars_total=10_000, created_at=old[0].date, today=TODAY
    )

    assert m.acceleration > 3.0
    assert m.velocity_14d == pytest.approx(100.0)


def test_relative_growth_surfaces_small_repos_that_are_exploding():
    """A 2k repo adding 1.4k in a fortnight is a bigger story than a 200k repo
    adding the same, and relative growth is what says so."""
    small = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=13), TODAY, 100),
        stars_total=2_000,
        created_at=TODAY - dt.timedelta(days=400),
        today=TODAY,
    )
    huge = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=13), TODAY, 100),
        stars_total=200_000,
        created_at=TODAY - dt.timedelta(days=400),
        today=TODAY,
    )

    assert small.relative_growth_14d > huge.relative_growth_14d * 50


def test_peak_detection_flags_a_project_whose_hype_has_passed():
    """The spike sits inside the 90-day baseline, so the repo is measurably
    running below its own norm — that is what a faded project looks like."""
    spike = series(TODAY - dt.timedelta(days=85), TODAY - dt.timedelta(days=65), 500)
    after = series(TODAY - dt.timedelta(days=64), TODAY, 5)
    m = compute_repo_metrics(
        days=spike + after, stars_total=11_000, created_at=spike[0].date, today=TODAY
    )

    assert m.peak_velocity > 400
    assert m.days_since_peak is not None and m.days_since_peak > 60
    assert m.acceleration < 1.0  # currently running below its own baseline


def test_time_to_milestones_is_none_when_history_is_incomplete():
    """Never invent a milestone we cannot see: a partial series must say so."""
    created = TODAY - dt.timedelta(days=5 * 365)
    partial = series(TODAY - dt.timedelta(days=100), TODAY, 10)
    m = compute_repo_metrics(days=partial, stars_total=50_000, created_at=created, today=TODAY)

    assert m.days_to_1k is None
    assert m.days_to_10k is None
    assert m.history_complete is False
    assert m.coverage_days == 101


def test_archived_or_dead_repo_scores_zero_momentum_without_dividing_by_zero():
    created = TODAY - dt.timedelta(days=2000)
    m = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=200), TODAY, 0),
        stars_total=8_000,
        created_at=created,
        today=TODAY,
    )

    assert m.velocity_14d == 0.0
    assert m.fresh_power == 0.0
    assert m.acceleration == 0.0
    assert m.relative_growth_14d == 0.0


def test_repo_with_no_history_at_all_is_handled():
    m = compute_repo_metrics(days=[], stars_total=120, created_at=TODAY, today=TODAY)
    assert m.velocity_14d == 0.0
    assert m.fresh_power == 0.0
    assert m.coverage_days == 0


# --- cohort ranking --------------------------------------------------------


def test_momentum_score_is_a_bounded_percentile_blend():
    cohort = [
        compute_repo_metrics(**overnight_hit(), today=TODAY),
        compute_repo_metrics(**slow_burner(), today=TODAY),
        compute_repo_metrics(
            days=series(TODAY - dt.timedelta(days=90), TODAY, 1),
            stars_total=300,
            created_at=TODAY - dt.timedelta(days=90),
            today=TODAY,
        ),
    ]

    scored = score_cohort(cohort)

    assert all(0.0 <= s.momentum_score <= 100.0 for s in scored)
    assert scored[0].momentum_score == max(s.momentum_score for s in scored)


def test_breakout_needs_both_absolute_speed_and_acceleration():
    """A consistently fast project is not a breakout; a suddenly fast one is."""
    steady = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=200), TODAY, 300),
        stars_total=60_000,
        created_at=TODAY - dt.timedelta(days=200),
        today=TODAY,
    )
    surging = compute_repo_metrics(
        days=(
            series(TODAY - dt.timedelta(days=200), TODAY - dt.timedelta(days=15), 5)
            + series(TODAY - dt.timedelta(days=14), TODAY, 400)
        ),
        stars_total=6_500,
        created_at=TODAY - dt.timedelta(days=200),
        today=TODAY,
    )
    quiet = [
        compute_repo_metrics(
            days=series(TODAY - dt.timedelta(days=200), TODAY, 1),
            stars_total=200,
            created_at=TODAY - dt.timedelta(days=200),
            today=TODAY,
        )
        for _ in range(30)
    ]

    scored = score_cohort([steady, surging, *quiet])
    by_id = {id(s): s for s in scored}

    assert by_id[id(surging)].breakout is True
    assert by_id[id(steady)].breakout is False


def test_score_cohort_on_a_single_repo_does_not_crash():
    scored = score_cohort([compute_repo_metrics(**overnight_hit(), today=TODAY)])
    assert len(scored) == 1
    assert 0.0 <= scored[0].momentum_score <= 100.0
