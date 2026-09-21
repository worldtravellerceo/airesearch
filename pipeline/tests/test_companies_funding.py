"""Money, and the ways it can be attributed to the wrong company.

The plan called entity resolution the riskiest step in this universe, and it
is: a name lookup that finds a different Acme returns a perfectly well-formed
row. Most of what is tested here is the refusal to believe one.
"""

import asyncio
import datetime as dt

import pytest

from airadar.companies import enrich, funding
from airadar.companies import g2 as g2_mod
from airadar.companies.apify import ApifyClient
from airadar.companies.domains import CompanySeed
from airadar.db import company as company_db
from tests.test_companies_apify import _no_sleep, fake_apify

NOW = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)
TODAY = NOW.date()


def seed(conn, domain, stars=10_000):
    company_db.upsert_seeds(conn, [CompanySeed(domain=domain, stars=stars, repos=1)], now=NOW)


# --- rounds ----------------------------------------------------------------


ROUND = {
    "roundId": "abc-123",
    "companyName": "Mistral AI",
    "companyPermalink": "mistral-ai",
    "investmentType": "series_c",
    "moneyRaisedUsd": 2_000_000_000,
    "announcedOn": "2026-09-08",
    "investors": [{"name": "ASML"}, {"name": "General Catalyst"}],
    "company": {"name": "Mistral AI", "website": "https://mistral.ai"},
}


def test_a_round_carries_its_amount_date_and_investors():
    rows = funding.round_rows([ROUND], collected_at=NOW)

    assert rows[0]["amount_usd"] == 2_000_000_000
    assert rows[0]["announced_on"] == dt.date(2026, 9, 8)
    assert rows[0]["investors"] == "ASML, General Catalyst"
    assert rows[0]["company_domain"] == "mistral.ai"


def test_an_undisclosed_amount_stays_empty_rather_than_zero():
    """Zero would sort as the smallest round rather than as unknown, and would
    read on the board as a company that raised nothing."""
    rows = funding.round_rows([{**ROUND, "moneyRaisedUsd": 0}], collected_at=NOW)

    assert rows[0]["amount_usd"] is None


def test_a_round_with_no_id_still_gets_a_key_that_survives_a_re_run(conn):
    """Without a stable key the same round arrives again next week as a second
    row, and the Funded board shows one raise twice."""
    item = {k: v for k, v in ROUND.items() if k != "roundId"}

    first = funding.round_rows([item], collected_at=NOW)
    second = funding.round_rows([item], collected_at=NOW + dt.timedelta(days=7))

    assert first[0]["round_key"] == second[0]["round_key"]
    company_db.record_rounds(conn, first)
    company_db.record_rounds(conn, second)
    assert conn.execute("SELECT count(*) AS n FROM funding_round").fetchone()["n"] == 1


async def test_a_rounds_sweep_needs_no_company_list(conn):
    """The point of this query: it finds companies we have never heard of."""
    async with ApifyClient(
        "t", transport=fake_apify([ROUND], cost=0.06), sleep=_no_sleep
    ) as client:
        report = await enrich.refresh_rounds(conn, client, now=NOW)

    assert report.rounds_written == 1
    assert conn.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 0
    row = conn.execute("SELECT * FROM funding_round").fetchone()
    assert row["company_name"] == "Mistral AI"


# --- who the profile actually belongs to -----------------------------------


PROFILE = {
    "name": "LangChain",
    "permalink": "langchain",
    "website": "https://www.langchain.com",
    "country": "United States",
    "ipoStatus": "private",
    "funding": {
        "totalUsd": 125_000_000,
        "numFundingRounds": 3,
        "numInvestors": 12,
        "rounds": [{"investmentType": "series_b", "announcedOn": "2026-02-14"}],
    },
    "people": {"employeeRange": "51-100"},
    "ma": {"acquisitions": [{"name": "Tiny Startup", "announcedOn": "2026-05-01"}]},
}


