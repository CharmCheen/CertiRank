#!/usr/bin/env python3
"""Audit C/D deep-dive: oracle details for improvement-space groups, ranking-boundary
structure of key pairs, cross-query selection dependence, and vote-split anatomy of
the disagreeing groups. EXPLORATORY.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import (BUDGETS, GATE3, K, OUT, SEED, SLOT_GROUPS, AuditLedger,
                          d_em, execute, load_pool, save_json, tournament)
from audit_common import FrozenValueModel

MODEL = FrozenValueModel(GATE3 / "formal" / "dev_results" / "value_model.json")


def main():
    out = {"exploratory": True}

    # ---- 1. oracle details for groups with base space
    details = {}
    for split in ("dev", "heldout"):
        workload, calls, pool = load_pool(split)
        for qid, entry in pool.items():
            table = entry["table"]
            n = len(entry["doc_ids"])
            base_topks = [execute(table, n, K, g, "Base", "0.2", 0)["topk"] for g in SLOT_GROUPS]
            base_dem = d_em(base_topks)
            if base_dem == 0:
                continue
            # anatomy: which slot-0 verdict vectors differ across executions
            vecs = {g: {(i, j): table[(i, j)][g[0]] for (i, j) in table} for g in SLOT_GROUPS}
            diff_pairs = set()
            for ga, gb in combinations(SLOT_GROUPS, 2):
                for p in vecs[ga]:
                    if vecs[ga][p] != vecs[gb][p]:
                        diff_pairs.add(p)
            # per-execution actionable+key info
            per_exec = []
            for g in SLOT_GROUPS:
                led = AuditLedger(table, g)
                btopk, _ = tournament(n, K, led.observe)
                key_pairs = []
                for pair in table:
                    modified = {p: list(v) for p, v in table.items()}
                    modified[pair][g[0]] = not modified[pair][g[0]]
                    cf = AuditLedger(modified, g)
                    cftopk, _ = tournament(n, K, cf.observe)
                    if set(cftopk) != set(btopk):
                        key_pairs.append(pair)
                act = [p for p, v in table.items() if v[g[1]] == v[g[2]] and v[g[1]] != v[g[0]]]
                per_exec.append({
                    "slots": list(g), "base_topk": list(btopk),
                    "actionable": [list(p) for p in act],
                    "actionable_and_key": [list(p) for p in act if p in key_pairs],
                    "key_pairs": [list(p) for p in key_pairs],
                    "key_pairs_unstable": [list(p) for p in key_pairs if len(set(table[p])) > 1],
                })
            # oracle specifics from audit_c output
            oc = json.load(open(OUT / "audit_c_oracle.json"))[split]["per_query_group"][qid]
            details[f"{split}:{qid}"] = {
                "base_d_em": base_dem,
                "base_topks": [list(t) for t in base_topks],
                "slot0_differring_pairs_across_execs": [list(p) for p in sorted(diff_pairs)],
                "n_slot0_differring_pairs": len(diff_pairs),
                "per_exec": per_exec,
                "oracle_04": oc["0.4"],
                "oracle_02": oc["0.2"],
            }

    out["space_group_anatomy"] = details

    # ---- 2. key-pair boundary position (all queries, both splits)
    boundary_stats = Counter()
    for split in ("dev", "heldout"):
        workload, calls, pool = load_pool(split)
        for qid, entry in pool.items():
            table = entry["table"]
            n = len(entry["doc_ids"])
            for g in SLOT_GROUPS:
                led = AuditLedger(table, g)
                btopk, _ = tournament(n, K, led.observe)
                wins = [0] * n
                for (i, j), votes in table.items():
                    if votes[g[0]]:
                        wins[i] += 1
                    else:
                        wins[j] += 1
                ranking = sorted(range(n), key=lambda x: (-wins[x], x))
                rank_of = {d: r for r, d in enumerate(ranking)}
                for pair in led.decisions:
                    modified = {p: list(v) for p, v in table.items()}
                    modified[pair][g[0]] = not modified[pair][g[0]]
                    cf = AuditLedger(modified, g)
                    cftopk, _ = tournament(n, K, cf.observe)
                    boundary_stats["visible_pairs"] += 1
                    if set(cftopk) != set(btopk):
                        boundary_stats["key_pairs"] += 1
                        ranks = sorted(abs(rank_of[pair[0]] - 2.5) + abs(rank_of[pair[1]] - 2.5) for _ in [0])
                        rmin = min(rank_of[pair[0]], rank_of[pair[1]])
                        rmax = max(rank_of[pair[0]], rank_of[pair[1]])
                        boundary_stats["key_pair_with_rank_le_2"] += int(rmin <= 2)
                        boundary_stats["key_pair_with_rank_le_4"] += int(rmax <= 4)
                        boundary_stats["key_pair_straddles_boundary"] += int(rmin <= 2 and rmax >= 3)
    out["boundary_stats"] = {k: v for k, v in boundary_stats.items()}

    # ---- 3. cross-query Random selection dependence (pairwise identity rate)
    dep = {}
    for split in ("dev", "heldout"):
        workload, calls, pool = load_pool(split)
        for budget in BUDGETS:
            sels = {}
            for qid, entry in pool.items():
                table = entry["table"]
                n = len(entry["doc_ids"])
                sels[qid] = [tuple(p) for gi, g in enumerate(SLOT_GROUPS)
                             for p in execute(table, n, K, g, "Random", budget, SEED + gi)["selected"]]
            qids = sorted(sels)
            same = total = 0
            for qa, qb in combinations(qids, 2):
                a, b = set(sels[qa]), set(sels[qb])
                if a or b:
                    total += 1
                    same += int(a == b)
            dep[f"{split}|{budget}"] = {
                "query_pairs_with_identical_selection_sets": same,
                "query_pairs_total": total,
                "identical_ratio": round(same / total, 4) if total else None,
            }
    out["random_selection_dependence"] = dep

    # ---- 4. vote-split anatomy: probability a probe flips, conditional structure
    mech = {}
    for split in ("dev", "heldout"):
        workload, calls, pool = load_pool(split)
        c = Counter()
        for qid, entry in pool.items():
            table = entry["table"]
            for g in SLOT_GROUPS:
                for p, v in table.items():
                    v0, v1, v2 = v[g[0]], v[g[1]], v[g[2]]
                    c["total"] += 1
                    c["p1_diff"] += v1 != v0
                    c["p2_diff"] += v2 != v0
                    c["flip"] += (v1 == v2 != v0)
        mech[split] = {
            "P(probe1 != initial)": round(c["p1_diff"] / c["total"], 6),
            "P(probe2 != initial)": round(c["p2_diff"] / c["total"], 6),
            "P(probe flips majority)": round(c["flip"] / c["total"], 6),
            "P(flip | probe1 != initial)": round(c["flip"] / c["p1_diff"], 6) if c["p1_diff"] else None,
            "note": "probe flips majority iff BOTH probe slots disagree with the initial slot",
        }
    out["probe_flip_probabilities"] = mech

    save_json("audit_cd_deepdive.json", out)

    print("space groups:", list(details.keys()))
    for k, d in details.items():
        print(f"\n-- {k}: base D_EM={d['base_d_em']}, slot0-differing pairs={d['n_slot0_differring_pairs']}")
        for pe in d["per_exec"]:
            print(f"   slots={pe['slots']} topk={pe['base_topk']} actionable={pe['actionable']} "
                  f"act_and_key={pe['actionable_and_key']} key_unstable={pe['key_pairs_unstable']}")
        print(f"   oracle@0.4 D_EM={d['oracle_04']['oracle_d_em']} calls={d['oracle_04']['oracle_total_extra_calls']}")
        print(f"   oracle@0.2 D_EM={d['oracle_02']['oracle_d_em']}")
    print("\nboundary stats:", dict(boundary_stats))
    print("selection dependence:", json.dumps(dep, indent=1))
    print("probe flip probabilities:", json.dumps(mech, indent=1))


if __name__ == "__main__":
    main()
