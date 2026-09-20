"""Pruning must not change any answer.

The database lives in the repository, so only a rolling window of per-day rows
is kept. `fresh_power` integrates a project's whole life, so dropping old rows
would silently shrink every long-lived project's score — the exact opposite of
what the metric is for, since it would make old projects look *newer*.

The fix is that the decay is multiplicative: everything pruned collapses into a
single carried number that is decayed forward one day at a time. These tests
pin that the collapse is lossless.
"""

import datetime as dt
import math

import pytest

from airadar.db import repo as db
from airadar.gh.metrics import DailyStars
from airadar.scoring.metrics import DEFAULT_HALF_LIFE_DAYS, compute_repo_metrics

TODAY = dt.date(2026, 9, 20)
RETAIN = 120


def series(start: dt.date, end: dt.date, per_day: int) -> list[DailyStars]:
    out, day = [], start
    while day <= end:
        out.append(DailyStars(day, per_day))
        day += dt.timedelta(days=1)
    return out


def tail_of(days: list[DailyStars], *, cutoff: dt.date, today: dt.date) -> float:
    """What `prune_star_history` folds away, computed independently here."""
    decay = math.log(2) / DEFAULT_HALF_LIFE_DAYS
    return sum(
        d.stars_gained * math.exp(-decay * (today - d.date).days) for d in days if d.date < cutoff
    )


# --- the core property -----------------------------------------------------


def test_pruned_history_plus_carried_tail_equals_full_history():
    created = TODAY - dt.timedelta(days=5 * 365)
    full = series(created, TODAY, 27)
    cutoff = TODAY - dt.timedelta(days=RETAIN)
    retained = [d for d in full if d.date >= cutoff]

    complete = compute_repo_metrics(days=full, stars_total=50_000, created_at=created, today=TODAY)
    pruned = compute_repo_metrics(
        days=retained,
        stars_total=50_000,
        created_at=created,
        today=TODAY,
        carried_tail=tail_of(full, cutoff=cutoff, today=TODAY),
    )

    assert pruned.fresh_power == pytest.approx(complete.fresh_power, rel=1e-9)


def test_window_metrics_are_untouched_by_pruning():
    """Every bounded window is shorter than the retention period, so pruning
    cannot move them at all."""
    created = TODAY - dt.timedelta(days=900)
    full = series(created, TODAY, 40)
    retained = [d for d in full if d.date >= TODAY - dt.timedelta(days=RETAIN)]

    complete = compute_repo_metrics(days=full, stars_total=36_000, created_at=created, today=TODAY)
    pruned = compute_repo_metrics(
        days=retained, stars_total=36_000, created_at=created, today=TODAY
    )

    for field in (
        "velocity_7d",
        "velocity_14d",
        "velocity_28d",
        "velocity_90d",
        "acceleration",
        "relative_growth_14d",
    ):
        assert getattr(pruned, field) == pytest.approx(getattr(complete, field)), field


def test_the_headline_verdict_survives_pruning():
    """The whole point of the project: a two-week rocket still beats a five-year
    incumbent once both have been pruned."""
    old_created = TODAY - dt.timedelta(days=5 * 365)
    old_full = series(old_created, TODAY, 27)
    cutoff = TODAY - dt.timedelta(days=RETAIN)

    incumbent = compute_repo_metrics(
        days=[d for d in old_full if d.date >= cutoff],
        stars_total=50_000,
        created_at=old_created,
        today=TODAY,
        carried_tail=tail_of(old_full, cutoff=cutoff, today=TODAY),
    )
    newcomer = compute_repo_metrics(
        days=series(TODAY - dt.timedelta(days=13), TODAY, 3571),
        stars_total=50_000,
        created_at=TODAY - dt.timedelta(days=13),
        today=TODAY,
    )

    assert newcomer.fresh_power > incumbent.fresh_power * 5


# --- completeness and milestones survive pruning ---------------------------


def test_backfilled_through_keeps_history_complete_true_after_pruning():
    """Without it, a pruned repo would look like one we know nothing about and
    would drop off the Fresh Power board the day after it was backfilled."""
    created = TODAY - dt.timedelta(days=800)
    retained = series(TODAY - dt.timedelta(days=RETAIN), TODAY, 10)

    without = compute_repo_metrics(
        days=retained, stars_total=8_000, created_at=created, today=TODAY
    )
    with_watermark = compute_repo_metrics(
        days=retained,
        stars_total=8_000,
        created_at=created,
        today=TODAY,
        backfilled_through=created,
    )

    assert without.history_complete is False
    assert with_watermark.history_complete is True


def test_persisted_milestones_are_used_when_the_days_are_gone():
    created = TODAY - dt.timedelta(days=800)
    retained = series(TODAY - dt.timedelta(days=RETAIN), TODAY, 10)

    metrics = compute_repo_metrics(
        days=retained,
        stars_total=8_000,
        created_at=created,
        today=TODAY,
        backfilled_through=created,
        milestones=(37, 370, None),
    )

    assert (metrics.days_to_1k, metrics.days_to_10k, metrics.days_to_50k) == (37, 370, None)


def test_a_carried_tail_alone_still_produces_a_score():
    """A repo whose entire history is older than the window is not a zero."""
    metrics = compute_repo_metrics(
        days=[],
        stars_total=9_000,
        created_at=TODAY - dt.timedelta(days=2000),
        today=TODAY,
        carried_tail=1234.5,
    )
    assert metrics.fresh_power == pytest.approx(1234.5)


# --- the database side -----------------------------------------------------


