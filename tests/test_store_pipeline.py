"""Store, pipeline, export, and ingest CLI on the ten-document mini patient.

No test calls a model. FixtureExtractor stands in for extract.extract_document: it returns the hand-written extraction of the
text it is given, so ingest runs the real registration, failure ledger, fingerprint, reconciliation, storage, and export code.
"""

import hashlib
import json
import shutil
import threading
import time
from datetime import date

import pytest

from clinical_abstraction import cli, extract, llm, pipeline, store
from clinical_abstraction.jev import Jev
from clinical_abstraction.pipeline import ingest
from clinical_abstraction.reconcile import build_patient
from clinical_abstraction.schema import Interval
from tests.fixtures import PATIENT_LINE, document, mini_patient_docs, service_event

USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cost_usd": 0.001,
    "calls": 1,
    "repair_turns": 0,
    "unrepaired_problems": 0,
}


class FixtureExtractor:
    """A model that always reads a document correctly: it returns the hand-written extraction for the text it is given."""

    def __init__(self, docs):
        self.by_text = {doc.text: doc.extraction for doc in docs}
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, doc_id, text):
        with self.lock:
            self.seen.append(doc_id)
        return self.by_text[text].model_copy(deep=True), [], dict(USAGE)


class FailOnceExtractor(FixtureExtractor):
    """Raises a rate-limit error the first time it reads the named document, then reads it correctly."""

    def __init__(self, docs, failing_doc_id):
        super().__init__(docs)
        self.failing = {failing_doc_id}

    def __call__(self, doc_id, text):
        with self.lock:
            fail = doc_id in self.failing
            self.failing.discard(doc_id)
        if fail:
            raise RuntimeError("rate limit")
        return super().__call__(doc_id, text)


class CapReachedExtractor(FixtureExtractor):
    """Reads two documents, then raises BudgetExceeded on every call, like a ledger that stays over its cap."""

    def __call__(self, doc_id, text):
        with self.lock:
            over = len(self.seen) >= 2
            if over:
                self.seen.append(f"{doc_id} (refused)")
        if over:
            raise llm.BudgetExceeded("claude spend reached $10.0100, over the $10.00 cap (extract)")
        return super().__call__(doc_id, text)


class FakeJev:
    """Exposes second_read(document_id, text, extraction, client) like jev.second_read, with the answers a test chooses."""

    def __init__(self, disputes=None, fail_for=()):
        self.disputes, self.fail_for, self.seen = disputes or {}, set(fail_for), []

    def second_read(self, document_id, text, extraction, client):
        self.seen.append(document_id)
        if document_id in self.fail_for:
            raise ConnectionError("jev unreachable")
        return self.disputes.get(document_id, [])


@pytest.fixture
def mini_files(tmp_path):
    folder = tmp_path / "mini"
    folder.mkdir()
    for doc in mini_patient_docs():
        (folder / f"{doc.doc_id}.txt").write_text(doc.text)
    return folder


@pytest.fixture
def fixture_extractor():
    return FixtureExtractor(mini_patient_docs())


@pytest.fixture
def fail_once_extractor():
    return FailOnceExtractor(mini_patient_docs(), "D7")


def run(tmp_path, folder, extractor, **options):
    return ingest([folder], tmp_path / "db", tmp_path / "out", "m", extractor=extractor, **options)


def weeks_of(connection, mrn, week_start):
    return connection.execute("SELECT * FROM weeks WHERE mrn = ? AND week_start = ?", (mrn, week_start)).fetchall()


def scalar(tmp_path, sql, *parameters):
    return store.connect(tmp_path / "db").execute(sql, parameters).fetchone()[0]


# The plan's five tests.


