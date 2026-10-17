"""Reconciliation: deterministic code that turns admitted extractions into one abstraction per patient.

Policy, in the order it is applied:
1. Identity. Records of the same contact merge by encounter id, then appointment id, then the same date with a compatible
   service type and overlapping times.
2. Evidence tiers. Signed clinical notes, attendance records, and telehealth logs are primary. Schedule listings say what was
   booked. Copies repeat their original. Drafts, templates, unsigned notes, and charges never establish that care happened.
3. Dates. Records that share an encounter id but disagree on the date keep both dates and raise a date conflict.
4. Corrections apply in authored order to every record authored before them, copies received later included. Receipt time
   never decides. A record authored after a correction that still disagrees is a conflict, not an override.
5. Minutes are presence intervals minus breaks, disconnects, and parts without the patient. Primary records that still
   disagree are carried as a range [low, high].
6. Counting. A service counts only if the plan counts its type and a record establishes that it was delivered.
7. Second readers. Keyword rules and the decision model's label disputes can hold an encounter as disputed. Neither ever
   creates care, and a dispute changes the count only when the other label would change it.
Every conflict flag cites both sides and names the document that would settle it.

The extractions arrive admitted by verify.admit, so every value sits in its cited lines. verify.admit keeps the model's
spelling of a value, so this module normalizes dates and times before using them. Citations read "D7 L3" or "D7 L3-5".
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import get_args

from pydantic import BaseModel

from clinical_abstraction import calculate, verify
from clinical_abstraction.schema import Attendance, DocumentExtraction, Interval, ServiceEvent, ServiceType

THERAPY_TYPES = ["individual_psychotherapy", "group_psychotherapy", "family_psychotherapy"]
CONTEXT_TYPES = {"administrative", "assessment_review", "outreach_or_scheduling_call", "other"}
DELIVERED = {"attended", "attended_partial"}
NOT_DELIVERED = {"no_show", "cancelled_by_clinic", "cancelled_by_patient"}
UNDELIVERED = NOT_DELIVERED | {"patient_absent", "not_documented"}
TIER_ORDER = {"primary": 0, "schedule": 1, "copy": 2, "unsupported": 3}
CORRECTED = {"patient_arrival": "arrival", "patient_departure": "departure", "attendance": "attendance"}
FIELD_WORDS = {
    "patient_arrival": "arrival time",
    "patient_departure": "departure time",
    "attendance": "attendance",
    "service_type": "service type",
    "record_role": "record type",
    "copy_or_draft": "record status",
}

# Keyword second reader: plain rules that can contradict a model label but never create care.
ABSENT_WORDS = re.compile(
    r"absent for the entire|did not attend|did not join|no patient (?:treatment )?contact|"
    r"not present by (?:phone|video)|\bno.?show\b|was not seen",
    re.IGNORECASE,
)
# Upper-case DRAFT only, because therapy notes often mention a draft message the patient wrote.
DRAFT_WORDS = re.compile(r"(?-i:\bDRAFT\b)|\bstatus:\s*draft\b|\bunsigned\b|\bauto-?generated\b|\btemplate\b", re.IGNORECASE)
RESENT_WORDS = re.compile(r"retransmi|\bresent\b|re-sent", re.IGNORECASE)


@dataclass
class Doc:
    doc_id: str
    sha: str
    text: str
    extraction: DocumentExtraction
    authored_at: str | None
    received_at: str | None
    disputes: list[dict] = field(default_factory=list)


@dataclass
class Record:
    """One service event as one document states it, with ISO dates and HH:MM times."""

    doc: Doc
    index: int
    event: ServiceEvent
    citations: list[str]


@dataclass
class CorrectionRecord:
    doc: Doc
    field: str
    value: str
    replaced: str | None
    citations: list[str]
    authored: str | None


@dataclass
class View:
    """One record's effective statement about an encounter, after corrections."""

    record: Record
    tier: str
    text: str
    arrival: str | None
    departure: str | None
    intervals: list[tuple[int, int]]
    attendance: str
    notes: list[str] = field(default_factory=list)


# Time and citation helpers ----------------------------------------------------------------------