def test_a_profile_is_only_ours_if_its_own_website_says_so():
    """This is the whole guard. `swish` is four different companies on
    Crunchbase, and each of them returns a valid-looking row."""
    assert funding.company_row(PROFILE, asked_domain="langchain.com", collected_at=NOW)
    assert funding.company_row(PROFILE, asked_domain="langsmith.dev", collected_at=NOW) is None


def test_a_subdomain_on_the_profile_still_counts_as_the_same_company():
    item = {**PROFILE, "website": "https://docs.langchain.com/"}

    assert funding.company_row(item, asked_domain="langchain.com", collected_at=NOW)


async def test_the_wrong_company_is_recorded_as_wrong_not_silently_dropped(conn):
    """A mismatch that leaves no row is asked for again, and paid for again,
    every month."""
    seed(conn, "deepseek.com")
    other = {"name": "DeepL", "permalink": "deepl", "website": "https://www.deepl.com"}

    async with ApifyClient("t", transport=fake_apify([other]), sleep=_no_sleep) as client:
        report = await enrich.refresh_company_funding(conn, client, limit=10, now=NOW)

    assert report.matched == 0
    assert report.missing == 1
    row = conn.execute("SELECT * FROM company_crunchbase").fetchone()
    assert row["domain"] == "deepseek.com"
    assert row["match_state"] == "missing"


async def test_a_company_crunchbase_has_never_heard_of_is_not_asked_about_twice(conn):
    seed(conn, "obscure.dev")

    async with ApifyClient("t", transport=fake_apify([]), sleep=_no_sleep) as client:
        await enrich.refresh_company_funding(conn, client, limit=10, now=NOW)

    assert (
        conn.execute("SELECT match_state FROM company_crunchbase").fetchone()["match_state"]
        == "missing"
    )
    assert company_db.companies_to_match(conn, limit=10) == []


async def test_a_matched_profile_stores_funding_and_both_sides_of_m_and_a(conn):
    seed(conn, "langchain.com")

    async with ApifyClient(
        "t", transport=fake_apify([PROFILE], cost=0.06), sleep=_no_sleep
    ) as client:
        report = await enrich.refresh_company_funding(conn, client, limit=10, now=NOW)

    assert report.matched == 1
    row = conn.execute("SELECT * FROM company_funding").fetchone()
    assert row["total_usd"] == 125_000_000
    assert row["last_round"] == "series_b"
    assert row["employee_range"] == "51-100"

    acquisition = conn.execute("SELECT * FROM acquisition").fetchone()
    assert (acquisition["acquirer"], acquisition["target"]) == ("LangChain", "Tiny Startup")
    assert conn.execute("SELECT name FROM companies").fetchone()["name"] == "LangChain"


# --- valuation -------------------------------------------------------------


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Mistral AI Raises At $24B Valuation In Another Record Round", 24_000_000_000),
        ("Anthropic hits a $350 billion valuation", 350_000_000_000),
        ("Startup valued at $1.5B valuation after Series C", 1_500_000_000),
    ],
)
def test_a_headline_valuation_is_read_as_dollars(headline, expected):
    assert funding.valuation_from_headline(headline) == expected


@pytest.mark.parametrize(
    "headline",
    [
        "Acme raises $200M Series B",
        "Acme lays off 200 people",
        "Acme raises $200M to build agents",
        "",
    ],
)
def test_a_round_is_not_mistaken_for_a_valuation(headline):
    """They differ by an order of magnitude. A wrong number on a Valuation
    board is worse than an empty cell, which is why this is deliberately
    narrow and lets real valuations slip rather than guess."""
    assert funding.valuation_from_headline(headline) is None


