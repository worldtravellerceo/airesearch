"""Merge a round of judging into one dated verdict file, and refuse bad rows.

A verdict is permanent in a way no other record here is. `reviewed_names` reads
these files to keep the queue from handing back what has already been read, so a
repository that lands in one is never offered again: a wrong label is not a
label that gets corrected later, it is a label nobody looks at twice. The three
ways a round can produce one are all silent, so all three are checked here.

A name that was in no packet means the row was invented rather than read. A
hash that does not match its packet means the judgement is about a description
that has since moved, and `import_verdicts` would skip it — the row would look
written and do nothing. A name already in an earlier verdict file means this
round re-judged something that was settled, which the queue should have
prevented.

The fourth check is the one that cannot be made per row. A model with no way to
say "no" says yes; so does an agent that stops reading and starts pattern
matching on the word "AI". The 661-repo round of 2026-09-20 came out 59.2% AI,
and that is what a read slice looks like. A round that comes back near 100% has
stopped judging, and is rejected whether or not every row validates.

Omitting a repository is not a failure and never fails this script: it returns
to the queue and comes back. It is reported, because a round that omits most of
its slice means the packets were not readable, which is a different problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airadar.classify.taxonomy import CATEGORIES  # noqa: E402

#: Above this share claiming AI, the round has stopped distinguishing. Anchored
#: on the only read round there is: 391 of 661 repos, 59.2%, on a slice drawn
#: from the same queue. The ceiling sits well clear of that so a genuinely
#: AI-heavy slice passes, and well below the 100% that a model saying yes to
#: everything produces.
AI_SHARE_CEILING = 0.85
#: And below this, it has stopped finding the AI that is there. The queue is
#: drawn from repositories the engine found *some* signal in, so an all-no round
#: means the reading went wrong, not that the slice was clean.
AI_SHARE_FLOOR = 0.15


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packets", required=True, help="Directory the agents read")
    parser.add_argument("--pending", required=True, help="Directory the agents wrote")
    parser.add_argument("--out", required=True, help="The dated verdict file to write")
    parser.add_argument(
        "--verdicts",
        default="verdicts",
        help="Past verdict files, checked for repos judged twice",
    )
    args = parser.parse_args()

    expected: dict[str, str] = {}
    readmes: dict[str, str] = {}
    for path in sorted(Path(args.packets).glob("judge-*.json")):
        for row in json.loads(path.read_text(encoding="utf-8"))["repos"]:
            expected[row["full_name"]] = row["inputs_hash"]
            readmes[row["full_name"]] = hashlib.sha256(
                (row.get("readme") or "").encode("utf-8")
            ).hexdigest()[:16]
    print(f"packets   {len(expected)} repos")

    out_path = Path(args.out)
    already: dict[str, str] = {}
    for path in sorted(Path(args.verdicts).glob("*.json")):
        if path.resolve() == out_path.resolve():
            continue
        for row in json.loads(path.read_text(encoding="utf-8")).get("repos", []):
            already[row["full_name"]] = path.name

    merged: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(Path(args.pending).glob("judge-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}: not valid JSON ({exc})")
            continue
        for row in payload.get("repos", []):
            name = row.get("full_name")
            if name not in expected:
                problems.append(f"{path.name}: {name} was in no packet")
                continue
            if row.get("inputs_hash") != expected[name]:
                problems.append(f"{path.name}: {name} inputs_hash does not match its packet")
                continue
            if name in merged:
                problems.append(f"{path.name}: {name} judged twice in this round")
                continue
            if name in already:
                problems.append(f"{path.name}: {name} was already judged in {already[name]}")
                continue
            is_ai = bool(row.get("is_ai"))
            category = row.get("category")
            if is_ai and category not in CATEGORIES:
                problems.append(f"{path.name}: {name} category {category!r} is not in the taxonomy")
                continue
            if not is_ai and category:
                problems.append(f"{path.name}: {name} is not AI but carries category {category!r}")
                continue
            merged[name] = {
                "full_name": name,
                "is_ai": is_ai,
                "category": category if is_ai else None,
                "confidence": float(row.get("confidence", 0.92)),
                "inputs_hash": expected[name],
                # The README this judgement was read from. Nothing uses it yet,
                # and that is the point: `inputs_hash` deliberately excludes the
                # README, so a verdict never expires when a project repositions
                # itself in its own README. `coder/coder` was judged not-AI in
                # September and AI in October — by two readings of a README
                # that had changed in between, not by a reading error — and
                # there is no way to tell how often that happens, because the
                # text each judgement was made from was never recorded.
                #
                # Re-offering a repo whenever its README moves is the obvious
                # fix and is probably wrong: excerpts churn on badge counts, so
                # it could re-offer most of the 661 existing verdicts forever,
                # which is buying recall with precision. Recording the hash
                # costs 16 bytes a row and turns that into a number a later
                # round can read off.
                "readme_sha": readmes[name],
            }

    ai = sum(1 for row in merged.values() if row["is_ai"])
    share = ai / len(merged) if merged else 0.0
    omitted = sorted(set(expected) - set(merged))

    print(
        f"judged    {len(merged)}  ({100 * len(merged) / max(len(expected), 1):.1f}% of the slice)"
    )
    print(f"is AI     {ai}  ({100 * share:.1f}%)")
    print(f"not AI    {len(merged) - ai}")
    print(f"omitted   {len(omitted)}  (back to the queue)")
    if merged:
        for name, count in Counter(
            row["category"] for row in merged.values() if row["is_ai"]
        ).most_common():
            print(f"  {count:4d}  {name}")
    if omitted:
        print("\nomitted, will be offered again:")
        for name in omitted[:40]:
            print(f"  {name}")

    failed = False
    if problems:
        print(f"\nproblems ({len(problems)}):")
        for line in problems[:40]:
            print(f"  {line}")
        failed = True
    if merged and not AI_SHARE_FLOOR <= share <= AI_SHARE_CEILING:
        print(
            f"\nFAIL: {100 * share:.1f}% judged AI, outside the "
            f"{100 * AI_SHARE_FLOOR:.0f}-{100 * AI_SHARE_CEILING:.0f}% band. "
            "The round has stopped distinguishing; nothing written."
        )
        failed = True
    if not merged:
        print("\nFAIL: nothing to merge.")
        failed = True
    if failed:
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"repos": sorted(merged.values(), key=lambda row: row["full_name"])},
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {len(merged)} verdicts to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
