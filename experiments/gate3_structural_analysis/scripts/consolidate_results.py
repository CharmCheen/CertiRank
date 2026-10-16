"""Consolidate all gate outputs into results/summary_numbers.json + per-query CSV.

Pure aggregation of already-computed results; no new experiments.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESULTS = Path(__file__).resolve().parents[1] / "results"


def main() -> None:
    b1 = json.loads((RESULTS / "b1_summary.json").read_text(encoding="utf-8"))
    b1d = json.loads((RESULTS / "b1_diagnostics.json").read_text(encoding="utf-8"))
    b2 = json.loads((RESULTS / "b2_summary.json").read_text(encoding="utf-8"))
    b3 = json.loads((RESULTS / "b3_summary.json").read_text(encoding="utf-8"))
    b3d = json.loads((RESULTS / "b3_certification.json").read_text(encoding="utf-8"))

    ops = ["tournament", "minheap", "quickselect", "fullcompare"]
    summary = {
        "identity": {
            "data": "Gate 3 formal cache (NFCorpus, GLM-5.3-Flash, temperature=null, N=8, K=3, 12 reps)",
            "queries": "18 (dev 6 + heldout 12); 12 frozen slot worlds each = 216 base executions per operator",
            "study_kind": "exploratory mechanism study; zero new LLM calls",
        },
        "b1_stability": {
            op: {
                "d_em12_pooled": round(b1["per_split"]["pooled"][op]["mean_d_em12"], 4),
                "d_em12_dev": round(b1["per_split"]["dev"][op]["mean_d_em12"], 4),
                "d_em12_heldout": round(b1["per_split"]["heldout"][op]["mean_d_em12"], 4),
                "d_em4_official_pooled": round(b1["per_split"]["pooled"][op]["mean_d_em_official4"], 4),
                "mean_calls": round(b1["per_split"]["pooled"][op]["mean_calls"], 2),
                "unstable_queries_of_18": b1["per_split"]["pooled"][op]["queries_with_any_change"],
            } for op in ops},
        "b1_paired": {k: {"mean_diff": round(v["mean_diff"], 4), "ci95": [round(x, 4) for x in v["ci95"]],
                          "p_sign": round(v["p_value"], 3)}
                      for k, v in b1["paired_d_em12"].items()},
        "b1_same_world_agreement": {k: round(v["mean_same_topk_rate"], 4)
                                    for k, v in b1["same_world_disagreement"].items()},
        "b1_input_order_range": {
            op: round(max(d[op]["mean_d_em12"] for d in b1["input_order_sensitivity"]["aggregate"].values())
                      - min(d[op]["mean_d_em12"] for d in b1["input_order_sensitivity"]["aggregate"].values()), 4)
            for op in ops},
        "b2_branches": {
            op: {
                "branch_escape_rate": round(b2["per_operator"][op]["branch_escape_rate"], 4),
                "mean_new_pairs_given_escape": round(b2["per_operator"][op]["mean_new_pairs_given_escape"], 2),
                "fixed_trace_unresolved_rate": round(b2["per_operator"][op]["fixed_trace_unresolved_rate"], 4),
                "fixed_trace_resolved_agreement": b2["per_operator"][op]["fixed_trace_resolved_agreement"],
                "topk_set_sensitivity": round(b2["per_operator"][op]["topk_set_sensitivity"], 4),
                "double_emergent_rate": round(b2["per_operator"][op]["double_emergent_rate"], 5),
                "single_flips": b2["per_operator"][op]["single_flips_total"],
                "double_flips": b2["per_operator"][op]["double_flips_total"],
            } for op in ops},
        "b2_critical_edges": b2["tournament_critical_edges"],
        "b3_coverage": {
            op: {m: {"certified": b3["coverage"][op][m]["certified"],
                     "n": b3["coverage"][op][m]["n"],
                     "rate": round(b3["coverage"][op][m]["coverage_rate"], 4)}
                 for m in ("empirical_b1", "empirical_b2", "worst_b1", "worst_b2")}
            for op in ops},
        "b3_shortcut_soundness": b3["soundness"]["tournament_boundary_shortcut_vs_empirical_b1"],
    }

    # observed-noise stability (D_EM12 == 0) vs hypothetical b=1 certification
    obs_stable = {op: sum(1 for r in b1d if r[op] == 0.0) for op in ops}
    summary["observed_vs_hypothetical"] = {
        "queries_stable_across_all_12_worlds": obs_stable,
        "fullcompare_b1_certified_executions_of_216": b3["coverage"]["fullcompare"]["empirical_b1"]["certified"],
        "adaptive_operators_b1_certified_of_216": 0,
    }

    (RESULTS / "summary_numbers.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # per-query CSV
    with open(RESULTS / "per_query_metrics.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["split", "query_id", "noise", "cycles_mean",
                         "d_em12_tournament", "d_em12_minheap", "d_em12_quickselect", "d_em12_fullcompare"])
        for r in b1d:
            writer.writerow([r["split"], r["qid"], r["noise"], r["cycles_mean"],
                             r["tournament"], r["minheap"], r["quickselect"], r["fullcompare"]])

    # certified-execution detail CSV
    with open(RESULTS / "b3_certified_executions.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["split", "query_id", "slot", "operator", "model", "b", "base_topk"])
        for d in b3d:
            for model, cert in d["cert"].items():
                if cert["verdict"] == "certified":
                    writer.writerow([d["split"], d["query_id"], d["slot"], d["operator"],
                                     model.replace("_b", " b"), model[-1], d["base_topk"]])
    print("wrote summary_numbers.json, per_query_metrics.csv, b3_certified_executions.csv")
    print(json.dumps(summary["observed_vs_hypothetical"], indent=1))


if __name__ == "__main__":
    main()