def test_a_valuation_is_kept_with_the_article_that_stated_it(conn):
    seed(conn, "mistral.ai")
    company_db.record_valuation(
        conn,
        "mistral.ai",
        usd=24_000_000_000,
        source_url="https://news.crunchbase.com/x",
        on=dt.date(2026, 9, 8),
        collected_at=NOW,
    )
    conn.commit()

    row = conn.execute("SELECT * FROM company_funding").fetchone()
    assert row["valuation_usd"] == 24_000_000_000
    assert row["valuation_src"] == "https://news.crunchbase.com/x"


def test_a_funding_refresh_does_not_wipe_a_valuation_it_has_no_field_for(conn):
    seed(conn, "langchain.com")
    company_db.record_valuation(
        conn, "langchain.com", usd=1_000_000_000, source_url="u", on=TODAY, collected_at=NOW
    )
    row = funding.company_row(PROFILE, asked_domain="langchain.com", collected_at=NOW)
    company_db.record_company_funding(conn, row)
    conn.commit()

    stored = conn.execute("SELECT * FROM company_funding").fetchone()
    assert stored["valuation_usd"] == 1_000_000_000
    assert stored["total_usd"] == 125_000_000


# --- G2 --------------------------------------------------------------------


def _review(rating, name="LangChain"):
    return {"productName": name, "rating": rating, "publishedDate": "2026-09-01"}


def test_reviews_fold_into_one_rating_with_its_histogram():
    items = [_review(5), _review(5), _review(4), _review(2)]

    row = g2_mod.rating_snapshot(
        items, domain="langchain.com", asked_as="langchain", today=TODAY, collected_at=NOW
    )

    assert row["reviews"] == 4
    assert row["avg_rating"] == 4.0
    assert (row["rating_5"], row["rating_4"], row["rating_2"]) == (2, 1, 1)


def test_a_product_whose_name_does_not_match_is_rejected_rather_than_stored():
    """The G2 row carries no website, so this name check is all there is. It
    catches `deepseek` coming back as DeepL and nothing subtler — which is why
    a match here is never called verified."""
    row = g2_mod.rating_snapshot(
        [_review(5, name="DeepL Translate")],
        domain="deepseek.com",
        asked_as="deepseek",
        today=TODAY,
        collected_at=NOW,
    )

    assert row is None


def test_a_company_with_no_g2_page_is_recorded_as_having_none(conn):
    """Storing the miss is the point: asking again every month for a page that
    does not exist is what costs money."""
    seed(conn, "vllm.ai")

    async def go():
        async with ApifyClient("t", transport=fake_apify([]), sleep=_no_sleep) as client:
            return await enrich.refresh_ratings(conn, client, limit=10, today=TODAY, now=NOW)

    report = asyncio.run(go())

    assert report.products_found == 0
    assert conn.execute("SELECT reviews FROM company_g2").fetchone()["reviews"] == 0
    assert company_db.companies_to_rate(conn, limit=10) == []


def test_the_prices_are_arithmetic_we_can_check():
    """All three checked against the Apify API on 2026-09-21. If they drift,
    the budget claim drifts with them."""
    assert enrich.rounds_estimate_usd(3_000) == pytest.approx(24.05, abs=0.01)
    assert enrich.rounds_estimate_usd(400) == pytest.approx(3.25, abs=0.01)
    assert enrich.g2_estimate_usd(100) == pytest.approx(2.505, abs=0.01)


async def test_two_domains_guessing_the_same_slug_do_not_both_count_as_asked(conn):
    """`langchain.com` and `langchain.dev` both guess `langchain`. Only one of
    them can be the company that comes back, and filing the other as "Crunchbase
    has nothing" would drop it from every future run for a question nobody put."""
    seed(conn, "langchain.com", stars=100_000)
    seed(conn, "langchain.dev", stars=90_000)

    async with ApifyClient("t", transport=fake_apify([PROFILE]), sleep=_no_sleep) as client:
        report = await enrich.refresh_company_funding(conn, client, limit=10, now=NOW)

    assert report.companies_asked == 1
    assert report.matched == 1
    assert report.ambiguous == 1

    states = {
        row["domain"]: row["match_state"]
        for row in conn.execute("SELECT domain, match_state FROM company_crunchbase")
    }
    assert states == {"langchain.com": "matched", "langchain.dev": "ambiguous"}


