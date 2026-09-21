"""The only code in this project that spends money.

Two things are tested hardest: that a run cannot be started without a ceiling
Apify itself enforces, and that what it cost is written down whatever happens.
A budget nobody records is not a budget.
"""

import datetime as dt
import json

import httpx
import pytest

from airadar.companies import enrich, traffic
from airadar.companies.apify import (
    SIMILARWEB_ACTOR,
    ActorRun,
    ApifyClient,
    ApifyError,
    BudgetExceeded,
    spend_this_month,
)
from airadar.companies.domains import CompanySeed
from airadar.db import company as company_db

NOW = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)


async def _no_sleep(_seconds):
    return None


def _run_json(run_id="r1", status="SUCCEEDED", cost=1.23, dataset="d1"):
    return {
        "data": {
            "id": run_id,
            "status": status,
            "usageTotalUsd": cost,
            "defaultDatasetId": dataset,
        }
    }


def fake_apify(items, *, status="SUCCEEDED", cost=1.23, seen=None):
    """A stand-in for Apify: start -> poll -> dataset."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "POST" and "/runs" in request.url.path:
            return httpx.Response(201, json=_run_json(status="RUNNING", cost=0))
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json=_run_json(status=status, cost=cost))
        if "/items" in request.url.path:
            offset = int(request.url.params.get("offset", 0))
            return httpx.Response(200, json=items if offset == 0 else [])
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    return httpx.MockTransport(handler)


# --- the ceiling -----------------------------------------------------------


async def test_every_run_carries_apifys_own_spending_cap(conn):
    """A cap that lives only in our arithmetic is not a cap: it disappears the
    moment this code has a bug or is handed a list ten times too long."""
    seen: list[httpx.Request] = []
    async with ApifyClient("t", transport=fake_apify([], seen=seen), sleep=_no_sleep) as client:
        await client.run_actor(conn, "acme~actor", {"domains": []}, max_charge_usd=4.5)

    start = next(r for r in seen if r.method == "POST")
    assert start.url.params["maxTotalChargeUsd"] == "4.50"


async def test_a_run_that_would_break_the_month_is_not_started(conn):
    """Refusing beforehand is the point. Apify's per-run cap cannot see the
    month; only the ledger can."""
    conn.execute(
        "INSERT INTO apify_run (actor, started_at, status, cost_usd) VALUES (?, ?, ?, ?)",
        ("acme~actor", NOW, "SUCCEEDED", 24.0),
    )
    conn.commit()
    seen: list[httpx.Request] = []

    async with ApifyClient(
        "t", monthly_cap_usd=25.0, transport=fake_apify([], seen=seen), sleep=_no_sleep
    ) as client:
        with pytest.raises(BudgetExceeded):
            await client.run_actor(conn, "acme~actor", {}, max_charge_usd=5.0)

    assert seen == []


async def test_no_token_is_refused_before_anything_is_attempted():
    with pytest.raises(ApifyError):
        ApifyClient("")


# --- the ledger ------------------------------------------------------------


async def test_the_bill_is_written_down(conn):
    async with ApifyClient(
        "t", transport=fake_apify([{"domain": "openai.com"}], cost=2.5), sleep=_no_sleep
    ) as client:
        run = await client.run_actor(conn, SIMILARWEB_ACTOR, {}, max_charge_usd=9.0)

    assert isinstance(run, ActorRun)
    assert run.cost_usd == 2.5
    row = conn.execute("SELECT * FROM apify_run").fetchone()
    assert row["actor"] == SIMILARWEB_ACTOR
    assert row["cost_usd"] == 2.5
    assert row["items"] == 1
    assert row["status"] == "SUCCEEDED"
    assert spend_this_month(conn, today=NOW.date()) == 2.5


async def test_a_run_that_never_starts_still_leaves_a_trace(conn):
    """Silence after a failed start is how spending goes unnoticed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="payment required")

    async with ApifyClient("t", transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
        with pytest.raises(ApifyError):
            await client.run_actor(conn, "acme~actor", {}, max_charge_usd=1.0)

    row = conn.execute("SELECT * FROM apify_run").fetchone()
    assert row["status"] == "START-FAILED"


async def test_last_months_spending_does_not_count_against_this_month(conn):
    conn.execute(
        "INSERT INTO apify_run (actor, started_at, status, cost_usd) VALUES (?, ?, ?, ?)",
        ("acme~actor", dt.datetime(2026, 8, 30, tzinfo=dt.UTC), "SUCCEEDED", 19.0),
    )
    conn.commit()

    assert spend_this_month(conn, today=dt.date(2026, 9, 21)) == 0.0


# --- what a result is worth ------------------------------------------------


SAMPLE = {
    "domain": "openai.com",
    "title": "OpenAI",
    "hasData": True,
    "globalRank": 201,
    "category": "ai_chatbots_and_tools",
    "categoryRank": 6,
    "monthlyVisits": 203086642,
    "bounceRate": 0.5819,
    "trafficGenAi": 0.2358,
    "engagementMonth": "2026-05",
    "monthlyVisitsHistory": [
        {"date": "2026-03-01", "visits": 203740797},
        {"date": "2026-04-01", "visits": 195737812},
        {"date": "2026-05-01", "visits": 203086642},
    ],
}


def test_one_paid_row_buys_three_months_of_trend():
    """The history array is why the row is worth paying for: the first reading
    already has a direction instead of waiting a month for a second point."""
    rows = traffic.traffic_rows(SAMPLE, collected_at=NOW)

    assert [row["month"] for row in rows] == [
        dt.date(2026, 3, 1),
        dt.date(2026, 4, 1),
        dt.date(2026, 5, 1),
    ]
    assert [row["visits"] for row in rows] == [203740797, 195737812, 203086642]


def test_rank_is_not_copied_backwards_onto_months_it_was_not_measured_in():
    """Similarweb gives one rank, for the snapshot month. Writing it onto March
    and April would invent a history the source never supplied — and a flat
    rank line is exactly what a board would read as 'stable'."""
    rows = traffic.traffic_rows(SAMPLE, collected_at=NOW)

    assert [row["global_rank"] for row in rows] == [None, None, 201]
    assert [row["traffic_genai"] for row in rows] == [None, None, 0.2358]


def test_a_domain_similarweb_knows_nothing_about_produces_no_rows():
    assert traffic.traffic_rows({"domain": "nowhere.dev", "hasData": False}, collected_at=NOW) == []


def test_a_result_with_only_a_snapshot_month_still_lands():
    item = {"domain": "acme.ai", "engagementMonth": "2026-09", "monthlyVisits": 1000}
    rows = traffic.traffic_rows(item, collected_at=NOW)

    assert len(rows) == 1
    assert rows[0]["month"] == dt.date(2026, 9, 1)
    assert rows[0]["visits"] == 1000


# --- the queue -------------------------------------------------------------


def seed_company(conn, domain, stars):
    company_db.upsert_seeds(conn, [CompanySeed(domain=domain, stars=stars, repos=1)], now=NOW)


def test_the_queue_serves_whoever_has_waited_longest(conn):
    """Biggest-first every run would buy the same head of the list forever and
    never reach the tail."""
    for domain, stars in [("a.com", 300), ("b.com", 200), ("c.com", 100)]:
        seed_company(conn, domain, stars)
    company_db.record_traffic(
        conn,
        [
            {
                "domain": "a.com",
                "month": dt.date(2026, 9, 1),
                "visits": 1,
                "global_rank": None,
                "category": None,
                "category_rank": None,
                "bounce_rate": None,
                "traffic_genai": None,
                "collected_at": NOW,
            }
        ],
    )

    assert company_db.companies_to_enrich(conn, limit=10) == ["b.com", "c.com", "a.com"]


def test_companies_below_the_star_floor_are_not_paid_for(conn):
    seed_company(conn, "big.com", 5_000)
    seed_company(conn, "tiny.com", 10)

    assert company_db.companies_to_enrich(conn, limit=10, min_stars=1_000) == ["big.com"]


def test_the_price_of_a_run_is_arithmetic_we_can_check():
    """3,000 companies was the plan's figure, at $8 a thousand for Crunchbase
    and $0.99 a thousand here. If this drifts, the budget claim drifts with it."""
    assert enrich.estimate_usd(1_000) == pytest.approx(1.015, abs=0.001)
    assert enrich.estimate_usd(3_000) == pytest.approx(3.045, abs=0.001)


async def test_a_refresh_stores_what_it_bought(conn):
    seed_company(conn, "openai.com", 90_000)

    async with ApifyClient(
        "t", transport=fake_apify([SAMPLE], cost=0.03), sleep=_no_sleep
    ) as client:
        report = await enrich.refresh_traffic(conn, client, limit=10, now=NOW)

    assert report.domains_asked == 1
    assert report.domains_with_data == 1
    assert report.months_written == 3
    assert report.cost_usd == 0.03
    # Deliberately unnamed. Similarweb's `title` is the scraped HTML page
    # title, not a company name: it called `openclaw.ai` "The ClawCast Episode
    # 1" and `claude.com` "Kundensupport | Claude" across 874 companies, and
    # those titles then blocked the real names from ever being written.
    assert conn.execute("SELECT name FROM companies").fetchone()["name"] is None
    assert conn.execute("SELECT count(*) AS n FROM company_traffic").fetchone()["n"] == 3


async def test_a_refresh_with_no_budget_left_spends_nothing_and_says_so(conn):
    seed_company(conn, "openai.com", 90_000)
    conn.execute(
        "INSERT INTO apify_run (actor, started_at, status, cost_usd) VALUES (?, ?, ?, ?)",
        ("x", NOW, "SUCCEEDED", 25.0),
    )
    conn.commit()
    seen: list[httpx.Request] = []

    async with ApifyClient(
        "t", monthly_cap_usd=25.0, transport=fake_apify([SAMPLE], seen=seen), sleep=_no_sleep
    ) as client:
        report = await enrich.refresh_traffic(conn, client, limit=10, now=NOW)

    assert report.domains_asked == 0
    assert [r for r in seen if r.method == "POST"] == []


async def test_re_reading_a_month_replaces_it_rather_than_doubling_it(conn):
    """The repository side shipped with exactly this bug: a roll-up that added
    instead of replacing, which inflated every figure it touched."""
    seed_company(conn, "openai.com", 90_000)

    for _ in range(2):
        async with ApifyClient(
            "t", transport=fake_apify([SAMPLE], cost=0.01), sleep=_no_sleep
        ) as client:
            await enrich.refresh_traffic(conn, client, limit=10, now=NOW)

    rows = conn.execute("SELECT month, visits FROM company_traffic ORDER BY month").fetchall()
    assert len(rows) == 3
    assert rows[-1]["visits"] == 203086642


def test_the_actor_ids_are_the_ones_that_were_priced():
    """A typo here does not fail — it starts somebody else's actor and bills us
    for it. These three were checked against the Apify API on 2026-09-21."""
    from airadar.companies import apify

    assert apify.SIMILARWEB_ACTOR == "memo23~similarweb-scraper"
    assert apify.CRUNCHBASE_ACTOR == "memo23~crunchbase-scraper"
    assert apify.G2_ACTOR == "automation_craft~g2-reviews-scraper"
    assert json.dumps(apify.API_ROOT).startswith('"https://')
