"""Two experiments on decisions the design rests on, and the Langfuse mirror of their scores.

    uv run --frozen python -m evals.experiments routing --arm agreement
    uv run --frozen python -m evals.experiments tier --model claude-haiku-5-5 --manifest outputs/experiments/synthetic/manifest.json
    uv run --frozen python -m evals.experiments tier --model claude-haiku-5-5 --store outputs/experiments/tier-haiku-run1/abstraction.db

routing: every question in evals/questions.json is planned by one of three arms. `rules` is the keyword rules alone, `jev` is the
decision model alone, `agreement` is questions.plan with both readers. The plans are scored against the expected ones.

tier: the abstraction of each synthetic patient, made by one extraction model, is scored against the gold file generated with the
patient. The abstractions come from an ingest of that patient's documents, or from a store a run already made (`--store`).

Every score is computed here and written to outputs/experiments/. When Langfuse keys exist, record() mirrors the same scores as a
run on a Langfuse dataset (one dataset per kind of experiment, one run per arm or model, so the Experiments view can compare
them); without keys, or when Langfuse fails, the local result is all there is (decision D6.2).
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from clinical_abstraction import questions, read, store
from evals import score

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
EXPERIMENTS = OUTPUTS / "experiments"
ROUTING_DATASET, TIER_DATASET = "routing", "extraction-tier"  # the Langfuse datasets the two experiments record their runs on

Arm = Literal["rules", "jev", "agreement"]
ARMS = ("rules", "jev", "agreement")
EXACT = ("metric_exact", "attendance_exact", "date_range_exact")
FIELDS = (*questions.ENUMS, "patients", "date_range")  # what a plan must match for the question to be answered right
NOTHING = {  # the abstraction of a patient the store has none for: every gold check fails
    "encounters": [],
    "weeks": [],
    "review_period": {"start": None, "end": None},
    "assessments": [],
    "flags": [],
    "consecutive_weeks_below_goal": None,
}


def _shown(path: Path) -> str:
    """A path as written into results: relative to the repository when it is inside, so a committed result names no machine."""
    path = Path(path)
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _save(result: dict, path: Path) -> dict:
    """Stamp the result and write it as JSON. Returns the stamped result."""
    result = {**result, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1) + "\n")
    return result


def _means(items: list[dict]) -> dict:
    """The mean of each score over the items that have one. A score no item has is None."""
    means = {}
    for name in dict.fromkeys(name for item in items for name in item["scores"]):
        values = [item["scores"][name] for item in items if item["scores"].get(name) is not None]
        means[name] = round(sum(values) / len(values), 6) if values else None
    return means


# ---- Routing ----


def _window(date_range: questions.DateRange | None) -> list | None:
    return None if date_range is None else [day.isoformat() if day else None for day in (date_range.start, date_range.end)]


def _jev_alone(question: str, known: list[tuple[str, str]], year: int, jev: read.Jev) -> questions.Plan:
    """The decision model's choices when it is at least 0.9 sure of every field the answer uses, else a refusal.

    Patients and dates are read by code, as in every arm. The fields that count are the metric, and the three filters when the
    metric is one that filters (the rule questions.plan uses to look for disputes).
    """
    patients = questions.parse_patients(question, known)
    date_range = questions.parse_dates(question, year)
    answers = questions.choose(question, jev)  # after the code checks, so a refused question costs nothing
    used = [field for field in questions.ENUMS if field == "metric" or answers["metric"].choice in questions.FILTERED_METRICS]
    unsure = [
        f"{field} {answers[field].confidence:.2f}" for field in used if answers[field].confidence < questions.AGREEMENT_CONFIDENCE
    ]
    if unsure:
        raise questions.PlanError(f"the decision model is not sure enough: {', '.join(unsure)}")
    values = {field: answers[field].choice for field in questions.ENUMS}
    if values["metric"] not in questions.FILTERED_METRICS:
        values |= questions.NEUTRAL
    readings = {field: {"decision_model": [answers[field].choice, answers[field].confidence]} for field in questions.ENUMS}
    kind = "open" if values["metric"] == "other" else "computed"
    return questions.Plan(kind, **values, patients=patients, date_range=date_range, readings=readings, warnings=[])


def _read(arm: Arm, question: str, known: list[tuple[str, str]], year: int, jev: read.Jev | None) -> dict:
    """What one arm makes of one question, as plain data: the verdict a person would see and the plan behind it."""
    try:
        if arm == "jev":
            plan = _jev_alone(question, known, year, jev)
        else:
            plan = questions.plan(question, known, year, jev if arm == "agreement" else None)
    except questions.PlanError as error:
        return {"verdict": f"REFUSED: {error}", "refused": str(error)}
    if plan.kind == "open":
        verdict = "AGENT"
    elif plan.warnings:
        verdict = "COMPUTED WITH WARNING: " + "; ".join(plan.warnings)
    else:
        verdict = "COMPUTED"
    return {
        "verdict": verdict,
        **{field: getattr(plan, field) for field in questions.ENUMS},
        "patients": plan.patients,
        "date_range": _window(plan.date_range),
        "warnings": plan.warnings,
        "readings": plan.readings,
    }


def _routing_scores(entry: dict, output: dict) -> tuple[dict, str]:
    """The four scores of one question, and a comment that says what was wrong.

    A question whose entry has `refused` is right only when it is refused for that reason. Any other question is wrong when it
    is refused. no_wrong_plain_computed is 0.0 only for a plan that differs from the expected one in any field and still
    prints a plain COMPUTED verdict; a refusal, a warning, or a route to the agent is safe.
    """
    expected, reason, refused = entry["expected"], entry.get("refused"), output.get("refused")
    if reason:
        right = refused is not None and reason in refused
        exact = dict.fromkeys(EXACT, right)
        wanted, found = f"a refusal for '{reason}'", output["verdict"] if refused is not None else _describe(output)
    elif refused is not None:
        right, exact = False, dict.fromkeys(EXACT, False)
        wanted, found = _describe(expected), output["verdict"]
    else:
        right = all(output[field] == expected[field] for field in FIELDS)
        exact = {f"{field}_exact": output[field] == expected[field] for field in ("metric", "attendance", "date_range")}
        wanted, found = _describe(expected), _describe(output)
    exact["no_wrong_plain_computed"] = right or output["verdict"] != "COMPUTED"
    comment = output["verdict"] if right else f"expected {wanted}; got {found}; verdict {output['verdict']}"
    return {name: float(value) for name, value in exact.items()}, comment


def _describe(values: dict) -> str:
    return ", ".join(f"{field} {values.get(field)}" for field in FIELDS)


def routing(db: Path, questions_path: Path, arm: Arm, jev: read.Jev | None, *, out: Path = EXPERIMENTS) -> dict:
    """Plan every question in questions_path with one arm against the store, score the plans, and write routing-<arm>.json.

    The store must hold the patients the questions name, or refusals for an unknown patient would count as routing errors.
    The jev and agreement arms need a decision model (a Jev, or any object with the same ask method).
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; choose one of {', '.join(ARMS)}")
    if arm != "rules" and jev is None:
        raise ValueError(f"the {arm} arm needs a decision model")
    if not Path(db).exists():
        raise FileNotFoundError(f"no store at {db}")
    entries = json.loads(Path(questions_path).read_text())
    connection = store.connect(db)
    try:
        known, year = store.known_patients(connection), store.episode_year(connection)
    finally:
        connection.close()
    missing = sorted({mrn for entry in entries for mrn in entry["expected"]["patients"] or []} - {mrn for mrn, _ in known})
    if missing:
        raise ValueError(
            f"the store {db} lacks patients the questions expect: {', '.join(missing)}. Use a store that holds them all."
        )
    items = []
    for entry in entries:
        output = _read(arm, entry["question"], known, year, jev)
        scores, comment = _routing_scores(entry, output)
        items.append(
            {
                "id": entry["id"],
                "input": entry["question"],
                "expected_output": {"refused": entry["refused"]} if "refused" in entry else entry["expected"],
                "output": output,
                "scores": scores,
                "comment": comment,
                "metadata": {"note": entry.get("note"), "rules_wrong": bool(entry.get("rules_wrong"))},
            }
        )
    result = {
        "arm": arm,
        "dataset": ROUTING_DATASET,
        "store": _shown(db),
        "questions": _shown(questions_path),
        "items": items,
        "scores": _means(items),
    }
    return _save(result, Path(out) / f"routing-{arm}.json")


