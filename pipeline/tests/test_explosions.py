"""The explosion boards: every young repository above a level, AI or not.

What they exist for: `storytold/photocraft`, a Photoshop clone in Rust, went
from 2,818 to 31,865 stars between two census days and appeared nowhere on the
site, because the classifier read it — correctly — as an image editor and every
board was gated on is_ai=1. The data to see it had been collected all along.
"""

import datetime as dt
import json

import pytest

from airadar import export_site
from airadar.collect import score, score_explosions
from airadar.db import repo as db
from airadar.gh.metrics import DailyStars
from airadar.scoring import explosions as ex

TODAY = dt.date(2026, 10, 9)


def day(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


# --- measuring a window ------------------------------------------------------


def test_a_repo_younger_than_the_window_is_measured_from_zero_at_creation():
    """Exact without any earlier capture: it had no stars the day it was made."""
    item = ex.measure(1, [(day(1), 22_791), (TODAY, 31_865)], created_at=day(3), today=TODAY)
    assert item.window_days == 3
    assert item.gain_window == 31_865
    assert item.gain_1d == 31_865 - 22_791


def test_an_older_repo_is_measured_from_the_capture_a_week_back():
    points = [(day(10), 1_000), (day(7), 1_500), (day(3), 2_000), (TODAY, 2_700)]
    item = ex.measure(1, points, created_at=day(60), today=TODAY)
    assert item.window_days == 7
    assert item.gain_window == 1_200


def test_without_a_week_of_captures_the_window_is_shorter_and_says_so():
    """The census's first week: three days measured is labelled three days."""
    item = ex.measure(
        1, [(day(3), 5_000), (day(1), 5_300), (TODAY, 5_600)], created_at=day(60), today=TODAY
    )
    assert item.window_days == 3
    assert item.velocity == pytest.approx(200.0)


def test_an_anchor_older_than_two_weeks_is_not_a_current_speed():
    item = ex.measure(1, [(day(19), 1_000), (TODAY, 3_000)], created_at=day(200), today=TODAY)
    assert item.window_days is None
    assert item.velocity is None


def test_a_repository_the_census_did_not_see_today_is_not_measured():
    """Deleted, renamed or taken down: its last count would be stale."""
    assert (
        ex.measure(1, [(day(2), 1_900), (day(1), 1_950)], created_at=day(20), today=TODAY) is None
    )


# --- the level ---------------------------------------------------------------


def young(stars, age, *, velocity=None, window=3):
    gain = None if velocity is None else int(velocity * window)
    return ex.Explosion(
        repo_id=1,
        stars=stars,
        age_days=age,
        window_days=None if velocity is None else window,
        gain_window=gain,
    )


def test_photocraft_is_above_the_level():
    assert ex.board_for(young(31_865, 9, velocity=9_682)) == ex.YOUNG


def test_a_thousand_stars_inside_ninety_days_is_enough_on_its_own():
    assert ex.board_for(young(1_000, 89)) == ex.YOUNG


def test_the_malware_lure_wave_stays_below_the_floor():
    """Measured on 2026-10-06: at a 300-star floor, 11 of the board's top 50
    were lures — Adobe-Acrobat-Pro, AnyDesk, Autodesk-Inventor and friends,
    all at exactly 392 stars, a day old, gone the next day."""
    assert ex.board_for(young(392, 1, velocity=392, window=1)) is None


def test_a_fast_day_old_repository_above_the_floor_is_kept():
    """`noahdunnagan/fsearch`: 869 stars in its first day."""
    assert ex.board_for(young(869, 1, velocity=869, window=1)) == ex.YOUNG


def test_a_slow_young_repository_is_not_an_explosion():
    assert ex.board_for(young(600, 80, velocity=5)) is None


def test_an_older_repository_that_explodes_goes_to_its_own_board():
    """`morluto/rea`: 7,306 to 36,419 stars at 179 days old."""
    item = ex.Explosion(repo_id=1, stars=36_419, age_days=179, window_days=3, gain_window=29_113)
    assert ex.board_for(item) == ex.RESURGENT


def test_a_steady_giant_is_not_resurgent():
    """`sindresorhus/awesome` gains a thousand stars in three days on 515,000."""
    item = ex.Explosion(repo_id=1, stars=516_600, age_days=4_473, window_days=3, gain_window=1_153)
    assert ex.board_for(item) is None


def test_ranking_is_by_current_speed_and_per_board():
    items = [
        ex.Explosion(repo_id=1, stars=2_000, age_days=30, window_days=3, gain_window=300),
        ex.Explosion(repo_id=2, stars=31_865, age_days=9, window_days=3, gain_window=29_047),
        ex.Explosion(repo_id=3, stars=36_419, age_days=179, window_days=3, gain_window=29_113),
        ex.Explosion(repo_id=4, stars=200, age_days=5, window_days=3, gain_window=150),
    ]
    ranked = {e.repo_id: (e.board, e.rank) for e in ex.rank(items)}
    assert ranked == {2: (ex.YOUNG, 1), 1: (ex.YOUNG, 2), 3: (ex.RESURGENT, 1)}


# --- scoring against the database --------------------------------------------


def add_repo(conn, repo_id, full_name, *, age, is_ai, category=None, description=None):
    owner, name = full_name.split("/")
    created = TODAY - dt.timedelta(days=age)
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=repo_id,
                full_name=full_name,
                owner=owner,
                name=name,
                created_at=dt.datetime.combine(created, dt.time(), dt.UTC),
                description=description or f"{name} project",
                language="Rust",
                stars=0,
                discovered_via="nursery",
            )
        ],
    )
    if is_ai is not ...:
        db.save_classification(
            conn,
            repo_id,
            is_ai=is_ai,
            category=category,
            subcategory=None,
            confidence=0.18 if is_ai is False else 0.5,
            method="rules",
            content_hash=f"h{repo_id}",
        )


