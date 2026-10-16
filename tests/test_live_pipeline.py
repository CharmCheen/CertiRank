"""End-to-end: live-shaped acquisition -> pool manifest -> offline experiment."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from certirank.live import build_workload, collect_pair_calls, write_acquisition, build_manifest
from test_live_collect import FakeClient, sample_queries


def test_pipeline_collect_manifest_and_run(tmp_path):
    api_params = {"temperature_requested": 0.0, "thinking_mode_requested": "low", "max_tokens_requested": 128}
    pools = {}
    groups = []
    for split in ("dev", "heldout"):
        prefixed = []
        for q in sample_queries():
            prefixed.append({**q, "query_id": f"{split}-{q['query_id']}"})
        workload = build_workload(
            prefixed,
            experiment_id="CertiRank|live",
            condition=f"live|{split}",
            model="demo-model",
            model_parameters=api_params,
            k=1,
        )
        rows = collect_pair_calls(workload, client=FakeClient(), repetitions=6, run_prefix=split)
        identity = {
            "experiment_id": workload["experiment_id"],
            "condition": workload["condition"],
            "requested_model": workload["requested_model"],
            "model_parameters": workload["model_parameters"],
        }
        out = tmp_path / split
        paths = write_acquisition(out, workload=workload, rows=rows, identity=identity)
        pools[split] = {k: paths[k] for k in ("workload", "pair_calls", "identity")}
        for q in prefixed:
            groups.append({"query_id": q["query_id"], "query_group": f"{split}-g-{q['query_id']}", "split": split,
                           "label_source": "synthetic truth"})

    manifest_path = tmp_path / "pool_manifest.json"
    manifest = build_manifest(dataset_id="synthetic-fixture", source_kind="synthetic", query_groups=groups, pools=pools)
    base = manifest_path.parent
    for split, files in pools.items():
        entry = {"sha256": manifest["pools"][split]["sha256"]}
        for name, path in files.items():
            entry[name] = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        manifest["pools"][split] = entry
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    config = json.loads((ROOT / "configs" / "experiment.json").read_text(encoding="utf-8"))
    config.update(k=1, budgets=["2"], primary_budget="2")
    config["formal"].update(n=3, response_count=6, slot_groups=[[0, 1, 2], [3, 4, 5]],
                            minimum_dev_groups=2, minimum_heldout_groups=2, bootstrap_samples=16)
    config_path = tmp_path / "experiment.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    dev_out = tmp_path / "dev_results"
    test_out = tmp_path / "heldout_results"
    entry = [sys.executable, "-B", str(ROOT / "scripts/run_experiment.py"),
             "--config", str(config_path), "--manifest", str(manifest_path)]
    dev = subprocess.run(entry + ["--split", "dev", "--out", str(dev_out), "--create-freeze", "--decision", "proceed"],
                         capture_output=True, text=True)
    assert dev.returncode == 0, dev.stderr
    heldout = subprocess.run(entry + ["--split", "heldout", "--out", str(test_out),
                                      "--model", str(dev_out / "value_model.json"),
                                      "--freeze", str(dev_out / "freeze_manifest.json")],
                             capture_output=True, text=True)
    assert heldout.returncode == 0, heldout.stderr
    summary = json.loads((test_out / "summary.json").read_text(encoding="utf-8"))
    assert summary["source_kind"] == "synthetic"
    assert summary["new_physical_calls"] == 0