def test_failed_extraction_marks_incomplete_then_rebuilds(tmp_path, mini_files, fail_once_extractor):
    first = run(tmp_path, mini_files, fail_once_extractor)
    assert first["extraction"]["failed"] == 1 and first["incomplete"] == {"T-M001": 1}
    conn = store.connect(tmp_path / "db")
    assert json.loads(conn.execute("SELECT abstraction FROM patients").fetchone()[0])["incomplete"][0]["doc_id"] == "D7"
    # A missing document never makes a week more certain: D7 is a signed note, so no week of this patient is settled.
    assert {w["status"] for w in weeks_of(conn, "T-M001", "2026-01-26")} == {"cannot_determine"}
    assert "rate limit" in (tmp_path / "out" / "abstraction" / "T-M001.md").read_text()
    second = run(tmp_path, mini_files, fail_once_extractor)
    assert second["extraction"]["documents"] == 1 and second["rebuilt"] == ["T-M001"] and second["incomplete"] == {}
    assert scalar(tmp_path, "SELECT COUNT(*) FROM failures") == 0
    assert "rate limit" not in (tmp_path / "out" / "abstraction" / "T-M001.md").read_text()


def test_duplicate_file_changes_nothing_and_costs_nothing(tmp_path, mini_files, fixture_extractor):
    run(tmp_path, mini_files, fixture_extractor)
    reads = len(fixture_extractor.seen)
    shutil.copy(mini_files / "D2.txt", mini_files / "D2_resent_copy.txt")
    # The same text with a byte-order mark, Windows line endings, and trailing spaces is still the same document.
    (mini_files / "D2_windows.txt").write_bytes(b"\xef\xbb\xbf" + mini_patient_docs()[1].text.replace("\n", "  \r\n").encode())
    again = run(tmp_path, mini_files, fixture_extractor)
    assert again["new_documents"] == 0 and again["extraction"]["documents"] == 0 and again["rebuilt"] == []
    assert again["extraction"]["calls"] == 0 and len(fixture_extractor.seen) == reads
    assert scalar(tmp_path, "SELECT COUNT(*) FROM documents") == 10
    assert scalar(tmp_path, "SELECT COUNT(*) FROM files") == 12  # the resent files are on record, pointing at the same document


def test_version_change_rebuilds_every_touched_patient(tmp_path, mini_files, fixture_extractor, monkeypatch):
    run(tmp_path, mini_files, fixture_extractor)
    old_version, old_fingerprint = extract.extractor_version("m"), scalar(tmp_path, "SELECT fingerprint FROM patients")
    monkeypatch.setattr(extract, "extractor_version", lambda model: "new-version-1")
    again = run(tmp_path, mini_files, fixture_extractor)
    assert again["extraction"]["documents"] == 10 and again["rebuilt"] == ["T-M001"]
    assert scalar(tmp_path, "SELECT fingerprint FROM patients") != old_fingerprint
    # Extraction rows only grow, so the old version stays as the rollback.
    counts = store.connect(tmp_path / "db").execute("SELECT version, COUNT(*) FROM extractions GROUP BY version").fetchall()
    assert {row[0]: row[1] for row in counts} == {old_version: 10, "new-version-1": 10}


def test_provisional_patient_id():
    assert store.provisional_patient_id("Patient: Rowan Mercer  MRN: HG-M042") == "HG-M042"
    assert store.provisional_patient_id("Record #NF-20817  Visit #NF-V3107") == "NF-20817"
    assert store.provisional_patient_id("no identifiers here") is None
    assert store.provisional_patient_id(PATIENT_LINE) == "T-M001"  # the fixture's MRN has a one-letter prefix
    # Without a label, the first token that is not a visit, encounter, or document id.
    assert store.provisional_patient_id("Visit NF-V3107 | Encounter HG-E1102 | Chart NF-20817") == "NF-20817"


