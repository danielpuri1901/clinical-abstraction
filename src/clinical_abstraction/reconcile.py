"""Check, merge, count: the trusted code between the model's extractions and every number an answer prints.

Sections, in order:
1. The referee: admit checks every value the extraction model cites against its cited lines; coverage names what it left out.
2. Reconciliation: build_patient merges one patient's admitted extractions into one abstraction, by the policy at its top.
3. Calculation: weeks against the treatment plan with three-valued goal checks, runs of weeks below goal, and plan comparison.

Nothing here calls a model, and nothing repairs a value: an unsupported value becomes None, or its item is dropped.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from itertools import product
from typing import Literal, get_args

from pydantic import BaseModel

from clinical_abstraction.schema import Attendance, DocumentExtraction, Interval, ServiceEvent, ServiceType

# 1. The referee ================================================================================================================
# The model reads a document once and cites 1-based line numbers. This section checks every cited value against the
# source text. A value it cannot find is set to None, or its whole item is dropped when the value is structural
# (an interval endpoint, a correction value, a required date). Nothing is repaired: a wrong value is never replaced
# by a guessed right one.

Kind = Literal["time", "date", "number", "text"]
DateMention = tuple[int, int, int | None, int, int]  # start, end, year (None when the text omits it), month, day

# H:MM or HH:MM with optional AM or PM. The lookarounds keep it out of decimals and longer numbers.
_CLOCK_TIME = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?!\d)(?:\s*([ap])\.?m\.?(?!\w))?", re.IGNORECASE)
# Bare HHMM such as 1415. It only accepts a claimed value. coverage never scans it: years, doses, and ids look the same.
# ponytail: a bare-time document gets no omission warnings; add a context rule if that loses real omissions.
_BARE_TIME = re.compile(r"(?<![\w:./$#+])(?<![A-Za-z]-)(\d{2})(\d{2})(?![\w:/]|\.\d)")
# A whole number that stands alone, so digits inside ids ("HG-E113", "PHQ-9"), decimals, and times never count.
_WHOLE_NUMBER = re.compile(r"(?<![\w./-])\d+(?!\d|[.,]\d)")

MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH = (
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|(?-i:May|MAY)|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?![A-Za-z])\.?"
)
_YEAR = r"(?:,?\s+(?P<year>(?:19|20)\d{2})(?!\d))?"
_DATE_FORMS = [  # tried in this order; each match is blanked so the next form cannot reuse its digits
    re.compile(r"(?<![\w/-])(?P<year>(?:19|20)\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})(?!\d)"),  # 2026-01-19
    re.compile(r"(?<![\w/.-])(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>(?:19|20)\d{2})(?!\d)"),  # 01/19/2026, month first
    re.compile(rf"(?<![A-Za-z]){_MONTH}\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?!\d){_YEAR}", re.IGNORECASE),  # January 19, 2026; Jan05
    re.compile(
        rf"(?<![\w:./-])(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}{_YEAR}", re.IGNORECASE
    ),  # 19th of January 2026
]


@dataclass(frozen=True)
class Problem:
    document_id: str
    item: str
    field: str
    reason: str


def numbered(text: str) -> str:
    return "\n".join(f"L{number}: {line}" for number, line in enumerate(text.splitlines(), 1))


def cited_lines(item: BaseModel) -> list[int]:
    return sorted(set(getattr(item, "lines", None) or []))


def _line_ranges(numbers: list[int]) -> str:
    """[2, 3, 7] becomes "2-3, 7". The numbers arrive sorted and unique from cited_lines."""
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in runs)


def _format_time(hour: int, minute: int, meridiem: str | None = None) -> str | None:
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        # ponytail: "9:00-10:00 PM" reads 9:00 as AM; handle a shared suffix if such ranges appear.
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _time_from_match(found: re.Match) -> str | None:
    hour, minute, *meridiem = found.groups()
    return _format_time(int(hour), int(minute), *meridiem)


def normalize_time(value: str) -> str | None:
    value = value.strip()
    found = _CLOCK_TIME.fullmatch(value) or _BARE_TIME.fullmatch(value)
    return _time_from_match(found) if found else None


def _is_real_date(year: int | None, month: int, day: int) -> bool:
    try:
        date(year or 2000, month, day)  # 2000 is a leap year, so an unstated year never rejects 29 February
    except ValueError:
        return False
    return True


def _find_dates(line: str) -> tuple[list[DateMention], str]:
    """Real dates in one line, and the line with every date-like span blanked (impossible dates too)."""
    found: list[DateMention] = []
    for form in _DATE_FORMS:
        matches = list(form.finditer(line))
        for match in matches:
            month = match["month"]
            month = int(month) if month.isdigit() else MONTH_NAMES.index(month[:3].lower()) + 1
            year = int(match["year"]) if match["year"] else None
            if _is_real_date(year, month, int(match["day"])):
                found.append((*match.span(), year, month, int(match["day"])))
        for match in matches:
            line = line[: match.start()] + " " * (match.end() - match.start()) + line[match.end() :]
    return found, line


def normalize_date(value: str, default_year: int | None = None) -> str | None:
    value = value.strip()
    found = _find_dates(value)[0]
    if len(found) != 1 or found[0][:2] != (0, len(value)):
        return None
    _, _, year, month, day = found[0]
    try:
        return date(year or default_year, month, day).isoformat() if year or default_year else None
    except ValueError:
        return None


def _find_times(line: str, bare: bool = False) -> list[tuple[int, str]]:
    """Valid times in one line as (position, "HH:MM"). Dates are blanked first so a year is never read as a time."""
    rest = _find_dates(line)[1]
    found = [(match.start(), _time_from_match(match)) for match in _CLOCK_TIME.finditer(rest)]
    if bare:
        found += [(match.start(), _time_from_match(match)) for match in _BARE_TIME.finditer(rest)]
    return [(position, clock) for position, clock in found if clock]


def _find_whole_numbers(line: str) -> set[int]:
    rest = _CLOCK_TIME.sub(" ", _find_dates(line)[1])
    return {int(match.group()) for match in _WHOLE_NUMBER.finditer(rest)}


def _whole_number(value: str) -> int | None:
    found = re.fullmatch(r"\s*(\d+)(?:\s*min(?:ute)?s?)?\s*", value, re.IGNORECASE)  # the model may add the unit
    return int(found[1]) if found else None


def _fold_text(text: str) -> str:
    return " ".join(text.replace("_", " ").split()).lower()


def value_in_lines(value: str, kind: Kind, line_texts: list[str]) -> bool:
    """True when the value, normalized, occurs in the normalized text of the lines. A text value is a substring match."""
    if kind == "text":
        wanted = _fold_text(value)
        return bool(wanted) and wanted in _fold_text(" ".join(line_texts))
    if kind == "time":
        clock = normalize_time(value)
        return clock is not None and any(clock == found for line in line_texts for _, found in _find_times(line, bare=True))
    if kind == "date":
        iso = normalize_date(value)
        if iso is None:
            return False
        year, month, day = map(int, iso.split("-"))
        # A date written without a year matches any year; one written with a year must match it.
        return any(
            (found_month, found_day) == (month, day) and found_year in (None, year)
            for line in line_texts
            for _, _, found_year, found_month, found_day in _find_dates(line)[0]
        )
    if kind == "number":
        number = _whole_number(value)
        return number is not None and any(number in _find_whole_numbers(line) for line in line_texts)
    raise ValueError(f"unknown kind {kind!r}")


def admit(document_id: str, text: str, extraction: DocumentExtraction) -> tuple[DocumentExtraction, list[Problem]]:
    """Return a copy of the extraction holding only values the text supports, and one Problem per value removed.

    Times, numbers, and correction values must sit in the item's own cited lines. Dates may sit anywhere in the document,
    because most documents state the service date once in a header.
    """
    kept = extraction.model_copy(deep=True)
    document = text.splitlines()
    problems: list[Problem] = []

    def cites_real_lines(path: str, owner: BaseModel) -> bool:
        lines = cited_lines(owner)
        missing = [number for number in lines if not 1 <= number <= len(document)]
        if not lines:
            problems.append(Problem(document_id, path, "lines", "cites no lines"))
        elif missing:
            problems.append(
                Problem(document_id, path, "lines", f"lines {_line_ranges(missing)} not in document of {len(document)} lines")
            )
        return bool(lines) and not missing

    def supported(path: str, field: str, value: str, kind: Kind, owner: BaseModel) -> bool:
        lines = cited_lines(owner)
        if value_in_lines(value, kind, document if kind == "date" else [document[number - 1] for number in lines]):
            return True
        where = "document" if kind == "date" else f"cited lines {_line_ranges(lines)}"
        problems.append(Problem(document_id, path, field, f"{value} not in {where}"))
        return False

    def none_if_unsupported(path: str, owner, field: str, kind: Kind) -> None:
        value = getattr(owner, field)
        if value is not None and not supported(path, field, str(value), kind, owner):
            setattr(owner, field, None)

    def keep_admitted(path: str, items: list, admitted: Callable) -> list:
        return [item for index, item in enumerate(items) if admitted(f"{path}[{index}]", item)]

    def interval_supported(path: str, interval, owner: BaseModel) -> bool:
        start = supported(path, "start", interval.start, "time", owner)
        end = supported(path, "end", interval.end, "time", owner)  # check both so one Problem names each bad endpoint
        return start and end

    def admit_event(path: str, event) -> bool:
        if not cites_real_lines(path, event) or not supported(path, "service_date", event.service_date, "date", event):
            return False
        for name in ("scheduled_start", "scheduled_end", "patient_arrival", "patient_departure"):
            none_if_unsupported(path, event, name, "time")
        event.patient_present_intervals = keep_admitted(
            f"{path}.patient_present_intervals",
            event.patient_present_intervals,
            lambda interval_path, interval: interval_supported(interval_path, interval, event),
        )
        none_if_unsupported(path, event, "stated_patient_minutes", "number")
        event.excluded_intervals = keep_admitted(
            f"{path}.excluded_intervals",
            event.excluded_intervals,
            lambda interval_path, interval: (
                cites_real_lines(interval_path, interval) and interval_supported(interval_path, interval, interval)
            ),
        )
        return True

    def admit_correction(path: str, correction) -> bool:
        if not cites_real_lines(path, correction):
            return False
        kind = {"patient_arrival": "time", "patient_departure": "time", "patient_minutes": "number"}.get(correction.field, "text")
        none_if_unsupported(path, correction, "service_date", "date")
        corrected_value_found = supported(path, "corrected_value", correction.corrected_value, kind, correction)
        none_if_unsupported(path, correction, "replaced_value", kind)
        return corrected_value_found

    def admit_plan_requirement(path: str, requirement) -> bool:
        if not cites_real_lines(path, requirement):
            return False
        for name in ("effective_start", "effective_end"):
            none_if_unsupported(path, requirement, name, "date")
        for name in ("min_therapy_days_per_week", "min_patient_present_minutes_per_week"):
            none_if_unsupported(path, requirement, name, "number")
        return True

    def admit_assessment(path: str, assessment) -> bool:
        if not cites_real_lines(path, assessment):
            return False
        none_if_unsupported(path, assessment, "total_score", "number")
        assessment.item_scores = keep_admitted(
            f"{path}.item_scores",
            assessment.item_scores,
            lambda score_path, item_score: supported(score_path, "score", str(item_score.score), "number", assessment),
        )
        none_if_unsupported(path, assessment, "completed_on", "date")
        none_if_unsupported(path, assessment, "completed_time", "time")
        return True

    def admit_authorization(path: str, authorization) -> bool:
        if not cites_real_lines(path, authorization):
            return False
        for name in ("start", "end"):
            none_if_unsupported(path, authorization, name, "date")
        return True

    def admit_charge(path: str, charge) -> bool:
        if not cites_real_lines(path, charge):
            return False
        none_if_unsupported(path, charge, "service_date", "date")
        return True

    def admit_observation(path: str, observation) -> bool:
        return cites_real_lines(path, observation) and supported(
            path, "observed_on", observation.observed_on, "date", observation
        )

    kept.service_events = keep_admitted("service_events", kept.service_events, admit_event)
    kept.corrections = keep_admitted("corrections", kept.corrections, admit_correction)
    kept.plan_requirements = keep_admitted("plan_requirements", kept.plan_requirements, admit_plan_requirement)
    kept.assessments = keep_admitted("assessments", kept.assessments, admit_assessment)
    kept.authorizations = keep_admitted("authorizations", kept.authorizations, admit_authorization)
    kept.charges = keep_admitted("charges", kept.charges, admit_charge)
    kept.observations = keep_admitted("observations", kept.observations, admit_observation)
    return kept, problems


def _date_is_carried(year: int | None, month: int, day: int, carried: set[tuple[int | None, int, int]]) -> bool:
    return any(
        (carried_month, carried_day) == (month, day) and (year is None or carried_year is None or year == carried_year)
        for carried_year, carried_month, carried_day in carried
    )


def coverage(text: str, extraction: DocumentExtraction) -> list[str]:
    """Name every time and date in the text that no admitted item carries, as "possible omission: L7 09:10".

    A value counts as carried when any field of the extraction holds it, written in any format. Times are the HH:MM kind.
    """
    everything_stated = extraction.model_dump_json()
    carried_times = {clock for _, clock in _find_times(everything_stated, bare=True)}
    carried_dates = {(year, month, day) for _, _, year, month, day in _find_dates(everything_stated)[0]}
    notes: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        missing = [(position, clock) for position, clock in _find_times(line) if clock not in carried_times]
        for start, end, year, month, day in _find_dates(line)[0]:
            if not _date_is_carried(year, month, day, carried_dates):
                missing.append((start, f"{year:04d}-{month:02d}-{day:02d}" if year else line[start:end].strip()))
        for _, value in sorted(missing):
            note = f"possible omission: L{number} {value}"
            if note not in notes:
                notes.append(note)
    return notes


# 2. Reconciliation =============================================================================================================
# Deterministic code that turns admitted extractions into one abstraction per patient.
#
# Policy, in the order it is applied:
# 1. Identity. Records of the same contact merge by encounter id, then appointment id, then the same date with a compatible
#    service type and overlapping times.
# 2. Evidence tiers. Signed clinical notes, attendance records, and telehealth logs are primary. Schedule listings say what was
#    booked. Copies repeat their original. Drafts, templates, unsigned notes, and charges never establish that care happened.
# 3. Dates. Records that share an encounter id but disagree on the date keep both dates and raise a date conflict.
# 4. Corrections apply in authored order to every record authored before them, copies received later included. Receipt time
#    never decides. A record authored after a correction that still disagrees is a conflict, not an override.
# 5. Minutes are presence intervals minus breaks, disconnects, and parts without the patient. Primary records that still
#    disagree are carried as a range [low, high].
# 6. Counting. A service counts only if the plan counts its type and a record establishes that it was delivered.
# 7. Second readers. Keyword rules and the decision model's label disputes can hold an encounter as disputed. Neither ever
#    creates care, and a dispute changes the count only when the other label would change it.
# Every conflict flag cites both sides and names the document that would settle it.
#
# The extractions arrive admitted by admit, so every value sits in its cited lines. admit keeps the model's spelling of a
# value, so this section normalizes dates and times before using them. Citations read "D7 L3" or "D7 L3-5".

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
    return normalize_time(value) if value else None


def _date(value: str | None) -> str | None:
    return normalize_date(value) if value else None


def _moment(value: str | None) -> str | None:
    """A timestamp as "YYYY-MM-DDTHH:MM", or the date alone, so two clocks compare as text. None when unreadable."""
    if not value:
        return None
    day, _, time_of_day = value.strip().replace(" ", "T", 1).partition("T")
    iso = normalize_date(day)
    hour = time_of_day and (normalize_time(time_of_day) or normalize_time(time_of_day[:5]))
    return iso and (f"{iso}T{hour}" if hour else iso)


def _authored(doc: Doc) -> str | None:
    """Valid time: when the content was written. A copy carries its original's authored time."""
    return _moment(doc.authored_at) or _moment(doc.extraction.authored_at)


