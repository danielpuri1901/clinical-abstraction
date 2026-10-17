# Design decisions

This file records every design decision with the gap it closes, the options rejected, and the check that proves it.
The first entries come from the design review of the previous attempt on 2026-10-17.
Add a dated entry to change a decision; do not rewrite history.
The gap ids (B1, B2, B3, M1, M11) refer to the review file solution_review.md in the previous repository's research notes.

This file records the decisions Daniel makes while we close the gaps that the independent review found.
Each decision names the gap it closes, the option chosen, the options rejected, and the check that proves it.
Decisions move into docs/design.md when they are implemented.

## Gaps to close (from solution_review.md)

| Id | Gap | Severity |
|---|---|---|
| B1 | A retried or re-versioned extraction never rebuilds its patient (pipeline.py:179-183) | blocker |
| B2 | Every ingest loads and validates the whole corpus twice; 14.3 s per document at 62K documents, 1.1 GB memory | blocker |
| B3 | Question router mishandled 20 of 34 unseen questions; every wrong answer still said COMPUTED | blocker for the call |
| M-scale | Answers slow down with the collection: DEV-01 2.6 to 3.5 s at 2,001 patients; one collection question 50.6 s and 2.6 million characters | major |
| M1 | Checks cannot see model mistakes: a wrong label or a dropped break flips a week with no flag | major |
| M-format | Format drift drops care silently: day-first dates, 12-hour times, "1415" times, ordinal dates | major |
| M-evidence | Gold labels not human-checked; two patients by the same author; error rate bounded only below 78 percent | major |
| M11 | Conflict flags cite only one side; answers do not say which document would settle an open question | minor |

## Rounds

1. Question interface and routing (B3, M-scale output size)
2. Incremental ingestion correctness (B1, failure isolation, prompt-version change)
3. Scale of the deterministic layers (B2, indexes, SQLite to Postgres, batch inference, cascade)
4. Accuracy assurance at scale (M1, M-format, M-evidence; synthetic generator with known truth; budget)
5. Verification stack (Lean versus property tests versus differential tests; where a decision model fits)
6. Development process and what to show on the call

## Report verdicts to weigh (reports/Clinical abstraction design review.md)

- Lean 4 kernel: no for now; Hypothesis property tests instead (in Cedar, proofs found 4 bugs and testing found 21).
- Calibrated decision model: yes, as a tie-breaker behind a deterministic question parser and as a second opinion on the four counting labels; measure first.
- OCR: name the stage in the design, do not build it; OCR confidence must feed uncertainty because a misread passes the quote check.
- Small or cheap first tier: not before submission; shrink model output first (output tokens are about two thirds of per-document cost).
- Accuracy at scale: a free part (inject errors into cached extractions) plus a paid 300-document synthetic run, about $5 to $8.40 on the Claude API.
- New code findings: encounters sharing an ID take the first record's date with no date comparison; max_tokens 16,000 is reserved against the Bedrock quota and cuts concurrency; Anthropic Batch API is not HIPAA eligible, Bedrock batch is but has no prompt caching; Haiku 4.5 can be retired with 60 days notice.

## Decisions

### Round 1: question interface

Decided 2026-10-17. Closes B3 and the collection output size part of M-scale.

- D1.1 Hybrid parser. Code parses what code is good at: patients (name or MRN matched against the store), dates, date ranges, and numbers, all validated. Unknown patient or impossible date fails loud with no numbers.
- D1.2 A decision model picks enum values only: metric, attendance filter, service type, grouping, or "other". Jev when a key exists, otherwise Claude Haiku 5.5 with a strict JSON schema; same interface, swappable.
- D1.3 Two independent readers must agree for a plain COMPUTED verdict: the keyword rules propose candidates and the decision model chooses. Disagreement or low confidence gives COMPUTED WITH WARNING, with both readings printed. The rules never decide alone again.
- D1.4 Every answer starts with an interpretation line, for example "Interpreted as: sessions, attended, HG-M042, 2026-01-12 to 2026-01-25, by week". The plan is saved with the answer so verify-answers replays it without a model.
- D1.5 Outcomes: invalid plan gives no numbers and names the unsupported part for a human; valid and agreed gives COMPUTED; valid and disputed gives COMPUTED WITH WARNING; open questions (kind "other") go to the agent, verdict AGENT, unbound numbers flagged.
- D1.6 Code owns the vocabulary: a small hand-written enum reusing the schema Literals for service type and attendance, one executor function per value, versioned.
- D1.7 Cost accepted: about 1 s and under one cent per question. Plans are cached by normalized question text. Tests use recorded plans. Without model credentials the rules-only path runs with a loud warning.
- D1.8 Collection answers carry a limit: counts, top N, and "N more", never every patient's table.
- Rejected: hardened regex router alone (silent wrong family, as the 7 wrong headlines showed); model-written full plan including entities (code is better at dates and names, and a hallucinated patient is worse than a parse error).
- Check: the 34 review questions plus 20 new paraphrases run through the parser with recorded plans; target is zero wrong plain COMPUTED answers, with misreads surfacing as warnings or refusals.

