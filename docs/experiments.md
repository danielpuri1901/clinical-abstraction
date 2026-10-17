# Experiments: every measured run

All runs happened on 2026-10-17 on one laptop.
Times are local (UTC+2), as in the log file names.
Log paths are relative to `outputs/`.
Costs use list prices (Sonnet 5.5 $2 input and $10 output per million tokens, Haiku 5.5 $0.10 and $0.50, Jev $0.042 input).
Each ingest log ends with an `ingest_finished` event that holds documents, calls, cost, tokens, and seconds.
"Calls" can exceed "documents": a reply that was cut off at 3,000 tokens is asked again at 8,000, a reply that fails the schema is sent back, and a repair turn goes out when the referee rejects values.
Where a later run overwrote a result file, the earlier numbers come from git history, and the commit is named.

## Run 1 (22:20): the supplied record with Haiku 5.5

- Log: `logs/ingest-20261017-222004.jsonl`.
  Extractor version `44447ececf95` (first prompt).
- 31 documents, 33 calls, 0 failed, 0 repair turns, $0.04225, 45.757 s.
  Tokens: 23,673 input and 60,050 output.
- Gold: 95 of 95.
  Weeks: 140, 120, 180, and 145 to 155 minutes (not met, not met, met, cannot be determined).
- Two documents needed a second call.
  Both wrote 6,000 to 6,500 output tokens, which fits a cut-off at 3,000 and a retry.

## Run 2 (22:22 to 22:26): the second read of four labels

- Question: at what confidence should a Jev disagreement count as a dispute?
- The second read of the 31 documents ran during Run 1: 47 Jev calls (one per service event), $0.0027, in `logs/spend.json`.
- At the first bar (0.7, any model label) the 31 documents gave 16 disputes.
  Most were low-stakes.
- New rule (commit `bb53e38`): confidence of 0.9 and a specific model label.
  The same 31 documents give 3 disputes.
  A middle rule (0.7, specific labels) gives 9.
- The 3 disputes: Jev reads a cancellation notice (HG-E108) and a no-show row (HG-E116) as outreach calls, and reads a video contact (HG-E112) as attended where the model said attended in part.
  No count changes.
- These counts were recomputed from the saved Jev answers in `jev_cache.json`, with no model call.
- Backfill: `logs/ingest-20261017-222621.jsonl`.
  It stored the label checks of the 31 documents in 0.059 s, with 0 failed and no new Jev charge, because the answers were already cached.

## Run 3 (22:27): the held-out patient with Haiku 5.5

- Log: `experiments/held-out/logs/ingest-20261017-222703.jsonl`.
  The store is a copy of the main store plus the 15 files of patient NF-20817, which use another format.
- 14 new documents and 1 byte-identical duplicate (recognized with no call).
  14 calls, 0 failed, 0 repair turns, $0.017515, 14.674 s.
- Gold: 53 of 53, on the first run, before any fix.
  Weeks: met, not met, cannot be determined.

## Run 4 (22:27 to 22:31): the synthetic set, first Haiku pass

- Set: seed 7, 25 patients, 319 files (311 unique, 8 duplicates), templates A 8, B 8, C 9.
  Gold weeks: 47 met, 48 not met, 5 cannot be determined.
- Log: `experiments/tier-haiku-run1/logs/ingest-20261017-222743.jsonl`.
  311 documents, 323 calls, 2 repair turns, 0 failed, $0.282365, 252.189 s.
- Score: 20 of 25 patients perfect, 1,813 of 1,841 checks.
  The score comes from `evals/score.py` run on the abstractions that commit `d2d68bf` saved.
- Five patients failed (SY-M07002, 05, 09, 13, 25), with 28 failing checks.
  Two causes:
  - Four patients: an addendum settled two conflicting signed notes, but its correction was labelled `other`, so the reconciler did not apply it.
    A range of 30 to 45 minutes stayed.
    Two weeks flipped from not met to cannot be determined, and two runs of consecutive weeks moved from established to unresolved.
  - One patient: one signed record stated minutes for two contacts (45 and 50) but gave no times and no attendance, so no record established that either took place.
    10 of 12 sessions counted, and 570 of 665 minutes.

