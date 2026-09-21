"""Rounds, totals and acquisitions, out of Crunchbase's own archive.

Two queries answer different questions and cost differently, so they are kept
apart.

**Rounds.** `roundsDatabase` selects funding rounds by type, amount and date
with no company list at all — "every round over $1m announced since August" is
one run of a few hundred rows. The amount and the announcement date are gated
behind a Crunchbase Pro login for anonymous callers; the actor runs a paid seat,
so they come back filled in. This is what the Funded board is built from, and
it finds companies we have never heard of, which is the point.

**Companies.** One row per company carries total raised, investor count,
employee band, IPO status and both directions of M&A, for $0.008. That is the
Valuation and Acquired boards — but only for companies we can name.

Which brings up the part the plan called the riskiest step. We hold domains;
Crunchbase wants a name or a slug. The guess is cheap and often right —
`langchain.com` is `langchain` — but a wrong guess does not fail, it returns
somebody else's company and files it under ours. So every returned profile is
checked against the domain it was asked for: a profile whose own website is not
that domain is recorded as a mismatch and never treated as a match. Knowing a
guess was wrong is what stops it being made again.

Valuation is the one figure none of this exposes as a field. Crunchbase's news
mode reports it in headlines — "Mistral AI Raises At $24B Valuation" — and that
is where it comes from, stored with the article that said it and labelled as
press-reported. It is never inferred from the money raised.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

from airadar.companies.domains import registrable_domain

log = logging.getLogger(__name__)

# Round types worth watching. Deliberately not every type Crunchbase has:
# `post_ipo_debt` and `secondary_market` are not a startup raising money, and
# on a board called "Funded" they would be noise.
WATCHED_ROUND_TYPES = (
    "pre_seed",
    "seed",
    "angel",
    "series_a",
    "series_b",
    "series_c",
    "series_d",
    "series_e",
    "series_f",
    "series_g",
    "series_unknown",
    "corporate_round",
    "private_equity",
    "convertible_note",
    "grant",
)

# A round below this is almost always an accelerator cheque or an undisclosed
# placeholder, and there are thousands of them. The board is about movement
# worth noticing.
MIN_ROUND_USD = 1_000_000


@dataclass
class FundingReport:
    rounds_seen: int = 0
    rounds_written: int = 0
    companies_asked: int = 0
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    # Two domains guessed the same slug, so only one of them was really asked
    # about. Counted apart from `missing`, which means Crunchbase answered and
    # had nothing.
    ambiguous: int = 0
    acquisitions: int = 0
    cost_usd: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.rounds_written}/{self.rounds_seen} rounds, "
            f"{self.matched} matched of {self.companies_asked} asked "
            f"({self.mismatched} wrong company, {self.missing} unknown, "
            f"{self.ambiguous} slug taken), "
            f"{self.acquisitions} acquisitions, ${self.cost_usd:.2f}"
        )


def _date(value: object) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _usd(value: object) -> int | None:
    """A money figure, or None. Never zero as a stand-in for unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value > 0:
        return int(value)
    return None


def slug_candidate(domain: str) -> str:
    """The Crunchbase slug a domain most likely maps to.

    `langchain.com` -> `langchain`. A guess, and treated as one: nothing is
    stored as a match until the profile that comes back says it lives here.
    """
    return domain.rsplit(".", 1)[0].replace(".", "-")


def round_rows(items: list[dict], *, collected_at: dt.datetime) -> list[dict]:
    """Funding-round rows from a rounds-mode run.

    A round with no disclosed amount is kept — that a round happened is the
    news, and a board sorted by amount simply leaves it below the ones with a
    figure rather than inventing one.
    """
    rows: list[dict] = []
    for item in items:
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        name = (item.get("companyName") or company.get("name") or "").strip()
        if not name:
            continue
        permalink = item.get("companyPermalink") or company.get("permalink")
        key = (
            item.get("roundId")
            or item.get("uuid")
            or item.get("id")
            # Nothing usable as an id: build one that is stable across runs so a
            # re-run updates the round instead of filing a second copy.
            or f"{permalink or name}:{item.get('investmentType')}:{item.get('announcedOn')}"
        )
        investors = item.get("investors")
        if isinstance(investors, list):
            investors = ", ".join(
                str(i.get("name") if isinstance(i, dict) else i) for i in investors
            )
        rows.append(
            {
                "round_key": f"cb:{key}",
                "company_name": name,
                "company_domain": registrable_domain(company.get("website")),
                "cb_permalink": permalink,
                "round_type": item.get("investmentType") or item.get("roundType"),
                "amount_usd": _usd(item.get("moneyRaisedUsd") or item.get("moneyRaised")),
                "announced_on": _date(item.get("announcedOn") or item.get("announced_on")),
                "investors": investors or None,
                "source": "crunchbase",
                "source_url": item.get("url") or item.get("companyUrl"),
                "collected_at": collected_at,
            }
        )
    return rows


