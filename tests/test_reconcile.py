"""Reconciliation and weekly calculation on a ten-document patient whose citations match its text line for line."""

import random
from dataclasses import replace

import pytest

from clinical_abstraction import calculate, verify
from clinical_abstraction.reconcile import build_patient
from clinical_abstraction.schema import Correction
from tests.fixtures import PATIENT_LINE, document, mini_patient_docs, replace_service_date, service_event

PLAN_3_150 = {"min_days_per_week": 3, "min_minutes_per_week": 150}


@pytest.fixture
def seeded_random_cases():
    random_numbers = random.Random(0)
    cases = []
    for _ in range(1000):
        goal, low = random_numbers.randint(0, 240), random_numbers.randint(0, 240)
        cases.append((goal, low, low + random_numbers.randint(0, 60)))
    return cases


def encounters_by_id(patient: dict) -> dict:
    return {encounter["encounter"]: encounter for encounter in patient["encounters"]}


# The plan's seven tests.


def test_minutes_follow_corrections_breaks_and_conflicts():
    patient = build_patient("T-M001", mini_patient_docs())
    by_id = encounters_by_id(patient)
    assert (by_id["E1"]["minutes_low"], by_id["E1"]["minutes_high"]) == (60, 60)  # 10:00-11:15 minus the 15-minute break
    assert (by_id["E2"]["minutes_low"], by_id["E2"]["minutes_high"]) == (45, 45)
    assert (by_id["E3"]["minutes_low"], by_id["E3"]["minutes_high"]) == (40, 50) and by_id["E3"]["counts_as_therapy"] == "yes"
    assert by_id["E4"]["status"] == "no_show" and by_id["E4"]["counts_as_therapy"] == "no"
    kinds = {(flag["kind"], flag["encounter"]) for flag in patient["flags"]}
    assert ("charge_for_undelivered_service", "E4") in kinds and ("unresolved_minutes_conflict", "E3") in kinds
    conflict = next(flag for flag in patient["flags"] if flag["kind"] == "unresolved_minutes_conflict")
    assert {"D6 L3", "D7 L3"} <= set(conflict["citations"]) and "start time of E3" in conflict["settles"]


def test_order_and_copies_do_not_change_results():
    docs = mini_patient_docs()
    base = build_patient("T-M001", docs)
    assert build_patient("T-M001", list(reversed(docs))) == base
    assert build_patient("T-M001", [doc for doc in docs if doc.doc_id != "D4"])["encounters"] == base["encounters"]


def test_removing_the_correction_raises_the_overlap_flag():
    docs = [doc for doc in mini_patient_docs() if doc.doc_id != "D3"]
    assert any(flag["kind"] == "overlapping_encounters" for flag in build_patient("T-M001", docs)["flags"])


def test_shared_id_with_different_dates_is_a_conflict():
    docs = mini_patient_docs()
    moved = [replace_service_date(doc, "E3", "2026-01-27") if doc.doc_id == "D7" else doc for doc in docs]
    patient = build_patient("T-M001", moved)
    assert any(flag["kind"] == "date_conflict" and flag["encounter"] == "E3" for flag in patient["flags"])
    weeks = {week["week_start"]: week["status"] for week in patient["weeks"]}
    assert weeks["2026-01-26"] == "cannot_determine"


def test_weeks_three_valued_and_consecutive_runs():
    assert calculate.goal_status(PLAN_3_150, 3, 3, 145, 155)[0] == "cannot_determine"
    assert calculate.goal_status(PLAN_3_150, 3, 3, 150, 160)[0] == "met"
    assert calculate.goal_status(PLAN_3_150, 2, 2, 180, 180)[0] == "not_met"
    weeks = [
        {"week_start": "2026-01-05", "status": "not_met"},
        {"week_start": "2026-01-12", "status": "not_met"},
        {"week_start": "2026-01-19", "status": "met"},
        {"week_start": "2026-01-26", "status": "cannot_determine"},
    ]
    # A met week ends a run, so the last pair depends on nothing; the gold file HG-M042 expects exactly this.
    assert calculate.consecutive_shortfalls(weeks) == {
        "run_length": 2,
        "established": [["2026-01-05", "2026-01-12"]],
        "depends_on_unresolved_records": [],
    }
    # A shortfall next to an unresolved week is only possible; the gold file NF-20817 expects exactly this.
    weeks = [
        {"week_start": "2026-02-02", "status": "met"},
        {"week_start": "2026-02-09", "status": "not_met"},
        {"week_start": "2026-02-16", "status": "cannot_determine"},
    ]
    assert calculate.consecutive_shortfalls(weeks) == {
        "run_length": 2,
        "established": [],
        "depends_on_unresolved_records": [["2026-02-09", "2026-02-16"]],
    }