def _received(doc: Doc) -> str | None:
    """Transaction time: when the system got the document. It never decides which statement wins."""
    return _moment(doc.received_at) or _moment(doc.extraction.received_at)


def _citations(doc_id: str, item: BaseModel) -> list[str]:
    lines = cited_lines(item)
    return [f"{doc_id} L{run}" for run in _line_ranges(lines).split(", ")] if lines else []


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
    cited = [lines[number - 1] for number in cited_lines(record.event) if 1 <= number <= len(lines)]
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
        for note in coverage(doc.text, doc.extraction):
            number, value = re.fullmatch(r"possible omission: L(\d+) (.+)", note).groups()
            notes.append({"citation": f"{doc.doc_id} L{number}", "value": value})
            clock_value = normalize_time(value)  # None for a date, which never becomes a flag
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
                        "week_start_day": week_start_day(requirement.week_definition),
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

    def entry(assessment, completed_on: str | None, note: str | None = None) -> dict:
        found = {"instrument": assessment.instrument, "completed_on": completed_on, "total_score": assessment.total_score}
        return found | {"copies": [], "sources": [], "note": note}

    for doc in docs:
        for assessment in doc.extraction.assessments:
            same = (assessment.instrument.upper().replace(" ", ""), _date(assessment.completed_on), assessment.total_score)
            citations = _citations(doc.doc_id, assessment)
            if assessment.is_copy_of_earlier_result:
                copies.append((same, assessment, citations))
            else:
                distinct.setdefault(same, entry(assessment, same[1]))["sources"] += citations
    for same, assessment, citations in copies:
        if same in distinct:
            distinct[same]["copies"] += citations
        else:
            note = "known only from a copy; the original form is not in the record"
            distinct[same] = entry(assessment, same[1], note) | {"sources": citations}
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
    abstraction["weeks"] = weekly(abstraction)
    abstraction["consecutive_weeks_below_goal"] = consecutive_shortfalls(abstraction["weeks"])
    abstraction["plan_versions_compared"] = compare_plan_versions(abstraction)
    return abstraction


