# Handoff

Updated 2026-10-18 after the refactor, the rewritten extraction prompt, and the routing defaults.
The system is complete and verified.
Daniel confirms the gold labels and sends the submission.

## What exists

- Code: six modules in `src/clinical_abstraction/`: `schema`, `read` (every model call, the budget ledger, Jev), `reconcile` (check, merge, count), `store` (SQLite, ingest, export), `answer` (question plans, renderers, the open-question agent), `cli`.
  One test file per module, 203 tests, none calls a model.
  The plan is `docs/plans/2026-10-17-rebuild.md`, every decision is in `docs/decisions.md`, and the simplification audit is `docs/ponytail-audit.md`.
- Extraction prompt: XML sections, 15 rules, five worked examples, a forced tool call on Haiku, effort `low`.
  Extractor versions: `c05e76740919` (Haiku 5.5), `76dd8605fc7e` (Sonnet 5.5). Older versions stay in the stores.
- Main store `outputs/abstraction.db`: built by Sonnet 5.5 on the final prompt, 95 of 95 gold checks, $0.5475, 21.9 s.
  It holds 31 documents for 1 patient (HG-M042), 25 encounters, 4 weeks, 3 PHQ-9 administrations, 6 flags, and the saved answers.
- Exports: `outputs/abstraction/HG-M042.json` and `.md`. Answers: `outputs/answers/DEV-01.md` to `DEV-05.md` and `answers.json`, regenerated on the Sonnet build; DEV-04 carries its attendance warning.
- Held-out patient `outputs/experiments/held-out/`: 53 of 53 (Haiku 5.5, final prompt).
- Synthetic set (seed 7, 25 patients, 319 files) in `outputs/experiments/synthetic/`; Haiku 5.5 three passes on the final prompt in `tier-haiku-run1` to `run3`, each 25 of 25 patients and 1,841 of 1,841 checks (two passes re-read one document on a second ingest after a validation failure); Sonnet 5.5 on 12 patients in `tier-sonnet-run1`, 877 of 877.
- Question interface, 54 questions: two readers 0.944 metric, 0.944 filter, 1.0 dates, zero wrong plain COMPUTED, 3 flagged misses (DEV-04, REVIEW-18, REVIEW-21), 4 correct refusals.
- Benchmark: `outputs/benchmarks.json` (stores of 1, 2,000, and 20,000 patients).
- Langfuse project `backbone`: datasets `routing` (54 items) and `extraction-tier` (25 items) with the local scores mirrored; dataset runs were not visible through the public API when last checked. Model calls are not traced to Langfuse; only the evals are mirrored.
- GitHub: private repository `danielpuri1901/clinical-abstraction`, branch `main`. Actions did not start: "recent account payments have failed or your spending limit needs to be increased", an account billing setting; a public repository runs Actions without it. The same gates pass locally.
- Spend across all ledgers: $6.30 Claude, $0.12 Jev, against caps of $10 and $0.50.
- The previous attempt is at `/Users/danielpuri/Desktop/Projects/backbone-clinical-abstraction` (reference only).

## Last verified

- `uv run --frozen ruff check .` clean; `uv run --frozen pytest -q` 203 passed; `uv run --frozen abstraction verify-answers` exit 0.
- Gold gates from the stored extractions: 95 of 95 (Sonnet and Haiku), 53 of 53.
- `python -m evals.experiments routing --arm agreement --db outputs/experiments/held-out/abstraction.db` reproduces the scores above from the Jev cache.
- `python -m evals.experiments benchmark --patients 2000` writes `outputs/benchmarks.json`.

## Open

1. Daniel confirms the gold labels for the supplied record with `docs/gold-checklist.md` (24 rows, every cited line printed).
2. Daniel sends the submission (a Gmail draft exists, unsent) after giving Manan and Andrew access to the private repository or making it public.
3. Optional: Langfuse tracing of model calls (about 25 lines around `read.call_anthropic`, opt-in by an environment variable). Not built.
4. One new document costs 2.4 s of code at 620,000 documents: replace the pending-work scans in `store.extract_missing` and `store.backfill_label_checks` with a pending table; move the budget ledger and the Jev cache out of whole-file JSON rewrites.
5. About one Haiku document per 311 fails schema validation after 3 attempts in some passes; the next ingest re-reads it. A fourth attempt or a repair prompt would remove the second ingest.
6. Known referee limits: a date with no year matches any year, bare `HHMM` times are not scanned for omissions, `authored_at` is not checked against the text.
7. "Week 2" and clinician names in questions are refused; corrections of dates or service types are flagged, not applied.

## How to continue

- Read `AGENTS.md`, then run the gates above. Use `uv run --frozen` for every command. Every command and key rule is in the README.
- A new document: work on a copy of the store, and ingest with the model that built it. A new format that loses facts gets its pattern in `reconcile.py` with a test first.
- A new prompt or model gets a new extractor version; the old rows stay. Score it with `evals.experiments tier --store`, compare with `docs/experiments.md`, and add a dated entry to `docs/decisions.md`.
- A new rule: write the failing test, implement the minimum, update `docs/design.md`, re-measure the numbers it touches.
- For the follow-up call: a five-minute narrative, then three deep dives (decision D6.3): the question interface with two readers, incremental ingestion with fingerprints and incompleteness, accuracy at scale with synthetic truth and the cheap tier. Live checks: `abstraction source "BH-D103 L7"`, `abstraction ask "..."`, and an ingest of `examples/new_document/` into a copy of the store.
