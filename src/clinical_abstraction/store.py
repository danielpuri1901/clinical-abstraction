"""Persist, ingest, export: the SQLite store, the ingest pipeline that fills it, and the Markdown and JSON a reviewer reads.

Sections, in order:
1. The store: the schema, documents with their versioned extractions and label checks, and each patient's derived rows.
2. Ingest: register documents, extract what is missing, second-read their labels, and rebuild the patients that changed.
3. Exports: one patient's abstraction as Markdown for a reviewer and as JSON for code, and the table helpers answers share.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from clinical_abstraction import read, reconcile
from clinical_abstraction.reconcile import Doc
from clinical_abstraction.schema import DocumentExtraction

# 1. The store ==================================================================================================================
# Documents, extractions, label checks, and failures only grow. The patient tables are derived: replace_patient swaps one
# patient's rows in one transaction, so every patient can be rebuilt from the extractions without a model call.
# A patient's fingerprint names the documents and the extractor version its abstraction was built from (decision D2.1).

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    sha TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL UNIQUE,
    patient_id TEXT,
    text TEXT NOT NULL,
    authored_at TEXT,
    received_at TEXT
);
CREATE INDEX IF NOT EXISTS documents_patient ON documents(patient_id);
CREATE TABLE IF NOT EXISTS extractions (
    sha TEXT NOT NULL,
    version TEXT NOT NULL,
    model TEXT NOT NULL,
    payload TEXT NOT NULL,
    usage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (sha, version)
);
CREATE TABLE IF NOT EXISTS label_checks (sha TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (sha, version));
CREATE TABLE IF NOT EXISTS failures (
    sha TEXT NOT NULL,
    version TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (sha, version)
);
CREATE TABLE IF NOT EXISTS stale_patients (mrn TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS patients (
    mrn TEXT PRIMARY KEY,
    name TEXT,
    fingerprint TEXT NOT NULL,
    incomplete TEXT NOT NULL,
    built_at TEXT NOT NULL,
    abstraction TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS patients_incomplete ON patients(mrn) WHERE incomplete != '[]';
CREATE TABLE IF NOT EXISTS encounters (
    mrn TEXT NOT NULL,
    encounter TEXT NOT NULL,
    service_date TEXT NOT NULL,
    service_type TEXT NOT NULL,
    modality TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_as_therapy TEXT NOT NULL,
    minutes_low INTEGER NOT NULL,
    minutes_high INTEGER NOT NULL,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS encounters_patient_date ON encounters(mrn, service_date);
CREATE TABLE IF NOT EXISTS weeks (
    mrn TEXT NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    therapy_days_low INTEGER NOT NULL,
    therapy_days_high INTEGER NOT NULL,
    minutes_low INTEGER NOT NULL,
    minutes_high INTEGER NOT NULL,
    status TEXT NOT NULL,
    explanation TEXT NOT NULL,
    plan_version INTEGER,
    PRIMARY KEY (mrn, week_start)
);
CREATE TABLE IF NOT EXISTS flags (
    mrn TEXT NOT NULL,
    kind TEXT NOT NULL,
    encounter TEXT,
    text TEXT NOT NULL,
    citations TEXT NOT NULL,
    settles TEXT
);
CREATE INDEX IF NOT EXISTS flags_patient ON flags(mrn);
CREATE TABLE IF NOT EXISTS assessments (
    mrn TEXT NOT NULL,
    instrument TEXT NOT NULL,
    completed_on TEXT,
    total_score INTEGER,
    sources TEXT NOT NULL,
    copies TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assessments_patient ON assessments(mrn);
CREATE TABLE IF NOT EXISTS observations (
    mrn TEXT NOT NULL,
    category TEXT NOT NULL,
    observed_on TEXT,
    text TEXT NOT NULL,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_patient ON observations(mrn);
CREATE TABLE IF NOT EXISTS answers (
    question_key TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    plan TEXT NOT NULL,
    answer TEXT NOT NULL,
    verdict TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
PATIENT_TABLES = ("encounters", "weeks", "flags", "assessments", "observations")

# Labelled ids are explicit, so one to three letters before the dash is fine (the Task 3 fixture patient is T-M001).
LABELED_ID = re.compile(r"(?:MRN|Record\s*#|Medical record(?: number)?)[:\s#]*([A-Z]{1,3}-[A-Z]?\d{3,6})")
# An id with no label is a guess, so it must not look like another kind of id:
# D document, E encounter, V visit, A appointment, Q questionnaire, C charge.
UNLABELED_ID = re.compile(r"\b[A-Z]{2}-(?![DEVAQC]\d)[A-Z]?\d{4,6}\b")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_text(text: str) -> str:
    """A byte-order mark, line endings, and trailing spaces never change clinical meaning, so a resent copy hashes the same.

    Leading blank lines stay, so the line numbers a citation uses still match the file.
    """
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip("\n") + "\n"


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def provisional_patient_id(text: str) -> str | None:
    """The patient id a document prints, found by pattern. The extraction's own reading replaces it once the model has run."""
    labeled = LABELED_ID.search(text)
    if labeled:
        return labeled.group(1)
    unlabeled = UNLABELED_ID.search(text)
    return unlabeled.group(0) if unlabeled else None


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")  # safe with WAL: a crash can lose the last commit, never corrupt the file
    connection.executescript(SCHEMA)
    return connection


