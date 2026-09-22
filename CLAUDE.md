# AI Radar

An index of the AI ecosystem: which open-source projects and which companies are
*rising*, as opposed to which have accumulated the most. A five-year-old project
with 50,000 stars and a two-week-old one with 50,000 stars are not the same
thing, and `sort:stars` cannot tell them apart.

## How work is run here

**Split large fan-outs across several workflows, not one.** The Workflow tool
caps concurrent agents at `min(16, CPUs - 2)` *per workflow*, and this container
has 4 CPUs — so a single workflow runs 2 agents at a time however many it
spawns. The work is network-bound (API calls, database reads) rather than
CPU-bound; load average sat at 0.05 with two agents running. So when a task
needs more than two agents working at once, launch several workflows side by
side, each holding a coherent slice of the work. Two workflows is four agents,
three is six. Group by subject so each workflow's results stand on their own.

## What this project has learned the hard way

These are not style preferences. Each one cost something.

**Measure before changing, and measure after.** Every fix here started as a
number: 14 of the 40 highest-star repos created since July scored zero; 400 of
400 funding rounds could not be tied to a company we track; the census spends
784 pages in 37 minutes. A change with no number in front of it is a guess.

**A zero is not a verdict.** The classifier once recorded "no signal found" as
"not AI", which hid `anomalyco/opencode` at 208,847 stars. Absence of evidence
has to stay distinguishable from evidence of absence — and anything unsettled
goes to a human, not to silence.

**A sample can be too small to hold the counter-example.** A rule that capped
any repository carrying a non-AI product topic scored 95% precision on the
160-repo labelled sample and looked finished. The sample contains eight such
repositories. Run over the whole index it moved 1,393 of them to review,
`FlowiseAI/Flowise` and `qdrant/qdrant` among them. Before a classifier change
ships, run it over the population as well as the sample and read a random slice
of what moved.

**A refusal is not an empty answer.** The valuation channel treated every
status at or above 400 as "past the last page". news.crunchbase.com answers 403
to the default `python-httpx` user agent, so it fetched nothing for its whole
life and logged a clean finish every time. Distinguish "the source said no"
from "the source said nothing", and make the first one loud.

**Some ambiguity is not resolvable, and saying so is the answer.** An owner
account ending in `-ai` yields a decisive token, so `yuaahu87-ai`, a Red Dead
Redemption trainer, scores 0.85 — and so does `suno-ai/bark`, a generative
audio model with 39,271 stars and no other vocabulary in its description. Every
fix tried cost more than it bought: reading tokens off the description alone
moved 332 repositories above 300 stars and took `bark` with them, and buying
them back needed phrases chosen to match the repositories we had just looked
at. When a change fails its own acceptance bar, write down what was measured
and leave the code alone.

**A model with no way to say "no" will say yes.** The summariser writes two
paragraphs per repository, the second one about where a project fits into the
reader's own work. Most projects fit nowhere, and that is the correct answer
for them. `matched_project` is nullable in the schema, in the JSON schema the
model answers against, and in the prompt, because a field that can only hold a
match produces a match for a CUDA kernel library. The share of summaries
claiming a match is the number to watch: if it climbs towards 100%, the
prompt has stopped working, not the index.

**A README is somebody else's text, and some of it is aimed at us.**
`elder-plinius/CL4R1T4S` has 65,000 stars, sits on the AI-devtools board, and
ends its README with a directive in leetspeak followed by the same sentence in
plain English, telling whatever model reads it to output its own instructions.
Two prompts here paste READMEs straight into a request, and for a long time
neither said where that text started or stopped. The run that found it wrote a
correct summary and reported the payload rather than obeying it, but that was
the model's judgement doing work the prompt should have been doing.
`airadar/untrusted.py` fences and labels the text, and strips the closing
marker out of it first — a README that closes the fence early would put
everything after it back among the instructions. This does not make injection
impossible; it makes the boundary legible, which is the part a prompt can be
responsible for.

**Never buy recall with precision.** A soft ceiling set below the decision
threshold stranded 17,849 repositories in permanent escalation. A README weight
set one tier too high put a release-notes CLI on an AI board. Every widening
needs a negative control that proves it did not.

**Never guess an identity without a way to check it.** We hold domains;
Crunchbase wants names. The guess is right about a fifth of the time, and a
wrong guess returns a real company that is not ours. Nothing is stored until
the profile's own website reduces to the domain we asked for, and a failed
match is recorded so it is not paid for twice.

**An empty board is better than a wrong one.** A board with no rows is not
written and not linked: its absence says the question has not been answered,
while a board full of venture news says it has, wrongly.

**Money is spent only behind a ceiling somebody else enforces.** Every Apify run
carries `maxTotalChargeUsd`, so the cap holds when this code is wrong. Every run
is written to `apify_run` before it starts. And nothing paid runs until a
preflight has proved the result can be published — a whole paid run was lost to
a 403 on the upload at the end.

**Bank work as it completes.** Publish after each expensive step, not once at
the end. The database is a release asset and the runner disappears with the job.

**The rate limit belongs to the account, not the token.** Extra personal access
tokens from the same user share one allowance. `airadar doctor` measures which
is true rather than assuming.

## The reader profile

`SAM_PROFILE.md` is the context the second paragraph under each board row is
written from — who the reader is, what they are building, what they would
actually use a project for. Section 7 of that file is the writing contract the
summariser follows, and the quality of every `usage_tr` paragraph comes from it.

**It is not in this repository and must not be added to it.** This repository
and the site it publishes are public; the profile holds personal and financial
detail. It is a GitHub Actions secret (`SAM_PROFILE`), written to disk by the
daily workflow and gitignored locally. `AIRADAR_PROFILE_PATH` points somewhere
else if needed. With no profile, `airadar summarise` exits non-zero rather than
writing a generic paragraph — a generic answer to "where does this fit into my
work" still reads like an answer, and would quietly replace the real one.

What the model may write is narrower than what it may read: the profile's own
rule 7.6 forbids copying personal or financial detail into the output, so a
published paragraph names a project or a need and never a number.

## Layout

- `pipeline/airadar/` — the Python pipeline. `gh/` (GitHub client, discovery,
  content), `classify/` (rules engine, taxonomy, LLM), `companies/` (the second
  universe: domains, Apify, funding, G2), `scoring/`, `db/`.
- `web/` — the Next.js static site.
- `verdicts/` — hand-made classification judgements, validated on `inputs_hash`
  so a vocabulary change never discards them.
- `.github/workflows/` — daily, discover, backfill, readmes, companies,
  reclassify, publish. All the ones that write the database share the
  `airadar-pipeline` concurrency group, because each publishes the whole file
  and two at once means one erases the other.

## Conventions

- Tests use `MockTransport` against the real client, never a stubbed client, so
  a shape change in the API shows up as a failing test.
- Every bug gets a regression test that fails before the fix, and the test's
  docstring says what it cost.
- Comments explain why, and cite the measurement that produced the decision.
- Run `ruff format` then `ruff check` then `pytest` before committing.
- Turkish for anything the reader sees; English in the code and its comments.
