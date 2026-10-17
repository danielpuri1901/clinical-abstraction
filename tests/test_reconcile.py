"""The referee, reconciliation, and the weekly calculation in reconcile.py.

The referee checks every value the model cites against the source text and never repairs one. Reconciliation and the
calculation run on a ten-document patient whose citations match its text line for line.
"""

import random
from dataclasses import replace
from pathlib import Path

import pytest

from clinical_abstraction import reconcile
from clinical_abstraction.reconcile import build_patient
from clinical_abstraction.schema import (
    Assessment,
    Authorization,
    Charge,
    Correction,
    DocumentExtraction,
    ExcludedInterval,
    Interval,
    ItemScore,
    Observation,
    PlanRequirement,
    ServiceEvent,
)
from tests.fixtures import PATIENT_LINE, document, mini_patient_docs, replace_service_date, service_event

ROOT = Path(__file__).resolve().parent.parent


def event(**fields) -> ServiceEvent:
    return ServiceEvent(
        **{
            "encounter_id": None,
            "appointment_ids": [],
            "service_date": "2026-01-19",
            "service_type": "group_psychotherapy",
            "modality": "in_person",
            "scheduled_start": None,
            "scheduled_end": None,
            "attendance": "attended",
            "patient_arrival": None,
            "patient_departure": None,
            "patient_present_intervals": [],
            "stated_patient_minutes": None,
            "excluded_intervals": [],
            "record_role": "attendance_record",
            "signed": True,
            "clinicians": [],
            "lines": [1],
            **fields,
        }
    )


def correction(**fields) -> Correction:
    return Correction(
        **{
            "encounter_id": None,
            "service_date": None,
            "field": "patient_departure",
            "corrected_value": "11:15",
            "replaced_value": None,
            "lines": [1],
            **fields,
        }
    )


def assessment(**fields) -> Assessment:
    return Assessment(
        **{
            "instrument": "PHQ-9",
            "total_score": None,
            "item_scores": [],
            "completed_on": None,
            "completed_time": None,
            "source_form_id": None,
            "is_copy_of_earlier_result": False,
            "lines": [1],
            **fields,
        }
    )


def authorization(**fields) -> Authorization:
    return Authorization(
        **{
            "authorization_id": "HG-A1",
            "service_type": "group_psychotherapy",
            "quantity": 8,
            "unit": "sessions",
            "start": None,
            "end": None,
            "lines": [1],
            **fields,
        }
    )


def charge(**fields) -> Charge:
    return Charge(
        **{
            "charge_id": "CH-1",
            "encounter_id": None,
            "service_date": None,
            "description": "Group psychotherapy",
            "quantity": 1,
            "status": "Posted",
            "lines": [1],
            **fields,
        }
    )


def observation(**fields) -> Observation:
    return Observation(**{"observed_on": "2026-01-19", "category": "sleep", "statement": "Slept poorly.", "lines": [1], **fields})


def plan(**fields) -> PlanRequirement:
    return PlanRequirement(
        **{
            "effective_start": None,
            "effective_end": None,
            "min_therapy_days_per_week": None,
            "min_patient_present_minutes_per_week": None,
            "week_definition": None,
            "counted_service_types": ["individual_psychotherapy"],
            "not_counted_service_types": [],
            "requires_patient_present": True,
            "lines": [1],
            **fields,
        }
    )


def extraction(**fields) -> DocumentExtraction:
    return DocumentExtraction(
        **{
            "document_id": "D1",
            "patient_name": None,
            "patient_dob": None,
            "patient_mrn": None,
            "document_type": "test record",
            "authored_at": None,
            "received_at": None,
            "is_copy_or_retransmission": False,
            "copy_lines": [],
            "final_or_signed": True,
            "service_events": [],
            "corrections": [],
            "plan_requirements": [],
            "assessments": [],
            "authorizations": [],
            "charges": [],
            "observations": [],
            **fields,
        }
    )


def test_numbered_keeps_blank_lines_so_citations_line_up():
    assert reconcile.numbered("first\nsecond\n\nfourth\n") == "L1: first\nL2: second\nL3: \nL4: fourth"