def fingerprint(connection: sqlite3.Connection, mrn: str, version: str) -> tuple[str, list[dict]]:
    """The hash of what a patient's abstraction is built from, and the documents still pending.

    The hash covers the sorted (sha, version) pairs that have an extraction row, each with its label check (the disputes shape
    the abstraction too), and the shas that have none. A document that arrives, is read, is read again under a new version, fails
    to be read, or gets a different label check each change it. A pending document is one with no extraction row for this
    version; its reason is the last failure, else "not extracted yet".
    """
    rows = connection.execute(
        "SELECT d.sha, d.doc_id, e.sha IS NOT NULL AS extracted, c.payload AS labels, f.error, f.attempts FROM documents d "
        "LEFT JOIN extractions e ON e.sha = d.sha AND e.version = :version "
        "LEFT JOIN label_checks c ON c.sha = d.sha AND c.version = :version "
        "LEFT JOIN failures f ON f.sha = d.sha AND f.version = :version "
        "WHERE d.patient_id = :mrn ORDER BY d.sha",
        {"version": version, "mrn": mrn},
    ).fetchall()
    extracted = [[row["sha"], version, row["labels"]] for row in rows if row["extracted"]]
    pending = [row for row in rows if not row["extracted"]]
    digest = hashlib.sha256(json.dumps([extracted, [row["sha"] for row in pending]]).encode()).hexdigest()
    listed = [
        {"doc_id": row["doc_id"], "sha": row["sha"], "reason": row["error"] or "not extracted yet", "attempts": row["attempts"] or 0}
        for row in pending
    ]
    return digest, sorted(listed, key=lambda item: item["doc_id"])


def mark_stale(connection: sqlite3.Connection, mrn: str) -> None:
    """Record, in the caller's transaction, that a patient's documents changed. Ingest rebuilds a marked patient if its fingerprint moved."""
    connection.execute("INSERT OR IGNORE INTO stale_patients (mrn) VALUES (?)", (mrn,))


