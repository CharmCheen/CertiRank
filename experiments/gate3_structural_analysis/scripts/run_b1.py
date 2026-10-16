"""Gate B1: physical operator noise propagation.

Same frozen judgment worlds (12 repetition slots per query), four physical Top-K
plans, identical candidates/K/input order/cache semantics. Measures:
  - per-operator cross-world instability (D_EM over 12 slot worlds, and over the
    4 official initial slots for direct comparability with Gate 3 Base D_EM);
  - same-world cross-operator disagreement;
  - comparison cost vs stability (is any plan's stability just a workload effect?);
  - input-order sensitivity (4 fixed permutations);
  - call-level resampling (supplementary semantics, reported separately).

Writes results/b1_operator_comparison.json and results/b1_summary.json.
"""
from __future__ import annotations

import math
import random
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import (Judge, OFFICIAL_INITIAL_SLOTS, OPERATORS, REPS, jaccard,
                     load_all, pairwise_set_disagreement, run_operator, save_json,
                     slot_world)

OPS = list(OPERATORS)
SEED = 20261016


def sign_test_paired(diffs: list[float]) -> dict:
    """Two-sided exact sign test on nonzero paired differences (descriptive)."""
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    pos = sum(1 for d in nonzero if d > 0)
    if n == 0:
        return {"n_nonzero": 0, "positive": 0, "p_value": 1.0}
    p = sum(math.comb(n, i) for i in range(min(pos, n - pos) + 1)) / 2 ** (n - 1)
    p = min(1.0, p)
    return {"n_nonzero": n, "positive": pos, "p_value": p}


def bootstrap_ci(values: list[float], samples: int = 2000, seed: int = 17) -> list:
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        means.append(sum(rng.choice(values) for _ in values) / len(values))
    means.sort()
    return [means[int(0.025 * len(means))], means[min(len(means) - 1, int(0.975 * len(means)))]]


def build_permutation_worlds(table: dict, perm: list[int]) -> dict[int, dict]:
    """All 12 slot worlds reindexed under one candidate permutation."""
    worlds = {}
    for r in range(REPS):
        world = slot_world(table, r)
        out = {}
        for new_i in range(len(perm)):
            for new_j in range(new_i + 1, len(perm)):
                old_i, old_j = perm[new_i], perm[new_j]
                lo, hi = (old_i, old_j) if old_i < old_j else (old_j, old_i)
                verdict = world[(lo, hi)]
                winner_old = lo if verdict else hi
                out[(new_i, new_j)] = (perm.index(winner_old) == new_i)
        worlds[r] = out
    return worlds


def run_b1() -> dict:
    pools = load_all()
    detail = []
    for split, pool in pools.items():
        for qid, entry in sorted(pool.items()):
            worlds = {r: slot_world(entry["table"], r) for r in range(REPS)}
            row = {"split": split, "query_id": qid, "operators": {}, "cross_world": {}}
            for op in OPS:
                runs = [run_operator(op, worlds[r]) for r in range(REPS)]
                sets = [r.topk_set for r in runs]
                ordered = [r.topk for r in runs]
                official_sets = [sets[r] for r in OFFICIAL_INITIAL_SLOTS]
                row["operators"][op] = {
                    "d_em_12": pairwise_set_disagreement(sets),
                    "d_em_official_4": pairwise_set_disagreement(official_sets),
                    "distinct_topk_sets": len(set(sets)),
                    "distinct_ordered_topk": len(set(ordered)),
                    "mean_calls": sum(r.calls for r in runs) / len(runs),
                    "mean_compare_invocations": sum(r.calls + r.cache_hits for r in runs) / len(runs),
                    "mean_cache_hits": sum(r.cache_hits for r in runs) / len(runs),
                    "topk_sets": [sorted(s) for s in sets],
                }
            # same-world cross-operator agreement
            for a, b in combinations(OPS, 2):
                same = sum(
                    run_operator(a, worlds[r]).topk_set == run_operator(b, worlds[r]).topk_set
                    for r in range(REPS))
                jac = sum(
                    jaccard(run_operator(a, worlds[r]).topk_set,
                            run_operator(b, worlds[r]).topk_set)
                    for r in range(REPS)) / REPS
                row["cross_world"][f"{a}|{b}"] = {
                    "same_topk_worlds": same, "same_topk_rate": same / REPS, "mean_jaccard": jac}
            detail.append(row)
    return detail


