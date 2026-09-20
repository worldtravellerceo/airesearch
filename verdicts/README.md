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