def test_incomplete_patients_carry_their_pending_documents():
    patient = build_patient("T-M001", mini_patient_docs(), pending=[{"doc_id": "D11", "reason": "rate limit"}])
    assert patient["incomplete"] == [{"doc_id": "D11", "reason": "rate limit"}]
    # A missing document never makes an answer more confident: no week is settled while one is pending.
    assert {week["status"] for week in patient["weeks"]} == {"cannot_determine"}
    assert all("D11" in week["explanation"] for week in patient["weeks"])


def test_property_ranges_hold_for_every_value(seeded_random_cases):
    for goal, low, high in seeded_random_cases:  # 1000 cases from random.Random(0)
        status, _ = calculate.goal_status({"min_minutes_per_week": goal, "min_days_per_week": 0}, 0, 0, low, high)
        if status == "met":
            assert all(value >= goal for value in (low, high))
        if status == "not_met":
            assert all(value < goal for value in (low, high))
        if status == "cannot_determine":
            assert low < goal <= high


# Rules the plan's seven tests do not pin.


def test_fixture_values_sit_in_their_cited_lines():
    for doc in mini_patient_docs():
        assert verify.admit(doc.doc_id, doc.text, doc.extraction) == (doc.extraction, []), doc.doc_id
        assert verify.coverage(doc.text, doc.extraction) == [], doc.doc_id


def test_late_copy_takes_the_correction_by_authored_time():
    # Without D2, the copy D4 is the only roster: received 2026-01-26, after the correction, but authored 2026-01-19, before it.
    patient = build_patient("T-M001", [doc for doc in mini_patient_docs() if doc.doc_id != "D2"])
    e1 = encounters_by_id(patient)["E1"]
    assert (e1["minutes_low"], e1["minutes_high"]) == (60, 60) and "D4 L1" in e1["sources"]
    assert not any(flag["kind"] == "conflict_after_correction" for flag in patient["flags"])


def test_label_dispute_holds_the_count_only_when_it_could_change_it():
    disputes = {
        "D5": {"encounter_id": "E2", "field": "attendance", "model_value": "attended", "jev_value": "no_show", "confidence": 0.9},
        "D6": {
            "encounter_id": "E3",
            "field": "attendance",
            "model_value": "attended",
            "jev_value": "attended_partial",
            "confidence": 0.85,
        },
        "D8": {
            "encounter_id": "E4",
            "field": "attendance",
            "model_value": "no_show",
            "jev_value": "cancelled_by_patient",
            "confidence": 0.8,
        },
    }
    docs = [replace(doc, disputes=[disputes[doc.doc_id]]) if doc.doc_id in disputes else doc for doc in mini_patient_docs()]
    patient = build_patient("T-M001", docs)
    by_id = encounters_by_id(patient)
    assert by_id["E2"]["counts_as_therapy"] == "disputed" and (by_id["E2"]["minutes_low"], by_id["E2"]["minutes_high"]) == (0, 45)
    assert by_id["E3"]["counts_as_therapy"] == "yes"  # attended or attended in part: delivered either way
    assert by_id["E4"]["counts_as_therapy"] == "no"  # no show or patient cancellation: not delivered either way
    flags = {flag["encounter"]: flag for flag in patient["flags"] if flag["kind"] == "label_disputed"}
    assert set(flags) == {"E2", "E3", "E4"}
    assert "D5 L3" in flags["E2"]["citations"] and "no_show" in flags["E2"]["text"] and "E2" in flags["E2"]["settles"]


