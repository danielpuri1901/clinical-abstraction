"""A ten-document patient written by hand. Every value sits on the line its extraction cites, so verify.admit keeps all of it.

D1 schedule listing for group E1 on 2026-01-19, 10:00-11:30.
D2 signed attendance record for E1: arrival 10:00, departure 11:30, break 10:45-11:00.
D3 correction of the E1 departure to 11:15, authored 2026-01-20.
D4 copy of D2, received 2026-01-26 after the correction, authored 2026-01-19 like its original.
D5 signed individual note for E2 on 2026-01-19, present 11:15-12:00.
D6 and D7 signed notes for E3 on 2026-01-26 that disagree on the start: 09:00-09:50 and 09:10-09:50.
D8 encounter packet for group E4 on 2026-01-27: attendance says no show, a draft note says attended, a charge is posted.
D9 signed treatment plan from 2026-01-05: individual, group, and family therapy count; 3 days and 150 minutes each Monday week.
D10 PHQ-9 reviews (2026-01-05 score 18, 2026-01-16 score 14), an imported copy of the 2026-01-16 result, and one observation.
"""

import threading
from dataclasses import replace

from clinical_abstraction import store
from clinical_abstraction.reconcile import Doc
from clinical_abstraction.schema import (
    Assessment,
    Charge,
    Correction,
    DocumentExtraction,
    ExcludedInterval,
    Interval,
    Observation,
    PlanRequirement,
    ServiceEvent,
)

PATIENT_LINE = "Patient: Rowan Hale | MRN: T-M001"
USAGE = {  # what one extraction call reports
    "input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    "cost_usd": 0.001, "calls": 1, "repair_turns": 0, "unrepaired_problems": 0,
}


class FixtureExtractor:
    """A model that always reads a document correctly: it returns the known extraction of the text it is given."""

    def __init__(self, docs):
        self.by_text = {store.normalize_text(doc.text): doc.extraction for doc in docs}
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, doc_id, text):
        with self.lock:
            self.seen.append(doc_id)
        return self.by_text[text].model_copy(deep=True), [], dict(USAGE)


def service_event(**fields) -> ServiceEvent:
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
            "record_role": "clinical_note",
            "signed": True,
            "clinicians": [],
            "lines": [1],
            **fields,
        }
    )


def _phq9(score: int, completed_on: str, line: int, copy: bool = False) -> Assessment:
    return Assessment(
        instrument="PHQ-9",
        total_score=score,
        item_scores=[],
        completed_on=completed_on,
        completed_time=None,
        source_form_id=None,
        is_copy_of_earlier_result=copy,
        lines=[line],
    )


def extraction(**fields) -> DocumentExtraction:
    """An extraction of document D1 that names no patient and lists nothing, with the given fields replaced."""
    empty = ("service_events", "corrections", "plan_requirements", "assessments", "authorizations", "charges", "observations")
    defaults = {"document_id": "D1", "document_type": "test record", "is_copy_or_retransmission": False, "copy_lines": []}
    nulls = dict.fromkeys(("patient_name", "patient_dob", "patient_mrn", "authored_at", "received_at"))
    return DocumentExtraction(**{**defaults, **nulls, "final_or_signed": True, **{name: [] for name in empty}, **fields})


def document(
    doc_id: str, lines: list[str], document_type: str, authored_at: str | None, received_at: str | None = None, **parts
) -> Doc:
    text = "\n".join(lines) + "\n"
    header = {"document_id": doc_id, "patient_name": "Rowan Hale", "patient_mrn": "T-M001", "document_type": document_type}
    found = extraction(**{**header, "authored_at": authored_at, "received_at": received_at, **parts})
    return Doc(doc_id, store.content_sha(text), text, found, authored_at, received_at)


