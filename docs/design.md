# Design: the rules and why

The system builds one abstraction per patient from the documents, once.
Code then answers every question from that abstraction.
This file lists each rule, the reason for it, and the check that fails when the rule breaks.
`docs/decisions.md` holds the dated decisions and the options that were rejected.
`docs/experiments.md` holds the measured runs.

## What the model does and what code does

| Step | Done by | Reason |
|---|---|---|
| Read one document and cite line numbers | Claude, one call per document (`extract.py`) | Reading messy text is what a model does well. |
| Check each cited value against its cited lines | Code (`verify.py`, the referee) | A model can state a value that the text does not hold. |
| Second-read four labels of each contact | Jev, the decision model (`jev.py`) | The value check cannot see a wrong label. |
| Merge records, apply corrections, compute minutes, ranges, and weeks | Code (`reconcile.py`, `calculate.py`) | The same extractions must give the same numbers, and each number must trace to lines. |
| Read patients and dates in a question | Code (`questions.py`) | A made-up patient is worse than a parse error. |
| Pick metric, attendance filter, service type, and grouping | Keyword rules and Jev, as two readers (`questions.py`) | Two independent readers expose a misread. |
| Compute and write the answer | Code (`answers.py`) | Every number comes from stored rows and cites source lines. |
| Answer an open question | A Claude agent with read-only tools, then code checks its numbers (`agent.py`) | No fixed renderer exists for these questions. |

## Why not retrieval plus an answer

The questions ask for counts, minutes, and weeks that several documents describe at once.
A search returns separate passages, so a model would have to merge them and add them up again for every question.
Documents also duplicate, disagree with, and correct each other.
The rule for which record wins must run the same way every time.
That rule is code, not a prompt.
The abstraction makes reading a one-time cost.
A question costs the decision model about $0.000044 on average (52 measured calls), and no new read of the record.
Search has one role.
The open-question agent searches one patient's lines by keyword, and code then checks that each number came from a tool result.

## The referee (`verify.py`)

The extraction model is untrusted.
The referee checks every claim before reconciliation uses it.

| Rule | Reason | Check |
|---|---|---|
| Each item cites line numbers, not quotes. Code reads the text of the line. | A line number costs fewer output tokens than a quote, and it cannot be misquoted. A reviewer can open the line. | `test_item_citing_missing_or_no_lines_is_dropped` |
| Each time, number, score, and correction value must appear in the lines the item cites. | The same value on another line can belong to another contact. | `test_value_must_sit_in_its_own_cited_lines` |
| A date may sit on any line of the document. | Most documents state the service date once, in a header. | `test_dates_may_sit_on_any_line_but_must_exist` |
| Code never repairs a value. An unsupported value becomes null, or the item is dropped, and a `Problem` is recorded. | A guessed repair hides an error. | `test_admit_drops_unsupported_values_and_reports_them` |
| The model gets one repair turn that lists the values the referee could not confirm. | The model can cite the right line or answer null. The first synthetic pass needed 2 repair turns for 311 documents, and later passes needed none. | `test_extract_validates_admits_and_repairs_once` |
| Dates and times are normalized before the comparison: day-first, month names, month-first numbers, ordinals, `Jan05`, 12-hour times, bare `1415`. | A format the referee cannot read would drop care without a sign. | `test_document_formats_are_accepted_not_dropped` |
| A coverage check lists every time and date in the text that no admitted item carries. A time inside a session window becomes a `possible_omission` flag. | The value check cannot see what the model left out. | `test_possible_omission_only_inside_a_session_window` |
| Jev second-reads four labels of each contact (record role, attendance, service type, copy or draft) from the cited lines and the document header. It never sees the model's labels. | The value check cannot see a wrong label. | `test_second_read_requests_do_not_depend_on_the_models_labels` |
| A dispute needs a confidence of 0.9 and a specific model label (not `not_stated` or `other`). It always raises a flag. It changes the count only when the other label would change it. | At the 0.7 bar the 31 supplied documents gave 16 disputes. At 0.9 with specific labels they gave 3. | `test_label_dispute_holds_the_count_only_when_it_could_change_it` |

Known limits: a date with no year matches any year, bare `HHMM` times are accepted but not scanned by the coverage check, and `authored_at` is not checked against the text.

## Reconciliation (`reconcile.py`, `calculate.py`)

The code applies these rules in this order.

