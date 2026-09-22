"""Collapse `matched_project` to the reader profile's own names.

The label is rendered on the board, next to the second paragraph, so it is the
one field where wording is not a private detail. Seventy-one agents wrote these
independently and produced 34 spellings of about sixteen things — "Claude
Code", "Claude Code iş akışı", "Claude Code orkestrasyonu" and "Claude Code
kurulumu" are one label; so are four different arrangements of the assistant
and holding tooling; so are "Kişisel bilgi tabanı" and "İkinci beyin", which
are the two halves of that section's own heading.

The canonical names come from the profile's section 4 and 5 headings, plus
"Claude Code" for the tooling category that is priority 2 in section 7.3 and
has no section-4 project of its own. Anything not in the table is left alone
and reported, so a name nobody anticipated shows up rather than being silently
folded into its nearest neighbour.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter
from pathlib import Path

CANONICAL: dict[str, str] = {
    # Section 4.3: "Kişisel bilgi tabanı / 'ikinci beyin'" — one heading.
    "ikinci beyin": "Kişisel bilgi tabanı",
    # Section 7.3 priority 2: coding agents, plugins, local model plumbing.
    "claude code iş akışı": "Claude Code",
    "claude code orkestrasyonu": "Claude Code",
    "claude code kurulumu": "Claude Code",
    "claude code / ajan altyapısı": "Claude Code",
    "kod ajanları / yerel model altyapısı": "Claude Code",
    "opencode ios istemcisi": "Claude Code",
    # Section 4.5, four ways of arranging the same three words.
    "asistan araçları": "Asistan ve holding araçları",
    "asistan operasyon araçları": "Asistan ve holding araçları",
    "asistan operasyonları": "Asistan ve holding araçları",
    "asistan ve holding operasyon araçları": "Asistan ve holding araçları",
    "asistan / holding operasyon araçları": "Asistan ve holding araçları",
    "asistan / holding operasyonu": "Asistan ve holding araçları",
    "asistan/orkestratör ajan": "Asistan ve holding araçları",
    "orkestratör ajan": "Asistan ve holding araçları",
    # Section 4.4.
    "hayat orkestrasyon": "Hayat orkestrasyonu",
    # Section 4.9.
    "dubai ai uygulama stüdyosu": "Dubai uygulama stüdyosu",
    "dubai app stüdyosu": "Dubai uygulama stüdyosu",
    # Section 4.8.
    "neurotech tezi": "Neuroteknoloji tezi",
    # Section 4.7.
    "güneş enerjisi yatırımı": "Güneş enerjisi",
    # Section 5.
    "stand-up komedi analizi": "Stand-up analizi",
    "öğrenme (hobi)": "Öğrenme",
}

# Names that are already canonical. Kept explicit so an unexpected label is
# reported rather than assumed correct.
KNOWN = {
    "Lyricdrop",
    "Folio",
    "Kişisel bilgi tabanı",
    "Arslan Holding",
    "Claude Code",
    "Asistan ve holding araçları",
    "Hayat orkestrasyonu",
    "Kanarya",
    "Model ajansı",
    "Dubai uygulama stüdyosu",
    "Neuroteknoloji tezi",
    "Stand-up analizi",
    "Güneş enerjisi",
    "Öğrenme",
    "Ev teknolojisi",
}


_TABLE: dict[str, str] = {}


def _key(label: str) -> str:
    """Lower-case a Turkish label so it can be looked up.

    `"İkinci beyin".lower()` is not `"ikinci beyin"`. Python lowers the dotted
    capital to `i` followed by U+0307 COMBINING DOT ABOVE, so a plain ASCII key
    never matches and the label survives the table untouched. The first run of
    this script folded 33 of 34 labels and left that one behind, which is the
    only reason the mismatch was visible at all — the report lists anything it
    could not place instead of assuming the table was complete.

    Normalising to NFD and dropping combining marks makes the lookup
    insensitive to that, and to the same problem in `ş`, `ğ` and `ı`.
    """
    decomposed = unicodedata.normalize("NFD", label.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_TABLE.update({_key(k): v for k, v in CANONICAL.items()})


def canonical(label: str) -> str:
    return _TABLE.get(_key(label), label.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summaries", required=True)
    parser.add_argument("--apply", action="store_true", help="Write the files back")
    args = parser.parse_args()

    before, after, unknown = Counter(), Counter(), Counter()
    changed_files = 0
    for path in sorted(Path(args.summaries).glob("*-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        touched = False
        for row in payload["repos"]:
            label = row.get("matched_project")
            if not label:
                continue
            before[label] += 1
            fixed = canonical(label)
            after[fixed] += 1
            if fixed not in KNOWN:
                unknown[fixed] += 1
            if fixed != label:
                row["matched_project"] = fixed
                touched = True
        if touched and args.apply:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            changed_files += 1

    print(f"{len(before)} labels -> {len(after)}")
    for name, count in after.most_common():
        print(f"  {count:4d}  {name}")
    if unknown:
        print("\nnot in the canonical list — check these by hand:")
        for name, count in unknown.most_common():
            print(f"  {count:4d}  {name}")
    print(f"\n{'rewrote' if args.apply else 'would rewrite'} {changed_files} files")
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