def mini_patient_docs() -> list[Doc]:
    roster = [
        "Group attendance record - E1 - group psychotherapy - 2026-01-19",
        PATIENT_LINE,
        "Arrival 10:00 | Departure 11:30",
        "Break 10:45-11:00, no therapy",
        "Signed by L. Chen, LCSW on 2026-01-19 11:40",
    ]
    roster_event = service_event(
        encounter_id="E1",
        record_role="attendance_record",
        patient_arrival="10:00",
        patient_departure="11:30",
        excluded_intervals=[ExcludedInterval(start="10:45", end="11:00", reason="break", lines=[4])],
        clinicians=["L. Chen"],
        lines=[1, 3],
    )
    individual = {"service_type": "individual_psychotherapy", "lines": [1, 3]}
    return [
        document(
            "D1",
            [
                "Clinic schedule printed 2026-01-18 08:00",
                PATIENT_LINE,
                "E1 | Group psychotherapy | 2026-01-19 | 10:00-11:30 | booked",
            ],
            "schedule listing",
            "2026-01-18T08:00",
            service_events=[
                service_event(
                    encounter_id="E1",
                    scheduled_start="10:00",
                    scheduled_end="11:30",
                    attendance="not_stated",
                    record_role="schedule_listing",
                    signed=None,
                    lines=[3],
                )
            ],
        ),
        document("D2", roster, "group attendance record", "2026-01-19T11:40", service_events=[roster_event]),
        document(
            "D3",
            [
                "Attendance correction - E1 - 2026-01-19",
                PATIENT_LINE,
                "Departure corrected from 11:30 to 11:15; the patient left at 11:15.",
                "Signed by L. Chen, LCSW on 2026-01-20 09:00",
            ],
            "attendance correction",
            "2026-01-20T09:00",
            corrections=[
                Correction(
                    encounter_id="E1",
                    service_date="2026-01-19",
                    field="patient_departure",
                    corrected_value="11:15",
                    replaced_value="11:30",
                    lines=[3],
                )
            ],
        ),
        document(
            "D4",
            [*roster, "Copy resent to records on 2026-01-26 16:30"],
            "copy of group attendance record",
            "2026-01-19T11:40",
            "2026-01-26T16:30",
            is_copy_or_retransmission=True,
            copy_lines=[6],
            service_events=[roster_event],
        ),
        document(
            "D5",
            [
                "Individual psychotherapy note - E2 - 2026-01-19",
                PATIENT_LINE,
                "Patient present 11:15-12:00, in person",
                "Signed by J. Ortiz, LCSW on 2026-01-19 12:30",
            ],
            "individual psychotherapy note",
            "2026-01-19T12:30",
            service_events=[
                service_event(
                    encounter_id="E2",
                    patient_present_intervals=[Interval(start="11:15", end="12:00")],
                    clinicians=["J. Ortiz"],
                    **individual,
                )
            ],
        ),
        document(
            "D6",
            [
                "Individual psychotherapy note - E3 - 2026-01-26",
                PATIENT_LINE,
                "Patient present 09:00-09:50, in person",
                "Signed by J. Ortiz, LCSW on 2026-01-26 10:05",
            ],
            "individual psychotherapy note",
            "2026-01-26T10:05",
            service_events=[
                service_event(
                    encounter_id="E3",
                    service_date="2026-01-26",
                    patient_present_intervals=[Interval(start="09:00", end="09:50")],
                    clinicians=["J. Ortiz"],
                    **individual,
                )
            ],
        ),
        document(
            "D7",
            [
                "Individual psychotherapy note - E3 - 2026-01-26",
                PATIENT_LINE,
                "Patient present 09:10-09:50, in person",
                "Signed by N. Ellis, LCSW on 2026-01-26 10:20",
            ],
            "individual psychotherapy note",
            "2026-01-26T10:20",
            service_events=[
                service_event(
                    encounter_id="E3",
                    service_date="2026-01-26",
                    patient_present_intervals=[Interval(start="09:10", end="09:50")],
                    clinicians=["N. Ellis"],
                    **individual,
                )
            ],
        ),
        document(
            "D8",
            [
                "Encounter packet - E4 - group psychotherapy - 2026-01-27",
                PATIENT_LINE,
                "Attendance: no show for the 10:00-11:30 group, signed by L. Chen, LCSW",
                "Progress note: DRAFT auto-generated from template. Patient attended and participated.",
                "Charge C4 for E4 group psychotherapy: posted",
            ],
            "encounter packet",
            None,
            service_events=[
                service_event(
                    encounter_id="E4",
                    service_date="2026-01-27",
                    scheduled_start="10:00",
                    scheduled_end="11:30",
                    attendance="no_show",
                    record_role="attendance_record",
                    clinicians=["L. Chen"],
                    lines=[1, 3],
                ),
                # The extraction calls the draft a signed clinical note; only its own text says otherwise.
                service_event(encounter_id="E4", service_date="2026-01-27", lines=[1, 4]),
            ],
            charges=[
                Charge(
                    charge_id="C4",
                    encounter_id="E4",
                    service_date="2026-01-27",
                    description="group psychotherapy",
                    quantity=None,
                    status="posted",
                    lines=[5],
                )
            ],
        ),
        document(
            "D9",
            [
                "Treatment plan signed 2026-01-05 13:00",
                PATIENT_LINE,
                "Effective 2026-01-05. Goal: 3 therapy days and 150 patient-present minutes each Monday-Sunday week.",
                "Counted: individual, group, and family psychotherapy.",
            ],
            "treatment plan",
            "2026-01-05T13:00",
            plan_requirements=[
                PlanRequirement(
                    effective_start="2026-01-05",
                    effective_end=None,
                    min_therapy_days_per_week=3,
                    min_patient_present_minutes_per_week=150,
                    week_definition="Monday-Sunday",
                    counted_service_types=["individual_psychotherapy", "group_psychotherapy", "family_psychotherapy"],
                    not_counted_service_types=[],
                    requires_patient_present=True,
                    lines=[3, 4],
                )
            ],
        ),
        document(
            "D10",
            [
                "Symptom measure review signed 2026-01-30 15:00",
                PATIENT_LINE,
                "PHQ-9 completed 2026-01-05: total score 18",
                "PHQ-9 completed 2026-01-16: total score 14",
                "Imported copy of the 2026-01-16 PHQ-9 result: total score 14",
                "Clinician review of 2026-01-16: mood still low, sleep improving.",
            ],
            "symptom measure review",
            "2026-01-30T15:00",
            assessments=[_phq9(18, "2026-01-05", 3), _phq9(14, "2026-01-16", 4), _phq9(14, "2026-01-16", 5, copy=True)],
            observations=[
                Observation(observed_on="2026-01-16", category="mood", statement="Mood still low, sleep improving", lines=[6])
            ],
        ),
    ]


def replace_service_date(doc: Doc, encounter_id: str, new_date: str) -> Doc:
    """The same document with one encounter moved to another date, in its text and its extraction alike."""
    events = doc.extraction.service_events
    text = doc.text
    for old_date in {event.service_date for event in events if event.encounter_id == encounter_id}:
        text = text.replace(old_date, new_date)
    moved = [
        event.model_copy(update={"service_date": new_date}) if event.encounter_id == encounter_id else event for event in events
    ]
    return replace(doc, sha=store.content_sha(text), text=text, extraction=doc.extraction.model_copy(update={"service_events": moved}))
