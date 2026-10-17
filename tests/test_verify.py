"""The referee checks every value the model cites against the source text and never repairs one."""

from pathlib import Path

import pytest

from clinical_abstraction import verify
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
    assert verify.numbered("first\nsecond\n\nfourth\n") == "L1: first\nL2: second\nL3: \nL4: fourth"


def test_cited_lines_are_sorted_and_unique():
    assert verify.cited_lines(event(lines=[3, 1, 3])) == [1, 3]
    assert verify.cited_lines(extraction()) == []


def test_times_and_dates_normalize():
    assert verify.normalize_time("9:00 AM") == "09:00" and verify.normalize_time("1415") == "14:15"
    assert verify.normalize_time("09:50") == "09:50" and verify.normalize_time("25:00") is None
    assert verify.normalize_date("30 January 2026") == "2026-01-30"
    assert verify.normalize_date("January 19, 2026") == "2026-01-19" == verify.normalize_date("01/19/2026")
    assert verify.normalize_date("Jan 5", default_year=2026) == "2026-01-05"
    assert verify.normalize_date("Jan05", default_year=2026) == "2026-01-05"  # the appointment export writes dates this way
    assert verify.value_in_lines("2026-01-05", "date", ["HG-E101   | Jan05 | Individual therapy"])
    assert verify.normalize_date("19th of January 2026") == "2026-01-19"
    assert verify.normalize_date("February 30, 2026") is None


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
    assert verify.normalize_time(value) == expected


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
    assert verify.normalize_date(value) == expected


def test_value_must_sit_in_its_own_cited_lines():
    lines = ["Scheduled group: 10:00-11:30.", "Patient arrival: 10:00 AM | Patient departure: 11:15"]
    assert verify.value_in_lines("11:15", "time", [lines[1]])
    assert not verify.value_in_lines("11:30", "time", [lines[1]])
    assert verify.value_in_lines("2026-01-30", "date", ["Service date: 30 January 2026"])


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
    assert verify.value_in_lines(value, kind, [line])


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
    assert not verify.value_in_lines(value, kind, [line])


def test_numbers_must_be_stated_not_read_out_of_times_dates_or_names():
    assert verify.value_in_lines("50", "number", ["Actual contact: 09:00–09:50, 50 minutes."])
    assert not verify.value_in_lines("50", "number", ["Actual contact: 09:00–09:50."])
    assert not verify.value_in_lines("22", "number", ["January 22 | HG-E113 | Skills group"])
    assert not verify.value_in_lines("5", "number", ["Total 3.5 hours"]) and not verify.value_in_lines(
        "3", "number", ["Total 3.5 hours"]
    )
    assert verify.value_in_lines("45", "number", ["Total 45 minutes"]) and verify.value_in_lines(
        "45 minutes", "number", ["Total 45"]
    )
    assert verify.value_in_lines("14", "number", ["Instrument: PHQ-9 | Total score: 14"])
    assert not verify.value_in_lines("9", "number", ["Instrument: PHQ-9 | Total score: 14"])
    assert verify.value_in_lines("0", "number", ["PHQ-9 total: 10. Item 9: 0."])


def test_text_ignores_case_whitespace_and_underscores():
    assert verify.value_in_lines("no show", "text", ["Final disposition:  NO   Show; patient did not attend"])
    assert verify.value_in_lines("no_show", "text", ["No show"])
    assert not verify.value_in_lines("attended", "text", ["No show"])
    assert not verify.value_in_lines("  ", "text", ["anything at all"])


def test_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        verify.value_in_lines("x", "color", ["x"])


def test_admit_drops_unsupported_values_and_reports_them():
    text = "Scheduled group: 10:00-11:30.\nPatient arrival: 10:00 | Patient departure: 11:15\nService date: January 19, 2026\n"
    raw = event(patient_arrival="10:00", patient_departure="11:30", service_date="2026-01-19", lines=[2, 3])
    kept, problems = verify.admit("D1", text, extraction(service_events=[raw]))
    assert kept.service_events[0].patient_departure is None and kept.service_events[0].patient_arrival == "10:00"
    assert problems == [verify.Problem("D1", "service_events[0]", "patient_departure", "11:30 not in cited lines 2-3")]
    assert raw.patient_departure == "11:30"  # the model's own record stays untouched for the audit trail


def test_scheduled_times_are_checked_like_actual_times():
    text = "January 19, 2026 | Scheduled group: 10:00-11:30.\n"
    booked = event(scheduled_start="10:00", scheduled_end="11:45", lines=[1])
    kept, problems = verify.admit("D1", text, extraction(service_events=[booked]))
    assert (kept.service_events[0].scheduled_start, kept.service_events[0].scheduled_end) == ("10:00", None)
    assert problems == [verify.Problem("D1", "service_events[0]", "scheduled_end", "11:45 not in cited lines 1")]


