"""G2 ratings, and an honest account of what they can and cannot tell us.

G2 indexes software that buyers review. Most of this universe is not that: a
model lab, an inference server and a tensor library have no G2 page and never
will, and that is not a coverage failure to be chased. What G2 does cover —
the AI companies selling a product to a business — it covers with the one
signal none of the other sources has: whether the people paying for it like it.

The matching here is weaker than the Crunchbase side and the code says so
rather than hiding it. A G2 review carries the product's name and nothing that
identifies the company behind it — no website, no domain. So a guessed slug
that lands on a different company's product cannot be caught the way a
Crunchbase profile can, by reading its website back. All that can be checked is
that the product name resembles what was asked for, which rules out the gross
errors and not the subtle ones. Anything that does not clear that bar is
recorded as unmatched rather than stored.

The cost shape makes the sweep affordable anyway: the run is billed per review
found, so the thousands of companies with no G2 page cost only the run they
were asked in. A miss is written down too — asking again next month for a page
that does not exist is the expensive mistake.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Enough reviews to put a rating on, not enough to pay for precision nobody
# reads. At $0.001 a review this is 2.5 cents for a company that has a page.
REVIEWS_PER_PRODUCT = 25

_NOISE = re.compile(r"[^a-z0-9]+")


@dataclass
class G2Report:
    products_asked: int = 0
    products_found: int = 0
    reviews: int = 0
    rejected: int = 0
    cost_usd: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.products_found}/{self.products_asked} companies had a G2 page, "
            f"{self.reviews} reviews, {self.rejected} name mismatches, ${self.cost_usd:.2f}"
        )


def product_candidate(domain: str) -> str:
    """The G2 product slug a domain most likely maps to. A guess, like the rest.

    Measured over the 400 companies bought so far: 61 came back with a rating
    and 339 came back with zero reviews. Whether those 339 are companies with
    no G2 page or slugs guessed wrong cannot be told apart from here, and not
    for free either — g2.com answers 403 to every unpaid request, including a
    plain HEAD on a public product URL, which is the reason the paid actor
    exists at all. So the zeroes are recorded rather than retried: a second
    run costs the same money to learn the same nothing.
    """
    return domain.rsplit(".", 1)[0].replace(".", "-")


def _normalise(value: str) -> str:
    return _NOISE.sub("", (value or "").lower())


def names_agree(asked: str, product_name: str) -> bool:
    """Whether a returned product plausibly belongs to the company we asked about.

    All that can be checked without a website on the row. It catches
    `deepseek` coming back as `DeepL` and does not catch a genuinely
    same-named product from another company — which is why a match here is
    never described as verified.
    """
    left, right = _normalise(asked), _normalise(product_name)
    if not left or not right:
        return False
    return left in right or right in left


def rating_snapshot(
    items: list[dict],
    *,
    domain: str,
    asked_as: str,
    today: dt.date,
    collected_at: dt.datetime,
) -> dict | None:
    """Fold a run's reviews into one row per company.

    Returns a row with `reviews = 0` when nothing came back, because "this
    company has no G2 page" is a finding worth storing: without it the same
    empty lookup is paid for again every month.
    """
    ratings: Counter[int] = Counter()
    product_name = ""
    for item in items:
        rating = item.get("rating")
        if not isinstance(rating, int | float) or not 1 <= rating <= 5:
            continue
        name = item.get("productName") or ""
        if name and not names_agree(asked_as, name):
            return None
        product_name = product_name or name
        ratings[int(round(rating))] += 1

    total = sum(ratings.values())
    return {
        "domain": domain,
        "product_slug": asked_as,
        "collected_on": today,
        "reviews": total,
        "avg_rating": (
            round(sum(star * n for star, n in ratings.items()) / total, 2) if total else None
        ),
        "rating_1": ratings[1],
        "rating_2": ratings[2],
        "rating_3": ratings[3],
        "rating_4": ratings[4],
        "rating_5": ratings[5],
        "collected_at": collected_at,
    }