- Identity.
  Records of one contact merge by encounter id, then appointment id, then the same date with a compatible service type and overlapping times.
  Family and collateral count as compatible, because one record can call a partner session either one.
  Check: `test_order_and_copies_do_not_change_results`.
- Evidence tiers.
  Signed clinical notes, attendance records, and telehealth logs are primary.
  Schedule listings say what was booked.
  Copies repeat their original and count only when the original is missing.
  Drafts, templates, unsigned notes, and charges never establish that care happened.
  A scheduled appointment, a note, and an attendance entry each add different information, and a later document does not override an earlier one.
  Checks: `test_absent_words_in_a_note_marked_attended_hold_it_disputed`, `test_register_rows_of_other_encounters_do_not_contradict_this_one`.
- Two clocks.
  Every document has an authored time (when the clinician wrote it) and a received time (when the system got it).
  Corrections apply by authored time to every record authored before them, including copies received later.
  Receipt order is a transport artifact.
  A record authored after a correction that still disagrees is a conflict (`conflict_after_correction`), not an override.
  Check: `test_late_copy_takes_the_correction_by_authored_time`.
- Corrections that code applies.
  Arrival, departure, attendance, and time corrections labelled `other` apply when both values are times and the replaced value is only a start or only an end.
  A correction with no encounter id goes to the encounter on its date that states the replaced value.
  Any other correction raises `correction_not_applied`, because a correction guessed onto the wrong contact is worse than a flag.
  Checks: `test_time_correction_labelled_other_applies_to_the_start_or_end_it_replaces`, `test_correction_without_an_id_goes_to_the_encounter_holding_the_replaced_value`.
- Date conflicts.
  Records that share an encounter id but disagree on the date keep both dates and raise `date_conflict`.
  Any week that holds either date is `cannot_determine`.
  The listed date follows this order: signed note, attendance record, schedule, copy, draft.
  Check: `test_shared_id_with_different_dates_is_a_conflict`.
- Minutes.
  Patient-present minutes are the presence intervals minus breaks, disconnects, and parts without the patient.
  A break that any admissible record documents applies to every record of the contact.
  A signed note or attendance record that states minutes, and gives no times and no attendance, establishes delivery (the second gap of the synthetic study).
  Checks: `test_minutes_follow_corrections_breaks_and_conflicts`, `test_stated_minutes_on_a_signed_note_without_times_establish_delivery`.
- Attendance.
  The strongest tier that states attendance decides.
  A keyword check on the cited lines ("absent", "no show", and similar) holds a contact labelled attended as `disputed`.
  A schedule listing alone holds the contact as `disputed`.
- Counting.
  A contact counts only when the treatment plan counts its service type and a record establishes delivery.
  `counts_as_therapy` is `yes`, `no`, or `disputed`.
  A disputed contact counts in the high bounds only.
- Ranges.
  When primary records disagree on minutes, the contact carries a range `low-high` and an `unresolved_minutes_conflict` flag.
  The flag cites both sides.
- Weeks with three values.
  A week is met when the low bounds meet the goal.
  A week is not met when even the high bounds miss it.
  Otherwise the week cannot be determined.
  The answer is then right for every way the open conflicts could be resolved.
  This follows the idea of consistent answers: an answer that holds in every way the conflicts could be resolved.
  Reference, supplied with the design notes and not opened in the session that wrote this file: http://www.cs.toronto.edu/~marenas/publications/tcs03.pdf
  Checks: `test_weeks_three_valued_and_consecutive_runs`, `test_property_ranges_hold_for_every_value` (1,000 seeded cases).
- Settle sentences.
  Every conflict flag cites both sides and names the document that would settle it, for example "An addendum or signed note stating the start time of HG-E115 would settle it."
  A week that cannot be determined prints these sentences.
- Consecutive shortfalls.
  A run is two weeks long.
  It is established when both weeks are not met.
  It depends on unresolved records when each week is not met or cannot be determined.
- Treatment plans.
  Versions sort by effective date, and a version ends the day before the next one starts.
  A later version that says the counting rules are unchanged keeps the earlier types.
  The plan sets the week definition (Monday to Sunday in the supplied record).
- Incomplete patients.
  A document that failed or is pending leaves the patient built from what exists, with the document ids and reasons in `incomplete`.
  Every week of that patient is `cannot_determine`.
  Every answer prints "Incomplete: N documents pending" and a warning.
  A missing document never makes an answer more confident.
  Check: `test_incomplete_patients_carry_their_pending_documents`.