### Round 2: incremental ingestion

Decided 2026-10-17. Closes B1, the failure-isolation gap, and the shared-ID date gap.

- D2.1 Rebuild trigger is a fingerprint, not an event. Each patient stores the hash of its sorted (document sha, extractor version) pairs at build time. After every ingest, a patient whose current pairs hash differently is rebuilt. This covers new documents, retried extractions, and prompt or model changes with one rule. A byte-identical duplicate adds no pair, so no rebuild.
- D2.2 Patient key is stored at registration (MRN pattern from the text, model-confirmed later), so the pipeline loads only the touched patient's documents.
- D2.3 A failed extraction never makes an answer more confident. The patient is built from what exists and marked incomplete with the pending document ids and reasons. Every answer for that patient prints "incomplete: N documents pending" and its verdict carries the warning. Failed documents are retried with backoff and stay visible in a per-patient ledger (registered, extracted, failed).
- D2.4 A prompt or schema change never re-extracts the collection blindly. The extractor version is split per document type or schema section, so a change re-extracts only the affected documents. A new version runs first as a shadow on a sample and the gold set; the abstraction diff is reviewed; then the backfill runs through batch inference. Extraction rows are append-only, keyed by (sha, version), so rollback is a pointer flip.
- D2.5 Records that share an encounter ID but disagree on the date are a conflict, not a silent first-wins. Both dates are kept, the encounter is flagged, and if the dates fall in different weeks both weeks become "cannot be determined". Precedence when it must be resolved: correction, then signed clinical note, then attendance record, then schedule.
- D2.6 Two clocks are stored for every document: authored time (valid time, when the clinician wrote it) and received time (transaction time, when the system got it). Corrections apply in authored order. Receipt order is a transport artifact, so a copy received later carries its original's authored time. Keeping both lets an auditor replay "what we knew on date X" and "what was true on date X".
- Rejected: rebuild on registration only (the current code; stale after retries and prompt changes); refusing all answers for an incomplete patient (the brief asks for what can be established plus what is missing).
- Check: a fake extractor that fails once on BH-D111; run 1 prints incomplete and week 4 is not "met"; run 2 rebuilds HG-M042 with 31 documents and week 4 is "cannot be determined"; a prompt version bump rebuilds every patient whose documents it touched; a duplicate file rebuilds nothing.

### Round 3: scale of the deterministic layers

Decided 2026-10-17. Closes B2 and the per-patient part of M-scale; sets the storage and model-bill plan.

- D3.1 Write path loads one patient, not the collection. The patient id is stored on the document row at registration and indexed. A new document loads only that patient's documents: about 30 rows at any collection size. Today the code reloads and re-parses every document and extraction (14.3 s and 1.1 GB at 62,000 documents, no model time).
- D3.2 Read path gets indexes on (patient), (patient, week start), (patient, service date). Measured in the review: DEV-01 from 2.6 to 3.5 s down to 0.56 s at 2,001 patients. Renderers read rows instead of parsing the 71 KB patient JSON.
- D3.3 SQLite stays for the prototype. Its limit is concurrency, not size: one writer at a time per file, all processes on one machine. The event that forces Postgres: more than one process must write at the same time (parallel ingest workers on several machines, or a question service on another host). Then Postgres, hash-partitioned by tenant, primary key including the partition key, a materialized weeks table for collection questions. Partition by patient is not recommended by any source found.
- D3.4 Model bill levers, in order: (1) cut what the model writes, since output tokens are about two thirds of per-document cost (span-first quotes: the model returns sentence ids and code cuts the quote; fewer observations); (2) batch inference at half price for backfills (Bedrock batch for PHI, no prompt caching; Anthropic Batch is not HIPAA eligible); (3) the cheapest model that passes the gold gate, with the gate re-run per model and the model id in config, because Haiku 4.5 can be retired on 60 days notice.
- D3.5 max_tokens is set near twice the observed p99 (about 3,000, not 16,000), with one retry at a higher cap on truncation. Bedrock reserves max_tokens against the tokens-per-minute quota before the call, so 16,000 against 1,240 used cuts concurrency by about 13 times (estimate, to be measured).
- D3.6 Rate limiting at the provider is handled by a queue with workers and backoff, not by an API gateway. A gateway protects our HTTP service; the limit here is Bedrock's.
- Rejected: moving to Postgres before submission (no concurrent writers exist yet; it would add setup with no measured gain).
- Check: re-run evals/benchmark.py after D3.1 and D3.2; a one-document ingest at 62,000 documents must stay under 1 s with no model time, and DEV-01 under 0.6 s at 2,001 patients; README scale table relabelled measured versus estimate, and the "14 ms" claim restated as the SQL query only or replaced by the full answer time.