@pytest.fixture
def replicated_store_5000_docs(tmp_path):
    """500 patients with the ten mini documents each, inserted as rows; every patient already has its abstraction."""
    connection = store.connect(tmp_path / "replicated.db")
    version = extract.extractor_version("m")
    docs = mini_patient_docs()
    payloads = {doc.doc_id: doc.extraction.model_dump_json() for doc in docs}
    abstraction = build_patient("T-M001", docs)
    for number in range(500):
        mrn = f"T-R{number:03d}"
        texts = {doc.doc_id: doc.text.replace("T-M001", mrn) for doc in docs}
        shas = {doc_id: hashlib.sha256(text.encode()).hexdigest() for doc_id, text in texts.items()}
        connection.executemany(
            "INSERT INTO documents (sha, doc_id, patient_id, text, authored_at, received_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(shas[d.doc_id], f"{mrn}-{d.doc_id}", mrn, texts[d.doc_id], d.authored_at, d.received_at) for d in docs],
        )
        connection.executemany(
            "INSERT INTO extractions (sha, version, model, payload, usage, created_at) VALUES (?, ?, 'm', ?, '{}', '2026-01-01')",
            [(shas[d.doc_id], version, payloads[d.doc_id].replace('"T-M001"', f'"{mrn}"')) for d in docs],
        )
        connection.commit()
        fingerprint, _ = store.fingerprint(connection, mrn, version)
        store.replace_patient(connection, mrn, {**abstraction, "patient": {**abstraction["patient"], "mrn": mrn}}, fingerprint)
    connection.close()
    return tmp_path / "replicated.db"


def test_one_document_ingest_does_not_scale_with_the_collection(tmp_path, replicated_store_5000_docs, monkeypatch):
    new_doc = document(
        "D11_new",
        [
            "Individual psychotherapy note - E5 - 2026-01-28",
            "Patient: Rowan Hale | MRN: T-R250",
            "Patient present 14:00-14:50, in person",
            "Signed by J. Ortiz, LCSW on 2026-01-28 15:00",
        ],
        "individual psychotherapy note",
        "2026-01-28T15:00",
        patient_mrn="T-R250",
        service_events=[
            service_event(
                encounter_id="E5",
                service_date="2026-01-28",
                service_type="individual_psychotherapy",
                patient_present_intervals=[Interval(start="14:00", end="14:50")],
                lines=[1, 3],
            )
        ],
    )
    folder = tmp_path / "new"
    folder.mkdir()
    (folder / "D11_new.txt").write_text(new_doc.text)
    loaded = []
    real_load = pipeline.load_patient_docs

    def spy(connection, mrn, version):
        loaded.append(mrn)
        return real_load(connection, mrn, version)

    monkeypatch.setattr(pipeline, "load_patient_docs", spy)
    started = time.perf_counter()
    summary = ingest([folder], replicated_store_5000_docs, tmp_path / "out", "m", extractor=FixtureExtractor([new_doc]))
    assert time.perf_counter() - started < 0.5
    assert summary["new_documents"] == 1 and summary["rebuilt"] == ["T-R250"] and loaded == ["T-R250"]
    assert store.connect(replicated_store_5000_docs).execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 5001


# Five more tests for rules the plan's five do not pin.


def test_unread_documents_leave_the_patient_incomplete_and_a_budget_cap_stops_the_batch(tmp_path, mini_files, fixture_extractor):
    # A budget cap stops the batch: what was read is kept, the rest stays pending, and the next run finishes the job.
    capped = tmp_path / "capped"
    first = run(capped, mini_files, CapReachedExtractor(mini_patient_docs()), workers=1)
    assert first["stopped"].startswith("claude spend reached") and first["extraction"]["extracted"] == 2
    assert first["extraction"]["failed"] >= 1 and first["incomplete"] == {"T-M001": 8}
    second = run(capped, mini_files, FixtureExtractor(mini_patient_docs()), workers=1)
    assert second["stopped"] is None and second["extraction"]["documents"] == 8 and second["incomplete"] == {}
    # A new document that fails marks an already built patient incomplete: the fingerprint covers pending documents.
    assert run(tmp_path, mini_files, fixture_extractor)["incomplete"] == {}
    (mini_files / "D11.txt").write_text(f"Individual psychotherapy note - E5 - 2026-01-28\n{PATIENT_LINE}\nPatient present 14:00-14:50\n")

    def times_out(doc_id, text):
        raise TimeoutError("read timed out")

    again = run(tmp_path, mini_files, times_out)
    assert again["new_documents"] == 1 and again["extraction"]["failed"] == 1
    assert again["rebuilt"] == ["T-M001"] and again["incomplete"] == {"T-M001": 1}
    statuses = store.connect(tmp_path / "db").execute("SELECT status FROM weeks WHERE mrn = 'T-M001'").fetchall()
    assert {row[0] for row in statuses} == {"cannot_determine"}


