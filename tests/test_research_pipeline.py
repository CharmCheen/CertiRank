"""Research alignment: actual voting value, feasibility, split isolation, inference."""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from certirank.campaign import (load_cases, run_cases, make_freeze, validate_freeze,
                               paired_group_comparison, research_verdict)
from certirank.evaluation import run_algorithm
from certirank.experiment import load_config, balanced_triples, config_hash, frozen_settings
from certirank.oracle import TableSource
from certirank.research_diagnostics import budgeted_oracle, fit_review_value


def small_config():
    config = load_config(ROOT / "configs/experiment.json")
    config.update(k=1, budgets=["2"], primary_budget="2")
    config["formal"].update(n=2, response_count=6, slot_groups=[[0, 1, 2], [3, 4, 5]],
                           minimum_dev_groups=2, minimum_heldout_groups=2, bootstrap_samples=64)
    return config


def make_pool_manifest(tmp_path):
    config = small_config()
    metadata, pools = [], {}
    for split in ("dev", "heldout"):
        entries, calls = [], []
        ids = [f"{split}-{i}" for i in range(2)]
        identity = {"experiment_id": "synthetic|P0", "condition": "P0", "requested_model": "scripted",
                    "model_parameters": {"max_tokens_requested": 128}}
        for q in ids:
            docs = [q + "-a", q + "-b"]
            pair = {"pair_position": 0, "position_i": 0, "position_j": 1, "document_1_id": docs[0],
                    "document_2_id": docs[1], "pair_id": q + "|a|b", "request_hash": "hash-" + q}
            entries.append({"query_id": q, "doc_ids": docs, "pairs": [pair], "qrels": {docs[0]: 1}})
            metadata.append({"query_id": q, "query_group": q, "split": split, "label_source": "synthetic truth"})
            for rep, verdict in enumerate([True, False, False, False, True, True]):
                calls.append({**identity, **pair, "query_id": q, "run_id": q + f"-r{rep}", "repetition_id": rep,
                    "document_order": list(docs), "provider_request_id": q + f"-p{rep}",
                    "timestamp_utc": "2026-10-15T00:00:00+00:00", "strict_parser_status": "valid",
                    "raw_response_content": "Document 1" if verdict else "Document 2",
                    "strict_parsed_decision": verdict, "lotus_parsed_decision": verdict})
        workload = {"query_ids": ids, "pairs_per_query": 1, "k": 1, "condition": "P0", "entries": entries}
        values = {"workload": json.dumps(workload), "pair_calls": "".join(json.dumps(row) + "\n" for row in calls),
                  "identity": json.dumps(identity)}
        spec = {"sha256": {}}
        for name, value in values.items():
            file = tmp_path / f"{split}_{name}.json"
            file.write_text(value, encoding="utf-8")
            spec[name] = file.name
            spec["sha256"][name] = sha256(file.read_bytes()).hexdigest()
        pools[split] = spec
    manifest = {"schema_version": 1, "dataset_id": "synthetic-fixture", "source_kind": "synthetic",
                "query_groups": metadata, "pools": pools}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return config, path, manifest


def test_balanced_roles_are_equal_and_still_correlated():
    groups = balanced_triples(5)
    assert len(groups) == 30
    assert Counter(g[0] for g in groups) == {i: 6 for i in range(5)}
    assert all(len(set(group)) == 3 for group in groups)


def test_oracle_uses_actual_votes_and_repairs_under_budget():
    source = TableSource({(0, 1): [True, False, False, False, True, True]})
    result = budgeted_oracle(source, 2, 1, ((0, 1, 2), (3, 4, 5)), "2", {0}, state_cap=20)
    assert result["search_complete"]
    assert result["base"]["set_d_em"] == 1 and result["oracle"]["set_d_em"] == 0
    assert all(row["extra_calls"] <= 2 for row in result["oracle"]["selected"])
    capped = budgeted_oracle(source, 2, 1, ((0, 1, 2), (3, 4, 5)), "2", {0}, state_cap=1)
    assert not capped["search_complete"]  # cannot use this as a global upper bound
    assert capped["oracle"]["set_d_em"] == 1


