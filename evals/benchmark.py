"""Benchmarks. Every number is measured on this machine, and nothing here calls a model.

    uv run --frozen python -m evals.benchmark                 # stores of 1, 2,000, and 20,000 patients; writes outputs/benchmarks.json
    uv run --frozen python -m evals.benchmark --patients 50   # a quick run: stores of 1, 50, and 500 patients

The first patient of the store is the template. For each store size (1, --patients, and ten times --patients) a fresh temporary
store gets that many patients: the template, then copies with their own MRNs, names, and document ids. Each copy has its
documents, extractions, label checks, and abstraction, so the tables and indexes have the size of a real collection. That store is
timed on a one-document ingest (five new documents for one patient, each ingested alone, with an extractor that replays an
existing extraction), and, at 1 and --patients patients, on the five development questions and one collection question answered by
answers.answer with a decision model that always gives the expected plan. The answer sections are null until
clinical_abstraction.answers exists. A store of ten times --patients patients takes minutes and disk in proportion to the template;
a size that would not fit in the free disk space is skipped, and the row says so.
"""

import argparse
import importlib
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from clinical_abstraction import pipeline, read, store
from clinical_abstraction.schema import DocumentExtraction

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs"
QUESTIONS = ROOT / "evals" / "questions.json"
COLLECTION_QUESTION = "REVIEW-10"  # across the collection, which patients had two consecutive weeks below their goal
REPEAT = 5  # one-document ingests per store size
ANSWER_REPEAT = 3
TEMPLATE_DOCUMENTS = (
    "SELECT d.doc_id, d.text, d.authored_at, d.received_at, e.payload, e.usage, e.created_at, c.payload AS labels "
    "FROM documents d JOIN extractions e ON e.sha = d.sha AND e.version = :version "
    "LEFT JOIN label_checks c ON c.sha = d.sha AND c.version = :version WHERE d.patient_id = :mrn ORDER BY d.doc_id"
)
COUNTED_TABLES = (
    "documents",
    "extractions",
    "label_checks",
    "patients",
    "encounters",
    "weeks",
    "flags",
    "assessments",
    "observations",
)


