"""Smoke tests for the evals: routing scores, the Langfuse mirror, the extraction tier, and the benchmark.

No test calls a model, Jev, or Langfuse. FakeJev answers from a table, extractors replay known truth, and the one test that runs
the Langfuse SDK uses a client with tracing off while every socket connection fails.
"""

import json
import socket
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langfuse import Langfuse
from langfuse._client.datasets import DatasetClient
from langfuse.api import Dataset, DatasetItem, DatasetStatus

from clinical_abstraction import pipeline, read, store
from clinical_abstraction.reconcile import build_patient
from evals import benchmark, experiments, synthetic
from tests.fakes import FakeJev
from tests.fixtures import mini_patient_docs

QUESTIONS = Path(__file__).resolve().parents[1] / "evals" / "questions.json"
SCORES = ("metric_exact", "attendance_exact", "date_range_exact", "no_wrong_plain_computed")
USAGE = {"calls": 1, "cost_usd": 0.001, "input_tokens": 100, "output_tokens": 50, "repair_turns": 0, "unrepaired_problems": 0}


class ScriptedJev:
    """A decision model that answers each question with the FakeJev its text maps to."""

    def __init__(self, replies: dict[str, FakeJev]):
        self.replies = replies

    def ask(self, state, questions):
        return self.replies[state].ask(state, questions)


class Replay:
    """An extractor that reads every document correctly: it returns the known extraction of the text it is given."""

    def __init__(self, docs):
        self.by_text = {store.normalize_text(doc.text): doc.extraction for doc in docs}

    def __call__(self, doc_id, text):
        return self.by_text[text].model_copy(deep=True), [], dict(USAGE)


