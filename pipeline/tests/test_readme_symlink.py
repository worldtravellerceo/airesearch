"""A README that is a symbolic link reads as 22 bytes of path, with status 200.

Git stores a symlink as a blob holding its target, and raw.githubusercontent.com
serves that blob. `colinhacks/zod`'s root README.md comes back as
`packages/zod/README.md` — a successful fetch of a file that is not a README,
with nothing in the response to say so.

What it cost: three of the 1,324 repositories on the boards had their two
Turkish paragraphs written from a path instead of a README, `vercel/ai` among
them — the AI SDK, on an AI board, described from 22 bytes. Three of the 200
repositories in the 2026-10-05 review slice had the same, and two of those went
back to the queue unjudged, which is the better of the two failures and still a
wasted slot. The API path does not have this: measured over 55,534 stored
excerpts, none is a symlink body. Only the token-free raw path needs the follow,
and that is the path the packet builders use.
"""

from __future__ import annotations

from airadar.gh.content import symlink_target


def test_a_symlink_body_is_recognised() -> None:
    assert symlink_target("packages/zod/README.md") == "packages/zod/README.md"
    assert symlink_target("directus/readme.md") == "directus/readme.md"
    assert symlink_target("packages/core/react/README.md") == "packages/core/react/README.md"


def test_a_sibling_symlink_has_no_directory_in_it() -> None:
    """`README.md` -> `readme.md` in the same directory.

    The first version of the pattern required a leading path, so this case —
    the plainest symlink there is — was not recognised at all.
    """
    assert symlink_target("readme.md") == "readme.md"
    assert symlink_target("README.markdown") == "README.markdown"


def test_a_real_readme_is_not_mistaken_for_one() -> None:
    assert symlink_target("# AI SDK\n\nThe AI SDK is a TypeScript toolkit.") is None
    assert symlink_target("i. am. speed.") is None
    # A line that merely mentions a README file is not a symlink body.
    assert symlink_target("see readme.md for details") is None
    # Nor is a long single line, whatever it ends with.
    assert symlink_target("x" * 300 + "/README.md") is None


def test_nothing_is_not_a_symlink() -> None:
    assert symlink_target("") is None
    assert symlink_target("   \n  ") is None