async def test_the_domain_that_lost_the_slug_is_asked_about_next_time(conn):
    """It converges: the winner is answered and leaves the queue, so the loser
    gets the slug to itself on the next run."""
    seed(conn, "langchain.com", stars=100_000)
    seed(conn, "langchain.dev", stars=90_000)

    async with ApifyClient("t", transport=fake_apify([PROFILE]), sleep=_no_sleep) as client:
        await enrich.refresh_company_funding(conn, client, limit=10, now=NOW)

    assert company_db.companies_to_match(conn, limit=10) == ["langchain.dev"]


def test_the_round_size_is_not_read_as_the_valuation():
    """Crunchbase News writes both figures into one headline. From the actor's
    own sample: "Mistral AI Raises $3.5B At $24B Valuation" — and the first
    version of this pattern returned $3.5bn, the round rather than the
    valuation, wrong by a factor of seven on a public board. The gap between
    the figure and the word may not contain another dollar sign."""
    headline = "Mistral AI Raises $3.5B At $24B Valuation In Another Record European Round"

    assert funding.valuation_from_headline(headline) == 24_000_000_000


# --- the bought dictionary -------------------------------------------------


DIRECTORY_ROW = {
    "permalink": "mistral-ai",
    "name": "Mistral AI",
    "website": "https://mistral.ai",
    "categories": [{"name": "Artificial Intelligence"}],
    "country": "France",
}


async def test_the_directory_matches_a_company_without_guessing_anything(conn):
    """The slug guess was right 19% of the time. This is the opposite: the
    company's own Crunchbase profile names the domain we hold."""
    seed(conn, "mistral.ai", stars=340_000)

    async with ApifyClient(
        "t", transport=fake_apify([DIRECTORY_ROW], cost=0.02), sleep=_no_sleep
    ) as client:
        report = await enrich.refresh_directory(conn, client, limit=10, now=NOW)

    assert report["matched"] == 1
    row = conn.execute("SELECT * FROM company_crunchbase").fetchone()
    assert row["match_state"] == "matched"
    assert row["asked_as"] == "directory"
    assert conn.execute("SELECT name FROM companies").fetchone()["name"] == "Mistral AI"


async def test_the_directory_gives_a_round_somewhere_to_land(conn):
    """A round row is flat — a permalink and no website — which is why all 400
    rounds from the first paid run had a NULL domain and not one could be tied
    to a company we track."""
    seed(conn, "mistral.ai", stars=340_000)
    company_db.record_rounds(
        conn,
        [
            {
                "round_key": "k1",
                "company_name": "Mistral AI",
                "company_domain": None,
                "cb_permalink": "mistral-ai",
                "round_type": "series_c",
                "amount_usd": 2_000_000_000,
                "announced_on": TODAY - dt.timedelta(days=10),
                "investors": None,
                "source": "crunchbase",
                "source_url": None,
                "collected_at": NOW,
            }
        ],
    )

    async with ApifyClient("t", transport=fake_apify([DIRECTORY_ROW]), sleep=_no_sleep) as client:
        report = await enrich.refresh_directory(conn, client, limit=10, now=NOW)

    assert report["rounds_attached"] == 1
    assert (
        conn.execute("SELECT company_domain FROM funding_round").fetchone()["company_domain"]
        == "mistral.ai"
    )


async def test_a_directory_row_is_a_dictionary_entry_not_a_claim(conn):
    """`dbQuery` is a substring match over name and description, so asking for
    "ai" also returns Airbnb and Raiffeisen. Being in the table must not make
    a company part of this index."""
    async with ApifyClient(
        "t",
        transport=fake_apify(
            [{"permalink": "airbnb", "name": "Airbnb", "website": "https://airbnb.com"}]
        ),
        sleep=_no_sleep,
    ) as client:
        await enrich.refresh_directory(conn, client, limit=10, now=NOW)

    assert conn.execute("SELECT count(*) AS n FROM crunchbase_directory").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 0