def test_budget_feasibility_distinguishes_vote_from_new_branch():
    source = TableSource({(0, 1): [True] * 3, (2, 3): [True, False, False],
                          (0, 2): [True] * 3, (0, 3): [False] * 3})
    a = run_algorithm(source, 4, 1, (0, 1, 2), "OracleSchedule", "0.4", schedule=((2, 3),), absolute_budget=2)
    b = run_algorithm(source, 4, 1, (0, 1, 2), "OracleSchedule", "0.4", schedule=((2, 3),), absolute_budget=3)
    assert a["reviews"][0]["decision_changed"] and a["reviews"][0]["new_branch_needed"]
    assert a["status"] == "aborted" and a["extra_calls"] == 2
    assert b["status"] == "complete" and b["extra_calls"] == 3 and b["reviews"][0]["repair_calls"] == 1


def test_config_budget_changes_actual_historical_runs(tmp_path):
    import analyze_offline
    config = load_config(ROOT / "configs/experiment.json")
    config.update(budgets=["0.3"], primary_budget="0.3")
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    artifacts = analyze_offline.analyze(path)
    rows = [json.loads(line) for line in artifacts["strategy_smoke.jsonl"].splitlines()]
    assert {row["budget_fraction"] for row in rows} == {"0.3"}
    assert all(row["budget_limit"] == 3 for row in rows if row["method"] in ("Random", "SimpleTopology", "TraceSensitivity"))


def test_heldout_refusal_precedes_any_pool_read(tmp_path, monkeypatch):
    config, path, _ = make_pool_manifest(tmp_path)
    def forbidden(self):
        pytest.fail("heldout pool read before method freeze")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(ValueError, match="sealed"):
        load_cases(path, config, "heldout")


def test_development_fit_freeze_and_heldout_end_to_end(tmp_path):
    config, path, manifest = make_pool_manifest(tmp_path)
    _, dev = load_cases(path, config, "dev")
    model = fit_review_value(dev, config["formal"]["slot_groups"], config["primary_budget"], config_hash(frozen_settings(config)))
    _, metrics = run_cases(dev, config, model=model)
    freeze = make_freeze(config, manifest, model, metrics, decision="proceed")
    _, heldout = load_cases(path, config, "heldout", freeze=freeze, model=model)
    rows, metrics = run_cases(heldout, config, model=model)
    assert len(rows) == 2 * 2 * 6
    comparison = paired_group_comparison(metrics, "ReviewValue", freeze["comparator"], "2", bootstrap_samples=64)
    assert comparison["groups"] == 2
    # A positive single-step proxy does not guarantee a complete-policy gain.
    assert comparison["point"]["gain"] == 0
    assert research_verdict(comparison, config, split="heldout", source_kind="synthetic")["verdict"] == "synthetic_only"
    changed = deepcopy(config)
    changed["seed"] += 1
    with pytest.raises(ValueError, match="settings"):
        validate_freeze(changed, manifest, freeze, model)
    with pytest.raises(ValueError, match="heldout"):
        fit_review_value(heldout, config["formal"]["slot_groups"], "2", "bad")


def test_group_bootstrap_averages_variants_before_groups():
    metrics = []
    for group, queries, gain in (("g1", ["a", "b", "c"], 0), ("g2", ["d"], 1)):
        for query in queries:
            for method, d in (("ReviewValue", 0), ("SimpleTopology", gain)):
                metrics.append({"query_id": query, "query_group": group, "method": method, "budget": "0.4",
                                "set_d_em": d, "jaccard_distance": d, "mean_recall": 1, "mean_calls": 10})
    result = paired_group_comparison(metrics, "ReviewValue", "SimpleTopology", "0.4", bootstrap_samples=64)
    assert result["point"]["gain"] == 0.5  # query-average would incorrectly yield 0.25