def test_second_read_disputes_are_stored_and_reach_the_reconciler(tmp_path, mini_files, fixture_extractor):
    dispute = {"encounter_id": "E2", "field": "attendance", "model_value": "attended", "jev_value": "no_show", "confidence": 0.9}
    jev = FakeJev({"D5": [dispute]}, fail_for={"D3"})
    summary = run(tmp_path, mini_files, fixture_extractor, jev=jev)
    assert sorted(jev.seen) == sorted(f"D{n}" for n in range(1, 11)) and summary["extraction"]["second_read_failed"] == 1
    conn = store.connect(tmp_path / "db")
    stored = {
        r["doc_id"]: json.loads(r["payload"])
        for r in conn.execute("SELECT d.doc_id, c.payload FROM label_checks c JOIN documents d ON d.sha = c.sha")
    }
    assert len(stored) == 9 and "D3" not in stored and stored["D5"] == [dispute] and stored["D1"] == []
    assert conn.execute("SELECT counts_as_therapy FROM encounters WHERE encounter = 'E2'").fetchone()[0] == "disputed"
    assert conn.execute("SELECT encounter FROM flags WHERE kind = 'label_disputed'").fetchall()[0][0] == "E2"
    run(tmp_path, mini_files, fixture_extractor, jev=jev)  # nothing new was extracted, so Jev reads nothing again
    assert len(jev.seen) == 10


def test_exports_and_derived_tables_carry_the_abstraction(tmp_path, mini_files, fixture_extractor):
    empty = store.connect(tmp_path / "empty.db")
    assert store.known_patients(empty) == [] and store.episode_year(empty) == date.today().year
    run(tmp_path, mini_files, fixture_extractor)
    conn = store.connect(tmp_path / "db")
    assert store.known_patients(conn) == [("T-M001", "Rowan Hale")] and store.episode_year(conn) == 2026
    stored = json.loads(conn.execute("SELECT abstraction FROM patients").fetchone()[0])
    assert json.loads((tmp_path / "out" / "abstraction" / "T-M001.json").read_text()) == stored
    assert stored["built_with"]["documents_extracted"] == 10 and stored["incomplete"] == []
    minutes = {r["encounter"]: (r["minutes_low"], r["minutes_high"]) for r in conn.execute("SELECT * FROM encounters")}
    assert minutes == {"E1": (60, 60), "E2": (45, 45), "E3": (40, 50), "E4": (0, 0)}
    weeks = [r["week_start"] for r in conn.execute("SELECT week_start FROM weeks ORDER BY week_start")]
    assert weeks == ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"]
    conflict = conn.execute("SELECT * FROM flags WHERE kind = 'unresolved_minutes_conflict'").fetchone()
    assert "start time of E3" in conflict["settles"] and {"D6 L3", "D7 L3"} <= set(json.loads(conflict["citations"]))
    assessments = conn.execute("SELECT * FROM assessments ORDER BY completed_on").fetchall()
    assert [(a["completed_on"], a["total_score"], json.loads(a["copies"])) for a in assessments] == [
        ("2026-01-05", 18, []),
        ("2026-01-16", 14, ["D10 L5"]),
    ]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    text = (tmp_path / "out" / "abstraction" / "T-M001.md").read_text()
    for fact in ("40-50", "start time of E3", "D6 L3", "charge_for_undelivered_service", "PHQ-9", "not_met"):
        assert fact in text


