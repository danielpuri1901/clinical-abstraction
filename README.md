# Clinical abstraction for behavioral-health records

This program reads a folder of clinical documents about a patient and builds one clear record of the care the patient received.
It then answers questions about that care from the record, with the source line behind every number.
A language model reads each document once.
Code does everything else: it checks what the model read, merges the documents, counts, and writes the answers.

On the supplied record, all 95 expected values are correct with the cheap model (Haiku 5.5, $0.03) and with the larger model (Sonnet 5.5, $0.55).
The five development answers are in [outputs/answers](outputs/answers).

## The five answers

- [DEV-01](outputs/answers/DEV-01.md): 12 therapy sessions on 11 different days (5 individual, 5 group, 2 family).
- [DEV-02](outputs/answers/DEV-02.md): 585 to 595 minutes of therapy. By week: 140, 120, 180, and 145 to 155.
- [DEV-03](outputs/answers/DEV-03.md): weeks 1 and 2 did not meet the plan, week 3 met it, week 4 cannot be determined (145 to 155 minutes against a goal of 150).
- [DEV-04](outputs/answers/DEV-04.md): January 19 had 2 contacts and 90 minutes. January 21 had 1 video contact and 45 minutes.
- [DEV-05](outputs/answers/DEV-05.md): the PHQ-9 score fell 8 points over 3 administrations (18, 14, 10). One later copy is not a new administration.

Why a range such as "145 to 155"?
Two signed notes disagree about one start time, and no document settles it.
The program keeps both values instead of picking one.
A week is "met" only if the lowest value meets the goal, and "not met" only if the highest value misses it.

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

## How to run it

```bash
uv sync --frozen                                   # Python 3.12 and the locked dependencies
uv run --frozen abstraction ingest documents/      # read the documents (0 model calls now: the saved store already holds them)
uv run --frozen abstraction ask "How many minutes of therapy did Rowan receive in the week of January 12?"
uv run --frozen abstraction answer-questions questions.json    # write the five answers to outputs/answers
uv run --frozen abstraction verify-answers         # replay the saved answers with no model
uv run --frozen pytest -q                          # 203 tests, none calls a model
```

Keys: put `ANTHROPIC_API_KEY=...` in `~/.config/backbone-take-home/anthropic.env` and `JEV_API_KEY=...` in `jev.env`, or export them.
Without a Jev key, `ask` still works on the keyword rules alone and says so in every answer.
Without an Anthropic key, everything that does not read new documents still works.
Spend caps: `MODEL_BUDGET_USD` (default 10) and `JEV_BUDGET_USD` (default 0.50).

### How to rerun one step

| Step | Command | Model calls | Cost |
|---|---|---|---|
| Read new documents | `abstraction ingest <folder>` | one per new document | about $0.001 (Haiku) to $0.017 (Sonnet) per document |
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

Prefix each command with `uv run --frozen`.
A changed prompt or model gets a new version; `ingest` then reads every document again, and the old readings stay in the store.

## Results (measured)

| Check | Result |
|---|---|
| Supplied record, 95 expected values, 31 documents | 95 of 95 with Sonnet 5.5 and with Haiku 5.5 |
| Held-out patient in another document format, 53 expected values | 53 of 53 with Haiku 5.5, first run |
| 25 synthetic patients with known truth, 1,841 expected values | 25 of 25 patients in each of three Haiku 5.5 runs on the final prompt (in two runs one document failed validation and passed on the next ingest); 12 of 12 patients with Sonnet 5.5 |
| Reading the meaning of 54 questions | Two readers: 94 percent of metrics right, 94 percent of filters right, 0 wrong answers printed as plain COMPUTED |
| Five development answers | All computed by code in 0.3 s each; the replay finds no change |
| Tests | 203 pass, none calls a model |
| Spend for the whole build, at list prices | $6.30 Claude, $0.12 Jev |

The expected values for the supplied record were derived by the coding assistant and confirmed by me against the cited source lines on 2026-10-18 (`docs/gold-checklist.md` prints every cited line under each value).
The synthetic truth and the program have the same author, so a blind spot could sit in both.

