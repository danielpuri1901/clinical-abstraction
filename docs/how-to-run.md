# How it works, and how to rerun each step

## What happens, step by step

Each step says who does the work: the model or code.

1. **Register (code).** Each file gets a fingerprint of its text. A second copy of the same document is recognized and skipped. The patient id (`MRN`) is read from the text.
2. **Read (model).** The model gets one document with numbered lines. It fills a form: contacts, times, corrections, plan rules, scores, charges. For every value it writes the line numbers that state it. One model call per document.
3. **Check (code).** Code opens the cited lines. A time, a score, or a minute count that is not on its cited lines is dropped. Dates may be anywhere in the document. Nothing is repaired by code; the model gets one chance to fix its own citations. Code also lists every time in the document that no value cites, so a missed break shows up.
4. **Second opinion on labels (decision model, Jev).** Jev reads the same lines and picks the type of record, the attendance, the type of service, and whether the document is a copy or a draft. When Jev is sure (0.9 or more) and disagrees with the model, the contact gets a flag.
5. **Merge and count (code).** All records about the same contact become one row. A later correction wins by the time it was written, not by the time it arrived. Breaks and disconnects are subtracted. Drafts, copies, and schedules never prove that care happened. Disagreements stay as ranges. Weeks are counted against the plan that was in force.
6. **Store (code).** Everything goes into one SQLite file: documents, the model's readings by prompt version, and one record per patient with tables for contacts, weeks, flags, and assessments. A patient is rebuilt only when its documents or readings changed.
7. **Answer a question (code, with Jev for the meaning).** Code reads the patient names and dates from the question. Two readers pick the meaning: keyword rules propose, Jev chooses. If they disagree, the answer says so. Code then computes the answer from the stored tables and cites the lines. The first line of every answer says how the question was read.
8. **Open questions (model agent).** A question that no table answers, for example "who wrote the January 22 note", goes to a model that can only read the stored record and the source lines. Every number it writes is checked against what it read.

The model never counts, never decides across documents, and never answers a question that code can answer.

## Keys and caps

Put `ANTHROPIC_API_KEY=...` in `~/.config/backbone-take-home/anthropic.env` and `JEV_API_KEY=...` in `jev.env`, or export them.
Without a Jev key, `ask` still works on the keyword rules alone and says so in every answer.
Without an Anthropic key, everything that does not read new documents still works.
Spend caps: `MODEL_BUDGET_USD` (default 10) and `JEV_BUDGET_USD` (default 0.50); the ledger is `outputs/logs/spend.json`.

## How to rerun one step

Prefix each command with `uv run --frozen`.

| Step | Command | Model calls | Cost |
|---|---|---|---|
| Read new documents | `abstraction ingest <folder>` | one per new document | about $0.001 (Haiku) to $0.018 (Sonnet) per document |
| Read with the cheap model | `abstraction ingest <folder> --model claude-haiku-5-5` | one per new document | about $0.001 per document |
| Re-check, merge, and count again, no reading | `abstraction ingest <folder> --rebuild-all` | 0 | $0 |
| Add one document | `cp outputs/abstraction.db outputs/demo.db` then `abstraction --db outputs/demo.db --out outputs/demo ingest examples/new_document/` | 1 | $0.03, 5 s |
| Show the record | `abstraction report --patient HG-M042` | 0 | $0 |
| Show the lines behind a citation | `abstraction source "BH-D103 L7"` | 0 | $0 |
| Answer a question | `abstraction ask "..."` | 1 Jev call | $0.00005, 0.3 s |
| Answer an open question | `abstraction ask "..."` (routed to the agent automatically, or `--agent`) | a few | about $0.04 |
| Replay saved answers | `abstraction verify-answers` | 0 | $0 |
| Run the tests | `pytest -q` | 0 | $0 |
| Make synthetic patients with known truth | `python -m evals.synthetic --seed 7 --patients 25 --out outputs/experiments/synthetic` | 0 | $0 |
| Score a saved extraction run against the truth | `python -m evals.experiments tier --model claude-haiku-5-5 --store outputs/experiments/tier-haiku-run1/abstraction.db` | 0 | $0 |
| Measure how well questions are read | `python -m evals.experiments routing --arm agreement --db outputs/experiments/held-out/abstraction.db` | 54 Jev calls | $0.003 |
| Quick benchmark | `python -m evals.experiments benchmark --patients 50 --db outputs/abstraction.db --out outputs/quick` | 0 | $0, about 5 s |

A changed prompt or model gets a new extractor version; `ingest` then reads every document again, and the old readings stay in the store.
Without flags, the benchmark builds 1, 2,000, and 20,000 patients (4 GB of temporary disk, minutes) and overwrites `outputs/benchmarks.json`.

## Other gaps

- "Week 2" works when the question names one patient (the second week of that patient's episode); across several patients it is refused, because their weeks differ.
- A date without a year matches any year.
- Corrections of dates or service types are flagged, not applied.
- A document with no patient id stays unassigned and `ingest` exits with code 1.
- The last week of the supplied record is partial (Monday to Friday) and is judged against the full weekly goal.
- About one Haiku document in 300 fails schema validation after three attempts; it stays pending, the patient is marked incomplete, and the next `ingest` reads it again.
- Model calls are logged locally, not traced to Langfuse; only the eval scores are mirrored there.
