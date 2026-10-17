# Handoff

Updated 2026-10-17 at the start of the rebuild.

## State

- Repository scaffolded: schema, gold files, supplied documents, examples, agent instructions, plan.
- No pipeline code yet; the plan in `docs/plans/2026-10-17-rebuild.md` defines Tasks 1 to 9.
- Previous attempt (reference only): `/Users/danielpuri/Desktop/Projects/backbone-clinical-abstraction`, with its review and research under `research_notes/` and `reports/` there.

## Last verified

- `uv sync` and `uv run --frozen pytest -q` run with no tests.

## Open

- Tasks 1 to 4 (referee, model clients, reconciler, store and pipeline) run in parallel next.
- Tasks 5 and 6 (questions and answers, synthetic patients) follow, then evals, real runs, and documents.
- Budget: Claude API cap $10, Jev cap $0.50; spend ledger at `outputs/logs/spend.json` once runs start.