## What I tested: cheap model or large model?

Question: is the cheap model (Haiku 5.5, about 11 times cheaper) good enough to read documents when code checks and decides?

The first Haiku run on 25 synthetic patients scored 20 of 25.
All 28 wrong values had two causes.
An addendum that settled two conflicting notes was labelled as a correction of "other", so code did not apply it.
One signed record stated minutes without times, so two contacts were not counted.
Both were gaps in code and in the prompt, not in the model.
After the fix, the same readings scored 25 of 25 with no new model call.
Three fresh Haiku runs on the tightened prompt each scored 25 of 25.
Sonnet 5.5 scored 12 of 12 on the same patients for $1.53 where Haiku cost $0.09.

What I learned: both tiers pass, so the cheap model can read when code checks and decides.
The held-out patient, written before the program saw it, passed on the first run.
The production store uses Sonnet; at scale I would run Haiku behind the same checks.

Second test: the prompt, rewritten after the official prompting guidance (XML sections, five worked examples, a forced tool call on Haiku, effort set to low because extraction copies values and needs no long thinking).
Same model, same documents, exact checks:

| Haiku 5.5 | Previous prompt | Rewritten prompt |
|---|---|---|
| Supplied record, 31 documents | 95 of 95; $0.042; 41 s; 59,499 output tokens | 95 of 95; $0.030; 16 s; 30,107 output tokens |
| Synthetic set, 311 documents | 25 of 25; $0.27; 235 s; 471,781 output tokens | 25 of 25; $0.19; 125 s; 275,049 output tokens |

The first draft of the rewrite lost 2 of 1,934 facts (a "no show" row read as absent, a "Visit #" id dropped); two sentences in the rules fixed both.
Thinking is on by default and counts toward `max_tokens`, which is where the cut-offs and the extra output tokens came from.
Sonnet 5.5 rejects a forced tool call, so it keeps the automatic choice; it scored 95 of 95 and 12 of 12 on the rewritten prompt too.

Third test: how to read a question.

| Reader | Metric right | Filter right | Wrong answers printed as plain COMPUTED |
|---|---|---|---|
| Keyword rules alone | 54 percent | 50 percent | 0 (but 22 of 50 questions refused) |
| Decision model alone | 57 percent | 57 percent | 1 |
| Both readers | 94 percent | 94 percent | 0 |

Each reader alone refuses almost half of the questions.
Together they answer all of them, and each of the 3 misread plans carries a warning that shows both readings.
A question that names no filter takes the plain reading (every contact when listing sessions, attended ones when counting minutes) and says so in its first line: `any attendance (default)`.

## What does not work well yet

Reading the question is the weak point.
3 of 54 questions get a different reading than expected: DEV-04 (the two readers disagree on whether "therapy contacts" means attended ones), REVIEW-18 ("fewer than 3 therapy days" read as a day count rather than a goal check), and REVIEW-21 (an attendance question about a plan change).
All 3 print a warning with both readings, and none prints a plain answer.

What I would do next:

1. Collect real reviewer questions; the 54 here come from the builder and from a review of an earlier attempt.
2. Run them through the routing experiment, locally and in Langfuse.
3. Tune the criteria text and the 0.9 bar on that set.
4. When the two readers disagree, ask the user instead of answering with a warning.

Other gaps:

- "Week 2" is refused, because week numbers differ by patient. Ask with a date.
- A clinician name in a question is refused as an unknown patient.
- A date without a year matches any year.
- Corrections of dates or service types are flagged, not applied.
- A document with no patient id stays unassigned and `ingest` exits with code 1.
- The last week of the supplied record is partial (Monday to Friday) and is judged against the full weekly goal.
- About one Haiku document in 300 fails schema validation after three attempts; it stays pending, the patient is marked incomplete, and the next `ingest` reads it again.
- Model calls are logged locally, not traced to Langfuse; only the eval scores are mirrored there.

## Speed and cost (measured)

