"""Build the work packets the summary agents read.

Needs no credentials and no database. Everything a paragraph is written from is
already public: the board files and per-repo detail JSON come off the published
site, and the README comes off raw.githubusercontent.com. That is the whole
reason this path exists — the paid Batch API route needs a key, and there is
none.

Writes one JSON packet per agent under `--out`, grouped by subject so each
workflow's slice stands on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airadar.summarise import SummaryInput  # noqa: E402

BASE = "https://worldtravellerceo.github.io/airesearch/data"
BOARDS = ("popular", "momentum", "breakout", "fresh")
README_CHARS = 6000

# Subject groups, one per workflow. Each one holds categories that sit near each
# other, so an agent builds context about a domain rather than ping-ponging.
GROUPS: dict[str, tuple[str, ...]] = {
    "agents": ("agent-framework", "mcp"),
    "llm-apps": ("llm-app",),
    "devtools": ("ai-devtools", "data-tooling", "prompt-eval-observability"),
    "perception": ("multimodal-vision", "audio-speech", "robotics-embodied"),
    "serving": ("rag-vectordb", "inference-serving", "model-weights", "training-finetuning"),
    "classic": ("classic-ml", "awesome-list"),
}


def get_json(client: httpx.Client, url: str):
    try:
        response = client.get(url, timeout=30)
        if response.status_code == 200:
            return response.json()
    except Exception:
        return None
    return None


def fetch_readme(client: httpx.Client, full_name: str) -> str:
    """The README off the repository's *default* branch.

    `HEAD` rather than a guess between `main` and `master`. Trying `main` first
    and taking the first 200 looks equivalent and is not: a repository that
    kept `master` as its default can still have a stale `main` sitting around,
    and the fetch silently prefers it. `qdrant/qdrant` is exactly that —
    `main/README.md` is a 2,072-byte React/Vite interview template, while the
    real 11,560-byte README is on `master`. Measured across the 1,324
    repositories on the boards, the branch guess fetched substantially wrong
    text for 25 of them, and nothing about the result looked wrong.

    A repository with no README goes forward with an empty string rather than
    being dropped: the agent can still write the first paragraph from the
    description, and is told to say so instead of inventing the rest.
    """
    for name in ("README.md", "readme.md", "README.rst", "README"):
        try:
            response = client.get(
                f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}",
                timeout=30,
            )
        except Exception:
            continue
        if response.status_code == 200 and response.text.strip():
            return response.text[:README_CHARS]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-agent", type=int, default=19)
    parser.add_argument(
        "--only",
        help="A file of full_names, one per line. Build packets for just these — "
        "the top-up path, for repos that entered the boards after the last sweep "
        "or whose metadata moved since their paragraph was written.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True) as client:
        categories = get_json(client, f"{BASE}/categories.json") or {}
        slugs = ["_all"] + [row["category"] for row in categories.get("categories", [])]

        names: dict[str, dict] = {}
        for board in BOARDS:
            for slug in slugs:
                payload = get_json(client, f"{BASE}/boards/{board}/{slug}.json")
                for entry in (payload or {}).get("entries", []):
                    names.setdefault(entry["full_name"], entry)
        print(f"{len(names)} unique repos across every published board file", flush=True)

        if args.only:
            wanted = {
                line.strip()
                for line in Path(args.only).read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            absent = wanted - set(names)
            names = {k: v for k, v in names.items() if k in wanted}
            print(f"--only: {len(names)} of {len(wanted)} requested repos are on a board")
            for missing in sorted(absent):
                print(f"  not on any board any more, skipped: {missing}")

        ordered = sorted(names, key=lambda n: -(names[n].get("stars") or 0))

        def detail(full_name: str):
            owner, _, name = full_name.partition("/")
            return full_name, get_json(client, f"{BASE}/repos/{owner}/{name}.json")

        with ThreadPoolExecutor(max_workers=16) as pool:
            details = dict(pool.map(detail, ordered))
        print(f"{sum(1 for v in details.values() if v)} detail files fetched", flush=True)

        with ThreadPoolExecutor(max_workers=16) as pool:
            fetched = pool.map(lambda n: fetch_readme(client, n), ordered)
            readmes = dict(zip(ordered, fetched, strict=True))
        print(f"{sum(1 for v in readmes.values() if v)} readmes fetched", flush=True)

    by_group: dict[str, list[dict]] = {key: [] for key in GROUPS}
    by_group["classic"] = by_group.get("classic", [])
    group_of = {cat: key for key, cats in GROUPS.items() for cat in cats}

    for full_name in ordered:
        row = details.get(full_name) or {}
        board_row = names[full_name]
        topics = tuple(row.get("topics") or [])
        item = SummaryInput(
            repo_id=0,
            full_name=full_name,
            description=row.get("description") or board_row.get("description"),
            topics=topics,
            language=row.get("language") or board_row.get("language"),
            license=row.get("license"),
            stars=row.get("stars") or board_row.get("stars") or 0,
            readme_excerpt=readmes.get(full_name, ""),
        )
        category = row.get("category") or board_row.get("category") or "classic-ml"
        by_group.setdefault(group_of.get(category, "classic"), []).append(
            {
                "full_name": full_name,
                "stars": item.stars,
                "language": item.language,
                "license": item.license,
                "category": category,
                "topics": list(topics),
                "created_at": row.get("created_at"),
                "homepage": row.get("homepage"),
                "description": item.description,
                "readme": item.readme_excerpt,
                # What the import validates against. A repo whose description,
                # topics or language move has a paragraph written about a
                # different project, and the import skips it.
                "inputs_hash": item.inputs_hash(),
            }
        )

    # A top-up is too small to split by subject — six groups of four would be
    # six agents doing a quarter of an agent's work each. It is flattened into
    # one sequence instead, and numbered independently of the groups: numbering
    # per group and naming every file `topup-NN` had each group overwrite the
    # last, which left 6 of 40 repositories in a single file.
    groups = (
        {"topup": [row for rows in by_group.values() for row in rows]} if args.only else by_group
    )

    manifest = []
    for group, rows in groups.items():
        if not rows:
            continue
        for index in range(0, len(rows), args.per_agent):
            chunk = rows[index : index + args.per_agent]
            number = index // args.per_agent + 1
            path = out / f"{group}-{number:02d}.json"
            path.write_text(
                json.dumps({"group": group, "repos": chunk}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            manifest.append({"group": group, "packet": path.name, "repos": len(chunk)})

    (out / "manifest.json").write_text(
        json.dumps({"total": len(ordered), "packets": manifest}, indent=1), encoding="utf-8"
    )
    for group in sorted(by_group):
        packets = [m for m in manifest if m["group"] == group]
        print(f"{group:12s} {sum(m['repos'] for m in packets):4d} repos  {len(packets):2d} packets")
    print(f"total {len(ordered)} repos, {len(manifest)} packets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
