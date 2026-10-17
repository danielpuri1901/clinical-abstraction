# Handoff

Updated 2026-10-17 at the start of the rebuild.

## State (updated after the first real run)

- Merged into main: referee (Task 1), model clients with budget ledger and Jev (Task 2), reconciler and weeks (Task 3), store and pipeline with fingerprint rebuilds and label-check backfill (Task 4), question planner (Task 5a), synthetic patients (Task 6).
- In progress: answers and CLI (Task 5b, branch task5), evals and benchmark with Langfuse recording (Task 7, branch task7).
- First real run, 2026-10-17: `abstraction ingest documents/ --model claude-haiku-5-5` read 31 documents in 46 s for $0.042 (33 calls, 0 failures, 0 repair turns); gold 95 of 95; weeks 140, 120, 180, 145 to 155 with the last "cannot_determine". Jev second reads: 16 disputes at the 0.7 bar, 3 at the 0.9 bar with specific labels only (two are the decision model reading a cancellation notice and a no-show row as outreach calls; harmless, counts unchanged).
- Held-out patient with Haiku 5.5 into `outputs/experiments/held-out/`: 14 new documents, 1 duplicate recognized, 14.7 s, $0.018, gold 53 of 53.
- Synthetic set (seed 7, 25 patients, 319 documents) generated into `outputs/experiments/synthetic/`; first Haiku 5.5 pass into `outputs/experiments/tier-haiku-run1/`: 252 s, $0.28, 20 of 25 patients perfect (1,813 of 1,841 checks). Two root causes: addendum corrections labelled `other` were not applied, and a signed note with stated minutes but no interval did not establish delivery. Both fixed in the reconciler; rebuilt from the same extractions at zero cost: 25 of 25 (1,841 of 1,841), with the real records still 95 of 95 and 53 of 53. The extraction prompt is being tightened for the same two cases (new extractor version; all stores must be re-extracted once it lands).
- Spend so far: $0.34 Claude, $0.03 Jev.

## Earlier state

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
