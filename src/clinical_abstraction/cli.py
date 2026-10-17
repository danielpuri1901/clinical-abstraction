"""Command line: `abstraction ingest | report | source | ask | answer-questions | verify-answers`."""

import argparse
import json
import re
import sys
from pathlib import Path

from clinical_abstraction import answer, read, store

DEFAULT_OUT = Path("outputs")
DEFAULT_DB = DEFAULT_OUT / "abstraction.db"
DEFAULT_MODEL = "claude-sonnet-5-5"
CITATION = re.compile(r"([^\s,;]+) L(\d+)(?:-(\d+))?")
NO_JEV = "warning: JEV_API_KEY is not set, so keyword rules alone read each question, and every answer carries that warning."


def verified(verdict: str) -> bool:
    """A computed answer (with or without a warning), or an agent answer whose every number is bound."""
    return verdict.startswith(("COMPUTED", "AGENT: all"))


def answer_markdown(result: dict) -> str:
    return (
        f"# {result['id']}\n\n> {result['question']}\n\n{result['answer'].rstrip()}\n\n---\n\n"
        f"Verdict: {result['verdict']}\n\n{result['latency_seconds']} s; ${result['cost_usd']} model spend at list price.\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="abstraction", description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite store (default outputs/abstraction.db)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (default outputs/)")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="register documents, extract new ones, rebuild the patients that changed")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--model", default=DEFAULT_MODEL)
    ingest.add_argument("--workers", type=int, default=8)
    ingest.add_argument("--rebuild-all", action="store_true", help="rebuild every patient, not only the ones that changed")

    report = commands.add_parser("report", help="print a patient's saved abstraction, or list the patients (no model call)")
    report.add_argument("--patient", help="MRN; without it, one line per patient")

    source = commands.add_parser("source", help="print the source lines behind a citation such as 'BH-D103 L7-8'")
    source.add_argument("citation", nargs="+")

    ask = commands.add_parser("ask", help="answer one question from the saved abstraction")
    ask.add_argument("question")
    batch = commands.add_parser("answer-questions", help="answer each {id, question} item of a JSON file into <out>/answers/")
    batch.add_argument("questions", type=Path)
    for command in (ask, batch):
        command.add_argument("--agent", action="store_true", help="send the question to the open-question agent")
        command.add_argument("--model", default=DEFAULT_MODEL, help="model for the open-question agent")

    verify = commands.add_parser("verify-answers", help="replay every saved computed answer without a model and compare it")
    verify.add_argument("answers", type=Path, nargs="?", help="saved answers (default <out>/answers/answers.json)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        read.load_keys()
        budget = read.default_budget(args.out)
        client = read.jev_client(args.out, budget)
        summary = store.ingest(
            args.paths, args.db, args.out, args.model, args.workers, args.rebuild_all, budget=budget, jev=client
        )
        print(json.dumps(summary, indent=1))
        problems = summary["extraction"]["failed"] or summary["stopped"] or summary["unassigned"]
        if problems:
            print("ingest finished with problems: see failed, stopped, and unassigned in the summary.", file=sys.stderr)
        return 1 if problems else 0
    if not args.db.exists():
        print(f"No store at {args.db}. Run `abstraction ingest documents/` first.", file=sys.stderr)
        return 2
    connection = store.connect(args.db)
    if args.command == "report":
        if args.patient:
            abstraction = store.stored_abstraction(connection, args.patient)
            if abstraction is None:
                print(f"No patient {args.patient} in the store.", file=sys.stderr)
                return 1
            print(store.markdown(abstraction))
            return 0
        for row in connection.execute("SELECT mrn, name, incomplete FROM patients ORDER BY mrn"):
            pending = len(json.loads(row["incomplete"]))
            print(f"{row['mrn']}  {row['name'] or '-'}" + (f"  incomplete: {pending} documents pending" if pending else ""))
        return 0
    if args.command == "source":
        citations = CITATION.findall(" ".join(args.citation))
        if not citations:
            print("Give a citation such as 'BH-D103 L7' or 'BH-D103 L7-8'.", file=sys.stderr)
            return 2
        for doc_id, first, last in citations:
            lines, start, end = store.document_lines(connection, doc_id), int(first), int(last or first)
            if not lines:
                print(f"No document {doc_id} in the store.", file=sys.stderr)
                return 1
            if not 1 <= start <= end <= len(lines):
                print(f"{doc_id} L{first}{'-' + last if last else ''} is outside the document, which has {len(lines)} lines.", file=sys.stderr)
                return 1
            for number in range(start, end + 1):
                print(f"{doc_id} L{number}: {lines[number - 1]}")
        return 0
    if args.command == "verify-answers":
        results = answer.verify_saved(connection, args.answers or args.out / "answers" / "answers.json")
        if not results:
            print("No saved answers to verify.", file=sys.stderr)
            return 1
        for result in results:
            print(f"{result.get('id', result['question'])}: {result['verdict']}")
        return 0 if all(verified(result["verdict"]) for result in results) else 1
    read.load_keys()
    budget = read.default_budget(args.out)
    client = read.jev_client(args.out, budget)
    if client is None:
        print(NO_JEV, file=sys.stderr)
    if args.command == "ask":
        result = answer.answer(connection, args.question, client, budget, args.model, args.agent)
        print(f"{result['answer'].rstrip()}\n\nVerdict: {result['verdict']}")
        return 0 if verified(result["verdict"]) else 1
    folder = args.out / "answers"
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for item in json.loads(args.questions.read_text()):
        result = {"id": item["id"], **answer.answer(connection, item["question"], client, budget, args.model, args.agent)}
        (folder / f"{item['id']}.md").write_text(answer_markdown(result))
        print(f"{item['id']}: {result['verdict'][:100]} ({result['latency_seconds']} s, ${result['cost_usd']})")
        results.append(result)
    (folder / "answers.json").write_text(json.dumps(results, indent=1))
    return 0 if all(verified(result["verdict"]) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
