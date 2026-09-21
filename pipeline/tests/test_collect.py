"""End-to-end collection tests: mocked GitHub transport, real database."""

import datetime as dt

import httpx
import pytest

from airadar.collect import backfill, collect, score
from airadar.db import repo as db
from airadar.gh.client import GitHubClient

TODAY = dt.date(2026, 9, 20)
SUNDAY = dt.datetime(2026, 9, 13, tzinfo=dt.UTC)


def repo_json(repo_id=1, full_name="acme/agent", stars=1_000, created="2026-09-01T00:00:00Z"):
    owner, name = full_name.split("/")
    return {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "created_at": created,
        "pushed_at": "2026-09-19T12:00:00Z",
        "description": "agent framework",
        "homepage": "",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "archived": False,
        "fork": False,
        "stargazers_count": stars,
        "forks_count": 42,
        "open_issues_count": 7,
        "topics": ["llm", "agents"],
    }


def week_json(start: dt.datetime, per_day: int):
    return {"week": int(start.timestamp()), "total": per_day * 7, "days": [per_day] * 7}


def _headers(**extra):
    base = {
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": str(int(dt.datetime.now().timestamp()) + 3600),
    }
    base.update(extra)
    return base


async def _noop_sleep(_seconds):  # pragma: no cover - never reached in these tests
    raise AssertionError("no throttling expected in this test")


def seed(conn, *, repo_id=1, full_name="acme/agent", stars=1_000, created="2026-09-01T00:00:00Z"):
    db.upsert_repos(conn, [db.RepoRecord.from_api(repo_json(repo_id, full_name, stars, created))])
    db.save_classification(
        conn,
        repo_id,
        is_ai=True,
        category="agent-framework",
        subcategory=None,
        confidence=1.0,
        method="rules",
        content_hash="h",
    )
    conn.commit()


async def test_collect_writes_snapshot_and_daily_series(conn):
    seed(conn)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/stargazers/history"):
            return httpx.Response(
                200,
                json=[week_json(SUNDAY, 10), week_json(SUNDAY - dt.timedelta(days=7), 4)],
                headers=_headers(etag='W/"hist1"'),
            )
        return httpx.Response(200, json=repo_json(stars=1_500), headers=_headers(etag='W/"repo1"'))

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        report = await collect(conn, client, today=TODAY)

    assert report.refreshed == 1
    assert report.days_written == 14
    assert len(calls) == 2  # exactly two requests per repo

    series = db.load_daily_series(conn, 1)
    assert len(series) == 14
    assert sum(d.stars_gained for d in series) == 98

    snapshot = conn.execute("SELECT * FROM repo_snapshots").fetchone()
    assert snapshot["stars"] == 1_500
    assert snapshot["forks"] == 42
    assert snapshot["date"] == TODAY

    row = conn.execute("SELECT etag_repo, etag_history, last_checked_at FROM repos").fetchone()
    assert row["etag_repo"] == 'W/"repo1"'
    assert row["etag_history"] == 'W/"hist1"'
    assert row["last_checked_at"] is not None