def snapshots(conn, repo_id, counts: dict[int, int]):
    for back, stars in counts.items():
        db.record_snapshot(conn, repo_id, day(back), stars=stars, forks=stars // 10)
    conn.commit()


def background(conn, *, days=range(4)):
    """What every real day has around the rows under test: a census of quiet
    repositories above 1,000 stars and a nursery of quiet young ones below it.
    A day's coverage is judged against these, as it is against the real 65,000
    and 13,000."""
    for i in range(20):
        add_repo(conn, 500 + i, f"census/quiet{i}", age=400, is_ai=False)
        add_repo(conn, 600 + i, f"nursery/quiet{i}", age=30, is_ai=False)
        for back in days:
            db.record_snapshot(conn, 500 + i, day(back), stars=5_000)
            db.record_snapshot(conn, 600 + i, day(back), stars=100)
    conn.commit()


def seeded(conn):
    """photocraft (not AI), an unsettled one, an AI one, a slow one, a vanished
    one, plus a tracked AI repository with real star history for the AI boards."""
    background(conn)
    add_repo(conn, 10, "storytold/photocraft", age=9, is_ai=False)
    snapshots(conn, 10, {3: 2_818, 2: 13_021, 1: 22_791, 0: 31_865})
    add_repo(conn, 11, "storytold/lightcraft", age=9, is_ai=None)
    snapshots(conn, 11, {3: 561, 2: 2_100, 1: 4_287, 0: 6_932})
    add_repo(conn, 12, "openai/math", age=3, is_ai=True, category="llm-app")
    snapshots(conn, 12, {2: 4_000, 1: 11_462, 0: 12_792})
    add_repo(conn, 13, "slow/project", age=80, is_ai=False)
    snapshots(conn, 13, {3: 590, 2: 595, 1: 598, 0: 600})
    add_repo(conn, 14, "lure/Adobe-Acrobat-Pro", age=2, is_ai=False)
    snapshots(conn, 14, {1: 1_200})  # not seen today
    add_repo(conn, 15, "new/today", age=1, is_ai=False)
    snapshots(conn, 15, {1: 100, 0: 900})  # crosses the level today

    add_repo(conn, 20, "tracked/agent", age=400, is_ai=True, category="agent-framework")
    created = TODAY - dt.timedelta(days=400)
    db.record_star_daily(
        conn, 20, [DailyStars(created + dt.timedelta(days=i), 20) for i in range(401)]
    )
    db.set_backfill_watermark(conn, 20, created)
    conn.commit()


def test_a_non_ai_explosion_is_ranked_and_the_vanished_one_is_not(conn):
    seeded(conn)
    score_explosions(conn, today=TODAY)
    rows = db.load_explosion_board(conn, date=TODAY, board=ex.YOUNG)
    names = [row["full_name"] for row in rows]
    assert names[0] == "storytold/photocraft"
    assert set(names) == {
        "storytold/photocraft",
        "storytold/lightcraft",
        "openai/math",
        "new/today",
    }
    by_name = {row["full_name"]: row for row in rows}
    # The label keeps its three values apart.
    assert by_name["storytold/photocraft"]["is_ai"] is False
    assert by_name["storytold/lightcraft"]["is_ai"] is None
    assert by_name["openai/math"]["is_ai"] is True


def ai_boards_of(conn) -> list[tuple]:
    return [
        (row["board"], row["category"], row["rank"], row["repo_id"], row["score"])
        for row in conn.execute(
            "SELECT board, category, rank, repo_id, score FROM leaderboard_snapshots "
            "ORDER BY 1, 2, 3"
        )
    ]


def test_explosions_never_reach_or_move_the_ai_boards(tmp_path):
    """The negative control: two databases, one with the non-AI explosions and
    one without, score to identical AI boards."""
    boards = []
    for with_explosions in (True, False):
        with db.connect(tmp_path / f"{with_explosions}.db") as conn:
            db.apply_schema(conn)
            seeded(conn)
            if not with_explosions:
                conn.execute("DELETE FROM repo_snapshots WHERE repo_id IN (10, 11, 13, 14, 15)")
                conn.commit()
            score(conn, today=TODAY)
            boards.append(ai_boards_of(conn))
    with_them, without_them = boards
    assert with_them == without_them
    assert {row[3] for row in with_them} <= {12, 20}


def test_missing_earlier_days_are_filled_in_from_their_snapshots(conn):
    """ "New today" needs yesterday's board, and the first run had none."""
    seeded(conn)
    score_explosions(conn, today=TODAY)
    dates = [
        row["date"]
        for row in conn.execute("SELECT DISTINCT date FROM explosion_board ORDER BY date")
    ]
    assert dates == [day(3), day(2), day(1), TODAY]


def test_a_day_the_census_did_not_cover_is_not_backfilled(conn):
    """2,900 tracked-repo snapshots are not a census, and a board ranked from
    them would be a different board."""
    seeded(conn)
    conn.execute("DELETE FROM repo_snapshots WHERE date = ? AND repo_id != 20", (day(2),))
    conn.commit()
    score_explosions(conn, today=TODAY)
    dates = {row["date"] for row in conn.execute("SELECT DISTINCT date FROM explosion_board")}
    assert day(2) not in dates
    assert TODAY in dates


def test_rescoring_a_day_replaces_it(conn):
    """`score` runs several times a day (five on 2026-10-05)."""
    seeded(conn)
    score_explosions(conn, today=TODAY)
    score_explosions(conn, today=TODAY)
    n = conn.execute("SELECT count(*) AS n FROM explosion_board WHERE date = ?", (TODAY,))
    assert n.fetchone()["n"] == 4


# --- export ------------------------------------------------------------------


def read(out_dir, *parts):
    return json.loads(out_dir.joinpath(*parts).read_text())


def export(conn, tmp_path):
    seeded(conn)
    score(conn, today=TODAY)
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)
    return out