### Round 4: accuracy at scale

Decided 2026-10-17. Closes M1, M-format, and M-evidence.

- D4.1 The quote check cannot catch a wrong label or an omission, so two more checks exist. Labels: a second reader (the decision model) re-reads the four counting labels, record role, attendance, service type, and copy or draft, from the cited lines; a disagreement raises a `label_disputed` flag, and when the other label would change the count the encounter becomes `disputed`. Omissions: a coverage check lists every time in the document that no admitted item cites; times inside a session window become `possible_omission` flags.
- D4.2 Accuracy at scale is measured on synthetic patients with known truth. The truth (encounters, breaks, corrections, duplicates, late copies, drafts, conflicts, plan changes) is drawn first, documents are rendered from it in three templates and several date and time formats, and the pipeline's abstraction is scored against the truth with the same gold scorer. Unsettled conflicts are placed so that the week straddles the goal in most cases, so "cannot be determined" is exercised.
- D4.3 Dates and times are normalized at the referee boundary from all common formats (day-first, month names, numeric month-first, ordinals, compact "Jan05", 12-hour, bare "1415") before comparison. The format table test and the coverage check catch new formats as dropped facts.
- D4.4 Budget plan: synthetic run of 25 patients, about 300 documents, on Haiku 5.5 (about $0.35, run three times for variance) and Sonnet 5.5 on 12 patients (about $3.30); the supplied record on Sonnet 5.5 (about $0.70) after a Haiku development run; the held-out patient (about $0.35). All estimates from the list prices on 2026-10-17; the ledger records the measured cost. Caps: $10 Claude API, $0.50 Jev.
- D4.5 Daniel checks the real patient's gold labels against the source documents with the gold checklist before submission, because the labels were derived by the coding assistant.
- Rejected: trusting repeated runs on the same two patients as evidence of accuracy (zero errors on two patients bounds the error rate only below 78 percent).

### Round 5: verification stack

Decided 2026-10-17.

- D5.1 No Lean kernel now. A proof checker certifies derivations, not that an extracted fact is true to the document, and every failure found so far lives in extraction or reconciliation inputs. The range arithmetic gets seeded random property checks in pytest (stdlib only). The README keeps one sentence on a Lean model with differential testing as a later step.
- D5.2 The decision model sits at two points only: question parsing (enum values with abstention, Round 1) and the second reading of the four counting labels (D4.1). Never for open text, never alone; thresholds are measured on the synthetic set before they are trusted.
- D5.3 Definition of done for a rule: one runnable check that fails if the rule breaks; the gold gates still pass; `docs/design.md` has the rule and its why; the measured numbers it touches are re-measured.

### Round 6: process and presentation

Decided 2026-10-17.

- D6.1 Development: the smallest end-to-end slice per task, run on the real path, output inspected. A fresh implementer per independent task (Sonnet 5.5 for bounded work, Opus for design-heavy tasks), test first, no per-task review passes; errors are found by running tests, gates, and the benchmark.
- D6.2 Evals run through Langfuse datasets and experiments when keys exist; every score is computed locally and written to `outputs/experiments/`, so reviewers can run the evals without an account.
- D6.3 On the follow-up call: a five-minute narrative, then three deep dives: the question interface with two readers, incremental ingestion with fingerprints and incompleteness, and accuracy at scale with synthetic truth and the cheap tier.