def test_cited_lines_are_sorted_and_unique():
    assert reconcile.cited_lines(event(lines=[3, 1, 3])) == [1, 3]
    assert reconcile.cited_lines(extraction()) == []


def test_times_and_dates_normalize():
    assert reconcile.normalize_time("9:00 AM") == "09:00" and reconcile.normalize_time("1415") == "14:15"
    assert reconcile.normalize_time("09:50") == "09:50" and reconcile.normalize_time("25:00") is None
    assert reconcile.normalize_date("30 January 2026") == "2026-01-30"
    assert reconcile.normalize_date("January 19, 2026") == "2026-01-19" == reconcile.normalize_date("01/19/2026")
    assert reconcile.normalize_date("Jan 5", default_year=2026) == "2026-01-05"
    assert reconcile.normalize_date("Jan05", default_year=2026) == "2026-01-05"  # the appointment export writes dates this way
    assert reconcile.value_in_lines("2026-01-05", "date", ["HG-E101   | Jan05 | Individual therapy"])
    assert reconcile.normalize_date("19th of January 2026") == "2026-01-19"
    assert reconcile.normalize_date("February 30, 2026") is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("12:00 AM", "00:00"),
        ("12:30 PM", "12:30"),
        ("1:05 pm", "13:05"),
        ("11:59 p.m.", "23:59"),
        ("0905", "09:05"),
        ("13:00 PM", None),
        ("24:00", None),
        ("9:75", None),
        ("noon", None),
        ("", None),
        ("at 9:00", None),
        ("9:00 and 9:30", None),
        (" 09:50 ", "09:50"),
    ],
)
def test_clock_edges_and_malformed_times(value, expected):
    assert reconcile.normalize_time(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-1-5", "2026-01-05"),
        ("5 Jan 2026", "2026-01-05"),
        ("Sept. 3, 2026", "2026-09-03"),
        ("May 5th, 2026", "2026-05-05"),
        ("29 February 2028", "2028-02-29"),
        ("29 February 2026", None),
        ("13/01/2026", None),
        ("2026-02-30", None),
        ("Jan 5", None),
        ("soon", None),
        ("on January 19, 2026", None),
        ("2026-01-19T12:08", None),
        (" 2026-01-19 ", "2026-01-19"),
    ],
)
def test_date_edges_and_impossible_dates(value, expected):
    assert reconcile.normalize_date(value) == expected


def test_value_must_sit_in_its_own_cited_lines():
    lines = ["Scheduled group: 10:00-11:30.", "Patient arrival: 10:00 AM | Patient departure: 11:15"]
    assert reconcile.value_in_lines("11:15", "time", [lines[1]])
    assert not reconcile.value_in_lines("11:30", "time", [lines[1]])
    assert reconcile.value_in_lines("2026-01-30", "date", ["Service date: 30 January 2026"])


@pytest.mark.parametrize(
    "line, kind, value",
    [
        ("Start 9:00 AM | End 9:50 AM", "time", "09:00"),
        ("Start 9:00 AM | End 9:50 AM", "time", "09:50"),
        ("Signed out 2:15 pm", "time", "14:15"),
        ("Time in 0900 | Time out 1415", "time", "09:00"),
        ("Time in 0900 | Time out 1415", "time", "14:15"),
        ("Contact 10:00–10:50 local", "time", "10:50"),
        ("Date of service: 30 January 2026", "date", "2026-01-30"),
        ("held on the 19th of January 2026", "date", "2026-01-19"),
        ("Date of service: 02/18/2026", "date", "2026-02-18"),
        ("Signed Jan. 5, 2026", "date", "2026-01-05"),
        ("Monday, January 19, 2026, 12:08", "date", "2026-01-19"),
        ("January 22 | HG-E113 | Skills group", "date", "2026-01-22"),
        ("Seen February 29", "date", "2028-02-29"),
        ("Episode dates: 2026-01-05 through 2026-01-30", "date", "2026-01-30"),
    ],
)
def test_document_formats_are_accepted_not_dropped(line, kind, value):
    assert reconcile.value_in_lines(value, kind, [line])