def test_admit_keeps_day_first_dates_twelve_hour_and_bare_times():
    text = "Date of service: 30 January 2026\nStart 9:00 AM | End 9:50 AM\nIn 0900 | Out 0950 | 50 minutes\n"
    booked = event(service_date="2026-01-30", scheduled_start="09:00", scheduled_end="09:50", lines=[2])
    seen = event(
        service_date="2026-01-30", patient_arrival="09:00", patient_departure="09:50", stated_patient_minutes=50, lines=[3]
    )
    raw = extraction(service_events=[booked, seen])
    kept, problems = verify.admit("D1", text, raw)
    assert problems == [] and kept == raw


def test_interval_with_one_unsupported_endpoint_is_dropped_whole():
    text = "January 19, 2026: patient present 10:00-10:50 and 11:00-11:40.\n"
    present = [Interval(start="10:00", end="10:50"), Interval(start="11:00", end="11:20")]
    kept, problems = verify.admit("D1", text, extraction(service_events=[event(patient_present_intervals=present)]))
    assert kept.service_events[0].patient_present_intervals == [Interval(start="10:00", end="10:50")]
    assert problems == [
        verify.Problem("D1", "service_events[0].patient_present_intervals[1]", "end", "11:20 not in cited lines 1")
    ]


def test_excluded_interval_is_checked_against_its_own_lines():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    good = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[2])
    wrong_line = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[1])
    kept, problems = verify.admit(
        "D1", text, extraction(service_events=[event(excluded_intervals=[good, wrong_line], lines=[1])])
    )
    assert kept.service_events[0].excluded_intervals == [good]
    assert problems == [
        verify.Problem("D1", "service_events[0].excluded_intervals[1]", "start", "10:45 not in cited lines 1"),
        verify.Problem("D1", "service_events[0].excluded_intervals[1]", "end", "11:00 not in cited lines 1"),
    ]


def test_item_citing_missing_or_no_lines_is_dropped():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    kept, problems = verify.admit("D1", text, extraction(service_events=[event(lines=[1, 9]), event(lines=[]), event(lines=[1])]))
    assert len(kept.service_events) == 1
    assert problems == [
        verify.Problem("D1", "service_events[0]", "lines", "lines 9 not in document of 2 lines"),
        verify.Problem("D1", "service_events[1]", "lines", "cites no lines"),
    ]