def test_prune_folds_old_rows_into_the_tail_and_deletes_them(conn):
    created = TODAY - dt.timedelta(days=400)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="acme/agent",
                owner="acme",
                name="agent",
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
                stars=4_000,
            )
        ],
    )
    full = series(created, TODAY, 10)
    db.record_star_daily(conn, 1, full)
    conn.commit()

    removed = db.prune_star_history(
        conn, today=TODAY, retain_days=RETAIN, half_life_days=DEFAULT_HALF_LIFE_DAYS
    )

    assert removed == len(full) - (RETAIN + 1)
    remaining = db.load_daily_series(conn, 1)
    assert min(d.date for d in remaining) == TODAY - dt.timedelta(days=RETAIN)

    row = conn.execute("SELECT fresh_power_tail, fresh_power_tail_asof FROM repos").fetchone()
    expected = tail_of(full, cutoff=TODAY - dt.timedelta(days=RETAIN), today=TODAY)
    assert row["fresh_power_tail"] == pytest.approx(expected, rel=1e-9)
    assert row["fresh_power_tail_asof"] == TODAY


def test_pruning_twice_does_not_double_count_the_tail(conn):
    """Re-running a day's pipeline is normal; the tail must not grow each time."""
    created = TODAY - dt.timedelta(days=400)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/b",
                owner="a",
                name="b",
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
            )
        ],
    )
    db.record_star_daily(conn, 1, series(created, TODAY, 10))
    conn.commit()

    db.prune_star_history(
        conn, today=TODAY, retain_days=RETAIN, half_life_days=DEFAULT_HALF_LIFE_DAYS
    )
    first = conn.execute("SELECT fresh_power_tail FROM repos").fetchone()["fresh_power_tail"]
    db.prune_star_history(
        conn, today=TODAY, retain_days=RETAIN, half_life_days=DEFAULT_HALF_LIFE_DAYS
    )
    second = conn.execute("SELECT fresh_power_tail FROM repos").fetchone()["fresh_power_tail"]

    assert second == pytest.approx(first)


def test_an_existing_tail_is_decayed_forward_not_overwritten(conn):
    """A second prune, a week later, must age the number it already carried."""
    created = TODAY - dt.timedelta(days=400)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/b",
                owner="a",
                name="b",
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
            )
        ],
    )
    db.record_star_daily(conn, 1, series(created, TODAY, 10))
    conn.commit()

    db.prune_star_history(
        conn, today=TODAY, retain_days=RETAIN, half_life_days=DEFAULT_HALF_LIFE_DAYS
    )
    first = conn.execute("SELECT fresh_power_tail FROM repos").fetchone()["fresh_power_tail"]

    later = TODAY + dt.timedelta(days=7)
    db.record_star_daily(conn, 1, series(TODAY + dt.timedelta(days=1), later, 10))
    conn.commit()
    db.prune_star_history(
        conn, today=later, retain_days=RETAIN, half_life_days=DEFAULT_HALF_LIFE_DAYS
    )
    second = conn.execute("SELECT fresh_power_tail FROM repos").fetchone()["fresh_power_tail"]

    decay = math.log(2) / DEFAULT_HALF_LIFE_DAYS
    aged_old_part = first * math.exp(-decay * 7)
    assert second > aged_old_part  # the newly pruned week was added
    assert second < first + 7 * 10  # but nothing was counted undecayed


def test_scoring_rows_hand_the_tail_over_already_decayed(conn):
    created = TODAY - dt.timedelta(days=400)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/b",
                owner="a",
                name="b",
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
            )
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="mcp",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    db.set_lifetime_stats(
        conn,
        1,
        tail=1000.0,
        tail_asof=TODAY,
        days_to_1k=30,
        days_to_10k=None,
        days_to_50k=None,
    )
    conn.commit()

    same_day = db.load_scoring_rows(conn, today=TODAY, half_life_days=DEFAULT_HALF_LIFE_DAYS)
    half_life_later = db.load_scoring_rows(
        conn,
        today=TODAY + dt.timedelta(days=180),
        half_life_days=DEFAULT_HALF_LIFE_DAYS,
    )

    assert same_day[0]["carried_tail"] == pytest.approx(1000.0)
    assert half_life_later[0]["carried_tail"] == pytest.approx(500.0, rel=1e-6)
    assert same_day[0]["days_to_1k"] == 30


def test_a_week_of_daily_runs_does_not_erode_fresh_power(conn):
    """The failure this guards against is gradual: each day's prune shaves a
    little off, nobody notices, and after a month every long-lived project has
    quietly sunk down the board."""
    from airadar.collect import score

    created = TODAY - dt.timedelta(days=600)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="a/steady",
                owner="a",
                name="steady",
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
                stars=6_000,
            )
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="mcp",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    db.record_star_daily(conn, 1, series(created, TODAY, 10))
    db.set_backfill_watermark(conn, 1, created)
    conn.commit()

    readings = []
    for offset in range(7):
        day = TODAY + dt.timedelta(days=offset)
        if offset:
            db.record_star_daily(conn, 1, [DailyStars(day, 10)])
            conn.commit()
        score(conn, today=day)
        readings.append(
            conn.execute("SELECT fresh_power FROM repo_scores WHERE date = ?", (day,)).fetchone()[
                "fresh_power"
            ]
        )

    # A project running at a constant pace sits at a steady state, so the score
    # should barely move across the week — certainly not drift downwards.
    assert min(readings) > 0
    assert max(readings) / min(readings) < 1.02, readings