# ---- Extraction tier ----


def _stored_run(path: Path, mrn: str) -> tuple[dict | None, float | None]:
    """One patient's abstraction from a store, and what the extractions behind it cost.

    The cost counts only the extractions of the version the abstraction was built with, so a store that was extracted again under
    a new prompt (it keeps the old rows) costs one pass, not two. Both are None when the store has no abstraction for the patient,
    which is what a patient whose documents all failed looks like.
    """
    if not Path(path).exists():
        raise FileNotFoundError(f"no store at {path}")
    connection = store.connect(path)
    try:
        row = connection.execute("SELECT abstraction FROM patients WHERE mrn = ?", (mrn,)).fetchone()
        if row is None:
            return None, None
        abstraction = json.loads(row["abstraction"])
        version = (abstraction.get("built_with") or {}).get("extractor_version")
        cost = connection.execute(
            "SELECT SUM(json_extract(e.usage, '$.cost_usd')) FROM documents d "
            "JOIN extractions e ON e.sha = d.sha AND e.version = ? WHERE d.patient_id = ?",
            (version, mrn),
        ).fetchone()[0]
    finally:
        connection.close()
    return abstraction, None if cost is None else round(cost, 6)


def _ingest_patient(manifest: Path, entry: dict, model: str, budget: read.Budget, extractor, workers: int) -> tuple[dict | None, dict]:
    """Ingest one synthetic patient's documents into a temporary store. Returns the abstraction and the ingest summary."""
    paths = [manifest.parent / "documents" / name for name in entry["files"]]
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "abstraction.db"
        summary = store.ingest(paths, db, Path(folder), model, workers, extractor=extractor, budget=budget, jev=None)
        return _stored_run(db, entry["mrn"])[0], summary


