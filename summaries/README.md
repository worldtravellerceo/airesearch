# Written summaries

The two Turkish paragraphs the site shows under each board row: one describing
the project, one saying where it fits into the reader's own work — or plainly
that it fits nowhere.

These are here for the same reason `verdicts/` is here. The database is a
release asset, not a tracked file, so anything written only into it is one lost
asset away from being gone. These files are replayed on every run
(`airadar summarise --import summaries/`), which makes the site's prose
reproducible from the repository alone.

There are two ways this text gets written and they produce the same rows:

- **The paid path**, `airadar summarise`, over the Batch API. Needs
  `ANTHROPIC_API_KEY`.
- **This one.** Written in a Claude Code session on a subscription and
  committed, which is what `classify_run.export_pending` already does for
  classification. It needs no key at all.

Each file is `{"repos": [{full_name, inputs_hash, description_tr, usage_tr,
matched_project, relevance, investment_note}]}`.

`inputs_hash` is the hash of the repository the paragraph was written about —
its name, description, topics and language, and deliberately **not** its
README. A README that has just been fetched is new evidence and should be
re-read by the paid path; it does not invalidate a paragraph somebody wrote by
reading it. If the description, topics or language have moved, the import skips
the record rather than applying it: a stale paragraph is worse than a missing
one, because a missing one gets written and a stale one is never looked at
again.

`matched_project` is nullable and most of these are null. Most projects do not
fit into the reader's work, and saying so is the correct answer — a field that
can only hold a match produces a match for a CUDA kernel library. The share of
records claiming a match is the number to watch: if it climbs towards 100%,
whatever wrote them has stopped reading.

A repository left out of these files simply has no paragraphs and falls back to
its GitHub description on the site, which is the honest outcome when there was
nothing to read.
