"""Answer a question: read it into a typed plan, compute the answer from the stored tables, and hand open questions to an agent.

Sections, in order:
1. Plans: code reads patients and dates; keyword rules and the decision model, as two readers, pick the enum values.
2. Answers by code: one executor per metric over the derived tables, Markdown that cites the source lines, the verdict, and
   verify_saved, which replays saved plans without a model.
3. The open-question agent: Claude with four read-only tools, then code checks that every number and citation it writes came
   from a tool. The executors in section 2 reuse its encounter query and its citation check.
"""

import calendar
import json
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, get_args

from clinical_abstraction import read, reconcile, store
from clinical_abstraction.read import Choice, ChoiceAnswer, Jev
from clinical_abstraction.reconcile import count_text, normalize_date, range_text
from clinical_abstraction.store import runs_text, table_lines

# 1. Plans ======================================================================================================================
# Code parses what code is good at: patients (MRN or name, matched against the store) and dates. An unknown patient or an
# impossible date raises PlanError, so no numbers are printed. The enum values (metric, attendance, service type, grouping)
# get two readings: keyword rules propose candidates and the decision model (Jev) chooses. A field is agreed when Jev's
# choice is among the rule candidates, or when the rules propose nothing and Jev is at least 0.9 sure. Anything else
# becomes a warning that prints both readings, so a misread surfaces instead of hiding behind a plain COMPUTED verdict.

Metric = Literal[
    "sessions", "minutes", "days", "goal_status", "consecutive_shortfall", "plan_change", "progress", "billing_integrity", "other"
]
AttendanceFilter = Literal["attended", "missed_by_patient", "cancelled_by_clinic", "any", "not_applicable"]
ServiceTypeFilter = Literal[  # values of schema.ServiceType, plus "any"
    "individual_psychotherapy",
    "group_psychotherapy",
    "family_psychotherapy",
    "collateral_contact",
    "medication_management",
    "any",
]
GroupBy = Literal["none", "week", "day", "service_type", "plan_version"]

ENUMS = {"metric": Metric, "attendance": AttendanceFilter, "service_type": ServiceTypeFilter, "group_by": GroupBy}
FILTERED_METRICS = {"sessions", "minutes", "days"}  # the only metrics whose answers filter and group
NEUTRAL = {"attendance": "not_applicable", "service_type": "any", "group_by": "none"}  # every other metric gets these
RULES_ONLY_DEFAULT = {"attendance": "attended", "service_type": "any", "group_by": "none"}  # filtered metric, no rule fired
AGREEMENT_CONFIDENCE = 0.9  # Jev decides a field alone only when the rules propose nothing and Jev is at least this sure


class PlanError(Exception):
    """The question cannot be planned. The message tells the person asking what was not understood."""


@dataclass(frozen=True)
class DateRange:
    start: date | None  # None: open at the start ("before January 19")
    end: date | None  # None: open at the end ("after January 19")
    label: str


@dataclass
class Plan:
    kind: Literal["computed", "open"]
    metric: Metric
    attendance: AttendanceFilter
    service_type: ServiceTypeFilter
    group_by: GroupBy
    patients: list[str]
    date_range: DateRange | None
    readings: dict  # field -> {"rules": candidates, "decision_model": [choice, confidence] or None}
    warnings: list[str]


# ---- Dates ----

_MONTH = (  # "May" only capitalized, so the verb "may" is no month; the lookahead keeps "decrease" from reading as "dec"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|(?-i:May|MAY)|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?![a-z])\.?"
)
_MONTH_KEYS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_YEAR = r"(?:19|20)\d{2}(?!\d)"
_DAY = r"\d{1,2}(?:st|nd|rd|th)?(?!\d)"
_DATE = (  # one date as written; verify.normalize_date reads it, so questions and the referee accept the same forms
    rf"(?<![\w/.-]){_YEAR}[-/]\d{{1,2}}[-/]\d{{1,2}}(?![\d/])"  # 2026-01-19
    rf"|(?<![\w/.-])\d{{1,2}}/\d{{1,2}}(?:/{_YEAR})?(?![\d/])"  # 1/19 or 01/19/2026, month first
    rf"|(?<![\w:./-])\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+{_YEAR})?"  # 30 January 2026, 22nd of January
    rf"|(?<![a-z]){_MONTH}\s*{_DAY}(?:,?\s+{_YEAR})?"  # January 19, 2026; Jan. 19; January 19th
)
_DASH = r"\s*[-\u2013\u2014]\s*"
_RANGE_END = rf"(?P<second>{_DATE}|{_DAY})(?:,?\s+(?P<year>{_YEAR}))?"
_DATES = re.compile(_DATE, re.IGNORECASE)
_RANGES = [
    re.compile(rf"\bbetween\s+(?P<first>{_DATE})\s+and\s+{_RANGE_END}", re.IGNORECASE),
    re.compile(rf"\bfrom\s+(?P<first>{_DATE})(?:\s+(?:to|through|until)\s+|{_DASH}){_RANGE_END}", re.IGNORECASE),
    re.compile(rf"(?P<first>{_DATE}){_DASH}{_RANGE_END}", re.IGNORECASE),  # January 5-30, 2026
]
_WEEK_OF = re.compile(rf"\bweek\s+of\s+(?:the\s+)?(?P<first>{_DATE})", re.IGNORECASE)
_STARTS = re.compile(
    rf"\b(?P<word>on\s+or\s+after|after|since|starting(?:\s+on)?|from)\s+(?:the\s+)?(?P<first>{_DATE})", re.IGNORECASE
)
_ENDS = re.compile(
    rf"\b(?P<word>on\s+or\s+before|before|until|till|through|up\s+to)\s+(?:the\s+)?(?P<first>{_DATE})", re.IGNORECASE
)
_MONTH_ONLY = re.compile(rf"(?<![a-z]){_MONTH}(?!\s*\d{{1,2}}(?!\d))(?:\s+(?P<year>{_YEAR}))?", re.IGNORECASE)
_WEEK_NUMBER = re.compile(r"\bweeks?\s+(?:number\s+|no\.?\s*|#)?\d{1,2}(?![\d/-])", re.IGNORECASE)


def _read_date(written: str, year: int) -> date:
    written = written.strip()
    with_year = f"{written}/{year}" if re.fullmatch(r"\d{1,2}/\d{1,2}", written) else written
    value = normalize_date(with_year, year)
    if value is None:
        raise PlanError(f"{written} is not a valid date")
    return date.fromisoformat(value)


def _read_range_end(written: str, start: date, year: int) -> date:
    """The second date of a range; a bare day ("January 12 to 25") takes the first date's month and year."""
    if not re.fullmatch(_DAY, written.strip()):
        return _read_date(written, year)
    day = int(re.match(r"\d+", written.strip()).group())
    try:
        return date(start.year, start.month, day)
    except ValueError:
        raise PlanError(f"{calendar.month_name[start.month]} {day} is not a valid date") from None


def _span(start: date, end: date) -> DateRange:
    if end < start:
        raise PlanError(f"{start} to {end} ends before it starts")
    return DateRange(start, end, str(start) if start == end else f"{start} to {end}")


