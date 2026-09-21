"""The boards the second universe can actually support.

Two things are worth pinning here. A board with nothing in it must not be
written — an empty tab claims the question was asked and came back blank, when
the truth is that the source has not run. And every board has to do something
sensible with the missing values this data is full of: an undisclosed round
amount, a company with no G2 page, a valuation nobody published.
"""

import datetime as dt
import json

import pytest

from airadar import export_site
from airadar.companies import boards
from airadar.companies.domains import CompanySeed
from airadar.db import company as company_db
from airadar.db import repo as db

TODAY = dt.date(2026, 9, 21)
NOW = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)


def seed(conn, domain, stars=10_000, name=None):
    company_db.upsert_seeds(conn, [CompanySeed(domain=domain, stars=stars, repos=1)], now=NOW)
    if name:
        company_db.set_name(conn, domain, name)
    conn.commit()


def add_round(conn, name, *, amount, days_ago, domain=None, key=None):
    company_db.record_rounds(
        conn,
        [
            {
                "round_key": key or f"k{name}{days_ago}",
                "company_name": name,
                "company_domain": domain,
                "cb_permalink": None,
                "round_type": "series_b",
                "amount_usd": amount,
                "announced_on": TODAY - dt.timedelta(days=days_ago),
                "investors": None,
                "source": "crunchbase",
                "source_url": None,
                "collected_at": NOW,
            }
        ],
    )


def add_traffic(conn, domain, month, visits, *, genai=None):
    company_db.record_traffic(
        conn,
        [
            {
                "domain": domain,
                "month": month,
                "visits": visits,
                "global_rank": None,
                "category": None,
                "category_rank": None,
                "bounce_rate": None,
                "traffic_genai": genai,
                "collected_at": NOW,
            }
        ],
    )


# --- funded ----------------------------------------------------------------


def test_the_funded_board_is_the_last_quarter_by_size(conn):
    for domain in ("big.ai", "small.ai", "old.ai"):
        seed(conn, domain)
    add_round(conn, "Big AI", amount=500_000_000, days_ago=10, domain="big.ai")
    add_round(conn, "Small AI", amount=5_000_000, days_ago=40, domain="small.ai")
    add_round(conn, "Old News", amount=900_000_000, days_ago=200, domain="old.ai")

    rows = boards.funded(conn, today=TODAY)

    assert [row["company_name"] for row in rows] == ["Big AI", "Small AI"]


def test_an_undisclosed_round_is_shown_last_rather_than_hidden(conn):
    """That a company raised is the news. An undisclosed amount is a fact about
    the round, not a reason to leave it off the board."""
    seed(conn, "quiet.ai")
    seed(conn, "loud.ai")
    add_round(conn, "Quiet AI", amount=None, days_ago=5, domain="quiet.ai")
    add_round(conn, "Loud AI", amount=1_000_000, days_ago=5, domain="loud.ai")

    rows = boards.funded(conn, today=TODAY)

    assert [row["company_name"] for row in rows] == ["Loud AI", "Quiet AI"]


def test_a_round_for_a_company_we_track_carries_its_repo(conn):
    """The join is the whole point of having both universes: this company has
    340,000 stars and just raised, and neither source knows both."""
    seed(conn, "mistral.ai", stars=340_000)
    add_round(conn, "Mistral AI", amount=2_000_000_000, days_ago=13, domain="mistral.ai")

    row = boards.funded(conn, today=TODAY)[0]

    assert row["repo_stars"] == 340_000


# --- valuation -------------------------------------------------------------


def test_a_valuation_without_its_article_never_reaches_the_board(conn):
    """A valuation with no source is indistinguishable from one we made up."""
    seed(conn, "acme.ai")
    conn.execute(
        "INSERT INTO company_funding (domain, valuation_usd, collected_at) VALUES (?, ?, ?)",
        ("acme.ai", 5_000_000_000, NOW),
    )
    conn.commit()

    assert boards.valuations(conn) == []


def test_a_sourced_valuation_reaches_it(conn):
    seed(conn, "mistral.ai", name="Mistral AI")
    company_db.record_valuation(
        conn,
        "mistral.ai",
        usd=24_000_000_000,
        source_url="https://news.crunchbase.com/x",
        on=dt.date(2026, 9, 8),
        collected_at=NOW,
    )
    conn.commit()

    row = boards.valuations(conn)[0]

    assert row["valuation_usd"] == 24_000_000_000
    assert row["valuation_src"].startswith("https://")


# --- traffic ---------------------------------------------------------------


def test_each_company_is_ranked_on_its_own_latest_month(conn):
    """Similarweb publishes a site's snapshot when it publishes it. Holding
    everyone to the slowest one would empty the board for a month at a time."""
    seed(conn, "fast.ai")
    seed(conn, "slow.ai")
    add_traffic(conn, "fast.ai", dt.date(2026, 8, 1), 500_000)
    add_traffic(conn, "slow.ai", dt.date(2026, 7, 1), 900_000)

    rows = boards.traffic(conn)

    assert [row["domain"] for row in rows] == ["slow.ai", "fast.ai"]