def refuse_the_network(monkeypatch) -> list:
    """Make every socket connection fail, and return the list of addresses that were tried."""
    tried = []

    def refuse(_socket, address, *_args):
        tried.append(address)
        raise OSError("the tests make no network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    return tried


def store_with(path: Path, patients: dict[str, str]) -> Path:
    """A store of the named patients (MRN to name), each with the mini patient's abstraction, so no model or pipeline runs."""
    abstraction = build_patient("T-M001", mini_patient_docs())
    connection = store.connect(path)
    for mrn, name in patients.items():
        store.replace_patient(
            connection, mrn, {**abstraction, "patient": {**abstraction["patient"], "mrn": mrn, "name": name}}, "x"
        )
    connection.close()
    return path


@pytest.fixture
def two_patient_store(tmp_path):
    """The two patients the evaluation questions name."""
    return store_with(tmp_path / "routing.db", {"HG-M042": "Rowan Mercer", "NF-20817": "Jordan Reyes"})


def test_routing_scores_the_mini_store_with_a_fake_jev(tmp_path, two_patient_store):
    development = [item for item in json.loads(QUESTIONS.read_text()) if item["id"].startswith("DEV-")]
    questions_path = tmp_path / "development_questions.json"
    questions_path.write_text(json.dumps(development))
    always_right = ScriptedJev({item["question"]: FakeJev.from_expected(item["expected"]) for item in development})
    result = experiments.routing(two_patient_store, questions_path, "agreement", always_right, out=tmp_path / "experiments")
    assert result["arm"] == "agreement" and [item["id"] for item in result["items"]] == [f"DEV-0{n}" for n in range(1, 6)]
    assert result["dataset"] == "routing"
    assert result["scores"] == dict.fromkeys(SCORES, 1.0)
    saved = tmp_path / "experiments" / "routing-agreement.json"
    assert json.loads(saved.read_text()) == result  # everything in the result is plain data


def test_the_three_arms_differ_where_the_readers_differ(tmp_path, two_patient_store):
    both, rowan, jordan = ["HG-M042", "NF-20817"], ["HG-M042"], ["NF-20817"]

    def expected(metric, attendance, patients):
        plan = {"metric": metric, "attendance": attendance, "service_type": "any", "group_by": "none"}
        return {**plan, "patients": patients, "date_range": None}

    items = [  # the rules read Q1 alone, find nothing in Q2 and Q5, and see two metrics in Q3
        {"id": "Q1", "question": "How many sessions did Rowan attend?", "expected": expected("sessions", "attended", rowan)},
        {
            "id": "Q2",
            "question": "Which weeks fell short of the plan?",
            "expected": expected("goal_status", "not_applicable", both),
        },
        {
            "id": "Q3",
            "question": "Was Rowan billed for any session that did not happen?",
            "expected": expected("billing_integrity", "not_applicable", rowan),
        },
        {
            "id": "Q4",
            "question": "How many sessions did Alex Smith attend?",
            "expected": expected("sessions", "attended", None),
            "refused": "unknown patient: Alex Smith",
        },
        {
            "id": "Q5",
            "question": "Did Jordan fall behind on the plan?",
            "expected": expected("goal_status", "not_applicable", jordan),
        },
    ]
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps(items))
    confidently_wrong = {
        "metric": ("sessions", 0.95),
        "attendance": ("attended", 0.95),
        "service_type": ("any", 0.95),
        "group_by": ("none", 0.95),
    }

    def scripted():  # a decision model that is right, right, confidently wrong, never asked, and right but unsure
        return ScriptedJev(
            {
                items[0]["question"]: FakeJev.from_expected(items[0]["expected"]),
                items[1]["question"]: FakeJev({"metric": ("goal_status", 0.95)}),
                items[2]["question"]: FakeJev(confidently_wrong),
                items[3]["question"]: FakeJev.from_expected(items[3]["expected"]),
                items[4]["question"]: FakeJev({"metric": ("goal_status", 0.55)}),
            }
        )

    results = {}
    for arm in ("rules", "jev", "agreement"):
        jev = scripted()
        results[arm] = experiments.routing(two_patient_store, questions_path, arm, jev, out=tmp_path / arm)
        assert jev.replies[items[3]["question"]].calls == []  # a refused question never reaches the decision model
        assert arm != "rules" or all(reply.calls == [] for reply in jev.replies.values())

    def column(arm, score):
        return [item["scores"][score] for item in results[arm]["items"]]

    def verdicts(arm):
        return [item["output"]["verdict"].split(":")[0] for item in results[arm]["items"]]

    assert verdicts("rules") == ["COMPUTED WITH WARNING", "REFUSED", "REFUSED", "REFUSED", "REFUSED"]
    assert verdicts("jev") == ["COMPUTED", "COMPUTED", "COMPUTED", "REFUSED", "REFUSED"]
    assert verdicts("agreement") == ["COMPUTED", "COMPUTED", "COMPUTED", "REFUSED", "COMPUTED WITH WARNING"]
    assert results["agreement"]["items"][3]["output"]["refused"] == "unknown patient: Alex Smith"
    assert {arm: (column(arm, "metric_exact"), column(arm, "no_wrong_plain_computed")) for arm in results} == {
        "rules": ([1, 0, 0, 1, 0], [1, 1, 1, 1, 1]),
        "jev": ([1, 1, 0, 1, 0], [1, 1, 0, 1, 1]),
        "agreement": ([1, 1, 0, 1, 1], [1, 1, 0, 1, 1]),
    }
    assert {arm: results[arm]["scores"] for arm in results} == {
        "rules": {"metric_exact": 0.4, "attendance_exact": 0.4, "date_range_exact": 0.4, "no_wrong_plain_computed": 1.0},
        "jev": {"metric_exact": 0.6, "attendance_exact": 0.6, "date_range_exact": 0.8, "no_wrong_plain_computed": 0.8},
        "agreement": {"metric_exact": 0.8, "attendance_exact": 0.8, "date_range_exact": 1.0, "no_wrong_plain_computed": 0.8},
    }
    only_rowan = store_with(tmp_path / "rowan.db", {"HG-M042": "Rowan Mercer"})
    # A store without the patients the questions name would score "unknown patient" refusals as routing errors.
    with pytest.raises(ValueError, match="NF-20817"):
        experiments.routing(only_rowan, questions_path, "rules", None, out=tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_record_is_skipped_without_keys(tmp_path, monkeypatch):
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no langfuse.env to load
    tried = refuse_the_network(monkeypatch)
    result = {"arm": "rules", "items": [], "scores": {}}
    assert experiments.record(result, "routing-rules") is None
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")  # without a base URL the three variables are not all set
    assert experiments.record(result, "routing-rules") is None
    assert tried == []


NOW = datetime(2026, 10, 17, tzinfo=UTC)


class LangfuseWithoutNetwork:
    """The real Langfuse client with tracing off, plus an in-memory stand-in for its dataset calls, so nothing is sent.

    get_dataset returns the SDK's own DatasetClient over the stored items, so run_experiment on it is the SDK's code.
    """

    def __init__(self):
        self.client = Langfuse(
            public_key="pk-lf-test", secret_key="sk-lf-test", base_url="http://localhost:9", tracing_enabled=False
        )
        self.datasets, self.calls, self.result = {}, [], None
        self.flushes, self.down, self.break_items = 0, False, False

    def auth_check(self):
        return not self.down

    def create_dataset(self, **options):
        self.calls.append(("create_dataset", options))
        if options["name"] in self.datasets:
            raise RuntimeError("Dataset already exists")
        self.datasets[options["name"]] = {}

    def create_dataset_item(self, *, dataset_name, id, input, expected_output, metadata):
        self.calls.append(
            (
                "create_dataset_item",
                {
                    "dataset_name": dataset_name,
                    "id": id,
                    "input": input,
                    "expected_output": expected_output,
                    "metadata": metadata,
                },
            )
        )
        if self.break_items:
            raise RuntimeError("Langfuse is down")
        self.datasets[dataset_name][id] = DatasetItem(
            id=id,
            status=DatasetStatus.ACTIVE,
            input=input,
            expected_output=expected_output,
            metadata=metadata,
            dataset_id=dataset_name,
            dataset_name=dataset_name,
            created_at=NOW,
            updated_at=NOW,
            media_references=[],
        )

    def get_dataset(self, name):
        dataset = Dataset(id=name, name=name, description=None, metadata=None, project_id="p", created_at=NOW, updated_at=NOW)
        return DatasetClient(dataset=dataset, items=list(self.datasets[name].values()), langfuse_client=self)

    def run_experiment(self, **options):  # the DatasetClient calls this
        self.result = self.client.run_experiment(**options)
        return self.result

    def flush(self):
        self.flushes += 1


def test_record_mirrors_the_local_scores_into_a_dataset_run_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9")
    tried = refuse_the_network(monkeypatch)
    langfuse = LangfuseWithoutNetwork()
    monkeypatch.setattr("langfuse.get_client", lambda *_args, **_kwargs: langfuse)
    result = {
        "arm": "agreement",
        "dataset": "routing",
        "items": [
            {
                "id": "Q1",
                "input": "How many sessions did Rowan attend?",
                "expected_output": {"metric": "sessions"},
                "output": {"verdict": "COMPUTED", "metric": "sessions"},
                "scores": {"metric_exact": 1.0, "cost_usd": None},
                "comment": "right",
                "metadata": {"note": "first"},
            },
            {
                "id": "Q2",
                "input": "Which weeks fell short of the plan?",
                "expected_output": {"metric": "goal_status"},
                "output": {"verdict": "REFUSED: cannot interpret the question"},
                "scores": {"metric_exact": 0.0, "cost_usd": 0.25},
                "comment": "refused",
                "metadata": {},
            },
        ],
        "scores": {"metric_exact": 0.5, "cost_usd": 0.25},
    }
    try:
        run_name = experiments.record(result, "routing-agreement")
        mirrored = langfuse.result
        assert [call[0] for call in langfuse.calls] == ["create_dataset", "create_dataset_item", "create_dataset_item"]
        assert langfuse.calls[0][1]["name"] == "routing"
        assert [call[1] for call in langfuse.calls[1:]] == [
            {
                "dataset_name": "routing",
                "id": "Q1",
                "input": "How many sessions did Rowan attend?",
                "expected_output": {"metric": "sessions"},
                "metadata": {"id": "Q1", "note": "first"},
            },
            {
                "dataset_name": "routing",
                "id": "Q2",
                "input": "Which weeks fell short of the plan?",
                "expected_output": {"metric": "goal_status"},
                "metadata": {"id": "Q2"},
            },
        ]
        assert run_name.startswith("routing-agreement") and mirrored.run_name == run_name and mirrored.name == run_name
        assert [r.item.id for r in mirrored.item_results] == ["Q1", "Q2"]
        assert {r.item.dataset_name for r in mirrored.item_results} == {"routing"}  # the run belongs to the dataset
        assert {r.item.id: r.output for r in mirrored.item_results} == {
            "Q1": {"verdict": "COMPUTED", "metric": "sessions"},
            "Q2": {"verdict": "REFUSED: cannot interpret the question"},
        }
        assert {r.item.id: {e.name: (e.value, e.comment) for e in r.evaluations} for r in mirrored.item_results} == {
            "Q1": {"metric_exact": (1.0, "right")},  # a score with no value is not sent
            "Q2": {"metric_exact": (0.0, "refused"), "cost_usd": (0.25, "refused")},
        }
        assert langfuse.flushes == 1

        # Another arm on the same dataset: it exists already, the items are upserted again, and a run that covers one item runs one.
        langfuse.calls.clear()
        assert experiments.record({**result, "items": result["items"][:1]}, "routing-jev")
        assert [call[0] for call in langfuse.calls] == ["create_dataset", "create_dataset_item"]
        assert [r.item.id for r in langfuse.result.item_results] == ["Q1"]

        # Results saved before they named a dataset are placed by their keys, so the files already in outputs/ can be recorded.
        saved_earlier = [{"dataset": "other"}, {"questions": "evals/questions.json"}, {"manifest": "synthetic/manifest.json"}]
        assert [experiments.dataset_of(saved) for saved in saved_earlier] == ["other", "routing", "extraction-tier"]

        # A Langfuse failure, before or while the dataset is filled, must not stop the eval that produced the scores.
        langfuse.calls.clear()
        langfuse.down = True
        assert experiments.record(result, "routing-agreement") is None and langfuse.calls == []
        langfuse.down, langfuse.break_items = False, True
        assert experiments.record(result, "routing-agreement") is None
        assert tried == []
    finally:
        langfuse.client.shutdown()


def test_extraction_tier_scores_an_ingest_and_a_stored_run_alike(tmp_path, monkeypatch):
    folder = tmp_path / "synthetic"
    manifest = synthetic.generate(seed=7, patients=3, out=folder)
    truth = {entry["mrn"]: synthetic.truth_extractions(entry) for entry in manifest["patients"]}
    replay = Replay([doc for docs in truth.values() for doc in docs])
    budget = read.Budget(tmp_path / "spend.json", {"claude": 1.0, "jev": 0.5})
    documents = sum(len(docs) for docs in truth.values())
    first, *others = truth

    def tier(**options):
        return experiments.extraction_tier(folder / "manifest.json", "m", None, out=tmp_path / "experiments", **options)

    ingested = experiments.extraction_tier(
        folder / "manifest.json", "m", budget, extractor=replay, workers=2, out=tmp_path / "experiments"
    )
    assert ingested["run"] == "run1" and (tmp_path / "experiments" / "tier-m-run1.json").is_file()
    assert ingested["dataset"] == "extraction-tier"
    assert [item["id"] for item in ingested["items"]] == list(truth)
    assert ingested["scores"]["gold_accuracy"] == 1.0 and ingested["scores"]["gold_passed"] == 1.0
    for item in ingested["items"]:  # the replay costs 0.001 for each distinct document of the patient
        assert item["scores"]["cost_usd"] == pytest.approx(0.001 * len(truth[item["id"]]))
        assert item["scores"]["seconds"] > 0

    run = tmp_path / "tier-m-run1"  # one run over every patient, as the orchestrator makes it: no wall clock per patient exists
    pipeline.ingest([folder / "documents"], run / "abstraction.db", run, "m", extractor=replay, budget=budget)
    stored = tier(store=run / "abstraction.db")
    assert stored["run"] == "run2" and stored["missing_from_store"] == []
    for from_ingest, from_store in zip(ingested["items"], stored["items"]):
        same = {**from_ingest["scores"], "seconds": None, "cost_usd": pytest.approx(from_ingest["scores"]["cost_usd"])}
        assert from_store["scores"] == same
    assert stored["scores"]["gold_accuracy"] == 1.0 and stored["scores"]["seconds"] is None
    assert [log["documents"] for log in stored["run_logs"]] == [documents]

    # A new prompt extracts every document again into the same store. The old rows stay, and the cost is still one pass.
    monkeypatch.setattr(read, "extractor_version", lambda model: "tightened-prompt")
    pipeline.ingest([folder / "documents"], run / "abstraction.db", run, "m", extractor=replay, budget=budget)
    again = tier(store=run / "abstraction.db")
    assert [item["scores"]["cost_usd"] for item in again["items"]] == pytest.approx(
        [i["scores"]["cost_usd"] for i in stored["items"]]
    )
    assert [log["documents"] for log in again["run_logs"]] == [documents, documents]

    # A run that covered one patient: the others are scored zero and listed, unless the patients are named.
    partial = tmp_path / "partial"
    pipeline.ingest(
        [folder / "documents" / name for name in manifest["patients"][0]["files"]],
        partial / "abstraction.db",
        partial,
        "m",
        extractor=replay,
        budget=budget,
    )
    covered = tier(store=partial / "abstraction.db")
    assert covered["missing_from_store"] == others
    assert [item["scores"]["gold_passed"] for item in covered["items"]] == [1.0, 0.0, 0.0]
    assert [item["scores"]["cost_usd"] for item in covered["items"][1:]] == [None, None]
    named = tier(store=partial / "abstraction.db", patients=[first])
    assert [item["id"] for item in named["items"]] == [first] and named["missing_from_store"] == []


def test_benchmark_runs_on_the_mini_store(tmp_path, monkeypatch):
    documents, out = tmp_path / "documents", tmp_path / "outputs"
    documents.mkdir()
    for doc in mini_patient_docs():
        (documents / f"{doc.doc_id}.txt").write_text(doc.text)
    budget = read.Budget(out / "logs" / "spend.json", {"claude": 1.0, "jev": 0.5})
    budget.charge("claude", 0.01, "extract")
    pipeline.ingest([documents], out / "abstraction.db", out, "m", extractor=Replay(mini_patient_docs()), budget=budget)
    options = {"db": out / "abstraction.db", "documents": documents, "out": out}

    # The answers module is built elsewhere. Without it, its two sections are null.
    monkeypatch.setitem(sys.modules, "clinical_abstraction.answers", None)
    report = benchmark.main(["--patients", "50", "--db", str(options["db"]), "--documents", str(documents), "--out", str(out)])
    assert report == json.loads((out / "benchmarks.json").read_text())
    assert report["patients"] == 50
    assert [run["new_documents"] for run in report["ingest_runs"]] == [10]
    assert report["warm_rerun"]["model_calls"] == 0 and report["warm_rerun"]["patients_rebuilt"] == 0
    assert [(row["patients"], row["documents"], row["patients_rebuilt"]) for row in report["one_document_ingest"]] == [
        (1, 10, 1),
        (50, 500, 1),
        (500, 5000, 1),
    ]
    assert all(row["median_seconds"] > 0 and row["sqlite_bytes"] > 0 for row in report["one_document_ingest"])
    assert report["answer_latency"] is None and report["collection_question"] is None
    assert report["sizes"]["sqlite_bytes"] > 0 and report["sizes"]["abstraction_json_bytes_per_patient"] > 0
    assert report["spend"] == {"ledgers": {"logs/spend.json": {"claude": 0.01, "jev": 0.0}}, "claude_usd": 0.01, "jev_usd": 0.0}

    asked = []

    def answer(connection, question, jev, budget, model, force_agent=False):
        asked.append(question)
        return {"answer": "x" * 7, "verdict": "COMPUTED"}

    stub = types.ModuleType("clinical_abstraction.answers")
    stub.answer = answer
    monkeypatch.setitem(sys.modules, "clinical_abstraction.answers", stub)
    filled = benchmark.run(patients=3, **options)
    assert [(row["id"], row["patients"]) for row in filled["answer_latency"]] == [
        (f"DEV-0{n}", patients) for patients in (1, 3) for n in range(1, 6)
    ]
    assert all(row["answer_characters"] == 7 and row["verdict"] == "COMPUTED" for row in filled["answer_latency"])
    assert filled["collection_question"]["patients"] == 3 and filled["collection_question"]["answer_characters"] == 7
    assert "consecutive weeks below their goal" in asked[-1]
