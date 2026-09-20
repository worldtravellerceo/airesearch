"""Read API for the AI Radar dashboard.

Runs as a Vercel Function: the Python runtime looks for a top-level `app` in
`app.py`, so this module is the entrypoint. Everything here is read-only — the
pipeline writes, this serves.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

import queries

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Data changes once a day, so anything served can sit on the edge for an hour.
CACHE_CONTROL = "public, s-maxage=3600, stale-while-revalidate=86400"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app = FastAPI(title="AI Radar API", version="0.1.0", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@contextmanager
def db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        # Serverless means one connection per invocation; point DATABASE_URL at
        # a pooled endpoint (Neon's `-pooler` host) or connections will pile up.
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def cached(payload) -> JSONResponse:
    return JSONResponse(content=jsonable(payload), headers={"Cache-Control": CACHE_CONTROL})


def jsonable(value):
    """Dates and Decimals do not survive the default JSON encoder."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if hasattr(value, "quantize"):  # Decimal
        return float(value)
    return value


@app.get("/api/health")
def health():
    if not DATABASE_URL:
        return {"ok": False, "reason": "DATABASE_URL is not configured"}
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/overview")
def overview():
    """Universe size, coverage and the last pipeline run — the honesty panel."""
    with db() as conn:
        return cached(queries.overview(conn))


@app.get("/api/leaderboard")
def leaderboard(
    board: str = Query("fresh", pattern="^(popular|momentum|breakout|fresh)$"),
    category: str = Query(queries.ALL_CATEGORIES),
    limit: int = Query(200, ge=1, le=queries.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    language: str | None = Query(None),
    include_archived: bool = Query(True),
    date: dt.date | None = Query(None),
):
    with db() as conn:
        rows = queries.leaderboard(
            conn,
            board=board,
            category=category,
            limit=limit,
            offset=offset,
            language=language,
            include_archived=include_archived,
            date=date,
        )
        as_of = date or queries.latest_board_date(conn)
    return cached({"board": board, "category": category, "as_of": as_of, "entries": rows})


@app.get("/api/categories")
def categories():
    with db() as conn:
        return cached({"categories": queries.categories(conn)})


@app.get("/api/search")
def search(q: str = Query(..., min_length=2), limit: int = Query(30, ge=1, le=100)):
    with db() as conn:
        return cached({"query": q, "results": queries.search(conn, q, limit=limit)})


@app.get("/api/movers")
def movers(
    board: str = Query("momentum", pattern="^(popular|momentum|breakout|fresh)$"),
    limit: int = Query(25, ge=1, le=100),
):
    with db() as conn:
        return cached(queries.movers(conn, board=board, limit=limit))


@app.get("/api/repos/{owner}/{name}")
def repo(owner: str, name: str):
    with db() as conn:
        detail = queries.repo_detail(conn, f"{owner}/{name}")
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{owner}/{name} is not tracked")
    return cached(detail)


@app.get("/api/repos/{owner}/{name}/history")
def history(
    owner: str,
    name: str,
    window: str = Query("1y", pattern="^(90d|1y|2y|all)$"),
):
    since = _window_start(window)
    with db() as conn:
        rows = queries.repo_history(conn, f"{owner}/{name}", since=since)
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"no star history collected for {owner}/{name}"
        )
    return cached({"repo": f"{owner}/{name}", "window": window, "points": rows})


@app.get("/api/compare")
def compare(
    repos: str = Query(..., description="Comma-separated owner/name, up to four"),
    window: str = Query("1y", pattern="^(90d|1y|2y|all)$"),
):
    """Several repos' star curves side by side.

    Each series also carries `day_index` — days since the repo's own creation —
    because that is the only fair way to put a five-year-old project and a
    two-week-old one on the same axis.
    """
    names = [n.strip() for n in repos.split(",") if n.strip()][:4]
    if not names:
        raise HTTPException(status_code=400, detail="no repositories given")

    since = _window_start(window)
    series = []
    with db() as conn:
        for full_name in names:
            detail = queries.repo_detail(conn, full_name)
            if detail is None:
                continue
            points = queries.repo_history(conn, full_name, since=since)
            created = detail.get("created_at")
            created_date = created.date() if isinstance(created, dt.datetime) else created
            for point in points:
                point["day_index"] = (
                    (point["date"] - created_date).days if created_date else None
                )
            series.append(
                {
                    "repo": detail["full_name"],
                    "created_at": created,
                    "stars": detail["stars"],
                    "category": detail.get("category"),
                    "points": points,
                }
            )

    if not series:
        raise HTTPException(status_code=404, detail="none of those repositories are tracked")
    return cached({"window": window, "series": series})


def _window_start(window: str) -> dt.date | None:
    today = dt.date.today()
    return {
        "90d": today - dt.timedelta(days=90),
        "1y": today - dt.timedelta(days=365),
        "2y": today - dt.timedelta(days=730),
        "all": None,
    }[window]