def parse_dates(question: str, default_year: int) -> DateRange | None:
    """The date range a question asks about, or None for the whole record. An impossible date raises PlanError."""
    if found := _WEEK_NUMBER.search(question):  # week numbers differ per patient's episode
        raise PlanError(f"{found.group()} is not a date; name the week by a date, for example the week of January 12")
    dates = [_read_date(found.group(), default_year) for found in _DATES.finditer(question)]  # refuses impossible dates
    for form in _RANGES:
        if found := form.search(question):
            hint = re.search(_YEAR, found["year"] or found["second"])  # "January 5-30, 2026" puts the year on the end
            year = int(hint.group()) if hint else default_year
            start = _read_date(found["first"], year)
            return _span(start, _read_range_end(found["second"], start, year))
    if found := _WEEK_OF.search(question):
        day = _read_date(found["first"], default_year)
        monday = day - timedelta(days=day.weekday())
        return _span(monday, monday + timedelta(days=6))
    starts, ends = _STARTS.search(question), _ENDS.search(question)
    if starts or ends:
        start = end = None
        labels = []
        if starts:
            word, day = " ".join(starts["word"].lower().split()), _read_date(starts["first"], default_year)
            start = day + timedelta(days=1) if word == "after" else day
            labels.append(f"{word} {day}")
        if ends:
            word, day = " ".join(ends["word"].lower().split()), _read_date(ends["first"], default_year)
            end = day - timedelta(days=1) if word == "before" else day
            labels.append(f"{word} {day}")
        if start and end and end < start:
            raise PlanError(f"{' and '.join(labels)} is an empty range")
        return DateRange(start, end, " and ".join(labels))
    if dates:
        # ponytail: separate dates become one span ("January 19 and 21" includes the 20th); keep a list if gaps matter.
        return _span(min(dates), max(dates))
    if found := _MONTH_ONLY.search(question):
        year = int(found["year"]) if found["year"] else default_year
        month = _MONTH_KEYS.index(found.group()[:3].lower()) + 1
        return _span(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
    return None


# ---- Patients ----

_CALENDAR_WORD = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?"
    r"|Nov(?:ember)?|Dec(?:ember)?|Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?"
    r"|Sat(?:urday)?|Sun(?:day)?)(?![a-z])"
)
_NAME_WORD = rf"(?!{_CALENDAR_WORD})[A-Z][a-z]+(?:['\u2019]s)?"
_NAME_RUN = re.compile(rf"(?<![\w'\u2019]){_NAME_WORD}(?:\s+{_NAME_WORD})*(?![\w'\u2019])")
_SENTENCE_START = re.compile(r"(?:^|[.?!:])[\s\"'(\u201c\u2018]*$")


def parse_patients(question: str, known: list[tuple[str, str]]) -> list[str]:
    """MRNs of the patients a question names, in store order. No name means every known patient (collection scope).

    A run of capitalized words that is not a month, a weekday, or the first word of a sentence must be a known full name
    or a unique first name; otherwise the question names an unknown patient and PlanError is raised. An MRN shaped like
    a known one but missing from the store is refused the same way.
    ponytail: a lone unknown name opening a sentence ("Morgan attended...") reads as an ordinary word; add a word list if
    that bites.
    """
    if not known:
        raise PlanError("no patients in the store")
    full_names, first_names = defaultdict(list), defaultdict(list)
    for mrn, name in known:
        full_names[name.lower()].append(mrn)
        first_names[name.split()[0].lower()].append(mrn)
    named = set()
    by_mrn = {mrn.upper(): mrn for mrn, _ in known}
    shapes = "|".join(sorted({re.sub(r"\d", r"\\d", re.escape(mrn)) for mrn in by_mrn}))
    for token in re.finditer(rf"(?<![\w-])(?:{shapes})(?![\w-])", question, re.IGNORECASE):
        if token.group().upper() not in by_mrn:
            raise PlanError(f"unknown patient: {token.group()}")
        named.add(by_mrn[token.group().upper()])

    def lookup(words: list[str]) -> str | None:
        text = " ".join(words).lower()
        mrns = full_names.get(text) or (first_names.get(text) if len(words) == 1 else None) or []
        if len(mrns) > 1:
            raise PlanError(f"ambiguous patient: {' '.join(words)} matches {len(mrns)} patients; use the full name or MRN")
        return mrns[0] if mrns else None

    for run in _NAME_RUN.finditer(question):
        words = [re.sub(r"['\u2019]s$", "", word) for word in run.group().split()]
        if _SENTENCE_START.search(question[: run.start()]) and not lookup(words):
            words = words[1:]  # the first word of a sentence is capitalized anyway
        if words:
            mrn = lookup(words)
            if mrn is None:
                raise PlanError(f"unknown patient: {' '.join(words)}")
            named.add(mrn)
    return [mrn for mrn, _ in known if mrn in named] or [mrn for mrn, _ in known]


# ---- The two readers ----

_ON_DATE = rf"\bon\s+(?:the\s+)?(?:[a-z]+day,?\s+)?(?:{_DATE})"
_RULES = {  # ported from the previous computed_answers.FAMILIES, plus the attendance, service type, and grouping words
    "metric": {
        "sessions": rf"\bsessions?\b|\battend(?!ance)|\breconstruct|{_ON_DATE}",  # the old day family lists sessions
        "minutes": r"\bminutes?\b|\bhours?\b",
        "goal_status": r"\bgoal\b|requirement|treatment.?plan(?! change)",
        "consecutive_shortfall": r"\bconsecutive\b|\bin a row\b|\binclusion\b",  # no longer every "patients" question
        "plan_change": r"plan change|plan-change|before and after|changed .* plan",
        "progress": r"symptom|progress|phq|assessment|improv|reason for",
        "billing_integrity": r"\bbill|\bcharge|\bclaim\b|\bdraft\b|\baudit",
        # Aggregations the renderers do not compute: a question that wants an average, a median, a ratio, or a
        # per-patient mean is an open question, so the rules propose "other" and a confident "minutes" from the
        # decision model becomes a visible dispute instead of a plain answer that silently omits the average.
        "other": r"\baverage\b|\bmean\b|\bmedian\b|\bratio\b|\bpercent(?:age)? of patients\b|\bper patient\b",
    },
    "attendance": {
        "attended": r"(?<!not )(?<!n't )(?<!n\u2019t )\battend(?:s|ed|ing)?\b",
        "missed_by_patient": r"\bmiss(?:es|ed)?\b|\bno[- ]?shows?\b|\b(?:did not|didn't|didn\u2019t) attend"
        r"|\bcancel\w* by the (?:patient|client)",
        "cancelled_by_clinic": r"\bcancel(?!\w* by the (?:patient|client))",
        "any": r"\breconstruct",
    },
    "service_type": {
        "individual_psychotherapy": r"\bindividual\b",
        "group_psychotherapy": r"\bgroups?\b",
        "family_psychotherapy": r"\bfamily\b",
    },
    "group_by": {
        "week": r"\b(?:by|each|per)\s+(?:[\w\u2013-]+\s+)?week\b|\bweekly\b",
        "day": r"\b(?:by|each|per)\s+(?:day|date)\b|\bdaily\b",
        "service_type": r"\b(?:by|each|per)\s+(?:service\s+)?type\b",
    },
}
_COMPILED_RULES = {
    field: {value: re.compile(pattern, re.IGNORECASE) for value, pattern in rules.items()} for field, rules in _RULES.items()
}

