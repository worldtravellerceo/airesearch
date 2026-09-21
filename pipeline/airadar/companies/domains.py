"""Turning the repository corpus into a list of companies.

The corpus already holds 35,375 owners with an AI repository, and their
homepages point somewhere. That is a seed list nobody else builds the same way:
AI companies that ship open source. It costs nothing and it exists already.

Two things have to happen before it is usable. A homepage is a URL, not a
company — `docs.langchain.com` and `chat.deepseek.com` are the same companies as
`langchain.com` and `deepseek.com`, so every host is reduced to the domain under
its public suffix. And a great many homepages are not companies at all: the
first fifteen domains by stars include `arxiv.org`, `npmjs.com` and
`gymlibrary.dev`, which are a preprint server, a package registry and a
documentation site.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

import tldextract

# The bundled public-suffix snapshot, with no network fetch: a pipeline run must
# not depend on a third-party list being reachable, and the suffix list moves
# slowly enough that a snapshot is fine.
_extract = tldextract.TLDExtract(suffix_list_urls=())

#: Domains that are somewhere a project lives, not a company that made it. A
#: repository pointing at its own documentation, its package page or the paper
#: behind it is the common case, and every one of those would otherwise become a
#: company. Blocking a domain here does not exclude the company behind it —
#: Hugging Face and Replicate are real companies and arrive through Wikidata,
#: Cloudflare or Product Hunt instead. It only says the link is not evidence.
PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        # code and documentation hosting
        "github.io",
        "github.com",
        "gitlab.io",
        "gitlab.com",
        "gitee.com",
        "readthedocs.io",
        "readthedocs.org",
        "gitbook.io",
        "gitbook.com",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "herokuapp.com",
        "surge.sh",
        "streamlit.app",
        "glitch.me",
        "codeberg.org",
        "sourceforge.net",
        "hf.space",
        "gradio.app",
        "ngrok.io",
        "cloudfront.net",
        # package registries
        "npmjs.com",
        "pypi.org",
        "crates.io",
        "rubygems.org",
        "nuget.org",
        "packagist.org",
        "maven.org",
        "pkg.go.dev",
        "cocoapods.org",
        # papers and academia
        "arxiv.org",
        "openreview.net",
        "semanticscholar.org",
        "acm.org",
        "ieee.org",
        "springer.com",
        "nature.com",
        "researchgate.net",
        "biorxiv.org",
        "paperswithcode.com",
        "doi.org",
        "aclanthology.org",
        # model and dataset hosting (the companies themselves come from
        # elsewhere; a link here is usually a model card, not a homepage)
        "huggingface.co",
        "modelscope.cn",
        "kaggle.com",
        "wandb.ai",
        "civitai.com",
        "ollama.com",
        "openxlab.org.cn",
        # general publishing and social
        "medium.com",
        "substack.com",
        "notion.site",
        "notion.so",
        "wordpress.com",
        "blogspot.com",
        "youtube.com",
        "youtu.be",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "discord.gg",
        "discord.com",
        "reddit.com",
        "t.me",
        "telegram.me",
        "bilibili.com",
        "zhihu.com",
        "juejin.cn",
        "csdn.net",
        "weixin.qq.com",
        "qq.com",
        "google.com",
        "goo.gl",
        "bit.ly",
        "linktr.ee",
        "buymeacoffee.com",
        "patreon.com",
        "opencollective.com",
        "ko-fi.com",
        "apache.org",
        "wikipedia.org",
        "stackoverflow.com",
        "devpost.com",
        "figma.com",
        # URL shorteners: the destination is the company, the shortener is not.
        # `aka.ms` arrived through the live corpus with 193,500 stars and 46
        # owners behind it.
        "aka.ms",
        "tinyurl.com",
        "rb.gy",
        "shorturl.at",
        "cutt.ly",
        "lnk.to",
    }
)


@dataclass
class CompanySeed:
    """A candidate company, with the evidence that produced it."""

    domain: str
    owners: set[str] = field(default_factory=set)
    repos: int = 0
    stars: int = 0
    top_repo: str = ""
    top_repo_stars: int = 0

    def as_row(self) -> dict:
        return {
            "domain": self.domain,
            "owners": sorted(self.owners),
            "repos": self.repos,
            "stars": self.stars,
            "top_repo": self.top_repo,
        }


def registrable_domain(url: str | None) -> str | None:
    """The domain under the public suffix, or None if there isn't one.

    `https://docs.langchain.com/introduction` -> `langchain.com`. A bare host,
    a URL with a port and a URL with no scheme all work; anything that is not a
    hostname at all, including an IP address, returns None rather than a
    plausible-looking string.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        host = urlparse(raw if "//" in raw else "//" + raw).netloc
    except ValueError:
        # urlparse raises on a malformed bracketed IPv6 host.
        return None
    host = host.split("@")[-1].split(":")[0].lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    parsed = _extract(host)
    if not parsed.domain or not parsed.suffix:
        return None
    return f"{parsed.domain}.{parsed.suffix}"


def is_company_domain(domain: str | None) -> bool:
    """Whether a domain is worth treating as a company homepage."""
    return bool(domain) and domain not in PLATFORM_DOMAINS


def seeds_from_corpus(conn: sqlite3.Connection, *, min_stars: int = 0) -> list[CompanySeed]:
    """Group the AI repositories by the company domain their homepage points at.

    Ordered by the stars standing behind the domain, because that is the order
    in which getting a company wrong costs something.
    """
    rows = conn.execute(
        """
        SELECT r.owner, r.full_name, r.homepage, r.stars
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        WHERE r.is_fork = 0 AND r.homepage IS NOT NULL AND r.homepage != ''
          AND r.stars >= :min_stars
        ORDER BY r.stars DESC
        """,
        {"min_stars": min_stars},
    )

    seeds: dict[str, CompanySeed] = defaultdict(lambda: CompanySeed(domain=""))
    for row in rows:
        domain = registrable_domain(row["homepage"])
        if not is_company_domain(domain):
            continue
        seed = seeds[domain]
        seed.domain = domain
        seed.owners.add(row["owner"])
        seed.repos += 1
        seed.stars += row["stars"]
        if row["stars"] > seed.top_repo_stars:
            seed.top_repo = row["full_name"]
            seed.top_repo_stars = row["stars"]

    return sorted(seeds.values(), key=lambda s: -s.stars)
