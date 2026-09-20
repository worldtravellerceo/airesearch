"""Read API tests against a seeded database.

The scenario is the one from the brief: a five-year incumbent and a two-week
newcomer with identical star totals, plus a genuine breakout. What the API must
get right is that the four boards disagree with each other.
"""

import datetime as dt
import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from airadar import collect as collect_mod  # noqa: E402
from airadar.db import repo as db  # noqa: E402
from airadar.gh.metrics import DailyStars  # noqa: E402

TEST_DSN = os.environ.get(
    "AIRADAR_TEST_DSN", "postgresql://postgres@/airadar_test?host=/tmp&port=55432"
)
TODAY = dt.date.today()


def _reachable() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test PostgreSQL available")


def flat_series(created: dt.date, end: dt.date, per_day: float) -> list[DailyStars]:
    out, day = [], created
    while day <= end:
        out.append(DailyStars(day, round(per_day)))
        day += dt.timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = TEST_DSN
    for module in ("app", "queries"):
        sys.modules.pop(module, None)
    import app as api_app
    from fastapi.testclient import TestClient

    _seed()
    with TestClient(api_app.app) as test_client:
        yield test_client


def _seed():
    scenarios = [
        # id, name, age_days, stars, category, shape
        (1, "legacy/ml-toolkit", 1826, 50_000, "classic-ml", "flat"),
        (2, "newcomer/agent-os", 14, 50_000, "agent-framework", "flat"),
        (3, "giant/deep-framework", 3200, 190_000, "classic-ml", "flat"),
        (4, "surging/mcp-bridge", 400, 6_800, "mcp", "surge"),
        (5, "quiet/rag-helper", 900, 2_100, "rag-vectordb", "flat"),
    ]
    with db.connect(TEST_DSN) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS leaderboard_snapshots, repo_scores, "
            "repo_classification, repo_star_daily, repo_snapshots, repo_topics, "
            "queried_topics, pending_repos, run_log, repos CASCADE"
        )
        conn.commit()
        db.apply_schema(conn)

        for repo_id, name, age, stars, category, shape in scenarios:
            created = TODAY - dt.timedelta(days=age - 1)
            if shape == "flat":
                days = flat_series(created, TODAY, stars / age)
            else:
                days = flat_series(
                    created, TODAY - dt.timedelta(days=15), stars * 0.25 / (age - 14)
                )
                days += flat_series(TODAY - dt.timedelta(days=14), TODAY, stars * 0.75 / 14)

            owner, repo = name.split("/")
            db.upsert_repos(
                conn,
                [
                    db.RepoRecord(
                        id=repo_id,
                        full_name=name,
                        owner=owner,
                        name=repo,
                        created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
                        description=f"{category} project",
                        language="Python",
                        stars=stars,
                        discovered_via="topic",
                        topics=("llm",),
                    )
                ],
            )
            db.save_classification(
                conn,
                repo_id,
                is_ai=True,
                category=category,
                subcategory=None,
                confidence=1.0,
                method="rules",
                content_hash=f"h{repo_id}",
                one_liner=f"A {category} project.",
            )
            db.record_star_daily(conn, repo_id, days)
            db.set_backfill_watermark(conn, repo_id, created)
            conn.commit()

        # Two consecutive scoring days, so rank deltas have something to compare.
        collect_mod.score(conn, today=TODAY - dt.timedelta(days=1))
        collect_mod.score(conn, today=TODAY)


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_the_four_boards_disagree(client):
    def ids(board):
        payload = client.get(f"/api/leaderboard?board={board}&limit=10").json()
        return [e["full_name"] for e in payload["entries"]]

    popular = ids("popular")
    fresh = ids("fresh")

    assert popular[0] == "giant/deep-framework"  # biggest by raw stars
    assert fresh[0] == "newcomer/agent-os"  # biggest once stars are aged
    assert popular != fresh  # the point of having both


def test_leaderboard_carries_everything_a_row_needs(client):
    entry = client.get("/api/leaderboard?board=momentum&limit=1").json()["entries"][0]

    for field in (
        "rank",
        "full_name",
        "description",
        "stars",
        "category",
        "one_liner",
        "velocity_14d",
        "acceleration",
        "fresh_power",
        "momentum_score",
        "breakout",
        "coverage_days",
        "sparkline",
        "rank_delta",
    ):
        assert field in entry, field
    assert isinstance(entry["sparkline"], list) and entry["sparkline"]


