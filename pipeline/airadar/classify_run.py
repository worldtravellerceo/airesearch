"""Classification orchestration.

The rule engine settles most repos for nothing. The rest get a README fetched
(one request each) and are judged by a model. Results are cached against the
hash of their inputs, so a weekly run only revisits repos that are new or whose
description, topics or language actually changed.

There are two ways to judge the borderline cases. `classify_all` sends them to
the Batch API, which costs money. `export_pending` / `import_verdicts` hand the
same repos to a Claude Code session instead — identical judgement, paid for by
a subscription rather than per token, which is why the automation defaults to
the free rule engine and leaves this as a manual step.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from airadar.classify import rules
from airadar.classify.llm import LLMClassifier, LLMInput, estimate_cost_usd
from airadar.classify.taxonomy import DEFAULT_CATEGORY, is_valid
from airadar.config import get_settings
from airadar.db import repo as db
from airadar.gh.client import GitHubClient
from airadar.gh.content import fetch_readme_excerpt

log = logging.getLogger(__name__)


@dataclass
class HandoffItem:
    """One borderline repo, packaged for a human or an assistant to judge."""

    full_name: str
    description: str | None
    topics: list[str]
    language: str | None
    readme_excerpt: str
    content_hash: str
    rule_confidence: float


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
    exported: int = 0
    imported: int = 0

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


# --- handing the borderline cases to someone else --------------------------


async def export_pending(
    conn: sqlite3.Connection,
    client: GitHubClient,
    path: Path,
    *,
    limit: int | None = None,
) -> ClassifyReport:
    """Write the repos the rule engine could not settle, with their READMEs.

    This is the free alternative to the Batch API: the same judgement, made in a
    Claude Code session on a subscription rather than billed per token. The file
    is self-contained, so whoever fills it in needs nothing else.
    """
    settings = get_settings()
    report = ClassifyReport()

    facts, ids_by_name = load_facts(conn)
    report.considered = len(facts)
    cached = db.cached_classification_hashes(conn)
    fresh = [f for f in facts if cached.get(ids_by_name[f.full_name]) != f.content_hash()]

    _, escalate = rules.partition(fresh, low=settings.llm_band_low, high=settings.llm_band_high)
    report.escalated = len(escalate)
    if limit is not None:
        escalate = escalate[:limit]

    items = []
    for item, verdict in escalate:
        items.append(
            HandoffItem(
                full_name=item.full_name,
                description=item.description,
                topics=list(item.topics),
                language=item.language,
                readme_excerpt=await fetch_readme_excerpt(client, item.full_name),
                content_hash=item.content_hash(),
                rule_confidence=verdict.confidence,
            ).__dict__
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "repos": items},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    report.exported = len(items)
    log.info("classify: exported %d borderline repos to %s", len(items), path)
    return report


def import_verdicts(conn: sqlite3.Connection, path: Path) -> ClassifyReport:
    """Read judged repos back in, from one file or a directory of them.

    Anything whose inputs have changed since the export is skipped rather than
    applied: the verdict was made about a different description, and a stale
    label is worse than no label.
    """
    path = Path(path)
    if path.is_dir():
        # Replayed on every classification run, so the judgement lives in the
        # repository rather than only in a database that is a release asset.
        total = ClassifyReport()
        for child in sorted(path.glob("*.json")):
            part = import_verdicts(conn, child)
            total.imported += part.imported
            total.unmatched.extend(part.unmatched)
            total.ai_repos = part.ai_repos
        return total

    report = ClassifyReport()
    payload = json.loads(path.read_text(encoding="utf-8"))

    facts, ids_by_name = load_facts(conn)
    hashes = {f.full_name: f.content_hash() for f in facts}

    for entry in payload.get("repos", []):
        full_name = entry.get("full_name")
        repo_id = ids_by_name.get(full_name)
        if repo_id is None:
            report.unmatched.append(full_name or "?")
            continue
        if entry.get("content_hash") and entry["content_hash"] != hashes.get(full_name):
            log.info("classify: %s changed since export, skipping", full_name)
            report.unmatched.append(full_name)
            continue

        category = entry.get("category")
        is_ai = bool(entry.get("is_ai"))
        db.save_classification(
            conn,
            repo_id,
            is_ai=is_ai,
            category=(category if is_valid(category) else DEFAULT_CATEGORY) if is_ai else None,
            subcategory=entry.get("subcategory"),
            confidence=float(entry.get("confidence", 0.9)),
            method="llm",
            content_hash=hashes[full_name],
            one_liner=entry.get("one_liner"),
        )
        report.imported += 1

    conn.commit()
    report.ai_repos = conn.execute(
        "SELECT count(*) AS n FROM repo_classification WHERE is_ai = 1"
    ).fetchone()["n"]
    log.info("classify: imported %d verdicts", report.imported)
    return report
