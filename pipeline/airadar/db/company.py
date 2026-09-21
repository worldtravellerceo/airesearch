"""Reading and writing the company universe.

Keyed on the registrable domain throughout — see the schema comment on
`companies` for why the domain and not a name.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence

from airadar.companies.domains import CompanySeed


def upsert_seeds(
    conn: sqlite3.Connection,
    seeds: Sequence[CompanySeed],
    *,
    source: str = "corpus",
    now: dt.datetime | None = None,
) -> int:
    """Insert or refresh companies from a seed list.

    `first_seen_at` and `seed_source` survive a re-run: when a company later
    arrives from a second source, the record of where it came from first is the
    interesting one.
    """
    if not seeds:
        return 0
    now = now or dt.datetime.now(dt.UTC)
    conn.executemany(
        """
        INSERT INTO companies
            (domain, first_seen_at, seed_source, repo_stars, repo_count, top_repo, gh_owners)
        VALUES (:domain, :now, :source, :stars, :repos, :top_repo, :owners)
        ON CONFLICT(domain) DO UPDATE SET
            repo_stars = excluded.repo_stars,
            repo_count = excluded.repo_count,
            top_repo   = excluded.top_repo,
            gh_owners  = excluded.gh_owners
        """,
        [
            {
                "domain": seed.domain,
                "now": now,
                "source": source,
                "stars": seed.stars,
                "repos": seed.repos,
                "top_repo": seed.top_repo,
                "owners": ",".join(sorted(seed.owners)),
            }
            for seed in seeds
        ],
    )
    conn.commit()
    return len(seeds)


def companies_to_enrich(conn: sqlite3.Connection, *, limit: int, min_stars: int = 0) -> list[str]:
    """Domains worth paying to look up, least recently measured first.

    Ordered so that a run bounded by budget spends it on the companies that
    have been waiting longest, and within those on the biggest — never on the
    same head of the list every time.
    """
    rows = conn.execute(
        """
        SELECT c.domain,
               (SELECT max(collected_at) FROM company_traffic t WHERE t.domain = c.domain) AS seen
        FROM companies c
        WHERE c.repo_stars >= :min_stars
        ORDER BY seen IS NOT NULL, seen ASC, c.repo_stars DESC
        LIMIT :limit
        """,
        {"limit": limit, "min_stars": min_stars},
    ).fetchall()
    return [row["domain"] for row in rows]


def record_traffic(conn: sqlite3.Connection, rows: Sequence[dict]) -> int:
    """Store monthly traffic points. Re-running a month replaces it."""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO company_traffic
            (domain, month, visits, global_rank, category, category_rank,
             bounce_rate, traffic_genai, collected_at)
        VALUES (:domain, :month, :visits, :global_rank, :category, :category_rank,
                :bounce_rate, :traffic_genai, :collected_at)
        ON CONFLICT(domain, month) DO UPDATE SET
            visits        = excluded.visits,
            global_rank   = COALESCE(excluded.global_rank, company_traffic.global_rank),
            category      = COALESCE(excluded.category, company_traffic.category),
            category_rank = COALESCE(excluded.category_rank, company_traffic.category_rank),
            bounce_rate   = COALESCE(excluded.bounce_rate, company_traffic.bounce_rate),
            traffic_genai = COALESCE(excluded.traffic_genai, company_traffic.traffic_genai),
            collected_at  = excluded.collected_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


#: How much a source's idea of a company's name is worth. First writer used to
#: win, which handed every name to whichever paid run happened to go first.
NAME_SOURCES: tuple[str, ...] = ("corpus", "crunchbase")


def set_name(
    conn: sqlite3.Connection, domain: str, name: str | None, *, source: str = "corpus"
) -> None:
    """Record a company's name, letting a better source replace a worse one.

    `source` has to be one this function knows about. That is deliberate:
    Similarweb returns the scraped HTML page title under `title`, and passing
    it here named `openclaw.ai` "The ClawCast Episode 1" and `claude.com`
    "Kundensupport | Claude" — 874 companies, 366 of them over forty characters
    long. It is not a source of names and there is no string it can pass.
    """
    if source not in NAME_SOURCES:
        raise ValueError(f"unknown name source {source!r}")
    if not name:
        return
    rank = NAME_SOURCES.index(source)
    conn.execute(
        """
        UPDATE companies SET name = :name, name_source = :source
         WHERE domain = :domain
           AND (name IS NULL OR name = '' OR :rank > COALESCE(
                 (SELECT CASE name_source
                         WHEN 'corpus' THEN 0 WHEN 'crunchbase' THEN 1 ELSE -1 END), -1))
        """,
        {"name": name, "source": source, "domain": domain, "rank": rank},
    )


# --- money -----------------------------------------------------------------


def record_rounds(conn: sqlite3.Connection, rows: Sequence[dict]) -> int:
    """Store funding rounds. The source's own round id keeps a re-run idempotent."""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO funding_round
            (round_key, company_name, company_domain, cb_permalink, round_type,
             amount_usd, announced_on, investors, source, source_url, collected_at)
        VALUES (:round_key, :company_name, :company_domain, :cb_permalink, :round_type,
                :amount_usd, :announced_on, :investors, :source, :source_url, :collected_at)
        ON CONFLICT(round_key) DO UPDATE SET
            company_domain = COALESCE(excluded.company_domain, funding_round.company_domain),
            amount_usd     = COALESCE(excluded.amount_usd, funding_round.amount_usd),
            announced_on   = COALESCE(excluded.announced_on, funding_round.announced_on),
            investors      = COALESCE(excluded.investors, funding_round.investors),
            collected_at   = excluded.collected_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def record_company_funding(conn: sqlite3.Connection, row: dict) -> None:
    """Replace a company's funding profile, keeping any valuation already found.

    The valuation comes from a headline, on its own schedule; a funding refresh
    must not wipe it just because this source has no field for it.
    """
    conn.execute(
        """
        INSERT INTO company_funding
            (domain, cb_permalink, total_usd, rounds, investors, last_round,
             last_round_on, employee_range, country, ipo_status,
             valuation_usd, valuation_src, valuation_on, collected_at)
        VALUES (:domain, :cb_permalink, :total_usd, :rounds, :investors, :last_round,
                :last_round_on, :employee_range, :country, :ipo_status,
                :valuation_usd, :valuation_src, :valuation_on, :collected_at)
        ON CONFLICT(domain) DO UPDATE SET
            cb_permalink   = excluded.cb_permalink,
            total_usd      = excluded.total_usd,
            rounds         = excluded.rounds,
            investors      = excluded.investors,
            last_round     = excluded.last_round,
            last_round_on  = excluded.last_round_on,
            employee_range = excluded.employee_range,
            country        = excluded.country,
            ipo_status     = excluded.ipo_status,
            valuation_usd  = COALESCE(excluded.valuation_usd, company_funding.valuation_usd),
            valuation_src  = COALESCE(excluded.valuation_src, company_funding.valuation_src),
            valuation_on   = COALESCE(excluded.valuation_on, company_funding.valuation_on),
            collected_at   = excluded.collected_at
        """,
        row,
    )