@pytest.mark.parametrize(
    "line, kind, value",
    [
        ("Start 9:00 AM | End 9:50 AM", "time", "21:00"),
        ("Time in 0900", "time", "09:50"),
        ("Arrival 25:00", "time", "25:00"),
        ("Signed January 19, 2026", "time", "20:26"),
        ("Authorization HG-MEAS-0126", "time", "01:26"),
        ("Service date: January 19, 2026", "date", "2026-01-20"),
        ("Service date: January 19, 2025", "date", "2026-01-19"),
        ("January 22 | HG-E113", "date", "2026-01-23"),
        ("Service date: February 30, 2026", "date", "2026-02-30"),
        ("Session 3 may start late", "date", "2026-05-03"),
        ("Group 5 Marching drill", "date", "2026-03-05"),
        ("Amar 5 attended", "date", "2026-03-05"),
        ("Encounter 110 January 2026 follow-up", "date", "2026-01-10"),
        ("Dose 14150 mg", "time", "14:15"),
        ("Reference 1415.5", "time", "14:15"),
        ("Invoice #1415", "time", "14:15"),
        ("Cost $1415", "time", "14:15"),
        ("Ratio 10:305", "time", "10:30"),
    ],
)
def test_values_absent_from_the_text_are_rejected(line, kind, value):
    assert not reconcile.value_in_lines(value, kind, [line])


def test_numbers_must_be_stated_not_read_out_of_times_dates_or_names():
    assert reconcile.value_in_lines("50", "number", ["Actual contact: 09:00–09:50, 50 minutes."])
    assert not reconcile.value_in_lines("50", "number", ["Actual contact: 09:00–09:50."])
    assert not reconcile.value_in_lines("22", "number", ["January 22 | HG-E113 | Skills group"])
    assert not reconcile.value_in_lines("5", "number", ["Total 3.5 hours"]) and not reconcile.value_in_lines(
        "3", "number", ["Total 3.5 hours"]
    )
    assert reconcile.value_in_lines("45", "number", ["Total 45 minutes"]) and reconcile.value_in_lines(
        "45 minutes", "number", ["Total 45"]
    )
    assert reconcile.value_in_lines("14", "number", ["Instrument: PHQ-9 | Total score: 14"])
    assert not reconcile.value_in_lines("9", "number", ["Instrument: PHQ-9 | Total score: 14"])
    assert reconcile.value_in_lines("0", "number", ["PHQ-9 total: 10. Item 9: 0."])


def test_text_ignores_case_whitespace_and_underscores():
    assert reconcile.value_in_lines("no show", "text", ["Final disposition:  NO   Show; patient did not attend"])
    assert reconcile.value_in_lines("no_show", "text", ["No show"])
    assert not reconcile.value_in_lines("attended", "text", ["No show"])
    assert not reconcile.value_in_lines("  ", "text", ["anything at all"])


def test_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        reconcile.value_in_lines("x", "color", ["x"])


def test_admit_drops_unsupported_values_and_reports_them():
    text = "Scheduled group: 10:00-11:30.\nPatient arrival: 10:00 | Patient departure: 11:15\nService date: January 19, 2026\n"
    raw = event(patient_arrival="10:00", patient_departure="11:30", service_date="2026-01-19", lines=[2, 3])
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[raw]))
    assert kept.service_events[0].patient_departure is None and kept.service_events[0].patient_arrival == "10:00"
    assert problems == [reconcile.Problem("D1", "service_events[0]", "patient_departure", "11:30 not in cited lines 2-3")]
    assert raw.patient_departure == "11:30"  # the model's own record stays untouched for the audit trail


def test_scheduled_times_are_checked_like_actual_times():
    text = "January 19, 2026 | Scheduled group: 10:00-11:30.\n"
    booked = event(scheduled_start="10:00", scheduled_end="11:45", lines=[1])
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[booked]))
    assert (kept.service_events[0].scheduled_start, kept.service_events[0].scheduled_end) == ("10:00", None)
    assert problems == [reconcile.Problem("D1", "service_events[0]", "scheduled_end", "11:45 not in cited lines 1")]