def company_row(item: dict, *, asked_domain: str, collected_at: dt.datetime) -> dict | None:
    """A company's funding profile, or None if this is not that company.

    The check is the whole point: a name lookup that finds the wrong Acme
    returns a perfectly well-formed row, and without this it would be filed
    under ours and believed.
    """
    website = registrable_domain(item.get("website"))
    if website != asked_domain:
        return None

    funding = item.get("funding") if isinstance(item.get("funding"), dict) else {}
    rounds = funding.get("rounds") if isinstance(funding.get("rounds"), list) else []
    last = rounds[0] if rounds else {}
    people = item.get("people") if isinstance(item.get("people"), dict) else {}

    return {
        "domain": asked_domain,
        "cb_permalink": item.get("permalink"),
        "total_usd": _usd(funding.get("totalUsd")),
        "rounds": funding.get("numFundingRounds"),
        "investors": funding.get("numInvestors"),
        "last_round": (last or {}).get("investmentType"),
        "last_round_on": _date((last or {}).get("announcedOn")),
        "employee_range": people.get("employeeRange"),
        "country": item.get("country"),
        "ipo_status": item.get("ipoStatus"),
        "valuation_usd": None,
        "valuation_src": None,
        "valuation_on": None,
        "collected_at": collected_at,
    }


def acquisition_rows(item: dict, *, domain: str, collected_at: dt.datetime) -> list[dict]:
    """Both directions of M&A for one company profile."""
    ma = item.get("ma") if isinstance(item.get("ma"), dict) else {}
    name = (item.get("name") or domain).strip()
    rows: list[dict] = []

    bought_by = ma.get("acquiredBy")
    if isinstance(bought_by, dict) and bought_by.get("name"):
        rows.append(
            {
                "acquirer": bought_by["name"],
                "target": name,
                "domain": domain,
                "announced_on": _date(bought_by.get("announcedOn")),
                "amount_usd": _usd(bought_by.get("priceUsd") or bought_by.get("price")),
                "source": "crunchbase",
                "collected_at": collected_at,
            }
        )

    for bought in ma.get("acquisitions") or []:
        if not isinstance(bought, dict) or not bought.get("name"):
            continue
        rows.append(
            {
                "acquirer": name,
                "target": bought["name"],
                "domain": domain,
                "announced_on": _date(bought.get("announcedOn")),
                "amount_usd": _usd(bought.get("priceUsd") or bought.get("price")),
                "source": "crunchbase",
                "collected_at": collected_at,
            }
        )
    return rows


# A headline is the only place any of these sources states a valuation, so it
# is read from one — and stored as what it is, a press report with a link.
_VALUATION = re.compile(
    r"\$\s?([0-9]+(?:\.[0-9]+)?)\s?([BMT])(?:illion)?\b[^.]{0,40}?valuation"
    r"|valuation[^.]{0,40}?\$\s?([0-9]+(?:\.[0-9]+)?)\s?([BMT])(?:illion)?\b",
    re.IGNORECASE,
)
_SCALE = {"M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}


def valuation_from_headline(text: str) -> int | None:
    """The valuation a headline states, in dollars, or None.

    Deliberately narrow. "Raises $200M" is a round, not a valuation, and the
    two being confused would put a number on the Valuation board that is off by
    an order of magnitude — worse than showing nothing.
    """
    match = _VALUATION.search(text or "")
    if not match:
        return None
    amount = match.group(1) or match.group(3)
    scale = (match.group(2) or match.group(4) or "").upper()
    if not amount or scale not in _SCALE:
        return None
    return int(float(amount) * _SCALE[scale])
