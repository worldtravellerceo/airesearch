"""The gates on a round of hand judging.

A verdict is the one record here that is never revisited: `reviewed_names`
reads the verdict files to keep the review queue from offering a repository
twice, so a wrong row is not corrected later — it is simply never looked at
again. Every way a round can produce one is silent, which is why each of these
is a test rather than a code review.

What it cost: the hidden-category rule shipped on a 160-repo sample at 95%
precision and moved 1,393 repositories on the population, `qdrant/qdrant`
among them. The lesson generalises past the classifier — a judging round that
validates row by row can still be wrong in aggregate, so the share gate is
tested here alongside the per-row ones.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_verdicts.py"


def write(path: Path, repos: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"repos": repos}, ensure_ascii=False), encoding="utf-8")


def packet(names: list[str]) -> list[dict]:
    return [{"full_name": name, "inputs_hash": f"hash-{name}"} for name in names]


def judged(
    name: str, *, is_ai: bool = True, category: str | None = "llm-app", hash_: str | None = None
) -> dict:
    return {
        "full_name": name,
        "inputs_hash": hash_ or f"hash-{name}",
        "is_ai": is_ai,
        "category": category if is_ai else None,
        "confidence": 0.92,
    }


def run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--packets",
            str(tmp_path / "packets"),
            "--pending",
            str(tmp_path / "pending"),
            "--out",
            str(tmp_path / "verdicts" / "2026-10-05-review.json"),
            "--verdicts",
            str(tmp_path / "verdicts"),
        ],
        capture_output=True,
        text=True,
    )


def mixed(names: list[str]) -> list[dict]:
    """Half AI, so a round passes the share gate on its own merits."""
    return [judged(name, is_ai=index % 2 == 0) for index, name in enumerate(names)]


def test_a_clean_round_is_written(tmp_path: Path) -> None:
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "pending" / "judge-01.json", mixed(names))

    result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    out = json.loads((tmp_path / "verdicts" / "2026-10-05-review.json").read_text())
    assert len(out["repos"]) == 10
    assert out["repos"] == sorted(out["repos"], key=lambda row: row["full_name"])


def test_an_omitted_repo_does_not_fail_the_round(tmp_path: Path) -> None:
    """Leaving a repo out is the honest outcome, not an error.

    It returns to the queue and is offered again. Failing the round for it
    would push a reader towards labelling everything, which is the one failure
    that cannot be undone.
    """
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "pending" / "judge-01.json", mixed(names[:6]))

    result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "omitted   4" in result.stdout
    assert (
        len(json.loads((tmp_path / "verdicts" / "2026-10-05-review.json").read_text())["repos"])
        == 6
    )


def test_a_stale_hash_is_refused(tmp_path: Path) -> None:
    """A hash that does not match its packet would import as nothing.

    `import_verdicts` skips a row whose inputs have moved, correctly — the
    judgement was about a different description. The row would look written
    and do nothing, so it is caught here instead of being committed.
    """
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    rows = mixed(names)
    rows[3]["inputs_hash"] = "hash-from-last-week"
    write(tmp_path / "pending" / "judge-01.json", rows)

    result = run(tmp_path)
    assert result.returncode == 1
    assert "inputs_hash does not match" in result.stdout
    assert not (tmp_path / "verdicts" / "2026-10-05-review.json").exists()


def test_a_repo_that_was_in_no_packet_is_refused(tmp_path: Path) -> None:
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "pending" / "judge-01.json", mixed(names) + [judged("owner/invented")])

    result = run(tmp_path)
    assert result.returncode == 1
    assert "was in no packet" in result.stdout


def test_a_repo_judged_in_an_earlier_round_is_refused(tmp_path: Path) -> None:
    """The queue should never have offered it, so something is wrong upstream.

    Overwriting silently would hide that: the second judgement wins and the
    disagreement with the first is never seen.
    """
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "verdicts" / "2026-09-20-tracked-band.json", [judged("owner/repo3")])
    write(tmp_path / "pending" / "judge-01.json", mixed(names))

    result = run(tmp_path)
    assert result.returncode == 1
    assert "already judged in 2026-09-20-tracked-band.json" in result.stdout


def test_a_category_outside_the_taxonomy_is_refused(tmp_path: Path) -> None:
    """`import_verdicts` would quietly substitute the default category.

    That turns a specific wrong answer into a plausible one, on a row nobody
    will look at again.
    """
    names = [f"owner/repo{index}" for index in range(10)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    rows = mixed(names)
    rows[0]["category"] = "ai-agents"  # plausible, and not in the taxonomy
    write(tmp_path / "pending" / "judge-01.json", rows)

    result = run(tmp_path)
    assert result.returncode == 1
    assert "is not in the taxonomy" in result.stdout


def test_a_round_that_says_yes_to_everything_is_refused(tmp_path: Path) -> None:
    """The gate no per-row check can make.

    A model with no way to say no says yes, and so does an agent that has
    stopped reading READMEs and started matching the word "AI". Every row
    validates; the round is still worthless. The only read round there is came
    out 391 of 661 AI — 59.2% — so a round near 100% is not a slice that
    happened to be clean.
    """
    names = [f"owner/repo{index}" for index in range(20)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "pending" / "judge-01.json", [judged(name) for name in names])

    result = run(tmp_path)
    assert result.returncode == 1
    assert "stopped distinguishing" in result.stdout
    assert not (tmp_path / "verdicts" / "2026-10-05-review.json").exists()


def test_a_round_that_says_no_to_everything_is_refused(tmp_path: Path) -> None:
    """The queue is drawn from repos the engine found signal in.

    An all-no round means the reading went wrong, not that the slice was
    clean — the band is where the AI the engine could not name actually is.
    """
    names = [f"owner/repo{index}" for index in range(20)]
    write(tmp_path / "packets" / "judge-01.json", packet(names))
    write(tmp_path / "pending" / "judge-01.json", [judged(name, is_ai=False) for name in names])

    result = run(tmp_path)
    assert result.returncode == 1
    assert "stopped distinguishing" in result.stdout
