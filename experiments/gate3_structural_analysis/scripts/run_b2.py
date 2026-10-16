"""Gate B2: branch-sensitive counterfactual propagation.

For every query x operator x frozen slot world:
  1. run the plan, keep its executed-pair trace and Top-K;
  2. flip each executed judgment once (single flips) and each pair of executed
     judgments (double flips, full enumeration at N=8);
  3. analyse with two semantics that are never mixed:
       Method A (fixed-trace): the counterfactual may only reuse the original
         trace's judgments; a comparison outside the trace -> UNRESOLVED.
       Method B (branch-aware replay): re-run the plan from the initial state on
         the perturbed frozen world; newly read pairs resolve from that world and
         are recorded as new logical comparisons (offline cache reads, never
         exposed to a simulated online algorithm).

Metrics: branch escape rate, new-comparison cost, fixed-trace incompleteness and
its agreement with full replay, Top-K sensitivity, conditional absorption,
critical-edge structure (tournament), and double-flip non-additivity.

Writes results/b2_counterfactual.json + results/b2_summary.json.
"""
from __future__ import annotations

import sys
import time
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import (OPERATORS, REPS, UnresolvedPair, load_all, run_operator,
                     run_operator_fixed_trace, save_json, slot_world)

OPS = ["tournament", "minheap", "quickselect", "fullcompare"]
ORDERED = ("tournament", "minheap", "fullcompare")


def flip_analysis_single(op: str, world: dict, base) -> dict:
    executed = list(base.access_order)
    allowed = set(executed)
    rows = []
    for pair in executed:
        flips = frozenset([pair])
        # Method B: branch-aware replay on the perturbed frozen world
        replay = run_operator(op, world, flips=flips)
        new_pairs = sorted(set(replay.access_order) - allowed)
        # Method A: fixed-trace counterfactual
        try:
            fixed = run_operator_fixed_trace(op, world, allowed, flips)
            a_status = "resolved"
            a_agrees = fixed.topk_set == replay.topk_set
        except UnresolvedPair:
            a_status, a_agrees = "unresolved", None
        rows.append({
            "pair": list(pair),
            "a_status": a_status,
            "a_agrees_with_b": a_agrees,
            "b_new_pairs": [list(p) for p in new_pairs],
            "b_new_pair_count": len(new_pairs),
            "b_new_calls": replay.calls - len(allowed & set(replay.access_order)),
            "b_topk_set_changed": replay.topk_set != base.topk_set,
            "b_topk_order_changed": (op in ORDERED and replay.topk != base.topk),
            "b_topk": sorted(replay.topk_set),
        })
    return {"executed": [list(p) for p in executed], "single_flips": rows}


def flip_analysis_double(op: str, world: dict, base, single_rows: list) -> dict:
    executed = list(base.access_order)
    allowed = set(executed)
    single_changed = {tuple(r["pair"]): r["b_topk_set_changed"] for r in single_rows}
    rows = []
    emergent = cancelling = joint_changed = 0
    for p1, p2 in combinations(executed, 2):
        flips = frozenset((p1, p2))
        replay = run_operator(op, world, flips=flips)
        new_pairs = sorted(set(replay.access_order) - allowed)
        changed = replay.topk_set != base.topk_set
        try:
            run_operator_fixed_trace(op, world, allowed, flips)
            a_status = "resolved"
        except UnresolvedPair:
            a_status = "unresolved"
        any_single = single_changed[p1] or single_changed[p2]
        if changed:
            joint_changed += 1
            if not any_single:
                emergent += 1
        elif any_single:
            cancelling += 1
        rows.append({
            "pairs": [list(p1), list(p2)],
            "a_status": a_status,
            "b_new_pair_count": len(new_pairs),
            "b_topk_set_changed": changed,
            "neither_single_changed_but_joint_did": changed and not any_single,
        })
    return {"double_flips": rows, "joint_changed": joint_changed,
            "joint_emergent": emergent, "cancelling": cancelling}


