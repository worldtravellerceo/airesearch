"""Check written summaries against the packets they were written from.

Three failures matter here and none of them announce themselves in the text:
a repository whose paragraphs went missing, a repository whose paragraphs were
written under somebody else's name, and a run where the model stopped admitting
that most projects do not fit the reader's work.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Above this share of records claiming a match, the honesty rule has stopped
# working: most repositories genuinely do not fit, so a high number means the
# writer is reaching, not that the index got more relevant.
MATCH_SHARE_CEILING = 0.80
MIN_PARAGRAPH_CHARS = 120


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packets", required=True)
    parser.add_argument("--summaries", required=True)
    parser.add_argument("--group", default=None, help="Only check one subject group")
    args = parser.parse_args()

    packets = Path(args.packets)
    summaries = Path(args.summaries)

    expected: dict[str, str] = {}
    for path in sorted(packets.glob("*-*.json")):
        if args.group and not path.name.startswith(f"{args.group}-"):
            continue
        for row in json.loads(path.read_text(encoding="utf-8"))["repos"]:
            expected[row["full_name"]] = row["inputs_hash"]

    seen: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(summaries.glob("*-*.json")):
        if args.group and not path.name.startswith(f"{args.group}-"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}: not valid JSON ({exc})")
            continue
        for row in payload.get("repos", []):
            name = row.get("full_name")
            if name in seen:
                problems.append(f"{path.name}: {name} written twice")
                continue
            if name not in expected:
                problems.append(f"{path.name}: {name} was in no packet")
                continue
            if row.get("inputs_hash") != expected[name]:
                problems.append(f"{path.name}: {name} inputs_hash does not match its packet")
                continue
            for field in ("description_tr", "usage_tr"):
                text = (row.get(field) or "").strip()
                if len(text) < MIN_PARAGRAPH_CHARS:
                    problems.append(f"{path.name}: {name} {field} is {len(text)} chars")
            seen[name] = row

    missing = sorted(set(expected) - set(seen))
    matched = sum(1 for row in seen.values() if row.get("matched_project"))
    share = matched / len(seen) if seen else 0.0

    print(f"expected  {len(expected)}")
    print(f"written   {len(seen)}  ({100 * len(seen) / max(len(expected), 1):.1f}%)")
    print(f"matched   {matched}  ({100 * share:.1f}% claim a match)")
    print(f"no match  {len(seen) - matched}")
    if missing:
        print(f"\nmissing ({len(missing)}):")
        for name in missing[:40]:
            print(f"  {name}")
    if problems:
        print(f"\nproblems ({len(problems)}):")
        for line in problems[:40]:
            print(f"  {line}")

    failed = bool(problems) or bool(missing)
    if seen and share > MATCH_SHARE_CEILING:
        print(
            f"\nFAIL: {100 * share:.1f}% claim a match, ceiling is "
            f"{100 * MATCH_SHARE_CEILING:.0f}%. The writer has stopped admitting misses."
        )
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
