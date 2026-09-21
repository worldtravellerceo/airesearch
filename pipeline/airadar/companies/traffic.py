"""Turning a Similarweb result into traffic history.

The actor returns one flat row per domain, and that row already carries three
months of estimated visits. Three monthly points is what this universe gets —
so the company boards are built on month-over-month movement, not on the
repository side's `fresh_power`. Feeding a 180-day decay integral three points
would produce a number that looks like the repository one and means nothing
like it, which is worse than not having it.

`trafficGenAi` is the field worth the money: the share of a site's visits that
arrive from ChatGPT, Claude, Gemini or Perplexity. For an index of AI companies
it is a first-class signal and there is nowhere else to read it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TrafficReport:
    domains_asked: int = 0
    domains_with_data: int = 0
    months_written: int = 0
    cost_usd: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.domains_with_data}/{self.domains_asked} domains had data, "
            f"{self.months_written} monthly points, ${self.cost_usd:.2f}"
        )


def _month(value: object) -> dt.date | None:
    """`2026-05` or `2026-05-01` -> the first of that month."""
    if not isinstance(value, str) or len(value) < 7:
        return None
    try:
        return dt.date(int(value[0:4]), int(value[5:7]), 1)
    except ValueError:
        return None


def traffic_rows(item: dict, *, collected_at: dt.datetime) -> list[dict]:
    """Every monthly point an actor result carries, oldest first.

    The history array is the point of paying for the row: one call buys three
    months, so the second month of tracking already has a trend rather than a
    single reading waiting for next month.
    """
    domain = (item.get("domain") or "").strip().lower()
    if not domain or item.get("hasData") is False:
        return []

    latest = _month(item.get("engagementMonth"))
    rows: list[dict] = []
    seen: set[dt.date] = set()

    for point in item.get("monthlyVisitsHistory") or []:
        if not isinstance(point, dict):
            continue
        month = _month(point.get("date"))
        if month is None or month in seen:
            continue
        seen.add(month)
        rows.append(
            {
                "domain": domain,
                "month": month,
                "visits": point.get("visits"),
                # Rank and engagement describe the snapshot month only. Copying
                # them onto older months would invent a history the source did
                # not supply.
                "global_rank": item.get("globalRank") if month == latest else None,
                "category": item.get("category") if month == latest else None,
                "category_rank": item.get("categoryRank") if month == latest else None,
                "bounce_rate": item.get("bounceRate") if month == latest else None,
                "traffic_genai": item.get("trafficGenAi") if month == latest else None,
                "collected_at": collected_at,
            }
        )

    if latest is not None and latest not in seen:
        rows.append(
            {
                "domain": domain,
                "month": latest,
                "visits": item.get("monthlyVisits"),
                "global_rank": item.get("globalRank"),
                "category": item.get("category"),
                "category_rank": item.get("categoryRank"),
                "bounce_rate": item.get("bounceRate"),
                "traffic_genai": item.get("trafficGenAi"),
                "collected_at": collected_at,
            }
        )

    rows.sort(key=lambda row: row["month"])
    return rows
