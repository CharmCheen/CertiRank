"""Sandbox validation: (1) reproduce every official Base execution from raw data,
(2) transitive-oracle sanity check across all four operators, (3) data completeness.

Writes results/sandbox_validation.json. Zero network, zero LLM calls.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import (GATE3_FORMAL, N, OFFICIAL_INITIAL_SLOTS, OPERATORS, Judge,
                     _load_jsonl, load_all, run_operator, save_json, slot_world)


def reproduce_official_base() -> dict:
    """Cross-validate the sandbox tournament against every Base run in runs.jsonl.

    Base executions read the slot-group initial slot (first element of each triple)
    for every first comparison; budget flags don't apply, so 2 budget rows per
    (query, slot-group) collapse to the same execution — both must match.
    """
    report = {"checked": 0, "mismatches": [], "unique_executions": 0}
    seen: set = set()
    for split in ("dev", "heldout"):
        pool = load_all()[split]
        runs = _load_jsonl(GATE3_FORMAL / f"{split}_results" / "runs.jsonl")
        for row in runs:
            if row["method"] != "Base":
                continue
            qid = row["query_id"]
            slots = row["slots"]
            world = slot_world(pool[qid]["table"], slots[0])
            run = run_operator("tournament", world, row["n"], row["k"])
            key = (split, qid, tuple(slots))
            if key not in seen:
                seen.add(key)
                report["unique_executions"] += 1
            ok = (list(run.topk) == list(row["topk"])
                  and run.calls == row["logical_calls"]
                  and run.cache_hits == row["cache_hits"])
            report["checked"] += 1
            if not ok:
                report["mismatches"].append({
                    "split": split, "query_id": qid, "slots": slots,
                    "official_topk": row["topk"], "sandbox_topk": list(run.topk),
                    "official_calls": row["logical_calls"], "sandbox_calls": run.calls,
                    "official_cache_hits": row["cache_hits"], "sandbox_cache_hits": run.cache_hits,
                })
    return report


def transitive_oracle_check(trials: int = 500, seed: int = 20261016) -> dict:
    """All operators must return the identical, correct Top-K under a total order."""
    rng = random.Random(seed)
    failures = []
    configs = [(8, 3), (8, 1), (8, 7), (5, 2), (6, 4), (12, 5), (16, 3)]
    checked = 0
    for n, k in configs:
        for _ in range(trials // len(configs) + 1):
            ranks = list(range(n))
            rng.shuffle(ranks)  # ranks[x] = strength rank of doc x (higher = stronger)
            true_topk = frozenset(sorted(sorted(range(n), key=lambda x: ranks[x], reverse=True)[:k]))

            def getter(pair, _ranks=ranks):
                lo, hi = pair
                return _ranks[lo] > _ranks[hi]  # smaller index wins iff stronger

            for name, fn in OPERATORS.items():
                judge = Judge(getter)
                run = fn(n, k, judge)
                checked += 1
                if run.topk_set != true_topk:
                    failures.append({"n": n, "k": k, "operator": name,
                                     "ranks": ranks, "got": sorted(run.topk_set),
                                     "expected": sorted(true_topk)})
    return {"comparisons_checked": checked, "failures": failures}


def data_completeness() -> dict:
    out = {}
    for split, pool in load_all().items():
        out[split] = {
            "query_groups": len(pool),
            "queries_with_28_pairs": sum(1 for e in pool.values() if len(e["table"]) == 28),
            "queries_with_full_12_reps": sum(
                1 for e in pool.values()
                if all(len(v) == 12 for v in e["table"].values())),
            "total_calls_expected": len(pool) * 28 * 12,
        }
    return out


def main() -> None:
    results_dir = Path(__file__).resolve().parents[1] / "results"
    report = {
        "data_completeness": data_completeness(),
        "official_base_reproduction": reproduce_official_base(),
        "transitive_oracle": transitive_oracle_check(),
    }
    repro = report["official_base_reproduction"]
    oracle = report["transitive_oracle"]
    print(f"[reproduce] rows checked={repro['checked']} unique executions="
          f"{repro['unique_executions']} mismatches={len(repro['mismatches'])}")
    print(f"[oracle] comparisons checked={oracle['comparisons_checked']} "
          f"failures={len(oracle['failures'])}")
    save_json(results_dir / "sandbox_validation.json", report)
    assert not repro["mismatches"], "sandbox failed to reproduce official Base runs"
    assert not oracle["failures"], "operator sanity check failed under total orders"


if __name__ == "__main__":
    main()
