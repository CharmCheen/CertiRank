"""Gate B3: bounded-flip structural certification.

Guarantee semantics (Top-K SET equality against the base execution's set):
  Empirical model: worlds differ from w0 on at most b *executed* judgments;
    newly read pairs resolve from the same frozen slot world.
  Worst-case model: same flip budget on executed pairs, but pairs never executed
    in the base trace may take ANY value when a branch first reads them (all
    branches explored). The two models are reported separately, never merged.

Exact oracle: exhaustive DFS over the branch tree (b <= 2, N = 8). Node budget
and wall-clock deadline -> verdict UNKNOWN, never silently "certified".

Conservative pruning attempts, each validated against the exact oracle on every
instance (any disagreement counts as a soundness violation):
  P1 memoization of (forced-values, remaining-budget) search states;
  P2 tournament boundary shortcut (b=1): soundness argument — in a
    winner-removal tournament every extracted member won every comparison it
    took part in against a non-member (a member that loses once can never
    re-enter the tree), so flipping an executed pair that spans the base Top-K
    boundary necessarily evicts the member and changes the set. Hence
    certified(b=1) requires zero boundary-spanning executed pairs; the leaf-pair
    pigeonhole (K=3 members among 4 adjacent leaf pairs) makes that impossible,
    so the shortcut returns "violated" for every tournament execution.

Writes results/b3_certification.json (+ prints headline).
"""
from __future__ import annotations

import sys
import time
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import (OPERATORS, REPS, certify_empirical, certify_worst_case,
                     load_all, run_operator, save_json, slot_world)

OPS = ["tournament", "minheap", "quickselect", "fullcompare"]
NODE_BUDGET = 200_000
DEADLINE_S = 20.0


def tournament_boundary_shortcut(base) -> dict:
    """P2: b=1 certification shortcut for the tournament operator."""
    base_set = base.topk_set
    boundary = [p for p in base.access_order
                if (p[0] in base_set) != (p[1] in base_set)]
    return {"boundary_pairs": len(boundary),
            "verdict": "violated" if boundary else "certified"}


def main() -> None:
    t0 = time.monotonic()
    pools = load_all()
    detail = []
    for split, pool in pools.items():
        for qid, entry in sorted(pool.items()):
            for r in range(REPS):
                world = slot_world(entry["table"], r)
                for op in OPS:
                    base = run_operator(op, world)
                    rec = {"split": split, "query_id": qid, "slot": r, "operator": op,
                           "base_topk": sorted(base.topk_set), "cert": {}}
                    for b in (1, 2):
                        emp = certify_empirical(op, world, base, b)
                        rec["cert"][f"empirical_b{b}"] = {
                            "verdict": emp.verdict, "leaves": emp.leaves,
                            "seconds": round(emp.seconds, 4),
                            "counterexample": emp.counterexample}
                        worst = certify_worst_case(op, world, base, b,
                                                   node_budget=NODE_BUDGET, deadline_s=DEADLINE_S)
                        rec["cert"][f"worst_b{b}"] = {
                            "verdict": worst.verdict, "nodes": worst.nodes,
                            "leaves": worst.leaves, "pruned_by_memo": worst.pruned_by_memo,
                            "seconds": round(worst.seconds, 4),
                            "counterexample": worst.counterexample}
                    if op == "tournament":
                        rec["boundary_shortcut_b1"] = tournament_boundary_shortcut(base)
                    detail.append(rec)
            print(f"  [{time.monotonic()-t0:7.1f}s] {split} {qid} done", flush=True)

    results_dir = Path(__file__).resolve().parents[1] / "results"
    save_json(results_dir / "b3_certification.json", detail)

    # ---------------- aggregate ----------------
    agg = {"coverage": {}, "cost": {}, "soundness": {}}
    for op in OPS:
        recs = [d for d in detail if d["operator"] == op]
        for model in ("empirical_b1", "empirical_b2", "worst_b1", "worst_b2"):
            verdicts = [d["cert"][model]["verdict"] for d in recs]
            n = len(verdicts)
            agg["coverage"].setdefault(op, {})[model] = {
                "n": n,
                "certified": verdicts.count("certified"),
                "violated": verdicts.count("violated"),
                "unknown": verdicts.count("unknown"),
                "coverage_rate": verdicts.count("certified") / n,
            }
            key = model.split("_")[0]
            cost_field = "leaves" if key == "empirical" else "nodes"
            costs = [d["cert"][model].get(cost_field, d["cert"][model].get("leaves", 0)) for d in recs]
            secs = [d["cert"][model]["seconds"] for d in recs]
            agg["cost"].setdefault(op, {})[model] = {
                "mean_search_units": sum(costs) / len(costs),
                "max_search_units": max(costs),
                "mean_seconds": sum(secs) / len(secs),
                "total_seconds": sum(secs),
            }

    # P2 shortcut soundness vs exact empirical b=1 (tournament)
    violations = []
    shortcut_stats = {"agree": 0, "disagree": 0}
    for d in detail:
        if d["operator"] != "tournament":
            continue
        shortcut = d["boundary_shortcut_b1"]["verdict"]
        exact = d["cert"]["empirical_b1"]["verdict"]
        if shortcut == exact:
            shortcut_stats["agree"] += 1
        else:
            shortcut_stats["disagree"] += 1
            violations.append({"query_id": d["query_id"], "slot": d["slot"],
                               "shortcut": shortcut, "exact": exact})
    agg["soundness"]["tournament_boundary_shortcut_vs_empirical_b1"] = {
        **shortcut_stats, "violations": violations}

    # worst-case b=1 shortcut validity also matters for the *worst* model:
    # a boundary flip changes the set under frozen values already, hence under
    # any new-pair assignment -> shortcut implies worst-case verdict too.
    worst_violations = []
    for d in detail:
        if d["operator"] != "tournament":
            continue
        shortcut = d["boundary_shortcut_b1"]["verdict"]
        exact = d["cert"]["worst_b1"]["verdict"]
        if shortcut != exact and not (shortcut == "violated" and exact == "unknown"):
            worst_violations.append({"query_id": d["query_id"], "slot": d["slot"],
                                     "shortcut": shortcut, "exact": exact})
    agg["soundness"]["tournament_boundary_shortcut_vs_worst_b1"] = {
        "disagree": len(worst_violations), "violations": worst_violations}

    save_json(results_dir / "b3_summary.json", agg)
    print("\n=== B3 certification coverage (216 base executions per operator) ===")
    for op in OPS:
        for model in ("empirical_b1", "empirical_b2", "worst_b1", "worst_b2"):
            c = agg["coverage"][op][model]
            print(f"{op:12s} {model:14s} certified={c['certified']:3d}/{c['n']} "
                  f"({c['coverage_rate']*100:5.1f}%)  violated={c['violated']:3d} unknown={c['unknown']:3d}")
    print("\nshortcut soundness:", agg["soundness"]["tournament_boundary_shortcut_vs_empirical_b1"])
    print("worst-case b1 shortcut:", agg["soundness"]["tournament_boundary_shortcut_vs_worst_b1"])


if __name__ == "__main__":
    main()