async def test_second_collect_sends_etags_and_costs_nothing_when_unchanged(conn):
    seed(conn)
    conn.execute("UPDATE repos SET etag_repo = 'W/\"r\"', etag_history = 'W/\"h\"' WHERE id = 1")
    conn.commit()
    conditional: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        conditional.append(request.headers.get("if-none-match"))
        return httpx.Response(304, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        report = await collect(conn, client, today=TODAY)

    assert conditional == ['W/"r"', 'W/"h"']
    assert report.unchanged == 1
    assert report.days_written == 0
    assert client.counters.calls == 0  # 304s are free
    assert client.counters.not_modified == 2


async def test_deleted_repo_is_dropped_from_the_queue(conn):
    seed(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"}, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        report = await collect(conn, client, today=TODAY)

    assert report.gone == 1
    assert conn.execute("SELECT count(*) AS n FROM repos").fetchone()["n"] == 0


async def test_one_repo_failing_does_not_abort_the_run(conn):
    seed(conn, repo_id=1, full_name="acme/broken")
    seed(conn, repo_id=2, full_name="acme/fine")

    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in request.url.path:
            return httpx.Response(451, json={"message": "Unavailable"}, headers=_headers())
        if request.url.path.endswith("/stargazers/history"):
            return httpx.Response(200, json=[week_json(SUNDAY, 3)], headers=_headers())
        return httpx.Response(200, json=repo_json(2, "acme/fine"), headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        report = await collect(conn, client, today=TODAY)

    assert report.gone == 1
    assert report.refreshed == 1
    assert db.load_daily_series(conn, 2)


async def test_backfill_walks_pages_until_it_reaches_creation(conn):
    seed(conn, created="2025-09-01T00:00:00Z")  # ~1 year old -> more than one page
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        pages_requested.append(page)
        # 30 weeks per page, walking backwards from the current week.
        start = SUNDAY - dt.timedelta(weeks=30 * (page - 1))
        weeks = [week_json(start - dt.timedelta(weeks=w), 2) for w in range(30)]
        return httpx.Response(200, json=weeks, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        written = await backfill(conn, client)

    assert pages_requested == [1, 2]  # stops as soon as it passes the creation date
    assert written == 2 * 30 * 7

    watermark = conn.execute("SELECT history_backfilled_through FROM repos").fetchone()
    assert watermark["history_backfilled_through"] < dt.date(2025, 9, 1)


async def test_backfill_skips_repos_already_complete(conn):
    seed(conn)
    db.set_backfill_watermark(conn, 1, dt.date(2026, 8, 25))  # before creation
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not call the API for a completed backfill")

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_noop_sleep
    ) as client:
        assert await backfill(conn, client) == 0


async def test_score_builds_boards_and_only_includes_ai_repos(conn):
    seed(conn, repo_id=1, full_name="acme/agent")
    # A non-AI repo that is never classified must not reach the boards.
    db.upsert_repos(conn, [db.RepoRecord.from_api(repo_json(2, "acme/not-ai", 99_000))])
    conn.commit()

    db.record_star_daily(
        conn,
        1,
        [db.DailyStars(TODAY - dt.timedelta(days=i), 100) for i in range(20)],
    )
    db.record_star_daily(conn, 2, [db.DailyStars(TODAY, 5_000)])
    conn.commit()

    scored, board_rows = score(conn, today=TODAY)

    assert scored == 1
    assert board_rows > 0
    board = db.load_leaderboard(conn, date=TODAY, board="momentum")
    assert [r["full_name"] for r in board] == ["acme/agent"]
    assert board[0]["velocity_14d"] == pytest.approx(100.0)


async def test_collect_bounds_itself_to_the_tracked_universe(conn, monkeypatch):
    """The bug this pins, found in the field: `track_limit` was supported by the
    query, documented in the settings, and never passed by the caller.

    The corpus was 64,373 repos at two requests each — 25 hours of quota
    against a job killed after five and a half. The run could not finish, so it
    was cut off part-way down the star order and the tail was never refreshed.
    Nothing failed; the run just quietly did a fraction of its job.
    """
    from airadar import collect as collect_mod

    captured: dict = {}
    real = collect_mod.db.repos_due_for_refresh

    def spy(conn_, **kwargs):
        captured.update(kwargs)
        return real(conn_, **kwargs)

    monkeypatch.setattr(collect_mod.db, "repos_due_for_refresh", spy)

    async def _sleep(_seconds):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={}, headers=_headers())

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_sleep
    ) as client:
        await collect_mod.collect(conn, client, today=TODAY)

    settings = collect_mod.get_settings()
    assert captured["track_limit"] == settings.track_limit
    assert captured["track_limit"] is not None


async def test_repeated_collects_do_not_inflate_the_lifetime_history(conn):
    """The bug a reader found on the site: `openclaw/openclaw` showed a star
    curve topping out near 527k against 390k actual stars, and
    `karpathy/autoresearch` 1.85x its real count. 518 of 1,200 backfilled repos
    were inflated, none were short.

    One history page is thirty weeks but only `retain_days` of it is kept as day
    rows; the rest has already been folded into the weekly buckets. Writing
    those days back handed the next prune the same days again, and the roll-up
    adds rather than replaces — so the weekly total and `fresh_power_tail` both
    grew on every cycle. Two collects over the same unchanged history must come
    to the same total as one.
    """
    seed(conn)
    weeks = [week_json(SUNDAY - dt.timedelta(days=7 * i), 10) for i in range(30)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stargazers/history"):
            return httpx.Response(200, json=weeks, headers=_headers())
        return httpx.Response(200, json=repo_json(stars=2_100), headers=_headers())

    async def _sleep(_seconds):
        return None

    def total() -> int:
        daily = conn.execute(
            "SELECT COALESCE(sum(stars_gained), 0) AS n FROM repo_star_daily"
        ).fetchone()["n"]
        weekly = conn.execute(
            "SELECT COALESCE(sum(stars_gained), 0) AS n FROM repo_star_weekly"
        ).fetchone()["n"]
        return daily + weekly

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_sleep
    ) as client:
        await collect(conn, client, today=TODAY)
        db.prune_star_history(conn, today=TODAY, retain_days=120, half_life_days=180)
        after_first = total()
        tail_first = conn.execute("SELECT fresh_power_tail AS t FROM repos").fetchone()["t"]

        # The same history again — nothing about the repo changed.
        conn.execute("UPDATE repos SET etag_history = NULL, last_checked_at = NULL")
        conn.commit()
        await collect(conn, client, today=TODAY)
        db.prune_star_history(conn, today=TODAY, retain_days=120, half_life_days=180)

    assert total() == after_first, "the lifetime history grew on a repeat collect"
    assert conn.execute("SELECT fresh_power_tail AS t FROM repos").fetchone()["t"] == tail_first
