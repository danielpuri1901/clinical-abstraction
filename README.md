# Clinical abstraction for behavioral-health records

A model reads each document once and cites line numbers.
Code checks every cited value, merges the records into one row per contact, keeps conflicts as ranges, counts, and writes the answers with the source line behind every number.
A small calibrated decision model (Jev) gives a second reading of each contact's labels and of each question's meaning; any disagreement is printed, never hidden.

## The five answers

- [DEV-01](outputs/answers/DEV-01.md): 12 therapy sessions on 11 different days (5 individual, 5 group, 2 family).
- [DEV-02](outputs/answers/DEV-02.md): 585 to 595 minutes of therapy. By week: 140, 120, 180, and 145 to 155.
- [DEV-03](outputs/answers/DEV-03.md): weeks 1 and 2 did not meet the plan, week 3 met it, week 4 cannot be determined (145 to 155 minutes against a goal of 150; two signed notes disagree on one start time and no document settles it).
- [DEV-04](outputs/answers/DEV-04.md): January 19 had 2 contacts and 90 minutes. January 21 had 1 video contact and 45 minutes.
- [DEV-05](outputs/answers/DEV-05.md): the PHQ-9 score fell 8 points over 3 administrations (18, 14, 10). One later copy is not a new administration.

The abstraction is in [outputs/abstraction](outputs/abstraction) (Markdown and JSON) and in the SQLite store `outputs/abstraction.db`.

## Results (measured)

| Check | Result |
|---|---|
| Supplied record, 95 expected values, 31 documents | 95 of 95 with Sonnet 5.5 and with Haiku 5.5; the values were derived by the coding assistant and confirmed by me against the cited lines (`docs/gold-checklist.md`) |
| Held-out patient in another document format, 53 expected values | 53 of 53 with Haiku 5.5, first run |
| 25 synthetic patients with known truth, 1,841 expected values | 25 of 25 patients in each of three Haiku 5.5 runs; 12 of 12 patients with Sonnet 5.5 |
| Reading the meaning of 54 unseen questions | 94 percent of metrics and filters right; 0 wrong answers printed as plain COMPUTED; every miss carries a warning |
| New document (an addendum) | 1 model call, $0.03, 5 s, one patient rebuilt; week 4 changes from cannot be determined to not met |
| Tests | 205 pass, none calls a model |
| Spend for the whole build, at list prices | $6.30 Claude, $0.12 Jev |

## How to run it

```bash
uv sync --frozen
uv run --frozen abstraction ingest documents/            # read documents; 0 model calls now, the saved store already holds them
uv run --frozen abstraction ask "How many minutes of therapy did Rowan receive in the week of January 12?"
uv run --frozen abstraction answer-questions questions.json
uv run --frozen abstraction verify-answers                # replay the saved answers with no model
uv run --frozen pytest -q
```

Keys: `ANTHROPIC_API_KEY` and `JEV_API_KEY` from the environment or from `~/.config/backbone-take-home/*.env`.
Everything that does not read a new document runs without keys; `ask` without Jev falls back to the keyword rules and says so.
Every step, how to rerun it, and what it costs: [docs/how-to-run.md](docs/how-to-run.md).

## Design decision I tested: cheap model or large model?

Can Haiku 5.5 (about 11 times cheaper than Sonnet 5.5) read the documents well enough when code checks and decides?
Test: 25 synthetic patients with known truth (seed 7, 3 document templates, duplicates, late copies, corrections, drafts, conflicts, plan changes), exact checks.

The first Haiku run scored 20 of 25.
All 28 wrong values had two causes: an addendum labelled as a correction of "other", which code did not apply, and a signed record that stated minutes without times.
Both were gaps in code and in the prompt, not in the model; the same readings scored 25 of 25 after the fix, with no new model call.
Three fresh Haiku runs then scored 25 of 25 each; Sonnet scored 12 of 12 on the same patients for $1.53 where Haiku cost $0.09.
What I learned: both tiers pass, so the cheap model can read when code checks and decides; the production store uses Sonnet, and at scale I would run Haiku behind the same checks.
A second measured decision, rewriting the prompt after the official prompting guidance, kept accuracy and cut cost by 30 percent and time by 60 percent; details in [docs/experiments.md](docs/experiments.md).

