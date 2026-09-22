"""Valuations, read from Crunchbase News rather than bought from it.

Every headline quoted here is real, fetched from the public WordPress API on
2026-09-21. The API is unauthenticated and unmetered; the Apify actor sells
the same articles at $0.008 each.
"""

import datetime as dt

import httpx
import pytest

from airadar.companies import news
from airadar.companies.domains import CompanySeed
from airadar.db import company as company_db

NOW = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)

# Verbatim from news.crunchbase.com.
HEADLINES = [
    (
        "Mistral AI Raises $3.5B At $24B Valuation In Another Record European AI Round",
        24_000_000_000,
    ),
    (
        "Former Apple Engineers' Physical AI Startup Lyte Raises $165M At $1.6B Valuation",
        1_600_000_000,
    ),
    (
        "Socure Secures $156M at $5.2B Valuation, Acquires AI Fraud Investigation Startup",
        5_200_000_000,
    ),
    (
        "AppsFlyer Reportedly Lands $1B At $2.7B Valuation To Help Companies Track Ads",
        2_700_000_000,
    ),
    ("Anthropic Nears $1T Valuation And Leapfrogs OpenAI On Unicorn Board", 1_000_000_000_000),
]


def _seed(conn, domain, name):
    company_db.upsert_seeds(conn, [CompanySeed(domain=domain, stars=1000, repos=1)], now=NOW)
    company_db.record_directory(
        conn,
        [
            {
                "permalink": name.lower().replace(" ", "-"),
                "name": name,
                "website": f"https://{domain}",
                "domain": domain,
                "categories": None,
                "country": None,
                "fetched_at": NOW,
            }
        ],
    )
    conn.commit()


def _api(articles):
    def handler(request: httpx.Request) -> httpx.Response:
        if int(request.url.params.get("page", 1)) > 1:
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(
            200,
            json=[
                {
                    "title": {"rendered": t},
                    "link": f"https://news.crunchbase.com/{i}",
                    "date": "2026-09-08T13:02:11",
                }
                for i, t in enumerate(articles)
            ],
        )

    return httpx.MockTransport(handler)


# --- reading the figure ----------------------------------------------------


def test_every_real_headline_yields_the_valuation_not_the_round():
    """Each of these names two figures, and the round comes first. The first
    version of the pattern returned $3.5bn for Mistral — the round rather than
    the valuation, wrong by a factor of seven."""
    from airadar.companies.funding import valuation_from_headline

    for headline, expected in HEADLINES:
        assert valuation_from_headline(headline) == expected, headline


# --- finding the company ---------------------------------------------------


def test_the_subject_is_the_first_known_company_in_the_headline():
    """The rest of a Crunchbase News headline is investors and acquirees.
    "Socure Secures $156M at $5.2B Valuation, Acquires AI Fraud Investigation
    Startup" is about Socure."""
    names = {"socure": "socure.com", "mistral ai": "mistral.ai", "anthropic": "anthropic.com"}

    assert news.subject_of(HEADLINES[2][0], names) == "socure.com"
    assert news.subject_of(HEADLINES[0][0], names) == "mistral.ai"


def test_a_name_is_matched_on_word_boundaries():
    """Without boundaries `Lyte` matches `Lytespeed`, and every headline
    containing "at" matches a company called At."""
    assert news.subject_of("Lytespeed Raises At $1B Valuation", {"lyte": "lyte.com"}) is None
    assert news.subject_of("Lyte Raises At $1B Valuation", {"lyte": "lyte.com"}) == "lyte.com"


def test_a_headline_about_nobody_we_track_finds_nothing():
    assert news.subject_of("Acme Raises At $9B Valuation", {"mistral ai": "mistral.ai"}) is None


# --- the whole pass --------------------------------------------------------


async def test_a_valuation_lands_with_the_article_that_reported_it(conn):
    _seed(conn, "mistral.ai", "Mistral AI")

    async with httpx.AsyncClient(transport=_api([HEADLINES[0][0]])) as client:
        report = await news.refresh_valuations(conn, limit=10, now=NOW, client=client)

    assert report["attached"] == 1
    assert report["cost_usd"] == 0.0
    row = conn.execute("SELECT * FROM company_funding").fetchone()
    assert row["valuation_usd"] == 24_000_000_000
    assert row["valuation_src"].startswith("https://news.crunchbase.com/")