def test_admit_keeps_day_first_dates_twelve_hour_and_bare_times():
    text = "Date of service: 30 January 2026\nStart 9:00 AM | End 9:50 AM\nIn 0900 | Out 0950 | 50 minutes\n"
    booked = event(service_date="2026-01-30", scheduled_start="09:00", scheduled_end="09:50", lines=[2])
    seen = event(
        service_date="2026-01-30", patient_arrival="09:00", patient_departure="09:50", stated_patient_minutes=50, lines=[3]
    )
    raw = extraction(service_events=[booked, seen])
    kept, problems = reconcile.admit("D1", text, raw)
    assert problems == [] and kept == raw


def test_interval_with_one_unsupported_endpoint_is_dropped_whole():
    text = "January 19, 2026: patient present 10:00-10:50 and 11:00-11:40.\n"
    present = [Interval(start="10:00", end="10:50"), Interval(start="11:00", end="11:20")]
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[event(patient_present_intervals=present)]))
    assert kept.service_events[0].patient_present_intervals == [Interval(start="10:00", end="10:50")]
    assert problems == [
        reconcile.Problem("D1", "service_events[0].patient_present_intervals[1]", "end", "11:20 not in cited lines 1")
    ]


def test_excluded_interval_is_checked_against_its_own_lines():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    good = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[2])
    wrong_line = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[1])
    kept, problems = reconcile.admit(
        "D1", text, extraction(service_events=[event(excluded_intervals=[good, wrong_line], lines=[1])])
    )
    assert kept.service_events[0].excluded_intervals == [good]
    assert problems == [
        reconcile.Problem("D1", "service_events[0].excluded_intervals[1]", "start", "10:45 not in cited lines 1"),
        reconcile.Problem("D1", "service_events[0].excluded_intervals[1]", "end", "11:00 not in cited lines 1"),
    ]


def test_item_citing_missing_or_no_lines_is_dropped():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[event(lines=[1, 9]), event(lines=[]), event(lines=[1])]))
    assert len(kept.service_events) == 1
    assert problems == [
        reconcile.Problem("D1", "service_events[0]", "lines", "lines 9 not in document of 2 lines"),
        reconcile.Problem("D1", "service_events[1]", "lines", "cites no lines"),
    ]


def test_excluded_interval_citing_a_missing_line_is_dropped():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    before_the_document = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[0])
    kept, problems = reconcile.admit(
        "D1", text, extraction(service_events=[event(excluded_intervals=[before_the_document], lines=[1])])
    )
    assert kept.service_events[0].excluded_intervals == []
    assert problems == [
        reconcile.Problem("D1", "service_events[0].excluded_intervals[0]", "lines", "lines 0 not in document of 2 lines")
    ]


@pytest.mark.parametrize(
    "collection, item",
    [
        ("corrections", correction(lines=[9])),
        ("plan_requirements", plan(lines=[9])),
        ("assessments", assessment(lines=[9])),
        ("authorizations", authorization(lines=[9])),
        ("charges", charge(lines=[9])),
        ("observations", observation(lines=[9])),
    ],
)
def test_every_kind_of_item_must_cite_real_lines(collection, item):
    kept, problems = reconcile.admit("D1", "January 19, 2026 only line\n", extraction(**{collection: [item]}))
    assert getattr(kept, collection) == []
    assert problems == [reconcile.Problem("D1", f"{collection}[0]", "lines", "lines 9 not in document of 1 lines")]


def test_dates_may_sit_on_any_line_but_must_exist():
    text = "Service date: January 19, 2026\nPatient arrival: 10:00\n"
    found = event(service_date="2026-01-19", patient_arrival="10:00", lines=[2])
    invented = event(service_date="2026-01-20", patient_arrival="10:00", lines=[2])
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[found, invented]))
    assert kept.service_events == [found]
    assert problems == [reconcile.Problem("D1", "service_events[1]", "service_date", "2026-01-20 not in document")]


def test_correction_with_unsupported_value_is_dropped_and_the_good_one_kept():
    text = "Correction: departure is 11:15, replacing 11:30 (January 19, 2026).\n"
    bad = correction(corrected_value="11:20", replaced_value="11:30", service_date="2026-01-19")
    good = correction(corrected_value="11:15", replaced_value="11:30", service_date="2026-01-19")
    kept, problems = reconcile.admit("D1", text, extraction(corrections=[bad, good]))
    assert kept.corrections == [good]
    assert problems == [reconcile.Problem("D1", "corrections[0]", "corrected_value", "11:20 not in cited lines 1")]


