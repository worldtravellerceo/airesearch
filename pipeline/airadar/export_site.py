"""Turning the database into the static files the dashboard is built from.

The site has no server and no API: a daily job writes JSON here, the Next.js
build inlines what the first paint needs and fetches the rest as static assets.
That is what removes the last hosted service — and the data only changes once a
day, so a live API was never buying anything.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from airadar.companies import boards as company_boards
from airadar.db import repo as db
from airadar.scoring.leaderboards import ALL_CATEGORIES, BOARDS

log = logging.getLogger(__name__)

SPARKLINE_DAYS = 90
BOARD_LIMIT = 200
CATEGORY_LIMIT = 50
# How many repos get a pre-rendered detail page. Beyond this the site links to
# GitHub instead: a page per tracked repo would dominate both the build time and
# the payload for repos nobody opens.
DETAIL_LIMIT = 1200


@dataclass
class ExportReport:
    boards: int = 0
    repos: int = 0
    bytes_written: int = 0
    as_of: dt.date | None = None

    def summary(self) -> str:
        return (
            f"{self.boards} board files, {self.repos} repo pages, "
            f"{self.bytes_written / 1_000_000:.1f} MB, as of {self.as_of}"
        )


def export(conn: sqlite3.Connection, out_dir: Path, *, date: dt.date | None = None) -> ExportReport:
    """Write every file the site needs. Destructive: the directory is rebuilt."""
    report = ExportReport()
    date = date or db.latest_board_date(conn)
    report.as_of = date

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    if date is None:
        # No scoring run yet. Write the empty shell rather than failing, so the
        # site builds and says so instead of 404ing.
        _write(out_dir / "overview.json", _overview(conn, None), report)
        _write(out_dir / "categories.json", {"categories": [], "pools": {}}, report)
        _write(out_dir / "index.json", {"repos": []}, report)
        _write(out_dir / "manifest.json", {"as_of": None, "boards": [], "repos": []}, report)
        log.warning("export: no leaderboard data yet, wrote an empty site")
        return report

    categories = _categories(conn, date)
    pools = db.load_board_pools(conn, date)
    _write(out_dir / "overview.json", _overview(conn, date), report)
    # The chips print a count and the filter returns at most CATEGORY_LIMIT
    # rows, so a chip reading "agent-framework 4.812" next to a list of fifty
    # is answering a question nobody asked. `pools` is per board, because a
    # category's size on Breakout is not its size in the universe.
    _write(
        out_dir / "categories.json",
        {"categories": categories, "pools": _pools_by_board(pools)},
        report,
    )
    _write(out_dir / "index.json", {"repos": _search_index(conn)}, report)

    slugs = _boards(conn, out_dir, date, categories, report)
    detail_slugs = _details(conn, out_dir, date, report)
    company_slugs = _company_boards(conn, out_dir, date, report)

    _write(
        out_dir / "manifest.json",
        {
            "as_of": date.isoformat(),
            "boards": slugs,
            "categories": [c["category"] for c in categories],
            "repos": detail_slugs,
            "companies": company_slugs,
        },
        report,
    )
    log.info("export: %s", report.summary())
    return report


def _write(path: Path, payload, report: ExportReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_encode)
    path.write_text(text, encoding="utf-8")
    report.bytes_written += len(text.encode("utf-8"))


def _encode(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


# --- pieces ----------------------------------------------------------------


def _overview(conn: sqlite3.Connection, date: dt.date | None) -> dict:
    # `ai_repos` and `ai_measured` are deliberately two numbers. Of 58,367
    # repositories classified as AI, 19,248 have so much as one day of star
    # history — the rest were classified from their metadata and have never
    # been measured, and their velocities are zero because the column is
    # NOT NULL DEFAULT 0, not because they are standing still. Printing only
    # the first overstates what this index has actually observed by a factor
    # of three.
    counts = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM repos r WHERE """
        + db.CURRENT_NAME_SQL
        + """) AS tracked,
            (SELECT count(*) FROM repo_classification WHERE is_ai = 1) AS ai_repos,
            (SELECT count(*) FROM repo_classification WHERE is_ai IS NULL) AS ai_unsettled,
            (SELECT count(DISTINCT c.repo_id)
               FROM repo_classification c
               JOIN repo_star_daily d ON d.repo_id = c.repo_id
              WHERE c.is_ai = 1) AS ai_measured,
            (SELECT count(*) FROM repos WHERE history_backfilled_through IS NOT NULL)
                AS backfilled,
            (SELECT count(*) FROM repo_star_daily) AS day_rows
        """
    ).fetchone()
    # What each board was allowed to rank today. The Fresh Power tile used to
    # print `backfilled`, which is a count over every repository in the
    # database, AI or not, and takes no account of whether the repo scores
    # above zero — a different question with a much larger answer.
    counts["pools"] = (
        {
            board: n
            for (board, category), n in db.load_board_pools(conn, date).items()
            if category == ALL_CATEGORIES
        }
        if date is not None
        else {}
    )
    last_run = conn.execute(
        "SELECT command, finished_at, ok, api_calls, api_304s, llm_cost_usd, notes "
        "FROM run_log WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    return {"as_of": date, "counts": counts, "last_run": last_run}


def _pools_by_board(pools: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for (board, category), eligible in pools.items():
        out.setdefault(board, {})[category] = eligible
    return out


def _categories(conn: sqlite3.Connection, date: dt.date) -> list[dict]:
    return conn.execute(
        """
        SELECT c.category,
               count(*) AS repos,
               sum(r.stars) AS stars,
               round(sum(COALESCE(s.velocity_14d, 0)), 1) AS velocity_14d,
               count(*) FILTER (WHERE s.breakout = 1) AS breakouts
        FROM repo_classification c
        JOIN repos r ON r.id = c.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = c.repo_id AND s.date = :date
        WHERE c.is_ai = 1 AND c.category IS NOT NULL
        GROUP BY c.category
        ORDER BY repos DESC
        """,
        {"date": date},
    ).fetchall()


def _search_index(conn: sqlite3.Connection) -> list[dict]:
    """A flat list the site filters in the browser — small enough to ship whole,
    which is simpler and faster than a search endpoint we would have to host."""
    return conn.execute(
        """
        SELECT r.full_name, r.description, r.stars, r.language, c.category, c.one_liner
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        WHERE """
        + db.CURRENT_NAME_SQL
        + """
        ORDER BY r.stars DESC
        """
    ).fetchall()


def _boards(
    conn: sqlite3.Connection,
    out_dir: Path,
    date: dt.date,
    categories: list[dict],
    report: ExportReport,
) -> list[str]:
    sparklines = _sparklines(conn, date)
    slugs: list[str] = []

    wanted = [(board, ALL_CATEGORIES, BOARD_LIMIT) for board in BOARDS]
    wanted += [(board, row["category"], CATEGORY_LIMIT) for row in categories for board in BOARDS]

    for board, category, limit in wanted:
        previous = db.previous_board_ranks(conn, before=date, board=board, category=category)
        entries = db.load_leaderboard(conn, date=date, board=board, category=category, limit=limit)
        for entry in entries:
            old = previous.get(entry["repo_id"])
            entry["rank_delta"] = None if old is None else old - entry["rank"]
            series = sparklines.get(entry["repo_id"])
            entry["sparkline"] = series["days"] if series else []
            # Where in the 90-day window the series starts. Without it every
            # row says "90 gün" and stretches whatever it has — two points or
            # ninety — across the same 96 pixels.
            entry["sparkline_from"] = series["from"] if series else None
            entry["archived"] = bool(entry["archived"])
            entry["breakout"] = bool(entry["breakout"])

        slug = f"{board}/{category}"
        _write(
            out_dir / "boards" / f"{slug}.json",
            {"board": board, "category": category, "as_of": date, "entries": entries},
            report,
        )
        slugs.append(slug)
        report.boards += 1

    return slugs


def _sparklines(conn: sqlite3.Connection, date: dt.date) -> dict[int, dict]:
    """90 days of daily gains per repo, in one pass rather than a query per row.

    Dense from the repo's first observed day to `date`, so a missing day inside
    the series is a `null` and not a silently skipped column. The leading gap is
    not padded — it is reported as `from` instead, because most repositories
    here have far fewer than ninety days recorded and ninety nulls per row
    across every board file is megabytes of nothing.
    """
    since = date - dt.timedelta(days=SPARKLINE_DAYS - 1)
    seen: dict[int, dict[dt.date, int]] = {}
    for row in conn.execute(
        "SELECT repo_id, date, stars_gained FROM repo_star_daily "
        "WHERE date >= :since AND date <= :date ORDER BY repo_id, date",
        {"since": since, "date": date},
    ):
        day = row["date"]
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        elif isinstance(day, dt.datetime):
            day = day.date()
        seen.setdefault(row["repo_id"], {})[day] = row["stars_gained"]

    out: dict[int, dict] = {}
    for repo_id, by_date in seen.items():
        first = min(by_date)
        span = (date - first).days + 1
        out[repo_id] = {
            "from": first,
            "days": [by_date.get(first + dt.timedelta(days=i)) for i in range(span)],
        }
    return out


def _details(
    conn: sqlite3.Connection, out_dir: Path, date: dt.date, report: ExportReport
) -> list[str]:
    """Write a page for every repo the site can link to.

    The selection used to be the top `DETAIL_LIMIT` by stars, which quietly
    broke the board this project exists for. Breakout ranks repos that are
    exploding *now*, and a repo exploding now has not had time to accumulate
    stars — so 114 of its 132 rows pointed at a page that was never written,
    and Momentum lost 64 of 200. The two boards whose whole subject is
    rising interest were the two with the broken links.

    So: the top N by stars, plus everyone on a board, whatever their rank.
    """
    rows = conn.execute(
        """
        SELECT r.id, r.full_name, r.owner, r.name, r.description, r.homepage,
               r.language, r.license, r.stars, r.archived, r.created_at,
               r.discovered_via, r.history_backfilled_through,
               c.category, c.subcategory, c.one_liner,
               s.velocity_7d, s.velocity_14d, s.velocity_28d, s.velocity_90d,
               s.acceleration, s.acceleration_basis,
               s.relative_growth_14d, s.fresh_power, s.momentum_score,
               s.peak_velocity, s.days_since_peak, s.days_to_1k, s.days_to_10k,
               s.days_to_50k, s.breakout, s.coverage_days
        FROM repos r
        JOIN repo_classification c ON c.repo_id = r.id AND c.is_ai = 1
        LEFT JOIN repo_scores s ON s.repo_id = r.id AND s.date = :date
        WHERE """
        + db.CURRENT_NAME_SQL
        + """
          AND (r.id IN (
                  SELECT id FROM repos WHERE is_fork = 0 ORDER BY stars DESC LIMIT :limit
                )
               OR r.id IN (SELECT repo_id FROM leaderboard_snapshots WHERE date = :date))
        ORDER BY r.stars DESC
        """,
        {"date": date, "limit": DETAIL_LIMIT},
    ).fetchall()

    topics = db.repo_topics_map(conn, [row["id"] for row in rows])
    ranks = _ranks_by_repo(conn, date)
    slugs: list[str] = []

    for row in rows:
        repo_id = row.pop("id")
        row["archived"] = bool(row["archived"])
        row["breakout"] = bool(row["breakout"])
        row["topics"] = topics.get(repo_id, [])
        row["ranks"] = ranks.get(repo_id, {})

        # The star curve is written separately and fetched by the browser. A
        # lifetime history is by far the largest field on a repo, and a static
        # build inlines whatever a page reads into both its HTML and its
        # client payload — so inlining it here cost about 125 KB per page,
        # twice over, for a chart most visitors never scroll to.
        history = _history(conn, repo_id, row["stars"], until=date)
        row["history_points"] = len(history)

        folder = out_dir / "repos" / row["owner"]
        _write(folder / f"{row['name']}.json", row, report)
        _write(folder / f"{row['name']}.history.json", {"points": history}, report)
        slugs.append(row["full_name"])
        report.repos += 1

    return slugs


def _ranks_by_repo(conn: sqlite3.Connection, date: dt.date) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT repo_id, board, rank FROM leaderboard_snapshots "
        "WHERE date = :date AND category = '_all'",
        {"date": date},
    ):
        out.setdefault(row["repo_id"], {})[row["board"]] = row["rank"]
    return out


def _history(
    conn: sqlite3.Connection, repo_id: int, stars_total: int, *, until: dt.date
) -> list[dict]:
    """The full star curve: weekly buckets for the aged-out part, daily for the
    retention window.

    Two things it has to get right, and the first version got neither.

    It stops at `until`. GitHub's star-history endpoint returns whole weeks,
    including the days of the current week that have not happened yet, each
    with a gain of zero — so every chart ran five days past the site's own
    "last updated" stamp as a flat line into the future.

    And it ends at the headline number. The running total used to start from
    `max(stars_total - sum(gains), 0)`, which anchors correctly only while the
    recorded history is smaller than the star count. When it is larger — 35
    repositories still are, left over from a roll-up that double-counted — the
    clamp fires and the curve finishes *above* the total printed beside it.
    `openclaw/openclaw` ended at 390,203 against a tile reading 390,187. So the
    curve is built backwards from the star count instead, which makes the last
    point right by construction rather than by arithmetic that can be wrong.
    """
    points: list[tuple[dt.date, int]] = list(db.load_weekly_series(conn, repo_id))
    points += [(d.date, d.stars_gained) for d in db.load_daily_series(conn, repo_id)]
    points = sorted((day, gained) for day, gained in points if day <= until)
    if not points:
        return []

    # Walk back from the known total, then emit forwards. A history longer than
    # the star count flattens at zero early on rather than overshooting at the
    # end, because the end is what a reader checks against the tile.
    cumulative = [0] * len(points)
    running = stars_total
    for index in range(len(points) - 1, -1, -1):
        cumulative[index] = max(running, 0)
        running -= points[index][1]

    return [
        {"date": day, "stars_gained": gained, "cumulative": cumulative[index]}
        for index, (day, gained) in enumerate(points)
    ]


def _company_boards(
    conn: sqlite3.Connection, out_dir: Path, date: dt.date, report: ExportReport
) -> list[dict]:
    """Write the boards for the second universe, skipping the ones with no rows.

    A board that writes itself empty claims the question was asked and came
    back blank. Until a source has run, the truthful thing is for the tab not
    to be there — so an empty board is left out of the manifest and the site
    never offers it.
    """
    written: list[dict] = []
    for slug, (build, title, blurb) in company_boards.BOARDS.items():
        try:
            rows = build(conn, today=date) if slug == "funded" else build(conn)
        except sqlite3.OperationalError as exc:
            # An older database on the data branch has none of these tables yet.
            log.warning("export: company board %s unavailable (%s)", slug, exc)
            continue
        if not rows:
            continue
        _write(
            out_dir / "companies" / f"{slug}.json",
            {"slug": slug, "title": title, "blurb": blurb, "as_of": date, "entries": rows},
            report,
        )
        written.append({"slug": slug, "title": title, "blurb": blurb, "count": len(rows)})
    if written:
        log.info("export: %d company boards", len(written))
    return written