JEV_QUESTIONS = {
    "metric": Choice(
        "What does this question about patients' therapy records ask for?",
        {
            "sessions": "Counts or lists of sessions or contacts: attended, missed, or cancelled, of which types, "
            "or what happened on a date.",
            "minutes": "Therapy time: minutes or hours of patient-present therapy, in total, per week, or per day.",
            "days": "The number of distinct days with therapy.",
            "goal_status": "Whether therapy met the treatment plan's weekly requirement (goal, target, or standard), "
            "week by week, or what the plan requires.",
            "consecutive_shortfall": "Which patients had consecutive weeks below their requirement, or whose inclusion "
            "depends on unresolved documentation.",
            "plan_change": "How the type or amount of care changed before and after a treatment-plan change.",
            "progress": "Symptom course or progress: assessment scores such as the PHQ-9, improvement, or the reason "
            "for a contact.",
            "billing_integrity": "Charges for services that were not delivered, or services supported only by drafts or "
            "other unsupported records.",
            "other": "Anything else, for example who wrote a note, medications, authorizations, conflicting records, "
            "or a calculation none of the other values gives.",
        },
    ),
    "attendance": Choice(
        "Which sessions does the question count, by whether the patient took part?",
        {
            "attended": "Sessions the patient took part in: what the patient attended, received, or had.",
            "missed_by_patient": "Sessions the patient missed: no-shows, absences, or cancellations by the patient.",
            "cancelled_by_clinic": "Sessions the clinic or clinician cancelled.",
            "any": "Every scheduled or documented session whatever happened, for example everything on a date.",
            "not_applicable": "The question does not count sessions, minutes, or days.",
        },
    ),
    "service_type": Choice(
        "Which type of service does the question ask about?",
        {
            "individual_psychotherapy": "Individual therapy only.",
            "group_psychotherapy": "Group therapy only.",
            "family_psychotherapy": "Family therapy only.",
            "collateral_contact": "Sessions with relatives or other informants without the patient.",
            "medication_management": "Prescriber or medication visits only.",
            "any": "All types together, or the question names no type.",
        },
    ),
    "group_by": Choice(
        "How should the answer be broken down?",
        {
            "none": "One total; the question asks for no breakdown.",
            "week": "One row per Monday to Sunday week.",
            "day": "One row per date.",
            "service_type": "One row per type of service.",
            "plan_version": "One row per version of the treatment plan.",
        },
    ),
}


def rule_candidates(question: str) -> dict[str, set[str]]:
    """The values the keyword rules propose for each enum field. An empty set means the rules have no reading."""
    candidates = {
        field: {value for value, pattern in rules.items() if pattern.search(question)} for field, rules in _COMPILED_RULES.items()
    }
    if "plan_change" in candidates["metric"]:
        candidates["metric"].discard("goal_status")  # ported: a plan-change question does not want the weekly goal table
    return candidates


def choose(question: str, jev: Jev | None) -> dict[str, ChoiceAnswer]:
    """The decision model's reading of all four enum fields, asked in one request. No model gives no reading."""
    return jev.ask(question, JEV_QUESTIONS) if jev else {}


def _agreed(answer: ChoiceAnswer, candidates: set[str]) -> bool:
    return answer.choice in candidates or (not candidates and answer.confidence >= AGREEMENT_CONFIDENCE)


def plan_question(question: str, known_patients: list[tuple[str, str]], default_year: int, jev: Jev | None) -> Plan:
    """Parse patients and dates by code, then let the two readers settle the enum values; see the top of this section."""
    patients = parse_patients(question, known_patients)
    date_range = parse_dates(question, default_year)
    candidates = rule_candidates(question)
    answers = choose(question, jev)  # after the code checks, so a refused question costs nothing
    if answers:
        values = {field: answers[field].choice for field in ENUMS}
        warnings = [
            f"disputed {field}: rules say {' or '.join(sorted(candidates[field])) or 'nothing'}, "
            f"decision model says {answers[field].choice} ({answers[field].confidence:.2f})"
            for field in ENUMS
            if (field == "metric" or values["metric"] in FILTERED_METRICS) and not _agreed(answers[field], candidates[field])
        ]
    else:
        if len(candidates["metric"]) != 1:
            raise PlanError("cannot interpret the question")
        values = {"metric": next(iter(candidates["metric"]))}
        for field, default in RULES_ONLY_DEFAULT.items():
            proposed = [value for value in get_args(ENUMS[field]) if value in candidates[field]]
            values[field] = proposed[0] if proposed else default
        warnings = ["rules only, no decision model"]
    if "other" in candidates["metric"] and values["metric"] != "other":
        # The question asks for an aggregation no renderer computes (an average, a ratio). A rendered table would
        # silently omit it, so the open path wins over the decision model's metric, and the dispute stays visible.
        warnings.append(f"rules read an aggregation the renderers do not compute; routed as an open question instead of {values['metric']}")
        values["metric"] = "other"
    if values["metric"] not in FILTERED_METRICS:
        values |= NEUTRAL
    readings = {
        field: {
            "rules": sorted(candidates[field]),
            "decision_model": [answers[field].choice, answers[field].confidence] if answers else None,
        }
        for field in ENUMS
    }
    kind = "open" if values["metric"] == "other" else "computed"
    return Plan(kind, **values, patients=patients, date_range=date_range, readings=readings, warnings=warnings)


def interpretation(plan: Plan) -> str:
    """One line that says how the question was read, for example
    "Interpreted as: sessions, attended, HG-M042, 2026-01-12 to 2026-01-25, by week"."""
    parts = ["open question" if plan.kind == "open" else plan.metric]
    if plan.attendance != "not_applicable":
        parts.append("any attendance" if plan.attendance == "any" else plan.attendance)
    if plan.service_type != "any":
        parts.append(plan.service_type)
    parts.append(", ".join(plan.patients) if len(plan.patients) <= 3 else f"{len(plan.patients)} patients")
    parts.append(plan.date_range.label if plan.date_range else "all dates")
    if plan.group_by != "none":
        parts.append("by " + plan.group_by.replace("_", " "))
    return "Interpreted as: " + ", ".join(parts)


# 2. Answers by code ============================================================================================================
# A typed plan runs through one executor per metric that reads the derived tables and renders Markdown.
# Code computes every number, and every section cites the document lines behind it. The first line of an answer says how the
# question was read; the second names any document still pending for a patient in scope. The verdict says how far to trust it:
# COMPUTED when the two readers agreed and nothing is pending; COMPUTED WITH WARNING with the reasons when they disagreed, a
# patient in scope is incomplete, or a citation does not resolve; REFUSED, with no numbers, when the question cannot be planned;
# AGENT for open questions, with the agent's numbers checked against its tool outputs. The plan is saved with the answer, so
# verify_saved replays it without a model.

