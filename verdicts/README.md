# Reviewed verdicts

Repos the rule engine could not settle, judged by hand and checked in here so
the judgement survives the database.

These live outside `data/`, which is gitignored precisely because everything
in it is generated. A verdict is not generated — it was decided once, by
reading the repo — so it belongs in source control next to the rules it
supplements.

The database itself is a release asset, not a tracked file. Anything written
only into it is one lost asset away from being gone, and re-deciding 600
borderline repos by hand is not a thing anyone should have to do twice. These
files are replayed on every classification run, so the verdicts are
reproducible from the repository alone.

Each file is `{"repos": [{full_name, inputs_hash, is_ai, category,
confidence, readme_sha}]}`. `inputs_hash` is the hash of the inputs the
judgement was made from — full name, description, topics and language, and
deliberately not the README. If a repo's description, topics or language have changed since,
the import skips it rather than applying a verdict that was about a different
repository — a stale label is worse than no label, because no label gets
looked at again and a stale one does not.

Judging a repo is optional. A repo left out of these files stays in the
escalation band, which is the honest outcome when the evidence does not
support a call: `nikivdev/code` has no description and the topics
`agents, autonomy, moonbit`, and is not in here for that reason.

## The weekly review

The rule engine gives 52,674 repos above a thousand stars a confidence of
exactly zero — it found no signal at all — and records that as "not AI". Zero
is the one reading the score does not support: it means no evidence was found,
not that evidence of absence was. `anomalyco/opencode` sat in that pile at
208,847 stars, described as "The open source coding agent."

Teaching the engine new words fixes the words we already know are missing. It
cannot fix the ones that do not exist yet — the 2026 agent vocabulary was
invisible until someone read the repositories. So a slice gets read every week.

```
airadar review-queue --limit 1200 --out review-queue.json
```

Biggest first, because that is the order in which a miss costs something.
Repos named in any file here are skipped, so the queue does not hand back the
same projects; `remaining_after_this_slice` says how much is left.

Leaving a repo out of the verdicts is a valid answer. It stays in the queue and
comes back, which is the right outcome when the description genuinely does not
support a call. In the 2026-10-05 round 32 of 200 were left out and the readers
said why for each: `SigNoz/signoz`, whose description is saturated with agents
and whose README's first 4,000 characters are a Datadog alternative, is the
shape of a genuine undecidable.

## How a round runs

`.github/workflows/review.yml`, Monday mornings, and three scripts under
`pipeline/scripts/`:

```
airadar review-queue --limit 1200 --out review-queue.json --verdicts verdicts
python pipeline/scripts/build_review_packets.py --queue review-queue.json \
    --out review-packets --take 200 --min-confidence 0.5
#   ... the reading happens, into verdicts/pending/judge-NN.json ...
python pipeline/scripts/merge_verdicts.py --packets review-packets \
    --pending verdicts/pending --out verdicts/$(date +%F)-review.json
```

The packets add the one thing the queue cannot: the README. The queue's
`matched` field says *why* the engine hesitated, not what the project is — a
`readme-late:llm` is a model named once, far from the top, which is usually a
passing mention. 28 of the 46 repositories judged AI on 2026-10-05 were
hesitations of exactly that kind, and `state-spaces/mamba` at 18,885 stars was
one of them.

Draw the slice through `review-queue`, never from the database directly. The
exclusion of already-judged repos lives in the queue builder, and the
2026-10-05 round was prepared by hand without it: 21 of its 200 repositories
had been judged in September and 17 were read a second time. `merge_verdicts`
refused all 17, which is the only reason it was noticed.

That accident bought a measurement worth keeping. The two rounds agreed on
`is_ai` for 16 of the 17 and on the category for all 5 they both called AI —
94% agreement between independent readings seven weeks apart. The one
disagreement is not a reading error: `coder/coder` was judged not-AI in
September and AI in October because Coder shipped a native agent loop in
between and rewrote its README around it. The October verdict supersedes the
September one, which is why the file names carry dates.

That case is also the open question. `inputs_hash` excludes the README on
purpose — a verdict should not expire because a vocabulary list grew — but it
means a project that repositions itself in its own README keeps its old
verdict forever. Re-offering a repo whenever its README moves is the obvious
fix and is probably wrong: excerpts churn on badge counts, so it could re-offer
most of the existing verdicts permanently, which is buying recall with
precision. Nothing is changed for now. `merge_verdicts` records `readme_sha`
per row instead, so a later round can read the real number off rather than
guess at it.

## What a round may not do

`merge_verdicts.py` refuses the whole file, not the bad row, when any of these
holds. A verdict is never looked at twice, so a wrong one is permanent.

- A repo that was in no packet — the row was not read, it was invented.
- A hash that does not match its packet — the judgement is about a description
  that has since moved, and the import would skip the row while it looked
  written.
- A repo already judged in an earlier file — the queue should have prevented it.
- A category outside the taxonomy — the import would silently substitute the
  default, turning a specific wrong answer into a plausible one.
- A round outside 15–85% AI. This is the gate no per-row check can make: every
  row can validate while the round as a whole has stopped distinguishing. The
  2026-09-20 round came out 59.2% AI and the 2026-10-05 round 27.4%, both from
  readings of the same queue — so the band is wide, and 100% is not a slice
  that happened to be clean.

## The paid path

`airadar review-judge` reads the same slice with the Batch API instead of by
hand, writes the same `verdicts/pending/` shape, and goes through the same
`merge_verdicts` gates. It exists as a separate command because `classify`
with the LLM pass on does *not* do this job: that sends the rule engine's
escalation band, 1,938 repos, and never the 52,674 that scored zero —
which are most of the queue and where `anomalyco/opencode` sat at 208,847
stars.

Estimated at $0.00033 a repository with batching and the current model, a
200-repo slice is $0.07 and the whole 53,392-repo queue is about $17.40.
Against that, reading 200 a week by hand is 267 weeks. The workflow runs
without the key and says so rather than falling back to the rule engine: a
rules verdict committed here would be a permanent record of the engine
agreeing with itself, which is how `anomalyco/opencode` stayed hidden in the
first place.