def _run_logs(folder: Path) -> list[dict] | None:
    """The totals each ingest log in a run folder (or its logs folder) recorded, or None when there is no log.

    The logs hold the wall clock of a run, which no per-patient number can: one run over many patients shares its workers.
    A re-extraction under a new prompt adds a log to the same folder, so a store can hold several passes; the list shows them all.
    """
    runs = []
    for log in sorted([*folder.glob("ingest-*.jsonl"), *folder.glob("logs/ingest-*.jsonl")]):
        for line in filter(None, map(str.strip, log.read_text().splitlines())):
            event = json.loads(line)
            if event["event"] == "ingest_finished":
                extraction = event["extraction"]
                runs.append(
                    {
                        "log": log.name,
                        "documents": extraction["documents"],
                        "cost_usd": extraction["cost_usd"],
                        "total_seconds": event["total_seconds"],
                    }
                )
    return runs or None


def _next_run(out: Path, model: str) -> str:
    """run1, run2, ...: the first label with no tier file for this model."""
    taken = [
        int(found[1])
        for path in Path(out).glob("tier-*.json")
        if (found := re.fullmatch(rf"tier-{re.escape(model)}-run(\d+)\.json", path.name))
    ]
    return f"run{max(taken, default=0) + 1}"


def extraction_tier(
    manifest: Path,
    model: str,
    budget: read.Budget | None,
    patients: list[str] | None = None,
    *,
    store: Path | None = None,
    extractor=None,
    workers: int = 8,
    out: Path = EXPERIMENTS,
    run: str | None = None,
) -> dict:
    """Score the abstraction of each synthetic patient in the manifest against its gold file, and write tier-<model>-<run>.json.

    By default each patient's documents are ingested into a temporary store with `model`, spending from `budget`; the batch stops
    at the first patient whose ingest hit the budget cap. `extractor` replaces the model call, for tests.
    With `store`, nothing is ingested: the patients are scored from a store a run already made, and no budget is needed. Their cost
    is what the extractions behind each abstraction cost (see _stored_run). Their seconds are None, because one run over many
    patients has no wall clock per patient; `run_logs` has the wall clock of each ingest pass instead.
    A patient the store lacks scores zero and is listed in `missing_from_store`: pass `patients` to score only those a run covered.
    Run labels count up per model: run1, run2, ...
    """
    # In this function `store` is a path, so the helpers above are the ones that use the store module.
    manifest, out = Path(manifest), Path(out)
    entries = json.loads(manifest.read_text())["patients"]
    if patients:
        unknown = sorted(set(patients) - {entry["mrn"] for entry in entries})
        if unknown:
            raise ValueError(f"not in the manifest {manifest}: {', '.join(unknown)}")
        entries = [entry for entry in entries if entry["mrn"] in patients]
    if store is None and budget is None:
        raise ValueError("an ingest run needs a budget; pass store= to score a run that already exists")
    run_logs = _run_logs(Path(store).parent) if store is not None else None
    run = run or _next_run(out, model)
    items, stopped = [], None
    for entry in entries:
        mrn = entry["mrn"]
        gold = json.loads((manifest.parent / "gold" / f"{mrn}.json").read_text())
        if store is not None:
            (abstraction, cost), ingest, seconds = _stored_run(store, mrn), None, None
        else:
            abstraction, ingest = _ingest_patient(manifest, entry, model, budget, extractor, workers)
            cost, seconds = ingest["extraction"]["cost_usd"], ingest["total_seconds"]
        checked = score.score(abstraction or NOTHING, gold)
        failed = [failure["check"] for failure in checked["failures"]]
        output = {
            "passed": checked["passed"],
            "total": checked["total"],
            "failures": checked["failures"],
            "missing_from_store": abstraction is None,
        }
        if ingest is not None:
            output |= {
                "extraction": ingest["extraction"],
                "incomplete": ingest["incomplete"].get(mrn, 0),
                "stopped": ingest["stopped"],
            }
        items.append(
            {
                "id": mrn,
                "input": {key: entry[key] for key in ("mrn", "template", "hazards", "documents")},
                "expected_output": gold["totals"],
                "output": output,
                "scores": {
                    "gold_accuracy": checked["accuracy"],
                    "gold_passed": float(checked["passed"] == checked["total"]),
                    "cost_usd": cost,
                    "seconds": seconds,
                },
                "comment": f"{checked['passed']} of {checked['total']} checks passed"
                + (f"; first failures: {', '.join(failed[:5])}" if failed else ""),
                "metadata": {},
            }
        )
        if ingest is not None and ingest["stopped"]:
            stopped = ingest["stopped"]
            break  # the cap is reached: another patient would spend past it
    result = {
        "arm": model,
        "dataset": TIER_DATASET,
        "model": model,
        "run": run,
        "manifest": _shown(manifest),
        "store": None if store is None else _shown(store),
        "stopped": stopped,
        "missing_from_store": [item["id"] for item in items if item["output"]["missing_from_store"]],
        "run_logs": run_logs,
        "items": items,
        "scores": _means(items),
    }
    return _save(result, out / f"tier-{model}-{run}.json")


