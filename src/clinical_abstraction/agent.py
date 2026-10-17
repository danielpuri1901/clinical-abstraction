"""Read-only tools over the store, and the open-question agent that uses them.

A question whose plan has kind "open" has no executor, so Claude answers it through four read-only tools: search_source,
source_lines, encounters, and weeks. Code then checks the answer: every quantity must appear in some tool output, and every
citation must point at lines of a stored document. The verdict names what fails. answers.py reuses the encounter query and the
citation check.
"""

import json
import re
import time

from clinical_abstraction import read, store

MAX_MODEL_CALLS = 8
LISTED = 50  # patients named in the system prompt; the rest are counted
CITATION = re.compile(r"(?<![\w~.-])([A-Za-z][\w.~-]*\d[\w.~-]*)\s+L(\d+)(?:-(\d+))?(?![\w-])")

SYSTEM = """You answer a clinical reviewer's question about patients' therapy records. Read the records only through the tools.
1. Copy every number from a tool result. Never compute, count, or convert a number yourself: code flags every number in your
   answer that no tool result contains.
2. Cite the lines behind each statement in square brackets as the tools give them, for example [BH-D103 L7].
3. Say what the records do not settle, and which document would settle it. If the tools cannot answer the question, say so.
4. Start with a short direct answer, then the supporting detail. No preamble.
"""

_MRN = {"mrn": {"type": "string", "description": "Patient MRN, for example HG-M042"}}
_DAY = {"type": "string", "description": "YYYY-MM-DD, optional"}
TOOLS = [
    {
        "name": "search_source",
        "description": "Keyword search over one patient's source documents. Returns up to 20 matching lines, best first, with citations.",
        "input_schema": {
            "type": "object",
            "properties": {**_MRN, "query": {"type": "string", "description": "A few keywords"}},
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
            "properties": {**_MRN, "start_date": _DAY, "end_date": _DAY},
            "required": ["mrn"],
        },
    },
    {
        "name": "weeks",
        "description": "One row per treatment-plan week: therapy days and minutes as low and high bounds, the goal status "
        "(met, not_met, or cannot_determine), and why, including which document would settle an open week.",
        "input_schema": {"type": "object", "properties": _MRN, "required": ["mrn"]},
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


def _line_count(connection, doc_id: str) -> int:
    row = connection.execute("SELECT text FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return len(row[0].splitlines()) if row else 0


def source_lines(connection, citation: str) -> list[dict] | dict:
    found = CITATION.fullmatch(citation.strip())
    if not found:
        return {"error": "give a citation such as 'BH-D103 L7' or 'BH-D103 L7-9'"}
    doc_id, first, last = found[1], int(found[2]), int(found[3] or found[2])
    row = connection.execute("SELECT text FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    lines = row[0].splitlines() if row else []
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
            sizes[doc_id] = _line_count(connection, doc_id)
        if not 1 <= first <= last <= sizes[doc_id]:
            broken.append(found.group())
    return list(dict.fromkeys(broken))


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_NOT_QUANTITIES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"  # dates and timestamps: 2026-01-19, 2026-01-19T10:00
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"  # 1/19, 01/19/2026
    r"|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?"  # times: 09:00, 9:00 AM
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"  # January 19, 2026
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+\d{{4}})?"  # 19 January 2026
    rf"|\b{_MONTH}\s+\d{{4}}"  # January 2026
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
