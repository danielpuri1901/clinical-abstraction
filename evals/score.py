"""Score an abstraction against hand-derived gold labels. Every check is exact; no judge."""

import json
from pathlib import Path

from clinical_abstraction import calculate

GOLD_DIR = Path(__file__).parent / "gold"


def load_gold(mrn: str) -> dict:
    return json.loads((GOLD_DIR / f"{mrn}.json").read_text())


def score(abstraction: dict, gold: dict) -> dict:
    checks = []

    def check(name, expected, actual):
        checks.append({"check": name, "expected": expected, "actual": actual, "ok": expected == actual})

    encounters = {e["encounter"]: e for e in abstraction["encounters"]}
    for key, want in gold["encounters"].items():
        got = encounters.get(key)
        if got is None:
            check(f"{key} exists", True, False)
            continue
        for field in ("service_date", "service_type", "status", "counts_as_therapy", "modality"):
            if field in want:
                check(f"{key} {field}", want[field], got[field])
        if "minutes" in want:
            check(f"{key} minutes", want["minutes"], [got["minutes_low"], got["minutes_high"]])
    counted = sorted(e["encounter"] for e in abstraction["encounters"] if e["counts_as_therapy"] != "no")
    check("counted encounters (no extras, none missing)", sorted(gold["counted_encounters"]), counted)

    weeks = {w["week_start"]: w for w in abstraction["weeks"]}
    for want in gold["weeks"]:
        got = weeks.get(want["week_start"], {})
        check(f"week {want['week_start']} therapy days", want["therapy_days"],
              [got.get("therapy_days_low"), got.get("therapy_days_high")])
        check(f"week {want['week_start']} minutes", want["minutes"], [got.get("minutes_low"), got.get("minutes_high")])
        check(f"week {want['week_start']} status", want["status"], got.get("status"))

    start, end = gold["review_period"]
    check("review period", gold["review_period"],
          [abstraction["review_period"]["start"], abstraction["review_period"]["end"]])
    inside = [e for e in abstraction["encounters"] if start <= e["service_date"] <= end]
    totals = calculate.summarize(inside)
    want = gold["totals"]
    check("total sessions", [want["sessions"], want["sessions"]], [totals["sessions_low"], totals["sessions_high"]])
    check("sessions by service type", want["by_service_type"],
          {k: v["sessions_high"] for k, v in totals["by_service_type"].items()})
    check("distinct therapy days", [want["therapy_days"], want["therapy_days"]],
          [totals["therapy_days_low"], totals["therapy_days_high"]])
    check("total minutes", want["minutes"], [totals["minutes_low"], totals["minutes_high"]])

    distinct = sorted((a["completed_on"], a["total_score"]) for a in abstraction["assessments"]
                      if a["instrument"].upper().startswith("PHQ"))
    check("distinct PHQ-9 administrations", sorted((a["completed_on"], a["total_score"]) for a in gold["assessments"]),
          distinct)
    raised = {(f["kind"], f.get("encounter")) for f in abstraction["flags"]}
    for kind, encounter in gold["required_flags"]:
        check(f"flag {kind} on {encounter}", True, (kind, encounter) in raised)
    check("consecutive weeks below goal", gold["consecutive_weeks_below_goal"],
          abstraction["consecutive_weeks_below_goal"])

    passed = sum(c["ok"] for c in checks)
    return {"passed": passed, "total": len(checks), "accuracy": round(passed / len(checks), 4),
            "failures": [c for c in checks if not c["ok"]]}