# ---- Langfuse ----


def _langfuse_keys() -> bool:
    """True when the three Langfuse variables are set, after loading the key file for any that are missing."""
    read.load_env_file(Path.home() / ".config" / "backbone-take-home" / "langfuse.env")
    host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY") and host)


def dataset_of(result: dict) -> str:
    """The Langfuse dataset a result belongs to. It names one; results saved before they did are told apart by their keys."""
    return result.get("dataset") or (TIER_DATASET if "manifest" in result else ROUTING_DATASET)


def record(result: dict, name: str) -> str | None:
    """Mirror a result into Langfuse as a run on a dataset, so the run carries exactly the local scores. Returns the run name.

    The dataset (see dataset_of) is created when it does not exist, and holds one item per local item, upserted by id. Every
    arm or model that is recorded runs over the same items, so the Experiments view can compare the runs. The task of a run
    returns the output the local run stored, and one evaluator per local score returns that item's stored value, so nothing is
    computed again and no model is called. A score an item has no value for is not sent. Only fictional and synthetic records
    belong here. Returns None without keys, and never raises: a Langfuse failure is logged and the local result stays the record.
    """
    if not _langfuse_keys():
        return None
    try:
        from langfuse import Evaluation, get_client  # imported late: without keys nothing of Langfuse starts

        dataset_name = dataset_of(result)
        by_id = {item["id"]: item for item in result["items"]}

        def task(*, item, **_):  # item is the dataset item, whose id is the local item's
            return by_id[item.id]["output"]

        def evaluator(score_name: str):
            def evaluate(*, metadata, **_):
                item = by_id[metadata["id"]]
                value = item["scores"].get(score_name)
                return [] if value is None else Evaluation(name=score_name, value=value, comment=item.get("comment"))

            evaluate.__name__ = score_name
            return evaluate

        client = get_client()
        if not client.auth_check():
            raise RuntimeError("Langfuse is not set up to send")
        try:
            client.create_dataset(
                name=dataset_name, description=f"{dataset_name} items of evals/experiments.py; synthetic records only"
            )
        except Exception as error:  # noqa: BLE001 - the usual reason is that it exists; get_dataset below fails if it is not there
            logger.debug("create_dataset %s did not create it: %s", dataset_name, error)
        for item in result["items"]:
            client.create_dataset_item(
                dataset_name=dataset_name,
                id=item["id"],
                input=item["input"],
                expected_output=item["expected_output"],
                metadata={"id": item["id"], **item["metadata"]},
            )
        dataset = client.get_dataset(dataset_name)
        dataset.items = [item for item in dataset.items if item.id in by_id]  # the dataset also holds items other runs recorded
        if len(dataset.items) != len(by_id):
            raise RuntimeError(
                f"Langfuse dataset {dataset_name} lacks {len(by_id) - len(dataset.items)} of the items just upserted"
            )
        run_name = f"{name} {datetime.now(UTC):%Y-%m-%d %H:%M:%S}"
        dataset.run_experiment(
            name=run_name,
            run_name=run_name,
            description=f"Scores computed locally by evals/experiments.py: {', '.join(result['scores'])}",
            task=task,
            evaluators=[evaluator(score_name) for score_name in result["scores"]],
            max_concurrency=10,
            metadata={"arm": str(result["arm"])},
        )
        client.flush()
        return run_name
    except Exception as error:  # noqa: BLE001 - a Langfuse outage must not cost the scores that were just computed
        logger.warning("Langfuse recording failed; the local result is unaffected: %s", error)
        return None


