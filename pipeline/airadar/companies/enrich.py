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

from airadar.companies import domains as domains_mod
from airadar.companies import funding
from airadar.companies import g2 as g2_mod
from airadar.companies.apify import (
    CRUNCHBASE_ACTOR,
    G2_ACTOR,
    SIMILARWEB_ACTOR,
    ApifyClient,
    BudgetExceeded,
)
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


# --- money -----------------------------------------------------------------

# Measured from the actor's published pricing on 2026-09-21: $0.008 a result
# plus $0.05 to start a run.
CRUNCHBASE_PER_ROW_USD = 0.008
CRUNCHBASE_START_USD = 0.05
COMPANIES_PER_RUN = 500
# How far back the rounds sweep looks on a first run. After that the monitor
# only pays for rounds it has not seen.
ROUNDS_LOOKBACK_DAYS = 120


def rounds_estimate_usd(rows: int) -> float:
    return CRUNCHBASE_START_USD + rows * CRUNCHBASE_PER_ROW_USD


async def refresh_rounds(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    since: dt.date | None = None,
    min_amount_usd: int = funding.MIN_ROUND_USD,
    max_rounds: int = 400,
    monitor: bool = True,
    now: dt.datetime | None = None,
) -> funding.FundingReport:
    """Sweep announced funding rounds, newest first.

    This is the one paid query that needs no company list: rounds are selected
    by type, amount and date, so it finds the companies we have never heard of
    — which on a board about who just raised money is most of the value.

    `monitor` leaves out rounds a previous run with the same input already
    returned, and those are not billed. The input therefore has to stay stable
    between runs for the monthly top-up to be nearly free, which is why the
    lookback is a fixed number of days rather than "since last time".
    """
    now = now or dt.datetime.now(dt.UTC)
    since = since or (now.date() - dt.timedelta(days=ROUNDS_LOOKBACK_DAYS))
    report = funding.FundingReport()

    payload = {
        "roundsDatabase": True,
        "roundType": ",".join(funding.WATCHED_ROUND_TYPES),
        "minAmountUsd": min_amount_usd,
        "announcedAfter": since.isoformat(),
        "maxItems": max_rounds,
        "fundingMonitor": monitor,
    }
    try:
        run = await client.run_actor(
            conn,
            CRUNCHBASE_ACTOR,
            payload,
            max_charge_usd=round(rounds_estimate_usd(max_rounds) * RUN_CAP_MARGIN, 2),
            notes=f"rounds since {since}",
        )
    except BudgetExceeded as exc:
        log.warning("funding: %s", exc)
        return report

    report.cost_usd = run.cost_usd
    report.rounds_seen = len(run.items)
    rows = funding.round_rows(run.items, collected_at=now)
    report.rounds_written = db.record_rounds(conn, rows)
    log.info("funding: %s", report.summary())
    return report


async def refresh_company_funding(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    limit: int,
    min_stars: int = 0,
    now: dt.datetime | None = None,
) -> funding.FundingReport:
    """Look up the funding profile of companies we have never asked about.

    We hold domains and Crunchbase wants names, so the slug is guessed from the
    domain. A wrong guess does not fail — it returns a real company that is not
    ours — so nothing is stored until the profile's own website says it lives
    at the domain we asked for. A mismatch is recorded as such: knowing a guess
    was wrong is what stops it being paid for twice.
    """
    now = now or dt.datetime.now(dt.UTC)
    report = funding.FundingReport()

    domains = db.companies_to_match(conn, limit=limit, min_stars=min_stars)
    if not domains:
        return report

    for start in range(0, len(domains), COMPANIES_PER_RUN):
        batch = domains[start : start + COMPANIES_PER_RUN]
        # Several domains can guess the same slug — `langchain.com` and
        # `langchain.dev` both give `langchain`. Only one of them can be the
        # company that comes back, and the others were never really asked
        # about, so they must not be filed as "Crunchbase has nothing" and
        # thereby excluded from every future run.
        by_slug: dict[str, list[str]] = {}
        for domain in batch:
            by_slug.setdefault(funding.slug_candidate(domain), []).append(domain)
        asked = {slug: domains_for[0] for slug, domains_for in by_slug.items()}
        ambiguous = [d for domains_for in by_slug.values() for d in domains_for[1:]]

        cap = round(rounds_estimate_usd(len(asked)) * RUN_CAP_MARGIN, 2)
        try:
            run = await client.run_actor(
                conn,
                CRUNCHBASE_ACTOR,
                {"startUrls": sorted(asked), "maxItems": len(asked)},
                max_charge_usd=cap,
                notes=f"funding profiles for {len(asked)} companies",
            )
        except BudgetExceeded as exc:
            log.warning("funding: stopping early — %s", exc)
            break

        report.companies_asked += len(asked)
        report.cost_usd += run.cost_usd
        seen: set[str] = set()

        for item in run.items:
            slug = (item.get("permalink") or "").strip()
            domain = asked.get(slug)
            if domain is None:
                # Crunchbase resolved the guess to a different slug. The
                # website check below is what decides whether it is still ours.
                domain = _domain_for_website(item, asked)
            if domain is None:
                report.mismatched += 1
                continue
            seen.add(domain)

            row = funding.company_row(item, asked_domain=domain, collected_at=now)
            if row is None:
                report.mismatched += 1
                db.record_match(
                    conn,
                    domain,
                    state="mismatched",
                    asked_as=funding.slug_candidate(domain),
                    permalink=item.get("permalink"),
                    name=item.get("name"),
                    website=item.get("website"),
                    checked_at=now,
                )
                continue

            report.matched += 1
            db.record_company_funding(conn, row)
            db.record_match(
                conn,
                domain,
                state="matched",
                asked_as=funding.slug_candidate(domain),
                permalink=item.get("permalink"),
                name=item.get("name"),
                website=item.get("website"),
                checked_at=now,
            )
            db.set_name(conn, domain, item.get("name"))
            report.acquisitions += db.record_acquisitions(
                conn, funding.acquisition_rows(item, domain=domain, collected_at=now)
            )

        for domain in asked.values():
            if domain in seen:
                continue
            report.missing += 1
            db.record_match(
                conn,
                domain,
                state="missing",
                asked_as=funding.slug_candidate(domain),
                checked_at=now,
            )
        for domain in ambiguous:
            # A state, not a verdict: the slug went to somebody else this time,
            # and this one is still waiting to be looked up by another route.
            report.ambiguous += 1
            db.record_match(
                conn,
                domain,
                state="ambiguous",
                asked_as=funding.slug_candidate(domain),
                checked_at=now,
            )
        conn.commit()

        if not run.ok:
            log.error("funding: %s ended %s, stopping", CRUNCHBASE_ACTOR, run.status)
            break

    log.info("funding: %s", report.summary())
    return report


