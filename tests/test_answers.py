"""Answers: executors over the derived tables, verdicts, saved plans replayed without a model, the open-question agent, and the
CLI. No test calls a model: FakeJev reads the questions, and a scripted function stands in for Claude."""

import json
from dataclasses import replace
from datetime import date

import pytest

from clinical_abstraction import agent, answers, cli, read, reconcile, store
from clinical_abstraction.questions import DateRange, Plan
from clinical_abstraction.schema import Observation
from tests.fakes import FakeJev
from tests.fixtures import mini_patient_docs

PENDING = [{"doc_id": "D11", "sha": "f" * 64, "reason": "RuntimeError: rate limit", "attempts": 1}]
USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
ATTENDED = FakeJev({"metric": ("sessions", 1.0), "attendance": ("attended", 1.0)})


def add_patient(connection, mrn: str, docs, pending=None) -> None:
    """Store one patient the way a rebuild does, plus its documents, so citations resolve. No pipeline, no model."""
    store.replace_patient(connection, mrn, reconcile.build_patient(mrn, docs, pending), f"fingerprint {mrn}")
    connection.executemany(
        "INSERT OR IGNORE INTO documents (sha, doc_id, patient_id, text) VALUES (?, ?, ?, ?)",
        [(doc.sha, doc.doc_id, mrn, doc.text) for doc in docs],
    )
    connection.commit()


def renamed(docs, mrn: str, name: str):
    return [replace(doc, extraction=doc.extraction.model_copy(update={"patient_name": name, "patient_mrn": mrn})) for doc in docs]


def lowered_goal(docs):
    """The mini patient with a goal of 1 day and 45 minutes, so the week of E3 (40-50 minutes) straddles the goal."""
    plan_doc = docs[8]
    requirement = plan_doc.extraction.plan_requirements[0].model_copy(
        update={"min_therapy_days_per_week": 1, "min_patient_present_minutes_per_week": 45}
    )
    docs[8] = replace(plan_doc, extraction=plan_doc.extraction.model_copy(update={"plan_requirements": [requirement]}))
    return docs


@pytest.fixture
def mini_store(tmp_path):
    connection = store.connect(tmp_path / "abstraction.db")
    add_patient(connection, "T-M001", mini_patient_docs())
    yield connection
    connection.close()


@pytest.fixture
def mini_store_with_two_patients(mini_store):
    add_patient(mini_store, "T-M002", renamed(mini_patient_docs(), "T-M002", "Jordan Reyes"))
    return mini_store


@pytest.fixture
def mini_store_incomplete(tmp_path):
    connection = store.connect(tmp_path / "incomplete.db")
    add_patient(connection, "T-M001", mini_patient_docs(), PENDING)
    add_patient(connection, "T-M002", renamed(mini_patient_docs(), "T-M002", "Jordan Reyes"))
    yield connection
    connection.close()


@pytest.fixture
def budget(tmp_path):
    return read.Budget(tmp_path / "spend.json", {"claude": 1.0, "jev": 0.5})


def plan_for(metric, attendance="attended", service_type="any", group_by="none", patients=("T-M001",), date_range=None) -> Plan:
    return Plan("computed", metric, attendance, service_type, group_by, list(patients), date_range, {}, [])


def table_row(text: str, first_cell: str) -> list[str]:
    """The cells of the Markdown table row that starts with first_cell."""
    return next(line.strip("| ").split(" | ") for line in text.splitlines() if line.startswith(f"| {first_cell} |"))


def test_executors_respect_filters_on_the_mini_patient(mini_store):
    sessions = answers.run(mini_store, plan_for("sessions"))
    assert "3 therapy sessions" in sessions[0][1]  # E1, E2, E3 count; E4 is a no-show
    missed = answers.run(mini_store, plan_for("sessions", attendance="missed_by_patient"))
    assert "1 missed" in missed[0][1] and "E4" in missed[0][1] and "E1" not in missed[0][1]
    # A partner-only collateral call the patient did not join is not a session the patient missed.
    assert not answers._matches({"status": "patient_absent", "service_type": "collateral_contact", "counts_as_therapy": "no"}, "missed_by_patient")
    assert answers._matches({"status": "no_show", "service_type": "group_psychotherapy", "counts_as_therapy": "no"}, "missed_by_patient")
    group = answers.run(mini_store, plan_for("sessions", service_type="group_psychotherapy"))[0][1]
    assert "1 therapy session " in group and "E2" not in group and "E3" not in group
    week = DateRange(date(2026, 1, 19), date(2026, 1, 25), "2026-01-19 to 2026-01-25")
    ranged = answers.run(mini_store, plan_for("minutes", date_range=week))
    assert "105 minutes" in ranged[0][1]  # 60 + 45; E3 on January 26 is outside
    by_week = answers.run(mini_store, plan_for("minutes", group_by="week"))[0][1]
    assert table_row(by_week, "2026-01-05 to 2026-01-11") == ["2026-01-05 to 2026-01-11", "0", "0", "0", "0", "0"]  # no therapy
    week_of_e3 = ["2026-01-26 to 2026-01-27", "1", "1", "1", "40-50", "0.67-0.83"]  # the E3 conflict stays a range
    assert table_row(by_week, "2026-01-26 to 2026-01-27") == week_of_e3
    billing = answers.run(mini_store, plan_for("billing_integrity", attendance="not_applicable"))[0][1]
    assert "Charge C4" in billing and "D8 L5" in billing
    add_patient(mini_store, "T-M003", lowered_goal(mini_patient_docs()))
    goal = answers.run(mini_store, plan_for("goal_status", attendance="not_applicable", patients=["T-M003"]))
    assert "An addendum or signed note stating the start time of E3 would settle it" in goal[0][1]
    assert "cannot be determined" in table_row(goal[0][1], "4")[-1]