def test_cpu_measurement_includes_selection_and_maintenance():
    source = TableSource({(0, 1): [True, False, False]})
    run = run_algorithm(source, 2, 1, (0, 1, 2), "SimpleTopology", "2", measure_cpu=True)
    clocks = run["cpu_ns"]
    assert all(key in clocks and clocks[key] >= 0 for key in ("policy", "state_copy", "probe", "initial_update", "extraction"))
    # process_time_ns may quantize to ~15ms on some platforms; a tiny N=2 run can measure 0.
    assert clocks["total"] >= 0
    assert sum(value for key, value in clocks.items() if key != "total") <= clocks["total"]
    assert run_algorithm(source, 2, 1, (0, 1, 2), "Base", "2")["cpu_ns"] is None


def test_missing_value_model_is_rejected_before_any_execution():
    with pytest.raises(ValueError, match="unavailable"):
        run_algorithm(TableSource({}), 2, 1, (0, 1, 2), "ReviewValue", "0.4")


def test_unified_cli_development_and_frozen_heldout(tmp_path):
    config, path, _ = make_pool_manifest(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    entry = [sys.executable, "-B", str(ROOT / "scripts/run_experiment.py"), "--config", str(config_path), "--manifest", str(path)]
    dev_out, test_out = tmp_path / "dev_results", tmp_path / "heldout_results"
    dev = subprocess.run(entry + ["--split", "dev", "--out", str(dev_out), "--create-freeze", "--decision", "proceed"], capture_output=True, text=True)
    assert dev.returncode == 0, dev.stderr
    heldout = subprocess.run(entry + ["--split", "heldout", "--out", str(test_out), "--model", str(dev_out / "value_model.json"),
                                     "--freeze", str(dev_out / "freeze_manifest.json")], capture_output=True, text=True)
    assert heldout.returncode == 0, heldout.stderr
    summary = json.loads((test_out / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"]["verdict"] == "synthetic_only"
    assert summary["new_physical_calls"] == 0


def test_no_bootstrap_interval_is_claimed_from_one_query_group():
    metrics = [{"query_id": "q", "query_group": "g", "method": method, "budget": "0.4",
                "set_d_em": value, "jaccard_distance": value, "mean_recall": 1, "mean_calls": 10}
               for method, value in (("ReviewValue", 0), ("SimpleTopology", 1))]
    result = paired_group_comparison(metrics, "ReviewValue", "SimpleTopology", "0.4", bootstrap_samples=64)
    assert result["status"] == "inconclusive" and "bootstrap_95" not in result


def test_budget_estimator_uses_the_collection_config(tmp_path):
    import pilot_budget
    config = load_config(ROOT / "configs/experiment.json")
    config["collection"]["dev_query_groups"] = 8
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    result = json.loads(pilot_budget.build_artifacts(path)["budget_estimate.json"])
    assert result["blocks"][0]["calls"] == 8 * 28 * 12


def test_value_predictions_are_logged_for_calibration(tmp_path):
    config, path, _ = make_pool_manifest(tmp_path)
    _, cases = load_cases(path, config, "dev")
    model = fit_review_value(cases, config["formal"]["slot_groups"], "2", config_hash(frozen_settings(config)))
    rows, _ = run_cases(cases, config, model=model)
    predictions = [review["value_estimate"] for row in rows if row["method"] == "ReviewValue" for review in row["reviews"]]
    assert predictions and all(p["p_vote_changed"] == 1 and p["estimated_cost"] == 2 for p in predictions)


def test_query_group_variants_cannot_cross_partitions(tmp_path):
    config, path, manifest = make_pool_manifest(tmp_path)
    manifest["query_groups"][-1]["query_group"] = manifest["query_groups"][0]["query_group"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="crosses"):
        load_cases(path, config, "dev")
