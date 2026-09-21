"""Buying traffic data for the companies we already know about.

The shape mirrors the repository side: a queue ordered by how long each company
has gone unmeasured, a bounded amount of work per run, and a report at the end
saying what it cost. The difference is that here the bound is money rather than
rate limit.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3

from airadar.companies.apify import SIMILARWEB_ACTOR, ApifyClient, BudgetExceeded
from airadar.companies.traffic import TrafficReport, traffic_rows
from airadar.db import company as db

log = logging.getLogger(__name__)

# Measured from the actor's published pricing on 2026-09-21: $0.00099 per
# result plus a $0.025 start event per 512MB run. A thousand domains is
# therefore about $1.02.
SIMILARWEB_PER_DOMAIN_USD = 0.00099
ACTOR_START_USD = 0.025
# The actor's own default cap per run, and what the pricing above assumes.
DOMAINS_PER_RUN = 1000
# Room above the arithmetic, so a couple of extra result rows do not abort a
# run at Apify's ceiling. It is a cap on one run, not on the month.
RUN_CAP_MARGIN = 1.35


def estimate_usd(domains: int) -> float:
    """What looking up this many domains should cost, before margin."""
    runs = max(1, -(-domains // DOMAINS_PER_RUN))
    return runs * ACTOR_START_USD + domains * SIMILARWEB_PER_DOMAIN_USD


async def refresh_traffic(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    limit: int,
    min_stars: int = 0,
    now: dt.datetime | None = None,
) -> TrafficReport:
    """Look up the next `limit` companies due for a traffic reading.

    Runs are chunked at the actor's per-run cap and stop the moment the month's
    budget cannot cover another one — a half-finished refresh is a normal
    outcome here, and the queue order means the next run resumes where this one
    stopped rather than starting over.
    """
    now = now or dt.datetime.now(dt.UTC)
    report = TrafficReport()

    domains = db.companies_to_enrich(conn, limit=limit, min_stars=min_stars)
    if not domains:
        return report
    log.info(
        "companies: %d domains due, about $%.2f, $%.2f left this month",
        len(domains),
        estimate_usd(len(domains)),
        client.budget_for(conn),
    )

    for start in range(0, len(domains), DOMAINS_PER_RUN):
        batch = domains[start : start + DOMAINS_PER_RUN]
        cap = round(estimate_usd(len(batch)) * RUN_CAP_MARGIN, 2)
        try:
            run = await client.run_actor(
                conn,
                SIMILARWEB_ACTOR,
                {"domains": batch, "maxItems": len(batch)},
                max_charge_usd=cap,
                notes=f"traffic for {len(batch)} domains",
            )
        except BudgetExceeded as exc:
            # Not an error. The cap did its job; the rest of the queue waits
            # for next month or a raised cap.
            log.warning("companies: stopping early — %s", exc)
            break

        report.domains_asked += len(batch)
        report.cost_usd += run.cost_usd

        rows: list[dict] = []
        for item in run.items:
            item_rows = traffic_rows(item, collected_at=now)
            if not item_rows:
                continue
            report.domains_with_data += 1
            rows.extend(item_rows)
            db.set_name(conn, item_rows[0]["domain"], item.get("title"))
        report.months_written += db.record_traffic(conn, rows)
        conn.commit()

        if not run.ok:
            # A failed run is still billed for what it produced, so the rows
            # above are kept — but continuing to the next batch would most
            # likely repeat whatever went wrong, at full price.
            log.error("companies: %s ended %s, stopping", SIMILARWEB_ACTOR, run.status)
            break

    return report