| Measure | Result |
|---|---|
| Read the 31 supplied documents, 8 workers | Sonnet 5.5: 22 s, $0.55. Haiku 5.5: 16 s, $0.03 |
| Read 311 synthetic documents, Haiku 5.5 | about 125 s, $0.19 per run |
| Run again with nothing new | 0.009 s, 0 model calls |
| Add one document (an addendum), Sonnet 5.5 on the previous prompt | 5.0 s, 1 model call, $0.03 |
| Code time to add one document, after the model | 0.05 s with 31 documents; 0.20 s with 62,000; 2.9 s with 620,000 |
| Question about one patient | 0.3 s with the decision model; 6 to 26 ms of code at 2,000 patients |
| Question across 2,000 patients | 23 ms |
| Size of the saved store | 754 KB for the supplied record with 6 extractor versions; 400 MB at 62,000 documents; 4.0 GB at 620,000 |

The 2,000 and 20,000 patients are copies of the one real patient with new ids (`outputs/benchmarks.json`).

## At one million documents (estimates from the measured numbers)

Reading is the first bottleneck: one model call per document (`read.extract_document`, run by `store.extract_missing`).

| For 1,000,000 documents | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Measured cost per document (synthetic set to supplied record) | $0.0006 to $0.0010 | $0.010 to $0.018 |
| Cost at list price, estimate | $600 to $1,000 | $10,000 to $18,000 |
| Measured speed with 8 workers | 2.5 documents per second | 1.8 documents per second |
| Time with 8 workers, estimate | about 5 days | about 7 days |

Changes, in order: make the model write less, use the cheapest model that passes the checks, run backfills as batch jobs at half price, then add workers up to the account's token limit.
After that: the code that looks for pending work scans every document (`store.extract_missing`), which is why one document costs 2.9 s at 620,000 documents; it needs a pending-work table, then Postgres when more than one process writes.

## Models, settings, and help from AI

- Reading: `claude-sonnet-5-5` built the saved store; `claude-haiku-5-5` is the cheap tier. Both through the Anthropic API, one tool call per document checked by Pydantic, cached system prompt (XML sections, 15 rules, five worked examples), effort `low` (`EXTRACT_EFFORT`), a forced tool call on Haiku (Sonnet 5.5 rejects forced tool choice and keeps automatic), `max_tokens` 3,000 with one retry at 8,000, one repair turn when the check rejects a value, 8 worker threads.
- Decision model: Jev `jev-latest` (answers report `jev-1.13.0`) at `https://api.typesafe.ai/v1/systemone`, with a disk cache; agreement and dispute bars 0.9.
- Open questions: Claude Sonnet 5.5 with 4 read-only tools and at most 8 calls.
- List prices on 2026-10-17 per million tokens: Sonnet 5.5 $2 in and $10 out; Haiku 5.5 $0.10 and $0.50; Jev $0.042 in.
- Coding assistance: Claude Code. The orchestrating session ran Claude Fable 5.1 and Claude Opus 5.5. Implementers ran Claude Sonnet 5.5 and Claude Opus 5.5 on test-first tasks. Research subagents ran Claude Sonnet 5.5.
- Runtime and cost of the whole build: see the tables above and the ledgers in `outputs/**/logs/spend.json`.

## Where things are

- `src/clinical_abstraction/`: `read` (every model call: extraction, the Jev second opinion, and the spend ledger), `reconcile` (check, merge, and count), `store` (the SQLite store, ingest, and exports), `answer` (read the question, answer it, and the open-question agent), `schema` (what the model may claim), `cli`.
- `outputs/`: the saved store, the patient record (`abstraction/HG-M042.md`), the five answers, logs, experiments, `benchmarks.json`.
- `evals/`: expected values, the scorer, the synthetic patient generator, the experiments and the benchmark (`experiments.py`), the 54 questions.
- `docs/`: [design.md](docs/design.md) (every rule and why), [experiments.md](docs/experiments.md) (every run), [decisions.md](docs/decisions.md) (every decision with the gap it closes), the gold checklist, the plan, the simplification audit.
- `documents/`, `questions.json`, `examples/`: the supplied record, the five questions, a held-out patient and an addendum (both synthetic).