async def test_a_valuation_for_a_company_we_do_not_track_is_not_stored(conn):
    """This index is about its own companies, not about venture news."""
    _seed(conn, "mistral.ai", "Mistral AI")

    async with httpx.AsyncClient(transport=_api([HEADLINES[3][0]])) as client:
        report = await news.refresh_valuations(conn, limit=10, now=NOW, client=client)

    assert report["valuations"] == 1
    assert report["attached"] == 0


async def test_nothing_is_attempted_before_the_directory_exists(conn):
    """The names come from the bought directory. Without it there is nothing
    to match against, and guessing from the headline text is exactly what this
    avoids."""
    async with httpx.AsyncClient(transport=_api([HEADLINES[0][0]])) as client:
        report = await news.refresh_valuations(conn, limit=10, now=NOW, client=client)

    assert report == {"articles": 0, "valuations": 0, "attached": 0, "cost_usd": 0.0}


# --- what a year of real headlines taught this module ----------------------


async def test_a_refusal_is_not_an_empty_answer():
    """The pager treated any status >= 400 as "past the last page". The site's
    nginx answers 403 to the default `python-httpx` user agent, so this whole
    channel fetched nothing from the day it was written and reported success
    every time: `valuation_usd` was NULL for all 134 companies."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(news.NewsUnavailable, match="403"):
            await news.fetch_headlines(since=dt.date(2026, 1, 1), client=client)


async def test_the_request_says_who_it_is():
    """Not politeness alone — the anonymous request is the one that is
    refused, so the agent string is load-bearing."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(400, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": news.USER_AGENT}
    ) as client:
        await news.fetch_headlines(since=dt.date(2026, 1, 1), client=client)

    assert seen and all("airadar" in agent for agent in seen)


def test_the_subject_is_the_company_the_verb_belongs_to():
    """Taking the first known name in the headline is wrong in a way only real
    headlines showed: the start of a Crunchbase News headline carries investors
    and acquirees too. Over a year of them, anchoring on the funding verb made
    every match correct; the earlier rule filed "Former Apple Engineers'
    Physical AI Startup Lyte Raises $165M At $1.6B Valuation" under Apple."""
    names = {"apple": "apple.com", "socure": "socure.com", "mistral ai": "mistral.ai"}

    assert news.subject_of(HEADLINES[1][0], names) is None  # the Apple headline
    assert news.subject_of(HEADLINES[2][0], names) == "socure.com"
    assert news.subject_of(HEADLINES[0][0], names) == "mistral.ai"


def test_a_name_after_the_verb_is_not_the_subject():
    """ "Blitzy Raises $200M At $1.4B Valuation For Autonomous Software
    Development" is not about `autonomous.ai` — and a label that generic is
    kept out of the derived dictionary besides."""
    assert (
        news.subject_of(
            "Blitzy Raises $200M At $1.4B Valuation For Autonomous Software Development",
            {"autonomous": "autonomous.ai"},
        )
        is None
    )


def test_the_dictionary_is_every_domain_we_hold_not_the_47_we_bought(conn):
    """The bought directory covers 47 of 9,681 companies, and against a year of
    real headlines it matched zero valuations. A domain is a name its owner
    registered: `anthropic.com` is Anthropic. Generic labels stay out, because
    "scale" and "intelligence" are words before they are anyone's name."""
    for domain in ("anthropic.com", "intelligence.dev", "ab.io"):
        company_db.upsert_seeds(conn, [CompanySeed(domain=domain, stars=1, repos=1)], now=NOW)
    conn.commit()

    names = news.known_names(conn)

    assert names.get("anthropic") == "anthropic.com"
    assert "intelligence" not in names  # generic
    assert "ab" not in names  # under the floor


def test_a_contested_label_goes_to_the_domain_with_the_following(conn):
    """85 labels here are claimed by more than one domain, and almost every one
    is a single company holding several (`openai.com` and `openai.fm`). Dropping
    them all loses OpenAI; resolving them by SQLite's row order is a coin toss.
    The press writes about the domain people have heard of."""
    company_db.upsert_seeds(
        conn,
        [
            CompanySeed(domain="openai.fm", stars=2_894, repos=1),
            CompanySeed(domain="openai.com", stars=215_345, repos=37),
            CompanySeed(domain="distinctive.io", stars=10, repos=1),
        ],
        now=NOW,
    )
    conn.commit()

    names = news.known_names(conn)

    assert names["openai"] == "openai.com"
    assert names["distinctive"] == "distinctive.io"