def test_answer_marks_incomplete_patients_and_refuses_unknown_ones(mini_store_incomplete, budget):
    result = answers.answer(mini_store_incomplete, "How many sessions did Rowan attend?", ATTENDED, budget, "m")
    lines = result["answer"].splitlines()
    assert lines[0] == "Interpreted as: sessions, attended, T-M001, all dates"
    assert lines[1].startswith("Incomplete: 1 document pending (D11") and "rate limit" in lines[1]
    assert result["verdict"].startswith("COMPUTED WITH WARNING") and "1 document pending" in result["verdict"]
    other = answers.answer(mini_store_incomplete, "How many sessions did Jordan attend?", ATTENDED, budget, "m")
    assert other["verdict"] == "COMPUTED" and "Incomplete" not in other["answer"]  # only patients in scope count
    refused = answers.answer(mini_store_incomplete, "How many sessions did Alex Smith attend?", FakeJev({}), budget, "m")
    assert refused["verdict"] == "REFUSED: unknown patient: Alex Smith" and "sessions" not in refused["answer"]
    saved = dict(mini_store_incomplete.execute("SELECT question, verdict FROM answers").fetchall())
    assert saved["How many sessions did Alex Smith attend?"] == refused["verdict"] and len(saved) == 3


def test_unbound_numbers_are_listed():
    assert agent.unbound_numbers("Ten flags across 8 encounters on 2026-01-19 [BH-D103 L7]", ['{"count": 10}']) == ["8"]


def test_verify_saved_replays_plans_without_a_model(mini_store_with_two_patients, budget, tmp_path):
    connection = mini_store_with_two_patients
    rowan = answers.answer(connection, "How many sessions did Rowan attend?", ATTENDED, budget, "m")
    weekly = FakeJev({"metric": ("minutes", 1.0), "attendance": ("attended", 1.0), "group_by": ("week", 1.0)})
    jordan = answers.answer(connection, "How many minutes did Jordan receive each week?", weekly, budget, "m")
    assert (rowan["verdict"], jordan["verdict"], rowan["citations_broken"]) == ("COMPUTED", "COMPUTED", [])
    report = tmp_path / "answers.json"
    assert [result["verdict"] for result in answers.verify_saved(connection, report)] == ["COMPUTED", "COMPUTED"]
    connection.execute("UPDATE encounters SET minutes_low = 30, minutes_high = 30 WHERE mrn = 'T-M001' AND encounter = 'E2'")
    replayed = {result["question"]: result["verdict"] for result in answers.verify_saved(connection, report)}
    assert replayed["How many sessions did Rowan attend?"].startswith("NOT VERIFIED")
    assert replayed["How many minutes did Jordan receive each week?"] == "COMPUTED"


def test_collection_answers_give_counts_the_first_50_patients_and_how_many_more(tmp_path):
    connection = store.connect(tmp_path / "collection.db")
    complete = reconcile.build_patient("T-M001", mini_patient_docs())
    pending = reconcile.build_patient("T-M001", mini_patient_docs(), PENDING)
    for number in range(55):
        mrn = f"C-{number:04d}"
        abstraction = pending if number >= 52 else complete
        store.replace_patient(connection, mrn, {**abstraction, "patient": {"mrn": mrn, "name": None}}, "fingerprint")
    everyone = [mrn for mrn, _ in store.known_patients(connection)]
    text = answers.run(connection, plan_for("consecutive_shortfall", attendance="not_applicable", patients=everyone))[0][1]
    assert "55 patients reviewed: 52 with 2 consecutive weeks below goal established by the record" in text
    assert "3 whose inclusion depends on unresolved records" in text
    assert "C-0049" in text and "C-0050" not in text and "2 more" in text  # 50 of the 52 established patients are named
    assert "C-0054" in text and "Reading the pending document D11 would settle it" in text
    sections = answers.run(connection, plan_for("sessions", patients=everyone))
    assert len(sections) == 51 and "5 more patients" in sections[-1][1]


