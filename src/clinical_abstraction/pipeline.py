"""Ingest: register documents, extract what is missing, rebuild the patients whose fingerprint moved, and export.

Every step is idempotent. A document is its normalized text, so a duplicate file costs nothing and changes nothing. A failed
extraction is recorded and retried by the next run without blocking the other documents. A change to a patient's documents marks
the patient stale in the same transaction; ingest then rebuilds a stale patient when its fingerprint differs from the stored one
(decision D2.1), and never touches a patient nothing changed for (decision D3.1).
"""

import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from clinical_abstraction import extract, llm, reconcile, render, store
from clinical_abstraction.jev import Jev, second_read
from clinical_abstraction.reconcile import Doc
from clinical_abstraction.schema import DocumentExtraction
from clinical_abstraction.verify import Problem

Extractor = Callable[[str, str], tuple[DocumentExtraction, list[Problem], dict]]  # (doc_id, text) -> extraction, problems, usage

PRINTED_ID = re.compile(r"Document ID:\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
SUMMED = ("calls", "cost_usd", "repair_turns", "unrepaired_problems", *llm.USAGE_KEYS)


class RunLog:
    """One JSON line per event, flushed at once so a crashed run still shows how far it got."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a")

    def write(self, event: str, **fields) -> None:
        self._file.write(json.dumps({"at": store.now(), "event": event, **fields}) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def default_budget(out: Path) -> llm.Budget:
    """The spend ledger under out/logs, capped by MODEL_BUDGET_USD (default 10.0) and JEV_BUDGET_USD (default 0.50)."""
    caps = {"claude": float(os.environ.get("MODEL_BUDGET_USD", "10.0")), "jev": float(os.environ.get("JEV_BUDGET_USD", "0.50"))}
    return llm.Budget(Path(out) / "logs" / "spend.json", caps)


def register(connection, paths: list[Path], log: RunLog) -> tuple[list[str], int]:
    """Store each new document under the patient its text names. Returns the new shas and the count of files already known."""
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"no such file or folder: {', '.join(missing)}")  # a typo must not look like an empty ingest
    files = sorted({f for p in paths for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file() and not f.name.startswith(".")})
    new, known = [], 0
    for file in files:
        text = store.normalize_text(file.read_text(encoding="utf-8", errors="replace"))
        sha = store.content_sha(text)
        connection.execute("INSERT OR REPLACE INTO files (path, sha) VALUES (?, ?)", (str(file), sha))
        if connection.execute("SELECT 1 FROM documents WHERE sha = ?", (sha,)).fetchone():
            known += 1
            continue
        printed = PRINTED_ID.search(text)
        doc_id = printed.group(1) if printed else file.stem
        if connection.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone():
            doc_id = f"{doc_id}~{sha[:6]}"  # one id, two texts: keep both, and cite them apart
        patient = store.provisional_patient_id(text)
        connection.execute("INSERT INTO documents (sha, doc_id, patient_id, text) VALUES (?, ?, ?, ?)", (sha, doc_id, patient, text))
        if patient:
            store.mark_stale(connection, patient)
        new.append(sha)
        log.write("registered", file=str(file), doc=doc_id, sha=sha[:12], patient=patient, characters=len(text))
    connection.commit()
    return new, known


def confirmed_patient_id(extraction: DocumentExtraction, text: str) -> str | None:
    """The MRN the model read, only when the document prints it as a whole token. The model never gets to invent an id."""
    mrn = (extraction.patient_mrn or "").strip()
    return mrn if mrn and re.search(rf"(?<![\w-]){re.escape(mrn)}(?![\w-])", text) else None


def extract_missing(connection, version: str, model: str, workers: int, read: Extractor, log: RunLog):
    """Extract every document with no extraction row for this version. Returns the summary and why the batch stopped, if it did.

    A failure is recorded with its attempt count and the batch goes on; BudgetExceeded stops it. Workers only call the model;
    every database write happens here, in the calling thread, one transaction per document.
    """
    # ponytail: the anti-join walks the sha index of every document on each ingest (about tens of milliseconds at 60,000
    # documents). Keep a queue of pending documents instead if the collection passes about a million.
    shas = [
        row[0]
        for row in connection.execute(
            "SELECT d.sha FROM documents d WHERE NOT EXISTS (SELECT 1 FROM extractions e WHERE e.sha = d.sha AND e.version = ?)",
            (version,),
        )
    ]
    select = "SELECT sha, doc_id, patient_id, text FROM documents WHERE sha = ?"
    rows = sorted((dict(connection.execute(select, (sha,)).fetchone()) for sha in shas), key=lambda row: row["doc_id"])
    totals = {"documents": len(rows), "extracted": 0, "failed": 0, **dict.fromkeys(SUMMED, 0)}
    errors, stopped = Counter(), None

    def stop(error: Exception) -> None:
        nonlocal stopped
        stopped = stopped or str(error)
        for other in futures:
            other.cancel()

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(read, row["doc_id"], row["text"]): row for row in rows}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            row = futures[future]
            try:
                extraction, problems, usage = future.result()
            except Exception as error:  # noqa: BLE001 - one bad document must not stop the batch
                message = f"{type(error).__name__}: {error}"[:500]
                connection.execute(
                    "INSERT INTO failures (sha, version, error, attempts, last_at) VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT (sha, version) DO UPDATE SET error = excluded.error, attempts = attempts + 1, last_at = excluded.last_at",
                    (row["sha"], version, message, store.now()),
                )
                if row["patient_id"]:
                    store.mark_stale(connection, row["patient_id"])
                connection.commit()
                totals["failed"] += 1
                errors[message] += 1
                log.write("extraction_failed", doc=row["doc_id"], sha=row["sha"][:12], error=message)
                if isinstance(error, llm.BudgetExceeded):
                    stop(error)
                continue
            patient = confirmed_patient_id(extraction, row["text"]) or row["patient_id"]
            connection.execute(
                "INSERT OR REPLACE INTO extractions (sha, version, model, payload, usage, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["sha"], version, model, extraction.model_dump_json(), json.dumps(usage), store.now()),
            )
            connection.execute("DELETE FROM failures WHERE sha = ? AND version = ?", (row["sha"], version))
            connection.execute(
                "UPDATE documents SET patient_id = ?, authored_at = ?, received_at = ? WHERE sha = ?",
                (patient, extraction.authored_at, extraction.received_at, row["sha"]),
            )
            for mrn in {row["patient_id"], patient} - {None}:  # a document that moves patients changes both
                store.mark_stale(connection, mrn)
            connection.commit()
            totals["extracted"] += 1
            for key in SUMMED:
                totals[key] += usage.get(key) or 0
            log.write(
                "extracted",
                doc=row["doc_id"],
                sha=row["sha"][:12],
                patient=patient,
                **{key: usage.get(key) or 0 for key in SUMMED},
                problems=[asdict(problem) for problem in problems],
            )
            if patient != row["patient_id"]:
                log.write("patient_changed", doc=row["doc_id"], was=row["patient_id"], now=patient)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    totals["errors"] = [message for message, _ in errors.most_common(3)]
    return totals, stopped


def backfill_label_checks(connection, version: str, workers: int, jev: Jev, log: RunLog):
    """Second-read every extracted document that has no label check for this version, and mark its patient stale.

    A new document, a read that failed earlier, and a row deleted on purpose after a change to the dispute rule all look the
    same: an extraction with no label check. Jev caches its answers by request body, so re-reading after a rule change costs
    nothing. A failed read stores nothing, so the next run tries again. Returns the summary and why the batch stopped, if it did.
    """
    shas = [
        row[0]
        for row in connection.execute(
            "SELECT e.sha FROM extractions e WHERE e.version = ? "
            "AND NOT EXISTS (SELECT 1 FROM label_checks c WHERE c.sha = e.sha AND c.version = e.version)",
            (version,),
        )
    ]
    # ponytail: every missing document is loaded at once; read them in chunks if a backfill passes about 10,000 documents.
    select = (
        "SELECT d.sha, d.doc_id, d.patient_id, d.text, e.payload FROM documents d "
        "JOIN extractions e ON e.sha = d.sha AND e.version = ? WHERE d.sha = ?"
    )
    rows = sorted((dict(connection.execute(select, (version, sha)).fetchone()) for sha in shas), key=lambda row: row["doc_id"])
    totals, stopped = {"documents": len(rows), "stored": 0, "failed": 0}, None

    def read_labels(row: dict) -> list[dict]:  # runs in a worker thread and never touches the connection
        extraction = DocumentExtraction.model_validate_json(row["payload"])
        return getattr(jev, "second_read", second_read)(row["doc_id"], row["text"], extraction, jev)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(read_labels, row): row for row in rows}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            row = futures[future]
            try:
                disputes = future.result()
            except Exception as error:  # noqa: BLE001 - a Jev outage leaves a document unchecked, never unextracted
                totals["failed"] += 1
                log.write("second_read_failed", doc=row["doc_id"], sha=row["sha"][:12], error=f"{type(error).__name__}: {error}"[:500])
                if isinstance(error, llm.BudgetExceeded):
                    stopped = stopped or str(error)
                    for other in futures:
                        other.cancel()
                continue
            connection.execute(
                "INSERT OR REPLACE INTO label_checks (sha, version, payload) VALUES (?, ?, ?)", (row["sha"], version, json.dumps(disputes))
            )
            if row["patient_id"]:
                store.mark_stale(connection, row["patient_id"])
            connection.commit()
            totals["stored"] += 1
            log.write("label_checked", doc=row["doc_id"], sha=row["sha"][:12], disputes=len(disputes))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return totals, stopped


def load_patient_docs(connection, mrn: str, version: str) -> list[Doc]:
    """The documents of one patient that have an extraction for this version, with their label disputes. No other patient's rows."""
    rows = connection.execute(
        "SELECT d.sha, d.doc_id, d.text, d.authored_at, d.received_at, e.payload, c.payload AS disputes FROM documents d "
        "JOIN extractions e ON e.sha = d.sha AND e.version = ? "
        "LEFT JOIN label_checks c ON c.sha = d.sha AND c.version = e.version "
        "WHERE d.patient_id = ? ORDER BY d.doc_id",
        (version, mrn),
    )
    return [
        Doc(
            row["doc_id"],
            row["sha"],
            row["text"],
            DocumentExtraction.model_validate_json(row["payload"]),
            row["authored_at"],
            row["received_at"],
            json.loads(row["disputes"] or "[]"),
        )
        for row in rows
    ]


def export(out: Path, abstraction: dict) -> None:
    """Write out/abstraction/<MRN>.md and .json."""
    folder = Path(out) / "abstraction"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", abstraction["patient"]["mrn"])
    (folder / f"{name}.json").write_text(render.to_json(abstraction))
    (folder / f"{name}.md").write_text(render.markdown(abstraction))


def rebuild(connection, mrn: str, version: str, model: str, out: Path, log: RunLog, force: bool = False) -> bool:
    """Rebuild one patient when its fingerprint differs from the stored one (or when forced). Returns whether it was rebuilt."""
    if not connection.execute("SELECT 1 FROM documents WHERE patient_id = ? LIMIT 1", (mrn,)).fetchone():
        store.delete_patient(connection, mrn)  # every document of this patient moved to another one
        for suffix in ("json", "md"):
            (Path(out) / "abstraction" / f"{re.sub(r'[^\w.-]', '_', mrn)}.{suffix}").unlink(missing_ok=True)
        log.write("patient_removed", mrn=mrn)
        return False
    fingerprint, pending = store.fingerprint(connection, mrn, version)
    stored = connection.execute("SELECT fingerprint FROM patients WHERE mrn = ?", (mrn,)).fetchone()
    if stored and stored["fingerprint"] == fingerprint and not force:
        store.clear_stale(connection, mrn)
        connection.commit()
        return False
    docs = load_patient_docs(connection, mrn, version)
    abstraction = reconcile.build_patient(mrn, docs, pending)
    abstraction["built_with"] = {
        "model": model,
        "extractor_version": version,
        "documents_extracted": len(docs),
        "documents_pending": len(pending),
    }
    store.replace_patient(connection, mrn, abstraction, fingerprint)
    export(out, abstraction)
    log.write(
        "rebuilt",
        mrn=mrn,
        documents=len(docs),
        pending=len(pending),
        encounters=len(abstraction["encounters"]),
        flags=len(abstraction["flags"]),
    )
    return True


def ingest(
    paths: list[Path],
    db: Path,
    out: Path,
    model: str,
    workers: int = 8,
    rebuild_all: bool = False,
    extractor: Extractor | None = None,
    budget: llm.Budget | None = None,
    jev: Jev | None = None,
) -> dict:
    """Register the files, extract what is missing, rebuild the patients that changed, and export them. Returns a summary.

    `extractor(doc_id, text)` stands in for extract.extract_document in tests. `jev`, when given, second-reads the four
    counting labels of every extracted document that has no label check yet (see backfill_label_checks); the disputes are stored
    and are part of the patient's fingerprint, so a new or changed check rebuilds the patient.
    """
    started, out = time.perf_counter(), Path(out)
    budget = budget or default_budget(out)

    def read(doc_id: str, text: str):
        if extractor is not None:
            return extractor(doc_id, text)
        return extract.extract_document(doc_id, text, model, budget)  # looked up now, so a test's replacement applies

    version = extract.extractor_version(model)
    connection = store.connect(db)
    log = RunLog(out / "logs" / f"ingest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        new, known = register(connection, [Path(p) for p in paths], log)
        extraction, stopped = extract_missing(connection, version, model, workers, read, log)
        label_checks = {"documents": 0, "stored": 0, "failed": 0}
        if jev is not None:
            label_checks, label_stopped = backfill_label_checks(connection, version, workers, jev, log)
            stopped = stopped or label_stopped
        if rebuild_all:
            query = "SELECT patient_id FROM documents WHERE patient_id IS NOT NULL UNION SELECT mrn FROM patients"
            candidates = [row[0] for row in connection.execute(query)]
        else:
            candidates = store.stale_patients(connection)
        rebuilt = [mrn for mrn in sorted(candidates) if rebuild(connection, mrn, version, model, out, log, force=rebuild_all)]
        summary = {
            "new_documents": len(new),
            "duplicate_or_known_files": known,
            "extraction": extraction,
            "label_checks": label_checks,
            "rebuilt": rebuilt,
            "incomplete": {
                row["mrn"]: len(json.loads(row["incomplete"]))
                for row in connection.execute("SELECT mrn, incomplete FROM patients WHERE incomplete != '[]'")
            },
            "unassigned": [row[0] for row in connection.execute("SELECT doc_id FROM documents WHERE patient_id IS NULL ORDER BY doc_id")],
            "stopped": stopped,
            "total_seconds": round(time.perf_counter() - started, 3),
            "log": str(log.path),
        }
        log.write("ingest_finished", **summary)
        return summary
    finally:
        log.close()
        connection.close()
