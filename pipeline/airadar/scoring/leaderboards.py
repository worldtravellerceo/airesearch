"""Turning per-repo metrics into the four ranked boards.

Each board answers a different question, which is the entire point of having
more than one:

- **popular**  — what is biggest? (the conventional, age-biased view)
- **momentum** — what is moving fastest right now?
- **breakout** — what suddenly started moving, relative to its own past?
- **fresh**    — what is biggest *if you discount stars by age*?

`fresh` is gated on a complete backfill. fresh_power integrates a repo's whole
life, so ranking a backfilled repo against a partially-collected one would
silently favour the one we happen to know less about.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from airadar.scoring.metrics import RepoMetrics

ALL_CATEGORIES = "_all"
DEFAULT_LIMIT = 200
CATEGORY_LIMIT = 50

Board = str
BOARDS: tuple[Board, ...] = ("popular", "momentum", "breakout", "fresh")


@dataclass(frozen=True)
class Entry:
    board: Board
    category: str
    rank: int
    repo_id: int
    score: float


def _popular(m: RepoMetrics) -> float:
    return float(m.stars_total)


def _momentum(m: RepoMetrics) -> float:
    return m.velocity_14d


def _breakout(m: RepoMetrics) -> float:
    # Sudden speed relative to the repo's own past, weighted by how much of the
    # project's total following arrived during the window. Both factors are
    # needed: acceleration alone rewards noise off a near-zero base.
    return m.acceleration * (1.0 + m.relative_growth_14d)


def _fresh(m: RepoMetrics) -> float:
    return m.fresh_power


SCORERS: dict[Board, Callable[[RepoMetrics], float]] = {
    "popular": _popular,
    "momentum": _momentum,
    "breakout": _breakout,
    "fresh": _fresh,
}


def eligible(board: Board, metrics: RepoMetrics) -> bool:
    """Whether a repo may appear on a board at all."""
    if board == "fresh":
        # Comparing an integral over a full lifetime against one over seven
        # months is not a comparison. Backfill first.
        return metrics.history_complete
    if board == "breakout":
        return metrics.breakout
    return True


def rank_board(
    metrics: Iterable[RepoMetrics],
    board: Board,
    *,
    category: str = ALL_CATEGORIES,
    limit: int = DEFAULT_LIMIT,
) -> list[Entry]:
    """Rank one board, highest score first, ties broken by total stars then id."""
    if board not in SCORERS:
        raise ValueError(f"unknown board {board!r}; expected one of {BOARDS}")

    scorer = SCORERS[board]
    pool = [
        m
        for m in metrics
        if eligible(board, m)
        and (category == ALL_CATEGORIES or m.category == category)
        and m.repo_id is not None
    ]
    pool.sort(key=lambda m: (-scorer(m), -m.stars_total, m.repo_id or 0))

    return [
        Entry(board, category, rank, m.repo_id, scorer(m))  # type: ignore[arg-type]
        for rank, m in enumerate(pool[:limit], start=1)
    ]


def build_all(
    metrics: Sequence[RepoMetrics],
    *,
    limit: int = DEFAULT_LIMIT,
    category_limit: int = CATEGORY_LIMIT,
) -> list[Entry]:
    """Every board, globally and per category, as flat rows ready for insertion."""
    entries: list[Entry] = []
    for board in BOARDS:
        entries.extend(rank_board(metrics, board, limit=limit))

    categories = sorted({m.category for m in metrics if m.category})
    for category in categories:
        for board in BOARDS:
            entries.extend(rank_board(metrics, board, category=category, limit=category_limit))
    return entries


def rank_deltas(
    current: Iterable[Entry], previous: Iterable[Entry]
) -> dict[tuple[Board, str, int], int | None]:
    """Movement since a previous snapshot, keyed by (board, category, repo_id).

    Positive means the repo climbed. `None` means it was not on the board
    before, which is a different story from "did not move" and should be shown
    as such.
    """
    before = {(e.board, e.category, e.repo_id): e.rank for e in previous}
    deltas: dict[tuple[Board, str, int], int | None] = {}
    for entry in current:
        key = (entry.board, entry.category, entry.repo_id)
        old = before.get(key)
        deltas[key] = None if old is None else old - entry.rank
    return deltas