def critical_edges(world: dict, base, single_rows: list, k: int = 3) -> dict:
    """Tournament-only: where do output-changing flips sit in the plan structure?"""
    if not base.events:
        return {}
    base_set = base.topk_set
    # win counts over the FULL frozen world (rank context, audit-compatible)
    win = {i: 0 for i in range(8)}
    for (i, j), v in world.items():
        win[i if v else j] += 1
    ranks = {doc: r for r, doc in enumerate(sorted(win, key=lambda d: win[d], reverse=True))}
    events_by_pair: dict[tuple, list] = {}
    for node, pair, winner, stage in base.events:
        events_by_pair.setdefault(pair, []).append((node, stage))
    out = []
    for row in single_rows:
        pair = tuple(row["pair"])
        if pair not in events_by_pair:
            continue
        i, j = pair
        ev = events_by_pair[pair]
        out.append({
            "pair": list(pair),
            "changed": row["b_topk_set_changed"],
            "stages": sorted({s for _, s in ev}),
            "max_stage": max(s for _, s in ev),
            "node_depth": max(3 - (node.bit_length() - 1) for node, _ in ev),  # depth from root (root=0..3)
            "spans_base_topk_boundary": (i in base_set) != (j in base_set),
            "both_in_base_topk": (i in base_set) and (j in base_set),
            "winner_rank": ranks[i if world[pair] else j],
            "loser_rank": ranks[i if world[pair] else j] == ranks[i] and ranks[j] or ranks[j],
        })
    return {"flips": out,
            "note": "winner_rank/loser_rank by full-world win counts (0 = most wins)"}


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
                    single = flip_analysis_single(op, world, base)
                    double = flip_analysis_double(op, world, base, single["single_flips"])
                    rec = {"split": split, "query_id": qid, "slot": r, "operator": op,
                           "base_topk": sorted(base.topk_set), "base_calls": base.calls,
                           **single, **double}
                    if op == "tournament":
                        rec["critical"] = critical_edges(world, base, single["single_flips"])
                    detail.append(rec)
            print(f"  [{time.monotonic()-t0:7.1f}s] {split} {qid} done", flush=True)

    save_json(Path(__file__).resolve().parents[1] / "results" / "b2_counterfactual.json", detail)

    # ---------------- aggregate ----------------
    summary = {"per_operator": {}, "per_operator_split": {}}
    for op in OPS:
        recs = [d for d in detail if d["operator"] == op]
        flips = [r for d in recs for r in d["single_flips"]]
        dbls = [r for d in recs for r in d["double_flips"]]
        resolved_a = [r for r in flips if r["a_status"] == "resolved"]
        block = {
            "single_flips_total": len(flips),
            "branch_escape_rate": sum(1 for r in flips if r["b_new_pair_count"] > 0) / len(flips),
            "mean_new_pairs_given_escape": (
                sum(r["b_new_pair_count"] for r in flips if r["b_new_pair_count"] > 0)
                / max(1, sum(1 for r in flips if r["b_new_pair_count"] > 0))),
            "max_new_pairs": max(r["b_new_pair_count"] for r in flips),
            "fixed_trace_unresolved_rate": sum(1 for r in flips if r["a_status"] == "unresolved") / len(flips),
            "fixed_trace_resolved_agreement": (
                sum(1 for r in resolved_a if r["a_agrees_with_b"]) / len(resolved_a)) if resolved_a else None,
            "topk_set_sensitivity": sum(1 for r in flips if r["b_topk_set_changed"]) / len(flips),
            "topk_order_sensitivity": (
                sum(1 for r in flips if r["b_topk_order_changed"]) / len(flips)
                if any(r["b_topk_order_changed"] is not None for r in flips) else None),
            "conditional_absorption_rate": sum(1 for r in flips if not r["b_topk_set_changed"]) / len(flips),
            "double_flips_total": len(dbls),
            "double_joint_changed_rate": sum(1 for r in dbls if r["b_topk_set_changed"]) / len(dbls),
            "double_emergent_rate": sum(1 for r in dbls if r["neither_single_changed_but_joint_did"]) / len(dbls),
            "double_unresolved_rate": sum(1 for r in dbls if r["a_status"] == "unresolved") / len(dbls),
        }
        summary["per_operator"][op] = block
        for split in ("dev", "heldout"):
            srecs = [d for d in recs if d["split"] == split]
            sflips = [r for d in srecs for r in d["single_flips"]]
            if sflips:
                summary["per_operator_split"].setdefault(op, {})[split] = {
                    "n_flips": len(sflips),
                    "branch_escape_rate": sum(1 for r in sflips if r["b_new_pair_count"] > 0) / len(sflips),
                    "topk_set_sensitivity": sum(1 for r in sflips if r["b_topk_set_changed"]) / len(sflips),
                }

    # tournament critical-edge aggregate
    crit = [r for d in detail if d["operator"] == "tournament" for r in d.get("critical", {}).get("flips", [])]
    if crit:
        changed = [r for r in crit if r["changed"]]
        summary["tournament_critical_edges"] = {
            "flips_analysed": len(crit),
            "output_changing_flips": len(changed),
            "boundary_span_rate_among_changing": (
                sum(1 for r in changed if r["spans_base_topk_boundary"]) / len(changed)) if changed else None,
            "boundary_span_rate_among_absorbed": (
                sum(1 for r in crit if not r["changed"] and r["spans_base_topk_boundary"])
                / max(1, sum(1 for r in crit if not r["changed"]))),
            "changing_by_max_stage": {
                str(stage): sum(1 for r in changed if r["max_stage"] == stage) for stage in (0, 1, 2)},
            "absorbed_by_max_stage": {
                str(stage): sum(1 for r in crit if not r["changed"] and r["max_stage"] == stage)
                for stage in (0, 1, 2)},
        }

    save_json(Path(__file__).resolve().parents[1] / "results" / "b2_summary.json", summary)
    print("\n=== B2 headline (pooled, all 12 slot worlds) ===")
    for op in OPS:
        b = summary["per_operator"][op]
        print(f"{op:12s} escapes={b['branch_escape_rate']:.3f} newPairs|esc={b['mean_new_pairs_given_escape']:.2f} "
              f"A_unresolved={b['fixed_trace_unresolved_rate']:.3f} "
              f"A_agree={b['fixed_trace_resolved_agreement']} "
              f"topk_sens={b['topk_set_sensitivity']:.3f} "
              f"dbl_emergent={b['double_emergent_rate']:.3f}")
    if "tournament_critical_edges" in summary:
        print("critical edges:", summary["tournament_critical_edges"])


if __name__ == "__main__":
    main()