def test_photocraft_reaches_the_explosion_board_and_nothing_ai_only(conn, tmp_path):
    out = export(conn, tmp_path)
    board = read(out, "patlayanlar", "son-90-gun.json")
    names = [row["full_name"] for row in board["entries"]]
    assert names[0] == "storytold/photocraft"

    # ...and stays off every AI-only file.
    for path in [*out.joinpath("boards").rglob("*.json"), out / "index.json", out / "feed.json"]:
        assert "storytold/photocraft" not in path.read_text(), path
    assert "storytold/photocraft" not in out.joinpath("feed.ndjson").read_text()


def test_absence_of_evidence_is_never_printed_as_not_ai(conn, tmp_path):
    out = export(conn, tmp_path)
    rows = {r["full_name"]: r for r in read(out, "patlayanlar", "son-90-gun.json")["entries"]}
    # Nothing has been read yet: "not looked", not "nothing there".
    assert rows["storytold/photocraft"]["ai_tags"] == ["unchecked"]
    assert rows["storytold/lightcraft"]["ai_tags"] == ["unsettled"]
    assert rows["openai/math"]["ai_tags"] == ["ai_project"]
    for row in rows.values():
        assert "not_ai" not in row["ai_tags"]


def test_an_explosion_row_links_to_a_page_only_when_one_was_written(conn, tmp_path):
    out = export(conn, tmp_path)
    pages = set(read(out, "manifest.json")["repos"])
    for row in read(out, "patlayanlar", "son-90-gun.json")["entries"]:
        assert row["has_page"] == (row["full_name"] in pages)
    assert "storytold/photocraft" not in pages


def test_new_today_compares_with_the_previous_board(conn, tmp_path):
    out = export(conn, tmp_path)
    entered = read(out, "patlayanlar", "bugun-girenler.json")
    # new/today crossed the level on the last day; the others were already in.
    assert [row["full_name"] for row in entered["entries"]] == ["new/today"]
    assert entered["since"] == day(1).isoformat()
    digest = read(out, "digest.json")["explosions"]
    assert [row["full_name"] for row in digest["entered"]] == ["new/today"]
    assert digest["total"] == 4


