"""Question plans: code reads patients and dates, keyword rules and the decision model read the enum values, and any
disagreement becomes a warning. No test calls a model; FakeJev answers from a table."""

import json
import re
from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from clinical_abstraction import questions
from clinical_abstraction.questions import DateRange, Plan, PlanError, parse_dates, parse_patients
from clinical_abstraction.schema import ServiceType
from tests.fakes import FakeJev

KNOWN = [("HG-M042", "Rowan Mercer"), ("NF-20817", "Jordan Reyes")]
EVAL_QUESTIONS = Path(__file__).resolve().parents[1] / "evals" / "questions.json"
FIELDS = ("metric", "attendance", "service_type", "group_by")


def test_dates_parse_or_refuse():
    assert parse_dates("sessions between January 12 and January 25", 2026) == DateRange(
        date(2026, 1, 12), date(2026, 1, 25), "2026-01-12 to 2026-01-25"
    )
    assert parse_dates("hours in the week of January 12", 2026).end == date(2026, 1, 18)
    assert parse_dates("minutes after January 19", 2026) == DateRange(date(2026, 1, 20), None, "after 2026-01-19")
    assert parse_dates("what happened on 30 January 2026", 2026).start == date(2026, 1, 30)
    assert parse_dates("Did the PHQ-9 decrease 5 points or more?", 2026) is None  # not December 5
    with pytest.raises(PlanError, match="February 30"):
        parse_dates("What happened on February 30?", 2026)
    with pytest.raises(PlanError, match="week 2 is not a date"):
        parse_dates("How many individual sessions happened in week 2?", 2026)


def test_patients_match_or_refuse():
    assert parse_patients("How many sessions did Rowan attend?", KNOWN) == ["HG-M042"]
    assert parse_patients("Compare Rowan and Jordan", KNOWN) == ["HG-M042", "NF-20817"]
    assert parse_patients("Which patients had two consecutive weeks below goal?", KNOWN) == ["HG-M042", "NF-20817"]
    assert parse_patients("Did Rowan's sleep improve?", [*KNOWN, ("AL-1", "Ann Lee")]) == ["HG-M042"]  # "lee" in "sleep"
    with pytest.raises(PlanError, match="unknown patient: Alex Smith"):
        parse_patients("How many sessions did Alex Smith attend?", KNOWN)
    with pytest.raises(PlanError, match="unknown patient: Jordan Lee"):  # a known first name with a wrong surname
        parse_patients("How many sessions did Jordan Lee attend?", KNOWN)
    with pytest.raises(PlanError, match="unknown patient: HG-M999"):
        parse_patients("How many sessions did HG-M999 attend?", KNOWN)


def test_agreement_decides_the_verdict():
    agreed = FakeJev(
        {
            "metric": ("sessions", 1.0),
            "attendance": ("missed_by_patient", 1.0),
            "service_type": ("any", 0.95),
            "group_by": ("none", 0.95),
        }
    )
    plan = questions.plan("How many sessions did Rowan miss?", KNOWN, 2026, agreed)
    assert plan.metric == "sessions" and plan.attendance == "missed_by_patient" and plan.warnings == []
    assert [(state, set(names)) for state, names in agreed.calls] == [("How many sessions did Rowan miss?", set(FIELDS))]
    disputed = FakeJev(
        {"metric": ("sessions", 0.55), "attendance": ("any", 0.9), "service_type": ("any", 0.9), "group_by": ("none", 0.9)}
    )
    plan = questions.plan("Which weeks fell short of the plan?", KNOWN, 2026, disputed)
    assert plan.warnings and "disputed metric" in plan.warnings[0]
    target = questions.plan("Was the 150-minute weekly target met?", KNOWN, 2026, FakeJev({"metric": ("goal_status", 1.0)}))
    assert target.warnings == ["disputed metric: rules say minutes, decision model says goal_status (1.00)"]
    assert (
        questions.plan("Which clinician wrote the January 22 note?", KNOWN, 2026, FakeJev({"metric": ("other", 1.0)})).kind
        == "open"
    )
    rules_only = questions.plan("How many minutes of therapy did Rowan receive?", KNOWN, 2026, None)
    assert rules_only.metric == "minutes" and rules_only.warnings == ["rules only, no decision model"]