LISTED = 50  # patients shown one by one, in an answer or in the agent's prompt; the rest are counted (decision D1.8)
PENDING_LISTED = 10
MISSED = ("no_show", "patient_absent", "cancelled_by_patient")
STATUS_WORDS = {"met": "met", "not_met": "not met", "cannot_determine": "cannot be determined"}
# General PHQ-9 references, not patient facts.
PHQ9_BANDS = [(0, 4, "minimal"), (5, 9, "mild"), (10, 14, "moderate"), (15, 19, "moderately severe"), (20, 27, "severe")]
MEANINGFUL_CHANGE_POINTS = 5
SYMPTOM_AREAS = ("mood", "anxiety", "sleep", "avoidance", "functioning", "safety")
INTEGRITY_KINDS = (
    "charge_for_undelivered_service",
    "charge_without_encounter",
    "unsupported_record_for_undelivered_service",
    "keyword_contradicts_label",
    "correction_not_applied",
    "conflict_after_correction",
)
NOT_VERIFIED = "NOT VERIFIED: the saved answer differs from what the code renders now"
# ponytail: runs are two weeks long because a Plan has no number field; add one if a question asks for three weeks in a row.
COLLECTION_RUNS = """
WITH ordered AS (
    SELECT mrn, week_start, status,
           LAG(status) OVER (PARTITION BY mrn ORDER BY week_start) AS previous_status,
           LAG(week_start) OVER (PARTITION BY mrn ORDER BY week_start) AS previous_start
    FROM weeks
    WHERE mrn IN (SELECT value FROM json_each(?)) AND week_start <= ? AND week_end >= ?
)
SELECT mrn, previous_start, week_start, status = 'not_met' AND previous_status = 'not_met' AS established
FROM ordered
WHERE status IN ('not_met', 'cannot_determine') AND previous_status IN ('not_met', 'cannot_determine')
ORDER BY mrn, week_start
"""


# ---- Small helpers ----


def _hours(minutes: int) -> str:
    return f"{minutes / 60:.2f}".rstrip("0").rstrip(".")


def _cites(citations: list[str]) -> str:
    return f"[{', '.join(citations)}]" if citations else ""


def _bounds(window: DateRange | None) -> tuple[str, str]:
    start = window.start.isoformat() if window and window.start else "0000-01-01"
    return start, window.end.isoformat() if window and window.end else "9999-12-31"


def _listed(items: list[str]) -> str:
    more = f", and {len(items) - LISTED} more" if len(items) > LISTED else ""
    return (", ".join(items[:LISTED]) + more) or "none"


def _flag_line(flag) -> str:
    settles = f" {flag['settles']}" if flag["settles"] else ""
    return f"- {flag['encounter'] or 'patient'} ({flag['kind']}): {flag['text']} {_cites(json.loads(flag['citations']))}{settles}"


# ---- Sessions, minutes, and days ----


# Contacts the patient is not expected to attend in person: a partner-only collateral call or a care-coordination call is
# not a session the patient "missed", even when the record marks the patient absent.
NOT_PATIENT_APPOINTMENTS = {"collateral_contact", "care_coordination", "outreach_or_scheduling_call", "administrative", "assessment_review"}


def _matches(row: dict, attendance: str) -> bool:
    if attendance == "attended":
        return row["counts_as_therapy"] != "no"
    if attendance == "missed_by_patient":
        return row["status"] in MISSED and row["service_type"] not in NOT_PATIENT_APPOINTMENTS
    if attendance == "cancelled_by_clinic":
        return row["status"] == "cancelled_by_clinic"
    return True


def _headline(plan: Plan, rows: list[dict]) -> str:
    totals = reconcile.summarize(rows)
    low, high = totals["minutes_low"], totals["minutes_high"]
    minutes = f"{range_text(low, high)} minutes ({range_text(_hours(low), _hours(high))} hours)"
    sessions = count_text(totals["sessions_low"], totals["sessions_high"], "therapy session")
    if plan.metric == "minutes":
        return f"**{minutes}** of patient-present therapy"
    if plan.metric == "days":
        days = count_text(totals["therapy_days_low"], totals["therapy_days_high"], "distinct therapy day")
        return f"**{days}** ({sessions}, {minutes})"
    if plan.attendance == "attended":
        by_type = ", ".join(
            f"{range_text(row['sessions_low'], row['sessions_high'])} {kind.replace('_psychotherapy', '')} "
            f"({range_text(row['minutes_low'], row['minutes_high'])} min)"
            for kind, row in totals["by_service_type"].items()
        )
        days = count_text(totals["therapy_days_low"], totals["therapy_days_high"], "distinct day")
        return f"**{sessions} on {days}**" + (f" ({by_type})" if by_type else "")
    if plan.attendance == "missed_by_patient":
        return f"**{count_text(len(rows), len(rows), 'missed session')}** (no-show, absent, or cancelled by the patient)"
    if plan.attendance == "cancelled_by_clinic":
        return f"**{count_text(len(rows), len(rows), 'session')} cancelled by the clinic**"
    return f"**{count_text(len(rows), len(rows), 'session record')}**, of which {sessions} count toward the plan, with {minutes}"


def _groups(connection, plan: Plan, mrn: str, rows: list[dict]) -> list[tuple[str, list[dict]]]:
    if plan.group_by in ("day", "service_type"):
        key = "service_date" if plan.group_by == "day" else "service_type"
        return [(value, [row for row in rows if row[key] == value]) for value in dict.fromkeys(row[key] for row in rows)]
    # Weeks and plan versions come from the weeks table, so they follow the plan's own week definition.
    start, end = _bounds(plan.date_range)
    if plan.group_by == "week":
        query = "SELECT week_start, week_end FROM weeks WHERE mrn = ? AND week_end >= ? AND week_start <= ? ORDER BY week_start"
        spans = [(f"{first} to {last}", first, last) for first, last in connection.execute(query, (mrn, start, end))]
    else:
        query = (
            "SELECT plan_version, MIN(week_start), MAX(week_end) FROM weeks WHERE mrn = ? AND plan_version IS NOT NULL "
            "GROUP BY plan_version ORDER BY plan_version"
        )
        spans = [
            (f"version {version} ({first} to {last})", first, last) for version, first, last in connection.execute(query, (mrn,))
        ]
    groups = [(label, [row for row in rows if first <= row["service_date"] <= last]) for label, first, last in spans]
    outside = [row for row in rows if not any(first <= row["service_date"] <= last for _, first, last in spans)]
    return groups + ([("outside the plan's weeks", outside)] if outside else [])


def _group_row(label: str, rows: list[dict]) -> list:
    totals = reconcile.summarize(rows)
    low, high = totals["minutes_low"], totals["minutes_high"]
    return [
        label,
        len(rows),
        range_text(totals["sessions_low"], totals["sessions_high"]),
        range_text(totals["therapy_days_low"], totals["therapy_days_high"]),
        range_text(low, high),
        range_text(_hours(low), _hours(high)),
    ]


