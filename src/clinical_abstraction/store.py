"""SQLite store: documents, versioned extractions, the failure ledger, and per-patient abstractions with a rebuild fingerprint.

Documents, extractions, label checks, and failures only grow. The patient tables are derived: replace_patient swaps one
patient's rows in one transaction, so every patient can be rebuilt from the extractions without a model call.
A patient's fingerprint names the documents and the extractor version its abstraction was built from (decision D2.1).
"""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

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
            (
                mrn,
                abstraction["patient"]["name"],
                fingerprint,
                json.dumps(abstraction["incomplete"]),
                now(),
                json.dumps(abstraction),
            ),
        )
        connection.executemany(
            "INSERT INTO encounters (mrn, encounter, service_date, service_type, modality, status, counts_as_therapy, "
            "minutes_low, minutes_high, sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    mrn,
                    e["encounter"],
                    e["service_date"],
                    e["service_type"],
                    e["modality"],
                    e["status"],
                    e["counts_as_therapy"],
                    e["minutes_low"],
                    e["minutes_high"],
                    json.dumps(e["sources"]),
                )
                for e in abstraction["encounters"]
            ],
        )
        connection.executemany(
            "INSERT INTO weeks (mrn, week_start, week_end, therapy_days_low, therapy_days_high, minutes_low, minutes_high, "
            "status, explanation, plan_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    mrn,
                    w["week_start"],
                    w["week_end"],
                    w["therapy_days_low"],
                    w["therapy_days_high"],
                    w["minutes_low"],
                    w["minutes_high"],
                    w["status"],
                    w["explanation"],
                    w["plan_version"],
                )
                for w in abstraction["weeks"]
            ],
        )
        connection.executemany(
            "INSERT INTO flags (mrn, kind, encounter, text, citations, settles) VALUES (?, ?, ?, ?, ?, ?)",
            [(mrn, f["kind"], f["encounter"], f["text"], json.dumps(f["citations"]), f["settles"]) for f in abstraction["flags"]],
        )
        connection.executemany(
            "INSERT INTO assessments (mrn, instrument, completed_on, total_score, sources, copies) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (mrn, a["instrument"], a["completed_on"], a["total_score"], json.dumps(a["sources"]), json.dumps(a["copies"]))
                for a in abstraction["assessments"]
            ],
        )
        connection.executemany(
            "INSERT INTO observations (mrn, category, observed_on, text, sources) VALUES (?, ?, ?, ?, ?)",
            [(mrn, o["category"], o["observed_on"], o["text"], json.dumps(o["sources"])) for o in abstraction["observations"]],
        )
        clear_stale(connection, mrn)


def known_patients(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """(MRN, name) of every built patient, for the question parser. A patient with no name on any document has ''."""
    return [(row["mrn"], row["name"] or "") for row in connection.execute("SELECT mrn, name FROM patients ORDER BY mrn")]


def episode_year(connection: sqlite3.Connection) -> int:
    """The year of the earliest service date in the store, else the current year, for dates written without a year."""
    earliest = connection.execute("SELECT MIN(service_date) FROM encounters").fetchone()[0]
    return int(earliest[:4]) if earliest else date.today().year
