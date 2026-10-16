"""Offline campaign runner: split isolation, development fitting and group inference."""

from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import random
import statistics

from certirank.evaluation import run_algorithm, summarize_disagreement
from certirank.experiment import config_hash, frozen_settings
from certirank.oracle import TableSource
from certirank.pair_pool import load_jsonl, load_workload, validate_pair_calls, build_pair_pool
from certirank.research_diagnostics import fit_review_value, recall
from certirank.value_model import ReviewValueModel


def manifest_hash(manifest):
    return config_hash(manifest)


def validate_split_manifest(manifest):
    if manifest.get("schema_version") != 1 or manifest.get("source_kind") not in ("real", "synthetic"):
        raise ValueError("unsupported pool manifest")
    rows = manifest["query_groups"]
    if len({row["query_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate query ID in split manifest")
    group_splits = defaultdict(set)
    for row in rows:
        if row["split"] not in ("dev", "heldout") or not row.get("query_group") or not row.get("label_source"):
            raise ValueError("split/group/independent label provenance is required")
        group_splits[row["query_group"]].add(row["split"])
    if any(len(splits) != 1 for splits in group_splits.values()):
        raise ValueError("a query-group crosses development/heldout splits")


def implementation_hash():
    paths = sorted(Path(__file__).parent.rglob("*.py"))
    runner = Path(__file__).resolve().parents[2] / "scripts/run_experiment.py"
    if runner.exists():
        paths.append(runner)
    return config_hash({path.relative_to(Path(__file__).resolve().parents[2]).as_posix(): sha256(path.read_bytes()).hexdigest() for path in paths})


def validate_freeze(config, manifest, freeze, model):
    validate_split_manifest(manifest)
    if freeze.get("implementation_hash") != implementation_hash():
        raise ValueError("implementation changed after method freeze")
    if freeze.get("decision") != "proceed" or freeze.get("settings_hash") != config_hash(frozen_settings(config)):
        raise ValueError("heldout run requires an explicit proceed decision and matching frozen settings")
    if freeze.get("manifest_hash") != manifest_hash(manifest):
        raise ValueError("split/data manifest changed after method freeze")
    if freeze.get("model_hash") != config_hash(model.to_dict()):
        raise ValueError("development model changed after freeze")
    heldout = {row["query_group"] for row in manifest["query_groups"] if row["split"] == "heldout"}
    dev = {row["query_group"] for row in manifest["query_groups"] if row["split"] == "dev"}
    if len(heldout) < config["formal"]["minimum_heldout_groups"]:
        raise ValueError("insufficient heldout query groups")
    if set(model.training_query_groups) != dev or heldout & set(model.training_query_groups):
        raise ValueError("model training groups do not match the isolated development partition")
    if model.settings_hash != config_hash(frozen_settings(config)):
        raise ValueError("model was fitted under different settings")
    if freeze.get("comparator") not in ("Random", "SimpleTopology") or freeze.get("candidate") != config["candidate_method"]:
        raise ValueError("unfrozen comparator/candidate")


def load_cases(manifest_path, config, split, *, freeze=None, model=None):
    """Only read the selected pool; heldout validation precedes any heldout file read."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_split_manifest(manifest)
    if split == "heldout":
        if freeze is None or model is None:
            raise ValueError("heldout data remains sealed without freeze and development model")
        validate_freeze(config, manifest, freeze, model)
    selected = [row for row in manifest["query_groups"] if row["split"] == split]
    if split == "heldout" and manifest["dataset_id"] == config["historical"]["dataset_id"]:
        exposed = set(config["historical"]["exposed_queries"])
        if any(row["query_id"] in exposed or row["query_group"] in exposed for row in selected):
            raise ValueError("historical development queries cannot become heldout")
    if not selected:
        raise ValueError("empty selected partition")
    pool_spec = manifest["pools"][split]
    paths = {}
    for name in ("workload", "pair_calls", "identity"):
        path = manifest_path.parent / pool_spec[name]
        if sha256(path.read_bytes()).hexdigest() != pool_spec["sha256"][name]:
            raise ValueError(f"pool hash mismatch: {name}")
        paths[name] = path
    workload = load_workload(paths["workload"])
    calls = load_jsonl(paths["pair_calls"])
    identity = json.loads(paths["identity"].read_text(encoding="utf-8"))
    reps = config["formal"]["response_count"]
    validation = validate_pair_calls(calls, workload, expected_repetitions=reps, expected_identity=identity)
    if not validation["contract_ok"]:
        raise ValueError(validation["issues"])
    if set(map(str, workload["query_ids"])) != {row["query_id"] for row in selected}:
        raise ValueError("pool contains queries outside the selected partition")
    if workload["k"] != config["k"]:
        raise ValueError("pool K differs from experiment settings")
    metadata = {row["query_id"]: row for row in selected}
    pool = build_pair_pool(calls)
    cases = []
    for entry in workload["entries"]:
        q = str(entry["query_id"])
        docs = entry["doc_ids"]
        if len(docs) != config["formal"]["n"]:
            raise ValueError("pool candidate count differs from frozen N")
        relevant = {i for i, doc in enumerate(docs) if entry["qrels"].get(doc, 0) > 0}
        if not relevant:
            raise ValueError("independent task labels are missing")
        table = {(p["position_i"], p["position_j"]): [pool[q]["verdicts"][p["pair_position"]][r] for r in range(reps)]
                 for p in entry["pairs"]}
        context = f"{manifest['dataset_id']}:{q}:{manifest_hash(pool_spec)}"
        cases.append({**metadata[q], "source": TableSource(table, context_id=context), "n": len(docs),
                      "k": config["k"], "relevant": relevant, "source_kind": manifest["source_kind"]})
    return manifest, cases


def run_cases(cases, config, *, model=None):
    groups = config["formal"]["slot_groups"]
    if model is not None and model.settings_hash != config_hash(frozen_settings(config)):
        raise ValueError("value model settings differ from the campaign")
    methods = list(config["methods"])
    if model is not None and config["candidate_method"] not in methods:
        methods.append(config["candidate_method"])
    rows, query_metrics = [], []
    for case in cases:
        if model is not None and case["split"] == "heldout" and (
                case["query_group"] in model.training_query_groups or case["source"].source_id in model.training_source_ids):
            raise ValueError("heldout query/source leaked into model fitting")
        for budget in config["budgets"]:
            for method in methods:
                executions = []
                for index, group in enumerate(groups):
                    run = run_algorithm(case["source"], case["n"], case["k"], tuple(group), method, budget,
                                        seed=config["seed"] + index, query_id=case["query_id"], model=model, mode=config["policy_repair_mode"])
                    run.update(query_group=case["query_group"], split=case["split"], source_kind=case["source_kind"],
                               settings_hash=config_hash(config), recall_at_k=recall(run["topk"], case["relevant"]) if run["topk"] is not None else None)
                    executions.append(run)
                    rows.append(run)
                metrics = summarize_disagreement(executions)
                qualities = [run["recall_at_k"] for run in executions]
                query_metrics.append({"query_id": case["query_id"], "query_group": case["query_group"],
                    "split": case["split"], "method": method, "budget": budget, **metrics,
                    "mean_recall": statistics.mean(qualities) if all(q is not None for q in qualities) else None,
                    "mean_calls": statistics.mean(r["logical_calls"] for r in executions),
                    "aborted_rate": sum(r["status"] == "aborted" for r in executions) / len(executions)})
    return rows, query_metrics


def paired_group_comparison(query_metrics, candidate, comparator, budget, *, bootstrap_samples=2000, seed=17):
    by_query = defaultdict(dict)
    for row in query_metrics:
        if row["budget"] == budget and row["method"] in (candidate, comparator):
            by_query[(row["query_group"], row["query_id"])][row["method"]] = row
    grouped = defaultdict(list)
    for (group, query), methods in by_query.items():
        if set(methods) != {candidate, comparator}:
            raise ValueError("paired comparison has a missing method")
        a, b = methods[candidate], methods[comparator]
        if any(r[key] is None for r in (a, b) for key in ("set_d_em", "jaccard_distance", "mean_recall")):
            return {"status": "inconclusive", "reason": "failed execution or missing quality retained; no success-only estimate"}
        grouped[group].append({"query_id": query, "gain": b["set_d_em"] - a["set_d_em"],
            "quality_delta": a["mean_recall"] - b["mean_recall"], "baseline_d": b["set_d_em"],
            "candidate_calls": a["mean_calls"], "comparator_calls": b["mean_calls"]})
    if not grouped:
        return {"status": "inconclusive", "reason": "no paired query groups"}
    values = [{key: statistics.mean(r[key] for r in rows) for key in rows[0] if key != "query_id"}
              for _, rows in sorted(grouped.items())]
    point = {key: statistics.mean(v[key] for v in values) for key in values[0]}
    if len(values) < 2:
        return {"status": "inconclusive", "point": point, "reason": "fewer than two independent query groups"}
    rng = random.Random(seed)
    distribution = {"gain": [], "quality_delta": []}
    for _ in range(bootstrap_samples):
        sample = [rng.choice(values) for _ in values]
        for key in distribution:
            distribution[key].append(statistics.mean(row[key] for row in sample))
    intervals = {key: [sorted(vals)[int(0.025 * len(vals))], sorted(vals)[min(len(vals) - 1, int(0.975 * len(vals)))]]
                 for key, vals in distribution.items()}
    return {"status": "estimated", "unit": "query-group; variants averaged within group first",
            "groups": len(values), "queries": len(by_query), "point": point, "bootstrap_95": intervals,
            "relative_gain": point["gain"] / point["baseline_d"] if point["baseline_d"] else None,
            "positive_groups": sum(value["gain"] > 0 for value in values), "per_query_group": dict(grouped)}


def make_freeze(config, manifest, model, query_metrics, *, decision):
    if decision != "proceed":
        raise ValueError("an explicit week-one proceed decision is required")
    candidates = []
    for method in ("Random", "SimpleTopology"):
        metrics = [r for r in query_metrics if r["method"] == method and r["budget"] == config["primary_budget"]]
        if not metrics or any(r["set_d_em"] is None for r in metrics):
            raise ValueError("development comparison is incomplete")
        groups = defaultdict(list)
        for row in metrics:
            groups[row["query_group"]].append(row)
        if len(groups) < config["formal"]["minimum_dev_groups"]:
            raise ValueError("insufficient independent development query groups for freeze")
        candidates.append((statistics.mean(statistics.mean(r["set_d_em"] for r in rows) for rows in groups.values()),
                           statistics.mean(r["mean_calls"] for r in metrics), method))
    comparator = min(candidates)[2]
    freeze = {"schema_version": 1, "decision": decision, "implementation_hash": implementation_hash(),
              "settings_hash": config_hash(frozen_settings(config)), "manifest_hash": manifest_hash(manifest),
              "model_hash": config_hash(model.to_dict()), "candidate": config["candidate_method"], "comparator": comparator}
    validate_freeze(config, manifest, freeze, model)
    return freeze


def research_verdict(comparison, config, *, split, source_kind):
    if comparison.get("status") != "estimated":
        return {"verdict": "inconclusive", "reason": comparison.get("reason")}
    gates = config["gates"]
    point = comparison["point"]
    point_pass = (point["gain"] >= gates["absolute_disagreement_reduction"]
        and comparison["relative_gain"] is not None and comparison["relative_gain"] >= gates["relative_disagreement_reduction"]
        and point["quality_delta"] >= -gates["maximum_quality_drop"] and comparison["positive_groups"] >= 2)
    interval_pass = (comparison["bootstrap_95"]["gain"][0] > 0
                     and comparison["bootstrap_95"]["quality_delta"][0] >= -gates["maximum_quality_drop"])
    enough = comparison["groups"] >= config["formal"]["minimum_heldout_groups"]
    verdict = "synthetic_only" if source_kind == "synthetic" else "development_only" if split == "dev" else (
        "heldout_support_within_sample" if point_pass and interval_pass and enough else "exploratory_or_gate_not_met")
    return {"verdict": verdict, "point_gates_pass": point_pass, "intervals_support": interval_pass,
            "enough_query_groups": enough, "claim_beyond_confidence": False, "new_physical_calls": 0}