def test_filters_the_metric_does_not_use_are_neutral_and_never_disputed():
    unsure = FakeJev(
        {
            "metric": ("goal_status", 1.0),
            "attendance": ("cancelled_by_clinic", 0.4),
            "service_type": ("group_psychotherapy", 0.4),
            "group_by": ("day", 0.4),
        }
    )
    plan = questions.plan("Did Rowan meet the weekly goal?", KNOWN, 2026, unsure)
    assert (plan.attendance, plan.service_type, plan.group_by, plan.warnings) == ("not_applicable", "any", "none", [])


def test_rules_alone_refuse_a_question_without_one_reading():
    with pytest.raises(PlanError, match="cannot interpret the question"):
        questions.plan("Which weeks fell short of the plan?", KNOWN, 2026, None)  # no rule matches
    with pytest.raises(PlanError, match="cannot interpret the question"):
        questions.plan("Was Rowan billed for any session that did not happen?", KNOWN, 2026, None)  # two rules match


def test_interpretation_names_the_plan_and_stays_short_for_a_collection():
    window = DateRange(date(2026, 1, 12), date(2026, 1, 25), "2026-01-12 to 2026-01-25")
    plan = Plan("computed", "sessions", "attended", "any", "week", ["HG-M042"], window, {}, [])
    assert questions.interpretation(plan) == "Interpreted as: sessions, attended, HG-M042, 2026-01-12 to 2026-01-25, by week"
    collection = Plan(
        "computed", "consecutive_shortfall", "not_applicable", "any", "none", [f"P-{n}" for n in range(2000)], None, {}, []
    )
    assert questions.interpretation(collection) == "Interpreted as: consecutive_shortfall, 2000 patients, all dates"


def test_the_vocabulary_matches_the_schema_and_the_decision_model():
    assert set(get_args(questions.ServiceTypeFilter)) - {"any"} <= set(get_args(ServiceType))
    enums = {"metric": questions.Metric, "attendance": questions.AttendanceFilter}
    enums |= {"service_type": questions.ServiceTypeFilter, "group_by": questions.GroupBy}
    assert {field: set(choice.criteria) for field, choice in questions.JEV_QUESTIONS.items()} == {
        field: set(get_args(enum)) for field, enum in enums.items()
    }


def _iso(window: DateRange | None) -> list | None:
    return None if window is None else [day.isoformat() if day else None for day in (window.start, window.end)]


def test_every_eval_question_plans_without_a_wrong_plain_computed():
    items = json.loads(EVAL_QUESTIONS.read_text())
    assert len(items) == 54 and sum(bool(item.get("rules_wrong")) for item in items) == 7
    for item in items:
        expected = item["expected"]
        jev = FakeJev.from_expected(expected)
        if "refused" in item:
            with pytest.raises(PlanError, match=re.escape(item["refused"])):
                questions.plan(item["question"], KNOWN, 2026, jev)
            assert jev.calls == [], item["id"]  # refused before the decision model is paid
            continue
        plan = questions.plan(item["question"], KNOWN, 2026, jev)
        got = {field: getattr(plan, field) for field in FIELDS} | {"patients": plan.patients, "date_range": _iso(plan.date_range)}
        assert got == expected, item["id"]
        assert plan.kind == ("open" if expected["metric"] == "other" else "computed"), item["id"]
        if item.get("rules_wrong"):
            rules = questions.rule_candidates(item["question"])
            assert expected["metric"] not in rules.get("metric", set()) or plan.warnings == [], item["id"]