def _shown(path: Path) -> str:
    """A path as written into the report: relative to the repository when it is inside, so a committed report names no machine."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _timed(function, repeat: int) -> dict:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        samples.append(time.perf_counter() - started)
    return {"median_seconds": round(statistics.median(samples), 4), "max_seconds": round(max(samples), 4), "runs": repeat}


def _ingest_runs(out: Path) -> list[dict]:
    """Every ingest run that left a log under out, with the totals its last event recorded."""
    runs = []
    for log in sorted(out.rglob("ingest-*.jsonl")):
        events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        finished = next((event for event in events if event["event"] == "ingest_finished"), None)
        if finished:
            keys = ("documents", "extracted", "failed", "calls", "cost_usd", "input_tokens", "output_tokens")
            runs.append(
                {
                    "log": str(log.relative_to(out)),
                    **{key: finished[key] for key in ("new_documents", "duplicate_or_known_files", "total_seconds")},
                    "extraction": {key: finished["extraction"].get(key) for key in keys},
                }
            )
    return runs


def _spend(out: Path) -> dict:
    """Spend per ledger under out, and in total. Each run folder keeps its own ledger, so the total is the sum."""
    ledgers = {}
    for path in sorted(out.rglob("spend.json")):
        saved = json.loads(path.read_text())
        ledgers[str(path.relative_to(out))] = {account: round(entry["total_usd"], 6) for account, entry in saved.items()}
    totals = {
        f"{account}_usd": round(sum(ledger.get(account, 0.0) for ledger in ledgers.values()), 6) for account in ("claude", "jev")
    }
    return {"ledgers": ledgers, **totals}


def _sizes(connection, db: Path) -> dict:
    rows = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COUNTED_TABLES}
    json_bytes = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(abstraction AS BLOB))), 0) FROM patients").fetchone()[0]
    return {
        "sqlite_bytes": db.stat().st_size,
        "rows": rows,
        "abstraction_json_bytes_per_patient": json_bytes // max(1, rows["patients"]),
    }


def _template(connection) -> dict:
    """The first patient of the store, with the rows of its documents, extractions, and label checks, and its abstraction."""
    patient = connection.execute("SELECT mrn, abstraction FROM patients ORDER BY mrn LIMIT 1").fetchone()
    if patient is None:
        raise ValueError("the store has no patient to copy")
    built = connection.execute(
        "SELECT e.version, e.model FROM extractions e JOIN documents d ON d.sha = e.sha WHERE d.patient_id = ? "
        "GROUP BY e.version, e.model ORDER BY COUNT(*) DESC",
        (patient["mrn"],),
    ).fetchone()
    if built is None:
        raise ValueError(f"patient {patient['mrn']} has no extracted documents to copy")
    rows = [dict(row) for row in connection.execute(TEMPLATE_DOCUMENTS, {"version": built["version"], "mrn": patient["mrn"]})]
    return {"mrn": patient["mrn"], "model": built["model"], "abstraction": json.loads(patient["abstraction"]), "documents": rows}


def _warm_rerun(db: Path, documents: Path, model: str, folder: Path) -> dict:
    """Ingest the original documents again into a copy of the store. Nothing is new, so nothing should reach the model."""
    calls = []

    def no_model(doc_id, text):
        calls.append(doc_id)
        raise RuntimeError("the benchmark makes no model call")

    budget = read.Budget(folder / "warm-spend.json", {"claude": 0.0, "jev": 0.0})
    started = time.perf_counter()
    summary = pipeline.ingest([documents], db, folder / "warm", model, extractor=no_model, budget=budget)
    seconds = round(time.perf_counter() - started, 4)
    return {
        "seconds": seconds,
        "model_calls": len(calls),
        "new_documents": summary["new_documents"],
        "patients_rebuilt": len(summary["rebuilt"]),
    }


def _build(db: Path, template: dict, count: int) -> None:
    """Fill a new store with `count` patients: the template, then copies with their own MRN, name, document ids, and shas."""
    mrn_of_template, version = template["mrn"], read.extractor_version(template["model"])  # the current version: all are read
    connection = store.connect(db)
    for number in range(count):
        mrn = mrn_of_template if number == 0 else f"BM-{number:05d}"
        footer = "" if number == 0 else f"Replica of {mrn_of_template}: {mrn}\n"  # so no copy shares a sha with another document
        documents, extractions, label_checks = [], [], []
        for row in template["documents"]:
            text = row["text"].replace(mrn_of_template, mrn) + footer
            sha = store.content_sha(text)
            doc_id = row["doc_id"] if number == 0 else f"{mrn}-{row['doc_id']}"
            documents.append((sha, doc_id, mrn, text, row["authored_at"], row["received_at"]))
            payload = row["payload"].replace(mrn_of_template, mrn)
            extractions.append((sha, version, template["model"], payload, row["usage"], row["created_at"]))
            if row["labels"] is not None:
                label_checks.append((sha, version, row["labels"]))
        connection.executemany(
            "INSERT INTO documents (sha, doc_id, patient_id, text, authored_at, received_at) VALUES (?, ?, ?, ?, ?, ?)", documents
        )
        connection.executemany(
            "INSERT INTO extractions (sha, version, model, payload, usage, created_at) VALUES (?, ?, ?, ?, ?, ?)", extractions
        )
        connection.executemany("INSERT INTO label_checks (sha, version, payload) VALUES (?, ?, ?)", label_checks)
        fingerprint, _ = store.fingerprint(connection, mrn, version)
        abstraction = template["abstraction"]
        if number:
            abstraction = {**abstraction, "patient": {**abstraction["patient"], "mrn": mrn, "name": f"Replica {number:05d}"}}
        store.replace_patient(connection, mrn, abstraction, fingerprint)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # so the file size is the size of the store
    connection.close()


def _one_document_ingest(db: Path, template: dict, count: int, folder: Path) -> dict:
    """Ingest new documents of one patient, one at a time, with no model. Each must rebuild that patient and no other."""
    mrn_of_template = template["mrn"]
    target = mrn_of_template if count == 1 else f"BM-{count // 2:05d}"
    donor = next((row for row in reversed(template["documents"]) if mrn_of_template in row["text"]), None)
    if donor is None:
        raise ValueError("no document of the template patient prints its MRN, so a new one could not be filed under a patient")
    extraction = DocumentExtraction.model_validate_json(donor["payload"].replace(mrn_of_template, target))
    budget = read.Budget(folder / "ingest-spend.json", {"claude": 0.0, "jev": 0.0})  # a cap of zero: a model call would raise
    samples = []
    for number in range(REPEAT):
        new = folder / f"new-{number}"
        new.mkdir()
        (new / f"benchmark-{number}.txt").write_text(
            donor["text"].replace(mrn_of_template, target) + f"Filed again for the benchmark: copy {number}.\n"
        )
        started = time.perf_counter()
        summary = pipeline.ingest(
            [new],
            db,
            folder / "ingest",
            template["model"],
            1,
            extractor=lambda doc_id, text: (extraction.model_copy(deep=True), [], {}),
            budget=budget,
        )
        samples.append(time.perf_counter() - started)
        if summary["new_documents"] != 1 or summary["rebuilt"] != [target]:
            raise RuntimeError(f"a one-document ingest should rebuild {target} only, but it rebuilt {summary['rebuilt']}")
    return {
        "median_seconds": round(statistics.median(samples), 4),
        "max_seconds": round(max(samples), 4),
        "runs": REPEAT,
        "patients_rebuilt": 1,
    }


def _answer_row(connection, answers, model: str, budget: read.Budget, item: dict, count: int) -> dict:
    """Latency of one question answered by code, with a decision model that always gives the plan questions.json expects."""
    from tests.fakes import FakeJev  # the fakes the tests share; nothing here calls a model

    jev, last = FakeJev.from_expected(item["expected"]), {}
    timing = _timed(lambda: last.update(answers.answer(connection, item["question"], jev, budget, model)), ANSWER_REPEAT)
    return {"id": item["id"], "patients": count, **timing, "answer_characters": len(last["answer"]), "verdict": last["verdict"]}


def _bytes_per_patient(template: dict) -> int:
    """A rough estimate, for the disk check only: the stored text twice over, to cover indexes and the derived tables."""
    stored = sum(len(row["text"]) + len(row["payload"]) + len(row["usage"] or "") for row in template["documents"])
    return 2 * (stored + len(json.dumps(template["abstraction"])))


def _scale(template: dict, patients: int, answers) -> tuple[list[dict], list[dict] | None, dict | None]:
    """Time a one-document ingest at each store size, and the answers where the answers module exists."""
    items = {item["id"]: item for item in json.loads(QUESTIONS.read_text())}
    development = [item for item_id, item in items.items() if item_id.startswith("DEV-")]
    rows, collection = [], None
    latency = None if answers is None else []
    for count in sorted({1, patients, patients * 10}):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            row = {"patients": count, "documents": count * len(template["documents"])}
            needed, free = count * _bytes_per_patient(template), shutil.disk_usage(folder).free
            if needed * 2 > free:
                rows.append({**row, "skipped": f"needs about {needed / 1e9:.1f} GB and only {free / 1e9:.1f} GB is free"})
                continue
            started = time.perf_counter()
            _build(folder / "scale.db", template, count)
            row |= {
                "build_seconds": round(time.perf_counter() - started, 2),
                "sqlite_bytes": (folder / "scale.db").stat().st_size,
            }
            if answers is not None and count in (1, patients):
                connection = store.connect(folder / "scale.db")
                budget = read.Budget(folder / "answer-spend.json", {"claude": 0.0, "jev": 0.0})
                latency += [_answer_row(connection, answers, template["model"], budget, item, count) for item in development]
                if count == patients:
                    collection = _answer_row(connection, answers, template["model"], budget, items[COLLECTION_QUESTION], count)
                connection.close()
            rows.append(row | _one_document_ingest(folder / "scale.db", template, count, folder))
    return rows, latency, collection


def run(patients: int = 2000, out: Path = OUT, db: Path | None = None, documents: Path | None = None) -> dict:
    """Measure everything and return the report. `db` defaults to out/abstraction.db and `documents` to the repository's folder."""
    out = Path(out)
    db = Path(db) if db else out / "abstraction.db"
    documents = Path(documents) if documents else ROOT / "documents"
    if patients < 1:
        raise ValueError("--patients must be at least 1")
    if not db.exists():
        raise FileNotFoundError(f"no store at {db}")
    try:
        answers = importlib.import_module("clinical_abstraction.answers")
    except ImportError:
        answers = None  # the answers module is built elsewhere: its two sections are null until it exists
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(shutil.copy(db, Path(temporary) / "source.db"))  # the store itself is never opened, so it never changes
        connection = store.connect(source)
        template, sizes = _template(connection), _sizes(connection, db)
        connection.close()
        warm = _warm_rerun(source, documents, template["model"], Path(temporary))
    scale, latency, collection = _scale(template, patients, answers)
    return {
        "generated_at": store.now(),
        "patients": patients,
        "store": _shown(db),
        "template_patient": template["mrn"],
        "ingest_runs": _ingest_runs(out),
        "warm_rerun": warm,
        "one_document_ingest": scale,
        "answer_latency": latency,
        "collection_question": collection,
        "sizes": sizes,
        "spend": _spend(out),
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--patients", type=int, default=2000, help="the large store size (default 2,000); the largest is ten times this"
    )
    parser.add_argument("--db", type=Path, help="the store to copy a patient from (default: abstraction.db in --out)")
    parser.add_argument("--documents", type=Path, help="the documents that built the store (default: documents/)")
    parser.add_argument(
        "--out", type=Path, default=OUT, help="where the logs and ledgers are read and benchmarks.json is written"
    )
    args = parser.parse_args(argv)
    report = run(args.patients, args.out, args.db, args.documents)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "benchmarks.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "ingest_runs"}, indent=1))
    return report


if __name__ == "__main__":
    main()
