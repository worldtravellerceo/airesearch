"""Turning repository homepages into a company universe.

The measurement that produced this module: the corpus holds 35,375 owners with
an AI repository and 9,649 distinct homepage hosts, but the top fifteen by stars
included `arxiv.org`, `npmjs.com`, `dmtk.io` and `gymlibrary.dev` — a preprint
server, a package registry and two documentation sites.
"""

import datetime as dt

import pytest

from airadar.companies import domains
from airadar.db import repo as db


def add(conn, repo_id, full_name, homepage, stars=1_000, is_ai=True):
    owner, name = full_name.split("/")
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=repo_id,
                full_name=full_name,
                owner=owner,
                name=name,
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
                description="x",
                homepage=homepage,
                stars=stars,
            )
        ],
    )
    db.save_classification(
        conn,
        repo_id,
        is_ai=is_ai,
        category="llm-app" if is_ai else None,
        subcategory=None,
        confidence=0.9,
        method="rules",
        content_hash=f"h{repo_id}",
    )


# --- domain reduction ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://openai.com", "openai.com"),
        ("https://docs.langchain.com/introduction", "langchain.com"),
        ("chat.deepseek.com", "deepseek.com"),
        ("research.fb.com", "fb.com"),
        ("http://www.n8n.io/", "n8n.io"),
        ("https://example.co.uk/path", "example.co.uk"),
        ("https://user@host.example.com:8443/x", "example.com"),
    ],
)
def test_a_host_reduces_to_the_domain_under_its_public_suffix(url, expected):
    """`docs.langchain.com` and `langchain.com` are one company, not two."""
    assert domains.registrable_domain(url) == expected


@pytest.mark.parametrize(
    "url", ["", None, "   ", "not a url", "localhost", "http://[oops", "192.168.0.1"]
)
def test_anything_that_is_not_a_hostname_returns_nothing(url):
    """Returning a plausible-looking string for junk would put junk companies
    in the universe, and they would be indistinguishable from real ones."""
    assert domains.registrable_domain(url) is None


def test_platform_hosts_are_not_companies():
    """A repository linking to its own docs, package page or the paper behind
    it is the common case. Every one of those would otherwise become a
    company."""
    for host in ("arxiv.org", "npmjs.com", "foo.github.io", "readthedocs.io"):
        assert domains.is_company_domain(domains.registrable_domain(host)) is False

    assert domains.is_company_domain("openai.com") is True


def test_blocking_a_platform_does_not_claim_the_company_behind_it_is_fake():
    """Hugging Face is a real company; a link to a model card on huggingface.co
    is still not evidence that the repo's owner is Hugging Face. The company
    arrives through another seed."""
    assert "huggingface.co" in domains.PLATFORM_DOMAINS


# --- seeds from the corpus -------------------------------------------------


def test_subdomains_of_one_company_collapse_into_one_seed(conn):
    add(conn, 1, "deepseek-ai/DeepSeek-V3", "https://chat.deepseek.com", stars=100_000)
    add(conn, 2, "deepseek-ai/DeepSeek-R1", "https://deepseek.com/docs", stars=50_000)
    conn.commit()

    seeds = domains.seeds_from_corpus(conn)

    assert [s.domain for s in seeds] == ["deepseek.com"]
    assert seeds[0].repos == 2
    assert seeds[0].stars == 150_000
    assert seeds[0].top_repo == "deepseek-ai/DeepSeek-V3"


def test_seeds_are_ordered_by_the_stars_behind_them(conn):
    add(conn, 1, "small/one", "https://small.com", stars=1_000)
    add(conn, 2, "big/two", "https://big.com", stars=90_000)
    conn.commit()

    assert [s.domain for s in domains.seeds_from_corpus(conn)] == ["big.com", "small.com"]


def test_platform_links_never_become_seeds(conn):
    """From the live corpus: `arxiv.org` was the top domain by stars, with 1.8m
    stars behind it and 1,586 repos, because papers link to their preprint."""
    add(conn, 1, "HKUDS/LightRAG", "https://arxiv.org/abs/2410.05779", stars=1_800_000)
    add(conn, 2, "openai/whisper", "https://openai.com", stars=90_000)
    conn.commit()

    assert [s.domain for s in domains.seeds_from_corpus(conn)] == ["openai.com"]


def test_repos_that_are_not_ai_contribute_nothing(conn):
    add(conn, 1, "acme/web", "https://acme.com", stars=50_000, is_ai=False)
    conn.commit()

    assert domains.seeds_from_corpus(conn) == []


def test_one_company_can_have_several_github_owners(conn):
    """`facebookresearch` and `meta-llama` are the same company."""
    add(conn, 1, "facebookresearch/faiss", "https://ai.meta.com", stars=30_000)
    add(conn, 2, "meta-llama/llama", "https://ai.meta.com/llama", stars=60_000)
    conn.commit()

    seeds = domains.seeds_from_corpus(conn)

    assert len(seeds) == 1
    assert seeds[0].owners == {"facebookresearch", "meta-llama"}


def test_url_shorteners_are_not_companies():
    """`aka.ms` came through the live corpus with 193,500 stars and 46 owners
    behind it. The destination is the company; the shortener is not."""
    assert domains.is_company_domain("aka.ms") is False
