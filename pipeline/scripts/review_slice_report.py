"""Say what a review slice holds, for the workflow summary.

Its own file rather than a heredoc inside the workflow: a `python - <<'PY'`
nested inside the `{ ... } >> $GITHUB_STEP_SUMMARY` block has to end its
terminator at column zero, which is invisible in a YAML block scalar and fails
at run time rather than at parse time.

Reports "not measured" rather than zero for anything it could not read. A
summary that prints 0 for a file that is missing is a summary that says the
slice was empty when the truth is that the step before it failed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--packets", required=True)
    args = parser.parse_args()

    lines = ["### İnceleme dilimi", "", "```"]

    queue = load(Path(args.queue))
    if queue is None:
        lines.append("review-queue.json okunamadı — kuyruk çıkarılamadı.")
    else:
        rows = queue.get("repos", [])
        unsettled = sum(1 for row in rows if row.get("basis") == "unsettled")
        remaining = queue.get("remaining_after_this_slice")
        lines.append(
            f"kuyrukta {len(rows):,} repo: {unsettled:,} belirsiz, "
            f"{len(rows) - unsettled:,} sinyalsiz"
        )
        lines.append(
            f"bu dilimden sonra kalan: {remaining:,}"
            if isinstance(remaining, int)
            else "bu dilimden sonra kalan: ölçülmedi"
        )

    manifest = load(Path(args.packets) / "manifest.json")
    if manifest is None:
        lines.append("paket üretilmedi — manifest.json yok.")
    else:
        lines.append(f"okunmaya hazır: {manifest['total']} repo, {len(manifest['packets'])} paket")
        # The backlog in weeks, every run. Without it the queue is a number
        # that barely moves and nobody has to look at what that means: at 200
        # a week, 53,000 candidates is more than five years of Mondays. That
        # figure is the whole argument for the paid path, and it belongs where
        # it is read rather than in a design document.
        remaining = (queue or {}).get("remaining_after_this_slice")
        per_round = manifest["total"]
        if isinstance(remaining, int) and per_round:
            weeks = -(-remaining // per_round)
            lines.append(
                f"bu hızda kuyruğun bitmesi: {weeks:,} hafta "
                f"(~{weeks / 52:.1f} yıl), haftada {per_round} repo"
            )

    lines += [
        "```",
        "",
        "Paketler `review-packets` artefaktında. Okuma yolu için workflow'un",
        "başındaki `judge` notuna bak.",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
