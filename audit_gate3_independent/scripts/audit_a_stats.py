#!/usr/bin/env python3
"""Audit A part 2: independent recomputation of nondeterminism statistics
(ever-flip, mean pairwise run disagreement) for dev/heldout/calibration,
plus the null-vs-0.0 temperature comparison with an exact Fisher test.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import GATE3, load_jsonl, save_json


def pool_nondeterminism(calls, reps_expected):
    by_pair = defaultdict(dict)
    for r in calls:
        by_pair[(r["query_id"], r["pair_position"])][r["repetition_id"]] = r["strict_parsed_decision"]
    ever_flip = 0
    per_pair_disagreement = {}
    for key, votes in by_pair.items():
        values = [votes[r] for r in sorted(votes)]
        if len(votes) != reps_expected:
            raise ValueError(f"unexpected rep count {key}")
        if len(set(values)) > 1:
            ever_flip += 1
        pairs = list(combinations(range(len(values)), 2))
        per_pair_disagreement[key] = sum(values[i] != values[j] for i, j in pairs) / len(pairs)
    # pooled mean pairwise disagreement over pairs
    pooled = sum(per_pair_disagreement.values()) / len(per_pair_disagreement)
    # macro over queries
    per_query = defaultdict(list)
    for (qid, pos), d in per_pair_disagreement.items():
        per_query[qid].append(d)
    macro = {qid: sum(v) / len(v) for qid, v in per_query.items()}
    # pooled per run-pair disagreement (rep a vs rep b across all pairs)
    run_pair_rates = {}
    for a, b in combinations(range(reps_expected), 2):
        dis = 0
        for key, votes in by_pair.items():
            if votes[a] != votes[b]:
                dis += 1
        run_pair_rates[f"{a}-{b}"] = dis / len(by_pair)
    return {
        "pairs": len(by_pair),
        "ever_flip_pairs": ever_flip,
        "ever_flip_ratio": round(ever_flip / len(by_pair), 6),
        "mean_pairwise_disagreement_pooled_over_pairs": round(pooled, 6),
        "mean_pairwise_disagreement_macro_over_queries": round(sum(macro.values()) / len(macro), 6),
        "per_query_ever_flip": {qid: sum(1 for (q, p) in per_pair_disagreement if q == qid and len(set(
            [by_pair[(q, p)][r] for r in range(reps_expected)])) > 1) for qid in macro},
        "per_query_pair_count": {qid: sum(1 for (q, p) in per_pair_disagreement if q == qid) for qid in macro},
        "per_query_mean_disagreement": {k: round(v, 6) for k, v in macro.items()},
        "run_pair_disagreement_rates": {k: round(v, 6) for k, v in run_pair_rates.items()},
    }


def fisher_exact_2x2(a, b, c, d):
    """Two-sided Fisher exact via hypergeometric probabilities (independent impl)."""
    def logchoose(n, k):
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

    row1, row2, col1, col2 = a + b, c + d, a + c, b + d
    n = row1 + row2
    lo = max(0, col1 - row2)
    hi = min(row1, col1)

    def prob(x):
        return math.exp(logchoose(col1, x) + logchoose(col2, row1 - x) - logchoose(n, row1))

    p_obs = prob(a)
    total = sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p_obs * (1 + 1e-9))
    return min(1.0, total)


def main():
    out = {}
    for split in ("dev", "heldout"):
        calls = load_jsonl(GATE3 / "formal" / split / "pair_calls.jsonl")
        out[f"formal_{split}"] = pool_nondeterminism(calls, 12)

    # calibration lines
    cal = {}
    for name, path in (("A_null", "run_A"), ("B_zero", "run_B")):
        calls = load_jsonl(GATE3 / "temperature_calibration" / path / "pair_calls.jsonl")
        cal[name] = pool_nondeterminism(calls, 5)
    out["calibration"] = cal

    # cross-line majority/aligned comparison (independent recompute)
    def by_pair_seq(calls):
        d = defaultdict(dict)
        for r in calls:
            d[(r["query_id"], r["pair_id"])][r["repetition_id"]] = r["strict_parsed_decision"]
        return d

    a_seq = by_pair_seq(load_jsonl(GATE3 / "temperature_calibration" / "run_A" / "pair_calls.jsonl"))
    b_seq = by_pair_seq(load_jsonl(GATE3 / "temperature_calibration" / "run_B" / "pair_calls.jsonl"))
    common = set(a_seq) & set(b_seq)
    maj_diff = sum(1 for k in common if (sum(a_seq[k].values()) > len(a_seq[k]) / 2) != (sum(b_seq[k].values()) > len(b_seq[k]) / 2))
    aligned = aligned_diff = 0
    for k in common:
        for rep in set(a_seq[k]) & set(b_seq[k]):
            aligned += 1
            aligned_diff += a_seq[k][rep] != b_seq[k][rep]
    out["calibration_cross"] = {
        "common_pairs": len(common),
        "majority_verdict_differs": maj_diff,
        "aligned_rep_comparisons": aligned,
        "aligned_rep_differs": aligned_diff,
    }

    # Fisher tests: ever-flip counts A vs B; formal dev vs heldout
    out["fisher_tests"] = {
        "calib_A_vs_B_everflip": {
            "table": [cal["A_null"]["ever_flip_pairs"], 32 - cal["A_null"]["ever_flip_pairs"],
                      cal["B_zero"]["ever_flip_pairs"], 32 - cal["B_zero"]["ever_flip_pairs"]],
            "p_two_sided": round(fisher_exact_2x2(cal["A_null"]["ever_flip_pairs"], 32 - cal["A_null"]["ever_flip_pairs"],
                                                  cal["B_zero"]["ever_flip_pairs"], 32 - cal["B_zero"]["ever_flip_pairs"]), 4),
        },
        "dev_vs_heldout_everflip": {
            "table": [out["formal_dev"]["ever_flip_pairs"], 168 - out["formal_dev"]["ever_flip_pairs"],
                      out["formal_heldout"]["ever_flip_pairs"], 336 - out["formal_heldout"]["ever_flip_pairs"]],
            "p_two_sided": round(fisher_exact_2x2(out["formal_dev"]["ever_flip_pairs"], 168 - out["formal_dev"]["ever_flip_pairs"],
                                                  out["formal_heldout"]["ever_flip_pairs"], 336 - out["formal_heldout"]["ever_flip_pairs"]), 4),
        },
    }

    # rep-to-rep drift check: disagreement of rep r vs rep 0 over time (formal pools)
    for split in ("dev", "heldout"):
        calls = load_jsonl(GATE3 / "formal" / split / "pair_calls.jsonl")
        by_pair = defaultdict(dict)
        for r in calls:
            by_pair[(r["query_id"], r["pair_position"])][r["repetition_id"]] = r["strict_parsed_decision"]
        drift = {}
        for rep in range(12):
            dis = sum(1 for votes in by_pair.values() if votes[rep] != votes[0])
            drift[rep] = round(dis / len(by_pair), 6)
        out[f"formal_{split}_drift_vs_rep0"] = drift

    save_json("audit_a_nondeterminism.json", out)

    # console summary vs reported
    print("== independent recomputation vs reported ==")
    d, h = out["formal_dev"], out["formal_heldout"]
    print(f"dev: ever-flip {d['ever_flip_pairs']}/{d['pairs']} = {d['ever_flip_ratio']:.3%} (reported 41/168=24.4%)")
    print(f"dev: mean pairwise disagreement pooled={d['mean_pairwise_disagreement_pooled_over_pairs']:.4f} "
          f"macro={d['mean_pairwise_disagreement_macro_over_queries']:.4f} (reported 8.7%)")
    print(f"heldout: ever-flip {h['ever_flip_pairs']}/{h['pairs']} = {h['ever_flip_ratio']:.3%} (reported 56/336=16.7%)")
    print(f"heldout: mean pairwise disagreement pooled={h['mean_pairwise_disagreement_pooled_over_pairs']:.4f} "
          f"macro={h['mean_pairwise_disagreement_macro_over_queries']:.4f} (reported 6.0%)")
    print("calibration A_null:", {k: cal['A_null'][k] for k in ('ever_flip_pairs','ever_flip_ratio','mean_pairwise_disagreement_pooled_over_pairs')})
    print("calibration B_zero:", {k: cal['B_zero'][k] for k in ('ever_flip_pairs','ever_flip_ratio','mean_pairwise_disagreement_pooled_over_pairs')})
    print("fisher:", json.dumps(out["fisher_tests"], indent=1))


if __name__ == "__main__":
    main()