# 3. Calculation ================================================================================================================
# Pure functions over one patient's abstraction.
#
# Uncertain encounters carry minutes as a range [low, high], and disputed encounters count only toward the high bounds.
# Goal checks use three-valued logic: a week is "met" only if the low bounds meet the goal, "not_met" only if even the high
# bounds miss it, and "cannot_determine" otherwise. The answer is then the same for every way the open conflicts could be
# resolved. A week holding an encounter with conflicting dates, or any week of a patient with documents still pending, is
# "cannot_determine" whatever its totals say.

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
COUNTED = ("yes", "disputed")
TOTALS = ("therapy_days_low", "therapy_days_high", "minutes_low", "minutes_high")


def week_start_day(definition: str | None) -> int:
    """Weekday a plan's week starts on, from text such as 'Monday-Sunday' or 'Sunday through Saturday'. Default Monday."""
    lowered = (definition or "").lower()
    found = [(lowered.find(day), index) for index, day in enumerate(DAYS) if day in lowered]
    return min(found)[1] if found else 0


def weeks_between(start: str, end: str, first_day: int = 0) -> list[tuple[date, date]]:
    """Seven-day weeks starting on `first_day` (0 = Monday), clipped to [start, end]."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    week_start = first - timedelta(days=(first.weekday() - first_day) % 7)
    weeks = []
    while week_start <= last:
        weeks.append((max(week_start, first), min(week_start + timedelta(days=6), last)))
        week_start += timedelta(days=7)
    return weeks


def plan_for(plans: list[dict], day: date | str) -> dict | None:
    day = str(day)  # str(date) is the ISO form
    return next((plan for plan in plans if (plan["effective_date"] or "0000") <= day <= (plan["effective_end"] or "9999")), None)


def summarize(encounters: list[dict]) -> dict:
    """Session, day, and minute totals with low and high bounds, overall and by service type."""
    certain = [encounter for encounter in encounters if encounter["counts_as_therapy"] == "yes"]
    possible = [encounter for encounter in encounters if encounter["counts_as_therapy"] in COUNTED]
    by_type: dict[str, dict] = {}
    for encounter in possible:
        row = by_type.setdefault(
            encounter["service_type"], {"sessions_low": 0, "sessions_high": 0, "minutes_low": 0, "minutes_high": 0}
        )
        row["sessions_high"] += 1
        row["minutes_high"] += encounter["minutes_high"]
        if encounter["counts_as_therapy"] == "yes":
            row["sessions_low"] += 1
            row["minutes_low"] += encounter["minutes_low"]
    return {
        "sessions_low": len(certain),
        "sessions_high": len(possible),
        "therapy_days_low": len({encounter["service_date"] for encounter in certain}),
        "therapy_days_high": len({encounter["service_date"] for encounter in possible}),
        "minutes_low": sum(encounter["minutes_low"] for encounter in certain),
        "minutes_high": sum(encounter["minutes_high"] for encounter in possible),
        "by_service_type": by_type,
    }


def range_text(low, high) -> str:
    """A value with its bounds: "40" when they agree, else "40-50"."""
    return str(low) if low == high else f"{low}-{high}"


def count_text(low: int, high: int, noun: str) -> str:
    """A count with its bounds and its noun: "1 therapy day", "2-3 therapy days"."""
    return f"{range_text(low, high)} {noun}{'' if low == high == 1 else 's'}"


def goal_status(plan: dict | None, days_low: int, days_high: int, minutes_low: int, minutes_high: int) -> tuple[str, str]:
    if plan is None or (plan["min_days_per_week"] is None and plan["min_minutes_per_week"] is None):
        return "cannot_determine", "no measurable treatment-plan goal is documented for this week"
    need_days, need_minutes = plan["min_days_per_week"] or 0, plan["min_minutes_per_week"] or 0
    goal = f"goal of {count_text(need_days, need_days, "therapy day")} and {need_minutes} minutes"
    stated = f"{count_text(days_low, days_high, "therapy day")} and {range_text(minutes_low, minutes_high)} minutes"
    if days_low >= need_days and minutes_low >= need_minutes:
        return "met", f"{stated} meet the {goal}"
    if days_high < need_days or minutes_high < need_minutes:
        misses = [f"{count_text(days_high, days_high, "therapy day")} < {need_days}"] if days_high < need_days else []
        misses += [f"{minutes_high} minutes < {need_minutes}"] if minutes_high < need_minutes else []
        return "not_met", f"{stated} miss the {goal} ({'; '.join(misses)})"
    return "cannot_determine", (
        f"{stated}: the {goal} is met at the high bound but not at the low bound, so it depends on an unresolved record"
    )


def _dates(encounter: dict) -> list[str]:
    return encounter.get("service_dates") or [encounter["service_date"]]


def _week_totals(encounters: list[dict], start: str, end: str) -> dict:
    """Bounds over every way an encounter with conflicting dates could be placed, so no placement is assumed."""
    touching = [encounter for encounter in encounters if any(start <= day <= end for day in _dates(encounter))]
    fixed = [encounter for encounter in touching if len(_dates(encounter)) == 1]
    # ponytail: 2**n placements for n date-conflicted encounters in one week; fine while n stays below about 15.
    moving = [
        [{**encounter, "service_date": day} for day in _dates(encounter)] for encounter in touching if len(_dates(encounter)) > 1
    ]
    totals = [summarize([e for e in [*fixed, *placed] if start <= e["service_date"] <= end]) for placed in product(*moving)]
    return {key: (min if key.endswith("_low") else max)(total[key] for total in totals) for key in TOTALS}


def weekly(abstraction: dict) -> list[dict]:
    period, plans, pending = abstraction["review_period"], abstraction["plans"], abstraction.get("incomplete") or []
    if not period["start"] or not period["end"]:
        return []
    settles: dict[str, list[str]] = {}
    for flag in abstraction["flags"]:
        if flag["settles"]:
            settles.setdefault(flag["encounter"], []).append(flag["settles"])
    pending_ids = ", ".join(str(document.get("doc_id")) for document in pending)
    rows = []
    for first, last in weeks_between(period["start"], period["end"], plans[0]["week_start_day"] if plans else 0):
        start, end = first.isoformat(), last.isoformat()
        counted = [
            encounter
            for encounter in abstraction["encounters"]
            if encounter["counts_as_therapy"] in COUNTED and any(start <= day <= end for day in _dates(encounter))
        ]
        totals = _week_totals(abstraction["encounters"], start, end)
        plan = plan_for(plans, first)
        status, explanation = goal_status(plan, *(totals[key] for key in TOTALS))
        open_questions = []
        moving = [encounter["encounter"] for encounter in counted if len(_dates(encounter)) > 1]
        if moving:
            open_questions.append(f"{', '.join(moving)} {'has' if len(moving) == 1 else 'have'} conflicting dates")
        if pending:
            open_questions.append(
                f"{len(pending)} pending document{'s' if len(pending) > 1 else ''} ({pending_ids}) could change it"
            )
        if open_questions:
            status = "cannot_determine"
            explanation += f"; {' and '.join(open_questions)}, so it cannot be determined"
        if (last - first).days < 6:
            explanation += f"; partial week ({start} to {end}) inside the review period"
        if status == "cannot_determine":
            sentences = [sentence for encounter in counted for sentence in settles.get(encounter["encounter"], [])]
            if plan is None or (plan["min_days_per_week"] is None and plan["min_minutes_per_week"] is None):
                sentences.append("A signed treatment plan stating the weekly goal would settle it.")
            if pending:
                sentences.append(f"Reading the pending document{'s' if len(pending) > 1 else ''} {pending_ids} would settle it.")
            explanation += "." + "".join(f" {sentence}" for sentence in dict.fromkeys(sentences)) if sentences else ""
        rows.append(
            {
                "week_start": start,
                "week_end": end,
                **totals,
                "status": status,
                "explanation": explanation,
                "plan_version": plan["version"] if plan else None,
                "encounters": [encounter["encounter"] for encounter in counted],
            }
        )
    return rows


def consecutive_shortfalls(weeks: list[dict], run_length: int = 2) -> dict:
    """Runs of `run_length` consecutive weeks below goal: established, or dependent on unresolved records."""
    established, possible = [], []
    for index in range(len(weeks) - run_length + 1):
        window = weeks[index : index + run_length]
        label = [week["week_start"] for week in window]
        if all(week["status"] == "not_met" for week in window):
            established.append(label)
        elif all(week["status"] in ("not_met", "cannot_determine") for week in window):
            possible.append(label)
    return {"run_length": run_length, "established": established, "depends_on_unresolved_records": possible}


def compare_plan_versions(abstraction: dict) -> dict:
    """Care delivered under each treatment-plan version, normalized per week, so a plan change can be compared."""
    period, rows = abstraction["review_period"], []
    for plan in abstraction["plans"] if period["start"] and period["end"] else []:
        start = max(plan["effective_date"] or period["start"], period["start"])
        end = min(plan["effective_end"] or period["end"], period["end"])
        if start > end:
            continue
        totals = summarize([encounter for encounter in abstraction["encounters"] if start <= encounter["service_date"] <= end])
        weeks = ((date.fromisoformat(end) - date.fromisoformat(start)).days + 1) / 7
        rows.append(
            {
                "plan_version": plan["version"],
                "start": start,
                "end": end,
                "weeks": round(weeks, 2),
                "goal_days": plan["min_days_per_week"],
                "goal_minutes": plan["min_minutes_per_week"],
                "sources": plan["sources"],
                **totals,
                "minutes_per_week_low": round(totals["minutes_low"] / weeks, 1),
                "minutes_per_week_high": round(totals["minutes_high"] / weeks, 1),
            }
        )
    if len(rows) > 1:
        note = None
    elif rows:
        note = "Only one treatment-plan version is documented, so there is no before and after change."
    else:
        note = "No treatment-plan version falls inside the review period."
    return {"versions": rows, "note": note}