def test_correction_minutes_are_a_stated_number_not_a_substring():
    text = "January 26, 2026 correction: patient arrived 09:40.\n"
    changed = correction(field="patient_minutes", corrected_value="40", service_date="2026-01-26")
    kept, problems = reconcile.admit("D1", text, extraction(corrections=[changed]))
    assert kept.corrections == []
    assert problems == [reconcile.Problem("D1", "corrections[0]", "corrected_value", "40 not in cited lines 1")]


def test_correction_date_and_replaced_value_are_checked_but_do_not_drop_it():
    text = "Correction: departure is 11:15, replacing 11:30 (January 19, 2026).\n"
    changed = correction(corrected_value="11:15", replaced_value="11:45", service_date="2026-01-20")
    kept, problems = reconcile.admit("D1", text, extraction(corrections=[changed]))
    assert (kept.corrections[0].corrected_value, kept.corrections[0].service_date, kept.corrections[0].replaced_value) == (
        "11:15",
        None,
        None,
    )
    assert problems == [
        reconcile.Problem("D1", "corrections[0]", "service_date", "2026-01-20 not in document"),
        reconcile.Problem("D1", "corrections[0]", "replaced_value", "11:45 not in cited lines 1"),
    ]


@pytest.mark.parametrize(
    "field, value, supported",
    [
        ("patient_arrival", "09:10", True),
        ("patient_arrival", "9:10 AM", True),
        ("patient_arrival", "09:15", False),
        ("patient_minutes", "40", True),
        ("patient_minutes", "40 minutes", True),
        ("patient_minutes", "45", False),
        ("attendance", "attended", True),
        ("attendance", "no_show", False),
    ],
)
def test_correction_value_is_checked_by_the_kind_of_field(field, value, supported):
    text = "January 26, 2026 correction: began at 09:10, 40 minutes, attendance changed to attended.\n"
    changed = correction(field=field, corrected_value=value, service_date="2026-01-26")
    kept, problems = reconcile.admit("D1", text, extraction(corrections=[changed]))
    assert (len(kept.corrections) == 1) is supported and (problems == []) is supported


def test_scores_and_completion_must_sit_in_the_assessment_lines():
    text = "Instrument: PHQ-9 | Completed by patient: 2026-01-16, 08:17 local\nTotal score: 18. Item 9: 0.\n"
    stated = assessment(
        total_score=18,
        item_scores=[ItemScore(item="9", score=0)],
        completed_on="2026-01-16",
        completed_time="08:17",
        lines=[1, 2],
    )
    invented = assessment(
        total_score=14,
        item_scores=[ItemScore(item="9", score=0), ItemScore(item="1", score=3)],
        completed_on="2026-01-17",
        completed_time="08:19",
        lines=[1, 2],
    )
    kept, problems = reconcile.admit("D1", text, extraction(assessments=[stated, invented]))
    assert kept.assessments[0] == stated
    checked = kept.assessments[1]
    assert checked.total_score is None and checked.item_scores == [ItemScore(item="9", score=0)]
    assert (checked.completed_on, checked.completed_time) == (None, None)
    assert problems == [
        reconcile.Problem("D1", "assessments[1]", "total_score", "14 not in cited lines 1-2"),
        reconcile.Problem("D1", "assessments[1].item_scores[1]", "score", "3 not in cited lines 1-2"),
        reconcile.Problem("D1", "assessments[1]", "completed_on", "2026-01-17 not in document"),
        reconcile.Problem("D1", "assessments[1]", "completed_time", "08:19 not in cited lines 1-2"),
    ]


def test_stated_minutes_must_be_stated_in_the_cited_lines():
    text = "January 19, 2026: contact 09:00-09:50, 50 minutes.\nBilling shows 45 minutes.\n"
    kept, problems = reconcile.admit("D1", text, extraction(service_events=[event(stated_patient_minutes=45, lines=[1])]))
    assert kept.service_events[0].stated_patient_minutes is None
    assert problems == [reconcile.Problem("D1", "service_events[0]", "stated_patient_minutes", "45 not in cited lines 1")]