def test_possible_omission_names_the_line():
    docs = [
        replace(doc, text=doc.text + "Patient stepped out 11:40-11:45.\n") if doc.doc_id == "D5" else doc
        for doc in mini_patient_docs()
    ]
    omissions = [flag for flag in build_patient("T-M001", docs)["flags"] if flag["kind"] == "possible_omission"]
    assert [(flag["encounter"], flag["citations"]) for flag in omissions] == [("E2", ["D5 L5"])]  # one flag per line
    assert "11:40 and 11:45" in omissions[0]["text"]


def test_possible_omission_only_inside_a_session_window():
    # D1 books group E1 for 10:00-11:30. A stray 10:45 sits inside that window; a signature time and a date do not.
    extra = "Room check 10:45\nSigned by the scheduler at 12:40\nRebooked for 2026-02-02\n"
    docs = [replace(doc, text=doc.text + extra) if doc.doc_id == "D1" else doc for doc in mini_patient_docs()]
    patient = build_patient("T-M001", docs)
    omissions = [flag for flag in patient["flags"] if flag["kind"] == "possible_omission"]
    assert [(flag["encounter"], flag["citations"]) for flag in omissions] == [("E1", ["D1 L4"])]
    assert "10:45" in omissions[0]["text"]
    # Every unreferenced value stays visible to a reviewer, flagged or not.
    assert patient["coverage_notes"] == [
        {"citation": "D1 L4", "value": "10:45"},
        {"citation": "D1 L5", "value": "12:40"},
        {"citation": "D1 L6", "value": "2026-02-02"},
    ]


def test_absent_words_in_a_note_marked_attended_hold_it_disputed():
    def contradicted(doc):
        event = doc.extraction.service_events[0].model_copy(update={"lines": [1, 3, 5]})
        return replace(
            doc,
            text=doc.text + "Addendum: the patient did not attend; the times above came from the schedule.\n",
            extraction=doc.extraction.model_copy(update={"service_events": [event]}),
        )

    patient = build_patient("T-M001", [contradicted(doc) if doc.doc_id == "D5" else doc for doc in mini_patient_docs()])
    assert encounters_by_id(patient)["E2"]["counts_as_therapy"] == "disputed"
    flag = next(flag for flag in patient["flags"] if flag["kind"] == "keyword_contradicts_label" and flag["encounter"] == "E2")
    assert "D5 L5" in flag["citations"] and "E2" in flag["settles"]


def test_assessments_keep_distinct_administrations_and_name_copies():
    patient = build_patient("T-M001", mini_patient_docs())
    assert [(row["completed_on"], row["total_score"], row["sources"], row["copies"]) for row in patient["assessments"]] == [
        ("2026-01-05", 18, ["D10 L3"], []),
        ("2026-01-16", 14, ["D10 L4"], ["D10 L5"]),
    ]


def test_register_rows_of_other_encounters_do_not_contradict_this_one():
    register = document(
        "D11",
        [
            "Attendance register",
            PATIENT_LINE,
            "E1 | 2026-01-19 | group | attended 10:00-11:15",
            "E4 | 2026-01-27 | group | no show; the patient did not attend",
        ],
        "attendance register",
        "2026-01-28T09:00",
        service_events=[
            # The E1 row cites the E4 row too, as a model may cite a whole table.
            service_event(
                encounter_id="E1",
                record_role="attendance_record",
                patient_arrival="10:00",
                patient_departure="11:15",
                lines=[1, 3, 4],
            ),
            service_event(
                encounter_id="E4", service_date="2026-01-27", attendance="no_show", record_role="attendance_record", lines=[1, 4]
            ),
        ],
    )
    assert verify.admit("D11", register.text, register.extraction)[1] == []
    patient = build_patient("T-M001", [*mini_patient_docs(), register])
    assert encounters_by_id(patient)["E1"]["counts_as_therapy"] == "yes"
    assert not any(flag["kind"] == "keyword_contradicts_label" and flag["encounter"] == "E1" for flag in patient["flags"])


