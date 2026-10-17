"""Synthetic patients: the truth comes first, documents and gold files are rendered from it."""

import json
import re

import pytest

from evals import synthetic

# store.provisional_patient_id uses this pattern (plan Task 4); the test falls back to it until store.py lands.
PATIENT_ID = re.compile(r"(?:MRN|Record\s*#|Medical record(?: number)?)[:\s#]*([A-Z]{2}-[A-Z]?\d{3,6})")
TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*(AM|PM))?|\b([01]\d|2[0-3])([0-5]\d)\b")


def provisional_patient_id(text: str) -> str | None:
    try:
        from clinical_abstraction import store
    except ImportError:
        found = PATIENT_ID.search(text)
        return found.group(1) if found else None
    return store.provisional_patient_id(text)


def times_in(text: str) -> set[str]:
    """Every clock time in the text as HH:MM: 24-hour, 12-hour with AM or PM, and bare HHMM."""
    found = set()
    for hour, minute, half, bare_hour, bare_minute in TIME.findall(text):
        if bare_hour:
            found.add(f"{bare_hour}:{bare_minute}")
        else:
            found.add(f"{int(hour) % 12 + (12 if half == 'PM' else 0) if half else int(hour):02d}:{minute}")
    return found


def cited_values(extraction):
    """(value, cited line numbers) for every time, minute count, score, and plan number the referee checks."""
    for event in extraction.service_events:
        times = [event.scheduled_start, event.scheduled_end, event.patient_arrival, event.patient_departure]
        times += [t for interval in event.patient_present_intervals for t in (interval.start, interval.end)]
        yield from ((t, event.lines) for t in times if t)
        yield from ((t, cut.lines) for cut in event.excluded_intervals for t in (cut.start, cut.end))
        if event.stated_patient_minutes is not None:
            yield str(event.stated_patient_minutes), event.lines
    yield from ((correction.corrected_value, correction.lines) for correction in extraction.corrections)
    yield from ((str(assessment.total_score), assessment.lines) for assessment in extraction.assessments)
    for plan in extraction.plan_requirements:
        yield str(plan.min_therapy_days_per_week), plan.lines
        yield str(plan.min_patient_present_minutes_per_week), plan.lines


def test_generation_is_deterministic_and_complete(tmp_path):
    first = synthetic.generate(seed=7, patients=3, out=tmp_path / "a")
    second = synthetic.generate(seed=7, patients=3, out=tmp_path / "b")
    assert first == second and len(first["patients"]) == 3
    assert all(10 <= p["documents"] <= 15 for p in first["patients"])
    assert sum("duplicate" in p["hazards"] for p in synthetic.generate(seed=1, patients=25, out=tmp_path / "c")["patients"]) >= 3
    files = {name: sorted((tmp_path / name / "documents").glob("*.txt")) for name in ("a", "b")}
    assert [(p.name, p.read_bytes()) for p in files["a"]] == [(p.name, p.read_bytes()) for p in files["b"]]
    assert len(files["a"]) == sum(p["documents"] for p in first["patients"])
    assert all(
        json.loads((tmp_path / "a" / "gold" / f"{p['mrn']}.json").read_text())["mrn"] == p["mrn"] for p in first["patients"]
    )


def test_truth_replay_scores_perfectly(tmp_path):
    manifest = synthetic.generate(seed=7, patients=25, out=tmp_path)  # the Task 8 set; its first 5 patients miss two hazards
    assert {h for p in manifest["patients"] for h in p["hazards"]} == set(synthetic.HAZARDS)
    reconcile = pytest.importorskip("clinical_abstraction.reconcile")
    from clinical_abstraction import calculate
    from evals import score

    for entry in manifest["patients"]:
        abstraction = reconcile.build_patient(entry["mrn"], synthetic.truth_extractions(entry))
        abstraction["weeks"] = calculate.weekly(abstraction)
        abstraction["consecutive_weeks_below_goal"] = calculate.consecutive_shortfalls(abstraction["weeks"])
        result = score.score(abstraction, json.loads((tmp_path / "gold" / f"{entry['mrn']}.json").read_text()))
        assert result["passed"] == result["total"], (entry["mrn"], result["failures"])


def test_unsettled_conflicts_straddle_the_weekly_goal(tmp_path):
    manifest = synthetic.generate(seed=7, patients=25, out=tmp_path)
    weeks = [week for path in sorted((tmp_path / "gold").glob("*.json")) for week in json.loads(path.read_text())["weeks"]]
    counts = {status: sum(week["status"] == status for week in weeks) for status in ("met", "not_met", "cannot_determine")}
    unsettled = sum("conflicting_notes" in p["hazards"] and "addendum" not in p["hazards"] for p in manifest["patients"])
    assert counts["cannot_determine"] >= 4 and 2 * counts["cannot_determine"] >= unsettled
    assert manifest["week_statuses"] == counts


def test_templates_carry_their_format_hazards(tmp_path):
    synthetic.generate(seed=3, patients=6, out=tmp_path)
    files = sorted((tmp_path / "documents").glob("*.txt"))
    texts = [p.read_text() for p in files]
    assert any(re.search(r"\b\d{1,2} (January|February) 2026\b", t) for t in texts)
    assert any(re.search(r"\b\d{1,2}:\d{2} (AM|PM)\b", t) for t in texts) and any(
        re.search(r"\b(0[89]|1[0-7])[0-5]\d\b", t) for t in texts
    )
    assert all(provisional_patient_id(text) == path.name.split("_")[0] for path, text in zip(files, texts))


def test_truth_citations_point_at_their_values(tmp_path):
    try:
        from clinical_abstraction import verify
    except ImportError:
        verify = None
    manifest = synthetic.generate(seed=5, patients=6, out=tmp_path)
    assert {p["template"] for p in manifest["patients"]} == {"A", "B", "C"}
    for entry in manifest["patients"]:
        for doc in synthetic.truth_extractions(entry):
            lines = doc.text.splitlines()
            for value, numbers in cited_values(doc.extraction):
                assert numbers and all(1 <= n <= len(lines) for n in numbers), (doc.doc_id, value, numbers)
                cited = " ".join(lines[n - 1] for n in numbers)
                found = value in times_in(cited) if ":" in value else re.search(rf"\b{value}\b", cited)
                assert found, (doc.doc_id, value, numbers)
            if verify is not None:
                assert verify.admit(doc.doc_id, doc.text, doc.extraction)[1] == [], doc.doc_id


def test_gold_arithmetic_subtracts_exclusions_and_keeps_three_values():
    assert synthetic.minutes_present([(600, 675)], [(645, 660)]) == 60  # 10:00-11:15 minus the 10:45-11:00 break
    assert synthetic.minutes_present([(780, 800), (810, 835)], [(800, 810)]) == 45  # video, disconnect in the gap
    assert synthetic.minutes_present([(615, 645)], [(600, 615)]) == 30  # partner-only time before the patient joined
    plan = {"min_days_per_week": 3, "min_minutes_per_week": 150}
    assert synthetic.goal_status(plan, 3, 3, 145, 155) == "cannot_determine"
    assert synthetic.goal_status(plan, 3, 3, 150, 160) == "met"
    assert synthetic.goal_status(plan, 2, 2, 180, 180) == "not_met"