def aggregate(detail: list) -> dict:
    summary = {"per_split": {}, "paired_d_em12": {}, "same_world_disagreement": {}}
    for split in ("dev", "heldout", "pooled"):
        rows = detail if split == "pooled" else [d for d in detail if d["split"] == split]
        block = {"n_queries": len(rows)}
        for op in OPS:
            vals = [d["operators"][op]["d_em_12"] for d in rows]
            block[op] = {
                "mean_d_em12": sum(vals) / len(vals),
                "ci95": bootstrap_ci(vals),
                "mean_d_em_official4": sum(d["operators"][op]["d_em_official_4"] for d in rows) / len(rows),
                "mean_calls": sum(d["operators"][op]["mean_calls"] for d in rows) / len(rows),
                "mean_compare_invocations": sum(d["operators"][op]["mean_compare_invocations"] for d in rows) / len(rows),
                "queries_with_any_change": sum(1 for d in rows if d["operators"][op]["d_em_12"] > 0),
            }
        summary["per_split"][split] = block
    for a, b in combinations(OPS, 2):
        diffs = [d["operators"][a]["d_em_12"] - d["operators"][b]["d_em_12"] for d in detail]
        summary["paired_d_em12"][f"{a}-{b}"] = {
            "mean_diff": sum(diffs) / len(diffs),
            "ci95": bootstrap_ci(diffs),
            **sign_test_paired(diffs)}
    for key in detail[0]["cross_world"]:
        rates = [d["cross_world"][key]["same_topk_rate"] for d in detail]
        summary["same_world_disagreement"][key] = {
            "mean_same_topk_rate": sum(rates) / len(rates),
            "queries_with_any_disagreement": sum(1 for r in rates if r < 1.0)}
    return summary


def input_order_sensitivity() -> dict:
    pools = load_all()
    n = 8
    perms = {
        "identity": list(range(n)),
        "reverse": list(range(n - 1, -1, -1)),
        "shuffle_a": [5, 0, 7, 2, 4, 1, 6, 3],
        "shuffle_b": [3, 6, 1, 5, 0, 7, 2, 4],
    }
    out = {"permutations": {}}
    for split, pool in pools.items():
        for qid, entry in sorted(pool.items()):
            for pname, perm in perms.items():
                worlds = build_permutation_worlds(entry["table"], perm)
                for op in OPS:
                    sets = [run_operator(op, worlds[r]).topk_set for r in range(REPS)]
                    key = (split, qid, pname)
                    out["permutations"].setdefault(pname, {}).setdefault(op, []).append(
                        {"split": split, "query_id": qid,
                         "d_em12": pairwise_set_disagreement(sets)})
    # aggregate: mean d_em12 per (permutation, operator)
    agg = {}
    for pname, opmap in out["permutations"].items():
        agg[pname] = {}
        for op, rows in opmap.items():
            vals = [r["d_em12"] for r in rows]
            agg[pname][op] = {"mean_d_em12": sum(vals) / len(vals),
                              "queries_with_any_change": sum(1 for v in vals if v > 0)}
    out["aggregate"] = agg
    out.pop("permutations")
    return out


def call_level_resampling(executions: int = 100) -> dict:
    """Supplementary semantics: each fresh pair read samples a uniform repetition
    slot (decision cache still fixes the value within one execution). Reported
    separately from frozen-world results — the two are never mixed."""
    rng = random.Random(SEED)
    pools = load_all()
    out = {}
    for split, pool in pools.items():
        for qid, entry in sorted(pool.items()):
            table = entry["table"]
            pairs = sorted(table)
            for op in OPS:
                topks = []
                for _ in range(executions):
                    draw = {p: table[p][rng.randrange(REPS)] for p in pairs}

                    def getter(pair, _d=draw):
                        return _d[pair]

                    run = OPERATORS[op](8, 3, Judge(getter))
                    topks.append(run.topk_set)
                out.setdefault(split, {}).setdefault(qid, {})[op] = {
                    "mean_pairwise_disagreement": pairwise_set_disagreement(topks)}
    agg = {}
    for split, queries in out.items():
        agg[split] = {op: {"mean_instability": sum(q[op]["mean_pairwise_disagreement"]
                                                   for q in queries.values()) / len(queries)}
                      for op in OPS}
    return {"aggregate": agg,
            "note": "call-level resampling: no within-execution world consistency across pairs; "
                    "independent draws per fresh read. Supplementary sensitivity analysis only."}


def main() -> None:
    results_dir = Path(__file__).resolve().parents[1] / "results"
    detail = run_b1()
    save_json(results_dir / "b1_operator_comparison.json", detail)
    summary = aggregate(detail)
    summary["input_order_sensitivity"] = input_order_sensitivity()
    summary["call_level_resampling"] = call_level_resampling()
    save_json(results_dir / "b1_summary.json", summary)

    print("\n=== B1 headline (pooled, 18 query-groups) ===")
    pooled = summary["per_split"]["pooled"]
    for op in OPS:
        b = pooled[op]
        print(f"{op:12s} D_EM12={b['mean_d_em12']:.4f} D_EM4={b['mean_d_em_official4']:.4f} "
              f"calls={b['mean_calls']:.2f} invokes={b['mean_compare_invocations']:.2f} "
              f"unstable_queries={b['queries_with_any_change']}/18")
    print("\nsame-world agreement:")
    for key, v in summary["same_world_disagreement"].items():
        print(f"  {key:24s} same_topk_rate={v['mean_same_topk_rate']:.3f} "
              f"queries_with_disagreement={v['queries_with_any_disagreement']}/18")
    print("\ninput-order sensitivity (mean D_EM12):")
    for pname, opmap in summary["input_order_sensitivity"]["aggregate"].items():
        line = " ".join(f"{op}={opmap[op]['mean_d_em12']:.4f}" for op in OPS)
        print(f"  {pname:10s} {line}")


if __name__ == "__main__":
    main()
