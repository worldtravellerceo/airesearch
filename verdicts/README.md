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

Each file is `{"repos": [{full_name, content_hash, is_ai, category,
confidence}]}`. `content_hash` is the hash of the inputs the judgement was
made from. If a repo's description, topics or language have changed since,
the import skips it rather than applying a verdict that was about a different
repository — a stale label is worse than no label, because no label gets
looked at again and a stale one does not.

Judging a repo is optional. A repo left out of these files stays in the
escalation band, which is the honest outcome when the evidence does not
support a call: `nikivdev/code` has no description and the topics
`agents, autonomy, moonbit`, and is not in here for that reason.

## The weekly review

The rule engine gives 53,475 repos above a thousand stars a confidence of
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

Biggest first, because that is the order in which a miss costs something. Repos
named in any file here are skipped, so the queue does not hand back the same
projects; `remaining_after_this_slice` says how much is left.

Judge each one, write the verdicts as a new file here, and the next
classification run picks them up. Measured against a hand-read sample of 100,
about 7% of this pile is AI — call it 3,700 repos across the whole queue, and
the yield is highest in the 2,500–10,000 star band rather than at the very top.

Leaving a repo out of the verdicts is a valid answer. It stays in the queue and
comes back, which is the right outcome when the description genuinely does not
support a call.
