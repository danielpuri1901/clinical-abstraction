"""Exports: one patient's abstraction as Markdown for a reviewer and as JSON for code. Pure functions; no model, no database."""

import json


def to_json(abstraction: dict) -> str:
    return json.dumps(abstraction, indent=1) + "\n"


def _range(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def _cell(value) -> str:
    return "-" if value is None or value == "" else str(value).replace("|", "/").replace("\n", " ")


def _table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["None.", ""]
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows),
        "",
    ]


def _runs(runs: list[list[str]]) -> str:
    return "; ".join(" and ".join(run) for run in runs) or "none"


def markdown(abstraction: dict) -> str:
    patient, period, built = abstraction["patient"], abstraction["review_period"], abstraction.get("built_with")
    lines = [f"# Clinical abstraction: {patient['name'] or 'name not stated'} ({patient['mrn']})", ""]
    if period["start"]:
        lines.append(f"Review period: {period['start']} to {period['end']} (basis: {period['basis']}).")
    else:
        lines.append(f"Review period: not known ({period['basis']}).")
    if built:
        lines.append(
            f"Built from {built['documents_extracted']} documents; extractor model {built['model']}, version {built['extractor_version']}."
        )
    lines += ["Minutes are patient-present minutes. A range `low-high` means the records disagree and none of them settles it.", ""]

    pending = abstraction["incomplete"]
    if pending:
        noun = "document is" if len(pending) == 1 else "documents are"
        lines += ["## Incomplete", "", f"{len(pending)} {noun} not read yet, so no week is settled until it is read.", ""]
        lines += [f"- {item['doc_id']}: {item['reason']} (attempts: {item['attempts']})" for item in pending] + [""]

    lines += ["## Weeks", ""]
    weeks = [
        [
            f"{w['week_start']} to {w['week_end']}",
            _range(w["therapy_days_low"], w["therapy_days_high"]),
            _range(w["minutes_low"], w["minutes_high"]),
            w["status"],
            w["explanation"],
        ]
        for w in abstraction["weeks"]
    ]
    lines += _table(["Week", "Therapy days", "Minutes", "Status", "Why"], weeks)
    shortfalls = abstraction["consecutive_weeks_below_goal"]
    lines += [
        (
            f"Runs of {shortfalls['run_length']} weeks below goal, established: {_runs(shortfalls['established'])}. "
            f"Depending on unresolved records: {_runs(shortfalls['depends_on_unresolved_records'])}."
        ),
        "",
    ]

    lines += ["## Encounters", "", "One row per real contact, after merging every document that describes it.", ""]
    encounters = [
        [
            " / ".join(e["service_dates"]) if len(e.get("service_dates") or []) > 1 else e["service_date"],
            e["encounter"],
            f"{e['service_type']} ({e['modality']})",
            e["status"],
            e["counts_as_therapy"],
            _range(e["minutes_low"], e["minutes_high"]),
            ", ".join(e["sources"]),
        ]
        for e in abstraction["encounters"]
    ]
    lines += _table(["Date", "Encounter", "Type", "Status", "Counted", "Minutes", "Sources"], encounters)

    lines += ["## Flags", ""]
    for flag in abstraction["flags"]:
        cited = f" [{', '.join(flag['citations'])}]" if flag["citations"] else ""
        settles = f" {flag['settles']}" if flag["settles"] else ""
        lines.append(f"- **{flag['kind']}** ({flag['encounter'] or 'patient'}): {flag['text']}{cited}{settles}")
    lines += [""] if abstraction["flags"] else ["None.", ""]

    lines += ["## Assessments", "", "Each distinct administration once; a copy of a result is named, never counted again.", ""]
    assessments = [
        [a["completed_on"], a["instrument"], a["total_score"], ", ".join(a["sources"]), ", ".join(a["copies"])]
        for a in abstraction["assessments"]
    ]
    lines += _table(["Completed", "Instrument", "Total", "Sources", "Copies"], assessments)

    lines += ["## Plan versions", ""]
    compared = abstraction["plan_versions_compared"]
    delivered = {row["plan_version"]: row for row in compared["versions"]}
    plans = []
    for plan in abstraction["plans"]:
        row = delivered.get(plan["version"])
        plans.append(
            [
                plan["version"],
                f"{plan['effective_date'] or 'not stated'} to {plan['effective_end'] or 'open'}",
                f"{plan['min_days_per_week']} days, {plan['min_minutes_per_week']} minutes",
                ", ".join(plan["counted_service_types"]),
                _range(row["minutes_per_week_low"], row["minutes_per_week_high"]) if row else None,
                ", ".join(plan["sources"]),
            ]
        )
    lines += _table(["Version", "Effective", "Goal per week", "Counted types", "Delivered minutes per week", "Sources"], plans)
    if compared["note"]:
        lines += [compared["note"], ""]

    lines += ["## Observations", ""]
    observations = [[o["observed_on"], o["category"], o["text"], ", ".join(o["sources"])] for o in abstraction["observations"]]
    lines += _table(["Date", "Category", "Statement", "Sources"], observations)
    return "\n".join(lines).rstrip("\n") + "\n"