## Run 5 (22:38): reconciler fixes, rebuilt with no model call

- Fix (commits `91a7a9d`, `9f84dc5`): time corrections labelled `other` apply when both values are times, and a signed note with stated minutes and no times establishes delivery.
- Rebuilds from the saved extractions: `logs/ingest-20261017-223838.jsonl` (supplied record, 0.057 s), `experiments/held-out/logs/ingest-20261017-223839.jsonl` (2 patients, 0.068 s), `experiments/tier-haiku-run1/logs/ingest-20261017-223840.jsonl` (25 patients, 0.357 s).
  Each has 0 calls and $0.
- Scores: synthetic 25 of 25 patients and 1,841 of 1,841 checks, supplied 95 of 95, held-out 53 of 53.

## Run 6 (22:39 to 22:45): prompt tightened, every set read again with Haiku 5.5

- Change (commit `063daaf`): the prompt says that a corrected start or end of the contact goes in `patient_arrival` or `patient_departure`, and that a stated contact interval goes in the presence intervals.
  New extractor versions: `d6d18c7eb1e3` (Haiku) and `0c78c0f988fd` (Sonnet).
  Old rows stay.
- Effect: corrections labelled `other` fell from 4 to 0 on the synthetic set.
- Supplied record: `logs/ingest-20261017-223957.jsonl`.
  31 documents, 33 calls, 0 repair turns, $0.042379, 41.385 s.
  Gold 95 of 95.
- Held-out store (31 supplied plus 14 held-out documents): `experiments/held-out/logs/ingest-20261017-224040.jsonl`.
  45 documents, 47 calls, $0.046801, 43.721 s.
  Gold 95 of 95 and 53 of 53.
- Synthetic set: `experiments/tier-haiku-run1/logs/ingest-20261017-224126.jsonl`.
  311 documents, 321 calls, 0 repair turns, $0.276557, 229.13 s.
  Score: 25 of 25 patients, 1,841 of 1,841 checks (`experiments/tier-claude-haiku-5-5-run1.json`).

## Run 7 (22:45 to 22:49): the second synthetic Haiku pass

- Log: `experiments/tier-haiku-run2/logs/ingest-20261017-224553.jsonl`.
  311 documents, 318 calls, $0.270782, 236.175 s.
- Score: 25 of 25 patients, 1,841 of 1,841 checks (`experiments/tier-claude-haiku-5-5-run2.json`).

## Run 8 (22:46 to 22:48): Sonnet 5.5

- Supplied record: `logs/ingest-20261017-224605.jsonl`.
  31 documents, 31 calls, 0 repair turns, $0.524997, 23.425 s.
  Tokens: 22,062 input and 29,779 output (Haiku wrote 59,499 on the same documents).
  Gold 95 of 95.
  This store is now the main store (version `0c78c0f988fd`).
- Synthetic set: `experiments/tier-sonnet-run1/logs/ingest-20261017-224630.jsonl`.
  12 patients (SY-M07001 to SY-M07012), 156 files, 152 unique documents, 152 calls, $1.518866, 92.722 s.
  The 156 files are the SY-M07001 to SY-M07012 files of `experiments/synthetic/documents/`.
  The byte-identical copy in `experiments/tier-sonnet-run1/documents/` that the log names was removed on 2026-10-18.
  Score: 877 of 877 (`experiments/tier-claude-sonnet-5-5-run1.json`).
- Haiku cost for the same 12 patients in the three tightened passes: $0.1366, $0.1325, and $0.1333 (mean $0.1341).
  Sonnet cost 11.3 times more.
- Neither supplied nor synthetic Sonnet runs needed a second call.
  Haiku needed one for 2 of 31 supplied documents and for 7 to 12 of 311 synthetic documents per pass.

## Run 9 (22:48): the five development answers

- Command: `abstraction answer-questions questions.json` on the main store (Sonnet 5.5).
  Files: `answers/DEV-01.md` to `answers/DEV-05.md`, and `answers/answers.json`.