def test_a_tiny_site_tripling_does_not_take_the_growth_board(conn):
    """400 visits to 4,000 is a 10x that means nothing, and without a floor
    those are the only rows a growth board ever shows."""
    seed(conn, "tiny.ai")
    add_traffic(conn, "tiny.ai", dt.date(2026, 7, 1), 400)
    add_traffic(conn, "tiny.ai", dt.date(2026, 8, 1), 4_000)
    seed(conn, "real.ai")
    add_traffic(conn, "real.ai", dt.date(2026, 7, 1), 1_000_000)
    add_traffic(conn, "real.ai", dt.date(2026, 8, 1), 1_500_000)

    rows = boards.rising(conn)

    assert [row["domain"] for row in rows] == ["real.ai"]
    assert rows[0]["growth"] == pytest.approx(1.5)


def test_the_ai_traffic_board_ranks_on_the_share_not_the_size(conn):
    seed(conn, "huge.ai")
    seed(conn, "aimade.ai")
    add_traffic(conn, "huge.ai", dt.date(2026, 8, 1), 90_000_000, genai=0.02)
    add_traffic(conn, "aimade.ai", dt.date(2026, 8, 1), 200_000, genai=0.41)

    assert [row["domain"] for row in boards.ai_traffic(conn)] == ["aimade.ai", "huge.ai"]


# --- G2 --------------------------------------------------------------------


def test_a_rating_on_two_reviews_is_not_ranked(conn):
    seed(conn, "lucky.ai")
    seed(conn, "real.ai")
    for domain, reviews, rating in [("lucky.ai", 2, 5.0), ("real.ai", 40, 4.6)]:
        company_db.record_g2(
            conn,
            {
                "domain": domain,
                "product_slug": domain.split(".")[0],
                "collected_on": TODAY,
                "reviews": reviews,
                "avg_rating": rating,
                "rating_1": 0,
                "rating_2": 0,
                "rating_3": 0,
                "rating_4": 0,
                "rating_5": reviews,
                "collected_at": NOW,
            },
        )
    conn.commit()

    assert [row["domain"] for row in boards.rated(conn)] == ["real.ai"]


def test_a_company_with_no_g2_page_is_not_shown_as_unrated(conn):
    """The miss is stored so it is not paid for again. It is not a board row."""
    seed(conn, "vllm.ai")
    company_db.record_g2(
        conn,
        {
            "domain": "vllm.ai",
            "product_slug": "vllm",
            "collected_on": TODAY,
            "reviews": 0,
            "avg_rating": None,
            "rating_1": 0,
            "rating_2": 0,
            "rating_3": 0,
            "rating_4": 0,
            "rating_5": 0,
            "collected_at": NOW,
        },
    )
    conn.commit()

    assert boards.rated(conn) == []


# --- the export ------------------------------------------------------------


def _score_something(conn):
    """The export needs a leaderboard date before it writes anything at all."""
    from airadar.collect import score

    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=1,
                full_name="acme/agent",
                owner="acme",
                name="agent",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                stars=5_000,
            )
        ],
    )
    db.save_classification(
        conn,
        1,
        is_ai=True,
        category="agent-framework",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    conn.commit()
    score(conn, today=TODAY)


def test_a_board_with_no_rows_is_not_written_at_all(tmp_path, conn):
    """An empty tab says the question was asked and came back blank. Until a
    source has run, the truth is that the tab should not be there."""
    _score_something(conn)

    export_site.export(conn, tmp_path, date=TODAY)

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["companies"] == []
    assert not (tmp_path / "companies").exists()


def test_a_board_with_rows_is_written_and_listed(tmp_path, conn):
    _score_something(conn)
    seed(conn, "mistral.ai", stars=340_000)
    add_round(conn, "Mistral AI", amount=2_000_000_000, days_ago=13, domain="mistral.ai")

    export_site.export(conn, tmp_path, date=TODAY)

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [b["slug"] for b in manifest["companies"]] == ["funded"]
    board = json.loads((tmp_path / "companies" / "funded.json").read_text())
    assert board["entries"][0]["company_name"] == "Mistral AI"
    assert board["title"] == "Yatırım alanlar"


def test_a_round_that_belongs_to_nobody_we_track_stays_off_the_board(conn):
    """The first paid run returned 400 rounds led by a music publisher, a
    satellite company and a defence manufacturer — Crunchbase's rounds query
    has no industry filter. None of the 400 could be tied to a company we
    track. This board sits under a heading that says these are AI companies,
    so the tie is the claim, not a decoration."""
    add_round(conn, "BMG Music Publishing", amount=1_250_000_000, days_ago=4)

    assert boards.funded(conn, today=TODAY) == []


def test_a_round_reaches_the_board_through_the_crunchbase_match(conn):
    """A round carries a Crunchbase permalink but no website. When we have
    already matched that permalink to one of our domains — and verified it by
    reading the profile's own website back — the round is attributable."""
    seed(conn, "mistral.ai", stars=340_000, name="Mistral AI")
    company_db.record_match(
        conn,
        "mistral.ai",
        state="matched",
        asked_as="mistral",
        permalink="mistral-ai",
        website="https://mistral.ai",
        checked_at=NOW,
    )
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
                "announced_on": TODAY - dt.timedelta(days=13),
                "investors": None,
                "source": "crunchbase",
                "source_url": None,
                "collected_at": NOW,
            }
        ],
    )
    conn.commit()

    rows = boards.funded(conn, today=TODAY)

    assert [row["company_name"] for row in rows] == ["Mistral AI"]
    assert rows[0]["repo_stars"] == 340_000
