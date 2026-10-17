"""Answers: a typed plan runs through one executor per metric that reads the derived tables and renders Markdown.

Code computes every number, and every section cites the document lines behind it. The first line of an answer says how the
question was read; the second names any document still pending for a patient in scope. The verdict says how far to trust it:
COMPUTED when the two readers agreed and nothing is pending; COMPUTED WITH WARNING with the reasons when they disagreed, a
patient in scope is incomplete, or a citation does not resolve; REFUSED, with no numbers, when the question cannot be planned;
AGENT for open questions, with the agent's numbers checked against its tool outputs. The plan is saved with the answer, so
verify_saved replays it without a model.
"""

import dataclasses
import json
import time
from datetime import date
from pathlib import Path

from clinical_abstraction import agent, calculate, llm, questions, store
from clinical_abstraction.jev import Jev
from clinical_abstraction.questions import DateRange, Plan, PlanError
from clinical_abstraction.render import _range, _runs, _table

LISTED = 50  # patients shown one by one; the rest are counted (decision D1.8)
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


def _count(low: int, high: int, noun: str) -> str:
    return f"{_range(low, high)} {noun}{'' if low == high == 1 else 's'}"


def _cites(citations: list[str]) -> str:
    return f"[{', '.join(citations)}]" if citations else ""


def _bounds(window: DateRange | None) -> tuple[str, str]:
    start = window.start.isoformat() if window and window.start else "0000-01-01"
    return start, window.end.isoformat() if window and window.end else "9999-12-31"


def _listed(items: list[str]) -> str:
    more = f", and {len(items) - LISTED} more" if len(items) > LISTED else ""
    return (", ".join(items[:LISTED]) + more) or "none"


