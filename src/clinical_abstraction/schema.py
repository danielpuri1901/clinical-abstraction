"""What the model may claim about one document.

The extraction model is untrusted. It fills these fields from a single document
and cites the 1-based line numbers that state each value. The trusted code in
reconcile.py checks every cited line and value against the source text before any
claim is used, and drops what it cannot confirm.
"""

from typing import Literal

from pydantic import BaseModel, Field

ServiceType = Literal[
    "individual_psychotherapy",
    "group_psychotherapy",
    "family_psychotherapy",
    "collateral_contact",
    "medication_management",
    "care_coordination",
    "assessment_review",
    "outreach_or_scheduling_call",
    "administrative",
    "other",
]

Attendance = Literal[
    "attended",
    "attended_partial",
    "no_show",
    "cancelled_by_clinic",
    "cancelled_by_patient",
    "patient_absent",
    "not_stated",
]

RecordRole = Literal[
    "clinical_note",
    "attendance_record",
    "schedule_listing",
    "telehealth_log",
    "billing_charge",
    "draft_or_template",
    "copy_of_another_record",
    "other",
]

Modality = Literal["in_person", "video", "phone", "unknown"]

ObservationCategory = Literal["mood", "anxiety", "sleep", "functioning", "avoidance", "safety", "reason_for_contact"]

LINES = Field(description="1-based line numbers of the document that state the values of this item, 1 to 6 lines")


class Interval(BaseModel):
    start: str = Field(description="HH:MM, 24-hour local time")
    end: str = Field(description="HH:MM, 24-hour local time")


class ExcludedInterval(BaseModel):
    start: str = Field(description="HH:MM, 24-hour local time")
    end: str = Field(description="HH:MM, 24-hour local time")
    reason: Literal["break", "disconnect", "patient_not_present", "other"]
    lines: list[int] = LINES


class ServiceEvent(BaseModel):
    """One scheduled or delivered contact as this document describes it."""

    encounter_id: str | None = Field(description="Encounter identifier printed in the document, else null")
    appointment_ids: list[str] = Field(description="Appointment or call identifiers printed for this contact")
    service_date: str = Field(description="YYYY-MM-DD")
    service_type: ServiceType
    modality: Modality
    scheduled_start: str | None = Field(description="HH:MM scheduled start, else null")
    scheduled_end: str | None = Field(description="HH:MM scheduled end, else null")
    attendance: Attendance
    patient_arrival: str | None = Field(description="HH:MM actual patient arrival if stated, else null")
    patient_departure: str | None = Field(description="HH:MM actual patient departure if stated, else null")
    patient_present_intervals: list[Interval] = Field(
        description="Intervals the document states the patient was actually present for this service. "
        "Leave empty when the document gives only scheduled times or no times."
    )
    stated_patient_minutes: int | None = Field(description="Patient-present minutes stated as a number, else null")
    excluded_intervals: list[ExcludedInterval] = Field(
        description="Intervals inside the session with no patient therapy: breaks, disconnects, "
        "portions where the patient was not present"
    )
    record_role: RecordRole
    signed: bool | None = Field(description="True if this content carries a clinician signature or attestation")
    clinicians: list[str]
    lines: list[int] = LINES


class Correction(BaseModel):
    encounter_id: str | None
    service_date: str | None = Field(description="YYYY-MM-DD of the corrected service, else null")
    field: Literal["patient_arrival", "patient_departure", "attendance", "service_type", "patient_minutes", "other"] = Field(
        description="The field this correction changes. A corrected start time of the patient contact is patient_arrival, "
        "and a corrected end time of the patient contact is patient_departure. Use other only when no other value fits."
    )
    corrected_value: str
    replaced_value: str | None = Field(description="The earlier value this correction replaces, if stated")
    lines: list[int] = LINES


class PlanRequirement(BaseModel):
    effective_start: str | None = Field(description="YYYY-MM-DD the requirement starts, else null")
    effective_end: str | None = Field(description="YYYY-MM-DD the requirement ends, else null")
    min_therapy_days_per_week: int | None
    min_patient_present_minutes_per_week: int | None
    week_definition: str | None = Field(description="For example Monday-Sunday")
    counted_service_types: list[ServiceType]
    not_counted_service_types: list[ServiceType]
    requires_patient_present: bool
    lines: list[int] = LINES


class ItemScore(BaseModel):
    item: str
    score: int


class Assessment(BaseModel):
    instrument: str = Field(description="For example PHQ-9")
    total_score: int | None
    item_scores: list[ItemScore]
    completed_on: str | None = Field(description="YYYY-MM-DD the patient completed it, else null")
    completed_time: str | None = Field(description="HH:MM if stated, else null")
    source_form_id: str | None
    is_copy_of_earlier_result: bool = Field(
        description="True when this document repeats, imports, or cites a result completed earlier"
    )
    lines: list[int] = LINES


class Authorization(BaseModel):
    authorization_id: str | None
    service_type: ServiceType
    quantity: int | None
    unit: str | None
    start: str | None = Field(description="YYYY-MM-DD")
    end: str | None = Field(description="YYYY-MM-DD")
    lines: list[int] = LINES


class Charge(BaseModel):
    charge_id: str | None
    encounter_id: str | None
    service_date: str | None = Field(description="YYYY-MM-DD")
    description: str
    quantity: int | None
    status: str | None
    lines: list[int] = LINES


class Observation(BaseModel):
    observed_on: str = Field(description="YYYY-MM-DD the observation refers to")
    category: ObservationCategory
    statement: str = Field(description="One short sentence in plain words")
    lines: list[int] = LINES


class DocumentExtraction(BaseModel):
    document_id: str | None = Field(description="Document ID printed in the document, else null")
    patient_name: str | None
    patient_dob: str | None = Field(description="YYYY-MM-DD")
    patient_mrn: str | None
    document_type: str = Field(description="Short plain label, for example 'group attendance roster'")
    authored_at: str | None = Field(
        description="YYYY-MM-DDTHH:MM when the original content was created or signed. "
        "For a copy, use the original signature time printed on the copy."
    )
    received_at: str | None = Field(description="YYYY-MM-DDTHH:MM when received, imported, or filed, if stated")
    is_copy_or_retransmission: bool
    copy_lines: list[int] = Field(description="Line numbers that say this document is a copy or retransmission, else empty")
    final_or_signed: bool = Field(description="False for drafts, unsigned or template-generated content")
    service_events: list[ServiceEvent]
    corrections: list[Correction]
    plan_requirements: list[PlanRequirement]
    assessments: list[Assessment]
    authorizations: list[Authorization]
    charges: list[Charge]
    observations: list[Observation]
