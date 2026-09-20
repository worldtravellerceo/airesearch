"""SQL for the read API.

Deliberately standalone rather than importing the `airadar` package: this runs
as a Vercel Function, and pulling in the pipeline would drag httpx, typer and
the Anthropic SDK into a bundle that only ever reads from Postgres.
"""

from __future__ import annotations

import datetime as dt

BOARDS = ("popular", "momentum", "breakout", "fresh")
ALL_CATEGORIES = "_all"
MAX_LIMIT = 500

REPO_FIELDS = """
    r.id                AS repo_id,
    r.full_name,
    r.owner,
    r.name,
    r.description,
    r.homepage,
    r.language,
    r.license,
    r.stars,
    r.archived,
    r.created_at,
    c.category,
    c.subcategory,
    c.one_liner
"""

SCORE_FIELDS = """
    s.velocity_7d,
    s.velocity_14d,
    s.velocity_28d,
    s.velocity_90d,
    s.acceleration,
    s.relative_growth_14d,
    s.fresh_power,
    s.momentum_score,
    s.peak_velocity,
    s.days_since_peak,
    s.days_to_1k,
    s.days_to_10k,
    s.days_to_50k,
    s.breakout,
    s.coverage_days
"""


def latest_board_date(conn) -> dt.date | None:
    row = conn.execute("SELECT max(date) AS date FROM leaderboard_snapshots").fetchone()
    return row["date"] if row else None


def leaderboard(
    conn,
    *,
    board: str,
    category: str = ALL_CATEGORIES,
    limit: int = 200,
    offset: int = 0,
    date: dt.date | None = None,
    language: str | None = None,
    include_archived: bool = True,
) -> list[dict]:
    """One board, with each repo's movement since the previous snapshot.

    The rank delta is computed in SQL against whatever snapshot came last,
    rather than "seven days ago": runs can be missed, and comparing against a
    date that has no snapshot would silently report every repo as new.
    """
    date = date or latest_board_date(conn)
    if date is None:
        return []

    filters = []
    params: dict = {
        "date": date,
        "board": board,
        "category": category,
        "limit": min(limit, MAX_LIMIT),
        "offset": max(offset, 0),
    }
    if language:
        filters.append("AND r.language = %(language)s")
        params["language"] = language
    if not include_archived:
        filters.append("AND NOT r.archived")

    sql = f"""
        WITH previous AS (
            SELECT repo_id, rank
            FROM leaderboard_snapshots
            WHERE board = %(board)s AND category = %(category)s
              AND date = (
                  SELECT max(date) FROM leaderboard_snapshots
                  WHERE board = %(board)s AND category = %(category)s AND date < %(date)s
              )
        )
        SELECT l.rank, l.score, {REPO_FIELDS}, {SCORE_FIELDS},
               p.rank - l.rank AS rank_delta,
               (SELECT array_agg(d.stars_gained ORDER BY d.date)
                  FROM repo_star_daily d
                 WHERE d.repo_id = r.id AND d.date > %(date)s - 90) AS sparkline
        FROM leaderboard_snapshots l
        JOIN repos r ON r.id = l.repo_id
        LEFT JOIN repo_classification c ON c.repo_id = l.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = l.repo_id AND s.date = l.date
        LEFT JOIN previous p ON p.repo_id = l.repo_id
        WHERE l.date = %(date)s AND l.board = %(board)s AND l.category = %(category)s
        {" ".join(filters)}
        ORDER BY l.rank
        LIMIT %(limit)s OFFSET %(offset)s
    """
    return conn.execute(sql, params).fetchall()


def repo_detail(conn, full_name: str, *, date: dt.date | None = None) -> dict | None:
    date = date or latest_board_date(conn)
    row = conn.execute(
        f"""
        SELECT {REPO_FIELDS}, {SCORE_FIELDS},
               r.discovered_via, r.history_backfilled_through, r.first_seen_at,
               (SELECT array_agg(t.topic ORDER BY t.topic)
                  FROM repo_topics t WHERE t.repo_id = r.id) AS topics
        FROM repos r
        LEFT JOIN repo_classification c ON c.repo_id = r.id
        LEFT JOIN repo_scores s ON s.repo_id = r.id AND s.date = %(date)s
        WHERE lower(r.full_name) = lower(%(full_name)s)
        """,
        {"full_name": full_name, "date": date},
    ).fetchone()
    if row is None:
        return None

    row["ranks"] = {
        r["board"]: r["rank"]
        for r in conn.execute(
            """
            SELECT board, rank FROM leaderboard_snapshots
            WHERE repo_id = %s AND date = %s AND category = '_all'
            """,
            (row["repo_id"], date),
        )
    }
    return row


