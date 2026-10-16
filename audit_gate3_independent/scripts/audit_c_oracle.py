#!/usr/bin/env python3
"""Audit C (EXPLORATORY): post-hoc oracle upper bound and structural analysis.

Oracle question: under the SAME verification operations and the SAME budget
(probe = 2 calls from the reserved slots, majority-of-three update, cached-replay
re-runs charged to the budget), with hindsight about which pairs are most
favorable, how much could Query-level D_EM be reduced?

Structural questions: which pairs' counterfactual verdict changes could alter the
final Top-3; are key pairs visible to the review policy; are they at ranking
boundaries; is 12-rep instability correlated with tournament impact.

All results here are exploratory; nothing changes the preregistered gates.
"""
from __future__ import annotations

import json
import statistics
import sys
from fractions import Fraction
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import (BUDGETS, GATE3, K, OUT, SEED, SLOT_GROUPS, AuditLedger,
                          d_em, execute, load_pool, recall_at_k, save_json, tournament)
from audit_common import FrozenValueModel

MODEL = FrozenValueModel(GATE3 / "formal" / "dev_results" / "value_model.json")


def simulate_forced(table, n, k, slots, subset, fraction):
    """Run one execution with a FORCED probe subset (oracle schedule), full budget
    semantics: probes cost 2, post-flip cached-replay re-runs charge new comparisons,
    budget exhaustion aborts and falls back to the last complete output."""
    ledger = AuditLedger(table, slots)
    topk, events = tournament(n, k, ledger.observe)
    limit = ledger.base_calls * Fraction(fraction).numerator // Fraction(fraction).denominator
    ledger.limit = limit
    status = "complete"
    for pair in subset:
        if ledger.remaining < 2:
            status = "aborted"
            break
        try:
            before, after, _ = ledger.probe(pair)
        except Exception:
            status = "aborted"
            break
        if before != after:
            try:
                topk, events = tournament(n, k, ledger.observe)
            except Exception:
                status = "aborted"
                break
    return {"topk": tuple(topk), "extra_calls": ledger.extra_calls, "status": status}


def oracle_for_query(table, n, fraction, budget_mode):
    """Enumerate per-execution reachable outputs within budget; jointly minimize D_EM."""
    groups = SLOT_GROUPS
    per_exec_options = []
    for g in groups:
        ledger = AuditLedger(table, g)
        base_topk, _ = tournament(n, K, ledger.observe)
        visible = sorted(ledger.decisions)
        limit = ledger.base_calls * Fraction(fraction).numerator // Fraction(fraction).denominator
        max_probes = limit // 2
        # enumerate subsets up to max_probes (sorted order = canonical schedule)
        subsets = [()]
        if max_probes >= 1:
            subsets += [(p,) for p in visible]
        if max_probes >= 2:
            subsets += list(combinations(visible, 2))
        if max_probes >= 3:
            subsets += list(combinations(visible, 3))
        best_for_output = {}
        for s in subsets:
            res = simulate_forced(table, n, K, g, s, fraction)
            key = frozenset(res["topk"])
            cost = res["extra_calls"]
            if key not in best_for_output or cost < best_for_output[key]["extra_calls"]:
                best_for_output[key] = {"topk": list(res["topk"]), "extra_calls": cost,
                                        "subset": [list(p) for p in s], "status": res["status"]}
        per_exec_options.append(best_for_output)

    # joint minimization of D_EM over the product of distinct outputs
    best = None
    from itertools import product as iproduct
    keys = [sorted(opts.keys(), key=lambda fs: sorted(fs)) for opts in per_exec_options]
    for assignment in iproduct(*keys):
        topks = [list(sorted(a)) for a in assignment]
        # D_EM over SETS: set equality is what matters
        dem = d_em(topks)
        cost = sum(per_exec_options[i][assignment[i]]["extra_calls"] for i in range(4))
        cand = (dem, cost, [list(sorted(a)) for a in assignment])
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand
    n_options = [len(o) for o in per_exec_options]
    return {
        "oracle_d_em": best[0],
        "oracle_total_extra_calls": best[1],
        "oracle_outputs": best[2],
        "distinct_outputs_per_exec": n_options,
    }


def structural_analysis(table, n):
    """For each slot group: which pairs' verdict flips change the Top-3 (full
    counterfactual with recomputation), visibility, boundary position, and the
    relation to 12-rep instability."""
    result = []
    for g in SLOT_GROUPS:
        ledger = AuditLedger(table, g)
        base_topk, _ = tournament(n, K, ledger.observe)
        visible = set(ledger.decisions)
        # naive-sort ranking from the slot-0 verdict matrix (win counts, stable)
        wins = [0] * n
        for (i, j), votes in table.items():
            if votes[g[0]]:
                wins[i] += 1
            else:
                wins[j] += 1
        ranking = sorted(range(n), key=lambda x: (-wins[x], x))
        key_pairs = []
        for pair in table:
            modified = {}
            for p, votes in table.items():
                vals = list(votes)
                if p == pair:
                    vals[g[0]] = not vals[g[0]]
                modified[p] = vals
            cf_ledger = AuditLedger(modified, g)
            cf_topk, _ = tournament(n, K, cf_ledger.observe)
            if set(cf_topk) != set(base_topk):
                key_pairs.append(pair)
        # 12-rep instability per pair
        unstable = {p for p, votes in table.items() if len(set(votes)) > 1}
        result.append({
            "slots": list(g),
            "base_topk": list(base_topk),
            "ranking_by_wins": ranking,
            "visible_pairs": sorted(visible),
            "key_pairs_topk_changing": [list(p) for p in key_pairs],
            "key_pairs_visible": [list(p) for p in key_pairs if p in visible],
            "unstable_pairs_12rep": [list(p) for p in sorted(unstable)],
            "unstable_and_key": [list(p) for p in sorted(unstable & set(key_pairs))],
            "boundary_docs": ranking[2:5],
        })
    return result


