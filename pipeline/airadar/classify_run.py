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
    inputs_hash: str
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
               r.readme_excerpt,
               (SELECT json_group_array(topic)
                  FROM (SELECT topic FROM repo_topics
                        WHERE repo_id = r.id ORDER BY topic)) AS topics_json,
               (SELECT json_group_array(package)
                  FROM (SELECT DISTINCT package FROM repo_packages
                        WHERE full_name = r.full_name ORDER BY package)) AS packages_json
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
        row["packages"] = json.loads(row.pop("packages_json") or "[]")
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

    # The escalated repos, recorded as unsettled rather than left with no row
    # at all. This was the largest single hole in the index: with the LLM pass
    # off — which is how the daily run works — nothing was written for them, so
    # 1,842 repositories above a thousand stars had no classification row of
    # any kind. `karpathy/nanoGPT` was one of them, at 63,285 stars, and
    # `msitarzewski/agency-agents` at 153,893. They were absent from the
    # boards, absent from the counts, and absent from the review queue, which
    # is the worst of the three: nobody could even find them to decide.
    #
    # The row is written with is_ai NULL and the score that produced the
    # escalation. `content_hash` goes in too, so the same undecidable repo is
    # not re-derived every run — and because that hash carries the rules
    # version, the README excerpt and the package list, new evidence or a new
    # vocabulary brings it straight back for another look.
    if not use_llm:
        for item, verdict in escalate:
            db.save_classification(
                conn,
                ids_by_name[item.full_name],
                is_ai=None,
                category=None,
                subcategory=None,
                confidence=verdict.confidence,
                method="rules-unsettled",
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
                inputs_hash=item.inputs_hash(),
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
    # Validated against the repo's own inputs, not against the rules version: a
    # judgement made by reading a description does not expire because a phrase
    # was added to a list. Stored under `content_hash` so the rule engine still
    # treats it as cached.
    inputs = {f.full_name: f.inputs_hash() for f in facts}
    hashes = {f.full_name: f.content_hash() for f in facts}

    for entry in payload.get("repos", []):
        full_name = entry.get("full_name")
        repo_id = ids_by_name.get(full_name)
        if repo_id is None:
            report.unmatched.append(full_name or "?")
            continue
        if entry.get("inputs_hash") and entry["inputs_hash"] != inputs.get(full_name):
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


# --- the weekly review queue -----------------------------------------------


def reviewed_names(verdicts_dir: Path) -> set[str]:
    """Every repo a person has already judged, across all verdict files.

    The verdict files are the record of what has been looked at, not the
    database: a rules re-run rewrites `repo_classification.method`, so the
    database forgets. Reading the files keeps the queue from handing back the
    same repos every week.
    """
    seen: set[str] = set()
    if not verdicts_dir.is_dir():
        return seen
    for child in sorted(verdicts_dir.glob("*.json")):
        payload = json.loads(child.read_text(encoding="utf-8"))
        for entry in payload.get("repos", []):
            if entry.get("full_name"):
                seen.add(entry["full_name"])
    return seen


def export_review_queue(
    conn: sqlite3.Connection,
    path: Path,
    *,
    limit: int,
    min_stars: int = 1_000,
    verdicts_dir: Path | None = None,
) -> ClassifyReport:
    """Write the repos the rule engine did not settle, biggest first.

    Two kinds, and both are invisible until somebody reads them.

    A repo that scored zero was recorded as "not AI", which is the one reading
    the score does not support: absence of evidence. `anomalyco/opencode` sat
    here at 208,847 stars with the description "The open source coding agent.",
    and only a hand-written probe found it.

    A repo in the escalation band is worse off still. With the LLM pass
    disabled — which is how the daily run works — nothing is written for it at
    all: it is on no board, and it used to be filtered out of this queue for
    having a score above zero. That made partial evidence strictly worse than
    none, so a README that moved a repo from 0.00 to 0.40 would hide it.
    Everything the engine did not settle now comes here.

    Sorted by stars because that is the order in which a miss costs something,
    and capped because this is meant to be worked through a slice at a time.
    """
    report = ClassifyReport()
    facts, _ = load_facts(conn)
    already = reviewed_names(verdicts_dir) if verdicts_dir else set()

    stars = {
        row["full_name"]: row["stars"]
        for row in conn.execute("SELECT full_name, stars FROM repos WHERE is_fork = 0")
    }

    candidates: list[tuple[int, rules.RepoFacts]] = []
    for item in facts:
        if item.full_name in already:
            continue
        count = stars.get(item.full_name, 0)
        if count < min_stars:
            continue
        verdict = rules.classify(item)
        if verdict.is_ai:
            continue
        candidates.append((count, item, verdict))

    report.considered = len(candidates)
    # Unsettled first, and by stars within each group. Both kinds belong here,
    # but they are not the same question and sorting them together buries the
    # smaller one: 6,823 repositories carry evidence the engine could not
    # resolve, against 53,427 that carry none at all, so a slice ordered purely
    # by stars opens with `freeCodeCamp/freeCodeCamp` and never reaches an
    # actual open question.
    candidates.sort(key=lambda row: (not row[2].needs_llm, -row[0]))

    items = [
        {
            "full_name": item.full_name,
            "stars": count,
            "description": item.description,
            "topics": list(item.topics),
            "language": item.language,
            # What kind of "no" this is. `unsettled` means evidence was found
            # and did not settle it; `no-signal` means nothing was found, which
            # is absence of evidence and not evidence of absence — the reading
            # that once hid `anomalyco/opencode` at 208,847 stars.
            "basis": "unsettled" if verdict.needs_llm else "no-signal",
            "confidence": round(verdict.confidence, 3),
            "matched": list(verdict.matched)[:6],
            "inputs_hash": item.inputs_hash(),
        }
        for count, item, verdict in candidates[:limit]
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                "remaining_after_this_slice": max(0, len(candidates) - len(items)),
                "repos": items,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    report.exported = len(items)
    log.info(
        "classify: %d repos queued for review, %d left after this slice",
        len(items),
        max(0, len(candidates) - len(items)),
    )
    return report