def _domain_for_website(item: dict, asked: dict[str, str]) -> str | None:
    """Whether a profile that came back under another slug is still one of ours."""
    website = domains_mod.registrable_domain(item.get("website"))
    return website if website in set(asked.values()) else None


# --- ratings ---------------------------------------------------------------

# Measured on 2026-09-21: $0.001 a review, $0.005 to start a run. A company with
# a G2 page costs about 2.5 cents at the review cap; one without costs nothing
# beyond the run it was asked in, which is what makes a broad sweep affordable
# across a universe most of which has no page.
G2_PER_REVIEW_USD = 0.001
G2_START_USD = 0.005
PRODUCTS_PER_RUN = 100


def g2_estimate_usd(companies: int) -> float:
    runs = max(1, -(-companies // PRODUCTS_PER_RUN))
    return runs * G2_START_USD + companies * g2_mod.REVIEWS_PER_PRODUCT * G2_PER_REVIEW_USD


async def refresh_ratings(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    limit: int,
    min_stars: int = 0,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
) -> g2_mod.G2Report:
    """Ask G2 about companies we have never asked about.

    Most of them will have no page, and that answer is stored too — asking
    again next month for a page that does not exist is the expensive mistake,
    not the miss itself.
    """
    now = now or dt.datetime.now(dt.UTC)
    today = today or now.date()
    report = g2_mod.G2Report()

    domains = db.companies_to_rate(conn, limit=limit, min_stars=min_stars)
    if not domains:
        return report

    for start in range(0, len(domains), PRODUCTS_PER_RUN):
        batch = domains[start : start + PRODUCTS_PER_RUN]
        slugs = {g2_mod.product_candidate(domain): domain for domain in batch}
        cap = round(g2_estimate_usd(len(batch)) * RUN_CAP_MARGIN, 2)
        try:
            run = await client.run_actor(
                conn,
                G2_ACTOR,
                {
                    "productUrls": sorted(slugs),
                    "maxReviewsPerProduct": g2_mod.REVIEWS_PER_PRODUCT,
                    "source": "rss",
                },
                max_charge_usd=cap,
                notes=f"g2 ratings for {len(batch)} companies",
            )
        except BudgetExceeded as exc:
            log.warning("g2: stopping early — %s", exc)
            break

        report.products_asked += len(batch)
        report.cost_usd += run.cost_usd

        by_slug: dict[str, list[dict]] = {slug: [] for slug in slugs}
        for item in run.items:
            slug = (item.get("productSlug") or item.get("product") or "").strip().lower()
            if slug not in by_slug:
                slug = _slug_for_name(item.get("productName"), by_slug)
            if slug is not None:
                by_slug[slug].append(item)

        for slug, items in by_slug.items():
            row = g2_mod.rating_snapshot(
                items, domain=slugs[slug], asked_as=slug, today=today, collected_at=now
            )
            if row is None:
                report.rejected += 1
                continue
            if row["reviews"]:
                report.products_found += 1
                report.reviews += row["reviews"]
            db.record_g2(conn, row)
        conn.commit()

        if not run.ok:
            log.error("g2: %s ended %s, stopping", G2_ACTOR, run.status)
            break

    log.info("g2: %s", report.summary())
    return report


def _slug_for_name(name: str | None, by_slug: dict[str, list[dict]]) -> str | None:
    """Fall back to the product name when the row carries no slug."""
    for slug in by_slug:
        if g2_mod.names_agree(slug, name or ""):
            return slug
    return None
