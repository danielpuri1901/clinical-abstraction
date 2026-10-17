"""Model clients: prices and budget, one-document extraction with repair, and the Jev choice client. No test calls a model."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import get_args

import anthropic
import httpx
import httpx2
import pytest

from clinical_abstraction import extract, jev, llm
from clinical_abstraction.reconcile import Problem
from clinical_abstraction.schema import Attendance, DocumentExtraction, RecordRole, ServiceEvent, ServiceType

# conftest replaces llm.call_anthropic for every test. This keeps the real function for the one test that checks its request.
REAL_CALL_ANTHROPIC = llm.call_anthropic

DOC_TEXT = "Scheduled group: 10:00-11:30.\nPatient arrival: 10:00 | Patient departure: 11:15\nService date: January 19, 2026\n"
DOC_AS_SENT = (
    "Document ID: D1\n\nL1: Scheduled group: 10:00-11:30.\nL2: Patient arrival: 10:00 | Patient departure: 11:15\n"
    "L3: Service date: January 19, 2026"
)
USAGE = {"input_tokens": 10, "output_tokens": 5}  # 3.5e-06 USD on claude-haiku-5-5: 10 * 0.10 / 1e6 + 5 * 0.50 / 1e6


def event(**fields) -> ServiceEvent:
    return ServiceEvent(
        **{
            "encounter_id": "E1",
            "appointment_ids": [],
            "service_date": "2026-01-19",
            "service_type": "group_psychotherapy",
            "modality": "in_person",
            "scheduled_start": None,
            "scheduled_end": None,
            "attendance": "attended",
            "patient_arrival": "10:00",
            "patient_departure": "11:15",
            "patient_present_intervals": [],
            "stated_patient_minutes": None,
            "excluded_intervals": [],
            "record_role": "attendance_record",
            "signed": True,
            "clinicians": [],
            "lines": [2, 3],
            **fields,
        }
    )


def extraction(*events: ServiceEvent, is_copy_or_retransmission: bool = False) -> DocumentExtraction:
    return DocumentExtraction(
        document_id="D1",
        patient_name=None,
        patient_dob=None,
        patient_mrn=None,
        document_type="attendance roster",
        authored_at=None,
        received_at=None,
        is_copy_or_retransmission=is_copy_or_retransmission,
        copy_lines=[],
        final_or_signed=True,
        service_events=list(events),
        corrections=[],
        plan_requirements=[],
        assessments=[],
        authorizations=[],
        charges=[],
        observations=[],
    )


def tool_use(*events: ServiceEvent, tool_id: str = "toolu_01") -> dict:
    """What the API returns when the model calls record_extraction: the extraction as plain JSON that the schema accepts."""
    return {"type": "tool_use", "id": tool_id, "name": "record_extraction", "input": extraction(*events).model_dump(mode="json")}


# 11:30 sits on line 1 only, and this event cites lines 2-3, so the referee must not accept it.
TOOL_USE_WITH_DEPARTURE_1130 = tool_use(event(patient_departure="11:30"))
TOOL_USE_WITH_DEPARTURE_1115 = tool_use(event(patient_departure="11:15"), tool_id="toolu_02")
INVALID_TOOL_USE = {"type": "tool_use", "id": "toolu_03", "name": "record_extraction", "input": {"patient_name": "Rowan"}}


def reply(*content: dict, stop_reason: str = "tool_use", usage: dict = USAGE) -> dict:
    return {"content": list(content), "usage": usage, "stop_reason": stop_reason}


CUT_OFF = reply({"type": "tool_use", "id": "toolu_00", "name": "record_extraction", "input": {}}, stop_reason="max_tokens")
NO_TOOL_CALL = reply({"type": "text", "text": "Here is a summary."}, stop_reason="end_turn")


def scripted(*replies: dict):
    """A fake for llm.call_anthropic: answers from the list in order and records what each call received."""
    calls = []

    def fake_call(model, system, messages, tools, max_tokens=3000):
        assert len(calls) < len(replies), "the extractor made a model call that the test did not script"
        calls.append({"model": model, "system": system, "messages": list(messages), "tools": tools, "max_tokens": max_tokens})
        return replies[len(calls) - 1]

    return fake_call, calls


def budget_in(directory, claude: float = 1.0, jev_cap: float = 0.5) -> llm.Budget:
    return llm.Budget(directory / "spend.json", {"claude": claude, "jev": jev_cap})


def extract_with(tmp_path, *replies: dict, claude: float = 1.0):
    """Run extract_document against scripted replies; returns (result tuple, calls, budget)."""
    fake_call, calls = scripted(*replies)
    budget = budget_in(tmp_path, claude=claude)
    return extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget, call=fake_call), calls, budget


# ---- prices and budget ----


def test_usage_cost_uses_list_prices():
    assert llm.usage_cost("claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 1000}) == pytest.approx(0.012)
    assert llm.usage_cost("claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 1000}) == pytest.approx(0.0006)
    assert llm.usage_cost("claude-sonnet-5-5", {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.10)
    assert llm.usage_cost("claude-sonnet-5-5", {"cache_creation_input_tokens": 1_000_000}) == pytest.approx(2.50)
    assert llm.usage_cost("claude-haiku-5-5", {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.01)


def test_usage_cost_refuses_a_model_without_a_price():
    with pytest.raises(KeyError, match="claude-unpriced-9"):  # a model billed at zero would never reach the budget cap
        llm.usage_cost("claude-unpriced-9", {"input_tokens": 1})


def test_budget_stops_at_the_cap(tmp_path):
    budget = llm.Budget(tmp_path / "spend.json", {"claude": 1.0, "jev": 0.5})
    budget.charge("claude", 0.6, "a")
    budget.charge("claude", 0.3, "b")
    with pytest.raises(llm.BudgetExceeded):
        budget.charge("claude", 0.2, "c")
    # The call that crossed the cap already cost money, so the ledger records it before the batch stops.
    assert llm.Budget(tmp_path / "spend.json", {"claude": 1.0, "jev": 0.5}).spent("claude") == pytest.approx(1.1)


def test_budget_ledger_lists_every_account_with_its_entries(tmp_path):
    budget_in(tmp_path).charge("claude", 0.25, "extract D1")
    ledger = json.loads((tmp_path / "spend.json").read_text())
    assert ledger["claude"]["total_usd"] == pytest.approx(0.25)
    assert [(entry["usd"], entry["note"]) for entry in ledger["claude"]["entries"]] == [(0.25, "extract D1")]
    assert ledger["jev"] == {"total_usd": 0.0, "entries": []}


def test_budget_creates_the_ledger_directory_and_keeps_every_charge_from_threads(tmp_path):
    budget = llm.Budget(tmp_path / "outputs" / "logs" / "spend.json", {"claude": 100.0, "jev": 0.5})
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda number: budget.charge("claude", 0.01, f"call {number}"), range(200)))
    assert budget.spent("claude") == pytest.approx(2.0)
    assert len(json.loads(budget.ledger.read_text())["claude"]["entries"]) == 200


def test_load_env_file_sets_only_missing_variables(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"TEST_KEPT": "from-shell"})
    keys = tmp_path / "keys.env"
    lines = ["# a comment", "# TEST_COMMENTED=never", "", "TEST_NEW=plain-value", 'TEST_DOUBLE="two words"', "TEST_SINGLE='one'"]
    keys.write_text("\n".join([*lines, "export TEST_EXPORTED=exported-value", "TEST_KEPT=from-file"]))
    llm.load_env_file(keys)
    assert os.environ == {
        "TEST_KEPT": "from-shell",
        "TEST_NEW": "plain-value",
        "TEST_DOUBLE": "two words",
        "TEST_SINGLE": "one",
        "TEST_EXPORTED": "exported-value",
    }
    llm.load_env_file(tmp_path / "missing.env")  # a missing file is not an error
    assert len(os.environ) == 5


# Every schema field is required, so a null the model wrote must come back as a null, also inside nested objects.
SDK_INPUT = {"patient_name": "Rowan", "patient_dob": None, "service_events": [{"encounter_id": None, "lines": [2]}]}


def test_call_anthropic_caches_the_system_prompt_and_returns_plain_data(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [
                    {"type": "text", "text": "Recording."},
                    {"type": "tool_use", "id": "toolu_01", "name": "record_extraction", "input": SDK_INPUT},
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "cache_creation_input_tokens": 800,
                    "cache_read_input_tokens": 0,
                },
            },
        )

    sdk = anthropic.Anthropic(
        api_key="not-a-real-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "_client", lambda: sdk)
    tool = {
        "name": "record_extraction",
        "description": "Record the extraction.",
        "input_schema": {"type": "object", "properties": {}},
    }
    messages = [{"role": "user", "content": "hello"}]

    result = REAL_CALL_ANTHROPIC("claude-haiku-5-5", "SYSTEM TEXT", messages, [tool])
    assert seen[0]["max_tokens"] == 3000
    assert (seen[0]["model"], seen[0]["messages"]) == ("claude-haiku-5-5", messages)
    assert seen[0]["system"] == [{"type": "text", "text": "SYSTEM TEXT", "cache_control": {"type": "ephemeral"}}]
    assert seen[0]["tools"] == [tool] and seen[0]["tool_choice"] == {"type": "auto"} and "strict" not in seen[0]["tools"][0]
    assert result == {
        "content": [
            {"type": "text", "text": "Recording."},
            {"type": "tool_use", "id": "toolu_01", "name": "record_extraction", "input": SDK_INPUT},
        ],
        "usage": {"input_tokens": 120, "output_tokens": 45, "cache_creation_input_tokens": 800, "cache_read_input_tokens": 0},
        "stop_reason": "tool_use",
    }
    json.dumps(result)  # plain data: it goes back into the next request and into the extraction store

    REAL_CALL_ANTHROPIC("claude-haiku-5-5", "SYSTEM TEXT", messages, [], max_tokens=8000)
    assert seen[1]["max_tokens"] == 8000
    assert "tools" not in seen[1] and "tool_choice" not in seen[1]  # the API rejects tool_choice without tools


# ---- extraction ----


def test_extract_validates_admits_and_repairs_once(tmp_path):
    calls = []

    def fake_call(model, system, messages, tools, max_tokens=3000):
        calls.append(messages)
        bad_then_good = [TOOL_USE_WITH_DEPARTURE_1130, TOOL_USE_WITH_DEPARTURE_1115]
        return {
            "content": [bad_then_good[min(len(calls) - 1, 1)]],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "tool_use",
        }

    budget = llm.Budget(tmp_path / "spend.json", {"claude": 1.0, "jev": 0.5})
    result, problems, usage = extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget, call=fake_call)
    assert result.service_events[0].patient_departure == "11:15" and problems == []
    assert usage["calls"] == 2 and usage["repair_turns"] == 1 and "repair" in str(calls[1][-1]).lower()


def test_extract_sends_numbered_lines_the_prompt_and_the_record_tool(tmp_path):
    _, calls, _ = extract_with(tmp_path, reply(TOOL_USE_WITH_DEPARTURE_1115))
    first = calls[0]
    assert first["model"] == "claude-haiku-5-5" and first["system"] == extract.SYSTEM_PROMPT and first["max_tokens"] == 3000
    assert first["messages"] == [{"role": "user", "content": DOC_AS_SENT}]
    [tool] = first["tools"]
    assert tool["name"] == "record_extraction" and tool["input_schema"] == DocumentExtraction.model_json_schema()
    assert "strict" not in tool  # the schema is too large for strict structured output


def test_the_repair_turn_names_each_problem_and_answers_the_tool_call(tmp_path):
    _, calls, _ = extract_with(tmp_path, reply(TOOL_USE_WITH_DEPARTURE_1130), reply(TOOL_USE_WITH_DEPARTURE_1115))
    sent = calls[1]["messages"]
    assert [message["role"] for message in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [TOOL_USE_WITH_DEPARTURE_1130]  # the API needs the assistant turn exactly as it was returned
    [answer] = sent[2]["content"]
    assert answer["type"] == "tool_result" and answer["tool_use_id"] == "toolu_01" and answer["is_error"] is True
    assert "service_events[0]" in answer["content"] and "patient_departure" in answer["content"]
    assert "11:30 not in cited lines 2-3" in answer["content"]


def test_extract_reports_problems_the_repair_turn_did_not_fix(tmp_path):
    (result, problems, usage), calls, _ = extract_with(
        tmp_path, reply(TOOL_USE_WITH_DEPARTURE_1130), reply(TOOL_USE_WITH_DEPARTURE_1130)
    )
    assert len(calls) == 2  # one repair turn, never a second
    assert result.service_events[0].patient_departure is None and result.service_events[0].patient_arrival == "10:00"
    assert problems == [Problem("D1", "service_events[0]", "patient_departure", "11:30 not in cited lines 2-3")]
    assert usage["calls"] == 2 and usage["repair_turns"] == 1 and usage["unrepaired_problems"] == 1


def test_extract_keeps_the_first_admitted_result_when_the_repair_reply_is_unusable(tmp_path):
    (result, problems, usage), _, _ = extract_with(tmp_path, reply(TOOL_USE_WITH_DEPARTURE_1130), reply(INVALID_TOOL_USE))
    assert result.service_events[0].patient_departure is None and len(problems) == 1
    assert usage["calls"] == 2 and usage["repair_turns"] == 1 and usage["unrepaired_problems"] == 1


def test_extract_answers_every_tool_call_of_a_reply(tmp_path):
    two_calls = reply(INVALID_TOOL_USE, {**INVALID_TOOL_USE, "id": "toolu_04"})
    _, calls, _ = extract_with(tmp_path, two_calls, reply(TOOL_USE_WITH_DEPARTURE_1115))
    answered = [result["tool_use_id"] for result in calls[1]["messages"][-1]["content"]]
    assert answered == ["toolu_03", "toolu_04"]  # the API refuses a turn that leaves a tool call unanswered


def test_extract_sums_all_four_token_counts_and_their_cost(tmp_path):
    first = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 0}
    second = {"input_tokens": 12, "output_tokens": 6, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100}
    (_, _, usage), _, budget = extract_with(
        tmp_path, reply(TOOL_USE_WITH_DEPARTURE_1130, usage=first), reply(TOOL_USE_WITH_DEPARTURE_1115, usage=second)
    )
    assert {
        key: usage[key] for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    } == {
        "input_tokens": 22,
        "output_tokens": 11,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 100,
    }
    # per million tokens on claude-haiku-5-5: 22 * 0.10 + 11 * 0.50 + 100 * 0.125 + 100 * 0.01 = 21.2
    assert usage["cost_usd"] == pytest.approx(2.12e-5)
    assert budget.spent("claude") == pytest.approx(2.12e-5)


def test_extract_asks_again_with_a_larger_cap_when_the_reply_is_cut_off(tmp_path):
    (_, problems, usage), calls, budget = extract_with(tmp_path, CUT_OFF, reply(TOOL_USE_WITH_DEPARTURE_1115))
    assert [call["max_tokens"] for call in calls] == [3000, 8000]
    assert calls[1]["messages"] == calls[0]["messages"]  # the cut-off reply is not part of the conversation
    assert problems == [] and usage["calls"] == 2 and usage["repair_turns"] == 0
    assert usage["cost_usd"] == pytest.approx(7e-6) and budget.spent("claude") == pytest.approx(7e-6)  # both calls were paid for


def test_extract_gives_up_when_the_reply_is_cut_off_at_the_larger_cap(tmp_path):
    fake_call, calls = scripted(CUT_OFF, CUT_OFF)
    with pytest.raises(extract.ExtractionError, match="cut off"):
        extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget_in(tmp_path), call=fake_call)
    assert [call["max_tokens"] for call in calls] == [3000, 8000]


def test_extract_returns_schema_errors_to_the_model_and_accepts_the_third_attempt(tmp_path):
    (_, problems, usage), calls, _ = extract_with(
        tmp_path, reply(INVALID_TOOL_USE), NO_TOOL_CALL, reply(TOOL_USE_WITH_DEPARTURE_1115)
    )
    assert problems == [] and usage["calls"] == 3 and usage["repair_turns"] == 0
    [error] = calls[1]["messages"][-1]["content"]
    assert error["type"] == "tool_result" and error["tool_use_id"] == "toolu_03" and error["is_error"] is True
    assert "service_events" in error["content"]  # the schema error names the field the model left out
    nudge = calls[2]["messages"][-1]  # a reply with no tool call is told to call the tool
    assert nudge["role"] == "user" and "record_extraction" in nudge["content"]


def test_extract_stops_after_three_invalid_attempts(tmp_path):
    fake_call, calls = scripted(reply(INVALID_TOOL_USE), reply(INVALID_TOOL_USE), reply(INVALID_TOOL_USE))
    with pytest.raises(extract.ExtractionError, match="3 attempts"):
        extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget_in(tmp_path), call=fake_call)
    assert len(calls) == 3


def test_extract_stops_at_once_when_the_model_refuses(tmp_path):
    fake_call, calls = scripted(reply({"type": "text", "text": "I cannot help."}, stop_reason="refusal"))
    with pytest.raises(extract.ExtractionError, match="refus"):
        extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget_in(tmp_path), call=fake_call)
    assert len(calls) == 1


def test_extract_charges_each_call_and_stops_when_the_cap_is_reached(tmp_path):
    expensive = {"input_tokens": 1_000_000, "output_tokens": 0}  # 0.10 USD on claude-haiku-5-5
    fake_call, calls = scripted(
        reply(TOOL_USE_WITH_DEPARTURE_1130, usage=expensive), reply(TOOL_USE_WITH_DEPARTURE_1115, usage=expensive), NO_TOOL_CALL
    )
    budget = budget_in(tmp_path, claude=0.15)
    with pytest.raises(llm.BudgetExceeded):
        extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget, call=fake_call)
    assert len(calls) == 2 and budget.spent("claude") == pytest.approx(
        0.20
    )  # the second call crossed the cap: its cost is recorded, then the batch stops and nothing follows it


def test_extract_refuses_an_unpriced_model_before_it_spends(tmp_path):
    fake_call, calls = scripted(reply(TOOL_USE_WITH_DEPARTURE_1115))
    with pytest.raises(KeyError, match="claude-unpriced-9"):
        extract.extract_document("D1", DOC_TEXT, "claude-unpriced-9", budget_in(tmp_path), call=fake_call)
    assert calls == []  # a call that the ledger cannot bill must never happen


def test_extract_resolves_the_default_call_at_run_time(tmp_path, monkeypatch):
    def reached_the_sdk():
        raise AssertionError("the real Anthropic client was reached")

    monkeypatch.setattr(llm, "_client", reached_the_sdk)  # if the default were bound at import, the real function would run
    with pytest.raises(RuntimeError, match="must not call a model"):  # conftest's replacement of llm.call_anthropic
        extract.extract_document("D1", DOC_TEXT, "claude-haiku-5-5", budget_in(tmp_path))


def test_extractor_version_follows_the_model_the_prompt_and_the_tool(monkeypatch):
    sonnet, haiku = extract.extractor_version("claude-sonnet-5-5"), extract.extractor_version("claude-haiku-5-5")
    assert len(sonnet) == 12 and int(sonnet, 16) >= 0 and sonnet != haiku
    assert extract.extractor_version("claude-sonnet-5-5") == sonnet
    monkeypatch.setitem(extract.EXTRACTOR_VERSION_INPUTS, "system_prompt", extract.SYSTEM_PROMPT + " More.")
    changed_prompt = extract.extractor_version("claude-sonnet-5-5")
    assert changed_prompt != sonnet
    monkeypatch.setitem(extract.EXTRACTOR_VERSION_INPUTS, "tool", {**extract.RECORD_TOOL, "description": "Changed."})
    assert extract.extractor_version("claude-sonnet-5-5") not in (sonnet, changed_prompt)


# ---- Jev ----

CHOICE = jev.Choice("Which quantity?", {"sessions": "count", "other": "none"})
JEV_REPLY = {
    "model": "jev-1.13.0",
    "answers": {"metric": {"type": "choice", "choice": "sessions", "confidence": 1.0, "probabilities": {"sessions": 1.0}}},
    "usage": {"input_tokens": 100, "output_tokens": 10},
}
JEV_COST = 100 * 0.042 / 1e6


def jev_client(directory, transport=None, jev_cap: float = 0.5):
    return jev.Jev("test-key", budget_in(directory, jev_cap=jev_cap), directory / "jev-cache.json", transport=transport)


def flaky(statuses: list[int]):
    """A Jev transport that answers each request with the next status; 200 carries JEV_REPLY."""
    seen = []

    def handler(request):
        seen.append(request)
        status = statuses[len(seen) - 1]
        return httpx.Response(status, json=JEV_REPLY if status == 200 else {"error": "busy"})

    return httpx.MockTransport(handler), seen


def test_jev_request_shape_cache_and_budget(tmp_path):
    seen, requests = [], []

    def handler(request):
        requests.append(request)
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "metric": {"type": "choice", "choice": "sessions", "confidence": 1.0, "probabilities": {"sessions": 1.0}}
                },
                "usage": {"input_tokens": 574, "output_tokens": 142},
            },
        )

    client = jev.Jev(
        "k",
        llm.Budget(tmp_path / "s.json", {"claude": 1, "jev": 0.5}),
        tmp_path / "jev.json",
        transport=httpx.MockTransport(handler),
    )
    answer = client.ask(
        "How many sessions did Rowan miss?", {"metric": jev.Choice("Which quantity?", {"sessions": "count", "other": "none"})}
    )
    assert answer["metric"] == jev.ChoiceAnswer("sessions", 1.0, {"sessions": 1.0})
    assert seen[0]["questions"]["metric"] == {
        "type": "choice",
        "instructions": "Which quantity?",
        "criteria": {"sessions": "count", "other": "none"},
    }
    assert (seen[0]["state"], seen[0]["model"]) == ("How many sessions did Rowan miss?", "jev-latest")
    assert str(requests[0].url) == "https://api.typesafe.ai/v1/systemone" and requests[0].method == "POST"
    assert requests[0].headers["Authorization"] == "Bearer k"
    client.ask(
        "How many sessions did Rowan miss?", {"metric": jev.Choice("Which quantity?", {"sessions": "count", "other": "none"})}
    )
    assert len(seen) == 1 and client.budget.spent("jev") == pytest.approx(574 * 0.042 / 1e6)


def test_jev_retries_429_and_529_with_exponential_backoff(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr(jev.time, "sleep", delays.append)
    transport, seen = flaky([429, 529, 200])
    client = jev_client(tmp_path, transport)
    assert client.ask("state", {"metric": CHOICE})["metric"].choice == "sessions"
    assert len(seen) == 3 and delays == [1, 2]
    assert client.budget.spent("jev") == pytest.approx(JEV_COST)  # charged once, for the answer


def test_jev_gives_up_after_three_attempts_without_charging_or_caching(tmp_path, monkeypatch):
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: None)
    transport, seen = flaky([429, 429, 429, 200])
    client = jev_client(tmp_path, transport)
    with pytest.raises(httpx.HTTPStatusError):
        client.ask("state", {"metric": CHOICE})
    assert len(seen) == 3 and client.budget.spent("jev") == 0
    assert (
        client.ask("state", {"metric": CHOICE})["metric"].choice == "sessions" and len(seen) == 4
    )  # the failure left no cache entry


def test_jev_does_not_retry_other_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: pytest.fail("a 401 must not wait and retry"))
    transport, seen = flaky([401])
    with pytest.raises(httpx.HTTPStatusError):
        jev_client(tmp_path, transport).ask("state", {"metric": CHOICE})
    assert len(seen) == 1


def test_jev_cache_key_covers_the_state_the_questions_and_the_model(tmp_path):
    transport, seen = flaky([200] * 5)
    client = jev_client(tmp_path, transport)
    client.ask("state", {"metric": CHOICE})
    client.ask("other state", {"metric": CHOICE})
    client.ask("state", {"metric": jev.Choice("Another question?", CHOICE.criteria)})
    client.ask("state", {"metric": jev.Choice(CHOICE.instructions, {"sessions": "a count"})})
    other_model = jev.Jev("test-key", client.budget, tmp_path / "jev-cache.json", model="jev-1.13.0", transport=transport)
    other_model.ask("state", {"metric": CHOICE})
    assert len(seen) == 5  # each of these differs from the first request, so none came from the cache
    client.ask("state", {"metric": CHOICE})
    assert len(seen) == 5  # the first request again does


def test_jev_cache_survives_a_new_client(tmp_path):
    transport, seen = flaky([200])
    jev_client(tmp_path, transport).ask("state", {"metric": CHOICE})
    assert jev_client(tmp_path, transport).ask("state", {"metric": CHOICE})["metric"].choice == "sessions"
    assert len(seen) == 1  # the second client read the answer from the cache file


def test_jev_stops_at_its_budget_cap(tmp_path):
    transport, _ = flaky([200])
    with pytest.raises(llm.BudgetExceeded):
        jev_client(tmp_path, transport, jev_cap=1e-6).ask("state", {"metric": CHOICE})  # one answer costs 4.2e-06


def test_jev_builds_its_transport_at_call_time(tmp_path):
    client = jev_client(tmp_path)  # no transport: building the client must not reach for the network
    with pytest.raises(RuntimeError, match="must not call a model"):  # conftest's replacement of jev.default_transport
        client.ask("state", {"metric": CHOICE})


def test_jev_keeps_cache_and_ledger_whole_when_threads_ask_together(tmp_path):
    client = jev_client(tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json=JEV_REPLY)))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda number: client.ask(f"state {number}", {"metric": CHOICE}), range(80)))
    assert len(json.loads((tmp_path / "jev-cache.json").read_text())) == 80
    assert client.budget.spent("jev") == pytest.approx(80 * JEV_COST)


# ---- Jev as the second reader of the model's labels ----

ROSTER_TEXT = (
    "Document ID: D2\n"  # 1
    "HARBOR GROVE BEHAVIORAL HEALTH\n"  # 2
    "Attendance roster extract\n"  # 3
    "\n"  # 4 ends the header
    "Group encounter E1 | Service date: January 19, 2026\n"  # 5
    "Patient arrival: 10:00 | Patient departure: 11:15 | Status: Attended\n"  # 6
    "Parking reminder for all members.\n"  # 7
    "Group encounter E2 | Service date: January 20, 2026\n"  # 8
    "Status: No show\n"  # 9
)
HEADER = "Document ID: D2\nHARBOR GROVE BEHAVIORAL HEALTH\nAttendance roster extract\n"
STATE_E1 = (
    HEADER
    + "Group encounter E1 | Service date: January 19, 2026\nPatient arrival: 10:00 | Patient departure: 11:15 | Status: Attended"
)
STATE_E2 = HEADER + "Group encounter E2 | Service date: January 20, 2026\nStatus: No show"


def roster_extraction(first=None, second=None, **fields) -> DocumentExtraction:
    attended = first or event(encounter_id="E1", service_date="2026-01-19", lines=[5, 6])
    missed = second or event(
        encounter_id="E2",
        service_date="2026-01-20",
        attendance="no_show",
        patient_arrival=None,
        patient_departure=None,
        lines=[8, 9],
    )
    return extraction(attended, missed, **fields)


def jev_transport(answer_for):
    """A Jev transport whose answers come from answer_for(question_name, state) -> (choice, confidence)."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        answers = {}
        for name, question in body["questions"].items():
            choice, confidence = answer_for(name, body["state"])
            assert choice in question["criteria"], f"{choice} is not a choice of {name}"
            answers[name] = {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": {choice: confidence}}
        return httpx.Response(
            200, json={"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 300, "output_tokens": 60}}
        )

    return httpx.MockTransport(handler), seen


def jev_agrees(name, state):
    """The labels the model gave E1 and E2 in roster_extraction."""
    labels = {"record_role": "attendance_record", "service_type": "group_psychotherapy", "copy_or_draft": "original"}
    if name == "attendance":
        return ("no_show" if "No show" in state else "attended"), 1.0
    return labels[name], 1.0


def jev_says_no_show_for_e1(name, state, confidence=0.9):
    return ("no_show", confidence) if name == "attendance" and "Status: Attended" in state else jev_agrees(name, state)


def test_second_read_reports_disagreements(tmp_path):
    transport, _ = jev_transport(jev_says_no_show_for_e1)
    disputes = jev.second_read("D2", ROSTER_TEXT, roster_extraction(), jev_client(tmp_path / "disagree", transport))
    assert disputes == [
        {"encounter_id": "E1", "field": "attendance", "model_value": "attended", "jev_value": "no_show", "confidence": 0.9}
    ]
    transport, _ = jev_transport(jev_agrees)
    assert jev.second_read("D2", ROSTER_TEXT, roster_extraction(), jev_client(tmp_path / "agree", transport)) == []


@pytest.mark.parametrize("confidence, reported", [(0.89, 0), (0.9, 1)])
def test_second_read_ignores_disagreement_below_the_confidence_threshold(tmp_path, confidence, reported):
    transport, _ = jev_transport(lambda name, state: jev_says_no_show_for_e1(name, state, confidence))
    assert len(jev.second_read("D2", ROSTER_TEXT, roster_extraction(), jev_client(tmp_path, transport))) == reported


def test_second_read_compares_all_four_labels(tmp_path):
    other = {
        "record_role": "copy_of_another_record",
        "attendance": "patient_absent",
        "service_type": "individual_psychotherapy",
        "copy_or_draft": "copy_or_retransmission",
    }
    transport, _ = jev_transport(
        lambda name, state: (other[name], 0.95) if "Status: Attended" in state else jev_agrees(name, state)
    )
    disputes = jev.second_read("D2", ROSTER_TEXT, roster_extraction(), jev_client(tmp_path, transport))
    assert [(d["encounter_id"], d["field"], d["model_value"], d["jev_value"], d["confidence"]) for d in disputes] == [
        ("E1", "record_role", "attendance_record", "copy_of_another_record", 0.95),
        ("E1", "attendance", "attended", "patient_absent", 0.95),
        ("E1", "service_type", "group_psychotherapy", "individual_psychotherapy", 0.95),
        ("E1", "copy_or_draft", "original", "copy_or_retransmission", 0.95),
    ]


@pytest.mark.parametrize(
    "record_role, is_copy, label",
    [
        ("attendance_record", False, "original"),
        ("draft_or_template", False, "draft_or_template"),
        ("copy_of_another_record", False, "copy_or_retransmission"),
        ("attendance_record", True, "copy_or_retransmission"),
    ],
)
def test_second_read_turns_the_models_role_and_copy_flag_into_a_copy_or_draft_label(tmp_path, record_role, is_copy, label):
    other = "draft_or_template" if label == "original" else "original"  # Jev answers differently, so the dispute shows the label
    transport, _ = jev_transport(lambda name, state: (other, 1.0) if name == "copy_or_draft" else jev_agrees(name, state))
    document = roster_extraction(
        first=event(encounter_id="E1", lines=[5, 6], record_role=record_role), is_copy_or_retransmission=is_copy
    )
    disputes = jev.second_read("D2", ROSTER_TEXT, document, jev_client(tmp_path, transport))
    assert [
        (d["model_value"], d["jev_value"]) for d in disputes if d["field"] == "copy_or_draft" and d["encounter_id"] == "E1"
    ] == [(label, other)]


def test_second_read_sends_one_request_per_event_with_its_lines_the_header_and_the_schema_labels(tmp_path):
    transport, seen = jev_transport(jev_agrees)
    client = jev_client(tmp_path, transport)
    jev.second_read("D2", ROSTER_TEXT, roster_extraction(), client)
    assert [body["state"] for body in seen] == [STATE_E1, STATE_E2]  # not line 7, and not the other event's lines
    questions = seen[0]["questions"]
    assert list(questions) == ["record_role", "attendance", "service_type", "copy_or_draft"]
    assert {name: set(question["criteria"]) for name, question in questions.items()} == {
        "record_role": set(get_args(RecordRole)),
        "attendance": set(get_args(Attendance)),
        "service_type": set(get_args(ServiceType)),
        "copy_or_draft": {"original", "copy_or_retransmission", "draft_or_template"},
    }
    assert all(question["type"] == "choice" for question in questions.values())
    assert all("E1" in question["instructions"] and "2026-01-19" in question["instructions"] for question in questions.values())
    jev.second_read("D2", ROSTER_TEXT, roster_extraction(), client)
    assert len(seen) == 2  # the same events a second time come from the cache


def test_second_read_header_stops_at_the_first_blank_line_or_at_eight_lines(tmp_path):
    transport, seen = jev_transport(jev_agrees)
    twelve_lines = "\n".join(f"line {number}" for number in range(1, 13))
    jev.second_read("D3", twelve_lines, extraction(event(encounter_id="E9", lines=[12])), jev_client(tmp_path, transport))
    assert seen[0]["state"] == "\n".join([f"line {number}" for number in range(1, 9)] + ["line 12"])


def test_second_read_requests_do_not_depend_on_the_models_labels(tmp_path):
    transport, asked = jev_transport(jev_agrees)
    jev.second_read("D2", ROSTER_TEXT, roster_extraction(), jev_client(tmp_path / "first", transport))
    relabelled = event(
        encounter_id="E1",
        lines=[5, 6],
        attendance="no_show",
        service_type="individual_psychotherapy",
        record_role="clinical_note",
    )
    transport, asked_again = jev_transport(jev_agrees)
    jev.second_read("D2", ROSTER_TEXT, roster_extraction(first=relabelled), jev_client(tmp_path / "second", transport))
    assert asked == asked_again  # an independent reader never sees the labels it is checking
