#!/usr/bin/env python3
"""Audit A part 3: fully independent recomputation of the Gate 3 main comparison.

Independent engine (this directory) re-executes Base / Random / ReviewValue under
the frozen config (N=8, K=3, 4 slot groups, budgets 0.2/0.4, cached_replay) and
recomputes D_EM, Recall@3, call counts, per-group gains and the bootstrap interval,
then compares against the repo's reported summary.json values.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import (BUDGETS, GATE3, K, OUT, SEED, SLOT_GROUPS, bootstrap_group_means,
                          d_em, execute, load_pool, recall_at_k, save_json)
from audit_common import FrozenValueModel

ROOT = Path(__file__).resolve().parents[2]


def run_split(split: str, model: FrozenValueModel):
    workload, calls, pool = load_pool(split)
    per_query = {}
    for qid, entry in pool.items():
        table = entry["table"]
        n = len(entry["doc_ids"])
        relevant = entry["relevant"]
        per_query[qid] = {}
        for method in ("Base", "Random", "ReviewValue"):
            for budget in BUDGETS:
                if method == "Base" and budget != BUDGETS[0]:
                    continue  # budget irrelevant for Base; one row suffices
                runs = []
                for gi, group in enumerate(SLOT_GROUPS):
                    run = execute(table, n, K, group, method, budget, SEED + gi, model=model)
                    run["recall"] = recall_at_k(run["topk"], relevant)
                    runs.append(run)
                per_query[qid][(method, budget)] = {
                    "topks": [r["topk"] for r in runs],
                    "d_em": d_em([r["topk"] for r in runs]),
                    "mean_logical_calls": statistics.mean(r["logical_calls"] for r in runs),
                    "mean_base_calls": statistics.mean(r["base_calls"] for r in runs),
                    "mean_extra_calls": statistics.mean(r["extra_calls"] for r in runs),
                    "mean_recall": statistics.mean(r["recall"] for r in runs),
                    "statuses": [r["status"] for r in runs],
                    "n_reviews": [len(r["reviews"]) for r in runs],
                    "n_decision_changed": [sum(1 for rv in r["reviews"] if rv["decision_changed"]) for r in runs],
                    "runs": [{k: r[k] for k in ("topk", "selected", "reviews", "base_calls",
                                                "extra_calls", "logical_calls", "budget_limit", "status", "reason")}
                             for r in runs],
                }
    return per_query


def compare_vs_reported(split, per_query, reported):
    checks = []
    comp = reported["comparisons"]
    for budget in BUDGETS:
        gains, base_ds, cand_ds, rat = [], [], [], []
        for qid, methods in per_query.items():
            rand = methods[("Random", budget)]
            rv = methods[("ReviewValue", budget)]
            gains.append(rand["d_em"] - rv["d_em"])
            base_ds.append(rand["d_em"])
            cand_ds.append(rv["d_em"])
            rat.append((rand["mean_recall"], rv["mean_recall"]))
        point_gain = statistics.mean(gains)
        lo, hi = bootstrap_group_means(gains)
        rep = comp[budget]["point"]
        checks.append({
            "budget": budget,
            "mine_gain": round(point_gain, 6),
            "reported_gain": round(rep["gain"], 6),
            "match": abs(point_gain - rep["gain"]) < 1e-9,
            "mine_baseline_d_em_Random": round(statistics.mean(base_ds), 6),
            "reported_baseline_d": round(rep["baseline_d"], 6),
            "baseline_match": abs(statistics.mean(base_ds) - rep["baseline_d"]) < 1e-9,
            "mine_candidate_d_em_ReviewValue": round(statistics.mean(cand_ds), 6),
            "mine_bootstrap95_gain": [lo, hi],
            "reported_bootstrap95": comp[budget]["bootstrap_95"]["gain"],
            "quality_delta": round(statistics.mean(a - b for a, b in rat), 6),
            "reported_quality_delta": round(rep["quality_delta"], 6),
            "positive_groups": sum(g > 0 for g in gains),
            "reported_positive_groups": comp[budget]["positive_groups"],
            "mine_calls_Random": round(statistics.mean(
                methods[("Random", budget)]["mean_logical_calls"] for methods in per_query.values()), 4),
            "reported_candidate_calls": round(rep["candidate_calls"], 4),
            "mine_calls_ReviewValue": round(statistics.mean(
                methods[("ReviewValue", budget)]["mean_logical_calls"] for methods in per_query.values()), 4),
            "reported_comparator_calls": round(rep["comparator_calls"], 4),
            "per_query_gain": {qid: round(rand_rv[0] - rand_rv[1], 6) for qid, rand_rv in
                               ((qid, (methods[("Random", budget)]["d_em"], methods[("ReviewValue", budget)]["d_em"]))
                                for qid, methods in per_query.items())},
            "per_query_random_d_em": {qid: round(methods[("Random", budget)]["d_em"], 6)
                                      for qid, methods in per_query.items()},
            "per_query_rv_d_em": {qid: round(methods[("ReviewValue", budget)]["d_em"], 6)
                                  for qid, methods in per_query.items()},
            "per_query_base_d_em": {qid: round(methods[("Base", BUDGETS[0])]["d_em"], 6)
                                    for qid, methods in per_query.items()},
        })
    return checks


def main():
    model = FrozenValueModel(OUT.parent.parent / "data" / "gate3_nfcorpus" / "formal" / "dev_results" / "value_model.json")
    print("value model buckets:")
    for key, samples in sorted(model.samples.items()):
        gains = [s["gain"] for s in samples]
        changed = sum(s["decision_changed"] for s in samples)
        pos = sum(1 for g in gains if g > 0)
        print(f"  {key}: n={len(samples)} mean_gain={statistics.mean(gains):.4f} "
              f"pos_gains={pos} decision_changed={changed}")

    results = {}
    for split in ("dev", "heldout"):
        per_query = run_split(split, model)
        reported = json.loads((ROOT / "data" / "gate3_nfcorpus" / "formal" / f"{split}_results" / "summary.json").read_text(encoding="utf-8"))
        checks = compare_vs_reported(split, per_query, reported)
        results[split] = {
            "checks": checks,
            "per_query": {qid: {f"{m}|{b}": {k: v for k, v in d.items() if k != "runs"}
                                for (m, b), d in methods.items()}
                          for qid, methods in per_query.items()},
            "per_query_runs": {qid: {f"{m}|{b}": d["runs"] for (m, b), d in methods.items()}
                               for qid, methods in per_query.items()},
        }
        print(f"\n== {split} ==")
        for c in checks:
            print(f"budget {c['budget']}: gain mine={c['mine_gain']} reported={c['reported_gain']} "
                  f"match={c['match']} | baselineD mine={c['mine_baseline_d_em_Random']} rep={c['reported_baseline_d']} "
                  f"match={c['baseline_match']} | bootstrap mine={c['mine_bootstrap95_gain']} rep={c['reported_bootstrap95']} "
                  f"| positive mine={c['positive_groups']} rep={c['reported_positive_groups']} "
                  f"| calls RV mine={c['mine_calls_ReviewValue']} rep={c['reported_candidate_calls']} "
                  f"Rand mine={c['mine_calls_Random']} rep={c['reported_comparator_calls']}")

    save_json("audit_a_main_recompute.json", results)


if __name__ == "__main__":
    main()
