#!/usr/bin/env python
"""Compute the Gate 2A metrics from the raw trace and run artifacts.

Reads ``gate2_calls.jsonl`` / ``gate2_runs.jsonl`` (plus the frozen workload for qrels)
and writes ``gate2_analysis.json``. Pure offline recomputation: every number is derived
from the raw JSONL alone, so the analysis can be re-run at any time with ``--recompute``
semantics (it is always a recompute — there is no cached state to trust).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.protocol import write_json  # noqa: E402
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.metrics import (  # noqa: E402
    aggregate_metrics,
    operator_level_metrics,
    query_level_metrics,
)
from experiments.lotus_nondeterminism.gate2.protocol import read_jsonl  # noqa: E402
from experiments.lotus_nondeterminism.gate2.workload import load_workload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", default=str(P.CALLS_PATH))
    parser.add_argument("--runs", default=str(P.RUNS_PATH))
    parser.add_argument("--workload", default=str(P.WORKLOAD_PATH))
    parser.add_argument("--out", default=str(P.ANALYSIS_PATH))
    args = parser.parse_args()

    calls = read_jsonl(args.calls)
    runs = read_jsonl(args.runs)
    workload = load_workload(args.workload)

    query_ids = [str(q) for q in workload["query_ids"]]
    qrels_of = {str(e["query_id"]): e.get("qrels") or {} for e in workload["entries"]}

    traces_by_query: dict[str, list[dict]] = {q: [] for q in query_ids}
    for row in calls:
        traces_by_query.setdefault(str(row["query_id"]), []).append(row)
    runs_by_query: dict[str, list[dict]] = {q: [] for q in query_ids}
    for row in runs:
        runs_by_query.setdefault(str(row["query_id"]), []).append(row)

    per_query: list[dict[str, Any]] = []
    for query_id in query_ids:
        operator = operator_level_metrics(traces_by_query.get(query_id, []))
        query = query_level_metrics(
            runs_by_query.get(query_id, []), k=P.K, qrels=qrels_of.get(query_id) or None
        )
        per_query.append({"query_id": query_id, "operator": operator, "query": query})

    macro = aggregate_metrics(per_query)
    ndcg_ranges = [q["query"]["ndcg_at_k_range"] for q in per_query if q["query"]["ndcg_at_k_range"]]
    if ndcg_ranges:
        macro["macro_ndcg_at_k_range"] = [
            round(min(lo for lo, _hi in ndcg_ranges), 6),
            round(max(hi for _lo, hi in ndcg_ranges), 6),
        ]

    total_runs = len(runs)
    analysis = {
        "substudy": P.SUBSISTUDY,
        "gate": P.GATE,
        "sources": {"calls": Path(args.calls).name, "runs": Path(args.runs).name},
        "runs_total": total_runs,
        "runs_expected": P.QUERIES_SELECTED * P.REPETITIONS,
        "comparisons_total": len(calls),
        "comparisons_expected": P.PLANNED_CALLS,
        "replay_gate_all_runs": all(bool(r.get("replay_consistent_with_lotus")) for r in runs),
        "per_query": per_query,
        "macro": macro,
        "metric_definitions": {
            "ever_flipped_pair_ratio": "pairs with >=2 distinct strict-valid verdicts across the R runs / all pairs",
            "pairwise_run_disagreement": "per unordered run pair: disagreements / strict-valid-in-both pairs",
            "invalid_response_rate": "trace rows whose strict parse is not valid / all trace rows",
            "topk_set_exact_agreement": "run pairs whose Top-K *member sets* are identical",
            "topk_ordered_exact_agreement": "run pairs whose Top-K *orders* are identical",
            "jaccard_at_k": "|intersection| / |union| of two Top-K sets",
            "kendall_tau_b": "tau over the full 8-document rankings",
            "ndcg_at_k": "the paper's dcg(found)/dcg(sorted(gt)[:k]) formula, per run",
        },
        "statistical_caveat": (
            "The C(5,2)=10 run pairs per query share runs and are dependent; all run-pair "
            "statistics are descriptive. No inferential test is computed from them here."
        ),
    }

    write_json(args.out, analysis)
    print(f"wrote {args.out}")
    print(f"runs: {analysis['runs_total']}/{analysis['runs_expected']}  "
          f"comparisons: {analysis['comparisons_total']}/{analysis['comparisons_expected']}  "
          f"replay gate: {analysis['replay_gate_all_runs']}")
    for q in per_query:
        op, qr = q["operator"], q["query"]
        print(
            f"query {q['query_id']}: ever-flip {op['ever_flipped_pair_ratio']} "
            f"mean-disagree {op['mean_run_pair_disagreement']} invalid {op['invalid_response_rate']} | "
            f"set-agree {qr['topk_set_exact_agreement_rate']} order-agree {qr['topk_ordered_exact_agreement_rate']} "
            f"jaccard {qr['mean_jaccard_at_k']} tau {qr['mean_kendall_tau_b_full_ranking']} "
            f"ndcg {qr['ndcg_at_k_mean']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