def _stored(connection, mrn: str, path: str):
    """One part of a patient's stored abstraction; SQLite reads the JSON, so Python parses only that part."""
    row = connection.execute("SELECT json_extract(abstraction, ?) FROM patients WHERE mrn = ?", (path, mrn)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def _flag_line(flag) -> str:
    settles = f" {flag['settles']}" if flag["settles"] else ""
    return f"- {flag['encounter'] or 'patient'} ({flag['kind']}): {flag['text']} {_cites(json.loads(flag['citations']))}{settles}"


# ---- Sessions, minutes, and days ----


def _matches(row: dict, attendance: str) -> bool:
    if attendance == "attended":
        return row["counts_as_therapy"] != "no"
    if attendance == "missed_by_patient":
        return row["status"] in MISSED
    if attendance == "cancelled_by_clinic":
        return row["status"] == "cancelled_by_clinic"
    return True


def _headline(plan: Plan, rows: list[dict]) -> str:
    totals = calculate.summarize(rows)
    low, high = totals["minutes_low"], totals["minutes_high"]
    minutes = f"{_range(low, high)} minutes ({_range(_hours(low), _hours(high))} hours)"
    sessions = _count(totals["sessions_low"], totals["sessions_high"], "therapy session")
    if plan.metric == "minutes":
        return f"**{minutes}** of patient-present therapy"
    if plan.metric == "days":
        return f"**{_count(totals['therapy_days_low'], totals['therapy_days_high'], 'distinct therapy day')}** ({sessions}, {minutes})"
    if plan.attendance == "attended":
        by_type = ", ".join(
            f"{_range(row['sessions_low'], row['sessions_high'])} {kind.replace('_psychotherapy', '')} "
            f"({_range(row['minutes_low'], row['minutes_high'])} min)"
            for kind, row in totals["by_service_type"].items()
        )
        days = _count(totals["therapy_days_low"], totals["therapy_days_high"], "distinct day")
        return f"**{sessions} on {days}**" + (f" ({by_type})" if by_type else "")
    if plan.attendance == "missed_by_patient":
        return f"**{_count(len(rows), len(rows), 'missed session')}** (no-show, absent, or cancelled by the patient)"
    if plan.attendance == "cancelled_by_clinic":
        return f"**{_count(len(rows), len(rows), 'session')} cancelled by the clinic**"
    return f"**{_count(len(rows), len(rows), 'session record')}**, of which {sessions} count toward the plan, with {minutes}"


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
    totals = calculate.summarize(rows)
    low, high = totals["minutes_low"], totals["minutes_high"]
    return [
        label,
        len(rows),
        _range(totals["sessions_low"], totals["sessions_high"]),
        _range(totals["therapy_days_low"], totals["therapy_days_high"]),
        _range(low, high),
        _range(_hours(low), _hours(high)),
    ]


def _filtered(connection, plan: Plan, mrn: str) -> str:
    """Sessions, minutes, or days for one patient, filtered by date range, service type, and attendance, then grouped.

    ponytail: an encounter whose records disagree on the date sits on its listed date here, and its date_conflict flag prints
    below; the goal table bounds both placements. Read service_dates from the abstraction if a filtered total must too.
    """
    rows = [
        row
        for row in agent.encounters(connection, mrn, *_bounds(plan.date_range))
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
        parts.append("\n".join(_table(headers, [_group_row(label, group) for label, group in groups])).strip())
    # A sessions answer also lists what did not count, and why, so a reviewer sees every ineligible record.
    listed = rows if plan.metric == "sessions" and plan.attendance == "attended" else matching
    if not listed:
        return "\n\n".join(parts)
    calculations = {row["encounter"]: row.get("calculation") for row in _stored(connection, mrn, "$.encounters") or []}
    table = [
        [
            row["service_date"],
            row["encounter"],
            f"{row['service_type']} ({row['modality']})",
            row["status"],
            row["counts_as_therapy"],
            _range(row["minutes_low"], row["minutes_high"]),
            calculations.get(row["encounter"]),
            ", ".join(row["sources"]),
        ]
        for row in listed
    ]
    headers = ["Date", "Encounter", "Service", "Status", "Counted", "Minutes", "Calculation or reason", "Sources"]
    parts.append("\n".join(_table(headers, table)).strip())
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
        return f"{'-' if days is None else _count(days, days, 'day')}, {'-' if minutes is None else minutes} min"
    parts = [_count(days, days, "therapy day")] if days is not None else []
    parts += [f"{minutes} patient-present minutes"] if minutes is not None else []
    return " and ".join(parts) or "no measurable amount"


def _goal(connection, plan: Plan, mrn: str) -> str:
    plans = _stored(connection, mrn, "$.plans") or []
    lines = [
        f"Plan version {row['version']} ({row['effective_date'] or 'start not stated'} to {row['effective_end'] or 'open'}): "
        f"at least {_goal_text(row)} per {calculate.DAYS[row['week_start_day']].capitalize()}-"
        f"{calculate.DAYS[(row['week_start_day'] + 6) % 7].capitalize()} week; counts "
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
            _range(week["therapy_days_low"], week["therapy_days_high"]),
            _range(week["minutes_low"], week["minutes_high"]),
            goals.get(week["plan_version"], "none documented"),
            f"**{STATUS_WORDS[week['status']]}**",
        ]
        for number, week in numbered
    ]
    lines += ["", "\n".join(_table(["Week", "Dates", "Therapy days", "Minutes", "Goal", "Status"], table)).strip(), ""]
    lines += [f"- Week {number}: {week['explanation'].rstrip('.')}." for number, week in numbered]
    runs = calculate.consecutive_shortfalls([week for _, week in numbered])
    established, depending = _runs(runs["established"]), _runs(runs["depends_on_unresolved_records"])
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
        f"{_count(reviewed, reviewed, 'patient')} reviewed: {len(established)} with 2 consecutive weeks below goal established "
        f"by the record, {len(depending)} whose inclusion depends on unresolved records, "
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
    compared = _stored(connection, mrn, "$.plan_versions_compared") or {"versions": [], "note": "No treatment plan is stored."}
    rows = [
        [
            f"{row['plan_version']} {_cites(row['sources'])}",
            f"{row['start']} to {row['end']} ({row['weeks']} weeks)",
            f"{row['goal_days']} days, {row['goal_minutes']} min",
            _range(row["sessions_low"], row["sessions_high"]),
            _range(row["minutes_low"], row["minutes_high"]),
            _range(row["minutes_per_week_low"], row["minutes_per_week_high"]),
            ", ".join(
                f"{kind}: {_count(item['sessions_low'], item['sessions_high'], 'session')}, "
                f"{_range(item['minutes_low'], item['minutes_high'])} min"
                for kind, item in row["by_service_type"].items()
            ),
        ]
        for row in compared["versions"]
    ]
    headers = ["Plan version", "Dates", "Goal", "Sessions", "Minutes", "Minutes per week", "By service type"]
    parts = ["\n".join(_table(headers, rows)).strip()] if rows else []
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
    lines += ["\n".join(_table(["Completed", "Instrument", "Total", "Band", "Source", "Copies"], table)).strip()]
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
    count = _count(len(flags), len(flags), "billing or documentation-integrity finding")
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
    lines, warnings = [questions.interpretation(plan)], list(plan.warnings)
    rows = connection.execute(
        "SELECT mrn, incomplete FROM patients WHERE incomplete != '[]' AND mrn IN (SELECT value FROM json_each(?)) ORDER BY mrn",
        (json.dumps(plan.patients),),
    )
    pending = [(mrn, item) for mrn, incomplete in rows for item in json.loads(incomplete)]
    if pending:
        count = f"{_count(len(pending), len(pending), 'document')} pending"
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
    broken = agent.broken_citations(connection, text)
    if broken:
        warnings.append(
            f"{_count(len(broken), len(broken), 'citation')} not found in the stored documents ({', '.join(broken[:10])})"
        )
    return text, warnings, broken


def _verdict(warnings: list[str]) -> str:
    return "COMPUTED WITH WARNING: " + "; ".join(warnings) if warnings else "COMPUTED"


def _plan_json(plan: Plan) -> dict:
    data, window = dataclasses.asdict(plan), plan.date_range
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


def _spent(budget: llm.Budget) -> float:
    return sum(budget.spent(account) for account in budget.caps)


def answer(connection, question: str, jev: Jev | None, budget: llm.Budget, model: str, force_agent: bool = False) -> dict:
    """Plan the question, answer it by code (or by the agent for open questions), save it with its plan, and return it."""
    started, spent_before = time.perf_counter(), _spent(budget)
    result = {"question": question, "plan": None, "interpretation": None, "warnings": [], "citations_broken": []}
    try:
        plan = questions.plan(question, store.known_patients(connection), store.episode_year(connection), jev)
    except PlanError as error:
        result |= {"answer": f"Not answered: {error}.", "verdict": f"REFUSED: {error}"}
    else:
        result |= {"plan": _plan_json(plan), "interpretation": questions.interpretation(plan)}
        if plan.kind == "open" or force_agent:
            lines, warnings = _header(connection, plan)
            try:
                reply = agent.answer_open(connection, question, model, budget, patients=plan.patients)
            except llm.BudgetExceeded:
                raise
            except Exception as error:  # noqa: BLE001 - one failed agent call must not lose a batch of answers
                reply = {"answer": "", "verdict": f"AGENT: not answered ({type(error).__name__}: {error})"[:300]}
            text = "\n".join(lines) + "\n\n" + reply["answer"] + "\n"
            verdict = reply["verdict"] + (f"; WARNING: {'; '.join(warnings)}" if warnings else "")
            result |= {"answer": text, "verdict": verdict, "warnings": warnings}
            result["citations_broken"] = agent.broken_citations(connection, text)
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