def test_on_a_first_day_there_is_no_new_today_tab(conn, tmp_path):
    """With nothing to compare against, every row would be "new" — true of nothing."""
    background(conn, days=[0])
    add_repo(conn, 10, "storytold/photocraft", age=9, is_ai=False)
    snapshots(conn, 10, {0: 31_865})
    add_repo(conn, 20, "tracked/agent", age=400, is_ai=True, category="agent-framework")
    created = TODAY - dt.timedelta(days=400)
    db.record_star_daily(
        conn, 20, [DailyStars(created + dt.timedelta(days=i), 20) for i in range(401)]
    )
    conn.commit()
    score(conn, today=TODAY)
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)
    slugs = [b["slug"] for b in read(out, "manifest.json")["explosions"]]
    assert slugs == ["son-90-gun"]
    assert not out.joinpath("patlayanlar", "bugun-girenler.json").exists()


def test_every_repo_link_in_the_digest_has_a_page(conn, tmp_path):
    """/bugun linked every arrival to /repos/<name>/, and `_details` never wrote
    those pages: 17 of 17 arrivals on 2026-10-09 were links to a 404."""
    seeded(conn)
    add_repo(conn, 30, "fresh/arrival", age=5, is_ai=True, category="llm-app")
    conn.execute(
        "UPDATE repos SET stars = 900, first_seen_at = ? WHERE id = 30",
        (dt.datetime.combine(TODAY, dt.time(9), dt.UTC),),
    )
    # Push it off every board and out of the top-by-stars page set, as a real
    # arrival is: more than BOARD_LIMIT bigger, faster AI repositories.
    for repo_id in range(100, 100 + export_site.BOARD_LIMIT + 5):
        add_repo(conn, repo_id, f"big/repo{repo_id}", age=300, is_ai=True, category="llm-app")
        conn.execute("UPDATE repos SET stars = ? WHERE id = ?", (100_000 + repo_id, repo_id))
        created = TODAY - dt.timedelta(days=300)
        db.record_star_daily(
            conn,
            repo_id,
            [DailyStars(created + dt.timedelta(days=i), 300) for i in range(301)],
        )
        db.set_backfill_watermark(conn, repo_id, created)
    # And out of the top-by-stars page set: DETAIL_LIMIT bigger repositories,
    # AI or not, since that cut is taken over the whole corpus.
    db.upsert_repos(
        conn,
        [
            db.RepoRecord(
                id=repo_id,
                full_name=f"giant/repo{repo_id}",
                owner="giant",
                name=f"repo{repo_id}",
                stars=50_000,
            )
            for repo_id in range(1_000, 1_000 + export_site.DETAIL_LIMIT)
        ],
    )
    # Only the arrival arrived today.
    conn.execute(
        "UPDATE repos SET first_seen_at = ? WHERE id != 30",
        (dt.datetime.combine(day(30), dt.time(9), dt.UTC),),
    )
    conn.commit()
    score(conn, today=TODAY)
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)

    digest = read(out, "digest.json")
    pages = set(read(out, "manifest.json")["repos"])
    linked = {row["full_name"] for key in export_site.DIGEST_LINKED for row in digest[key]}
    assert "fresh/arrival" in linked
    assert linked <= pages


def test_a_day_the_nursery_missed_is_not_backfilled(conn, tmp_path):
    """The census alone is 65,000 of a full day's 78,500 snapshots, so a total
    count passed a day the nursery never ran. Ranked anyway, that board lacked
    every young repository under 1,000 stars, and the next day's "new today"
    read 228 instead of 29 — two hundred projects announced as arrivals."""
    seeded(conn)
    conn.execute("DELETE FROM repo_snapshots WHERE date = ? AND stars < 1000", (day(1),))
    conn.commit()
    score(conn, today=TODAY)
    dates = {row["date"] for row in conn.execute("SELECT DISTINCT date FROM explosion_board")}
    assert day(1) not in dates

    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)
    entered = read(out, "patlayanlar", "bugun-girenler.json")
    # Compared with the last day both channels covered, and labelled with it.
    assert entered["since"] == day(2).isoformat()
    assert "slow/project" not in [row["full_name"] for row in entered["entries"]]