def _filtered(connection, plan: Plan, mrn: str) -> str:
    """Sessions, minutes, or days for one patient, filtered by date range, service type, and attendance, then grouped.

    ponytail: an encounter whose records disagree on the date sits on its listed date here, and its date_conflict flag prints
    below; the goal table bounds both placements. Read service_dates from the abstraction if a filtered total must too.
    """
    rows = [
        row
        for row in encounters(connection, mrn, *_bounds(plan.date_range))
        if plan.service_type in ("any", row["service_type"])
    ]
    matching = [row for row in rows if _matches(row, plan.attendance)]
    scope = (plan.date_range.label if plan.date_range else "all dates") + (
        f", {plan.service_type} only" if plan.service_type != "any" else ""
    )
    parts = [f"{scope[0].upper()}{scope[1:]}: {_headline(plan, matching)}."]
    if plan.group_by != "none":
        heading = {"week": "Week", "day": "Date", "service_type": "Service type", "plan_version": "Plan version"}[plan.group_by]
        groups = _groups(connection, plan, mrn, matching)
        headers = [heading, "Records", "Therapy sessions", "Therapy days", "Minutes", "Hours"]
        parts.append("\n".join(table_lines(headers, [_group_row(label, group) for label, group in groups])).strip())
    # A sessions answer also lists what did not count, and why, so a reviewer sees every ineligible record.
    listed = rows if plan.metric == "sessions" and plan.attendance == "attended" else matching
    if not listed:
        return "\n\n".join(parts)
    calculations = {row["encounter"]: row.get("calculation") for row in store.stored_abstraction(connection, mrn, "$.encounters") or []}
    table = [
        [
            row["service_date"],
            row["encounter"],
            f"{row['service_type']} ({row['modality']})",
            row["status"],
            row["counts_as_therapy"],
            range_text(row["minutes_low"], row["minutes_high"]),
            calculations.get(row["encounter"]),
            ", ".join(row["sources"]),
        ]
        for row in listed
    ]
    headers = ["Date", "Encounter", "Service", "Status", "Counted", "Minutes", "Calculation or reason", "Sources"]
    parts.append("\n".join(table_lines(headers, table)).strip())
    counted = {row["encounter"]: row["counts_as_therapy"] for row in listed}
    flags = [
        _flag_line(flag)
        for flag in connection.execute("SELECT * FROM flags WHERE mrn = ? ORDER BY encounter, kind, text", (mrn,))
        if flag["encounter"] in counted and (flag["kind"] != "label_disputed" or counted[flag["encounter"]] == "disputed")
    ]
    parts.append("Flags on these records:\n\n" + ("\n".join(flags) or "- none"))
    return "\n\n".join(parts)


# ---- Goal, collection, plan change, progress, integrity ----


def _goal_text(plan_row: dict, short: bool = False) -> str:
    days, minutes = plan_row["min_days_per_week"], plan_row["min_minutes_per_week"]
    if short:
        return f"{'-' if days is None else count_text(days, days, 'day')}, {'-' if minutes is None else minutes} min"
    parts = [count_text(days, days, "therapy day")] if days is not None else []
    parts += [f"{minutes} patient-present minutes"] if minutes is not None else []
    return " and ".join(parts) or "no measurable amount"


def _goal(connection, plan: Plan, mrn: str) -> str:
    plans = store.stored_abstraction(connection, mrn, "$.plans") or []
    lines = [
        f"Plan version {row['version']} ({row['effective_date'] or 'start not stated'} to {row['effective_end'] or 'open'}): "
        f"at least {_goal_text(row)} per {reconcile.DAYS[row['week_start_day']].capitalize()}-"
        f"{reconcile.DAYS[(row['week_start_day'] + 6) % 7].capitalize()} week; counts "
        f"{', '.join(row['counted_service_types'])} {_cites(row['sources'])}."
        for row in plans
    ] or ["No measurable treatment-plan goal is documented."]
    start, end = _bounds(plan.date_range)
    numbered = [
        (number, dict(week))
        for number, week in enumerate(
            connection.execute("SELECT * FROM weeks WHERE mrn = ? ORDER BY week_start", (mrn,)), start=1
        )
        if week["week_end"] >= start and week["week_start"] <= end
    ]
    goals = {row["version"]: _goal_text(row, short=True) for row in plans}
    table = [
        [
            number,
            f"{week['week_start']} to {week['week_end']}",
            range_text(week["therapy_days_low"], week["therapy_days_high"]),
            range_text(week["minutes_low"], week["minutes_high"]),
            goals.get(week["plan_version"], "none documented"),
            f"**{STATUS_WORDS[week['status']]}**",
        ]
        for number, week in numbered
    ]
    lines += ["", "\n".join(table_lines(["Week", "Dates", "Therapy days", "Minutes", "Goal", "Status"], table)).strip(), ""]
    lines += [f"- Week {number}: {week['explanation'].rstrip('.')}." for number, week in numbered]
    runs = reconcile.consecutive_shortfalls([week for _, week in numbered])
    established, depending = runs_text(runs["established"]), runs_text(runs["depends_on_unresolved_records"])
    lines += ["", f"Consecutive weeks below goal: established {established}; depending on unresolved records {depending}."]
    return "\n".join(lines)


def _collection(connection, plan: Plan) -> str:
    """Runs of two weeks below goal across every patient in scope: one SQL window over the weeks table, then counts and the
    first patients, never every patient's table."""
    start, end = _bounds(plan.date_range)
    established, possible = {}, {}
    for mrn, first, second, settled in connection.execute(COLLECTION_RUNS, (json.dumps(plan.patients), end, start)):
        (established if settled else possible).setdefault(mrn, []).append(f"{first} and {second}")
    depending = sorted(mrn for mrn in possible if mrn not in established)
    reviewed = len(plan.patients)
    counts = (
        f"{count_text(reviewed, reviewed, 'patient')} reviewed: {len(established)} with 2 consecutive weeks below goal "
        f"established by the record, {len(depending)} whose inclusion depends on unresolved records, "
        f"{reviewed - len(established) - len(depending)} not included."
    )
    lines = [
        counts,
        "",
        f"Established ({len(established)}): {_listed(sorted(established))}.",
        "",
        f"Depending on unresolved records ({len(depending)}):",
    ]
    shown = depending[:LISTED]
    open_weeks = connection.execute(
        "SELECT mrn, week_start, explanation FROM weeks WHERE status = 'cannot_determine' "
        "AND mrn IN (SELECT value FROM json_each(?)) AND week_start <= ? AND week_end >= ? ORDER BY mrn, week_start",
        (json.dumps(shown), end, start),
    ).fetchall()
    for mrn in shown:
        lines.append(f"- {mrn}: weeks of {'; '.join(possible[mrn])}.")
        lines += [f"  - week of {week['week_start']}: {week['explanation']}" for week in open_weeks if week["mrn"] == mrn]
    if len(depending) > LISTED:
        lines.append(f"- and {len(depending) - LISTED} more.")
    return "\n".join(lines if depending else lines[:-1] + ["Depending on unresolved records: none."])


def _plan_change(connection, plan: Plan, mrn: str) -> str:
    compared = store.stored_abstraction(connection, mrn, "$.plan_versions_compared") or {"versions": [], "note": "No treatment plan is stored."}
    rows = [
        [
            f"{row['plan_version']} {_cites(row['sources'])}",
            f"{row['start']} to {row['end']} ({row['weeks']} weeks)",
            f"{row['goal_days']} days, {row['goal_minutes']} min",
            range_text(row["sessions_low"], row["sessions_high"]),
            range_text(row["minutes_low"], row["minutes_high"]),
            range_text(row["minutes_per_week_low"], row["minutes_per_week_high"]),
            ", ".join(
                f"{kind}: {count_text(item['sessions_low'], item['sessions_high'], 'session')}, "
                f"{range_text(item['minutes_low'], item['minutes_high'])} min"
                for kind, item in row["by_service_type"].items()
            ),
        ]
        for row in compared["versions"]
    ]
    headers = ["Plan version", "Dates", "Goal", "Sessions", "Minutes", "Minutes per week", "By service type"]
    parts = ["\n".join(table_lines(headers, rows)).strip()] if rows else []
    return "\n\n".join(parts + ([compared["note"]] if compared["note"] else []))


def _band(row: dict) -> str | None:
    if not row["instrument"].upper().replace(" ", "").startswith("PHQ-9") or row["total_score"] is None:
        return None
    return next(label for low, high, label in PHQ9_BANDS if low <= row["total_score"] <= high)


