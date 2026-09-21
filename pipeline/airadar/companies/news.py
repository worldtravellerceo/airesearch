"""Press-reported valuations, from Crunchbase News directly.

A valuation is the one figure none of the paid sources exposes as a field. It
exists in headlines — "Mistral AI Raises $3.5B At $24B Valuation" — and the
Apify actor will sell them at $0.008 an article.

It does not have to. Crunchbase News runs on WordPress and its REST API is
public, unauthenticated and unmetered: the same headlines, the same dates, the
same links, for nothing. The actor's advantage is that it also returns which
companies each article is about; here that has to be recovered from the
headline, which is the trade being made.

Recovered against the bought directory rather than by parsing English: a
headline is scanned for any company name we already hold, and the first one to
appear is taken as the subject. The rest of a Crunchbase News headline is
investors and acquirees — "Socure Secures $156M at $5.2B Valuation, Acquires
AI Fraud Investigation Startup" — and giving them the same figure would put a
valuation on whoever got a mention.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import sqlite3

import httpx

from airadar.companies.funding import valuation_from_headline

log = logging.getLogger(__name__)

API = "https://news.crunchbase.com/wp-json/wp/v2/posts"
PAGE_SIZE = 100
# Long enough that a word boundary makes an accidental hit unlikely. "Lyte" is
# a real company and four characters; three would start matching prepositions.
MIN_NAME_CHARS = 4
SEARCH_TERMS = ("valuation", "valued at", "raises at")


def _clean(title: str) -> str:
    """WordPress renders curly quotes and entities; the regex wants neither."""
    return html.unescape(title or "").replace("’", "'").replace("‘", "'")


def subject_of(headline: str, names: dict[str, str]) -> str | None:
    """The company a headline is about: the first known name to appear in it.

    `names` maps a lowercased company name to its domain. Matching is on word
    boundaries, so `Lyte` does not match `Lytespeed` and `AI` — which is below
    the length floor anyway — never matches at all.
    """
    lowered = headline.lower()
    best: tuple[int, str] | None = None
    for name, domain in names.items():
        if len(name) < MIN_NAME_CHARS:
            continue
        match = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), domain)
    return best[1] if best else None


async def fetch_headlines(
    *,
    since: dt.date,
    limit: int = 400,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Every article mentioning a valuation since `since`, newest first."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    seen: dict[str, dict] = {}
    try:
        for term in SEARCH_TERMS:
            page = 1
            while len(seen) < limit:
                response = await client.get(
                    API,
                    params={
                        "search": term,
                        "per_page": min(PAGE_SIZE, limit),
                        "page": page,
                        "after": f"{since.isoformat()}T00:00:00",
                        "_fields": "title,link,date",
                    },
                )
                if response.status_code >= 400:
                    # Past the last page WordPress answers 400, which is the
                    # documented way this API says "no more".
                    break
                items = response.json()
                if not items:
                    break
                for item in items:
                    seen.setdefault(item["link"], item)
                if len(items) < PAGE_SIZE:
                    break
                page += 1
    finally:
        if owned:
            await client.aclose()
    return list(seen.values())[:limit]


def known_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Lowercased company name -> domain, for companies this index tracks.

    Drawn from the bought Crunchbase directory rather than from our own
    guesses, and only for domains we actually hold: a valuation for a company
    nobody here tracks is venture news, not an entry in this index.
    """
    rows = conn.execute(
        """
        SELECT d.name, c.domain FROM crunchbase_directory d
          JOIN companies c ON c.domain = d.domain
         WHERE d.name IS NOT NULL AND length(d.name) >= ?
        """,
        (MIN_NAME_CHARS,),
    ).fetchall()
    return {row["name"].strip().lower(): row["domain"] for row in rows}


async def refresh_valuations(
    conn: sqlite3.Connection,
    *,
    limit: int = 400,
    since: dt.date | None = None,
    now: dt.datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Read valuations out of Crunchbase News and attach them to our companies.

    Costs nothing. Stores nothing it cannot attribute: a figure needs both a
    company this index tracks and the article that reported it, because a
    valuation without a source is indistinguishable from one we made up.
    """
    from airadar.db import company as db

    now = now or dt.datetime.now(dt.UTC)
    since = since or (now.date() - dt.timedelta(days=365))
    report = {"articles": 0, "valuations": 0, "attached": 0, "cost_usd": 0.0}

    names = known_names(conn)
    if not names:
        log.warning("valuations: no company names to match against — buy the directory first")
        return report

    articles = await fetch_headlines(since=since, limit=limit, client=client)
    report["articles"] = len(articles)

    for article in articles:
        headline = _clean((article.get("title") or {}).get("rendered", ""))
        usd = valuation_from_headline(headline)
        if usd is None:
            continue
        report["valuations"] += 1

        domain = subject_of(headline, names)
        if domain is None:
            continue
        published = article.get("date")
        on = dt.date.fromisoformat(published[:10]) if isinstance(published, str) else None
        db.record_valuation(
            conn,
            domain,
            usd=usd,
            source_url=article.get("link") or "",
            on=on,
            collected_at=now,
        )
        report["attached"] += 1
    conn.commit()

    log.info(
        "valuations: %d articles, %d carried a figure, %d attached, $0.00",
        report["articles"],
        report["valuations"],
        report["attached"],
    )
    return report