def test_today_is_not_ranked_when_the_nursery_missed_it(conn):
    """Better the last good board, labelled with its date, than a wrong one."""
    seeded(conn)
    conn.execute("DELETE FROM repo_snapshots WHERE date = ? AND stars < 1000", (TODAY,))
    conn.commit()
    assert score_explosions(conn, today=TODAY) == 0
    assert db.latest_explosion_date(conn, on_or_before=TODAY) == day(1)


def test_a_census_cut_short_is_not_backfilled(conn):
    """A census that died after 60% of its pages passed the first coverage
    check: half of a day's total is less than 60% of the census plus the
    nursery. Each channel is now held to its own share."""
    seeded(conn)
    for repo_id in range(500, 508):  # 8 of 20 census repositories missing
        conn.execute("DELETE FROM repo_snapshots WHERE repo_id = ? AND date = ?", (repo_id, day(1)))
    conn.commit()
    score_explosions(conn, today=TODAY)
    dates = {row["date"] for row in conn.execute("SELECT DISTINCT date FROM explosion_board")}
    assert day(1) not in dates


def test_overview_last_run_is_the_pipeline_not_an_auxiliary_step(conn, tmp_path):
    """explosion-evidence runs after score and may fail without the run having
    failed; the watchdog reads overview.json's last_run.ok as "did the run work"."""
    seeded(conn)
    score(conn, today=TODAY)
    run = db.start_run(conn, "score")
    db.finish_run(conn, run, ok=True, notes="score")
    aux = db.start_run(conn, "explosion-evidence")
    db.finish_run(conn, aux, ok=False, notes="refused")
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)
    last = read(out, "overview.json")["last_run"]
    assert (last["command"], bool(last["ok"])) == ("score", True)


def test_a_lure_shaped_repository_is_held_back_by_name(conn, tmp_path):
    """The 500-star floor held for four days and failed on the fifth: on
    2026-10-10 KMS-Pico, AnyUnlock, Total-Commander and four more entered
    "Bugün girenler" at 503-614 stars, a day old — 0 forks, no language, no
    licence, like every one of the 10-06 wave."""
    seeded(conn)
    add_repo(conn, 40, "KindStatesman/KMS-Pico", age=1, is_ai=False)
    conn.execute("UPDATE repos SET language = NULL WHERE id = 40")
    db.record_snapshot(conn, 40, TODAY, stars=614, forks=0)
    add_repo(conn, 41, "gry/zero-forks-but-code", age=1, is_ai=False)
    conn.execute("UPDATE repos SET language = 'Python' WHERE id = 41")
    db.record_snapshot(conn, 41, TODAY, stars=1_088, forks=0)
    conn.commit()
    score(conn, today=TODAY)
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)

    board = read(out, "patlayanlar", "son-90-gun.json")
    names = [row["full_name"] for row in board["entries"]]
    assert "KindStatesman/KMS-Pico" not in names
    assert [row["full_name"] for row in board["held_back"]] == ["KindStatesman/KMS-Pico"]
    # The negative control: no forks yet, but there is code.
    assert "gry/zero-forks-but-code" in names
    entered = [r["full_name"] for r in read(out, "patlayanlar", "bugun-girenler.json")["entries"]]
    assert "KindStatesman/KMS-Pico" not in entered


def test_an_unknown_fork_count_is_not_zero():
    """A capture with no fork count says nothing about forks."""
    assert not ex.lure_shaped(forks=None, language=None, license=None)
    assert ex.lure_shaped(forks=0, language=None, license=None)
    assert not ex.lure_shaped(forks=0, language=None, license="MIT")


def test_the_ai_digest_skips_a_lure_too(conn, tmp_path):
    """Discord-Server-Raider calls itself "an AI-driven solution", so the
    classifier put it on the AI digest's "Yeni projeler" on 2026-10-10."""
    seeded(conn)
    add_repo(
        conn, 42, "navyofficerpipe/Discord-Server-Raider", age=1, is_ai=True, category="llm-app"
    )
    conn.execute(
        "UPDATE repos SET stars = 605, language = NULL, first_seen_at = ? WHERE id = 42",
        (dt.datetime.combine(TODAY, dt.time(9), dt.UTC),),
    )
    db.record_snapshot(conn, 42, TODAY, stars=605, forks=0)
    conn.commit()
    score(conn, today=TODAY)
    out = tmp_path / "site"
    export_site.export(conn, out, date=TODAY)
    digest = read(out, "digest.json")
    listed = {row["full_name"] for key in export_site.DIGEST_LINKED for row in digest[key]}
    assert "navyofficerpipe/Discord-Server-Raider" not in listed
    assert 42 not in db.digest_arrival_ids(conn, date=TODAY)