def _phq9_conclusions(phq: list[dict]) -> tuple[list[str], list[str]]:
    """What the first and last PHQ-9 totals can and cannot support."""
    supported, unsupported = [], ["that therapy caused the change: the record has no comparison condition"]
    if len(phq) < 2:
        return supported, unsupported + [
            f"a measured change: {len(phq)} PHQ-9 total{'' if len(phq) == 1 else 's'} recorded, two are needed"
        ]
    first, last = phq[0], phq[-1]
    change = last["total_score"] - first["total_score"]
    percent = round(100 * abs(change) / first["total_score"], 1) if first["total_score"] else 0.0
    reduction = percent if change < 0 else 0.0
    supported.append(
        f"PHQ-9 total {'fell' if change < 0 else 'rose' if change else 'stayed the same'} {abs(change)} points, from "
        f"{first['total_score']} ({_band(first)}) on {first['completed_on']} to {last['total_score']} ({_band(last)}) on "
        f"{last['completed_on']}"
        + (f", a {percent} percent {'reduction' if change < 0 else 'increase'}" if change else "")
        + f" {_cites(first['sources'] + last['sources'])}"
    )
    threshold = f"{MEANINGFUL_CHANGE_POINTS}-point threshold"
    if abs(change) >= MEANINGFUL_CHANGE_POINTS:
        supported.append(f"the change is at least the {threshold} commonly used for a meaningful change")
    else:
        unsupported.append(f"a meaningful change: {abs(change)} points is below the {threshold}")
    if reduction >= 50:
        supported.append(f"a treatment response: a {reduction} percent reduction, at least the 50 percent needed")
    else:
        unsupported.append(f"treatment response: needs a 50 percent reduction, observed {reduction} percent")
    if last["total_score"] < 5:
        supported.append(f"remission: the last total, {last['total_score']}, is below 5")
    else:
        unsupported.append(f"remission: needs a total below 5, last total {last['total_score']}")
    return supported, unsupported


def _progress(connection, plan: Plan, mrn: str) -> str:
    """Distinct administrations, the PHQ-9 change, and the symptom course. A single-day range names the contact whose reason is
    asked (development question DEV-05), so it selects only the reason-for-contact section and never cuts the course."""
    one_day = plan.date_range is not None and plan.date_range.start == plan.date_range.end
    course = None if one_day else plan.date_range
    first_day, last_day = _bounds(course)

    def in_course(day: str | None) -> bool:
        return course is None or (day is not None and first_day <= day <= last_day)

    administrations = [
        dict(row) | {"sources": json.loads(row["sources"]), "copies": json.loads(row["copies"])}
        for row in connection.execute("SELECT * FROM assessments WHERE mrn = ? ORDER BY completed_on, instrument", (mrn,))
        if in_course(row["completed_on"])
    ]
    table = [
        [
            row["completed_on"],
            row["instrument"],
            row["total_score"],
            _band(row),
            _cites(row["sources"]),
            f"repeated in {_cites(row['copies'])}, not a new administration" if row["copies"] else None,
        ]
        for row in administrations
    ]
    supported, unsupported = _phq9_conclusions([row for row in administrations if _band(row)])
    if len({row["instrument"] for row in administrations}) <= 1:
        unsupported.append("a measured change in anxiety or sleep: no instrument for them is recorded, only narrative notes")
    lines = [f"{len(administrations)} distinct administrations; repeats and imports are merged into the original.", ""]
    lines += ["\n".join(table_lines(["Completed", "Instrument", "Total", "Band", "Source", "Copies"], table)).strip()]
    lines += ["", "Can be supported:", ""] + ([f"- {item}." for item in supported] or ["- nothing measured."])
    lines += ["", "Cannot be supported:", ""] + [f"- {item}." for item in unsupported]
    observations = [
        dict(row) | {"sources": json.loads(row["sources"])}
        for row in connection.execute("SELECT * FROM observations WHERE mrn = ? ORDER BY observed_on, category, text", (mrn,))
    ]
    lines += ["", "Course by symptom area, first and last statement in the record (verbatim):", ""]
    course_lines = []
    for area in SYMPTOM_AREAS:
        items = [row for row in observations if row["category"] == area and in_course(row["observed_on"])]
        if items:
            first, last = items[0], items[-1]
            course_lines.append(
                f'- {area}: {first["observed_on"]} "{first["text"]}" {_cites(first["sources"])}'
                + (f' ... {last["observed_on"]} "{last["text"]}" {_cites(last["sources"])}' if last is not first else "")
            )
    lines += course_lines or ["- none recorded."]
    start, end = _bounds(plan.date_range)
    reasons = [
        row
        for row in observations
        if row["category"] == "reason_for_contact"
        and (plan.date_range is None or (row["observed_on"] is not None and start <= row["observed_on"] <= end))
    ]
    if reasons:
        lines += ["", f"Reason for contact{' on ' + plan.date_range.label if plan.date_range else ''} (verbatim):", ""]
        lines += [f'- {row["observed_on"]}: "{row["text"]}" {_cites(row["sources"])}' for row in reasons[:8]]
    return "\n".join(lines)


def _integrity(connection, plan: Plan, mrn: str) -> str:
    start, end = _bounds(plan.date_range)
    flags = connection.execute(
        "SELECT f.* FROM flags f LEFT JOIN encounters e ON e.mrn = f.mrn AND e.encounter = f.encounter "
        "WHERE f.mrn = ? AND f.kind IN (SELECT value FROM json_each(?)) "
        "AND (e.service_date IS NULL OR e.service_date BETWEEN ? AND ?) ORDER BY f.encounter, f.kind, f.text",
        (mrn, json.dumps(INTEGRITY_KINDS), start, end),
    ).fetchall()
    if not flags:
        return "No billing or documentation-integrity findings."
    count = count_text(len(flags), len(flags), "billing or documentation-integrity finding")
    return f"{count}:\n\n" + "\n".join(_flag_line(flag) for flag in flags)


EXECUTORS = {
    "sessions": ("Sessions", _filtered),
    "minutes": ("Minutes and hours", _filtered),
    "days": ("Therapy days", _filtered),
    "goal_status": ("Treatment-plan goal by week", _goal),
    "plan_change": ("Care before and after plan changes", _plan_change),
    "progress": ("Symptom course and progress", _progress),
    "billing_integrity": ("Billing and documentation integrity", _integrity),
}


def run(connection, plan: Plan) -> list[tuple[str, str]]:
    """The answer's sections as (heading, Markdown) pairs, all computed from the derived tables. No model call."""
    if plan.metric == "consecutive_shortfall":
        return [("Across the collection", _collection(connection, plan))]
    title, executor = EXECUTORS[plan.metric]
    shown = plan.patients[:LISTED]
    query = "SELECT mrn, name FROM patients WHERE mrn IN (SELECT value FROM json_each(?))"
    names = {mrn: name for mrn, name in connection.execute(query, (json.dumps(shown),))}
    sections = [(f"{title}, {names.get(mrn) or 'name not stated'} ({mrn})", executor(connection, plan, mrn)) for mrn in shown]
    if len(plan.patients) > LISTED:
        more = (
            f"{len(plan.patients) - LISTED} more patients are in scope and not shown; name a patient or MRN to see their answer."
        )
        sections.append(("More patients", more))
    return sections


# ---- Verdicts, saving, and replay ----