- Verdicts: DEV-01, DEV-02, DEV-03, and DEV-05 are `COMPUTED`.
  DEV-04 is `COMPUTED WITH WARNING: disputed attendance: rules say any, decision model says attended (0.91); disputed service_type: rules say nothing, decision model says any (0.77)`.
- Latency: 0.387, 0.32, 0.274, 0.29, and 0.298 s.
  Decision-model cost: $0.000045 to $0.000046 each.
- `abstraction verify-answers` exits 0.
- Fix made at the same time (commit `f6feb87`): missed sessions no longer include contacts the patient is not expected to attend (collateral, care coordination, outreach calls, administrative, assessment review).

## Run 10 (22:49 to 22:53): the third synthetic Haiku pass

- Log: `experiments/tier-haiku-run3/logs/ingest-20261017-224951.jsonl`.
  311 documents, 320 calls, $0.271889, 239.911 s.
- Score: 25 of 25 patients, 1,841 of 1,841 checks (`experiments/tier-claude-haiku-5-5-run3.json`).
- The three tightened passes cost $0.2708 to $0.2766 and took 229 to 240 s (mean 235 s, 1.32 documents per second).

## Run 11 (22:54): a new document

- File: `examples/new_document/BH-D116_addendum_individual_2026-01-26.txt`.
  It is a synthetic addendum written for this demo.
  It sets the start of HG-E115 to 09:10.
- Log: `experiments/new-document/logs/ingest-20261017-225430.jsonl`.
  The store is a copy of the main store.
  1 document, 1 call, $0.029279, 4.962 s, 1 patient rebuilt.
- HG-E115 moves from a range of 40 to 50 minutes to 40.
  Week 4 moves from 145 to 155 minutes (cannot be determined) to 145 (not met).
  The established run of weeks 1 and 2 does not change.
- Most of the cost is the prompt cache write: 8,210 tokens, $0.0205 of the $0.0293.
  One document alone pays it.
  A batch shares it.

## Run 12 (22:54): an open question through the agent

- Question: "Which clinician wrote the January 22 group note, and what did it say about Rowan's participation?"
- The answer names the clinician and cites lines of BH-D107 and BH-D108.
  Verdict: `AGENT: all 1 numbers bound`.
  It is saved in the `answers` table of `abstraction.db`.
- 4 model calls with Sonnet 5.5, $0.037682 (`logs/spend.json`, entries named `agent`).

## Run 13 (22:57 to 23:00): the routing study

- Files: `experiments/routing-rules.json`, `experiments/routing-jev.json`, and `experiments/routing-agreement.json`.
  The store is `experiments/held-out/abstraction.db`, which holds both example patients.
  54 questions from `evals/questions.json`.
- First run (commit `6f70cce`): rules 0.500, 0.463, 0.556.
  Decision model alone 0.574, 0.574, 0.593, with `no_wrong_plain_computed` 0.981.
  Both readers 0.907, 0.852, 1.000, with 1.000.
  The columns are metric, attendance, and date range.
- Failure: the decision model alone answered PARAPHRASE-15 ("Across the collection, what was the average weekly therapy time per patient?") as plain minutes.
  It is the only wrong plain `COMPUTED` in any arm.
- Fix (commits `fc43168`, `d170120`): average, mean, median, ratio, percent of patients, and per patient make the rules propose `other`, and the open path wins over the decision model.
- Final: rules 0.519, 0.481, 0.574.
  Decision model alone 0.574, 0.574, 0.593 (0.981).
  Both readers 0.926, 0.870, 1.000 (1.000).
- Refusals: the rules alone refuse 23 of the 50 answerable questions, the decision model alone refuses 22, and both together refuse none.
  All three arms refuse the 4 questions that should be refused.
- Both readers: 7 plans differ from the expected reading (DEV-04, REVIEW-03, REVIEW-18, REVIEW-21, REVIEW-27, REVIEW-34, PARAPHRASE-04).
  All 7 carry a warning.
  All 7 misread attendance, and 4 also misread the metric.
- Commit `203741e` (23:11) is titled "experiments re-recorded as Langfuse dataset runs".
  The scores did not change.
  The files gained a `dataset` field and a new `created_at`.

