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
# The default `python-httpx/x.y` is refused by the site's nginx with a 403,
# which the pager below used to read as "no more pages" — so this whole channel
# returned zero articles from the day it was written and said nothing. Say who
# we are instead; it is also the polite thing to do to a free API.
USER_AGENT = "airadar/1.0 (+https://github.com/worldtravellerceo/airesearch)"
PAGE_SIZE = 100
# Long enough that a word boundary makes an accidental hit unlikely. "Lyte" is
# a real company and four characters; three would start matching prepositions.
MIN_NAME_CHARS = 4
# A name derived from a domain is a weaker claim than one bought from the
# directory, so it has to be longer before it is allowed to identify anybody.
DERIVED_MIN_CHARS = 5
SEARCH_TERMS = ("valuation", "valued at", "raises at")

#: Domain labels that are English before they are anybody's name. Measured
#: against a year of real headlines: `autonomous.ai` claimed "Blitzy Raises
#: $200M At $1.4B Valuation For Autonomous Software Development" and
#: `intelligence.dev` claimed "AI Lab Ricursive Intelligence Lands $300M".
GENERIC_LABELS: frozenset[str] = frozenset(
    {
        "agents",
        "assistant",
        "autonomous",
        "banking",
        "browser",
        "capital",
        "character",
        "cloud",
        "context",
        "copilot",
        "data",
        "digital",
        "energy",
        "finance",
        "future",
        "general",
        "global",
        "health",
        "intelligence",
        "labs",
        "memory",
        "network",
        "neural",
        "open",
        "platform",
        "protocol",
        "quantum",
        "research",
        "robotics",
        "scale",
        "science",
        "security",
        "studio",
        "super",
        "systems",
        "technologies",
        "together",
        "venture",
        "vision",
    }
)

#: What a headline says when a company raises money. The subject of one of
#: these verbs is the company the figure belongs to; everything after it is
#: investors, acquirees and commentary.
FUNDING_VERB = re.compile(
    r"\b(raises?|raised|lands?|secures?|nears?|soars?|valued|valuation|closes?|hits?)\b"
)
#: How far before the verb the subject may sit. Measured: at three words every
#: match over a year of headlines is correct; at five, "Former Apple Engineers'
#: Physical AI Startup Lyte Raises $165M" is filed under Apple.
MAX_WORDS_BEFORE_VERB = 3


class NewsUnavailable(RuntimeError):
    """The feed refused us. Distinct from "no articles", which is a real answer.

    Silently equating the two is what hid a 403 for the lifetime of this
    channel: `valuation_usd` was NULL for all 134 companies and every run
    reported success.
    """


def _clean(title: str) -> str:
    """WordPress renders curly quotes and entities; the regex wants neither."""
    return html.unescape(title or "").replace("’", "'").replace("‘", "'")


def subject_of(headline: str, names: dict[str, str]) -> str | None:
    """The company a headline is about: the one raising the money.

    `names` maps a lowercased company name to its domain. Matching is on word
    boundaries, so `Lyte` does not match `Lytespeed` and `AI` — which is below
    the length floor anyway — never matches at all.

    The subject is the last known name standing within a few words *before* the
    funding verb. Taking the first name in the headline was the earlier rule
    and it is wrong in a way that only showed up against real headlines: the
    rest of a Crunchbase News headline is investors and acquirees, but so is
    the start of it — "Former Apple Engineers' Physical AI Startup Lyte Raises
    $165M At $1.6B Valuation" is not about Apple. Anchoring on the verb files
    it under Lyte, or under nobody when Lyte is not a company we track, which
    is the right answer either way.
    """
    lowered = headline.lower()
    verb = FUNDING_VERB.search(lowered)
    if verb is None:
        return None
    best: tuple[int, str] | None = None
    for name, domain in names.items():
        if len(name) < MIN_NAME_CHARS:
            continue
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered):
            if match.end() > verb.start():
                break
            between = re.findall(r"[a-z0-9$%.]+", lowered[match.end() : verb.start()])
            if len(between) <= MAX_WORDS_BEFORE_VERB and (best is None or match.start() > best[0]):
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
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": USER_AGENT}
    )
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
                if response.status_code == 400:
                    # Past the last page WordPress answers 400, which is the
                    # documented way this API says "no more".
                    break
                if response.status_code >= 400:
                    # Anything else is a refusal, not an answer. Reading a 403
                    # as "no more pages" is exactly how this channel managed to
                    # report success while fetching nothing, for weeks.
                    raise NewsUnavailable(
                        f"crunchbase news answered {response.status_code} for {term!r}"
                    )
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
    names: dict[str, str] = {}

    # A domain is a name its owner chose and registered, which is a better
    # claim than any guess — `anthropic.com` is Anthropic. Measured: the bought
    # directory covers 47 of the 9,681 companies here, and against a year of
    # Crunchbase News that matched zero valuations. The label carries the other
    # 9,634. Generic labels are excluded and the result is anchored on the
    # funding verb, because "scale" and "intelligence" are words before they
    # are anyone's name.
    # 85 labels here are claimed by more than one domain, and `setdefault`
    # resolved them by whatever order SQLite happened to return — a coin toss
    # with a real company on the other end of it.
    #
    # Dropping every contested label was the first answer and it is too blunt:
    # almost all of them are one company holding several domains
    # (`openai.com` and `openai.fm`, `claude.com` and `claude.ai`,
    # `pytorch.org` and `pytorch.kr`), and refusing them loses OpenAI. Two
    # checks were tried and neither separates the cases: a star ratio leaves
    # `apifox.cn` against `apifox.com`, and shared GitHub owners calls
    # `tensorflow.org` and `tensorflow.blog` different companies.
    #
    # So the label goes to the domain with the following, ordered last so it
    # wins the insert. This dictionary exists to name the subject of a funding
    # headline, and the press writes about the domain people have heard of —
    # `openai.fm` will never be the subject of a round that `openai.com` is
    # not. What it cannot survive is two genuinely different companies of
    # comparable size sharing a name, which is ambiguous to a reader too.
    for row in conn.execute(
        "SELECT domain FROM companies WHERE repo_stars > 0 ORDER BY repo_stars ASC"
    ):
        label = row["domain"].split(".")[0].replace("-", " ").strip().lower()
        if len(label) >= DERIVED_MIN_CHARS and label not in GENERIC_LABELS:
            names[label] = row["domain"]

    # The bought name wins where we have one: it is the company's own, not a
    # label that happens to be in front of a dot.
    rows = conn.execute(
        """
        SELECT d.name, c.domain FROM crunchbase_directory d
          JOIN companies c ON c.domain = d.domain
         WHERE d.name IS NOT NULL AND length(d.name) >= ?
        """,
        (MIN_NAME_CHARS,),
    ).fetchall()
    names.update({row["name"].strip().lower(): row["domain"] for row in rows})
    return names


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
