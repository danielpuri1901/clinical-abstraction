# Handoff

Updated 2026-10-17 at the start of the rebuild.

## State

- Repository scaffolded: schema, gold files, supplied documents, examples, agent instructions, plan.
- Task 1 (referee) merged into main: `verify.py` with line citations, date and time normalization, admit, coverage; 102 tests.
- Task 6 (synthetic patients) merged into main: `evals/synthetic.py`, three templates, hazards with fixed rates, deterministic by seed; `python -m evals.synthetic` generates a set; the truth-replay test skips until the reconciler lands.
- Tasks 2 (model clients) and 3 (reconciler and weeks) are in progress on branches `task2` and `task3` under `.worktrees/`; a Task 6 follow-up (conflicts that straddle the weekly goal) is in progress on `task6`.
- Previous attempt (reference only): `/Users/danielpuri/Desktop/Projects/backbone-clinical-abstraction`, with its review and research under `research_notes/` and `reports/` there.

## Last verified

- On main at `bd2a62b`: `uv run --frozen pytest -q` gives 102 passed; `ruff check .` passes.

## Open

- Merge Tasks 2, 3, 6 when their branches land; run the synthetic truth-replay test after Task 3 is in.
- Then Task 4 (store and pipeline with fingerprint rebuilds), Task 5 (question plans and answers), Task 7 (evals and benchmark), the real runs, and the documents.
- Known referee limits from Task 1: a date without a year matches any year; bare `HHMM` times are accepted but not counted by coverage; `authored_at` is not checked against the text.
- Budget: Claude API cap $10, Jev cap $0.50; spend ledger at `outputs/logs/spend.json` once runs start.