## Run 14 (23:02): the benchmark

- File: `benchmarks.json`.
  No model call.
  Stores of 1, 2,000, and 20,000 patients, built as copies of the one real patient with new ids.
- Build: 12.52 s for 2,000 patients (62,000 documents, 412,250,112 bytes) and 262.19 s for 20,000 patients (620,000 documents, 4,124,356,608 bytes).
- One new document, code only, median and maximum of 5: 0.0517 and 0.0595 s at 1 patient; 0.0923 and 2.0625 s at 2,000; 2.3856 and 9.4711 s at 20,000.
  Each ingest rebuilt 1 patient.
- Warm re-run of the 31 documents: 0.009 s, 0 model calls, 0 patients rebuilt.
- Questions with a faked decision model, median of 3.
  At 1 patient: DEV-01 to DEV-05 take 0.0015, 0.0013, 0.0005, 0.0006, and 0.0007 s.
  At 2,000 patients: 0.0095, 0.0087, 0.0088, 0.0228, and 0.0318 s.
  The slowest of the 3 DEV-01 runs took 1.1163 s.
- At 2,000 patients DEV-04 and DEV-05 name no patient, so they cover the whole collection: 64,748 and 137,161 characters, cut to 50 patients plus a count.
  DEV-01 stays at 7,419 characters.
- Collection question REVIEW-10 at 2,000 patients: 0.0299 s median, 0.3022 s maximum, 824 characters.
- Saved abstraction: `abstraction.db` 516,096 bytes (31 documents, 93 extractions, 25 encounters, 6 flags, 75 observations), 35,624 bytes of abstraction JSON per patient.
- Spend, sum of the seven ledgers at that point: $3.361363 Claude and $0.100375 Jev. After Runs 15 to 19: $6.30 Claude and $0.12 Jev.

## Run 15 (2026-10-18, 00:30): the rewritten extraction prompt, first draft, Haiku 5.5

- Prompt rewritten after the official prompting guidance: XML sections, 15 rules, five worked examples, the document inside a `<document id>` tag, a forced `record_extraction` tool call, and `output_config.effort` set to `low`.
  Extractor version `d00b22a464cc` for Haiku.
- Supplied record: 32 calls, $0.0295, 16.0 s, 29,041 output tokens (the previous prompt: $0.042, 41 s, 59,499). Gold 94 of 95: the BH-D108 row "No show; patient did not attend" was read as `patient_absent`.
- Synthetic set: 319 calls, $0.1811, 121 s, 261,837 output tokens (the previous prompt: $0.27, 235 s, 471,781). 24 of 25 patients, 1,838 of 1,839 checks: a "Visit #" id was dropped on one collateral row of SY-M07024.
- Logs: `outputs/logs/ingest-20261018-*.jsonl` and `outputs/experiments/tier-haiku-run1/logs/`.

## Run 16 (2026-10-18, 00:45): the rewritten prompt, second draft, Haiku 5.5

- Two sentences added: `no_show` when the document says no show or did not attend even if the group ran for others, `patient_absent` only for a contact held on the patient's behalf; `encounter_id` is whatever identifier the document prints (Encounter, Visit #, Record #).
  Extractor version `c05e76740919` for Haiku; this is the final prompt.
- Supplied record: 33 calls, $0.0304, 15.9 s, 30,107 output tokens, 247,250 cache-read tokens. Gold 95 of 95.
- Synthetic set: 328 calls, $0.1911, 125 s, 275,049 output tokens. 25 of 25 patients, 1,841 of 1,841 checks.
- Held-out patient: 53 of 53.

## Run 17 (2026-10-18, 01:00): Sonnet 5.5 on the final prompt

- First attempt failed on every document: the API answered 400, `tool_choice: type "tool" and "any" are not supported for this model`. The patient was rebuilt as incomplete with 31 pending documents and no week was reported as met. The forced tool call is now sent to Haiku only (`read.extract_request_options`); Sonnet keeps the automatic choice and the prompt's instruction to call the tool once. Extractor version `76dd8605fc7e` for Sonnet.
- Supplied record: 31 calls, $0.5475, 21.9 s, 28,254 output tokens. Gold 95 of 95. This is the build in `outputs/abstraction.db`.
- Synthetic subset, 12 patients, 152 documents: $1.5286, 86.6 s, 124,798 output tokens. 12 of 12, 877 of 877.

