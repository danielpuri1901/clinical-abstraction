"""The referee: trusted checks on what the extraction model claims about one document.

The model reads a document once and cites 1-based line numbers. This module checks every cited value against the
source text. A value it cannot find is set to None, or its whole item is dropped when the value is structural
(an interval endpoint, a correction value, a required date). Nothing is repaired: a wrong value is never replaced
by a guessed right one.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel

from clinical_abstraction.schema import DocumentExtraction

Kind = Literal["time", "date", "number", "text"]
DateMention = tuple[int, int, int | None, int, int]  # start, end, year (None when the text omits it), month, day

# H:MM or HH:MM with optional AM or PM. The lookarounds keep it out of decimals and longer numbers.
_CLOCK_TIME = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?!\d)(?:\s*([ap])\.?m\.?(?!\w))?", re.IGNORECASE)
# Bare HHMM such as 1415. It only accepts a claimed value. coverage never scans it: years, doses, and ids look the same.
# ponytail: a bare-time document gets no omission warnings; add a context rule if that loses real omissions.
_BARE_TIME = re.compile(r"(?<![\w:./$#+])(?<![A-Za-z]-)(\d{2})(\d{2})(?![\w:/]|\.\d)")
# A whole number that stands alone, so digits inside ids ("HG-E113", "PHQ-9"), decimals, and times never count.
_WHOLE_NUMBER = re.compile(r"(?<![\w./-])\d+(?!\d|[.,]\d)")

_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
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
            month = int(month) if month.isdigit() else _MONTH_NAMES.index(month[:3].lower()) + 1
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
        for field in ("scheduled_start", "scheduled_end", "patient_arrival", "patient_departure"):
            none_if_unsupported(path, event, field, "time")
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
        for field in ("effective_start", "effective_end"):
            none_if_unsupported(path, requirement, field, "date")
        for field in ("min_therapy_days_per_week", "min_patient_present_minutes_per_week"):
            none_if_unsupported(path, requirement, field, "number")
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
        for field in ("start", "end"):
            none_if_unsupported(path, authorization, field, "date")
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