def _header(connection, plan: Plan) -> tuple[list[str], list[str]]:
    """The interpretation line, the incomplete line when a patient in scope has documents pending, and the warnings."""
    lines, warnings = [interpretation(plan)], list(plan.warnings)
    rows = connection.execute(
        "SELECT mrn, incomplete FROM patients WHERE incomplete != '[]' AND mrn IN (SELECT value FROM json_each(?)) ORDER BY mrn",
        (json.dumps(plan.patients),),
    )
    pending = [(mrn, item) for mrn, incomplete in rows for item in json.loads(incomplete)]
    if pending:
        count = f"{count_text(len(pending), len(pending), 'document')} pending"
        named = "; ".join(f"{item['doc_id']} for {mrn}: {item['reason']}" for mrn, item in pending[:PENDING_LISTED])
        more = f"; and {len(pending) - PENDING_LISTED} more" if len(pending) > PENDING_LISTED else ""
        lines.append(f"Incomplete: {count} ({named}{more})")
        warnings.append(f"incomplete: {count}")
    return lines, warnings


def _render(connection, plan: Plan) -> tuple[str, list[str], list[str]]:
    """The full answer text for a computed plan, its warnings, and its broken citations. answer and verify_saved share it."""
    lines, warnings = _header(connection, plan)
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in run(connection, plan))
    text = "\n".join(lines) + "\n\n" + body + "\n"
    broken = broken_citations(connection, text)
    if broken:
        warnings.append(
            f"{count_text(len(broken), len(broken), 'citation')} not found in the stored documents ({', '.join(broken[:10])})"
        )
    return text, warnings, broken


def _verdict(warnings: list[str]) -> str:
    return "COMPUTED WITH WARNING: " + "; ".join(warnings) if warnings else "COMPUTED"


def _plan_json(plan: Plan) -> dict:
    data, window = asdict(plan), plan.date_range
    data["date_range"] = window and {
        "start": window.start and window.start.isoformat(),
        "end": window.end and window.end.isoformat(),
        "label": window.label,
    }
    return data


def _plan_from_json(data: dict) -> Plan:
    window = data["date_range"]
    if window:
        window = DateRange(
            *(date.fromisoformat(window[key]) if window[key] else None for key in ("start", "end")), window["label"]
        )
    return Plan(**{**data, "date_range": window})


def _spent(budget: read.Budget) -> float:
    return sum(budget.spent(account) for account in budget.caps)


def answer(connection, question: str, jev: Jev | None, budget: read.Budget, model: str, force_agent: bool = False) -> dict:
    """Plan the question, answer it by code (or by the agent for open questions), save it with its plan, and return it."""
    started, spent_before = time.perf_counter(), _spent(budget)
    result = {"question": question, "plan": None, "interpretation": None, "warnings": [], "citations_broken": []}
    try:
        plan = plan_question(question, store.known_patients(connection), store.episode_year(connection), jev)
    except PlanError as error:
        result |= {"answer": f"Not answered: {error}.", "verdict": f"REFUSED: {error}"}
    else:
        result |= {"plan": _plan_json(plan), "interpretation": interpretation(plan)}
        if plan.kind == "open" or force_agent:
            lines, warnings = _header(connection, plan)
            try:
                reply = answer_open(connection, question, model, budget, patients=plan.patients)
            except read.BudgetExceeded:
                raise
            except Exception as error:  # noqa: BLE001 - one failed agent call must not lose a batch of answers
                reply = {"answer": "", "verdict": f"AGENT: not answered ({type(error).__name__}: {error})"[:300]}
            text = "\n".join(lines) + "\n\n" + reply["answer"] + "\n"
            verdict = reply["verdict"] + (f"; WARNING: {'; '.join(warnings)}" if warnings else "")
            result |= {"answer": text, "verdict": verdict, "warnings": warnings}
            result["citations_broken"] = broken_citations(connection, text)
        else:
            text, warnings, broken = _render(connection, plan)
            result |= {"answer": text, "verdict": _verdict(warnings), "warnings": warnings, "citations_broken": broken}
    result["cost_usd"] = round(_spent(budget) - spent_before, 6)
    result["latency_seconds"] = round(time.perf_counter() - started, 3)
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO answers (question_key, question, plan, answer, verdict, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                " ".join(question.lower().split()),
                question,
                json.dumps(result["plan"]),
                result["answer"],
                result["verdict"],
                store.now(),
            ),
        )
    return result


def verify_saved(connection, path: Path) -> list[dict]:
    """Replay every saved computed answer through run, with no model, and compare the text; write the verdicts to path.

    The saved answers come from path when it exists (the answer-questions file), else from the answers table. A refusal or
    an agent answer has no plan to replay, so its verdict stands.
    """
    path = Path(path)
    if path.exists():
        saved = json.loads(path.read_text())
    else:
        query = "SELECT question, plan, answer, verdict FROM answers ORDER BY created_at, question_key"
        saved = [dict(row) | {"plan": json.loads(row["plan"])} for row in connection.execute(query)]
    for item in saved:
        if item["plan"] is None or item["verdict"].startswith("AGENT"):
            continue
        text, warnings, _ = _render(connection, _plan_from_json(item["plan"]))
        item["verdict"] = _verdict(warnings) if text == item["answer"] else NOT_VERIFIED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=1))
    return saved


# 3. The open-question agent ====================================================================================================
# A question whose plan has kind "open" has no executor, so Claude answers it through four read-only tools: search_source,
# source_lines, encounters, and weeks. Code then checks the answer: every quantity must appear in some tool output, and every
# citation must point at lines of a stored document. The verdict names what fails.

MAX_MODEL_CALLS = 8
CITATION = re.compile(r"(?<![\w~.-])([A-Za-z][\w.~-]*\d[\w.~-]*)\s+L(\d+)(?:-(\d+))?(?![\w-])")

SYSTEM = """You answer a clinical reviewer's question about patients' therapy records. Read the records only through the tools.
1. Copy every number from a tool result. Never compute, count, or convert a number yourself: code flags every number in your
   answer that no tool result contains.
2. Cite the lines behind each statement in square brackets as the tools give them, for example [BH-D103 L7].
3. Say what the records do not settle, and which document would settle it. If the tools cannot answer the question, say so.
4. Start with a short direct answer, then the supporting detail. No preamble.
"""

_MRN_ARGUMENT = {"mrn": {"type": "string", "description": "Patient MRN, for example HG-M042"}}
_DATE_ARGUMENT = {"type": "string", "description": "YYYY-MM-DD, optional"}
TOOLS = [
    {
        "name": "search_source",
        "description": "Keyword search over one patient's source documents. Returns up to 20 matching lines, best first, with citations.",
        "input_schema": {
            "type": "object",
            "properties": {**_MRN_ARGUMENT, "query": {"type": "string", "description": "A few keywords"}},
            "required": ["mrn", "query"],
        },
    },
    {
        "name": "source_lines",
        "description": "The exact lines behind a citation such as 'BH-D103 L7' or 'BH-D103 L7-9'.",
        "input_schema": {"type": "object", "properties": {"citation": {"type": "string"}}, "required": ["citation"]},
    },
    {
        "name": "encounters",
        "description": "One row per real contact after merging every document that describes it: date, service type, status, "
        "whether it counts toward the treatment-plan goal (yes, no, or disputed), patient-present minutes as low and high "
        "bounds, and the citations of its sources.",
        "input_schema": {
            "type": "object",
            "properties": {**_MRN_ARGUMENT, "start_date": _DATE_ARGUMENT, "end_date": _DATE_ARGUMENT},
            "required": ["mrn"],
        },
    },
    {
        "name": "weeks",
        "description": "One row per treatment-plan week: therapy days and minutes as low and high bounds, the goal status "
        "(met, not_met, or cannot_determine), and why, including which document would settle an open week.",
        "input_schema": {"type": "object", "properties": _MRN_ARGUMENT, "required": ["mrn"]},
    },
]


