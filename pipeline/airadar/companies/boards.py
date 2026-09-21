"""The company boards, read straight out of the tables the sources wrote.

The repository boards rank on `fresh_power`, which integrates a daily series
over a 180-day half-life. Nothing here gets a daily series: traffic is a
monthly estimate, a funding round is a single dated event, a G2 rating is a
snapshot. So these are not the same boards with a different subject — they are
the questions this data can actually answer.

Every board omits itself when it has no rows. An empty tab claims a question
was asked and came back blank; a missing one says the source has not run yet,
which is the truth until it has.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

# The window on the Funded board. A quarter is long enough that the board is
# never empty between raises and short enough that "just raised" still means it.
FUNDED_WINDOW_DAYS = 90
BOARD_LIMIT = 100


def funded(conn: sqlite3.Connection, *, today: dt.date, limit: int = BOARD_LIMIT) -> list[dict]:
    """Who in *this index* raised in the last quarter, largest first.

    The join is not a nicety, it is the whole claim. This board sits under a
    heading that says these are AI companies, and the first run proved that a
    round on its own carries no such guarantee: Crunchbase's rounds query
    filters by type, amount and date and has no industry filter, so 400 rounds
    came back led by a music publisher, a satellite company and a defence
    manufacturer. None of the 400 could be tied to a company we track, by
    domain or by name.

    So a round appears only when it belongs to a company in our universe. That
    is a strict bar and it empties the board until the rounds are sourced with
    something that identifies the company — which is correct. A board that is
    absent says the question has not been answered; a board full of venture
    news says it has, wrongly.

    Rounds with no disclosed amount are kept and sort last: that a company
    raised at all is the news, and an undisclosed amount is a fact about the
    round rather than a reason to hide it.
    """
    since = today - dt.timedelta(days=FUNDED_WINDOW_DAYS)
    rows = conn.execute(
        """
        SELECT r.company_name, r.company_domain, r.round_type, r.amount_usd,
               r.announced_on, r.investors, r.source, r.source_url,
               c.repo_stars, c.top_repo, c.name
        FROM funding_round r
        JOIN companies c
          ON c.domain = r.company_domain
          OR c.domain = (SELECT m.domain FROM company_crunchbase m
                          WHERE m.permalink = r.cb_permalink AND m.match_state = 'matched')
        WHERE r.announced_on >= :since
        ORDER BY r.amount_usd IS NULL, r.amount_usd DESC, r.announced_on DESC
        LIMIT :limit
        """,
        {"since": since, "limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def valuations(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Last known valuation, largest first.

    Every figure here was reported by a named article rather than measured, so
    the article travels with it and the board has no row without one.
    """
    rows = conn.execute(
        """
        SELECT f.domain, c.name, f.valuation_usd, f.valuation_src, f.valuation_on,
               f.total_usd, f.rounds, c.repo_stars, c.top_repo
        FROM company_funding f
        JOIN companies c ON c.domain = f.domain
        WHERE f.valuation_usd IS NOT NULL AND f.valuation_src IS NOT NULL
        ORDER BY f.valuation_usd DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def raised(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Total raised to date, largest first."""
    rows = conn.execute(
        """
        SELECT f.domain, c.name, f.total_usd, f.rounds, f.investors, f.last_round,
               f.last_round_on, f.employee_range, f.country, c.repo_stars, c.top_repo
        FROM company_funding f
        JOIN companies c ON c.domain = f.domain
        WHERE f.total_usd IS NOT NULL
        ORDER BY f.total_usd DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def acquired(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Acquisitions, newest first. Both directions — bought and bought by."""
    rows = conn.execute(
        """
        SELECT a.acquirer, a.target, a.domain, a.announced_on, a.amount_usd, a.source,
               c.repo_stars, c.top_repo
        FROM acquisition a
        LEFT JOIN companies c ON c.domain = a.domain
        ORDER BY a.announced_on IS NULL, a.announced_on DESC, a.amount_usd DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def traffic(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Most-visited, on the latest month each company has a reading for.

    The month is per company rather than global: Similarweb publishes a site's
    snapshot when it publishes it, and holding everyone to the slowest one
    would empty the board for a month at a time.
    """
    rows = conn.execute(
        """
        SELECT t.domain, c.name, t.month, t.visits, t.global_rank, t.category,
               t.traffic_genai, c.repo_stars, c.top_repo
        FROM company_traffic t
        JOIN companies c ON c.domain = t.domain
        WHERE t.month = (SELECT max(month) FROM company_traffic x WHERE x.domain = t.domain)
          AND t.visits IS NOT NULL
        ORDER BY t.visits DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def rising(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Biggest month-over-month growth in visits.

    Three monthly points is all this source gives, so this is a ratio between
    two of them and is called that. It is not `fresh_power` and does not
    pretend to be — a decay integral over three points would produce a number
    that looks like the repository one and means nothing like it.

    The floor on the earlier month is what keeps the board honest: a site going
    from 400 visits to 4,000 is a 10x that means nothing, and without a floor
    those are the only rows a growth board ever shows.
    """
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT domain, max(month) AS month FROM company_traffic
            WHERE visits IS NOT NULL GROUP BY domain
        )
        SELECT t.domain, c.name, t.month, t.visits, p.visits AS prev_visits,
               t.traffic_genai, c.repo_stars, c.top_repo,
               (t.visits * 1.0 / p.visits) AS growth
        FROM latest l
        JOIN company_traffic t ON t.domain = l.domain AND t.month = l.month
        JOIN company_traffic p ON p.domain = l.domain
             AND p.month = date(l.month, '-1 month')
        JOIN companies c ON c.domain = t.domain
        WHERE p.visits >= 10000 AND t.visits IS NOT NULL
        ORDER BY growth DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def ai_traffic(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Largest share of visits arriving from an AI assistant.

    There is nowhere else to read this, and on an index of AI companies it is
    the most interesting column the paid source buys: which of them are
    themselves being found through ChatGPT, Claude, Gemini and Perplexity
    rather than through search.
    """
    rows = conn.execute(
        """
        SELECT t.domain, c.name, t.month, t.traffic_genai, t.visits, c.repo_stars,
               c.top_repo
        FROM company_traffic t
        JOIN companies c ON c.domain = t.domain
        WHERE t.month = (SELECT max(month) FROM company_traffic x WHERE x.domain = t.domain)
          AND t.traffic_genai IS NOT NULL AND t.visits >= 10000
        ORDER BY t.traffic_genai DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


def rated(conn: sqlite3.Connection, *, limit: int = BOARD_LIMIT) -> list[dict]:
    """Best-rated on G2, among the companies that have a page there at all.

    A rating on five reviews is noise, so the board asks for a handful before
    it will rank anything.
    """
    rows = conn.execute(
        """
        SELECT g.domain, c.name, g.product_slug, g.reviews, g.avg_rating,
               g.collected_on, c.repo_stars, c.top_repo
        FROM company_g2 g
        JOIN companies c ON c.domain = g.domain
        WHERE g.collected_on = (
                  SELECT max(collected_on) FROM company_g2 x
                  WHERE x.domain = g.domain AND x.product_slug = g.product_slug)
          AND g.reviews >= 5 AND g.avg_rating IS NOT NULL
        ORDER BY g.avg_rating DESC, g.reviews DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return [dict(row) for row in rows]


#: Board slug -> (builder, Turkish title, what the board answers).
BOARDS = {
    "funded": (funded, "Yatırım alanlar", "Son 90 günde tur açıklayan şirketler, tutara göre."),
    "valuation": (
        valuations,
        "Değerlemeler",
        "Basında geçen son değerleme. Her satırda haberin linki var — ölçülmüş değil, "
        "bildirilmiş bir rakam.",
    ),
    "raised": (raised, "Toplam yatırım", "Bugüne dek toplanan para."),
    "acquired": (acquired, "Satın almalar", "Kim kimi aldı, en yenisi önce."),
    "traffic": (traffic, "En çok ziyaret", "Aylık tahmini ziyaret. Ölçüm değil, tahmin."),
    "rising": (
        rising,
        "Yükselenler",
        "Aya göre en çok büyüyen trafik. Üç aylık veriden iki nokta arasındaki oran.",
    ),
    "ai-traffic": (
        ai_traffic,
        "AI'dan gelen trafik",
        "Ziyaretlerinin en büyük kısmı ChatGPT, Claude, Gemini ve Perplexity'den gelen şirketler.",
    ),
    "rated": (rated, "G2 puanı", "G2 sayfası olan şirketler arasında en iyi puan alanlar."),
}