def record_valuation(
    conn: sqlite3.Connection,
    domain: str,
    *,
    usd: int,
    source_url: str,
    on: dt.date | None,
    collected_at: dt.datetime,
) -> None:
    """Store a press-reported valuation, newest wins.

    Kept only with the article that stated it: a valuation with no source is
    indistinguishable from one we made up.
    """
    conn.execute(
        """
        INSERT INTO company_funding (domain, valuation_usd, valuation_src, valuation_on,
                                     collected_at)
        VALUES (:domain, :usd, :src, :on, :collected_at)
        ON CONFLICT(domain) DO UPDATE SET
            valuation_usd = excluded.valuation_usd,
            valuation_src = excluded.valuation_src,
            valuation_on  = excluded.valuation_on
        WHERE company_funding.valuation_on IS NULL
           OR excluded.valuation_on IS NULL
           OR excluded.valuation_on >= company_funding.valuation_on
        """,
        {"domain": domain, "usd": usd, "src": source_url, "on": on, "collected_at": collected_at},
    )


def record_acquisitions(conn: sqlite3.Connection, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO acquisition (acquirer, target, domain, announced_on, amount_usd,
                                 source, collected_at)
        VALUES (:acquirer, :target, :domain, :announced_on, :amount_usd, :source,
                :collected_at)
        ON CONFLICT(acquirer, target, COALESCE(announced_on, '')) DO UPDATE SET
            amount_usd   = COALESCE(excluded.amount_usd, acquisition.amount_usd),
            domain       = COALESCE(excluded.domain, acquisition.domain),
            collected_at = excluded.collected_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def record_match(
    conn: sqlite3.Connection,
    domain: str,
    *,
    state: str,
    asked_as: str,
    permalink: str | None = None,
    name: str | None = None,
    website: str | None = None,
    checked_at: dt.datetime | None = None,
) -> None:
    """Record how a domain mapped onto Crunchbase, including when it did not.

    A failed match has to be a stored state rather than a missing row. Without
    it the same wrong guess is made, and paid for, every month.
    """
    conn.execute(
        """
        INSERT INTO company_crunchbase (domain, permalink, name, website, match_state,
                                        asked_as, checked_at)
        VALUES (:domain, :permalink, :name, :website, :state, :asked_as, :checked_at)
        ON CONFLICT(domain) DO UPDATE SET
            permalink   = excluded.permalink,
            name        = excluded.name,
            website     = excluded.website,
            match_state = excluded.match_state,
            asked_as    = excluded.asked_as,
            checked_at  = excluded.checked_at
        """,
        {
            "domain": domain,
            "permalink": permalink,
            "name": name,
            "website": website,
            "state": state,
            "asked_as": asked_as,
            "checked_at": checked_at or dt.datetime.now(dt.UTC),
        },
    )


def companies_to_match(conn: sqlite3.Connection, *, limit: int, min_stars: int = 0) -> list[str]:
    """Domains never looked up on Crunchbase, biggest first.

    Unlike the traffic queue this one does not come round again: a company's
    profile is asked for once, and a mismatch or a miss is remembered so the
    same money is not spent on the same wrong answer.

    `ambiguous` is the exception, and the reason the state exists. It means
    another domain guessed the same slug first, so this one was never actually
    asked about — treating that as an answer would drop it from every future
    run for a question nobody put.
    """
    rows = conn.execute(
        """
        SELECT c.domain FROM companies c
        LEFT JOIN company_crunchbase m ON m.domain = c.domain
        WHERE c.repo_stars >= :min_stars
          AND (m.domain IS NULL OR m.match_state = 'ambiguous')
        ORDER BY c.repo_stars DESC
        LIMIT :limit
        """,
        {"limit": limit, "min_stars": min_stars},
    ).fetchall()
    return [row["domain"] for row in rows]


def companies_to_rate(conn: sqlite3.Connection, *, limit: int, min_stars: int = 0) -> list[str]:
    """Domains never asked about on G2, biggest first."""
    rows = conn.execute(
        """
        SELECT c.domain FROM companies c
        LEFT JOIN company_g2 g ON g.domain = c.domain
        WHERE c.repo_stars >= :min_stars AND g.domain IS NULL
        ORDER BY c.repo_stars DESC
        LIMIT :limit
        """,
        {"limit": limit, "min_stars": min_stars},
    ).fetchall()
    return [row["domain"] for row in rows]


def record_g2(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO company_g2 (domain, product_slug, collected_on, reviews, avg_rating,
                                rating_1, rating_2, rating_3, rating_4, rating_5,
                                collected_at)
        VALUES (:domain, :product_slug, :collected_on, :reviews, :avg_rating,
                :rating_1, :rating_2, :rating_3, :rating_4, :rating_5, :collected_at)
        ON CONFLICT(domain, product_slug, collected_on) DO UPDATE SET
            reviews    = excluded.reviews,
            avg_rating = excluded.avg_rating,
            rating_1   = excluded.rating_1,
            rating_2   = excluded.rating_2,
            rating_3   = excluded.rating_3,
            rating_4   = excluded.rating_4,
            rating_5   = excluded.rating_5
        """,
        row,
    )


# --- the bought dictionary -------------------------------------------------


def record_directory(conn: sqlite3.Connection, rows: Sequence[dict]) -> int:
    """Store Crunchbase's own company list, keyed on permalink.

    Bought rather than guessed. The slug guess was right 19% of the time, and a
    wrong guess is not a failure — it is a different real company filed under
    our domain.
    """
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO crunchbase_directory
            (permalink, name, website, domain, categories, country, fetched_at)
        VALUES (:permalink, :name, :website, :domain, :categories, :country, :fetched_at)
        ON CONFLICT(permalink) DO UPDATE SET
            name = excluded.name, website = excluded.website,
            domain = COALESCE(excluded.domain, crunchbase_directory.domain),
            categories = excluded.categories, country = excluded.country,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def attach_rounds_to_domains(conn: sqlite3.Connection) -> int:
    """Resolve each round's Crunchbase permalink to a domain, where we know it.

    A round row carries no website — that is the actor's shape, not a parsing
    failure — so this is the only way a round reaches the company it belongs
    to. Run after the directory is refreshed.
    """
    cursor = conn.execute(
        """
        UPDATE funding_round
           SET company_domain = (
                 SELECT d.domain FROM crunchbase_directory d
                  WHERE d.permalink = funding_round.cb_permalink AND d.domain IS NOT NULL)
         WHERE company_domain IS NULL
           AND cb_permalink IS NOT NULL
           AND EXISTS (SELECT 1 FROM crunchbase_directory d
                        WHERE d.permalink = funding_round.cb_permalink
                          AND d.domain IS NOT NULL)
        """
    )
    conn.commit()
    return cursor.rowcount


def match_from_directory(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> int:
    """Match our companies to Crunchbase by domain, with nothing guessed.

    A row here is the strongest identity this universe has: the company's own
    Crunchbase profile lists this exact domain as its website.
    """
    now = now or dt.datetime.now(dt.UTC)
    rows = conn.execute(
        """
        SELECT c.domain, d.permalink, d.name, d.website
          FROM companies c
          JOIN crunchbase_directory d ON d.domain = c.domain
         WHERE NOT EXISTS (SELECT 1 FROM company_crunchbase m
                            WHERE m.domain = c.domain AND m.match_state = 'matched')
        """
    ).fetchall()
    for row in rows:
        record_match(
            conn,
            row["domain"],
            state="matched",
            asked_as="directory",
            permalink=row["permalink"],
            name=row["name"],
            website=row["website"],
            checked_at=now,
        )
        set_name(conn, row["domain"], row["name"], source="crunchbase")
    conn.commit()
    return len(rows)