def search_source(connection, mrn: str, query: str, limit: int = 20) -> list[dict]:
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9-]+", query) if len(term) > 2}
    hits = []
    for doc_id, text in connection.execute("SELECT doc_id, text FROM documents WHERE patient_id = ? ORDER BY doc_id", (mrn,)):
        for number, line in enumerate(text.splitlines(), start=1):
            matched = sum(term in line.lower() for term in terms)
            if matched:
                hits.append((-matched, doc_id, number, line.strip()))
    return [{"citation": f"{doc_id} L{number}", "line": line} for _, doc_id, number, line in sorted(hits)[:limit]]


def source_lines(connection, citation: str) -> list[dict] | dict:
    found = CITATION.fullmatch(citation.strip())
    if not found:
        return {"error": "give a citation such as 'BH-D103 L7' or 'BH-D103 L7-9'"}
    doc_id, first, last = found[1], int(found[2]), int(found[3] or found[2])
    lines = store.document_lines(connection, doc_id)
    if not 1 <= first <= last <= len(lines):
        return {"error": f"{citation} does not point at lines of a stored document"}
    return [{"citation": f"{doc_id} L{number}", "line": lines[number - 1]} for number in range(first, last + 1)]


def encounters(connection, mrn: str, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    rows = connection.execute(
        "SELECT encounter, service_date, service_type, modality, status, counts_as_therapy, minutes_low, minutes_high, sources "
        "FROM encounters WHERE mrn = ? AND service_date BETWEEN ? AND ? ORDER BY service_date, encounter",
        (mrn, start_date or "0000-01-01", end_date or "9999-12-31"),
    )
    return [dict(row) | {"sources": json.loads(row["sources"])} for row in rows]


def weeks(connection, mrn: str) -> list[dict]:
    rows = connection.execute(
        "SELECT week_start, week_end, therapy_days_low, therapy_days_high, minutes_low, minutes_high, status, explanation, "
        "plan_version FROM weeks WHERE mrn = ? ORDER BY week_start",
        (mrn,),
    )
    return [dict(row) for row in rows]


TOOL_FUNCTIONS = {"search_source": search_source, "source_lines": source_lines, "encounters": encounters, "weeks": weeks}


def call_tool(connection, name: str, arguments: dict) -> str:
    """Run one tool for the model and return its output as JSON. A bad name, patient, or argument comes back as an error."""
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        result = {"error": f"no tool named {name}"}
    elif "mrn" in arguments and not connection.execute("SELECT 1 FROM patients WHERE mrn = ?", (arguments["mrn"],)).fetchone():
        result = {"error": f"unknown patient {arguments['mrn']}"}
    else:
        try:
            result = function(connection, **arguments)
        except TypeError as error:  # the model passed an argument the tool does not take
            result = {"error": str(error)}
    return json.dumps(result)


def broken_citations(connection, text: str) -> list[str]:
    """Citations in a text that do not point at lines of a stored document."""
    sizes: dict[str, int] = {}
    broken = []
    for found in CITATION.finditer(text):
        doc_id, first, last = found[1], int(found[2]), int(found[3] or found[2])
        if doc_id not in sizes:
            sizes[doc_id] = len(store.document_lines(connection, doc_id))
        if not 1 <= first <= last <= sizes[doc_id]:
            broken.append(found.group())
    return list(dict.fromkeys(broken))


_MONTH_WORD = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_NOT_QUANTITIES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"  # dates and timestamps: 2026-01-19, 2026-01-19T10:00
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"  # 1/19, 01/19/2026
    r"|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?"  # times: 09:00, 9:00 AM
    rf"|\b{_MONTH_WORD}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"  # January 19, 2026
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_WORD}(?:,?\s+\d{{4}})?"  # 19 January 2026
    rf"|\b{_MONTH_WORD}\s+\d{{4}}"  # January 2026
    r"|\b[A-Za-z]+-?[A-Za-z]*\d[\w~-]*"  # record ids and citations: HG-E110, BH-D103 L7-9, E4, PHQ-9
)
_QUANTITY = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w)")


def quantities(text: str) -> list[str]:
    """The distinct numbers a text states, leaving out dates, times, citations, and record ids."""
    plain = _NOT_QUANTITIES.sub(" ", re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text))  # 2,000 reads as 2000
    return list(dict.fromkeys(_QUANTITY.findall(plain)))


def unbound_numbers(answer_text: str, tool_outputs: list[str]) -> list[str]:
    """Quantities in an answer that appear in no tool output, so the model computed or invented them."""
    bound = {float(number) for output in tool_outputs for number in quantities(output)}
    return [number for number in quantities(answer_text) if float(number) not in bound]


def answer_open(connection, question: str, model: str, budget: read.Budget, call=None, patients: list[str] | None = None) -> dict:
    """Let Claude answer an open question through the tools, at most MAX_MODEL_CALLS calls, then check its numbers and citations."""
    call = call or read.call_anthropic  # looked up now, so a test's replacement applies
    started = time.perf_counter()
    names = dict(store.known_patients(connection))
    scope = list(names) if patients is None else patients
    listed = [f"- {mrn} ({names.get(mrn) or 'name not stated'})" for mrn in scope[:LISTED]]
    listed += [f"- and {len(scope) - LISTED} more"] if len(scope) > LISTED else []
    system = SYSTEM + "\nPatients in scope (MRN and name):\n" + "\n".join(listed) + "\n"
    messages: list[dict] = [{"role": "user", "content": question}]
    outputs, tool_calls, cost, text = [], [], 0.0, None
    for calls in range(1, MAX_MODEL_CALLS + 1):
        response = call(model, system, messages, TOOLS)
        charge = read.usage_cost(model, response["usage"])
        cost += charge
        budget.charge("claude", charge, f"agent {model}")
        messages.append({"role": "assistant", "content": response["content"]})
        uses = [block for block in response["content"] if block["type"] == "tool_use"]
        if not uses:
            text = "".join(block["text"] for block in response["content"] if block["type"] == "text").strip()
            break
        results = []
        for use in uses:
            output = call_tool(connection, use["name"], use["input"])
            outputs.append(output)
            tool_calls.append({"name": use["name"], "input": use["input"]})
            results.append({"type": "tool_result", "tool_use_id": use["id"], "content": output})
        messages.append({"role": "user", "content": results})
    unbound = unbound_numbers(text, outputs) if text else []
    broken = broken_citations(connection, text) if text else []
    problems = [f"unbound numbers {', '.join(unbound)}"] if unbound else []
    problems += [f"broken citations {', '.join(broken)}"] if broken else []
    if not text:  # still asking for tools at the cap, or an empty reply
        verdict = f"AGENT: no final answer after {calls} model calls"
    elif problems:
        verdict = "AGENT: " + "; ".join(problems)
    else:
        verdict = f"AGENT: all {len(quantities(text))} numbers bound"
    return {
        "answer": text or "",
        "verdict": verdict,
        "unbound_numbers": unbound,
        "tool_calls": tool_calls,
        "model_calls": calls,
        "cost_usd": round(cost, 6),
        "latency_seconds": round(time.perf_counter() - started, 3),
    }