def test_correction_without_an_id_goes_to_the_encounter_holding_the_replaced_value():
    # E1 and E2 share 2026-01-19; only the E1 roster states the replaced departure 11:30.
    docs = mini_patient_docs()
    d3 = next(doc for doc in docs if doc.doc_id == "D3")
    unnamed = d3.extraction.corrections[0].model_copy(update={"encounter_id": None})
    docs = [
        replace(doc, extraction=doc.extraction.model_copy(update={"corrections": [unnamed]})) if doc is d3 else doc
        for doc in docs
    ]
    patient = build_patient("T-M001", docs)
    by_id = encounters_by_id(patient)
    assert (by_id["E1"]["minutes_low"], by_id["E2"]["minutes_low"]) == (60, 45)
    assert not any(flag["kind"] == "correction_not_applied" for flag in patient["flags"])


def test_time_correction_labelled_other_applies_to_the_start_or_end_it_replaces():
    def addendum(corrected: str, replaced: str):
        return document(
            "D11",
            [
                "Addendum to individual psychotherapy note - E3 - 2026-01-26",
                PATIENT_LINE,
                f"Patient contact for E3 began at {corrected}, replacing the start time of {replaced} in the original note.",
                "Signed by J. Ortiz, LCSW on 2026-01-31 10:05",
            ],
            "addendum",
            "2026-01-31T10:05",
            corrections=[
                Correction(
                    encounter_id="E3",
                    service_date="2026-01-26",
                    field="other",
                    corrected_value=corrected,
                    replaced_value=replaced,
                    lines=[3],
                )
            ],
        )

    settling = addendum("09:10", "09:00")  # 09:00 is the start D6 states, so it corrects the arrival
    assert verify.admit("D11", settling.text, settling.extraction)[1] == []
    patient = build_patient("T-M001", [*mini_patient_docs(), settling])
    e3 = encounters_by_id(patient)["E3"]
    assert (e3["minutes_low"], e3["minutes_high"]) == (40, 40) and "D11 L3" in e3["sources"]
    assert not any(flag["kind"] in ("correction_not_applied", "unresolved_minutes_conflict") for flag in patient["flags"])
    # 09:30 is no start or end of E3, so the correction is not guessed: the flag and the range stay.
    patient = build_patient("T-M001", [*mini_patient_docs(), addendum("09:20", "09:30")])
    e3 = encounters_by_id(patient)["E3"]
    assert (e3["minutes_low"], e3["minutes_high"]) == (40, 50)
    assert any(flag["kind"] == "correction_not_applied" and flag["encounter"] == "E3" for flag in patient["flags"])


def test_stated_minutes_on_a_signed_note_without_times_establish_delivery():
    def service_record(signed: bool):
        return document(
            "D11",
            [
                "Service record - E5 - individual psychotherapy - 2026-01-21",
                PATIENT_LINE,
                "Patient contact: 14:00-14:45 | Completed: 45 minutes",
                "Signed by J. Ortiz, LCSW on 2026-01-21 15:10",
            ],
            "service record",
            "2026-01-21T15:10",
            service_events=[
                # As the model read it: the contact times went to the schedule fields and attendance was not stated.
                service_event(
                    encounter_id="E5",
                    service_date="2026-01-21",
                    service_type="individual_psychotherapy",
                    attendance="not_stated",
                    scheduled_start="14:00",
                    scheduled_end="14:45",
                    stated_patient_minutes=45,
                    signed=signed,
                    lines=[1, 3],
                )
            ],
        )

    signed = service_record(True)
    assert verify.admit("D11", signed.text, signed.extraction)[1] == []
    e5 = encounters_by_id(build_patient("T-M001", [*mini_patient_docs(), signed]))["E5"]
    assert (e5["status"], e5["counts_as_therapy"], e5["minutes_low"], e5["minutes_high"]) == ("attended", "yes", 45, 45)
    assert e5["calculation"] == "45 minutes as stated (no times given)"
    unsigned = encounters_by_id(build_patient("T-M001", [*mini_patient_docs(), service_record(False)]))["E5"]
    assert (unsigned["counts_as_therapy"], unsigned["minutes_high"]) == ("no", 0)