# ---- Command line ----


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.experiments", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    routing_parser = commands.add_parser("routing", help="score how one arm plans the evaluation questions")
    routing_parser.add_argument("--arm", choices=ARMS, required=True)
    routing_parser.add_argument(
        "--db", type=Path, default=OUTPUTS / "abstraction.db", help="a store with every patient the questions name"
    )
    routing_parser.add_argument("--questions", type=Path, default=ROOT / "evals" / "questions.json")
    tier_parser = commands.add_parser("tier", help="score one extraction model on the synthetic patients")
    tier_parser.add_argument("--model", required=True)
    tier_parser.add_argument("--manifest", type=Path, default=EXPERIMENTS / "synthetic" / "manifest.json")
    tier_parser.add_argument("--patients", nargs="+", metavar="MRN", help="only these patients")
    tier_parser.add_argument(
        "--store", type=Path, help="score the abstractions in this store instead of ingesting (no model call)"
    )
    tier_parser.add_argument("--run", help="label for this run (default: the next run1, run2, ...)")
    tier_parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        if args.command == "routing":
            jev = None
            if args.arm != "rules":
                read.load_keys()
                jev = read.jev_client(OUTPUTS, read.default_budget(OUTPUTS))
                if jev is None:
                    raise ValueError("JEV_API_KEY is not set, and the jev and agreement arms need the decision model")
            result = routing(args.db, args.questions, args.arm, jev)
            name = f"routing-{args.arm}"
        else:
            budget = None
            if args.store is None:
                read.load_keys()
                if not os.environ.get("ANTHROPIC_API_KEY"):
                    raise ValueError(
                        "ANTHROPIC_API_KEY is not set, and an ingest run needs the extraction model (--store scores a finished run)"
                    )
                budget = read.default_budget(OUTPUTS)
            result = extraction_tier(
                args.manifest, args.model, budget, args.patients, store=args.store, workers=args.workers, run=args.run
            )
            name = f"tier-{args.model}-{result['run']}"
    except (FileNotFoundError, ValueError, read.BudgetExceeded) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if result.get("missing_from_store"):
        print(
            f"warning: {len(result['missing_from_store'])} patients are not in the store and scored 0 "
            f"({', '.join(result['missing_from_store'][:5])}, ...). Use --patients to score only the patients the run covered.",
            file=sys.stderr,
        )
    summary = {"file": _shown(EXPERIMENTS / f"{name}.json"), "scores": result["scores"], "langfuse_run": record(result, name)}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
