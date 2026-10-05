"""Turn a review-queue slice into the packets a judging agent reads.

`airadar review-queue` writes everything the rule engine knows about a repo it
could not settle — description, topics, language, which signals fired — and
that is deliberately not enough to decide. The signals are the reason the
engine hesitated, not the answer: `readme-late:llm` says the README mentions a
model once, far from the top, which is normally a passing mention. The only way
to tell a project that *is* AI from one that merely says the word is to read the
README, so this adds it.

The README comes off raw.githubusercontent.com rather than the API: that needs
no token, so a slice can be prepared from anywhere, and it is the same source
`build_summary_packets.py` uses. `HEAD` resolves the repository's own default
branch — guessing between `main` and `master` fetched substantially wrong text
for 25 of 1,324 repositories, and nothing about the result looked wrong.

One packet per agent. Twenty repositories is the size that fits with the README
excerpts: the ten packets built this way for the 2026-10-05 review came to
~90 KB each.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airadar.classify.taxonomy import CATEGORIES  # noqa: E402
from airadar.gh.content import symlink_target  # noqa: E402

README_CHARS = 4000


def fetch_readme(client: httpx.Client, full_name: str) -> str:
    """The README off the repository's default branch, or an empty string.

    A repository with no README is kept rather than dropped. It is still a
    decidable case sometimes — `anomalyco/opencode` needed only its one-line
    description — and when it is not, the agent is told to leave it out, which
    returns it to the queue instead of labelling it wrongly.

    A body that turns out to be a symbolic link is followed once. `symlink_target`
    explains what that costs when it is not: `vercel/ai` had its board paragraph
    written from the 22 bytes `packages/ai/README.md`.
    """
    for name in ("README.md", "readme.md", "README.rst", "README"):
        text = _get(client, full_name, name)
        if not text:
            continue
        target = symlink_target(text)
        if target:
            text = _get(client, full_name, target)
            if not text or symlink_target(text):
                continue
        return text[:README_CHARS]
    return ""


def _get(client: httpx.Client, full_name: str, path: str) -> str:
    try:
        response = client.get(
            f"https://raw.githubusercontent.com/{full_name}/HEAD/{path}", timeout=30
        )
    except Exception:
        return ""
    return response.text if response.status_code == 200 and response.text.strip() else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, help="review-queue.json")
    parser.add_argument("--out", required=True, help="Directory for the packets")
    parser.add_argument("--per-agent", type=int, default=20)
    parser.add_argument(
        "--take",
        type=int,
        default=200,
        help="How many of the queue to prepare, biggest first. The queue holds "
        "a whole slice; a judging round works through part of it.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Only repos the engine scored at least this high. 0.5 selects the "
        "half of the escalation band where evidence exists and was strong — "
        "measured as the highest-yield slice to read by hand.",
    )
    args = parser.parse_args()

    queue = json.loads(Path(args.queue).read_text(encoding="utf-8"))
    rows = [
        row
        for row in queue.get("repos", [])
        if (row.get("confidence") or 0.0) >= args.min_confidence
    ]
    rows.sort(key=lambda row: -(row.get("stars") or 0))
    print(f"{len(queue.get('repos', []))} in the queue, {len(rows)} above the confidence floor")
    rows = rows[: args.take]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=16) as pool:
            readmes = list(pool.map(lambda row: fetch_readme(client, row["full_name"]), rows))
    print(f"{sum(1 for text in readmes if text)}/{len(rows)} readmes fetched")

    for row, readme in zip(rows, readmes, strict=True):
        row["readme"] = readme

    packets = []
    for index in range(0, len(rows), args.per_agent):
        chunk = rows[index : index + args.per_agent]
        path = out / f"judge-{index // args.per_agent + 1:02d}.json"
        path.write_text(
            json.dumps({"repos": chunk}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        packets.append({"packet": path.name, "repos": len(chunk)})

    # The agent picks a category from this file rather than from a list pasted
    # into its prompt, so a taxonomy change cannot leave the prompt behind.
    (out / "CATEGORIES.txt").write_text("\n".join(CATEGORIES), encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps({"total": len(rows), "packets": packets}, indent=1), encoding="utf-8"
    )
    print(f"{len(rows)} repos, {len(packets)} packets in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
