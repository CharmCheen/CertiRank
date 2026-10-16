#!/usr/bin/env python3
"""Recompute the historical Tournament phenomenon, with no provider access.

--check rebuilds every artifact in memory and verifies stored outputs byte-for-byte.
The 3 exposed historical queries are development diagnostics, never a holdout set.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from itertools import combinations
import csv
import io
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from certirank.diagnostics import conditional_effect
from certirank.experiment import load_config, config_hash, balanced_triples
from certirank.research_diagnostics import budgeted_oracle
from certirank.evaluation import METHODS, run_algorithm, summarize_disagreement
from certirank.operators.metrics import jaccard_at_k
from certirank.operators.naive_sort import naive_replay, single_point_counterfactuals
from certirank.operators.tournament import tournament
from certirank.oracle import ExecutionLedger, TableSource
from certirank.pair_pool import build_pair_pool, load_jsonl, load_workload, validate_pair_calls
from make_data_manifest import verify

OUT = ROOT / "experiments" / "certirank_pilot" / "analysis"


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def analyze(config_path=None) -> dict[str, str]:
    config_path = Path(config_path) if config_path else ROOT / "configs/experiment.json"
    config = load_config(config_path)
    historical = config["historical"]
    repetitions = historical["response_count"]
    baseline_slots = historical["baseline_slots"]
    budgets, methods = config["budgets"], config["methods"]
    manifest = json.loads((ROOT / "data/manifests/gate2_external_data.json").read_text(encoding="utf-8"))
    problems = verify(manifest)
    if problems:
        raise ValueError(problems)
    data = ROOT / "data/external/lotus_gate2"
    workload = load_workload(data / "gate2_workload.json")
    calls = load_jsonl(data / "gate2_live_pair_calls.jsonl")
    identity = json.loads((ROOT / "configs/gate2_identity.json").read_text(encoding="utf-8"))
    if workload["repetitions"] != repetitions or workload["k"] != config["k"]:
        raise ValueError("experiment config does not match the frozen historical workload")
    validation = validate_pair_calls(calls, workload, expected_repetitions=repetitions, expected_identity=identity)
    if not validation["contract_ok"]:
        raise ValueError(validation["issues"])
    pool = build_pair_pool(calls)
    runs, counterfactuals, rows, smoke, subsets = [], [], [], [], []
    oracle_rows = []
    for entry in workload["entries"]:
        q = str(entry["query_id"])
        docs, k = entry["doc_ids"], workload["k"]
        pairs = [(p["position_i"], p["position_j"]) for p in entry["pairs"]]
        table = {pair: [pool[q]["verdicts"][pos][r] for r in range(repetitions)] for pos, pair in enumerate(pairs)}
        identity_hash = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        source = TableSource(table, context_id=f"Gate2:{q}:{entry['candidate_set_hash']}:{identity_hash}")
        for method in methods:
            for fraction in budgets:
                subset_runs = [run_algorithm(source, len(docs), k, group, method, fraction, seed=config["seed"], query_id=q)
                               for group in (balanced_triples(repetitions) if historical["balanced_sensitivity"] else list(combinations(range(repetitions), 3)))]
                subsets.append({
                    "query_id": q, "method": method, "budget_fraction": fraction,
                    "scope": "balanced initial-slot assignments; correlated sensitivity only",
                    "initial_slot_counts": dict(Counter(r["slots"][0] for r in subset_runs)),
                    "distinct_topk_sets": len({tuple(sorted(r["topk"])) for r in subset_runs}),
                    "logical_calls_min": min(r["logical_calls"] for r in subset_runs),
                    "logical_calls_max": max(r["logical_calls"] for r in subset_runs),
                    "aborted_combinations": sum(r["status"] == "aborted" for r in subset_runs),
                    "independent_runs": None, "set_d_em": None,
                    "combinations": [{key: r[key] for key in ("slots", "topk", "status", "logical_calls", "extra_calls")}
                                     for r in subset_runs],
                })
        for fraction in budgets:
            for method in methods:
                modes = ("incremental", "cached_replay") if method not in ("Base", "Vote3") else ("incremental",)
                comparisons = []
                for mode in modes:
                    run = run_algorithm(source, len(docs), k, tuple(historical["smoke_group"]), method, fraction, mode=mode, seed=config["seed"], query_id=q)
                    run["disagreement"] = summarize_disagreement([run])
                    run["query_id"] = q
                    run["scope"] = "single historical response group; engineering smoke only"
                    smoke.append(run)
                    comparisons.append(run)
                if len(comparisons) == 2:
                    for key in ("topk", "status", "logical_calls", "selected_pairs", "ledger"):
                        assert comparisons[0][key] == comparisons[1][key], (q, method, key)
        relevant_indices = {i for i, doc in enumerate(docs) if entry["qrels"].get(doc, 0) > 0}
        for fraction in budgets:
            oracle = budgeted_oracle(source, len(docs), k, (tuple(historical["smoke_group"]),), fraction,
                relevant_indices, state_cap=config["diagnostics"]["oracle_state_cap"],
                joint_cap=config["diagnostics"]["oracle_joint_cap"],
                quality_tolerance=config["gates"]["maximum_quality_drop"])
            oracle_rows.append({"query_id": q, "fraction": fraction, **oracle})
        tops = {"Tournament": [], "naive": []}
        counts = Counter()
        call_counts = []
        recalls = {"Tournament": [], "naive": []}
        for rep in baseline_slots:
            ledger = ExecutionLedger(source, (rep,))
            result = tournament(len(docs), k, ledger.observe)
            repeated = tournament(len(docs), k, ledger.observe)
            assert repeated.topk == result.topk
            observed = dict(ledger.visible)
            matrix = {pair: values[rep] for pair, values in table.items()}
            top = [docs[i] for i in result.topk]
            tops["Tournament"].append(top)
            naive = naive_replay([matrix[p] for p in pairs], len(docs))
            tops["naive"].append([docs[i] for i in naive["indexes"][:k]])
            relevant = {d for d, rel in entry["qrels"].items() if rel > 0}
            recall = len(set(top) & relevant) / len(relevant) if relevant else None
            recalls["Tournament"].append(recall)
            naive_top = tops["naive"][-1]
            recalls["naive"].append(len(set(naive_top) & relevant) / len(relevant) if relevant else None)
            call_counts.append(ledger.base_calls)
            runs.append({"query_id": q, "response_slot": rep, "method": "Tournament-Base",
                         "topk_doc_ids": top, "recall_at_3": recall,
                         "logical_calls": ledger.base_calls, "physical_calls_this_analysis": 0,
                         "logical_trace_comparisons": len(result.events),
                         "events": [asdict(e) for e in result.events],
                         "ledger": [asdict(e) for e in ledger.events]})
            for pair in observed:
                conditional = conditional_effect(result, observed, pair)
                changed = dict(matrix)
                changed[pair] = not changed[pair]
                new_pairs = set()

                def compare(i, j):
                    key = (i, j)
                    if key not in observed:
                        new_pairs.add(key)
                    return changed[key]

                counterfactual = tournament(len(docs), k, compare)
                impact = set(counterfactual.topk) != set(result.topk)
                status = conditional["status"]
                if status != "unresolved":
                    assert (status == "conditional_impact") == impact
                counts[status] += 1
                counts["oracle_set_impact"] += impact
                counts["unknown_and_oracle_impact"] += status == "unresolved" and impact
                counts["observed_requests"] += 1
                counts["new_requests_sum"] += len(new_pairs)
                counterfactuals.append({
                    "query_id": q, "response_slot": rep, "flipped_pair": pair,
                    **conditional, "oracle_set_impact": impact,
                    "oracle_new_pairs": sorted(new_pairs), "oracle_new_request_count": len(new_pairs),
                    "scope": "single observed request decision; other decisions frozen",
                })
        for method, topks in tops.items():
            combos = list(combinations(topks, 2))
            d_em = statistics.mean(set(a) != set(b) for a, b in combos)
            d_j = statistics.mean(1 - jaccard_at_k(a, b) for a, b in combos)
            row = {"query_id": q, "method": method, "n_runs": len(baseline_slots), "dependent_run_pairs": len(combos),
                   "set_d_em": d_em, "jaccard_distance": d_j,
                   "mean_recall_at_3": statistics.mean(x for x in recalls[method] if x is not None)}
            if method == "Tournament":
                row.update(mean_calls=statistics.mean(call_counts), min_calls=min(call_counts),
                           max_calls=max(call_counts), **dict(counts))
            else:
                row.update(mean_calls=len(pairs), min_calls=len(pairs), max_calls=len(pairs))
            rows.append(row)

    # Retain Gate 2's ordered impact result while exposing the primary set-only count.
    set_tab = Counter()
    for entry in workload["entries"]:
        q = str(entry["query_id"])
        verdicts = [[pool[q]["verdicts"][pos][r] for pos in range(workload["pairs_per_query"])] for r in range(repetitions)]
        flipped = {p for p in range(len(verdicts[0])) if len({v[p] for v in verdicts}) > 1}
        effects = single_point_counterfactuals(base_verdicts=verdicts[0], n_docs=len(entry["doc_ids"]), k=config["k"])
        high = {e["pair_position"] for e in effects if e["effect_class"] == "topk_set"}
        set_tab.update(high_impact=len(high), ever_flipped_and_high_impact=len(high & flipped),
                       high_impact_never_flipped=len(high - flipped), ever_flipped_low_impact=len(flipped - high))

    funnel = []
    for q in workload["query_ids"]:
        for fraction in budgets:
            for method in methods:
                selected = [run for run in smoke if run["query_id"] == q and run["budget_fraction"] == fraction
                            and run["method"] == method and run["repair_mode"] == "incremental"]
                reviews = [review for run in selected for review in run["reviews"]]
                funnel.append({"query_id": q, "budget": fraction, "method": method,
                    "runs": len(selected), "reviews_attempted": len(reviews),
                    "votes_changed": sum(r["decision_changed"] for r in reviews),
                    "sets_changed": sum(r["set_changed"] for r in reviews),
                    "new_branch_needed": sum(r["new_branch_needed"] for r in reviews),
                    "repair_calls": sum(r["repair_calls"] for r in reviews),
                    "aborted_reviews": sum(r["status"] == "aborted" for r in reviews)})
    feasibility = []
    for q in workload["query_ids"]:
        requests = [row for row in counterfactuals if row["query_id"] == q]
        for cap in config["diagnostics"]["absolute_budgets"]:
            feasibility.append({"query_id": q, "absolute_budget": cap,
                "scope": "forced-flip repair feasibility, not voting value or independent evidence",
                "requests": len(requests),
                "feasible_forced_flip_repairs": sum(2 + r["oracle_new_request_count"] <= cap for r in requests),
                "new_request_distribution": dict(Counter(r["oracle_new_request_count"] for r in requests))})
    dependencies = sorted([Path(__file__).resolve(), ROOT / "configs/offline_protocol.json", config_path,
                           ROOT / "configs/gate2_identity.json", ROOT / "data/manifests/gate2_external_data.json",
                           *list((ROOT / "src/certirank").rglob("*.py"))])
    summary = {
        "schema_version": 2, "settings_hash": config_hash(config), "evidence_level": "historical_development_exploration",
        "operator": "fixed_leaf_tournament_v1", "primary_output": "unordered_top3_set",
        "new_physical_calls": 0,
        "source_hashes": {(p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p.as_posix()): sha256(p.read_bytes()).hexdigest() for p in dependencies},
        "queries_exposed_to_development": workload["query_ids"],
        "independence_caveat": "distinct historical response slots; temporal independence not established; run pairs dependent",
        "replay_checks": {"complete_runs": len(runs), "repeat_outputs_equal": len(runs)},
        "naive_r0_set_only_cross_tab": dict(set_tab),
        "query_metrics": rows,
        "strategy_comparison_ready": False,
        "single_group_strategy_smoke_rows": len(smoke),
        "review_value_model_status": "unavailable: historical pool has fewer than two disjoint review groups",
        "quality_diagnostic": {"single_relevant_queries": sum(sum(rel > 0 for rel in entry["qrels"].values()) == 1 for entry in workload["entries"]),
            "baseline_recall_saturated": all(row["mean_recall_at_3"] == 1 for row in rows),
            "noninferiority_established": False,
            "remaining_need": "independently judged boundary candidates or multi-relevant task; preserve original quality gate"},
        "strategy_smoke_scope": "output/call/repair equivalence only; D_EM intentionally null",
        "blockers": ["5 responses per pair cannot supply four disjoint review triples",
                     "additional independent query groups and independent labels required",
                     "current provider pricing and hard run caps unverified; live adapter absent"],
    }
    fields = list(dict.fromkeys(key for row in rows for key in row))
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    lines = ["# 离线现象报告：历史响应上的固定 Tournament", "",
             "证据等级：探索性。3 个历史查询已暴露给开发；新物理调用 0。", "",
             "同一原始比较矩阵分别运行两个固定算子。算子输出差异单独列示，不能算作同计划随机性。",
             f"主指标使用无序 Top-{config['k']} 集合；每查询 {len(baseline_slots)} 次单响应执行，执行对相互依赖。", "",
             "| 查询 | 算子 | D_EM | Jaccard 距离 | 平均调用 | Recall@3 |",
             "|---|---|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['query_id']} | {row['method']} | {row['set_d_em']:.3f} | {row['jaccard_distance']:.3f} | "
                     f"{row['mean_calls']:.1f} | {row['mean_recall_at_3']:.3f} |")
    lines.extend(["", "## 轨迹可观测性与单请求翻转", "",
                  "分母为每次执行实际观察到的唯一请求，汇总为 run-request 级；同一请求复用遵循统一缓存规则。",
                  "conditional_safe / conditional_impact 均以其他已观察判断固定为条件；不能解释为未来重采样保证。",
                  "unresolved 在首个未观察比较处停止。Oracle 用该次响应槽位的完整表补全反事实，计算新增请求数；这些结果只供离线分析。", "",
                  "| 查询 | 观察请求 | conditional_safe | conditional_impact | unresolved | Oracle 集合影响 | unknown 且有影响 | 新请求总数 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in rows:
        if row["method"] == "Tournament":
            keys = ("observed_requests", "conditional_safe", "conditional_impact", "unresolved", "oracle_set_impact", "unknown_and_oracle_impact", "new_requests_sum")
            lines.append("| " + row["query_id"] + " | " + " | ".join(str(row.get(key, 0)) for key in keys) + " |")
    lines.extend(["", "完整逐请求记录见 `counterfactuals.jsonl`，其中包含新分支分布，不能用平均数替代尾部。", "",
                  "## 历史 naive 指标对齐", "",
                  f"r0 集合口径：{dict(set_tab)}。有序口径 37 与集合口径 28 分别保留。", "",
                  "## 当前裁决", "",
                  "P1 执行基础已实现；P2 历史配置的现象与可观测性诊断已生成。",
                  "H0 按本表逐查询解释；H1/H2/H3 尚无真实策略收益验证。",
                  "历史 5 次响应最多容纳一个完整三次投票组，重叠组合无法扩充独立执行数。",
                  "可继续合成数据上的策略接口与成本归因验证；正式开发/留出评价需补数据。",
                  "Confidence 标为 unavailable；TraceSensitivity 属未冻结候选。不得据此宣称超越置信度。", "",
                  f"另生成 {len(smoke)} 行单响应组三次投票工程 smoke（`strategy_smoke.jsonl`）：Base、Random、SimpleTopology、TraceSensitivity、Vote3。",
                  "全部复核方法在固定选择规则下与合法缓存重放核对输出、状态和逐请求账目；因只有一个独立组，策略 D_EM 保持 null。",
                  "确定性产物保留节点计数；完整 CPU 时间由独立 benchmark 脚本测量，真实服务延迟尚未测量。", "",
                  "`subset_sensitivity.json` 平衡每个初始响应槽位：5 响应产生 30 个初始/复核角色分配，每个初始槽位出现 6 次；全部相互关联。",
                  "`review_funnel.json` 给出实际复核触发漏斗；`budget_feasibility.json` 给出绝对调用预算的强制翻转修复可行域。",
                  "`budgeted_oracle.json` 用实际追加响应与预算枚举合法复核序列；当前只有一个响应组，完整算法分歧保持未知。",
                  "ReviewValue 已提供开发集拟合接口，历史数据无法拟合独立组收益，故本报告不运行该策略。", "",
                  "预算估算只支持历史情景参考，付费运行仍需授权与完整 run manifest。", ""])
    return {
        "phenomenon.json": dumps(summary), "phenomenon_report.md": "\n".join(lines),
        "query_metrics.csv": buf.getvalue(),
        "runs.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs),
        "counterfactuals.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in counterfactuals),
        "strategy_smoke.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in smoke),
        "subset_sensitivity.json": dumps(subsets),
        "review_funnel.json": dumps(funnel), "budget_feasibility.json": dumps(feasibility),
        "budgeted_oracle.json": dumps(oracle_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/experiment.json")
    args = parser.parse_args()
    artifacts = analyze(args.config)
    if args.check:
        failures = [name for name, value in artifacts.items() if not (OUT / name).exists() or (OUT / name).read_text(encoding="utf-8") != value]
        if failures:
            print("FAIL stale/missing artifacts:", ", ".join(failures))
            return 1
        print(f"PASS {len(artifacts)} offline artifacts reproduced exactly; zero provider calls")
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, value in artifacts.items():
            (OUT / name).write_text(value, encoding="utf-8")
        print(f"wrote {len(artifacts)} artifacts to {OUT.relative_to(ROOT)}; zero provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