## Run 18 (2026-10-18, 01:05): two more Haiku passes on the final prompt

- Run 2: 325 calls, $0.1891, 128 s, one document (`SY-M07021_14_service_record`) failed validation after 3 attempts, so SY-M07021 was rebuilt as incomplete and scored 24 of 25. The next ingest re-read that one document for $0.0009 and the set scored 25 of 25, 1,841 of 1,841.
- Run 3: 326 calls, $0.1855, 130 s, the same for `SY-M07013_10_service_record`; after the retry 25 of 25, 1,841 of 1,841.
- The retry is the designed path: a failed document stays pending, the patient is marked incomplete, and the next ingest reads it again.

## Run 19 (2026-10-18, 00:20 to 01:10): the question interface after the default-filter rule

- When the rules propose nothing for a filter and the decision model is below 0.9, the filter takes its plain default and the interpretation line says `(default)` instead of a warning.
  Comparison words ("fewer than 3 days") propose `goal_status` alone, and authorization questions propose `other`.
- 54 questions, real Jev, three arms (`outputs/experiments/routing-*.json`): rules alone 0.537 metric, 0.500 attendance, 0.593 dates; Jev alone 0.574, 0.574, 0.593 with one wrong plain COMPUTED; both readers 0.944, 0.944, 1.000, zero wrong plain COMPUTED, 3 misses (DEV-04, REVIEW-18, REVIEW-21), all with a warning, 4 correct refusals.
- The five development answers were regenerated on the Sonnet build (`outputs/answers/`); `verify-answers` exits 0.

## Run 20 (2026-10-18, 01:20): the benchmark on the final store

- `benchmarks.json` regenerated after the prompt change; the main store now holds 6 extractor versions (186 extraction rows for 31 documents, 753,664 bytes, 32,915 bytes of abstraction JSON).
- One new document, code only, median of 5: 0.050 s at 1 patient; 0.204 s at 2,000 (62,000 documents, 399,765,504 bytes); 2.920 s at 20,000 (620,000 documents, 3,999,723,520 bytes). Slower than Run 14 because every copied patient now carries six extraction rows per document, which the pending-work scan reads.
- Questions at 2,000 patients, median of 3: DEV-01 0.0089 s, DEV-02 0.0070 s, DEV-03 0.0065 s, DEV-04 0.0184 s, DEV-05 0.0255 s. Collection question: 0.0226 s.

## What failed and what changed

| Failure | Found in | Change | Result |
|---|---|---|---|
| 16 Jev disputes on 31 documents, mostly low-stakes | Run 2 | Bar 0.9 and a specific model label | 3 disputes |
| 5 of 25 synthetic patients wrong | Run 4 | Two reconciler rules, then a tighter prompt | 25 of 25 in every later pass |
| An average question answered as plain minutes | Run 13 | Aggregation words route to the open path | Wrong plain `COMPUTED`: 1 to 0 |
| Haiku replies cut off at 3,000 tokens | Runs 1, 4, 6, 7, 10 | None. The retry at 8,000 handles it | 2 to 4 percent of synthetic documents need a second call |
| One new document costs 2.4 s at 620,000 documents | Run 14 | Not fixed. See the README | Open |
| The rewritten prompt lost 2 facts of 1,934 | Run 15 | Two rule sentences (no show, Visit # ids) | 95 of 95, 25 of 25 in Run 16 |
| Sonnet 5.5 rejects a forced tool call | Run 17 | Forced choice for Haiku only | 95 of 95, 12 of 12 |
| One Haiku document per pass fails validation after 3 attempts | Run 18 | None. The next ingest re-reads it | 25 of 25 after the retry |

## Not measured

- The tokens-per-minute quota of the account.
- The cause of the 2.4 s: the pending-work queries were not profiled.
- Which Langfuse runs exist.
  The saved files do not store run names.
- Any real patient other than the one supplied record.