def test_plan_thresholds_and_dates_must_exist_in_the_plan_text():
    text = (
        "Episode dates: 2026-01-05 through 2026-01-30\n"
        "Goal: at least 3 therapy days and at least 120 minutes in each Monday-Sunday week.\n"
    )
    raw = plan(
        effective_start="2026-01-05",
        effective_end="2026-01-31",
        min_therapy_days_per_week=3,
        min_patient_present_minutes_per_week=150,
        lines=[2],
    )
    kept, problems = reconcile.admit("D1", text, extraction(plan_requirements=[raw]))
    checked = kept.plan_requirements[0]
    assert (checked.min_therapy_days_per_week, checked.min_patient_present_minutes_per_week) == (3, None)
    assert (checked.effective_start, checked.effective_end) == ("2026-01-05", None)
    assert problems == [
        reconcile.Problem("D1", "plan_requirements[0]", "effective_end", "2026-01-31 not in document"),
        reconcile.Problem("D1", "plan_requirements[0]", "min_patient_present_minutes_per_week", "150 not in cited lines 2"),
    ]


def test_other_item_dates_must_exist_in_the_document():
    text = (
        "Authorization HG-A1: approved January 5, 2026 through January 30, 2026.\n"
        "Charge CH-1 posted for January 27, 2026.\n"
        "Slept poorly.\n"
    )
    approval = authorization(start="2026-01-05", end="2026-01-31", lines=[1])
    posted = charge(service_date="2026-01-28", lines=[2])
    seen = observation(observed_on="2026-01-15", lines=[3])
    kept, problems = reconcile.admit("D1", text, extraction(authorizations=[approval], charges=[posted], observations=[seen]))
    assert (kept.authorizations[0].start, kept.authorizations[0].end) == ("2026-01-05", None)
    assert kept.charges[0].service_date is None and kept.observations == []
    assert [(p.item, p.field) for p in problems] == [
        ("authorizations[0]", "end"),
        ("charges[0]", "service_date"),
        ("observations[0]", "observed_on"),
    ]


def test_real_group_record_is_admitted_and_a_dropped_break_is_named():
    group = (ROOT / "documents" / "BH-D101_group_content_2026-01-19.txt").read_text(encoding="utf-8")
    meeting = event(
        service_date="2026-01-19",
        scheduled_start="10:00",
        scheduled_end="11:30",
        lines=[3, 6],
        excluded_intervals=[ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[6])],
    )
    whole = extraction(patient_dob="1991-04-12", authored_at="2026-01-19T12:08", service_events=[meeting])
    assert reconcile.admit("BH-D101", group, whole) == (whole, [])
    assert reconcile.coverage(group, whole) == []
    no_break = extraction(
        patient_dob="1991-04-12",
        authored_at="2026-01-19T12:08",
        service_events=[event(service_date="2026-01-19", scheduled_start="10:00", scheduled_end="11:30", lines=[3, 6])],
    )
    assert reconcile.coverage(group, no_break) == ["possible omission: L6 10:45", "possible omission: L6 11:00"]


def test_real_held_out_note_with_numeric_dates_is_admitted():
    note = (ROOT / "examples" / "held_out_patient" / "NF_skills_group_2026-02-04.txt").read_text(encoding="utf-8")
    skills = event(
        service_date="2026-02-04",
        scheduled_start="13:00",
        scheduled_end="14:30",
        patient_arrival="13:00",
        patient_departure="14:30",
        lines=[4, 5, 6],
        excluded_intervals=[ExcludedInterval(start="13:45", end="14:00", reason="break", lines=[6])],
    )
    held_out = extraction(authored_at="2026-02-04T15:10", service_events=[skills])
    assert reconcile.admit("NF-D1", note, held_out) == (held_out, [])
    assert reconcile.coverage(note, held_out) == []


def test_real_correction_record_is_admitted():
    text = (ROOT / "documents" / "BH-D103_attendance_correction_2026-01-20.txt").read_text(encoding="utf-8")
    fixed = extraction(
        corrections=[correction(service_date="2026-01-19", corrected_value="11:15", replaced_value="11:30", lines=[7])]
    )
    assert reconcile.admit("BH-D103", text, fixed) == (fixed, [])


