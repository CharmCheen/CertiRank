#!/usr/bin/env python3
"""Audit B: ReviewValue failure mechanism, traced along the real execution chain.

Per query-group diagnostics: improvement space, actionable pairs (probe would flip
the majority), visibility of actionable pairs, budget usage, selection overlap
between Random and ReviewValue, verdict changes, tournament absorption, and
cross-method topk identity vs identical D_EM.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import (BUDGETS, GATE3, K, OUT, SEED, SLOT_GROUPS, d_em, execute,
                          load_pool, recall_at_k, save_json)
from audit_common import FrozenValueModel


def actionable_pairs(table, slots):
    """Pairs where the two probe slots agree with each other but not the initial slot,
    i.e. a completed probe would flip the majority verdict."""
    out = []
    for pair, votes in table.items():
        v0, v1, v2 = votes[slots[0]], votes[slots[1]], votes[slots[2]]
        if v1 == v2 and v1 != v0:
            out.append(pair)
    return out


def pair_one_step_flip(table, slots):
    """Pairs where the two probe slots do NOT both agree (probe cannot flip)."""
    return [p for p in table if p not in actionable_pairs(table, slots)]


def analyze_split(split, model):
    workload, calls, pool = load_pool(split)
    diag = {}
    for qid, entry in pool.items():
        table = entry["table"]
        n = len(entry["doc_ids"])
        relevant = entry["relevant"]

        base_runs = [execute(table, n, K, g, "Base", "0.2", 0) for g in SLOT_GROUPS]
        base_topks = [r["topk"] for r in base_runs]
        base_visible = [set(r["reviews"]) or set() for r in base_runs]  # unused; see below

        # re-run with visible-pair capture: execute() already records selections; for
        # visible pairs we recompute the baseline trace directly
        visible_sets = []
        for g in SLOT_GROUPS:
            from audit_common import AuditLedger, tournament
            led = AuditLedger(table, g)
            topk, events = tournament(n, K, led.observe)
            visible_sets.append((set(led.decisions), topk, events))

        qd = {
            "n_docs": n,
            "n_relevant": len(relevant),
            "relevant_idx": sorted(relevant),
            "base_d_em": d_em(base_topks),
            "base_topks": [list(t) for t in base_topks],
            "base_mean_recall": statistics.mean(recall_at_k(t, relevant) for t in base_topks),
        }

        for budget in BUDGETS:
            rand_runs = [execute(table, n, K, g, "Random", budget, SEED + gi) for gi, g in enumerate(SLOT_GROUPS)]
            rv_runs = [execute(table, n, K, g, "ReviewValue", budget, SEED + gi, model=model) for gi, g in enumerate(SLOT_GROUPS)]
            rand_topks = [r["topk"] for r in rand_runs]
            rv_topks = [r["topk"] for r in rv_runs]

            per_exec = []
            for gi, g in enumerate(SLOT_GROUPS):
                act = actionable_pairs(table, g)
                visible = visible_sets[gi][0]
                act_visible = [p for p in act if p in visible]
                rr, rv = rand_runs[gi], rv_runs[gi]
                per_exec.append({
                    "slots": list(g),
                    "base_calls": rr["base_calls"],
                    "budget_limit": rr["budget_limit"],
                    "actionable_pairs_total": len(act),
                    "actionable_pairs": [list(p) for p in act],
                    "actionable_visible": [list(p) for p in act_visible],
                    "visible_pair_count": len(visible),
                    "random": {
                        "probes": len(rr["reviews"]),
                        "selected": [list(p) for p in rr["selected"]],
                        "verdict_changes": sum(1 for r in rr["reviews"] if r["decision_changed"]),
                        "topk_changes": sum(1 for r in rr["reviews"] if r["set_changed"]),
                        "extra_calls": rr["extra_calls"],
                        "status": rr["status"],
                        "final_topk": list(rr["topk"]),
                    },
                    "review_value": {
                        "probes": len(rv["reviews"]),
                        "selected": [list(p) for p in rv["selected"]],
                        "verdict_changes": sum(1 for r in rv["reviews"] if r["decision_changed"]),
                        "topk_changes": sum(1 for r in rv["reviews"] if r["set_changed"]),
                        "extra_calls": rv["extra_calls"],
                        "status": rv["status"],
                        "reason": rv["reason"],
                        "final_topk": list(rv["topk"]),
                    },
                    "selection_overlap_random_rv": len(set(map(tuple, rr["selected"])) & set(map(tuple, rv["selected"]))),
                })

            rand_sel = [tuple(p) for r in rand_runs for p in r["selected"]]
            rv_sel = [tuple(p) for r in rv_runs for p in r["selected"]]
            qd[budget] = {
                "random_d_em": d_em(rand_topks),
                "rv_d_em": d_em(rv_topks),
                "gain": d_em(rand_topks) - d_em(rv_topks),
                "rand_rv_topk_identical_per_exec": [list(a) == list(b) for a, b in zip(rand_topks, rv_topks)],
                "rand_topk_equals_base": [list(a) == list(b) for a, b in zip(rand_topks, base_topks)],
                "rv_topk_equals_base": [list(a) == list(b) for a, b in zip(rv_topks, base_topks)],
                "pooled_selection_overlap_jaccard": (
                    len(set(rand_sel) & set(rv_sel)) / len(set(rand_sel) | set(rv_sel))
                    if set(rand_sel) | set(rv_sel) else None),
                "per_exec": per_exec,
            }
        diag[qid] = qd
    return diag


def cross_query_selection_dependence(split):
    """How often Random picks identical pairs across different queries at the same
    (slot group, step) — the seed does not include the query id."""
    workload, calls, pool = load_pool(split)
    out = {}
    for budget in BUDGETS:
        by_pos = {}
        for qid, entry in pool.items():
            table = entry["table"]
            n = len(entry["doc_ids"])
            for gi, g in enumerate(SLOT_GROUPS):
                run = execute(table, n, K, g, "Random", budget, SEED + gi)
                for step, pair in enumerate(run["selected"]):
                    by_pos.setdefault((gi, step), []).append((qid, pair))
        same = 0
        total = 0
        for key, choices in by_pos.items():
            pairs = [p for _, p in choices]
            total += 1
            if len(set(pairs)) == 1:
                same += 1
        out[budget] = {
            "slot_step_positions": total,
            "positions_where_all_queries_chose_identical_pair": same,
            "note": "identical candidate sets + query-independent seeds reproduce the same choice",
        }
    return out


def verdict_flip_mechanism(split):
    """Population-level: probability structure of probe flips from the raw tables."""
    workload, calls, pool = load_pool(split)
    stats = Counter()
    for qid, entry in pool.items():
        table = entry["table"]
        for g in SLOT_GROUPS:
            for pair, votes in table.items():
                v0, v1, v2 = votes[g[0]], votes[g[1]], votes[g[2]]
                stats["pairs"] += 1
                if v1 != v0:
                    stats["probe1_disagrees_initial"] += 1
                if v2 != v0:
                    stats["probe2_disagrees_initial"] += 1
                if v1 == v2 and v1 != v0:
                    stats["actionable_probe_flips_majority"] += 1
                if v1 != v0 and v2 != v0:
                    stats["both_probes_disagree_initial"] += 1
    return {k: v for k, v in stats.items()}


def main():
    model = FrozenValueModel(GATE3 / "formal" / "dev_results" / "value_model.json")
    out = {}
    for split in ("dev", "heldout"):
        diag = analyze_split(split, model)
        out[split] = {
            "per_query_group": diag,
            "cross_query_selection_dependence": cross_query_selection_dependence(split),
            "verdict_flip_mechanism": verdict_flip_mechanism(split),
        }
        # summary table
        print(f"\n===== {split}: per query-group =====")
        print(f"{'query':12s} {'baseD':>6s} {'randD@.4':>8s} {'rvD@.4':>6s} {'gain':>5s} | "
              f"{'act(vis)':>9s} {'R probes/ch':>11s} {'RV probes/ch':>11s} {'ovl':>4s}")
        for qid, qd in diag.items():
            b = qd["0.4"]
            acts = sum(e["actionable_pairs_total"] for e in b["per_exec"])
            actv = sum(len(e["actionable_visible"]) for e in b["per_exec"])
            rp = sum(e["random"]["probes"] for e in b["per_exec"])
            rch = sum(e["random"]["verdict_changes"] for e in b["per_exec"])
            rvp = sum(e["review_value"]["probes"] for e in b["per_exec"])
            rvch = sum(e["review_value"]["verdict_changes"] for e in b["per_exec"])
            ovl = sum(e["selection_overlap_random_rv"] for e in b["per_exec"])
            print(f"{qid:12s} {qd['base_d_em']:6.3f} {b['random_d_em']:8.3f} {b['rv_d_em']:6.3f} {b['gain']:5.2f} | "
                  f"{actv:.0f}/{acts:.0f}{'':>4s} {rp}/{rch}{'':>7s} {rvp}/{rvch}{'':>7s} {ovl:4d}")
        fm = out[split]["verdict_flip_mechanism"]
        print(f"flip mechanism: pairs={fm['pairs']} actionable={fm['actionable_probe_flips_majority']} "
              f"({fm['actionable_probe_flips_majority']/fm['pairs']:.4%})")
        print("cross-query selection dependence:", json.dumps(out[split]["cross_query_selection_dependence"]))

    save_json("audit_b_mechanism.json", out)


if __name__ == "__main__":
    main()