- Assessments.
  Only distinct administrations count.
  A copy is named on its original.
  A result known only from a copy is marked.
  Check: `test_assessments_keep_distinct_administrations_and_name_copies`.
- Progress conclusions.
  The PHQ-9 bands, the 5-point change, the 50 percent response, and the remission bar are fixed constants in `answers.py`.
  They are general references, not patient facts.
  Each answer lists what the record can and cannot support.

## The question interface (`questions.py`, `answers.py`, `agent.py`)

- Code reads patients and dates.
  A patient is an MRN or a name matched against the store.
  An unknown patient, an ambiguous first name, an impossible date, a reversed range, and a week number are refused with the reason and no numbers.
  Checks: `test_dates_parse_or_refuse`, `test_patients_match_or_refuse`.
- Two readers pick four enum values (metric, attendance, service type, grouping).
  The keyword rules propose candidates.
  Jev chooses and gives a confidence.
  A field is agreed when Jev's choice is among the rule candidates, or when the rules propose nothing and Jev is at least 0.9 sure.
  Otherwise the answer carries a warning that prints both readings.
  Check: `test_agreement_decides_the_verdict`.
- Without Jev, rules with exactly one metric candidate answer with the warning "rules only, no decision model".
  Otherwise the question is refused.
  Check: `test_rules_alone_refuse_a_question_without_one_reading`.
- Four outcomes.
  `COMPUTED`.
  `COMPUTED WITH WARNING` with the reasons (the readers disagree, documents are pending, or a citation is not found).
  `REFUSED` with the reason and no numbers.
  `AGENT` for open questions, with the agent's numbers checked.
- Plan echo.
  The first line of an answer is "Interpreted as: ...".
  The plan is saved with the answer, so `verify-answers` replays it with no model and compares the text.
  A mismatch reads `NOT VERIFIED`.
  Check: `test_verify_saved_replays_plans_without_a_model`.
- Aggregation override.
  When the rules read an average, mean, median, ratio, percent of patients, or per patient, the question goes to the open path even if Jev is sure of a metric.
  The routing study found the decision model alone answering "average weekly therapy time per patient" as plain minutes.
  No renderer computes an average.
  This rule has no unit test yet.
  The routing result for PARAPHRASE-15 is its only check.
- Collection limit.
  A question that names no patient covers every patient.
  An answer lists at most 50 patients one by one and then counts the rest.
  The consecutive-shortfall question is one SQL window over the `weeks` table.
  The review of the previous attempt measured 50.6 s and 2.6 million characters for one collection question at 2,001 patients.
  Check: `test_collection_answers_give_counts_the_first_50_patients_and_how_many_more`.
- Open questions.
  The agent has four tools (`search_source`, `source_lines`, `encounters`, `weeks`) and at most 8 model calls.
  Any number in its answer that no tool output holds is listed as unbound.
  Any citation that points at no stored line is listed as broken.
  The verdict names both.
  Check: `test_open_questions_go_to_the_agent_and_numbers_must_come_from_tools`.

## Storage (`store.py`, `pipeline.py`)

- A document is its normalized text.
  A byte-order mark, line endings, and trailing spaces are removed.
  Leading blank lines stay, so line numbers still match the file.
  A duplicate hashes to the same document, costs nothing, and changes nothing.
  Check: `test_duplicate_file_changes_nothing_and_costs_nothing`.
- Extraction rows are append-only, keyed by the document hash and the extractor version.
  The version is 12 hex characters of sha256 over the system prompt, the tool schema, and the model.
  A new prompt or model adds rows.
  Old rows stay, so a rollback is reading the old version.
  Check: `test_extractor_version_follows_the_model_the_prompt_and_the_tool`.
- Fingerprint.
  Each patient stores a hash of its sorted (document hash, version, label check) pairs and the hashes still pending.
  After an ingest, a patient whose fingerprint changed is rebuilt.
  This one rule covers a new document, a retried extraction, a new label check, and a new version.
  Only patients marked stale in the same transaction as a change are examined.
  Checks: `test_version_change_rebuilds_every_touched_patient`, `test_failed_extraction_marks_incomplete_then_rebuilds`.
- Failure isolation.
  A failed extraction goes to the `failures` table with its attempt count, and the batch goes on.
  The budget cap stops the batch.
  The next run retries the failed documents.
  Check: `test_unread_documents_leave_the_patient_incomplete_and_a_budget_cap_stops_the_batch`.
