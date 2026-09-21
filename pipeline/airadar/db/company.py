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


def set_name(conn: sqlite3.Connection, domain: str, name: str | None) -> None:
    """Record a company's own name for the first source that supplies one."""
    if not name:
        return
    conn.execute(
        "UPDATE companies SET name = ? WHERE domain = ? AND (name IS NULL OR name = '')",
        (name, domain),
    )
