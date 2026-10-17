# Agent instructions for this repository

Read [HANDOFF.md](HANDOFF.md) first.
It says what exists, what was last verified, and what is open.
Update it before you stop working.

## What this repository is

A clinical abstraction system for behavioral-health records, built for the Backbone take-home.
A model reads each document once and cites line numbers.
Code checks every cited value, merges records into one row per real contact, keeps conflicts as ranges, and does all counting.
Questions are parsed by code plus a calibrated decision model (Jev); keyword rules act as a second reader, and disagreement is printed, never hidden.
The brief lives outside this repository; its text is quoted in `docs/plans/2026-10-17-rebuild.md`.

## Where decisions live

- [docs/decisions.md](docs/decisions.md) records every design decision with the gap it closes, the options rejected, and the check that proves it.
- [docs/plans/](docs/plans/) holds implementation plans with exact files, signatures, and tests.
- [docs/design.md](docs/design.md) explains each rule and why, for readers of the submission.
- [docs/experiments.md](docs/experiments.md) records every measured run with its log path.
Change a decision by adding a dated entry to `docs/decisions.md`, not by editing history.

## How to work

- Test first: write the failing test, run it, implement the minimum, run it again, commit.
- Every rule leaves one runnable check that fails if the rule breaks.
- Tests never call a model; `tests/conftest.py` makes any attempt fail. Inject fakes.
- Run from the repository root with `uv run --frozen <command>`.
- Gates before "done": `uv run --frozen ruff check .`, `uv run --frozen pytest -q`, `uv run --frozen abstraction verify-answers`, gold gates (`evals/gold/HG-M042.json` 95 of 95, `evals/gold/NF-20817.json` 53 of 53), synthetic truth replay.
- Every number in a document comes from a file in `outputs/`; label estimates as estimates.
- Names say what a thing is or does; no abbreviations.
- Markdown: one sentence per line, plain dashes only, never an em dash.
- Commits: plain English, no AI co-author line.

## Models, budget, and secrets

- Extraction: `claude-sonnet-5-5` for production runs, `claude-haiku-5-5` for development runs (`--model`).
- Decisions: Jev `jev-latest` at `https://api.typesafe.ai/v1/systemone`.
- Every call goes through `llm.Budget`; caps `MODEL_BUDGET_USD` (default 10.0) and `JEV_BUDGET_USD` (default 0.50); the ledger is `outputs/logs/spend.json`.
- Keys come from the environment or from `~/.config/backbone-take-home/anthropic.env`, `jev.env`, and `langfuse.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`; project `backbone` on Langfuse cloud, used only for evals and only with fictional or synthetic records).
- Keys never appear in code, tests, logs, exports, commits, or chat.

## Boundaries

- Do not download or install anything beyond `pyproject.toml` without Daniel's approval.
- Do not submit, publish, push, or email anything; Daniel does that.
- The container image builds in CI, never on a laptop.