def test_progress_keeps_the_whole_course_when_a_question_names_one_day(tmp_path):
    docs = mini_patient_docs()
    for index, day, reason in (
        (4, "2026-01-19", "Panic episode at work the day before"),
        (5, "2026-01-26", "Routine weekly session"),
    ):
        observation = Observation(observed_on=day, category="reason_for_contact", statement=reason, lines=[1])
        docs[index] = replace(docs[index], extraction=docs[index].extraction.model_copy(update={"observations": [observation]}))
    connection = store.connect(tmp_path / "progress.db")
    add_patient(connection, "T-M001", docs)
    day = DateRange(date(2026, 1, 19), date(2026, 1, 19), "2026-01-19")
    text = answers.run(connection, plan_for("progress", attendance="not_applicable", date_range=day))[0][1]
    assert "2 distinct administrations" in text and "D10 L5" in text  # the imported copy is named, not counted
    assert "fell 4 points, from 18" in text and "Mood still low, sleep improving" in text  # both before January 19
    assert "Panic episode at work the day before" in text and "Routine weekly session" not in text


def test_open_questions_go_to_the_agent_and_numbers_must_come_from_tools(mini_store, budget, monkeypatch):
    use = {"type": "tool_use", "id": "t1", "name": "encounters", "input": {"mrn": "T-M001", "start_date": "2026-01-26"}}
    final = {"type": "text", "text": "E3 on 2026-01-26 lasted 40-50 minutes [D6 L3, D7 L3]; 7 clinicians signed it."}
    replies = iter([{"content": [use], "usage": USAGE, "stop_reason": "tool_use"}, {"content": [final], "usage": USAGE}])
    sent = []

    def scripted_call(model, system, messages, tools, max_tokens=3000):
        sent.append(messages[-1])
        return next(replies)

    monkeypatch.setattr(read, "call_anthropic", scripted_call)
    other = FakeJev({"metric": ("other", 1.0)})
    result = answers.answer(mini_store, "Which clinician wrote the January 26 note?", other, budget, "claude-haiku-5-5")
    assert result["answer"].splitlines()[0] == "Interpreted as: open question, T-M001, 2026-01-26"
    assert result["verdict"] == "AGENT: unbound numbers 7"  # 40 and 50 come from the encounters tool
    tool_result = sent[1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and '"minutes_low": 40' in tool_result["content"]
    assert result["cost_usd"] > 0 and budget.spent("claude") == pytest.approx(result["cost_usd"])

    calls = []

    def always_a_tool(model, system, messages, tools, max_tokens=3000):
        calls.append(model)
        return {"content": [use], "usage": USAGE, "stop_reason": "tool_use"}

    stuck = agent.answer_open(mini_store, "Which clinician?", "claude-haiku-5-5", budget, call=always_a_tool)
    assert len(calls) == 8 and stuck["verdict"].startswith("AGENT: no final answer")


def test_cli_answers_saves_and_verifies_without_a_decision_model(tmp_path, monkeypatch, capsys):
    db = tmp_path / "abstraction.db"
    connection = store.connect(db)
    add_patient(connection, "T-M001", mini_patient_docs())
    connection.close()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no key files to load
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    questions_file = tmp_path / "questions.json"
    questions_file.write_text(json.dumps([{"id": "Q-01", "question": "How many minutes of therapy did Rowan receive?"}]))
    base = ["--db", str(db), "--out", str(tmp_path / "out")]
    assert cli.main([*base, "answer-questions", str(questions_file)]) == 0
    assert "JEV_API_KEY" in capsys.readouterr().err  # the rules-only path is loud
    saved = json.loads((tmp_path / "out" / "answers" / "answers.json").read_text())
    assert [(item["id"], item["verdict"]) for item in saved] == [("Q-01", "COMPUTED WITH WARNING: rules only, no decision model")]
    assert "145-155 minutes" in (tmp_path / "out" / "answers" / "Q-01.md").read_text()
    assert cli.main([*base, "verify-answers"]) == 0 and "Q-01: COMPUTED" in capsys.readouterr().out
    assert cli.main([*base, "ask", "How many sessions did Alex Smith attend?"]) == 1
    assert "REFUSED: unknown patient: Alex Smith" in capsys.readouterr().out
