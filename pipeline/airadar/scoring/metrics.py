"""Trend metrics: turning a daily star series into comparable numbers.

The design problem this file solves: raw star totals reward age, not relevance.
A project that spent five years collecting 50,000 stars and one that collected
50,000 in a fortnight look identical on `sort:stars`, even though only one of
them is currently winning. Each metric below attacks a different facet of that.

`fresh_power` is the flagship. It is the sum of every star the repo ever
received, each discounted by how long ago it arrived, with a half-life of 180
days. Old stars fade out; recent ones count fully. Because the decay is
exponential, the sum converges quickly — roughly seven months of history (one
page from the API) already lands within a few percent of the full-lifetime
value, which is what makes a one-request-per-repo daily refresh viable.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from airadar.gh.metrics import DailyStars

VELOCITY_WINDOWS = (7, 14, 28)
MOMENTUM_WINDOW = 14
BASELINE_WINDOW = 90
# Fewer baseline days than this and the ratio is noise rather than signal.
MIN_BASELINE_DAYS = 21
PEAK_WINDOW = 7
BREAKOUT_ACCELERATION = 3.0
# A breakout has to be moving fast enough to matter, but "fast enough" cannot be
# a high percentile of the whole universe: that bar is set by PyTorch and
# friends, and no genuinely breaking-out small project would ever clear it. An
# absolute floor plus the cohort median is the honest reading of "something is
# actually happening here".
BREAKOUT_MIN_VELOCITY = 10.0
MILESTONES = (1_000, 10_000, 50_000)
DEFAULT_HALF_LIFE_DAYS = 180.0

# Weights for the momentum blend. They sum to 1 and are applied to percentile
# ranks, not raw values, so one runaway repo cannot distort the whole board.
MOMENTUM_WEIGHTS = {"velocity": 0.5, "acceleration": 0.3, "relative_growth": 0.2}


@dataclass
class RepoMetrics:
    """Everything derivable from one repo's own history.

    `momentum_score` and `breakout` are left at their defaults here because they
    are relative measures — `score_cohort` fills them in once the peer group is
    known.
    """

    repo_id: int | None = None
    stars_total: int = 0
    velocity_7d: float = 0.0
    velocity_14d: float = 0.0
    velocity_28d: float = 0.0
    velocity_90d: float = 0.0
    baseline_velocity: float = 0.0
    acceleration: float = 0.0
    relative_growth_14d: float = 0.0
    fresh_power: float = 0.0
    peak_velocity: float | None = None
    days_since_peak: int | None = None
    days_to_1k: int | None = None
    days_to_10k: int | None = None
    days_to_50k: int | None = None
    coverage_days: int = 0
    history_complete: bool = False
    momentum_score: float = 0.0
    breakout: bool = False
    category: str | None = field(default=None)


def compute_repo_metrics(
    *,
    days: list[DailyStars],
    stars_total: int,
    created_at: dt.date,
    today: dt.date,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    repo_id: int | None = None,
    category: str | None = None,
) -> RepoMetrics:
    """Compute every self-contained metric for one repository."""
    metrics = RepoMetrics(repo_id=repo_id, stars_total=stars_total, category=category)
    if not days:
        return metrics

    ordered = sorted(days, key=lambda d: d.date)
    by_date = {d.date: d.stars_gained for d in ordered}

    metrics.coverage_days = (ordered[-1].date - ordered[0].date).days + 1
    # Week buckets start on a Sunday, so a complete history begins on or before
    # the creation date. The tolerance absorbs that alignment.
    metrics.history_complete = ordered[0].date <= created_at + dt.timedelta(days=7)

    for window in VELOCITY_WINDOWS:
        setattr(metrics, f"velocity_{window}d", _window_velocity(by_date, today, window))
    metrics.velocity_90d = _window_velocity(by_date, today, BASELINE_WINDOW)

    metrics.baseline_velocity, baseline_days = _baseline_velocity(by_date, today)
    metrics.acceleration = _acceleration(
        metrics.velocity_14d, metrics.baseline_velocity, baseline_days
    )

    stars_14d = _window_sum(by_date, today, MOMENTUM_WINDOW)
    # Growth measured against where the repo stood before the window opened, so
    # a small project adding 70% of its stars this fortnight outranks a giant
    # adding the same absolute number.
    stars_before = max(stars_total - stars_14d, 1)
    metrics.relative_growth_14d = stars_14d / stars_before if stars_14d else 0.0

    metrics.fresh_power = _fresh_power(ordered, today, half_life_days)
    metrics.peak_velocity, metrics.days_since_peak = _peak(ordered, today)

    if metrics.history_complete:
        milestones = _milestone_days(ordered, created_at)
        metrics.days_to_1k, metrics.days_to_10k, metrics.days_to_50k = milestones

    return metrics


# --- individual metrics ----------------------------------------------------


def _window_sum(by_date: dict[dt.date, int], today: dt.date, window: int) -> int:
    start = today - dt.timedelta(days=window - 1)
    return sum(count for day, count in by_date.items() if start <= day <= today)


def _window_velocity(by_date: dict[dt.date, int], today: dt.date, window: int) -> float:
    return _window_sum(by_date, today, window) / window


def _baseline_velocity(by_date: dict[dt.date, int], today: dt.date) -> tuple[float, int]:
    """The repo's own pace *before* the current momentum window.

    Excluding the recent fortnight is what makes the comparison meaningful: if
    the baseline included it, a spike would partly cancel itself out.
    """
    start = today - dt.timedelta(days=BASELINE_WINDOW - 1)
    end = today - dt.timedelta(days=MOMENTUM_WINDOW)
    observed = [count for day, count in by_date.items() if start <= day <= end]
    if not observed:
        return 0.0, 0
    return sum(observed) / len(observed), len(observed)


def _acceleration(velocity_14d: float, baseline: float, baseline_days: int) -> float:
    if velocity_14d == 0.0:
        return 0.0
    if baseline_days < MIN_BASELINE_DAYS:
        # Too young to have a baseline. Report "unremarkable" rather than a
        # number invented from two data points.
        return 1.0
    if baseline <= 0.0:
        # Dormant then suddenly alive. Cap it so a single star off a zero base
        # cannot dominate the rankings.
        return float(BREAKOUT_ACCELERATION * 10)
    return velocity_14d / baseline


def _fresh_power(days: list[DailyStars], today: dt.date, half_life_days: float) -> float:
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    decay = math.log(2) / half_life_days
    total = 0.0
    for day in days:
        age = (today - day.date).days
        if age < 0:
            continue
        total += day.stars_gained * math.exp(-decay * age)
    return total


def _peak(days: list[DailyStars], today: dt.date) -> tuple[float | None, int | None]:
    """Highest sustained 7-day pace, and how long ago it happened."""
    if len(days) < PEAK_WINDOW:
        return None, None

    counts = [d.stars_gained for d in days]
    running = sum(counts[:PEAK_WINDOW])
    best, best_end = running / PEAK_WINDOW, PEAK_WINDOW - 1
    for i in range(PEAK_WINDOW, len(counts)):
        running += counts[i] - counts[i - PEAK_WINDOW]
        average = running / PEAK_WINDOW
        if average > best:
            best, best_end = average, i

    if best == 0.0:
        return 0.0, None
    return best, (today - days[best_end].date).days


def _milestone_days(
    days: list[DailyStars], created_at: dt.date
) -> tuple[int | None, int | None, int | None]:
    """Days from creation to each star milestone — the "five years vs two weeks"
    question answered as a number."""
    reached: dict[int, int] = {}
    cumulative = 0
    for day in days:
        cumulative += day.stars_gained
        for milestone in MILESTONES:
            if milestone not in reached and cumulative >= milestone:
                reached[milestone] = max(0, (day.date - created_at).days)
    return tuple(reached.get(m) for m in MILESTONES)  # type: ignore[return-value]


# --- cohort-relative scoring ----------------------------------------------


def score_cohort(metrics: list[RepoMetrics]) -> list[RepoMetrics]:
    """Fill in `momentum_score` and `breakout` relative to the peer group.

    Percentile ranks rather than z-scores: star counts are heavy-tailed, and one
    viral repo should not flatten everyone else's score.
    """
    if not metrics:
        return metrics

    ranks = {
        "velocity": _percentiles([m.velocity_14d for m in metrics]),
        "acceleration": _percentiles([m.acceleration for m in metrics]),
        "relative_growth": _percentiles([m.relative_growth_14d for m in metrics]),
    }

    velocity_cutoff = max(
        BREAKOUT_MIN_VELOCITY, _quantile([m.velocity_14d for m in metrics], 0.5)
    )

    for index, repo in enumerate(metrics):
        repo.momentum_score = 100.0 * sum(
            weight * ranks[key][index] for key, weight in MOMENTUM_WEIGHTS.items()
        )
        # Both conditions matter: absolute speed alone just re-describes the
        # popularity board, and acceleration alone promotes noise off tiny bases.
        repo.breakout = (
            repo.velocity_14d >= velocity_cutoff
            and repo.acceleration >= BREAKOUT_ACCELERATION
        )

    return metrics


def _percentiles(values: list[float]) -> list[float]:
    """Mean-rank percentile in [0, 1]; ties share the midpoint of their span."""
    n = len(values)
    if n == 1:
        return [0.5]
    ordered = sorted(values)
    out = []
    for value in values:
        less = _bisect_left(ordered, value)
        equal = _bisect_right(ordered, value) - less
        out.append((less + 0.5 * equal) / n)
    return out


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default), without the dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def _bisect_left(ordered: list[float], value: float) -> int:
    import bisect

    return bisect.bisect_left(ordered, value)


def _bisect_right(ordered: list[float], value: float) -> int:
    import bisect

    return bisect.bisect_right(ordered, value)
