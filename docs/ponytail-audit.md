# Ponytail audit

Repository-wide audit for over-engineering on 2026-10-18, on `main` at `7db7cdc`: 9,856 lines of Python in `src/`, `evals/`, and `tests/`.
Findings are ranked by the lines they were expected to cut, biggest first, and `~` marks an estimate.
Every finding except 21 is applied on the branch `refactor`, and the closing line gives the measured result: 9,479 lines.
Every cut keeps behavior: the CLI commands and flags, the store schema, the JSON in `outputs/`, the extractor versions, and the Jev cache keys stay the same.

1. `delete:` 156 files in `outputs/experiments/tier-sonnet-run1/documents/` (1,772 lines of data) that are byte-identical copies of the SY-M07001 to SY-M07012 files in `outputs/experiments/synthetic/documents/` (same git blobs), plus `tier-haiku-run1/ingest-summary.json` (the `ingest_finished` event of its log) and the empty `ingest-stderr.txt`. Nothing; the run log keeps the file names and `docs/experiments.md` says where the files are. Data, not counted in the net. [outputs/experiments/]
2. `yagni:` fourteen source modules for six responsibilities, eleven test files, and two eval entry points. `schema`, `read` (llm, extract, jev), `reconcile` (verify, reconcile, calculate), `store` (store, pipeline, render), `answer` (questions, answers, agent), `cli`; one test file per module; `evals/benchmark.py` becomes `python -m evals.experiments benchmark`. ~-120 lines of headers, imports, module prefixes, and second copies of `ROOT`, `_shown`, and `LISTED`. [src/clinical_abstraction/, tests/, evals/]
3. `shrink:` six hand-written builders of the same `ServiceEvent` and `DocumentExtraction` defaults (`event`, `extraction` in two test files, the inline extraction in `fixtures.document`). One `fixtures.service_event` and one `fixtures.extraction(**fields)`; each test file keeps a one-line wrapper for its own defaults. ~-80. [tests/test_verify.py, tests/test_llm_extract_jev.py, tests/fixtures.py]
4. `delete:` scaffolding for modules that "are not built yet": the `try/except ImportError` in `conftest.py`, `test_synthetic.PATIENT_ID` with its fallback `provisional_patient_id`, `pytest.importorskip("...reconcile")`, the optional `verify` import, the weekly recompute `build_patient` already does, `synthetic.TruthDoc` with its fallback, and the `importlib` probe for `clinical_abstraction.answers` in the benchmark with the test half that pins its null sections. Plain imports. ~-60. [tests/, evals/synthetic.py, evals/benchmark.py]
5. `shrink:` five hand-written `INSERT` statements in `store.replace_patient` (64 lines). One loop that reads each table's columns from SQLite (`PRAGMA table_info`) and copies the abstraction's rows of the same name, storing lists as JSON. ~-50. [src/clinical_abstraction/store.py]
6. `shrink:` tests that hand-roll a helper their own file already has: `test_extract_validates_admits_and_repairs_once` writes its own fake call (use `extract_with`), `test_jev_request_shape_cache_and_budget` writes its own handler and client (use `jev_client` and `JEV_REPLY`). ~-20. [tests/test_llm_extract_jev.py]
7. `stdlib:` the 14-line `MONTHS` list. `calendar.month_name[on.month]` (English under Python's default C time locale; the seed-7 set still regenerates byte for byte). -13. [evals/synthetic.py]
8. `shrink:` `test_evals_smoke.Replay` repeats `test_store_pipeline.FixtureExtractor`, and both files keep a `USAGE` dict. One `FixtureExtractor` and one `USAGE` in `tests/fixtures.py`. ~-10. [tests/]
9. `shrink:` `benchmark.COUNTED_TABLES` lists the derived tables by hand. `("documents", "extractions", "label_checks", "patients", *store.PATIENT_TABLES)`. -9. [evals/benchmark.py]
10. `shrink:` `reconcile._assessments` writes the same six-key entry dict twice. One small builder for an original and for a result known only from a copy. ~-8. [src/clinical_abstraction/reconcile.py]
11. `shrink:` store reads repeated across modules: the document-text lookup in `cli source`, `agent._line_count`, and `agent.source_lines`, and the abstraction lookup in `cli report`, `answers._stored`, and `experiments._stored_run`. `store.document_lines(connection, doc_id)` and `store.stored_abstraction(connection, mrn, part="$")`. ~-8. [src/clinical_abstraction/, evals/experiments.py]
12. `delete:` `experiments.dataset_of` fallback for results saved before they named a dataset; all seven saved results name one. `result["dataset"]`, and drop the test lines that pin the fallback. ~-7. [evals/experiments.py, tests/test_evals_smoke.py]
13. `shrink:` duplicated range and count text: `render._range` and `calculate._bounded` are the same function, and `calculate._therapy_days(low, high)` is `answers._count(low, high, "therapy day")`. One `range_text` and one `count_text` in the calculation section of `reconcile.py`. ~-6. [src/clinical_abstraction/render.py, calculate.py, answers.py]
14. `shrink:` `benchmark._one_document_ingest` computes median and maximum by hand next to `_timed`. Time the ingest with `_timed` and check each summary inside the timed function. ~-6. [evals/benchmark.py]
15. `shrink:` the JSON ledger and the Jev cache each load-if-present and write a scratch file then rename. `_load_json(path)` and `_save_json(path, data, indent)` beside them in `read.py`, keeping each file's formatting. ~-5. [src/clinical_abstraction/llm.py, jev.py]
16. `yagni:` `questions.choose`, a wrapper that only delegates to `jev.ask`. Call `jev.ask(question, JEV_QUESTIONS)` at its two callers. -4. [src/clinical_abstraction/questions.py, evals/experiments.py]
17. `shrink:` `fixtures._sha` and two inline `hashlib.sha256(...).hexdigest()` in tests repeat `store.content_sha`. Use it. ~-4. [tests/]
18. `yagni:` `render.to_json`, one line with one caller. Inline into the export. -3. [src/clinical_abstraction/render.py]
19. `shrink:` the loop in `jev._header_length`. `next((n for n, line in enumerate(lines[:HEADER_LINES]) if not line.strip()), min(len(lines), HEADER_LINES))`. -3. [src/clinical_abstraction/jev.py]
20. `shrink:` the key folder `~/.config/backbone-take-home/` is built in `cli.load_keys` and in `experiments._langfuse_keys`. One `read.load_keys(*names)`. ~-2. [src/clinical_abstraction/cli.py, evals/experiments.py]
21. `shrink:` `llm.PRICES` uses its own key names and `_PRICE_KEYS` translates them. Key the prices by the usage keys and drop the translation. -2. Not applied: keyed by the usage names, each price line passes 130 characters, and the short labels read better. [src/clinical_abstraction/llm.py]
22. `shrink:` second copies of constants: `questions._MONTH_KEYS` is `verify._MONTH_NAMES`, and `fakes.NEUTRAL` is `questions.NEUTRAL`. Import them. -2. [src/clinical_abstraction/questions.py, tests/fakes.py]
23. `shrink:` the export file name `re.sub(r"[^\w.-]", "_", mrn)` is built twice in `pipeline`. One `_export_path(out, mrn, suffix)`. -1. [src/clinical_abstraction/pipeline.py]
24. `shrink:` `calculate.plan_for` converts a date with `str()` for its one caller, which already holds the ISO string. Pass the string. -1. [src/clinical_abstraction/calculate.py]
25. `shrink:` `experiments.Arm` and `ARMS` spell the same three names twice. `ARMS = get_args(Arm)`. 0. [evals/experiments.py]
26. `shrink:` found while applying: the benchmark's run list and the tier experiment's run logs each parse the `ingest_finished` event of every ingest log. One `_finished_runs(logs)`. -5. [evals/experiments.py]
27. `shrink:` found while applying: `test_times_and_dates_normalize` repeats five cases of the two parametrized date and time tests. Keep only the forms they do not cover. -4. [tests/test_reconcile.py]

Not cut, on purpose:

- The field definitions in `schema.py`, `extract.SYSTEM_PROMPT`, the tool definition, and the Jev criteria texts: they are hashed into the extractor version and the Jev cache keys, so any edit re-extracts every document or misses the cache.
- `evals/score.load_gold`, which no code calls: the gold gates are run with it by hand.
- One-key-per-line dict literals in `evals/synthetic.py` and the test builders: putting them on fewer lines cuts lines, not complexity.
- `cli.CITATION` next to `agent.CITATION`: the CLI accepts any id ("NOPE L1") and reports it as missing, which the agent's stricter pattern refuses.
- `langfuse`: it is the documented mirror of the eval scores (decision D6.2), and only a dev dependency.
- The two thread-pool loops in `pipeline.extract_missing` and `pipeline.backfill_label_checks`: one shared generator saves about five lines and moves the budget-stop logic out of sight.
- `httpx`, the one possible dependency cut: `jev.py` can use `httpx2`, which the anthropic SDK already installs with the same `Client`, `HTTPTransport(retries=)`, and `MockTransport`.
  Not applied, because it changes `pyproject.toml` and `uv.lock`, and installs need Daniel's approval.

net: -377 lines, -1 deps possible.
