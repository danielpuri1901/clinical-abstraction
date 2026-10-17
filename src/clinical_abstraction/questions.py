"""Questions become typed plans: code reads patients and dates, two independent readers pick the enum values.

Code parses what code is good at: patients (MRN or name, matched against the store) and dates. An unknown patient or an
impossible date raises PlanError, so no numbers are printed. The enum values (metric, attendance, service type, grouping)
get two readings: keyword rules propose candidates and the decision model (Jev) chooses. A field is agreed when Jev's
choice is among the rule candidates, or when the rules propose nothing and Jev is at least 0.9 sure. Anything else
becomes a warning that prints both readings, so a misread surfaces instead of hiding behind a plain COMPUTED verdict.
"""

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, get_args

from clinical_abstraction.jev import Choice, ChoiceAnswer, Jev
from clinical_abstraction.reconcile import normalize_date

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


def plan(question: str, known_patients: list[tuple[str, str]], default_year: int, jev: Jev | None) -> Plan:
    """Parse patients and dates by code, then let the two readers settle the enum values; see the module docstring."""
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