def test_cli_ingests_reports_and_cites_without_a_model(tmp_path, mini_files, fixture_extractor, monkeypatch, capsys):
    seen = {}

    def fake_extract_document(document_id, text, model, budget):
        seen.update(model=model, budget=budget)
        return fixture_extractor(document_id, text)

    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no key files to load
    monkeypatch.setenv("MODEL_BUDGET_USD", "7.5")
    monkeypatch.setenv("JEV_BUDGET_USD", "0.25")
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    base = ["--db", str(tmp_path / "db"), "--out", str(tmp_path / "out")]
    assert cli.main([*base, "ingest", str(mini_files), "--model", "m", "--workers", "2"]) == 0
    assert seen["model"] == "m" and seen["budget"].caps == {"claude": 7.5, "jev": 0.25}
    assert seen["budget"].ledger == tmp_path / "out" / "logs" / "spend.json"
    assert json.loads(capsys.readouterr().out)["rebuilt"] == ["T-M001"]
    # A Jev client exists only when its key does.
    assert cli.jev_client(tmp_path / "out", seen["budget"]) is None
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    assert isinstance(cli.jev_client(tmp_path / "out", seen["budget"]), Jev)
    assert cli.main([*base, "report", "--patient", "T-M001"]) == 0 and "40-50" in capsys.readouterr().out
    assert cli.main([*base, "report"]) == 0 and "T-M001" in capsys.readouterr().out
    assert cli.main([*base, "report", "--patient", "NOPE"]) == 1 and "NOPE" in capsys.readouterr().err
    assert cli.main([*base, "source", "D6 L3"]) == 0
    assert capsys.readouterr().out.strip() == "D6 L3: Patient present 09:00-09:50, in person"
    assert cli.main([*base, "source", "D6 L2-3"]) == 0 and len(capsys.readouterr().out.splitlines()) == 2
    assert cli.main([*base, "source", "NOPE L1"]) == 1 and "NOPE" in capsys.readouterr().err
    assert cli.main([*base, "source", "D6 L99"]) == 1 and "L99" in capsys.readouterr().err
    assert cli.main(["--db", str(tmp_path / "missing.db"), "report"]) == 2


def test_document_identity_patient_follows_the_extraction_and_ids_stay_unique(tmp_path):
    lines = {
        "X1": ["Document ID: X1", "Patient: Pat One | MRN: HG-X111", "Chart cross-reference: HG-X222", "Signed note."],
        "X2": ["Document ID: X2", "Patient: Pat Two | MRN: HG-X333", "Signed note."],
        "X2b": ["Document ID: X2", "Patient: Pat Two | MRN: HG-X333", "Signed note, revised."],  # one printed id, two texts
        "X3": ["Document ID: X3", "Unlabelled scrap of a note."],
    }
    claimed = {"X1": ("HG-X222", "Pat One"), "X2": ("HG-X999", "Pat Two"), "X2b": ("HG-X333", "Pat Two"), "X3": (None, None)}
    docs = [document(i, lines[i], "note", None, patient_mrn=claimed[i][0], patient_name=claimed[i][1]) for i in lines]
    folder = tmp_path / "files"
    folder.mkdir()
    for doc in docs:
        (folder / f"{doc.doc_id}.txt").write_text(doc.text)
    extractor = FailOnceExtractor(docs, "X1")  # X2 claims an id its text lacks, X3 has no id at all
    first = run(tmp_path, folder, extractor)
    assert first["incomplete"] == {"HG-X111": 1} and first["unassigned"] == ["X3"]
    second = run(tmp_path, folder, extractor)
    assert second["rebuilt"] == ["HG-X222"] and second["incomplete"] == {} and second["unassigned"] == ["X3"]
    # X1 moved to the patient the extraction named, and the patient it left no longer exists.
    conn = store.connect(tmp_path / "db")
    assert store.known_patients(conn) == [("HG-X222", "Pat One"), ("HG-X333", "Pat Two")]
    revised = hashlib.sha256(docs[2].text.encode()).hexdigest()[:6]
    assert [r[0] for r in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")] == ["X1", "X2", f"X2~{revised}", "X3"]