def stale_patients(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT mrn FROM stale_patients ORDER BY mrn")]


def clear_stale(connection: sqlite3.Connection, mrn: str) -> None:
    connection.execute("DELETE FROM stale_patients WHERE mrn = ?", (mrn,))


def _clear_derived(connection: sqlite3.Connection, mrn: str) -> None:
    for table in PATIENT_TABLES:
        connection.execute(f"DELETE FROM {table} WHERE mrn = ?", (mrn,))


def delete_patient(connection: sqlite3.Connection, mrn: str) -> None:
    """Remove a patient who no longer has documents, because they all moved to another patient."""
    with connection:
        _clear_derived(connection, mrn)
        connection.execute("DELETE FROM patients WHERE mrn = ?", (mrn,))
        clear_stale(connection, mrn)


def replace_patient(connection: sqlite3.Connection, mrn: str, abstraction: dict, fingerprint: str) -> None:
    """Swap one patient's rows for a new build in one transaction, and clear the patient's stale mark."""
    with connection:
        _clear_derived(connection, mrn)
        connection.execute(
            "INSERT OR REPLACE INTO patients (mrn, name, fingerprint, incomplete, built_at, abstraction) VALUES (?, ?, ?, ?, ?, ?)",
            (mrn, abstraction["patient"]["name"], fingerprint, json.dumps(abstraction["incomplete"]), now(), json.dumps(abstraction)),
        )
        for table in PATIENT_TABLES:  # each table copies the abstraction's rows of the same name; a list is stored as JSON
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")][1:]  # the columns after mrn
            connection.executemany(
                f"INSERT INTO {table} (mrn, {', '.join(columns)}) VALUES ({', '.join('?' * (len(columns) + 1))})",
                [(mrn, *(_sql_value(row[column]) for column in columns)) for row in abstraction[table]],
            )
        clear_stale(connection, mrn)


def _sql_value(value):
    return json.dumps(value) if isinstance(value, list) else value


def known_patients(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """(MRN, name) of every built patient, for the question parser. A patient with no name on any document has ''."""
    return [(row["mrn"], row["name"] or "") for row in connection.execute("SELECT mrn, name FROM patients ORDER BY mrn")]


def episode_year(connection: sqlite3.Connection) -> int:
    """The year of the earliest service date in the store, else the current year, for dates written without a year."""
    earliest = connection.execute("SELECT MIN(service_date) FROM encounters").fetchone()[0]
    return int(earliest[:4]) if earliest else date.today().year


# 2. Ingest =====================================================================================================================
# Every step is idempotent. A document is its normalized text, so a duplicate file costs nothing and changes nothing. A failed
# extraction is recorded and retried by the next run without blocking the other documents. A change to a patient's documents marks
# the patient stale in the same transaction; ingest then rebuilds a stale patient when its fingerprint differs from the stored one
# (decision D2.1), and never touches a patient nothing changed for (decision D3.1).

Extractor = Callable[[str, str], read.Extracted]  # (doc_id, text) -> extraction, problems, usage

PRINTED_ID = re.compile(r"Document ID:\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
SUMMED = ("calls", "cost_usd", "repair_turns", "unrepaired_problems", *read.USAGE_KEYS)


class RunLog:
    """One JSON line per event, flushed at once so a crashed run still shows how far it got."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a")

    def write(self, event: str, **fields) -> None:
        self._file.write(json.dumps({"at": now(), "event": event, **fields}) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def register(connection, paths: list[Path], log: RunLog) -> tuple[list[str], int]:
    """Store each new document under the patient its text names. Returns the new shas and the count of files already known."""
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"no such file or folder: {', '.join(missing)}")  # a typo must not look like an empty ingest
    files = sorted({f for p in paths for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file() and not f.name.startswith(".")})
    new, known = [], 0
    for file in files:
        text = normalize_text(file.read_text(encoding="utf-8", errors="replace"))
        sha = content_sha(text)
        connection.execute("INSERT OR REPLACE INTO files (path, sha) VALUES (?, ?)", (str(file), sha))
        if connection.execute("SELECT 1 FROM documents WHERE sha = ?", (sha,)).fetchone():
            known += 1
            continue
        printed = PRINTED_ID.search(text)
        doc_id = printed.group(1) if printed else file.stem
        if connection.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone():
            doc_id = f"{doc_id}~{sha[:6]}"  # one id, two texts: keep both, and cite them apart
        patient = provisional_patient_id(text)
        connection.execute("INSERT INTO documents (sha, doc_id, patient_id, text) VALUES (?, ?, ?, ?)", (sha, doc_id, patient, text))
        if patient:
            mark_stale(connection, patient)
        new.append(sha)
        log.write("registered", file=str(file), doc=doc_id, sha=sha[:12], patient=patient, characters=len(text))
    connection.commit()
    return new, known


def confirmed_patient_id(extraction: DocumentExtraction, text: str) -> str | None:
    """The MRN the model read, only when the document prints it as a whole token. The model never gets to invent an id."""
    mrn = (extraction.patient_mrn or "").strip()
    return mrn if mrn and re.search(rf"(?<![\w-]){re.escape(mrn)}(?![\w-])", text) else None


def extract_missing(connection, version: str, model: str, workers: int, extractor: Extractor, log: RunLog):
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
        futures = {pool.submit(extractor, row["doc_id"], row["text"]): row for row in rows}
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
                    (row["sha"], version, message, now()),
                )
                if row["patient_id"]:
                    mark_stale(connection, row["patient_id"])
                connection.commit()
                totals["failed"] += 1
                errors[message] += 1
                log.write("extraction_failed", doc=row["doc_id"], sha=row["sha"][:12], error=message)
                if isinstance(error, read.BudgetExceeded):
                    stop(error)
                continue
            patient = confirmed_patient_id(extraction, row["text"]) or row["patient_id"]
            connection.execute(
                "INSERT OR REPLACE INTO extractions (sha, version, model, payload, usage, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["sha"], version, model, extraction.model_dump_json(), json.dumps(usage), now()),
            )
            connection.execute("DELETE FROM failures WHERE sha = ? AND version = ?", (row["sha"], version))
            connection.execute(
                "UPDATE documents SET patient_id = ?, authored_at = ?, received_at = ? WHERE sha = ?",
                (patient, extraction.authored_at, extraction.received_at, row["sha"]),
            )
            for mrn in {row["patient_id"], patient} - {None}:  # a document that moves patients changes both
                mark_stale(connection, mrn)
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


def backfill_label_checks(connection, version: str, workers: int, jev: read.Jev, log: RunLog):
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
        return getattr(jev, "second_read", read.second_read)(row["doc_id"], row["text"], extraction, jev)

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
                if isinstance(error, read.BudgetExceeded):
                    stopped = stopped or str(error)
                    for other in futures:
                        other.cancel()
                continue
            connection.execute(
                "INSERT OR REPLACE INTO label_checks (sha, version, payload) VALUES (?, ?, ?)", (row["sha"], version, json.dumps(disputes))
            )
            if row["patient_id"]:
                mark_stale(connection, row["patient_id"])
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
    (folder / f"{name}.json").write_text(to_json(abstraction))
    (folder / f"{name}.md").write_text(markdown(abstraction))


def rebuild(connection, mrn: str, version: str, model: str, out: Path, log: RunLog, force: bool = False) -> bool:
    """Rebuild one patient when its fingerprint differs from the stored one (or when forced). Returns whether it was rebuilt."""
    if not connection.execute("SELECT 1 FROM documents WHERE patient_id = ? LIMIT 1", (mrn,)).fetchone():
        delete_patient(connection, mrn)  # every document of this patient moved to another one
        for suffix in ("json", "md"):
            (Path(out) / "abstraction" / f"{re.sub(r'[^\w.-]', '_', mrn)}.{suffix}").unlink(missing_ok=True)
        log.write("patient_removed", mrn=mrn)
        return False
    digest, pending = fingerprint(connection, mrn, version)
    stored = connection.execute("SELECT fingerprint FROM patients WHERE mrn = ?", (mrn,)).fetchone()
    if stored and stored["fingerprint"] == digest and not force:
        clear_stale(connection, mrn)
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
    replace_patient(connection, mrn, abstraction, digest)
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
    budget: read.Budget | None = None,
    jev: read.Jev | None = None,
) -> dict:
    """Register the files, extract what is missing, rebuild the patients that changed, and export them. Returns a summary.

    `extractor(doc_id, text)` stands in for extract.extract_document in tests. `jev`, when given, second-reads the four
    counting labels of every extracted document that has no label check yet (see backfill_label_checks); the disputes are stored
    and are part of the patient's fingerprint, so a new or changed check rebuilds the patient.
    """
    started, out = time.perf_counter(), Path(out)
    budget = budget or read.default_budget(out)

    def extract(doc_id: str, text: str):
        if extractor is not None:
            return extractor(doc_id, text)
        return read.extract_document(doc_id, text, model, budget)  # looked up now, so a test's replacement applies

    version = read.extractor_version(model)
    connection = connect(db)
    log = RunLog(out / "logs" / f"ingest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        new, known = register(connection, [Path(p) for p in paths], log)
        extraction, stopped = extract_missing(connection, version, model, workers, extract, log)
        label_checks = {"documents": 0, "stored": 0, "failed": 0}
        if jev is not None:
            label_checks, label_stopped = backfill_label_checks(connection, version, workers, jev, log)
            stopped = stopped or label_stopped
        if rebuild_all:
            query = "SELECT patient_id FROM documents WHERE patient_id IS NOT NULL UNION SELECT mrn FROM patients"
            candidates = [row[0] for row in connection.execute(query)]
        else:
            candidates = stale_patients(connection)
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


# 3. Exports ====================================================================================================================
# Pure functions: no model, no database.


def to_json(abstraction: dict) -> str:
    return json.dumps(abstraction, indent=1) + "\n"


def _range(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def _cell(value) -> str:
    return "-" if value is None or value == "" else str(value).replace("|", "/").replace("\n", " ")


def _table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["None.", ""]
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows),
        "",
    ]


def _runs(runs: list[list[str]]) -> str:
    return "; ".join(" and ".join(run) for run in runs) or "none"


def markdown(abstraction: dict) -> str:
    patient, period, built = abstraction["patient"], abstraction["review_period"], abstraction.get("built_with")
    lines = [f"# Clinical abstraction: {patient['name'] or 'name not stated'} ({patient['mrn']})", ""]
    if period["start"]:
        lines.append(f"Review period: {period['start']} to {period['end']} (basis: {period['basis']}).")
    else:
        lines.append(f"Review period: not known ({period['basis']}).")
    if built:
        lines.append(
            f"Built from {built['documents_extracted']} documents; extractor model {built['model']}, version {built['extractor_version']}."
        )
    lines += ["Minutes are patient-present minutes. A range `low-high` means the records disagree and none of them settles it.", ""]

    pending = abstraction["incomplete"]
    if pending:
        noun = "document is" if len(pending) == 1 else "documents are"
        lines += ["## Incomplete", "", f"{len(pending)} {noun} not read yet, so no week is settled until it is read.", ""]
        lines += [f"- {item['doc_id']}: {item['reason']} (attempts: {item['attempts']})" for item in pending] + [""]

    lines += ["## Weeks", ""]
    weeks = [
        [
            f"{w['week_start']} to {w['week_end']}",
            _range(w["therapy_days_low"], w["therapy_days_high"]),
            _range(w["minutes_low"], w["minutes_high"]),
            w["status"],
            w["explanation"],
        ]
        for w in abstraction["weeks"]
    ]
    lines += _table(["Week", "Therapy days", "Minutes", "Status", "Why"], weeks)
    shortfalls = abstraction["consecutive_weeks_below_goal"]
    lines += [
        (
            f"Runs of {shortfalls['run_length']} weeks below goal, established: {_runs(shortfalls['established'])}. "
            f"Depending on unresolved records: {_runs(shortfalls['depends_on_unresolved_records'])}."
        ),
        "",
    ]

    lines += ["## Encounters", "", "One row per real contact, after merging every document that describes it.", ""]
    encounters = [
        [
            " / ".join(e["service_dates"]) if len(e.get("service_dates") or []) > 1 else e["service_date"],
            e["encounter"],
            f"{e['service_type']} ({e['modality']})",
            e["status"],
            e["counts_as_therapy"],
            _range(e["minutes_low"], e["minutes_high"]),
            ", ".join(e["sources"]),
        ]
        for e in abstraction["encounters"]
    ]
    lines += _table(["Date", "Encounter", "Type", "Status", "Counted", "Minutes", "Sources"], encounters)

    lines += ["## Flags", ""]
    for flag in abstraction["flags"]:
        cited = f" [{', '.join(flag['citations'])}]" if flag["citations"] else ""
        settles = f" {flag['settles']}" if flag["settles"] else ""
        lines.append(f"- **{flag['kind']}** ({flag['encounter'] or 'patient'}): {flag['text']}{cited}{settles}")
    lines += [""] if abstraction["flags"] else ["None.", ""]

    lines += ["## Assessments", "", "Each distinct administration once; a copy of a result is named, never counted again.", ""]
    assessments = [
        [a["completed_on"], a["instrument"], a["total_score"], ", ".join(a["sources"]), ", ".join(a["copies"])]
        for a in abstraction["assessments"]
    ]
    lines += _table(["Completed", "Instrument", "Total", "Sources", "Copies"], assessments)

    lines += ["## Plan versions", ""]
    compared = abstraction["plan_versions_compared"]
    delivered = {row["plan_version"]: row for row in compared["versions"]}
    plans = []
    for plan in abstraction["plans"]:
        row = delivered.get(plan["version"])
        plans.append(
            [
                plan["version"],
                f"{plan['effective_date'] or 'not stated'} to {plan['effective_end'] or 'open'}",
                f"{plan['min_days_per_week']} days, {plan['min_minutes_per_week']} minutes",
                ", ".join(plan["counted_service_types"]),
                _range(row["minutes_per_week_low"], row["minutes_per_week_high"]) if row else None,
                ", ".join(plan["sources"]),
            ]
        )
    lines += _table(["Version", "Effective", "Goal per week", "Counted types", "Delivered minutes per week", "Sources"], plans)
    if compared["note"]:
        lines += [compared["note"], ""]

    lines += ["## Observations", ""]
    observations = [[o["observed_on"], o["category"], o["text"], ", ".join(o["sources"])] for o in abstraction["observations"]]
    lines += _table(["Date", "Category", "Statement", "Sources"], observations)
    return "\n".join(lines).rstrip("\n") + "\n"