## Observed limitation, and what I would do next

Reading the question is the weak point.
3 of 54 questions get a different reading than expected (DEV-04, REVIEW-18, REVIEW-21); all three print a warning with both readings, none prints a plain answer.
Next: collect real reviewer questions (the 54 here are mine and from a review of an earlier attempt), run them through the routing experiment in Langfuse, tune the criteria text and the 0.9 bar on that set, and ask the user when the two readers disagree instead of answering with a warning.
Smaller gaps are listed in [docs/how-to-run.md](docs/how-to-run.md).

## First bottleneck at one million documents

Reading: one model call per document (`read.extract_document`, run by `store.extract_missing`).

| For 1,000,000 documents | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Measured cost per document | $0.0006 to $0.0010 | $0.010 to $0.018 |
| Cost at list price, estimate | $600 to $1,000 | $10,000 to $18,000 |
| Measured speed with 8 workers | 2.5 documents per second | 1.8 documents per second |
| Time with 8 workers, estimate | about 5 days | about 7 days |

Changes, in order: the cheapest model that passes the checks, batch jobs at half price for backfills, more workers up to the account's token limit.
After that, the code that looks for pending work scans every document (`store.extract_missing`): 0.05 s per new document at 31 documents, 0.20 s at 62,000, 2.9 s at 620,000 (measured, copies of one patient); it needs a pending-work table, then Postgres when more than one process writes.
A question about one patient takes 0.3 s with the decision model and 6 to 26 ms of code at 2,000 patients; a collection question takes 23 ms.

## Models, settings, coding assistance, runtime, and cost

- Reading: `claude-sonnet-5-5` built the saved store; `claude-haiku-5-5` is the cheap tier. Anthropic API, one tool call per document checked by Pydantic, cached system prompt (XML sections, 15 rules, five worked examples), effort `low`, a forced tool call on Haiku (Sonnet 5.5 rejects forced tool choice), `max_tokens` 3,000 with one retry at 8,000, one repair turn when the check rejects a value, 8 worker threads.
- Decision model: Jev `jev-latest` (answers report `jev-1.13.0`) at `https://api.typesafe.ai/v1/systemone`, disk cache, agreement and dispute bars 0.9.
- Open questions: Claude Sonnet 5.5 with 4 read-only tools and at most 8 calls; every number it writes is checked against what it read.
- List prices on 2026-10-17 per million tokens: Sonnet 5.5 $2 in and $10 out; Haiku 5.5 $0.10 and $0.50; Jev $0.042 in.
- Coding assistance: Claude Code. The orchestrating session ran Claude Fable 5.1 and Claude Opus 5.5; implementers ran Claude Sonnet 5.5 and Claude Opus 5.5 on test-first tasks; research subagents ran Claude Sonnet 5.5.
- Runtime: the 31 supplied documents read in 22 s with Sonnet ($0.55) or 16 s with Haiku ($0.03); a re-run with nothing new takes 0.009 s. Ledgers: `outputs/**/logs/spend.json`.

## Where things are

`src/clinical_abstraction/` (`read`, `reconcile`, `store`, `answer`, `schema`, `cli`), `outputs/` (store, abstraction, answers, logs, experiments, benchmark), `evals/` (expected values, synthetic patients, experiments), `docs/` ([design.md](docs/design.md) every rule and why, [experiments.md](docs/experiments.md) every run, [decisions.md](docs/decisions.md), [how-to-run.md](docs/how-to-run.md)), `documents/` and `questions.json` (supplied), `examples/` (a held-out patient and an addendum, both synthetic).