def test_rank_delta_is_measured_against_the_previous_snapshot(client):
    entries = client.get("/api/leaderboard?board=fresh&limit=10").json()["entries"]
    # Both scoring days exist, so deltas are numbers rather than nulls.
    assert any(e["rank_delta"] is not None for e in entries)


def test_category_filter_narrows_the_board(client):
    payload = client.get("/api/leaderboard?board=popular&category=classic-ml").json()
    names = [e["full_name"] for e in payload["entries"]]

    assert names == ["giant/deep-framework", "legacy/ml-toolkit"]


def test_breakout_board_only_lists_flagged_repos(client):
    names = [
        e["full_name"] for e in client.get("/api/leaderboard?board=breakout").json()["entries"]
    ]
    assert names == ["surging/mcp-bridge"]


def test_categories_endpoint_aggregates(client):
    payload = client.get("/api/categories").json()["categories"]
    by_name = {c["category"]: c for c in payload}

    assert by_name["classic-ml"]["repos"] == 2
    assert by_name["mcp"]["breakouts"] == 1


def test_repo_detail_includes_ranks_and_milestones(client):
    detail = client.get("/api/repos/newcomer/agent-os").json()

    assert detail["full_name"] == "newcomer/agent-os"
    assert detail["topics"] == ["llm"]
    assert detail["ranks"]["fresh"] == 1
    assert detail["days_to_10k"] is not None and detail["days_to_10k"] < 5


def test_unknown_repo_is_a_404_not_an_empty_object(client):
    response = client.get("/api/repos/nobody/nothing")
    assert response.status_code == 404
    assert "not tracked" in response.json()["detail"]


def test_history_returns_a_cumulative_curve(client):
    points = client.get("/api/repos/newcomer/agent-os/history?window=90d").json()["points"]

    assert len(points) == 14
    assert points[0]["cumulative"] < points[-1]["cumulative"]
    assert points[-1]["cumulative"] == pytest.approx(50_000, rel=0.01)


def test_compare_puts_repos_on_a_days_since_creation_axis(client):
    """Absolute dates make a five-year-old and a two-week-old project
    incomparable; days since birth is the axis that answers the question."""
    payload = client.get("/api/compare?repos=legacy/ml-toolkit,newcomer/agent-os&window=all").json()
    by_repo = {s["repo"]: s for s in payload["series"]}

    assert set(by_repo) == {"legacy/ml-toolkit", "newcomer/agent-os"}
    assert by_repo["newcomer/agent-os"]["points"][0]["day_index"] == 0
    assert by_repo["legacy/ml-toolkit"]["points"][0]["day_index"] == 0
    assert by_repo["legacy/ml-toolkit"]["points"][-1]["day_index"] > 1_800


def test_compare_is_capped_at_four_series(client):
    payload = client.get(
        "/api/compare?repos=legacy/ml-toolkit,newcomer/agent-os,giant/deep-framework,"
        "surging/mcp-bridge,quiet/rag-helper"
    ).json()
    assert len(payload["series"]) == 4


def test_compare_with_no_known_repos_is_a_404(client):
    assert client.get("/api/compare?repos=nobody/nothing").status_code == 404


def test_search_matches_name_and_description(client):
    results = client.get("/api/search?q=agent").json()["results"]
    assert any(r["full_name"] == "newcomer/agent-os" for r in results)

    by_description = client.get("/api/search?q=rag-vectordb").json()["results"]
    assert any(r["full_name"] == "quiet/rag-helper" for r in by_description)


def test_movers_splits_risers_from_fallers(client):
    payload = client.get("/api/movers?board=fresh").json()
    assert set(payload) == {"risers", "fallers"}
    assert all(r["rank_delta"] > 0 for r in payload["risers"])
    assert all(f["rank_delta"] < 0 for f in payload["fallers"])


def test_overview_reports_coverage(client):
    payload = client.get("/api/overview").json()
    assert payload["counts"]["tracked"] == 5
    assert payload["counts"]["ai_repos"] == 5
    assert payload["counts"]["backfilled"] == 5
    assert payload["as_of"] is not None


def test_responses_are_edge_cacheable(client):
    response = client.get("/api/leaderboard?board=fresh&limit=1")
    assert "s-maxage" in response.headers["cache-control"]


def test_bad_board_name_is_rejected(client):
    assert client.get("/api/leaderboard?board=nonsense").status_code == 422