def repo_history(conn, full_name: str, *, since: dt.date | None = None) -> list[dict]:
    """Daily star gains, plus the running total, for charting."""
    return conn.execute(
        """
        SELECT d.date, d.stars_gained,
               sum(d.stars_gained) OVER (ORDER BY d.date) AS cumulative
        FROM repo_star_daily d
        JOIN repos r ON r.id = d.repo_id
        WHERE lower(r.full_name) = lower(%(full_name)s)
          AND (%(since)s::date IS NULL OR d.date >= %(since)s)
        ORDER BY d.date
        """,
        {"full_name": full_name, "since": since},
    ).fetchall()


def categories(conn, *, date: dt.date | None = None) -> list[dict]:
    date = date or latest_board_date(conn)
    return conn.execute(
        """
        SELECT c.category,
               count(*) AS repos,
               sum(r.stars) AS stars,
               round(sum(s.velocity_14d)::numeric, 1) AS velocity_14d,
               count(*) FILTER (WHERE s.breakout) AS breakouts
        FROM repo_classification c
        JOIN repos r ON r.id = c.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = c.repo_id AND s.date = %s
        WHERE c.is_ai AND c.category IS NOT NULL
        GROUP BY c.category
        ORDER BY repos DESC
        """,
        (date,),
    ).fetchall()


def search(conn, query: str, *, limit: int = 30, date: dt.date | None = None) -> list[dict]:
    date = date or latest_board_date(conn)
    pattern = f"%{query.strip()}%"
    return conn.execute(
        f"""
        SELECT {REPO_FIELDS}, {SCORE_FIELDS}
        FROM repos r
        LEFT JOIN repo_classification c ON c.repo_id = r.id
        LEFT JOIN repo_scores s ON s.repo_id = r.id AND s.date = %(date)s
        WHERE r.full_name ILIKE %(pattern)s OR r.description ILIKE %(pattern)s
        ORDER BY r.stars DESC
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": min(limit, 100), "date": date},
    ).fetchall()


def movers(
    conn, *, board: str = "momentum", limit: int = 25, date: dt.date | None = None
) -> dict[str, list[dict]]:
    """Biggest rank changes since the previous snapshot, both directions."""
    date = date or latest_board_date(conn)
    if date is None:
        return {"risers": [], "fallers": []}

    rows = conn.execute(
        """
        WITH previous AS (
            SELECT repo_id, rank FROM leaderboard_snapshots
            WHERE board = %(board)s AND category = '_all'
              AND date = (
                  SELECT max(date) FROM leaderboard_snapshots
                  WHERE board = %(board)s AND category = '_all' AND date < %(date)s
              )
        )
        SELECT l.rank, p.rank AS previous_rank, p.rank - l.rank AS rank_delta,
               r.full_name, r.description, r.stars, c.category,
               s.velocity_14d, s.acceleration, s.breakout
        FROM leaderboard_snapshots l
        JOIN repos r ON r.id = l.repo_id
        JOIN previous p ON p.repo_id = l.repo_id
        LEFT JOIN repo_classification c ON c.repo_id = l.repo_id
        LEFT JOIN repo_scores s ON s.repo_id = l.repo_id AND s.date = l.date
        WHERE l.date = %(date)s AND l.board = %(board)s AND l.category = '_all'
          AND p.rank <> l.rank
        ORDER BY abs(p.rank - l.rank) DESC
        LIMIT %(limit)s
        """,
        {"board": board, "date": date, "limit": min(limit, 100) * 2},
    ).fetchall()

    risers = [r for r in rows if r["rank_delta"] > 0][:limit]
    fallers = [r for r in rows if r["rank_delta"] < 0][:limit]
    return {"risers": risers, "fallers": fallers}


def overview(conn) -> dict:
    date = latest_board_date(conn)
    counts = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM repos) AS tracked,
            (SELECT count(*) FROM repo_classification WHERE is_ai) AS ai_repos,
            (SELECT count(*) FROM repos WHERE history_backfilled_through IS NOT NULL)
                AS backfilled,
            (SELECT count(*) FROM repo_star_daily) AS day_rows
        """
    ).fetchone()
    last_run = conn.execute(
        "SELECT command, finished_at, ok, api_calls, llm_cost_usd, notes "
        "FROM run_log WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    return {"as_of": date, "counts": counts, "last_run": last_run}
