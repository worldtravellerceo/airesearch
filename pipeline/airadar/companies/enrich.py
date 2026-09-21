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


# --- the dictionary --------------------------------------------------------

# Crunchbase's instant database serves the same clean company row as a live
# scrape, which means it carries `website` — and that single field is what
# ends the guessing. Ordered by Crunchbase rank, so a bounded purchase buys the
# most prominent companies rather than an arbitrary slice.
DIRECTORY_PER_RUN = 1000


async def refresh_directory(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    limit: int = 5000,
    query: str = "ai",
    now: dt.datetime | None = None,
) -> dict:
    """Buy Crunchbase's company list and use it to stop guessing.

    Three things follow from having `permalink -> website` in hand:

    Our companies match by domain, with nothing guessed. The slug guess was
    right 19% of the time; 140 of 498 lookups returned a real company that was
    not ours, and the only reason none of them was believed is that the profile
    had to name our domain before anything was stored.

    The funding rounds reach the companies they belong to. A round row is flat
    — `companyPermalink` and no website, which is the actor's shape and not a
    parsing failure — so all 400 rounds from the first run had a NULL domain
    and not one could be tied to anything we track.

    And `dbQuery` is a substring match over name and description, so "ai" also
    matches Airbnb and Raiffeisen. That is fine here: this table is a
    dictionary, not a claim. Nothing is called an AI company for being in it.
    """
    now = now or dt.datetime.now(dt.UTC)
    report = {"rows": 0, "with_domain": 0, "cost_usd": 0.0, "matched": 0, "rounds_attached": 0}

    for start in range(0, limit, DIRECTORY_PER_RUN):
        batch = min(DIRECTORY_PER_RUN, limit - start)
        cap = round(rounds_estimate_usd(batch) * RUN_CAP_MARGIN, 2)
        try:
            run = await client.run_actor(
                conn,
                CRUNCHBASE_ACTOR,
                {"instantDatabase": True, "dbQuery": query, "maxItems": batch},
                max_charge_usd=cap,
                notes=f"directory: {batch} rows of '{query}'",
            )
        except BudgetExceeded as exc:
            log.warning("directory: stopping early — %s", exc)
            break

        report["cost_usd"] += run.cost_usd
        rows = []
        for item in run.items:
            permalink = (item.get("permalink") or "").strip()
            if not permalink:
                continue
            categories = item.get("categories")
            if isinstance(categories, list):
                categories = ",".join(
                    str(c.get("name") if isinstance(c, dict) else c) for c in categories[:8]
                )
            domain = domains_mod.registrable_domain(item.get("website"))
            rows.append(
                {
                    "permalink": permalink,
                    "name": item.get("name"),
                    "website": item.get("website"),
                    "domain": domain,
                    "categories": categories or None,
                    "country": item.get("country"),
                    "fetched_at": now,
                }
            )
            report["with_domain"] += domain is not None
        report["rows"] += db.record_directory(conn, rows)

        if not run.ok:
            log.error("directory: %s ended %s, stopping", CRUNCHBASE_ACTOR, run.status)
            break

    # The point of the purchase: match without guessing, and give the rounds
    # somewhere to land.
    report["matched"] = db.match_from_directory(conn, now=now)
    report["rounds_attached"] = db.attach_rounds_to_domains(conn)
    log.info(
        "directory: %d rows (%d with a domain), %d companies matched, %d rounds attached, $%.2f",
        report["rows"],
        report["with_domain"],
        report["matched"],
        report["rounds_attached"],
        report["cost_usd"],
    )
    return report


# --- valuations ------------------------------------------------------------

# The only place any of these sources states a valuation is a headline. The
# actor's news mode returns them with the companies each article is about, so
# a figure arrives already attached to a permalink — which the directory turns
# into one of our domains.
NEWS_PER_RUN = 500


async def refresh_valuations(
    conn: sqlite3.Connection,
    client: ApifyClient,
    *,
    limit: int = 500,
    since: dt.date | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Read press-reported valuations out of Crunchbase News.

    Every figure here was written by a journalist rather than measured, so it
    is stored with the article that said it and shown as such. A headline with
    no valuation in it is the common case and costs nothing extra — the article
    was bought either way.
    """
    now = now or dt.datetime.now(dt.UTC)
    since = since or (now.date() - dt.timedelta(days=180))
    report = {"articles": 0, "valuations": 0, "attached": 0, "cost_usd": 0.0}

    for start in range(0, limit, NEWS_PER_RUN):
        batch = min(NEWS_PER_RUN, limit - start)
        try:
            run = await client.run_actor(
                conn,
                CRUNCHBASE_ACTOR,
                {
                    "newsMode": True,
                    "newsCategory": "ai,venture,startups",
                    "newsQuery": "valuation",
                    "newsDateFrom": since.isoformat(),
                    "maxItems": batch,
                },
                max_charge_usd=round(rounds_estimate_usd(batch) * RUN_CAP_MARGIN, 2),
                notes=f"valuation headlines since {since}",
            )
        except BudgetExceeded as exc:
            log.warning("valuations: stopping early — %s", exc)
            break

        report["cost_usd"] += run.cost_usd
        report["articles"] += len(run.items)

        for item in run.items:
            usd = funding.valuation_from_headline(item.get("title") or "")
            if usd is None:
                continue
            report["valuations"] += 1
            published = item.get("publishedAt")
            on = dt.date.fromisoformat(published[:10]) if isinstance(published, str) else None

            # An article names several companies — the one that raised, its
            # investors, sometimes a competitor. Only the first is the subject,
            # and attaching the figure to the rest would put a $24bn valuation
            # on whoever else got a mention.
            companies = item.get("companies")
            first = companies[0] if isinstance(companies, list) and companies else None
            permalink = (first or {}).get("permalink") if isinstance(first, dict) else None
            if not permalink:
                continue

            row = conn.execute(
                "SELECT domain FROM crunchbase_directory "
                "WHERE permalink = ? AND domain IS NOT NULL",
                (permalink,),
            ).fetchone()
            if row is None:
                continue
            known = conn.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (row["domain"],)
            ).fetchone()
            if known is None:
                continue

            db.record_valuation(
                conn,
                row["domain"],
                usd=usd,
                source_url=item.get("url") or "",
                on=on,
                collected_at=now,
            )
            report["attached"] += 1
        conn.commit()

        if not run.ok:
            break

    log.info(
        "valuations: %d articles, %d carried a figure, %d attached to a company we track, $%.2f",
        report["articles"],
        report["valuations"],
        report["attached"],
        report["cost_usd"],
    )
    return report
