"""Classification orchestration.

The rule engine settles most repos for nothing. The rest get a README fetched
(one request each) and go to the LLM in batches. Results are cached against the
hash of their inputs, so a weekly run only pays for repos that are new or whose
description, topics or language actually changed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from airadar.classify import rules
from airadar.classify.llm import LLMClassifier, LLMInput, estimate_cost_usd
from airadar.classify.taxonomy import DEFAULT_CATEGORY
from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh.client import GitHubClient
from airadar.gh.content import fetch_readme_excerpt

log = logging.getLogger(__name__)


@dataclass
class ClassifyReport:
    considered: int = 0
    cached: int = 0
    settled_by_rules: int = 0
    escalated: int = 0
    classified_by_llm: int = 0
    unmatched: list[str] = field(default_factory=list)
    ai_repos: int = 0
    llm_cost_usd: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.considered} considered, {self.cached} cached, "
            f"{self.settled_by_rules} by rules, {self.classified_by_llm} by LLM "
            f"({len(self.unmatched)} unmatched), {self.ai_repos} AI, "
            f"${self.llm_cost_usd:.2f} spent"
        )


def load_facts(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> tuple[list[rules.RepoFacts], dict[str, int]]:
    """Every tracked repo with its topics, plus a name -> id map.

    Classification keys on `full_name` because that is what the LLM sees, but
    everything is written back against the numeric id.
    """
    sql = """
        SELECT r.id, r.full_name, r.description, r.language, r.homepage,
               (SELECT json_group_array(topic)
                  FROM (SELECT topic FROM repo_topics
                        WHERE repo_id = r.id ORDER BY topic)) AS topics_json
        FROM repos r
        WHERE r.is_fork = 0
        ORDER BY r.stars DESC
    """
    params: dict = {}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        row["topics"] = json.loads(row.pop("topics_json") or "[]")
    facts = [rules.RepoFacts.from_row(row) for row in rows]
    return facts, {row["full_name"]: row["id"] for row in rows}


async def classify_all(
    conn: sqlite3.Connection,
    client: GitHubClient,
    *,
    use_llm: bool = True,
    limit: int | None = None,
    max_llm_repos: int | None = None,
    dry_run: bool = False,
) -> ClassifyReport:
    """Classify every tracked repo, escalating only the ambiguous ones.

    `dry_run` reports what a run would cost without spending anything, which is
    the cheapest way to sanity-check a sweep before it happens.
    """
    settings = get_settings()
    report = ClassifyReport()

    facts, ids_by_name = load_facts(conn, limit=limit)
    report.considered = len(facts)
    cached = db.cached_classification_hashes(conn)

    fresh: list[rules.RepoFacts] = []
    for item in facts:
        repo_id = ids_by_name[item.full_name]
        if cached.get(repo_id) == item.content_hash():
            report.cached += 1
            continue
        fresh.append(item)

    settled, escalate = rules.partition(
        fresh, low=settings.llm_band_low, high=settings.llm_band_high
    )
    report.settled_by_rules = len(settled)
    report.escalated = len(escalate)
    report.estimated_cost_usd = estimate_cost_usd(len(escalate))

    if max_llm_repos is not None and len(escalate) > max_llm_repos:
        log.warning(
            "classify: capping LLM work at %d of %d escalations", max_llm_repos, len(escalate)
        )
        escalate = escalate[:max_llm_repos]

    if dry_run:
        log.info(
            "classify: dry run — %d would be settled free, %d would cost ~$%.2f",
            report.settled_by_rules,
            report.escalated,
            report.estimated_cost_usd,
        )
        return report

    for item, verdict in settled:
        db.save_classification(
            conn,
            ids_by_name[item.full_name],
            is_ai=verdict.is_ai,
            category=verdict.category,
            subcategory=None,
            confidence=verdict.confidence,
            method="rules",
            content_hash=item.content_hash(),
        )
    conn.commit()

    if use_llm and escalate:
        await _run_llm(conn, client, escalate, ids_by_name, report, settings)

    report.ai_repos = conn.execute(
        "SELECT count(*) AS n FROM repo_classification WHERE is_ai = 1"
    ).fetchone()["n"]
    return report


async def _run_llm(
    conn: sqlite3.Connection,
    client: GitHubClient,
    escalate: list[tuple[rules.RepoFacts, rules.Verdict]],
    ids_by_name: dict[str, int],
    report: ClassifyReport,
    settings,
) -> None:
    inputs: list[LLMInput] = []
    for item, _ in escalate:
        excerpt = await fetch_readme_excerpt(client, item.full_name)
        inputs.append(
            LLMInput(
                full_name=item.full_name,
                description=item.description,
                topics=item.topics,
                language=item.language,
                readme_excerpt=excerpt,
                content_hash=item.content_hash(),
            )
        )

    classifier = LLMClassifier(api_key=settings.anthropic_api_key, model=settings.classifier_model)
    verdicts, usage = classifier.classify(inputs)

    hashes = {item.full_name: item.content_hash for item in inputs}
    for verdict in verdicts:
        db.save_classification(
            conn,
            ids_by_name[verdict.full_name],
            is_ai=verdict.is_ai,
            category=(verdict.category or DEFAULT_CATEGORY) if verdict.is_ai else None,
            subcategory=verdict.subcategory,
            confidence=verdict.confidence,
            method="llm",
            content_hash=hashes[verdict.full_name],
            one_liner=verdict.one_liner,
        )
    conn.commit()

    report.classified_by_llm = len(verdicts)
    report.unmatched = usage.unmatched
    report.llm_cost_usd = usage.cost_usd
    report.llm_input_tokens = usage.input_tokens
    report.llm_output_tokens = usage.output_tokens
    log.info("classify: llm — %s", usage.summary())