def to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(interval for interval in intervals if interval[1] > interval[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(intervals: list[tuple[int, int]], cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pieces = merge(intervals)
    for cut_start, cut_end in merge(cuts):
        next_pieces = []
        for start, end in pieces:
            if cut_end <= start or cut_start >= end:
                next_pieces.append((start, end))
                continue
            if start < cut_start:
                next_pieces.append((start, cut_start))
            if cut_end < end:
                next_pieces.append((cut_end, end))
        pieces = next_pieces
    return pieces


def length(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merge(intervals))


def describe(intervals: list[tuple[int, int]]) -> str:
    return ", ".join(f"{clock(start)}-{clock(end)}" for start, end in intervals) or "none"


def _time(value: str | None) -> str | None:
    return verify.normalize_time(value) if value else None


def _date(value: str | None) -> str | None:
    return verify.normalize_date(value) if value else None


def _moment(value: str | None) -> str | None:
    """A timestamp as "YYYY-MM-DDTHH:MM", or the date alone, so two clocks compare as text. None when unreadable."""
    if not value:
        return None
    day, _, time_of_day = value.strip().replace(" ", "T", 1).partition("T")
    iso = verify.normalize_date(day)
    hour = time_of_day and (verify.normalize_time(time_of_day) or verify.normalize_time(time_of_day[:5]))
    return iso and (f"{iso}T{hour}" if hour else iso)


def _authored(doc: Doc) -> str | None:
    """Valid time: when the content was written. A copy carries its original's authored time."""
    return _moment(doc.authored_at) or _moment(doc.extraction.authored_at)


def _received(doc: Doc) -> str | None:
    """Transaction time: when the system got the document. It never decides which statement wins."""
    return _moment(doc.received_at) or _moment(doc.extraction.received_at)


def _citations(doc_id: str, item: BaseModel) -> list[str]:
    lines = verify.cited_lines(item)
    return [f"{doc_id} L{run}" for run in verify._line_ranges(lines).split(", ")] if lines else []


def _cite(record: Record) -> str:
    return ", ".join(record.citations)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _flag(kind: str, encounter: str | None, text: str, citations: list[str], settles: str | None = None) -> dict:
    return {"kind": kind, "encounter": encounter, "text": text, "citations": _unique(citations), "settles": settles}


def _names(line: str, identifier: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(identifier)}(?!\w)", line) is not None


# Records and identity ----------------------------------------------------------------------------


def _normalized_event(event: ServiceEvent) -> ServiceEvent | None:
    """A copy with an ISO date and HH:MM times, dropping times that cannot be read. None when the date cannot be read."""
    day = _date(event.service_date)
    if day is None:
        return None
    times = {
        name: _time(getattr(event, name)) for name in ("scheduled_start", "scheduled_end", "patient_arrival", "patient_departure")
    }
    present = [
        Interval(start=start, end=end)
        for interval in event.patient_present_intervals
        if (start := _time(interval.start)) and (end := _time(interval.end))
    ]
    cuts = [
        cut.model_copy(update={"start": start, "end": end})
        for cut in event.excluded_intervals
        if (start := _time(cut.start)) and (end := _time(cut.end))
    ]
    return event.model_copy(
        update={"service_date": day, **times, "patient_present_intervals": present, "excluded_intervals": cuts}
    )


def _window(event: ServiceEvent) -> tuple[int, int] | None:
    if event.scheduled_start and event.scheduled_end:
        return to_minutes(event.scheduled_start), to_minutes(event.scheduled_end)
    if event.patient_present_intervals:
        spans = [(to_minutes(interval.start), to_minutes(interval.end)) for interval in event.patient_present_intervals]
        return min(start for start, _ in spans), max(end for _, end in spans)
    return None


def _compatible(first: str, second: str) -> bool:
    """A partner session can be recorded as family therapy in one record and collateral in another."""
    return first == second or {first, second} == {"family_psychotherapy", "collateral_contact"}


def _same_contact(event: ServiceEvent, other: ServiceEvent) -> bool:
    if event.service_date != other.service_date or not _compatible(event.service_type, other.service_type):
        return False
    window, other_window = _window(event), _window(other)
    if window and other_window:
        return window[0] < other_window[1] and other_window[0] < window[1]
    return event.service_type == other.service_type  # no times to compare: only an identical type on the same day merges


def _group(records: list[Record]) -> dict[str, list[Record]]:
    """Every document's view of the same contact under one key."""
    encounter_of_appointment = {
        appointment: record.event.encounter_id
        for record in records
        if record.event.encounter_id
        for appointment in record.event.appointment_ids
    }
    groups: dict[str, list[Record]] = defaultdict(list)
    loose = []
    for record in records:
        event = record.event
        key = (
            event.encounter_id
            or next((encounter_of_appointment[a] for a in event.appointment_ids if a in encounter_of_appointment), None)
            or next(iter(event.appointment_ids), None)
        )
        if key:
            groups[key].append(record)
        else:
            loose.append(record)
    for record in loose:
        key = next((key for key, members in groups.items() if any(_same_contact(record.event, m.event) for m in members)), None)
        if key is None:
            window = _window(record.event)
            key = f"{record.event.service_date} {record.event.service_type}" + (f" {clock(window[0])}" if window else "")
        groups[key].append(record)
    return dict(groups)


def _own_text(record: Record, key: str, ids_by_key: dict[str, set[str]]) -> str:
    """The record's cited lines, minus lines that name another encounter and not this one, such as other rows of a register."""
    lines = record.doc.text.splitlines()
    mine = ids_by_key[key]
    others = {identifier for other, ids in ids_by_key.items() if other != key for identifier in ids} - mine
    cited = [lines[number - 1] for number in verify.cited_lines(record.event) if 1 <= number <= len(lines)]
    return "\n".join(
        line for line in cited if any(_names(line, own) for own in mine) or not any(_names(line, other) for other in others)
    )


def _tier(record: Record, text: str) -> tuple[str, bool]:
    """How much a record can establish, and whether the text of a record labelled a clinical note says it is a draft."""
    event, extraction = record.event, record.doc.extraction
    if event.record_role in ("draft_or_template", "billing_charge"):
        return "unsupported", False
    if event.record_role == "clinical_note" and DRAFT_WORDS.search(text):
        return "unsupported", True
    if (
        extraction.is_copy_or_retransmission
        or event.record_role == "copy_of_another_record"
        or RESENT_WORDS.search(record.doc.text[:600])
    ):
        return "copy", False
    if event.record_role == "schedule_listing":
        return "schedule", False
    if event.record_role == "clinical_note" and (event.signed is False or not extraction.final_or_signed):
        return "unsupported", False
    return "primary", False


def _minutes_without_times(event: ServiceEvent) -> bool:
    """A signed clinical note or attendance record that states patient minutes but no attendance and no presence times."""
    return (
        event.record_role in ("clinical_note", "attendance_record")
        and event.signed is True
        and event.attendance == "not_stated"
        and not (event.patient_present_intervals or event.patient_arrival or event.patient_departure)
        and (event.stated_patient_minutes or 0) > 0
    )


def _date_rank(view: View) -> int:
    """Which record's date is listed when dates disagree: signed clinical note, attendance record, schedule, copy, draft."""
    if view.tier == "primary":
        return 0 if view.record.event.record_role == "clinical_note" else 1
    return 1 + TIER_ORDER[view.tier]


def _presence(view: View) -> list[tuple[int, int]] | None:
    if view.intervals:
        return view.intervals
    event = view.record.event
    start, end = view.arrival or event.scheduled_start, view.departure or event.scheduled_end
    if (view.arrival or view.departure) and start and end:
        return [(to_minutes(start), to_minutes(end))]
    return None


def _differing_part(candidates: list[dict]) -> str:
    spans = [candidate["spans"] for candidate in candidates]
    if not all(spans):
        return "patient-present minutes"
    starts, ends = {span[0][0] for span in spans}, {span[-1][1] for span in spans}
    if len(starts) > 1 and len(ends) > 1:
        return "start and end times"
    return "start time" if len(starts) > 1 else "end time" if len(ends) > 1 else "patient-present minutes"


# Corrections -------------------------------------------------------------------------------------


def _attendance_label(value: str | None) -> str | None:
    label = re.sub(r"[\s-]+", "_", value.strip().lower()) if value else None
    return label if label in get_args(Attendance) else None


def _states(event: ServiceEvent, field_name: str, value: str) -> bool:
    """Whether a record states this value for a corrected field, as a time field or as an edge of its presence."""
    if field_name == "attendance":
        return event.attendance == value
    spans = event.patient_present_intervals
    edge = (spans[0].start if field_name == "patient_arrival" else spans[-1].end) if spans else None
    return value in (getattr(event, field_name), edge)


def _replaced_edge(doc: Doc, members: list[Record], key: str, ids_by_key: dict[str, set[str]], replaced: str) -> str | None:
    """Whether a time correction labelled "other" replaces a start or an end, judged from the encounter's primary records.

    None when the replaced time is no start or end there, or is both, so the correction is flagged instead of guessed.
    """
    starts = ends = False
    for record in members:
        if record.doc is doc or _tier(record, _own_text(record, key, ids_by_key))[0] != "primary":
            continue
        event = record.event
        starts |= replaced in (event.patient_arrival, *(interval.start for interval in event.patient_present_intervals))
        ends |= replaced in (event.patient_departure, *(interval.end for interval in event.patient_present_intervals))
    return None if starts == ends else "patient_arrival" if starts else "patient_departure"


def _assign_corrections(
    docs: list[Doc], groups: dict[str, list[Record]], ids_by_key: dict[str, set[str]]
) -> tuple[dict[str, list[CorrectionRecord]], list[dict]]:
    """Each correction goes to the one encounter it names; anything else is flagged for a reviewer, never guessed."""
    assigned: dict[str, list[CorrectionRecord]] = defaultdict(list)
    flags = []
    for doc in docs:
        for correction in doc.extraction.corrections:
            citations = _citations(doc.doc_id, correction)
            day = _date(correction.service_date)
            if correction.encounter_id:
                keys = [key for key, ids in ids_by_key.items() if correction.encounter_id in ids]
            else:
                keys = [key for key, members in groups.items() if day and any(m.event.service_date == day for m in members)]
            field_name = correction.field
            times_labelled_other = field_name == "other" and bool(
                _time(correction.corrected_value) and _time(correction.replaced_value)
            )
            readable = _attendance_label if field_name == "attendance" else _time
            value = readable(correction.corrected_value) if field_name in CORRECTED or times_labelled_other else None
            replaced = readable(correction.replaced_value) if field_name in CORRECTED or times_labelled_other else None
            if len(keys) > 1 and replaced:  # several encounters that day: keep those whose records state the replaced value
                edges = [field_name] if field_name in CORRECTED else ["patient_arrival", "patient_departure"]
                keys = [
                    key for key in keys if any(_states(m.event, edge, replaced) for m in groups[key] for edge in edges)
                ] or keys
            if times_labelled_other and len(keys) == 1:
                field_name = _replaced_edge(doc, groups[keys[0]], keys[0], ids_by_key, replaced) or field_name
            if len(keys) == 1 and value and field_name in CORRECTED:
                assigned[keys[0]].append(CorrectionRecord(doc, field_name, value, replaced, citations, _authored(doc)))
                continue
            if field_name not in CORRECTED and not times_labelled_other:
                reason = "only arrival, departure, attendance, and time corrections are applied automatically"
            elif not value:
                reason = f"{correction.corrected_value} is not a readable {FIELD_WORDS[field_name]}"
            elif len(keys) > 1:
                reason = f"{len(keys)} encounters share {day} and the correction names none of them"
            elif not keys:
                reason = f"no admitted record describes {correction.encounter_id or day}"
            else:
                reason = f"{replaced} is not only a start or only an end in the signed records of {keys[0]}"
            named = FIELD_WORDS.get(correction.field, f"a field labelled {correction.field.replace('_', ' ')}")
            text = (
                f"The correction of {named} to {correction.corrected_value} ({', '.join(citations)}) is not applied: {reason}. "
                "A reviewer should check it."
            )
            flags.append(_flag("correction_not_applied", keys[0] if len(keys) == 1 else correction.encounter_id, text, citations))
    return assigned, flags


def _apply_corrections(key: str, views: list[View], corrections: list[CorrectionRecord], flag) -> list[str]:
    """Apply corrections in authored order; return the citations of the corrections that changed something."""
    applied = []
    for correction in sorted(corrections, key=lambda c: (c.authored or "", c.doc.doc_id)):
        attribute, words = CORRECTED[correction.field], FIELD_WORDS[correction.field]
        for view in views:
            if view.record.doc is correction.doc:
                continue
            old = getattr(view, attribute)
            if old is None and attribute != "attendance" and view.intervals:
                # A note may record presence only as an interval; its first start or last end is the corrected time.
                old = clock(view.intervals[0][0] if attribute == "arrival" else view.intervals[-1][1])
            if old is None or old == correction.value or (correction.replaced and old != correction.replaced):
                continue
            authored = _authored(view.record.doc)
            if authored and correction.authored and authored > correction.authored:
                flag(
                    "conflict_after_correction",
                    f"{_cite(view.record)} (authored {authored}) still states {words} {old} after the correction to "
                    f"{correction.value} ({', '.join(correction.citations)}, authored {correction.authored}).",
                    view.record.citations + correction.citations,
                    f"An addendum stating the {words} of {key} would settle it.",
                )
                continue
            setattr(view, attribute, correction.value)
            if attribute != "attendance":
                old_minute, new_minute, end = to_minutes(old), to_minutes(correction.value), int(attribute == "departure")
                view.intervals = [
                    tuple(new_minute if (index == end and point == old_minute) else point for index, point in enumerate(span))
                    for span in view.intervals
                ]
            note = f"{words} {old} corrected to {correction.value} ({correction.citations[0]})"
            received = _received(view.record.doc)
            if received and correction.authored and received > correction.authored:
                note += (
                    f"; {view.record.doc.doc_id} was received {received}, after the correction, but was authored "
                    f"{authored}, before it, so the correction applies to it"
                )
            view.notes.append(note)
            applied += correction.citations
    return _unique(applied)


# One encounter -----------------------------------------------------------------------------------


def _resolve(
    key: str, records: list[Record], corrections: list[CorrectionRecord], counted: list[str], ids_by_key: dict[str, set[str]]
) -> tuple[dict, list[dict]]:
    flags: list[dict] = []

    def flag(kind: str, text: str, citations: list[str], settles: str | None = None) -> None:
        flags.append(_flag(kind, key, text, citations, settles))

    views = []
    for record in records:
        text = _own_text(record, key, ids_by_key)
        tier, says_draft = _tier(record, text)
        event = record.event
        intervals = [(to_minutes(interval.start), to_minutes(interval.end)) for interval in event.patient_present_intervals]
        # A primary record that gives only a minute count ("Completed: 45 minutes") establishes delivery; copies,
        # schedules, drafts, unsigned notes, and charges never do this way.
        attendance = "attended" if tier == "primary" and _minutes_without_times(event) else event.attendance
        views.append(View(record, tier, text, event.patient_arrival, event.patient_departure, intervals, attendance))
        if says_draft:
            flag(
                "keyword_contradicts_label",
                f"{_cite(record)} is labelled a clinical note, but its text says draft, unsigned, or template; "
                "it is not evidence that care happened.",
                record.citations,
            )
    views.sort(key=lambda view: (TIER_ORDER[view.tier], view.record.doc.doc_id, view.record.index))
    primary = [view for view in views if view.tier == "primary"]

    considered = [view for view in views if view.tier != "unsupported"] or views
    dates = sorted({view.record.event.service_date for view in considered})
    service_date = min(considered, key=lambda view: (_date_rank(view), view.record.event.service_date)).record.event.service_date

    applied_corrections = _apply_corrections(key, views, corrections, flag)

    # Service type: clinical records first; context records (calls, imports) only describe it.
    def pick_type(candidates: list[View]) -> str | None:
        types = Counter(
            view.record.event.service_type for view in candidates if view.record.event.service_type not in CONTEXT_TYPES
        )
        return types.most_common(1)[0][0] if types else None

    service_type = (
        pick_type([view for view in primary if view.record.event.record_role == "clinical_note"])
        or pick_type(primary)
        or pick_type(views)
        or views[0].record.event.service_type
    )
    stated_types = {view.record.event.service_type for view in views if view.tier != "unsupported"} - CONTEXT_TYPES
    if len(stated_types) > 1:
        flag(
            "service_type_disagreement",
            f"Records of {key} disagree on the service type ({', '.join(sorted(stated_types))}); "
            f"{service_type} is used, from the strongest record.",
            [citation for view in views if view.tier != "unsupported" for citation in view.record.citations],
        )

    # Attendance from the strongest tier that states it.
    status = "not_documented"
    for tier in ("primary", "schedule", "copy"):
        stating = [view for view in views if view.tier == tier and view.attendance != "not_stated"]
        stated = {view.attendance for view in stating}
        if not stated:
            continue
        delivered, missed, absent = stated & DELIVERED, stated & NOT_DELIVERED, stated & {"patient_absent"}
        if delivered and (missed or absent) and service_type not in counted:
            status = min(delivered)  # a context record; the disagreement cannot change any count
        elif delivered and (missed or absent):
            status = "disputed"
            flag(
                "attendance_conflict",
                f"{tier.capitalize()} records of {key} disagree on attendance: "
                + "; ".join(f"{_cite(view.record)} says {view.attendance}" for view in stating)
                + ".",
                [citation for view in stating for citation in view.record.citations],
                f"A signed attendance record or note stating whether the patient attended {key} would settle it.",
            )
        elif delivered:
            status = "attended_partial" if "attended_partial" in delivered else "attended"
        elif missed:
            status = min(missed)  # a booked service the patient missed, more specific than "absent"
        else:
            status = "patient_absent"
        break
    if status == "not_documented" and any(_presence(view) for view in primary):
        status = "attended"  # a signed record states presence times, so the patient took part

    # Keyword cross-check: a counted, delivered service whose own lines say the patient was absent is disputed.
    if service_type in counted and status in DELIVERED:
        contradicting = [
            view
            for view in views
            if view.tier in ("primary", "copy") and view.attendance in DELIVERED and ABSENT_WORDS.search(view.text)
        ]
        if contradicting:
            status = "disputed"
            flag(
                "keyword_contradicts_label",
                f"The extraction marks the patient as attending {key}, but "
                f"{'; '.join(_cite(view.record) for view in contradicting)} says the patient was absent or not seen; "
                "the encounter is held as disputed.",
                [citation for view in contradicting for citation in view.record.citations],
                f"A signed attendance record or note stating whether the patient attended {key} would settle it.",
            )

    # Exclusions: every record that can establish anything may document a break or a disconnect.
    cuts, cut_notes = [], []
    for view in views:
        if view.tier == "unsupported":
            continue
        for cut in view.record.event.excluded_intervals:
            span = (to_minutes(cut.start), to_minutes(cut.end))
            if span not in cuts:
                cuts.append(span)
                cut_notes.append(
                    f"{cut.reason.replace('_', ' ')} {cut.start}-{cut.end} = {span[1] - span[0]} min "
                    f"({', '.join(_citations(view.record.doc.doc_id, cut))})"
                )

    # Presence candidates, one per record that states times or minutes.
    candidates = []
    for view in views:
        if view.tier not in ("primary", "copy"):
            continue
        event = view.record.event
        presence = _presence(view)
        if presence:
            therapy = subtract(presence, cuts)
            candidates.append({"view": view, "minutes": length(therapy), "spans": merge(presence), "therapy": describe(therapy)})
            own = length(subtract(presence, [(to_minutes(cut.start), to_minutes(cut.end)) for cut in event.excluded_intervals]))
            if event.stated_patient_minutes is not None and event.stated_patient_minutes != own and not view.notes:
                flag(
                    "stated_minutes_mismatch",
                    f"{_cite(view.record)} states {event.stated_patient_minutes} minutes, but its own times give {own}.",
                    view.record.citations,
                )
        elif event.stated_patient_minutes is not None:
            candidates.append(
                {
                    "view": view,
                    "minutes": event.stated_patient_minutes,
                    "spans": [],
                    "therapy": f"{event.stated_patient_minutes} minutes as stated (no times given)",
                }
            )
    independent = [candidate for candidate in candidates if candidate["view"].tier == "primary"] or candidates
    values = sorted({candidate["minutes"] for candidate in independent})

    assumption = None
    if status in UNDELIVERED:
        low = high = 0
    elif values:
        low, high = values[0], values[-1]
    elif service_type not in counted:
        low = high = 0  # not counted, so no presence assumption is needed
    else:
        window = next((window for view in views if (window := _window(view.record.event))), None)
        scheduled = length(subtract([window], cuts)) if window else 0
        low, high = (scheduled, scheduled) if status == "attended" else (0, scheduled)
        assumption = (
            f"No record states the patient's actual times; the scheduled slot minus exclusions is used ({scheduled} minutes)."
        )
        flag(
            "presence_from_schedule",
            assumption,
            [citation for view in views for citation in view.record.citations],
            f"A signed note or attendance record stating the patient's actual times for {key} would settle it.",
        )
    sides = "; ".join(f"{_cite(c['view'].record)} gives {c['therapy']} = {c['minutes']} min" for c in independent)
    if len(values) > 1:
        flag(
            "unresolved_minutes_conflict",
            f"Signed records of {key} disagree on patient-present minutes: {sides}. No correction or addendum resolves it, "
            f"so {key} is carried as a range {low}-{high} min.",
            [citation for candidate in independent for citation in candidate["view"].record.citations],
            f"An addendum or signed note stating the {_differing_part(independent)} of {key} would settle it.",
        )

    # Counting decision.
    delivered_by = {view.tier for view in views if view.attendance in DELIVERED or (view.tier == "primary" and _presence(view))}
    if service_type not in counted:
        counts, reason = "no", f"{service_type.replace('_', ' ')} does not count toward the plan goal"
    elif status in NOT_DELIVERED:
        counts, reason = "no", f"not delivered ({status.replace('_', ' ')})"
    elif status == "patient_absent":
        counts, reason = "no", "patient not present"
    elif status == "not_documented":
        counts, reason = "no", "no record establishes that the service was delivered"
    elif not delivered_by & {"primary", "copy"}:
        counts, reason = "disputed", "only a schedule listing says it was completed; no note or attendance record"
    elif status == "disputed" or low == 0 < high:
        counts, reason = "disputed", "records disagree on whether the patient attended"
    elif high == 0:
        counts, reason = "no", "no patient-present minutes"
    else:
        counts, reason = "yes", "patient-present psychotherapy of a counted type"
        if low != high:
            reason += f"; minutes are a range {low}-{high} because records disagree"

    if len(dates) > 1:
        stated_by = {
            day: [c for view in considered if view.record.event.service_date == day for c in view.record.citations]
            for day in dates
        }
        stated_dates = "; ".join(f"{day} ({', '.join(citations)})" for day, citations in stated_by.items())
        effect = (
            "a week that holds either date cannot be determined" if counts != "no" else "it does not count, so no week changes"
        )
        flag(
            "date_conflict",
            f"Records of {key} disagree on the service date: {stated_dates}. Both dates are kept; {service_date} is listed, "
            f"and {effect}.",
            [citation for view in considered for citation in view.record.citations],
            f"An addendum or signed note stating the date of {key} would settle it.",
        )

    unsupported = [view for view in views if view.tier == "unsupported"]
    if unsupported and status not in DELIVERED:
        flag(
            "unsupported_record_for_undelivered_service",
            f"{'; '.join(_cite(view.record) for view in unsupported)} (labelled "
            f"{', '.join(sorted({view.record.event.record_role.replace('_', ' ') for view in unsupported}))}) exists for {key}, "
            f"which the signed records show as {status.replace('_', ' ')}; it is not evidence that care was delivered.",
            [citation for view in unsupported + primary for citation in view.record.citations],
        )

    if len(values) == 1:
        chosen = independent[0]
        if chosen["spans"]:
            calculation = f"{describe(chosen['spans'])} present ({length(chosen['spans'])} min)"
            calculation += (f", minus {'; '.join(cut_notes)}" if cut_notes else "") + f" = {low} min"
        else:
            calculation = chosen["therapy"]
    elif len(values) > 1:
        calculation = f"range {low}-{high} min: {sides}"
    else:
        calculation = assumption or f"{low} min"
    calculation += "".join(f"; {note}" for candidate in independent for note in candidate["view"].notes)

    used = [view for view in views if view.tier != "copy" or not primary]  # a copy is evidence only when its original is missing
    return {
        "encounter": key,
        "service_date": service_date,
        "service_dates": dates,
        "service_type": service_type,
        "modality": next(
            (view.record.event.modality for view in primary if view.record.event.modality != "unknown"),
            views[0].record.event.modality,
        ),
        "status": status,
        "counts_as_therapy": counts,
        "reason": reason,
        "minutes_low": low,
        "minutes_high": high,
        "scheduled": next(
            (
                f"{describe([window])} ({window[1] - window[0]} min)"
                for view in views
                if view.record.event.scheduled_start and (window := _window(view.record.event))
            ),
            None,
        ),
        "patient_present": [f"{clock(start)}-{clock(end)}" for start, end in merge([s for c in independent for s in c["spans"]])],
        "excluded": cut_notes,
        "calculation": calculation if counts != "no" or low else reason,
        "sources": _unique([citation for view in used for citation in view.record.citations] + applied_corrections),
        "flags": [],
    }, flags


# The patient ------------------------------------------------------------------------------------


def _label_disputes(docs, records, key_of, groups, encounters, assigned, counted, ids_by_key) -> list[dict]:
    """The decision model's disputed labels: always flagged; the count is held only when the other label would change it."""
    original = dict(encounters)
    flags = []
    for doc in docs:
        for dispute in doc.disputes:
            name, model_value, other_value = dispute.get("field"), dispute.get("model_value"), dispute.get("jev_value")
            words = FIELD_WORDS.get(name, str(name).replace("_", " "))
            matches = [
                record
                for record in records
                if record.doc is doc
                and record.event.encounter_id == dispute.get("encounter_id")
                and getattr(record.event, str(name), model_value) == model_value
            ]
            if not matches:
                text = f"The decision model reads {doc.doc_id} as {name} {other_value}; no record there says {model_value}."
                flags.append(_flag("label_disputed", dispute.get("encounter_id"), text, []))
            for record in matches:
                key = key_of[id(record)]
                text = (
                    f"The decision model reads {_cite(record)} as {name} {other_value} "
                    f"(confidence {dispute.get('confidence', 0):.2f}); the extraction says {model_value}."
                )
                valid = {"attendance": get_args(Attendance), "service_type": get_args(ServiceType)}.get(name, ())
                if other_value in valid:
                    swapped = [
                        replace(m, event=m.event.model_copy(update={name: other_value})) if m is record else m
                        for m in groups[key]
                    ]
                    other, _ = _resolve(key, swapped, assigned[key], counted, ids_by_key)
                    if other["counts_as_therapy"] != original[key]["counts_as_therapy"]:
                        current = encounters[key]
                        encounters[key] = {
                            **current,
                            "counts_as_therapy": "disputed",
                            "minutes_low": min(current["minutes_low"], other["minutes_low"]),
                            "minutes_high": max(current["minutes_high"], other["minutes_high"]),
                            "reason": f"{current['reason']}; the decision model disputes the {words}, which decides the count",
                        }
                        text += f" That reading would change whether {key} counts, so {key} is held as disputed."
                    else:
                        text += f" That reading would not change whether {key} counts."
                settles = f"A reviewer reading {_cite(record)}, or a signed record stating the {words} of {key}, would settle it."
                flags.append(_flag("label_disputed", key, text, record.citations, settles))
    return flags


def _span(encounter: dict) -> tuple[int, int]:
    spans = [tuple(to_minutes(part) for part in text.split("-")) for text in encounter["patient_present"]]
    return min(start for start, _ in spans), max(end for _, end in spans)


def _overlaps(encounters: list[dict]) -> list[dict]:
    """A patient cannot be in two counted services at once; an overlap means a record is wrong."""
    by_day = defaultdict(list)
    for encounter in encounters:
        if encounter["counts_as_therapy"] != "no" and encounter["patient_present"]:
            by_day[encounter["service_date"]].append(encounter)
    flags = []
    for day, items in sorted(by_day.items()):
        for index, first in enumerate(items):
            for second in items[index + 1 :]:
                earlier, later = sorted((first, second), key=_span)
                (earlier_start, earlier_end), (later_start, later_end) = _span(earlier), _span(later)
                if later_start < earlier_end:
                    first_id, second_id = earlier["encounter"], later["encounter"]
                    text = (
                        f"{first_id} ({clock(earlier_start)}-{clock(earlier_end)}) and {second_id} ({clock(later_start)}-"
                        f"{clock(later_end)}) overlap on {day}; the patient cannot be in both, so one record is wrong."
                    )
                    settles = f"A correction or addendum stating when {first_id} ended or {second_id} started would settle it."
                    flags.append(_flag("overlapping_encounters", first_id, text, earlier["sources"] + later["sources"], settles))
    return flags


def _charges(docs: list[Doc], encounters: list[dict]) -> tuple[list[dict], list[dict]]:
    by_key = {encounter["encounter"]: encounter for encounter in encounters}
    rows, flags = [], []
    for doc in docs:
        for charge in doc.extraction.charges:
            day, citations = _date(charge.service_date), _citations(doc.doc_id, charge)
            encounter = by_key.get(charge.encounter_id or "") or next(
                (
                    e
                    for e in encounters
                    if e["service_date"] == day and e["service_type"].split("_")[0] in charge.description.lower()
                ),
                None,
            )
            rows.append(
                {
                    "charge_id": charge.charge_id,
                    "encounter": encounter["encounter"] if encounter else charge.encounter_id,
                    "service_date": day,
                    "description": charge.description,
                    "status": charge.status,
                    "sources": citations,
                }
            )
            described = f"Charge {charge.charge_id} ({charge.description}, status {charge.status}, {', '.join(citations)})"
            if encounter is None:
                text = f"{described} matches no documented encounter."
                flags.append(_flag("charge_without_encounter", charge.encounter_id, text, citations))
            elif encounter["status"] in UNDELIVERED:
                status = encounter["status"].replace("_", " ")
                text = f"{described} is posted for {encounter['encounter']}, which the signed records show as {status}."
                flags.append(
                    _flag("charge_for_undelivered_service", encounter["encounter"], text, citations + encounter["sources"])
                )
    return rows, flags


def _session_windows(event: ServiceEvent) -> list[tuple[int, int]]:
    """The scheduled window and the present windows of one service event, in minutes."""
    pairs = [
        (event.scheduled_start, event.scheduled_end),
        (event.patient_arrival or event.scheduled_start, event.patient_departure or event.scheduled_end),
        *((interval.start, interval.end) for interval in event.patient_present_intervals),
    ]
    return [(to_minutes(start), to_minutes(end)) for start, end in pairs if start and end]


def _omissions(docs: list[Doc], records: list[Record], key_of: dict[int, str]) -> tuple[list[dict], list[dict]]:
    """Every time and date a document states that no extracted item carries, as coverage notes for a reviewer.

    Only a time inside a scheduled or present window of a service event in the same document becomes a possible_omission
    flag, because a dropped break or a dropped arrival lands there; signature, posting, and call times fall outside.
    """
    notes, flags = [], []
    for doc in docs:
        own = [record for record in records if record.doc is doc]
        inside: dict[int, tuple[list[str], set[str]]] = {}
        for note in verify.coverage(doc.text, doc.extraction):
            number, value = re.fullmatch(r"possible omission: L(\d+) (.+)", note).groups()
            notes.append({"citation": f"{doc.doc_id} L{number}", "value": value})
            clock_value = verify.normalize_time(value)  # None for a date, which never becomes a flag
            minute = to_minutes(clock_value) if clock_value else None
            keys = {
                key_of[id(r)] for r in own if minute is not None and any(s <= minute <= e for s, e in _session_windows(r.event))
            }
            if keys:
                values, line_keys = inside.setdefault(int(number), ([], set()))
                values.append(value)
                line_keys |= keys
        for number, (values, keys) in inside.items():
            text = (
                f"{doc.doc_id} L{number} states {' and '.join(values)}, inside the session window of {', '.join(sorted(keys))}, "
                "which no extracted item carries; a break or a time may be missing."
            )
            flags.append(
                _flag("possible_omission", next(iter(keys)) if len(keys) == 1 else None, text, [f"{doc.doc_id} L{number}"])
            )
    return notes, flags


def _plans(docs: list[Doc]) -> list[dict]:
    """Treatment-plan versions in effective order; a version ends the day before the next one starts."""
    stated: dict[tuple, tuple[tuple, dict]] = {}
    for doc in docs:
        for requirement in doc.extraction.plan_requirements:
            start = _date(requirement.effective_start)
            same = (
                start,
                requirement.min_therapy_days_per_week,
                requirement.min_patient_present_minutes_per_week,
                tuple(requirement.counted_service_types),
            )
            _, row = stated.setdefault(
                same,
                (
                    (start or "", _authored(doc) or "", doc.doc_id),
                    {
                        "effective_date": start,
                        "effective_end": _date(requirement.effective_end),
                        "min_days_per_week": requirement.min_therapy_days_per_week,
                        "min_minutes_per_week": requirement.min_patient_present_minutes_per_week,
                        "week_start_day": calculate.week_start_day(requirement.week_definition),
                        "counted_service_types": list(requirement.counted_service_types),
                        "sources": [],
                    },
                ),
            )
            row["sources"] += _citations(doc.doc_id, requirement)
    rows = [row for _, row in sorted(stated.values(), key=lambda pair: pair[0])]
    for index, row in enumerate(rows):
        row["version"] = index + 1
        # "Counting rules are unchanged" leaves a later version without types; it keeps the earlier version's.
        row["counted_service_types"] = row["counted_service_types"] or list(
            rows[index - 1]["counted_service_types"] if index else THERAPY_TYPES
        )
        following = rows[index + 1]["effective_date"] if index + 1 < len(rows) else None
        if following and (row["effective_end"] is None or row["effective_end"] >= following):
            row["effective_end"] = (date.fromisoformat(following) - timedelta(days=1)).isoformat()
    return rows


def _assessments(docs: list[Doc]) -> list[dict]:
    """Distinct administrations only; a copy of an earlier result is named on its original, never counted again."""
    distinct: dict[tuple, dict] = {}
    copies = []
    for doc in docs:
        for assessment in doc.extraction.assessments:
            same = (assessment.instrument.upper().replace(" ", ""), _date(assessment.completed_on), assessment.total_score)
            citations = _citations(doc.doc_id, assessment)
            if assessment.is_copy_of_earlier_result:
                copies.append((same, assessment, citations))
                continue
            entry = distinct.setdefault(
                same,
                {
                    "instrument": assessment.instrument,
                    "completed_on": same[1],
                    "total_score": assessment.total_score,
                    "copies": [],
                    "sources": [],
                    "note": None,
                },
            )
            entry["sources"] += citations
    for same, assessment, citations in copies:
        if same in distinct:
            distinct[same]["copies"] += citations
        else:
            distinct[same] = {
                "instrument": assessment.instrument,
                "completed_on": same[1],
                "total_score": assessment.total_score,
                "copies": [],
                "sources": citations,
                "note": "known only from a copy; the original form is not in the record",
            }
    return sorted(
        distinct.values(),
        key=lambda row: (row["completed_on"] or "", row["instrument"], -1 if row["total_score"] is None else row["total_score"]),
    )


def build_patient(mrn: str, docs: list[Doc], pending: list[dict] | None = None) -> dict:
    """The clinical abstraction for one patient, with weeks, consecutive shortfalls, and plan comparison."""
    docs = sorted(docs, key=lambda doc: (doc.doc_id, doc.sha))
    records = [
        Record(doc, index, event, _citations(doc.doc_id, event))
        for doc in docs
        for index, raw in enumerate(doc.extraction.service_events)
        if (event := _normalized_event(raw))
    ]
    groups = _group(records)
    key_of = {id(record): key for key, members in groups.items() for record in members}
    ids_by_key = {
        key: {key}
        | {
            identifier
            for record in members
            for identifier in (record.event.encounter_id, *record.event.appointment_ids)
            if identifier
        }
        for key, members in groups.items()
    }
    plans = _plans(docs)
    # ponytail: one counted-type set for every plan version; split by date if a version ever changes which types count.
    counted = sorted({service_type for plan in plans for service_type in plan["counted_service_types"]}) or THERAPY_TYPES
    assigned, flags = _assign_corrections(docs, groups, ids_by_key)
    encounters = {}
    for key, members in groups.items():
        encounters[key], encounter_flags = _resolve(key, members, assigned[key], counted, ids_by_key)
        flags += encounter_flags
    flags += _label_disputes(docs, records, key_of, groups, encounters, assigned, counted, ids_by_key)
    ordered = sorted(encounters.values(), key=lambda encounter: (encounter["service_date"], encounter["encounter"]))
    charges, charge_flags = _charges(docs, ordered)
    coverage_notes, omission_flags = _omissions(docs, records, key_of)
    flags += charge_flags + _overlaps(ordered) + omission_flags
    flags.sort(key=lambda flag: (flag["kind"], flag["encounter"] or "", flag["text"]))
    for encounter in ordered:
        encounter["flags"] = sorted({flag["kind"] for flag in flags if flag["encounter"] == encounter["encounter"]})

    names = Counter(doc.extraction.patient_name for doc in docs if doc.extraction.patient_name)
    service_dates = sorted(encounter["service_date"] for encounter in ordered)
    if plans and plans[0]["effective_date"]:
        period = {
            "start": plans[0]["effective_date"],
            "end": plans[-1]["effective_end"] or max(service_dates or [plans[0]["effective_date"]]),
            "basis": f"treatment plan dates ({', '.join(plans[0]['sources'])})",
        }
    elif service_dates:
        period = {"start": service_dates[0], "end": service_dates[-1], "basis": "first and last documented service"}
    else:
        period = {"start": None, "end": None, "basis": "no dated service or treatment plan"}
    abstraction = {
        "patient": {"mrn": mrn, "name": names.most_common(1)[0][0] if names else None},
        "review_period": period,
        "encounters": ordered,
        "plans": plans,
        "assessments": _assessments(docs),
        "observations": sorted(
            (
                {
                    "observed_on": _date(observation.observed_on),
                    "category": observation.category,
                    "text": observation.statement,
                    "sources": _citations(doc.doc_id, observation),
                }
                for doc in docs
                for observation in doc.extraction.observations
            ),
            key=lambda row: (row["observed_on"] or "", row["category"], row["sources"]),
        ),
        "charges": charges,
        "flags": flags,
        "coverage_notes": coverage_notes,
        "incomplete": list(pending or []),
    }
    abstraction["weeks"] = calculate.weekly(abstraction)
    abstraction["consecutive_weeks_below_goal"] = calculate.consecutive_shortfalls(abstraction["weeks"])
    abstraction["plan_versions_compared"] = calculate.compare_plan_versions(abstraction)
    return abstraction