async def test_a_profile_with_no_website_is_stored_without_a_domain(conn):
    """Half the point of the row is the website. One without it is still worth
    keeping as a name for a permalink, and must not become a match."""
    async with ApifyClient(
        "t",
        transport=fake_apify([{"permalink": "stealth", "name": "Stealth Co"}]),
        sleep=_no_sleep,
    ) as client:
        report = await enrich.refresh_directory(conn, client, limit=10, now=NOW)

    assert report["rows"] == 1
    assert report["with_domain"] == 0
    assert conn.execute("SELECT domain FROM crunchbase_directory").fetchone()["domain"] is None


# --- names --------------------------------------------------------------


def test_a_page_title_cannot_be_passed_off_as_a_company_name(conn):
    """874 companies were named from Similarweb's `title`, which is the scraped
    HTML page title: `openclaw.ai` became "The ClawCast Episode 1",
    `claude.com` "Kundensupport | Claude", `opencode.ai` "References". 366 of
    them ran past forty characters. There is no string that source can pass."""
    seed(conn, "openclaw.ai")

    with pytest.raises(ValueError, match="unknown name source"):
        company_db.set_name(conn, "openclaw.ai", "The ClawCast Episode 1", source="similarweb")


def test_a_better_source_replaces_a_worse_one(conn):
    """The first writer used to win outright, so a page title written by an
    early run permanently blocked the real name from a later one."""
    seed(conn, "mistral.ai")
    company_db.set_name(conn, "mistral.ai", "mistral-ai", source="corpus")
    company_db.set_name(conn, "mistral.ai", "Mistral AI", source="crunchbase")
    conn.commit()

    row = conn.execute("SELECT name, name_source FROM companies").fetchone()
    assert (row["name"], row["name_source"]) == ("Mistral AI", "crunchbase")


def test_a_worse_source_does_not_replace_a_better_one(conn):
    seed(conn, "mistral.ai")
    company_db.set_name(conn, "mistral.ai", "Mistral AI", source="crunchbase")
    company_db.set_name(conn, "mistral.ai", "mistral", source="corpus")
    conn.commit()

    assert conn.execute("SELECT name FROM companies").fetchone()["name"] == "Mistral AI"


def test_an_acquisition_with_no_date_is_not_inserted_again_every_run(conn):
    """SQLite treats two NULLs as distinct, so a UNIQUE constraint over the
    columns did not deduplicate the common case — an acquisition whose date
    was never announced. Three identical inserts produced three rows."""
    row = {
        "acquirer": "LangChain",
        "target": "Tiny Startup",
        "domain": "langchain.com",
        "announced_on": None,
        "amount_usd": None,
        "source": "crunchbase",
        "collected_at": NOW,
    }
    for _ in range(3):
        company_db.record_acquisitions(conn, [row])

    assert conn.execute("SELECT count(*) AS n FROM acquisition").fetchone()["n"] == 1


def test_the_last_round_is_read_whichever_way_the_actor_spells_it(conn):
    """Round rows in rounds mode use `investmentType`; the nested rounds inside
    a company profile use `investment_type`. Reading one of them is why
    `last_round` was NULL for all 96 matched companies."""
    profile = {
        **PROFILE,
        "funding": {
            "totalUsd": None,
            "numFundingRounds": 3,
            "rounds": [{"investment_type": "series_b", "announced_on": "2026-02-14"}],
        },
    }

    row = funding.company_row(profile, asked_domain="langchain.com", collected_at=NOW)

    assert row["last_round"] == "series_b"
    assert row["last_round_on"] == dt.date(2026, 2, 14)
