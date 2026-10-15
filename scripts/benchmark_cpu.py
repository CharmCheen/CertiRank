#!/usr/bin/env python3
"""Instrumented paired offline CPU microbenchmark. Timings are not deterministic."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.evaluation import run_algorithm
from certirank.experiment import load_config, config_hash
from certirank.oracle import TableSource
from certirank.synthetic import make_table

OUT = ROOT / "experiments/certirank_pilot/cpu_benchmark"


def code_hash():
    return config_hash({str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
                        for p in sorted([Path(__file__), *list((ROOT / "src/certirank").rglob("*.py"))])})


def benchmark(config):
    rows = []
    bench = config["benchmark"]
    for n in bench["sizes"]:
        source = TableSource(make_table(n, 3, bench["seed"] + n), context_id=f"synthetic-cpu-N{n}")
        for method in ("SimpleTopology", "TraceSensitivity"):
            for repetition in range(-bench["warmups"], bench["repetitions"]):
                results = {}
                modes = ("incremental", "cached_replay") if repetition % 2 else ("cached_replay", "incremental")
                for mode in modes:
                    results[mode] = run_algorithm(source, n, config["k"], (0, 1, 2), method,
                        config["primary_budget"], mode=mode, seed=bench["seed"], measure_cpu=True)
                for key in ("topk", "status", "logical_calls", "selected_pairs", "ledger"):
                    if results["incremental"][key] != results["cached_replay"][key]:
                        raise AssertionError(f"repair mismatch: {key}")
                if repetition >= 0:
                    for mode, run in results.items():
                        rows.append({"n": n, "method": method, "mode": mode, "trial": repetition,
                            "status": run["status"], "logical_calls": run["logical_calls"],
                            "cpu_ns": run["cpu_ns"], "node_evaluations": run["executor_node_evaluations_including_aborted"]})
    summaries = []
    for n in bench["sizes"]:
        for method in ("SimpleTopology", "TraceSensitivity"):
            selected = [r for r in rows if r["n"] == n and r["method"] == method]
            means = {mode: statistics.mean(r["cpu_ns"]["total"] for r in selected if r["mode"] == mode)
                     for mode in ("incremental", "cached_replay")}
            phases = {mode: {key: statistics.mean(r["cpu_ns"].get(key, 0) for r in selected if r["mode"] == mode)
                for key in ("state_copy", "invalidation", "initial_update", "trace_build", "extraction", "policy", "probe")}
                for mode in means}
            summaries.append({"n": n, "method": method, "mean_total_cpu_ns": means, "mean_phase_cpu_ns": phases,
                              "paired_mean_reduction": 1 - means["incremental"] / means["cached_replay"]})
    return {"settings_hash": config_hash(config), "code_hash": code_hash(), "source_kind": "synthetic",
            "python": platform.python_version(), "platform": platform.platform(), "rows": rows, "summary": summaries,
            "limitations": "instrumented repeated CPU microbenchmark; no provider latency, no independent-query performance claim",
            "new_physical_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/experiment.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    path = OUT / "measurements.json"
    if args.check:
        if not path.exists():
            print("FAIL missing CPU measurements")
            return 1
        result = json.loads(path.read_text())
        valid = (result["settings_hash"] == config_hash(config) and result["code_hash"] == code_hash()
                 and len(result["rows"]) == len(config["benchmark"]["sizes"]) * 2 * 2 * config["benchmark"]["repetitions"]
                 and all(r["cpu_ns"]["total"] > 0 and sum(v for k, v in r["cpu_ns"].items() if k != "total") <= r["cpu_ns"]["total"] for r in result["rows"]))
        print("PASS CPU record provenance/schema (timings not replayed)" if valid else "FAIL stale/invalid CPU measurements")
        return 0 if valid else 1
    result = benchmark(config)
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    report = ["# 合成数据 CPU 微基准", "", "记录选边、复制、失效定位、节点更新、轨迹维护、提取和复核 CPU；包含中止工作。",
              "计时有 instrumentation 开销，两种修复模式使用相同测量条件并交错运行。结果仅描述本机合成微基准。", "",
              "| N | 策略 | incremental 平均 CPU ms | cached replay 平均 CPU ms | 相对降低 |",
              "|---|---|---:|---:|---:|"]
    for row in result["summary"]:
        means = row["mean_total_cpu_ns"]
        report.append(f"| {row['n']} | {row['method']} | {means['incremental']/1e6:.3f} | {means['cached_replay']/1e6:.3f} | {row['paired_mean_reduction']:.1%} |")
    report.extend(["", "未测真实 API/端到端服务延迟，未据此宣布 H3 通过。节点求值数不作为 CPU 收益的替代指标。", ""])
    (OUT / "report.md").write_text("\n".join(report))
    print("wrote measured CPU microbenchmark; zero provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
