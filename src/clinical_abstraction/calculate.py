"""Calculations over one patient's abstraction. Pure functions with no model calls; this module never imports reconcile.

Uncertain encounters carry minutes as a range [low, high], and disputed encounters count only toward the high bounds.
Goal checks use three-valued logic: a week is "met" only if the low bounds meet the goal, "not_met" only if even the high
bounds miss it, and "cannot_determine" otherwise. The answer is then the same for every way the open conflicts could be
resolved. A week holding an encounter with conflicting dates, or any week of a patient with documents still pending, is
"cannot_determine" whatever its totals say.
"""

from datetime import date, timedelta
from itertools import product

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


def _bounded(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def _therapy_days(low: int, high: int) -> str:
    return f"{_bounded(low, high)} therapy day{'' if (low, high) == (1, 1) else 's'}"


def goal_status(plan: dict | None, days_low: int, days_high: int, minutes_low: int, minutes_high: int) -> tuple[str, str]:
    if plan is None or (plan["min_days_per_week"] is None and plan["min_minutes_per_week"] is None):
        return "cannot_determine", "no measurable treatment-plan goal is documented for this week"
    need_days, need_minutes = plan["min_days_per_week"] or 0, plan["min_minutes_per_week"] or 0
    goal = f"goal of {_therapy_days(need_days, need_days)} and {need_minutes} minutes"
    stated = f"{_therapy_days(days_low, days_high)} and {_bounded(minutes_low, minutes_high)} minutes"
    if days_low >= need_days and minutes_low >= need_minutes:
        return "met", f"{stated} meet the {goal}"
    if days_high < need_days or minutes_high < need_minutes:
        misses = [f"{_therapy_days(days_high, days_high)} < {need_days}"] if days_high < need_days else []
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
