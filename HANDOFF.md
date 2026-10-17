# Handoff

Updated 2026-10-18 after the simplification on the local branch `refactor` (not merged into `main`, not pushed).
The system is complete and verified.
Daniel confirms the gold labels and sends the submission.

## What exists

- Code: 6 modules in `src/clinical_abstraction/` (see the README repository map): `schema`, `read`, `reconcile`, `store`, `answer`, and `cli`.
  The plan is `docs/plans/2026-10-17-rebuild.md`, and every decision is in `docs/decisions.md`.
  The simplification audit is `docs/ponytail-audit.md`, and decision D7 records the merge of fourteen modules into six.
- Main store: `outputs/abstraction.db`, built with Sonnet 5.5 (extractor version `0c78c0f988fd`).
  It holds 31 documents for 1 patient (HG-M042), 25 encounters, 4 weeks, 3 PHQ-9 administrations, and 6 flags.
  It also keeps the earlier Haiku extractions (`44447ececf95`, `d6d18c7eb1e3`) and 6 saved answers (the five development answers and one agent answer).
- Exports: `outputs/abstraction/HG-M042.json` and `.md`.
  Answers: `outputs/answers/DEV-01.md` to `DEV-05.md`, and `answers.json`.
- Experiments: `outputs/experiments/` holds the held-out, new-document, and three synthetic Haiku stores, one synthetic Sonnet store, the tier and routing results, and the synthetic set (`synthetic/`, seed 7).
- Benchmark: `outputs/benchmarks.json` (stores of 1, 2,000, and 20,000 patients).
- Submission documents: `README.md`, `docs/design.md` (rules and why), `docs/experiments.md` (every run with its log path).
- Spend so far: $3.36 Claude and $0.10 Jev at list prices, against caps of $10 and $0.50.
- `main` is pushed to the private GitHub repository `danielpuri1901/clinical-abstraction` (Open, item 8).
  The branch `refactor` is local only, and nothing is sent.
- The previous attempt is at `/Users/danielpuri/Desktop/Projects/backbone-clinical-abstraction` (reference only).

## Last verified

On the branch `refactor`, 2026-10-18, with no keys in the environment, after every commit of the simplification:

- `uv run --frozen ruff check .`: all checks passed.
- `uv run --frozen pytest -q`: 203 passed in about 5 s.
- Gold gates, from copies of the stores rebuilt with `abstraction ingest --rebuild-all` from the saved extractions and no model call: the main store scores 95 of 95 (Sonnet version `0c78c0f988fd`), and the held-out store scores 95 of 95 and 53 of 53 (Haiku version `d6d18c7eb1e3`).
- The rebuilt main, held-out, `tier-haiku-run1`, and `tier-sonnet-run1` stores match the build of `main` row for row, and their exports match byte for byte.
- `abstraction verify-answers`, run on the rebuilt copy of the main store and a copy of the answers file: exit 0.
  DEV-01, DEV-02, DEV-03, and DEV-05 are COMPUTED.
  DEV-04 is COMPUTED WITH WARNING (attendance and service type readings).
- Synthetic truth, scored from copies of the saved stores: each Haiku pass scores 25 of 25 patients and 1,841 of 1,841 checks, and Sonnet scores 12 of 12 patients and 877 of 877 checks.
- The synthetic set regenerates byte for byte from seed 7.
- All three arms of the routing study replay from the saved Jev cache to the saved scores, with no network call: rules 0.519, 0.481, 0.574; decision model 0.574, 0.574, 0.593; both readers 0.926, 0.870, 1.000.
- Offline commands on a copy of the store: `ask` without keys gives a rules-only answer with its warning, and it refuses an unknown patient, a week number, and an impossible date.
  `report` prints the saved abstraction exactly as the export, and `source` prints the source lines.
- `python -m evals.experiments benchmark --patients 50 --db outputs/abstraction.db --out <folder>` finishes in about 5 s with no model call.

## Open

1. Daniel confirms the gold labels for the supplied record with `docs/gold-checklist.md` (24 rows).
   Until then, 95 of 95 means agreement with the coding assistant's labels.
2. Daniel sends the submission.
   The brief names manan@backbonesystems.ai and andrew@backbonesystems.ai, and asks for a ZIP file or a GitHub link.
   No agent pushes, publishes, or emails.
3. Done: the aggregation override in `answer.plan_question` now has the unit test `test_an_aggregation_no_renderer_computes_routes_to_the_open_path`.
4. The one-document ingest costs 2.4 s at 620,000 documents.
   Profile it, then replace the pending-work scans in `store.extract_missing` and `store.backfill_label_checks` with a pending table.
   Move the budget ledger and the Jev cache to SQLite or append-only lines.
5. Confirm in the Langfuse project `backbone` that the dataset runs exist (datasets `routing` and `extraction-tier`).
   The saved result files do not store the run names.
6. The question interface misreads attendance on 7 of 54 questions, and every plan carries a warning (README, observed limitation).
7. Known referee limits: a date with no year matches any year, bare `HHMM` times are not scanned for omissions, and `authored_at` is not checked against the text.
8. The repository is pushed to the private GitHub repository `danielpuri1901/clinical-abstraction` (Daniel's request).
   CI (`.github/workflows/ci.yml`) did not start there: GitHub reported "The job was not started because recent account payments have failed or your spending limit needs to be increased".
   That is an account billing setting; a public repository runs Actions without it.
   Locally the same gates pass: `ruff check .`, 203 tests, `verify-answers` exit 0.
9. Done: the task worktrees and branches are removed; only `main` remains.

## How to continue

- Read `AGENTS.md`, then run the gates: `ruff check .`, `pytest -q`, `abstraction verify-answers`, and the gold gates.
- Use `uv run --frozen` for every command.
  Every command and key rule is in the README.
- A new document: work on a copy of the store, and ingest with the model that built it.
  If a new format loses facts, add the format to the referee in `reconcile.py` with a test first.
- A new prompt or model: it gets a new extractor version, and the old rows stay.
  Score it with `evals.experiments tier --store`, compare with `docs/experiments.md`, and add a dated entry to `docs/decisions.md`.
- A new rule: write the failing test, implement the minimum, update `docs/design.md`, and re-measure the numbers it touches.
- Every number in a document comes from a file in `outputs/`.
  Label estimates as estimates.
- For the follow-up call, plan a five-minute narrative and three deep dives (decision D6.3): the question interface with two readers, incremental ingestion with fingerprints and incompleteness, and accuracy at scale with synthetic truth and the cheap tier.
  Live checks: `abstraction source "BH-D103 L7"`, `abstraction ask "..."`, and an ingest of `examples/new_document/` into a copy of the store.
