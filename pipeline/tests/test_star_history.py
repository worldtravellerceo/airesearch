import datetime as dt

import pytest

from airadar.gh.metrics import (
    DailyStars,
    StarHistoryFormatError,
    history_covers,
    pages_needed,
    parse_star_history,
    total_from_history,
)

SUNDAY = dt.datetime(2026, 9, 6, tzinfo=dt.UTC)  # a Sunday


def _week(start: dt.datetime, days: list[int]) -> dict:
    return {"week": int(start.timestamp()), "total": sum(days), "days": days}


def test_weekly_payload_flattens_to_ascending_days():
    payload = [  # API returns most recent first
        _week(SUNDAY + dt.timedelta(days=7), [10, 0, 0, 0, 0, 0, 0]),
        _week(SUNDAY, [1, 2, 3, 4, 5, 6, 7]),
    ]

    days = parse_star_history(payload)

    assert len(days) == 14
    assert days[0] == DailyStars(dt.date(2026, 9, 6), 1)
    assert days[6] == DailyStars(dt.date(2026, 9, 12), 7)
    assert days[7] == DailyStars(dt.date(2026, 9, 13), 10)
    assert [d.date for d in days] == sorted(d.date for d in days)
    assert total_from_history(days) == 38


def test_zero_weeks_are_preserved_as_zero_not_dropped():
    """A week of zeros means "no stars", not "no data" — scoring depends on the gap."""
    payload = [_week(SUNDAY, [0] * 7)]
    days = parse_star_history(payload)
    assert len(days) == 7
    assert total_from_history(days) == 0


def test_empty_history_is_not_an_error():
    assert parse_star_history([]) == []
    assert parse_star_history(None) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"week": 1, "days": [0] * 7},  # object instead of list
        [{"week": 1, "days": [0] * 6}],  # short day array
        [{"week": 1, "total": 0}],  # missing days
        [{"days": [0] * 7}],  # missing week
    ],
)
def test_unexpected_shapes_raise_rather_than_silently_miscount(payload):
    with pytest.raises(StarHistoryFormatError):
        parse_star_history(payload)


def test_history_covers_reports_window_trustworthiness():
    days = parse_star_history([_week(SUNDAY, [1] * 7)])
    assert history_covers(days, dt.date(2026, 9, 10)) is True
    assert history_covers(days, dt.date(2026, 9, 1)) is False
    assert history_covers([], dt.date(2026, 9, 1)) is False


def test_pages_needed_for_backfill():
    today = dt.date(2026, 9, 20)
    assert pages_needed(dt.date(2026, 9, 1), today) == 1  # new repo, one page
    assert pages_needed(dt.date(2021, 9, 20), today) == 9  # 5 years ≈ 9 pages
    assert pages_needed(dt.date(1995, 1, 1), today) <= 100  # clamped to API max
