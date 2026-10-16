#!/usr/bin/env python3
"""Deterministic synthetic mechanism checks; never treated as LLM evidence."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.campaign import run_cases, paired_group_comparison, research_verdict
from certirank.evaluation import run_algorithm, summarize_disagreement
from certirank.experiment import load_config, config_hash, frozen_settings
from certirank.research_diagnostics import budgeted_oracle, fit_review_value
from certirank.synthetic import make_cases

OUT = ROOT / "experiments/certirank_pilot/research_diagnostics"


def artifacts(config):
    cases = make_cases(config)
    model = fit_review_value([case for case in cases if case["split"] == "dev"], config["formal"]["slot_groups"],
                             config["primary_budget"], config_hash(frozen_settings(config)))
    heldout = [case for case in cases if case["split"] == "heldout"]
    _, metrics = run_cases(heldout, config, model=model)
    comparisons = {budget: paired_group_comparison(metrics, config["candidate_method"], "SimpleTopology", budget,
        bootstrap_samples=config["formal"]["bootstrap_samples"], seed=config["seed"]) for budget in config["budgets"]}
    chosen = [next(case for case in cases if case["n"] == n and case["split"] == "dev")
              for n in config["diagnostics"]["synthetic_sizes"]]
    grid, oracles = [], []
    for case in chosen:
        for cap in config["diagnostics"]["absolute_budgets"]:
            for method in (*config["methods"], config["candidate_method"]):
                runs = [run_algorithm(case["source"], case["n"], case["k"], tuple(group), method,
                        config["primary_budget"], absolute_budget=cap, model=model, seed=config["seed"] + i,
                        query_id=case["query_id"]) for i, group in enumerate(config["formal"]["slot_groups"])]
                reviews = [review for run in runs for review in run["reviews"]]
                grid.append({"query_id": case["query_id"], "n": case["n"], "method": method, "absolute_budget": cap, "budget_applies": method not in ("Base", "Vote3"),
                    **summarize_disagreement(runs), "calls": [r["logical_calls"] for r in runs],
                    "votes_changed": sum(r["decision_changed"] for r in reviews),
                    "new_branch_needed": sum(r["new_branch_needed"] for r in reviews),
                    "aborted_reviews": sum(r["status"] == "aborted" for r in reviews)})
        oracle = budgeted_oracle(case["source"], case["n"], case["k"], config["formal"]["slot_groups"],
            config["primary_budget"], case["relevant"], state_cap=config["diagnostics"]["oracle_state_cap"],
            joint_cap=config["diagnostics"]["oracle_joint_cap"], quality_tolerance=config["gates"]["maximum_quality_drop"])
        oracles.append({"query_id": case["query_id"], "n": case["n"], **oracle})
    deps = [Path(__file__), *list((ROOT / "src/certirank").rglob("*.py"))]
    summary = {"source_kind": "synthetic", "settings_hash": config_hash(config),
        "code_hashes": {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(deps)},
        "comparisons": comparisons, "verdict": research_verdict(comparisons[config["primary_budget"]], config,
            split="heldout", source_kind="synthetic"), "new_physical_calls": 0,
        "limitations": "synthetic heldout fixtures verify machinery, not real strategy effectiveness; oracle may be truncated"}
    report = ["# 合成机制诊断", "", "全部为固定 seed 合成比较，不能充当真实 LLM 收益证据。", "",
              "开发和诊断留出查询使用不同 seed、不同 source；每次完整执行使用互不重叠三槽位组。",
              "ReviewValue 在合成开发样本拟合单步收益代理；未知特征或无正收益预测时停止复核。",
              "四个独立响应组可计算完整算法分歧，Bootstrap 按 query-group 抽样。", "",
              "预算绝对值网格见 budget_grid.json。它属于开发诊断，不替换 +40% 主比较。",
              "理想参考见 oracle.json：搜索真实投票及修复；搜索截断时只能解释为已达到的乐观参考，无法作为全局上界。", ""]
    for row in oracles:
        report.append(f"- N={row['n']}：Base D_EM={row['base']['set_d_em']}；Oracle={row['oracle'] and row['oracle']['set_d_em']}；搜索完整={row['search_complete']}。")
    report.extend(["", "结论限定为合成机制与实验流程检查。真实响应采集、置信度信号及独立任务质量验证仍待完成。", ""])
    return {"summary.json": json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            "budget_grid.json": json.dumps(grid, ensure_ascii=False, indent=2) + "\n",
            "oracle.json": json.dumps(oracles, ensure_ascii=False, indent=2) + "\n",
            "value_model.json": json.dumps(model.to_dict(), ensure_ascii=False, indent=2) + "\n",
            "report.md": "\n".join(report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/experiment.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = artifacts(load_config(args.config))
    if args.check:
        bad = [name for name, value in result.items() if not (OUT / name).exists() or (OUT / name).read_text(encoding="utf-8") != value]
        if bad:
            print("FAIL synthetic diagnostics stale:", bad)
            return 1
        print("PASS synthetic diagnostics reproduced; no real research claim")
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, value in result.items():
            (OUT / name).write_text(value, encoding="utf-8")
        print("wrote synthetic-only research diagnostics; zero provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
