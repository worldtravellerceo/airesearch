"""Driving the summariser: pick the repos, guard the spend, save the text.

Separate from `summarise.py` the way `classify_run.py` is separate from
`classify/llm.py` — the module above knows how to talk to the model, this one
knows which repositories are worth talking about and what a run is allowed to
cost.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from airadar.config import get_settings
from airadar.db import repo as db
from airadar.export_site import DETAIL_LIMIT
from airadar.summarise import (
    DEFAULT_MODEL,
    SpendCeilingExceeded,
    Summariser,
    SummaryInput,
    estimate_cost_usd,
    load_profile,
    profile_fingerprint,
)

log = logging.getLogger(__name__)


@dataclass
class SummariseReport:
    visible: int = 0
    stale: int = 0
    attempted: int = 0
    written: int = 0
    no_match: int = 0
    estimate_usd: float = 0.0
    cost_usd: float = 0.0
    unmatched: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        if self.dry_run:
            return (
                f"{self.visible} visible, {self.stale} need writing, "
                f"{self.attempted} in scope, estimate ${self.estimate_usd:.2f} (dry run)"
            )
        share = f", {self.no_match} with no match" if self.written else ""
        note = f", {len(self.unmatched)} unmatched" if self.unmatched else ""
        return (
            f"{self.visible} visible, {self.stale} needed writing, "
            f"{self.written} written{share}, ${self.cost_usd:.2f} "
            f"(estimated ${self.estimate_usd:.2f}){note}"
        )


def load_candidates(conn, *, date: dt.date, profile_hash: str) -> list[SummaryInput]:
    """Every visible repo, biggest first, with what the model needs to read."""
    ids = db.visible_repo_ids(conn, date=date, detail_limit=DETAIL_LIMIT)
    if not ids:
        return []

    topics = db.repo_topics_map(conn, ids)
    placeholders = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT id, full_name, description, language, license, stars,
               readme_excerpt, readme_hash
        FROM repos WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()

    by_id = {row["id"]: row for row in rows}
    candidates: list[SummaryInput] = []
    # `ids` is already ordered by stars, and that order is the run's priority:
    # an interrupted sweep should have spent its money on the rows a reader
    # reaches first.
    for repo_id in ids:
        row = by_id.get(repo_id)
        if row is None:
            continue
        candidates.append(
            SummaryInput(
                repo_id=repo_id,
                full_name=row["full_name"],
                description=row["description"],
                topics=tuple(topics.get(repo_id, [])),
                language=row["language"],
                license=row["license"],
                stars=row["stars"] or 0,
                readme_excerpt=row["readme_excerpt"] or "",
                readme_hash=row["readme_hash"] or "",
            )
        )
    return candidates


def run(
    conn,
    *,
    date: dt.date,
    limit: int | None = None,
    max_spend_usd: float = 25.0,
    dry_run: bool = False,
) -> SummariseReport:
    settings = get_settings()
    report = SummariseReport(dry_run=dry_run)

    # Fails loudly when the profile is absent. A summary written without it
    # still reads like an answer, which is worse than no summary at all.
    profile_text = load_profile(settings.profile_path)
    profile_hash = profile_fingerprint(profile_text)
    model = settings.summary_model or DEFAULT_MODEL

    candidates = load_candidates(conn, date=date, profile_hash=profile_hash)
    report.visible = len(candidates)

    cached = db.cached_summary_hashes(conn)
    stale = [c for c in candidates if cached.get(c.repo_id) != c.content_hash(profile_hash)]
    report.stale = len(stale)

    if limit is not None:
        stale = stale[:limit]
    report.attempted = len(stale)

    report.estimate_usd = estimate_cost_usd(len(stale))
    if report.estimate_usd > max_spend_usd:
        raise SpendCeilingExceeded(
            f"{len(stale)} repos would cost about ${report.estimate_usd:.2f}, over the "
            f"${max_spend_usd:.2f} ceiling. Lower --limit or raise --max-spend-usd."
        )

    if dry_run or not stale:
        log.info("summarise: %s", report.summary())
        return report

    run_id = db.start_llm_run(
        conn,
        command="summarise",
        model=model,
        repos=len(stale),
        estimate_usd=report.estimate_usd,
    )

    summariser = Summariser(
        profile_text=profile_text,
        api_key=settings.anthropic_api_key,
        model=model,
    )
    try:
        summaries, usage = summariser.run(stale)
    except Exception as exc:
        db.finish_llm_run(conn, run_id, status="failed", notes=str(exc)[:500])
        raise

    hashes = {item.repo_id: item.content_hash(profile_hash) for item in stale}
    for summary in summaries:
        db.save_summary(
            conn,
            summary.repo_id,
            description_tr=summary.description_tr,
            usage_tr=summary.usage_tr,
            matched_project=summary.matched_project,
            relevance=summary.relevance,
            investment_note=summary.investment_note,
            model=model,
            content_hash=hashes[summary.repo_id],
        )
    conn.commit()

    report.written = len(summaries)
    report.no_match = sum(1 for s in summaries if not s.matched_project)
    report.cost_usd = usage.cost_usd
    report.unmatched = usage.unmatched

    db.finish_llm_run(
        conn,
        run_id,
        status="ok",
        batch_id=summariser.batch_id,
        cost_usd=usage.cost_usd,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        notes=usage.summary(),
    )
    log.info("summarise: %s", usage.summary())
    return report
