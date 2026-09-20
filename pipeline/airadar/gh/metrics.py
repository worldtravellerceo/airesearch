"""Star-history parsing and per-repo metric collection.

`GET /repos/{owner}/{repo}/stargazers/history` (REST API version 2026-03-10) is
the privacy-safe replacement for the stargazer listing endpoints that were
restricted to admins and collaborators in July 2026. It returns stars grouped by
calendar week, most recent first, with a seven-element `days` array per week
starting on Sunday — i.e. daily granularity, which is exactly what every trend
metric in this project needs.

One page (30 weeks) covers ~7 months, so a daily refresh costs a single request
per repo and still supports 7/14/28/90-day windows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

HISTORY_PATH = "/repos/{full_name}/stargazers/history"
WEEKS_PER_PAGE = 30  # API maximum for per_page
MAX_HISTORY_PAGES = 100  # API maximum for page


@dataclass(frozen=True)
class DailyStars:
    date: dt.date
    stars_gained: int


class StarHistoryFormatError(ValueError):
    """The endpoint returned something we do not recognise."""


def parse_star_history(payload: Any) -> list[DailyStars]:
    """Flatten the weekly API payload into ascending per-day star counts.

    Weeks with no stars legitimately contain zeros; they are kept so that gaps in
    the series mean "no data" rather than "no stars", which matters for the
    coverage checks in scoring.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise StarHistoryFormatError(f"expected a list of weeks, got {type(payload).__name__}")

    out: list[DailyStars] = []
    for week in payload:
        if not isinstance(week, dict) or "week" not in week or "days" not in week:
            raise StarHistoryFormatError(f"malformed week entry: {week!r}")
        days = week["days"]
        if not isinstance(days, list) or len(days) != 7:
            raise StarHistoryFormatError(
                f"expected 7 day counts for week {week['week']!r}, got {days!r}"
            )
        week_start = dt.datetime.fromtimestamp(int(week["week"]), tz=dt.UTC).date()
        for offset, count in enumerate(days):
            out.append(DailyStars(week_start + dt.timedelta(days=offset), int(count)))

    out.sort(key=lambda d: d.date)
    return out


def total_from_history(days: Iterable[DailyStars]) -> int:
    return sum(d.stars_gained for d in days)


def history_covers(days: list[DailyStars], since: dt.date) -> bool:
    """True when the series reaches back to `since`, i.e. windows are trustworthy."""
    return bool(days) and days[0].date <= since


def pages_needed(created_at: dt.date, today: dt.date) -> int:
    """How many history pages a full-lifetime backfill of this repo requires."""
    weeks = max(1, ((today - created_at).days // 7) + 1)
    return min(MAX_HISTORY_PAGES, -(-weeks // WEEKS_PER_PAGE))
