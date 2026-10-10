"""Explosions: the fastest-rising young repositories on GitHub, AI or not.

Every other board here ranks the AI universe, which is decided by a classifier.
That was the right gate for "what is happening in AI" and the wrong one for
"what just exploded": `storytold/photocraft`, a Photoshop clone written in Rust,
went from 2,818 to 31,865 stars in three census days and was invisible, because
the rules engine read it as an image editor (is_ai=0, confidence 0.18) — which
it is. The reader asked for every project above an explosion level, AI or not,
on the grounds that one extra row costs less than one missed project.

So this board has no classifier in it at all. Membership is decided by stars
and age alone; how a project relates to AI is a label on the row, never a
filter. And it costs no GitHub requests: the daily census writes a snapshot for
every repository at or above 1,000 stars, and the nursery does the same for
every repository created in the last 90 days at or above 50. Measured on
2026-10-09, 13,649 young repositories had a snapshot that day, and nothing in
the pipeline had ever read them.

What a snapshot can and cannot say: it is a total captured once a day, not a
stargazer date, so a window's gain is exact only between two captures, and the
whole-census captures start on 2026-10-06. A repository younger than the window
is the exception — it had zero stars when it was created, so its gain since
creation is exact without any earlier capture.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: "Son üç ay", in the reader's words, and the nursery's own horizon — a
#: repository older than this under 1,000 stars is no longer snapshotted at all.
MAX_AGE_DAYS = 90
#: The window a current speed is read over. One day is too noisy: giants lost
#: 205-219 stars between two census days with nothing happening to them.
WINDOW_DAYS = 7
#: The oldest capture accepted as the start of a window. Older anchors exist
#: (sweeps on 2026-09-20 and 09-28), but a speed averaged over three weeks is
#: not a current speed.
MAX_ANCHOR_DAYS = 14

# The explosion level. Measured on 2026-10-09 over the 13,649 young repositories
# with a snapshot that day:
#
#   >= 1,000 stars outright                      572
#   >= 500 and >= 50/day over the window          (adds speed that is now)
#   >= 500 and >= 20/day averaged since creation  (adds speed that was)
#   all three together                           784  (437 AI, 260 not, 87 unsettled)
#
# The 500-star floor under the two speed paths was 300 until it was replayed
# over the four census days. At 300, the 2026-10-06 board's top 50 held 11
# malware lures — Adobe-Acrobat-Pro, AnyDesk, Autodesk-Inventor and friends,
# all at exactly 392 stars, a day old, no language, and deleted by GitHub the
# next day. At 500, none of the four days' top 50 held a single repository
# that later vanished. The floor costs a genuine riser a day, not its place: a
# project at 300 on its first day that keeps going is past 500 on its second.
#
# Everything above the level is kept: no owner cap (storytold holds 14 rows,
# and all 14 are exploding), no cluster rule (near-identical star clusters
# measured no better than shuffled creation times: 231 observed, 212-248 by
# chance), no "not AI" filter. The reader asked for the extra row over the
# missed one.
MIN_STARS_OUTRIGHT = 1_000
MIN_STARS = 500
MIN_WINDOW_VELOCITY = 50.0
MIN_LIFETIME_VELOCITY = 20.0

# An older repository is not young, but it can still explode — `morluto/rea`
# went from 7,306 to 36,419 stars between 10-06 and 10-09 at 179 days old. These
# go on their own board so that "son üç ay" stays what it says. Relative growth
# is what keeps the steady giants off it: `sindresorhus/awesome` gains a
# thousand stars in three days on a base of half a million.
RESURGENT_MIN_GAIN = 1_000
RESURGENT_MIN_GROWTH = 0.10

YOUNG = "young"
RESURGENT = "resurgent"
EXPLOSION_BOARDS = (YOUNG, RESURGENT)
#: Above the level, but shaped like a malware lure: kept out of the boards and
#: published by name only, so that holding them back is visible.
HELD_BACK = "held_back"


def lure_shaped(*, forks: int | None, language: str | None, license: str | None) -> bool:
    """No forks, no code GitHub can name, no licence.

    The 500-star floor held for four days and failed on the fifth: on
    2026-10-10 seven repositories created the day before entered "Bugün
    girenler" at 503-614 stars — KMS-Pico, AnyUnlock, Total-Commander,
    Discord-Server-Raider, the same "optimizes your professional desktop"
    template as the 392-star wave of 10-06. All seven had 0 forks, no
    language and no licence, as had all fourteen of the 10-06 wave. Across
    every census day from 10-06 to 10-10, no young repository at 500 stars
    or more had all three and was anything else; of the 1,226 young
    repositories at 500+ on 10-10, eleven had 0 forks, and the three of those
    with code (a language) were left alone. A project that real people star
    gets forked.
    """
    # 0, not None: a capture with no fork count is unknown, not unforked.
    return forks == 0 and not language and not license


@dataclass
class Explosion:
    repo_id: int
    stars: int
    age_days: int | None
    #: Days between the window's anchor and today, or None when there is no
    #: anchor — never 0, which would read as "measured, and nothing happened".
    window_days: int | None = None
    gain_window: int | None = None
    gain_1d: int | None = None
    forks: int | None = None
    lure: bool = False
    board: str | None = None
    rank: int = 0

    @property
    def velocity(self) -> float | None:
        """Stars a day over the window, when there is a window."""
        if self.window_days is None or self.gain_window is None:
            return None
        return self.gain_window / self.window_days

    @property
    def lifetime_velocity(self) -> float | None:
        """Stars a day averaged since creation — exact, and slow to react."""
        if self.age_days is None:
            return None
        return self.stars / max(1, self.age_days)

    @property
    def growth(self) -> float | None:
        """Gain over the window relative to where it started."""
        if self.gain_window is None:
            return None
        return self.gain_window / max(1, self.stars - self.gain_window)

    @property
    def score(self) -> float:
        """What the boards rank by: the current speed, else the lifetime one."""
        if self.velocity is not None:
            return self.velocity
        return self.lifetime_velocity or 0.0


def measure(
    repo_id: int,
    points: Sequence[tuple[dt.date, int]],
    *,
    created_at: dt.date | None,
    today: dt.date,
    forks: int | None = None,
    language: str | None = None,
    license: str | None = None,
) -> Explosion | None:
    """Read one repository's window off its daily captures.

    `points` are (date, total stars), any order; one of them must be today's,
    because a repository the census did not see today may have been deleted,
    renamed or taken down, and its last count would be stale.
    """
    series = sorted(points)
    if not series or series[-1][0] != today:
        return None
    stars = series[-1][1]
    age = (today - created_at).days if created_at is not None else None

    anchor: tuple[dt.date, int] | None = None
    if age is not None and age <= WINDOW_DAYS:
        # Zero stars on the day it was created: exact, with no capture needed.
        anchor = (created_at, 0)  # type: ignore[assignment]
    else:
        target = today - dt.timedelta(days=WINDOW_DAYS)
        oldest = today - dt.timedelta(days=MAX_ANCHOR_DAYS)
        before = [p for p in series if oldest <= p[0] <= target]
        if before:
            anchor = before[-1]
        else:
            # A shorter window than asked for, and labelled as such by its
            # length, beats no window: on the census's first week this is
            # every repository.
            inside = [p for p in series if target < p[0] < today]
            anchor = inside[0] if inside else None

    window_days = gain_window = None
    if anchor is not None:
        window_days = max(1, (today - anchor[0]).days)
        gain_window = stars - anchor[1]

    yesterday = today - dt.timedelta(days=1)
    previous = [s for d, s in series if d == yesterday]
    gain_1d = stars - previous[0] if previous else None

    return Explosion(
        repo_id=repo_id,
        stars=stars,
        age_days=age,
        window_days=window_days,
        gain_window=gain_window,
        gain_1d=gain_1d,
        forks=forks,
        lure=lure_shaped(forks=forks, language=language, license=license),
    )


def board_for(item: Explosion) -> str | None:
    """Which explosion board a repository belongs on, if any."""
    if item.age_days is None:
        return None
    if item.age_days <= MAX_AGE_DAYS:
        if item.stars >= MIN_STARS_OUTRIGHT:
            return YOUNG
        if item.stars >= MIN_STARS and (
            (item.velocity or 0.0) >= MIN_WINDOW_VELOCITY
            or (item.lifetime_velocity or 0.0) >= MIN_LIFETIME_VELOCITY
        ):
            return YOUNG
        return None
    if (
        item.gain_window is not None
        and item.gain_window >= RESURGENT_MIN_GAIN
        and (item.growth or 0.0) >= RESURGENT_MIN_GROWTH
    ):
        return RESURGENT
    return None


def rank(items: Iterable[Explosion]) -> list[Explosion]:
    """Assign boards and ranks; drop everything below the explosion level."""
    boards: dict[str, list[Explosion]] = {name: [] for name in (*EXPLOSION_BOARDS, HELD_BACK)}
    for item in items:
        board = board_for(item)
        if board is None:
            continue
        if item.lure:
            board = HELD_BACK
        item.board = board
        boards[board].append(item)
    ranked: list[Explosion] = []
    for members in boards.values():
        members.sort(key=lambda e: (-e.score, -e.stars, e.repo_id))
        for position, item in enumerate(members, start=1):
            item.rank = position
        ranked.extend(members)
    return ranked