def test_excluded_interval_citing_a_missing_line_is_dropped():
    text = "January 19, 2026 | Group 10:00-11:30\nBreak 10:45-11:00\n"
    before_the_document = ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[0])
    kept, problems = verify.admit(
        "D1", text, extraction(service_events=[event(excluded_intervals=[before_the_document], lines=[1])])
    )
    assert kept.service_events[0].excluded_intervals == []
    assert problems == [
        verify.Problem("D1", "service_events[0].excluded_intervals[0]", "lines", "lines 0 not in document of 2 lines")
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
    kept, problems = verify.admit("D1", "January 19, 2026 only line\n", extraction(**{collection: [item]}))
    assert getattr(kept, collection) == []
    assert problems == [verify.Problem("D1", f"{collection}[0]", "lines", "lines 9 not in document of 1 lines")]


def test_dates_may_sit_on_any_line_but_must_exist():
    text = "Service date: January 19, 2026\nPatient arrival: 10:00\n"
    found = event(service_date="2026-01-19", patient_arrival="10:00", lines=[2])
    invented = event(service_date="2026-01-20", patient_arrival="10:00", lines=[2])
    kept, problems = verify.admit("D1", text, extraction(service_events=[found, invented]))
    assert kept.service_events == [found]
    assert problems == [verify.Problem("D1", "service_events[1]", "service_date", "2026-01-20 not in document")]


def test_correction_with_unsupported_value_is_dropped_and_the_good_one_kept():
    text = "Correction: departure is 11:15, replacing 11:30 (January 19, 2026).\n"
    bad = correction(corrected_value="11:20", replaced_value="11:30", service_date="2026-01-19")
    good = correction(corrected_value="11:15", replaced_value="11:30", service_date="2026-01-19")
    kept, problems = verify.admit("D1", text, extraction(corrections=[bad, good]))
    assert kept.corrections == [good]
    assert problems == [verify.Problem("D1", "corrections[0]", "corrected_value", "11:20 not in cited lines 1")]


def test_correction_minutes_are_a_stated_number_not_a_substring():
    text = "January 26, 2026 correction: patient arrived 09:40.\n"
    changed = correction(field="patient_minutes", corrected_value="40", service_date="2026-01-26")
    kept, problems = verify.admit("D1", text, extraction(corrections=[changed]))
    assert kept.corrections == []
    assert problems == [verify.Problem("D1", "corrections[0]", "corrected_value", "40 not in cited lines 1")]


def test_correction_date_and_replaced_value_are_checked_but_do_not_drop_it():
    text = "Correction: departure is 11:15, replacing 11:30 (January 19, 2026).\n"
    changed = correction(corrected_value="11:15", replaced_value="11:45", service_date="2026-01-20")
    kept, problems = verify.admit("D1", text, extraction(corrections=[changed]))
    assert (kept.corrections[0].corrected_value, kept.corrections[0].service_date, kept.corrections[0].replaced_value) == (
        "11:15",
        None,
        None,
    )
    assert problems == [
        verify.Problem("D1", "corrections[0]", "service_date", "2026-01-20 not in document"),
        verify.Problem("D1", "corrections[0]", "replaced_value", "11:45 not in cited lines 1"),
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
    kept, problems = verify.admit("D1", text, extraction(corrections=[changed]))
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
    kept, problems = verify.admit("D1", text, extraction(assessments=[stated, invented]))
    assert kept.assessments[0] == stated
    checked = kept.assessments[1]
    assert checked.total_score is None and checked.item_scores == [ItemScore(item="9", score=0)]
    assert (checked.completed_on, checked.completed_time) == (None, None)
    assert problems == [
        verify.Problem("D1", "assessments[1]", "total_score", "14 not in cited lines 1-2"),
        verify.Problem("D1", "assessments[1].item_scores[1]", "score", "3 not in cited lines 1-2"),
        verify.Problem("D1", "assessments[1]", "completed_on", "2026-01-17 not in document"),
        verify.Problem("D1", "assessments[1]", "completed_time", "08:19 not in cited lines 1-2"),
    ]


def test_stated_minutes_must_be_stated_in_the_cited_lines():
    text = "January 19, 2026: contact 09:00-09:50, 50 minutes.\nBilling shows 45 minutes.\n"
    kept, problems = verify.admit("D1", text, extraction(service_events=[event(stated_patient_minutes=45, lines=[1])]))
    assert kept.service_events[0].stated_patient_minutes is None
    assert problems == [verify.Problem("D1", "service_events[0]", "stated_patient_minutes", "45 not in cited lines 1")]


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
    kept, problems = verify.admit("D1", text, extraction(plan_requirements=[raw]))
    checked = kept.plan_requirements[0]
    assert (checked.min_therapy_days_per_week, checked.min_patient_present_minutes_per_week) == (3, None)
    assert (checked.effective_start, checked.effective_end) == ("2026-01-05", None)
    assert problems == [
        verify.Problem("D1", "plan_requirements[0]", "effective_end", "2026-01-31 not in document"),
        verify.Problem("D1", "plan_requirements[0]", "min_patient_present_minutes_per_week", "150 not in cited lines 2"),
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
    kept, problems = verify.admit("D1", text, extraction(authorizations=[approval], charges=[posted], observations=[seen]))
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
    assert verify.admit("BH-D101", group, whole) == (whole, [])
    assert verify.coverage(group, whole) == []
    no_break = extraction(
        patient_dob="1991-04-12",
        authored_at="2026-01-19T12:08",
        service_events=[event(service_date="2026-01-19", scheduled_start="10:00", scheduled_end="11:30", lines=[3, 6])],
    )
    assert verify.coverage(group, no_break) == ["possible omission: L6 10:45", "possible omission: L6 11:00"]


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
    assert verify.admit("NF-D1", note, held_out) == (held_out, [])
    assert verify.coverage(note, held_out) == []


def test_real_correction_record_is_admitted():
    text = (ROOT / "documents" / "BH-D103_attendance_correction_2026-01-20.txt").read_text(encoding="utf-8")
    fixed = extraction(
        corrections=[correction(service_date="2026-01-19", corrected_value="11:15", replaced_value="11:30", lines=[7])]
    )
    assert verify.admit("BH-D103", text, fixed) == (fixed, [])


def test_coverage_names_unreferenced_times():
    text = "Actual contact: 09:10-09:50.\nRowan entered the room at 09:10.\n"
    raw = event(patient_present_intervals=[Interval(start="09:10", end="09:50")], lines=[1])
    assert verify.coverage(text, extraction(service_events=[raw])) == []
    assert verify.coverage("Break 10:45-11:00.\n" + text, extraction(service_events=[raw])) == [
        "possible omission: L1 10:45",
        "possible omission: L1 11:00",
    ]


def test_coverage_names_unreferenced_dates():
    text = "Service date: January 19, 2026\nFollow-up on January 22 and review on 2026-02-03.\n"
    assert verify.coverage(text, extraction(service_events=[event(service_date="2026-01-19")])) == [
        "possible omission: L2 January 22",
        "possible omission: L2 2026-02-03",
    ]


def test_coverage_matches_values_written_in_any_format():
    text = "Date of service: 30 January 2026\nStart 9:00 AM | End 2:15 PM | seen January 30\nSigned 2026-01-30T16:05\n"
    seen = event(service_date="2026-01-30", patient_present_intervals=[Interval(start="09:00", end="14:15")])
    assert verify.coverage(text, extraction(authored_at="2026-01-30T16:05", service_events=[seen])) == []


def test_coverage_reports_each_value_once_per_line_in_document_order():
    assert verify.coverage("Break 10:45-11:00, resumed 10:45.\n", extraction()) == [
        "possible omission: L1 10:45",
        "possible omission: L1 11:00",
    ]


def test_coverage_does_not_read_years_or_identifiers_as_times():
    text = "Imported January 26, 2026 from batch HG-MEAS-0126, form HG-Q116.\n"
    assert verify.coverage(text, extraction(service_events=[event(service_date="2026-01-26")])) == []