def test_coverage_names_unreferenced_times():
    text = "Actual contact: 09:10-09:50.\nRowan entered the room at 09:10.\n"
    raw = event(patient_present_intervals=[Interval(start="09:10", end="09:50")], lines=[1])
    assert reconcile.coverage(text, extraction(service_events=[raw])) == []
    assert reconcile.coverage("Break 10:45-11:00.\n" + text, extraction(service_events=[raw])) == [
        "possible omission: L1 10:45",
        "possible omission: L1 11:00",
    ]


def test_coverage_names_unreferenced_dates():
    text = "Service date: January 19, 2026\nFollow-up on January 22 and review on 2026-02-03.\n"
    assert reconcile.coverage(text, extraction(service_events=[event(service_date="2026-01-19")])) == [
        "possible omission: L2 January 22",
        "possible omission: L2 2026-02-03",
    ]


def test_coverage_matches_values_written_in_any_format():
    text = "Date of service: 30 January 2026\nStart 9:00 AM | End 2:15 PM | seen January 30\nSigned 2026-01-30T16:05\n"
    seen = event(service_date="2026-01-30", patient_present_intervals=[Interval(start="09:00", end="14:15")])
    assert reconcile.coverage(text, extraction(authored_at="2026-01-30T16:05", service_events=[seen])) == []


def test_coverage_reports_each_value_once_per_line_in_document_order():
    assert reconcile.coverage("Break 10:45-11:00, resumed 10:45.\n", extraction()) == [
        "possible omission: L1 10:45",
        "possible omission: L1 11:00",
    ]


def test_coverage_does_not_read_years_or_identifiers_as_times():
    text = "Imported January 26, 2026 from batch HG-MEAS-0126, form HG-Q116.\n"
    assert reconcile.coverage(text, extraction(service_events=[event(service_date="2026-01-26")])) == []


# ---- Reconciliation and calculation ----


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
    assert reconcile.goal_status(PLAN_3_150, 3, 3, 145, 155)[0] == "cannot_determine"
    assert reconcile.goal_status(PLAN_3_150, 3, 3, 150, 160)[0] == "met"
    assert reconcile.goal_status(PLAN_3_150, 2, 2, 180, 180)[0] == "not_met"
    weeks = [
        {"week_start": "2026-01-05", "status": "not_met"},
        {"week_start": "2026-01-12", "status": "not_met"},
        {"week_start": "2026-01-19", "status": "met"},
        {"week_start": "2026-01-26", "status": "cannot_determine"},
    ]
    # A met week ends a run, so the last pair depends on nothing; the gold file HG-M042 expects exactly this.
    assert reconcile.consecutive_shortfalls(weeks) == {
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
    assert reconcile.consecutive_shortfalls(weeks) == {
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
        status, _ = reconcile.goal_status({"min_minutes_per_week": goal, "min_days_per_week": 0}, 0, 0, low, high)
        if status == "met":
            assert all(value >= goal for value in (low, high))
        if status == "not_met":
            assert all(value < goal for value in (low, high))
        if status == "cannot_determine":
            assert low < goal <= high


def test_fixture_values_sit_in_their_cited_lines():
    for doc in mini_patient_docs():
        assert reconcile.admit(doc.doc_id, doc.text, doc.extraction) == (doc.extraction, []), doc.doc_id
        assert reconcile.coverage(doc.text, doc.extraction) == [], doc.doc_id


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
    assert reconcile.admit("D11", register.text, register.extraction)[1] == []
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
    assert reconcile.admit("D11", settling.text, settling.extraction)[1] == []
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
    assert reconcile.admit("D11", signed.text, signed.extraction)[1] == []
    e5 = encounters_by_id(build_patient("T-M001", [*mini_patient_docs(), signed]))["E5"]
    assert (e5["status"], e5["counts_as_therapy"], e5["minutes_low"], e5["minutes_high"]) == ("attended", "yes", 45, 45)
    assert e5["calculation"] == "45 minutes as stated (no times given)"
    unsigned = encounters_by_id(build_patient("T-M001", [*mini_patient_docs(), service_record(False)]))["E5"]
    assert (unsigned["counts_as_therapy"], unsigned["minutes_high"]) == ("no", 0)