- Label-check backfill.
  Extracted documents with no label check are second-read at the next ingest.
  A Jev failure leaves a document unchecked, not unextracted.
  Jev answers are cached by request body, so a read after a rule change costs nothing.
  Check: `test_backfill_reads_extracted_documents_that_have_no_label_check`.
- Derived tables.
  `replace_patient` swaps one patient's rows in one transaction.
  Any patient can be rebuilt from the extractions with no model call.
  Indexes cover `documents(patient_id)`, `encounters(mrn, service_date)`, `weeks(mrn, week_start)`, and `flags(mrn)`.
  The write path loads only the touched patient's documents.
  Check: `test_one_document_ingest_does_not_scale_with_the_collection`.
- Tables: `files`, `documents`, `extractions`, `label_checks`, `failures`, `stale_patients`, `patients` (the abstraction JSON, the fingerprint, and the pending list), the derived `encounters`, `weeks`, `flags`, `assessments`, `observations`, and `answers`.
- Patient ids.
  A labelled pattern (`MRN`, `Record #`, `Medical record`) gives the provisional id at registration.
  The model's `patient_mrn` replaces it only when the text prints that id as a whole token.
  A document with no id is listed as unassigned, and `ingest` exits 1.
  Check: `test_document_identity_patient_follows_the_extraction_and_ids_stay_unique`.
- SQLite runs in WAL mode, with one writer per file and threads inside one process.
  Postgres is the next step when more than one process must write.
- Exports.
  Each rebuilt patient gets `outputs/abstraction/<MRN>.json` for code and `.md` for a reviewer.

## Evals

- Gold gates.
  `evals/gold/HG-M042.json` has 95 checks and `evals/gold/NF-20817.json` has 53.
  Every check is exact, and there is no judge model.
  `evals/score.py` is unchanged from the previous attempt.
  The coding assistant derived the HG-M042 labels, and a person has not confirmed them yet (`docs/gold-checklist.md`).
  The NF-20817 values were written with the held-out documents before the pipeline saw them.
- Synthetic truth (`evals/synthetic.py`).
  A seeded generator draws the truth first: the treatment plan, each contact with its real intervals, breaks, disconnects, partner-only minutes, PHQ-9 administrations, and hazards at fixed rates.
  Documents are rendered from that truth in three templates.
  The gold weeks come from the generator's own arithmetic, not from `calculate.weekly`, so the scorer does not depend on the reconciler.
  `truth_extractions()` renders what a perfect extractor reports, so the reconciler is scored with no model.
  Unsettled conflicts sit so that the week straddles the goal (5 of 100 weeks are `cannot_determine`).
  Checks: `test_truth_replay_scores_perfectly`, `test_unsettled_conflicts_straddle_the_weekly_goal`, `test_generation_is_deterministic_and_complete`.
- Routing study (`evals/experiments.py routing`).
  Three arms (rules, decision model, both) read 54 questions.
  Four scores: exact metric, exact attendance, exact date range, and `no_wrong_plain_computed` (0.0 only when a wrong plan prints a plain COMPUTED).
  Check: `test_the_three_arms_differ_where_the_readers_differ`.
- Tier study (`evals/experiments.py tier`).
  It scores the abstractions in a store against the gold files, with the cost of the extractions behind them.
- Langfuse mirror.
  When Langfuse keys exist, `record()` also sends each run to Langfuse as a dataset run (datasets `routing` and `extraction-tier`).
  The JSON files in `outputs/experiments/` are the record.
  Langfuse holds fictional or synthetic records only.
  Check: `test_record_is_skipped_without_keys`.
- No Lean kernel (decision D5.1).
  A proof checker certifies a derivation.
  It does not show that an extracted fact is true to the document, and every failure so far sat in extraction or in reconciliation inputs.
  Seeded property checks cover the range arithmetic.
- Tests never call a model.
  `tests/conftest.py` makes `llm.call_anthropic` and `jev.default_transport` raise, and `tests/fakes.py` supplies the fakes.

## Decided but not built

- Extractor versions per document type or schema section, and a shadow run on a sample before a backfill (D2.4).
  One version covers the whole prompt and schema.
- Batch inference at half price for backfills (D3.4).
- Postgres, hash-partitioned by tenant, with a materialized weeks table (D3.3).
- An OCR stage.
  A misread would pass the line check, because the text itself is wrong, so OCR confidence would have to feed uncertainty.
- A clarifying question when the two readers disagree.
- Human confirmation of the gold labels (D4.5).
