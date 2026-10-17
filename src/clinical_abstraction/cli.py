"""Command line: `abstraction ingest | report | source`. Task 5 adds `ask`, `answer-questions`, and `verify-answers`."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from clinical_abstraction import llm, pipeline, render, store
from clinical_abstraction.jev import Jev

DEFAULT_OUT = Path("outputs")
DEFAULT_DB = DEFAULT_OUT / "abstraction.db"
DEFAULT_MODEL = "claude-sonnet-5-5"
KEY_FILES = ("anthropic.env", "jev.env")
CITATION = re.compile(r"([^\s,;]+) L(\d+)(?:-(\d+))?")


def load_keys() -> None:
    """Set missing keys from ~/.config/backbone-take-home. A key never leaves the environment: nothing here prints it."""
    for name in KEY_FILES:
        llm.load_env_file(Path.home() / ".config" / "backbone-take-home" / name)


def jev_client(out: Path, budget: llm.Budget) -> Jev | None:
    """A Jev client when JEV_API_KEY is set, else None (the pipeline then skips the second read)."""
    key = os.environ.get("JEV_API_KEY")
    return Jev(key, budget, Path(out) / "jev_cache.json") if key else None


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        load_keys()
        budget = pipeline.default_budget(args.out)
        client = jev_client(args.out, budget)
        summary = pipeline.ingest(
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
            row = connection.execute("SELECT abstraction FROM patients WHERE mrn = ?", (args.patient,)).fetchone()
            if row is None:
                print(f"No patient {args.patient} in the store.", file=sys.stderr)
                return 1
            print(render.markdown(json.loads(row["abstraction"])))
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
            row = connection.execute("SELECT text FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
            if row is None:
                print(f"No document {doc_id} in the store.", file=sys.stderr)
                return 1
            lines, start, end = row["text"].splitlines(), int(first), int(last or first)
            if not 1 <= start <= end <= len(lines):
                print(f"{doc_id} L{first}{'-' + last if last else ''} is outside the document, which has {len(lines)} lines.", file=sys.stderr)
                return 1
            for number in range(start, end + 1):
                print(f"{doc_id} L{number}: {lines[number - 1]}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