def main():
    out = {"exploratory": True, "note": "post-hoc oracle on cached responses only; "
            "no new API calls; does not modify preregistered gates"}
    for split in ("dev", "heldout"):
        workload, calls, pool = load_pool(split)
        rows = {}
        for qid, entry in pool.items():
            table = entry["table"]
            n = len(entry["doc_ids"])
            base_runs = [execute(table, n, K, g, "Base", "0.2", 0) for g in SLOT_GROUPS]
            base_dem = d_em([r["topk"] for r in base_runs])
            res = {"base_d_em": base_dem}
            for budget in BUDGETS:
                rand_runs = [execute(table, n, K, g, "Random", budget, SEED + gi) for gi, g in enumerate(SLOT_GROUPS)]
                rv_runs = [execute(table, n, K, g, "ReviewValue", budget, SEED + gi, model=MODEL) for gi, g in enumerate(SLOT_GROUPS)]
                orc = oracle_for_query(table, n, budget, "fraction")
                res[budget] = {
                    "random_d_em": d_em([r["topk"] for r in rand_runs]),
                    "rv_d_em": d_em([r["topk"] for r in rv_runs]),
                    "oracle_d_em": orc["oracle_d_em"],
                    "oracle_gain_vs_random": d_em([r["topk"] for r in rand_runs]) - orc["oracle_d_em"],
                    "oracle_gain_vs_base": base_dem - orc["oracle_d_em"],
                    "oracle_total_extra_calls": orc["oracle_total_extra_calls"],
                    "distinct_outputs_per_exec": orc["distinct_outputs_per_exec"],
                    "oracle_outputs": orc["oracle_outputs"],
                }
            res["structural"] = structural_analysis(table, n)
            rows[qid] = res

        summary = {}
        for budget in BUDGETS:
            gains_r = [rows[q][budget]["oracle_gain_vs_random"] for q in rows]
            gains_b = [rows[q][budget]["oracle_gain_vs_base"] for q in rows]
            space = [q for q in rows if rows[q]["base_d_em"] > 0]
            summary[budget] = {
                "mean_oracle_gain_vs_random": round(statistics.mean(gains_r), 6),
                "mean_oracle_gain_vs_base": round(statistics.mean(gains_b), 6),
                "groups_with_base_space": len(space),
                "oracle_gain_vs_base_in_space_groups": round(
                    statistics.mean([rows[q][budget]["oracle_gain_vs_base"] for q in space]), 6) if space else None,
                "oracle_gain_vs_random_in_space_groups": round(
                    statistics.mean([rows[q][budget]["oracle_gain_vs_random"] for q in space]), 6) if space else None,
                "per_group_oracle_gain_vs_base": {q: round(rows[q][budget]["oracle_gain_vs_base"], 4) for q in rows},
                "per_group_oracle_d_em": {q: rows[q][budget]["oracle_d_em"] for q in rows},
                "per_group_base_d_em": {q: rows[q]["base_d_em"] for q in rows},
                "per_group_random_d_em": {q: rows[q][budget]["random_d_em"] for q in rows},
                "mean_oracle_extra_calls_total": round(statistics.mean(
                    [rows[q][budget]["oracle_total_extra_calls"] for q in rows]), 4),
            }
        # structural aggregates
        agg = {"key_pair_positions": [], "key_pairs_visible_ratio": [], "unstable_key_overlap": []}
        for q, r in rows.items():
            for s in r["structural"]:
                kp = s["key_pairs_topk_changing"]
                agg["key_pairs_visible_ratio"].append(
                    len(s["key_pairs_visible"]) / len(kp) if kp else None)
                agg["unstable_key_overlap"].append(
                    len(s["unstable_and_key"]) / len(s["unstable_pairs_12rep"]) if s["unstable_pairs_12rep"] else None)
        out[split] = {"per_query_group": rows, "summary": summary,
                      "structural_aggregates": {
                          "mean_key_pairs_visible_ratio": round(statistics.mean(
                              [v for v in agg["key_pairs_visible_ratio"] if v is not None]), 4) if any(
                              v is not None for v in agg["key_pairs_visible_ratio"]) else None,
                          "mean_unstable_pairs_that_are_key_ratio": round(statistics.mean(
                              [v for v in agg["unstable_key_overlap"] if v is not None]), 4) if any(
                              v is not None for v in agg["unstable_key_overlap"]) else None,
                      }}

        print(f"\n===== {split} oracle (exploratory) =====")
        for budget in BUDGETS:
            s = summary[budget]
            print(f"budget {budget}: mean oracle gain vs Base={s['mean_oracle_gain_vs_base']} "
                  f"vs Random={s['mean_oracle_gain_vs_random']} | groups with space={s['groups_with_base_space']} "
                  f"| in-space oracle gain vs Base={s['oracle_gain_vs_base_in_space_groups']}")
            print(f"  per-group oracle gain vs Base: {s['per_group_oracle_gain_vs_base']}")
        print("structural aggregates:", out[split]["structural_aggregates"])

    save_json("audit_c_oracle.json", out)


if __name__ == "__main__":
    main()
