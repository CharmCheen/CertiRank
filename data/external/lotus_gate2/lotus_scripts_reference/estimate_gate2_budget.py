#!/usr/bin/env python
"""Estimate the Gate 2A budget offline: call counts, tokens, cost, runtime.

Uses the frozen workload's actual production prompts (token-counted locally, no
network) and the same rate card as Gate 1. Both peak and off-peak prices are reported
because the design must not depend on which window is active. Transport-retry headroom
is shown separately: retries are attempts, never scientific repetitions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.pricing import cost_scenarios  # noqa: E402
from experiments.lotus_nondeterminism.gate1.protocol import write_json  # noqa: E402
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.workload import load_workload, production_inputs  # noqa: E402
from experiments.lotus_nondeterminism.paperpath.scifact_beir import load_paper_scifact  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default=str(P.WORKLOAD_PATH))
    parser.add_argument("--out", default=str(P.BUDGET_PATH))
    parser.add_argument("--retry-headroom", type=int, default=40,
                        help="extra transport attempts assumed possible (default 40 ~= 9.5%%)")
    args = parser.parse_args()

    workload = load_workload(args.workload)
    print("loading SciFact via the BEIR loader ...")
    data = load_paper_scifact()

    # token counts from the *actual* production prompts, measured locally
    from experiments.lotus_nondeterminism.oracle.lotus_adapter import build_oracle_lm
    from experiments.lotus_nondeterminism.gate23.protocol import p0_hashing_oracle

    lm = build_oracle_lm(p0_hashing_oracle(), run_id="G2-budget-estimate")

    prompt_tokens_per_pair: dict[str, list[int]] = {}
    total_prompt_tokens = 0
    for entry in workload["entries"]:
        inputs = production_inputs(entry, data)
        tokens = []
        for pair in entry["pairs"]:
            doc1 = inputs["multimodal"][pair["position_i"]]
            doc2 = inputs["multimodal"][pair["position_j"]]
            from lotus.sem_ops.sem_topk import get_match_prompt_binary

            messages = get_match_prompt_binary(
                dict(doc1), dict(doc2), inputs["instruction"], strategy=None, model=lm
            )
            count = int(lm.count_tokens(list(messages)))
            tokens.append(count)
            total_prompt_tokens += count
        prompt_tokens_per_pair[str(entry["query_id"])] = tokens

    calls = P.PLANNED_CALLS
    # completion side: P0 answers are the short "Document N" label; Gate 1B measured ~3
    completion_tokens_per_call = 3
    prompt_tokens_total = total_prompt_tokens  # one call per pair per repetition
    completion_tokens_total = calls * completion_tokens_per_call

    # scale to repetitions: prompt tokens measured once per pair, needed R times
    prompt_tokens_all_reps = total_prompt_tokens * P.REPETITIONS

    cost = cost_scenarios(
        input_tokens=prompt_tokens_all_reps,
        output_tokens=completion_tokens_total,
        cached_input_tokens=0,  # provider-side caching unknown at estimate time; worst case
    )

    # runtime: Gate 1B measured ~1.2 s/call for P0 (thinking disabled), sequential transport
    seconds_per_call = 1.2
    runtime_seconds = calls * seconds_per_call

    per_query_prompt_mean = {
        qid: round(sum(toks) / len(toks), 2) for qid, toks in prompt_tokens_per_pair.items()
    }
    estimate = {
        "substudy": P.SUBSISTUDY,
        "workload": Path(args.workload).name,
        "planned_calls": calls,
        "planned_calls_breakdown": {
            "queries": P.QUERIES_SELECTED,
            "pairs_per_query": P.PAIRS_PER_QUERY,
            "repetitions": P.REPETITIONS,
        },
        "protocol_budget_cap": P.BUDGET_CAP,
        "planned_calls_over_cap": round(calls / P.BUDGET_CAP, 6),
        "retry_headroom_attempts": args.retry_headroom,
        "max_attempts_with_headroom": calls + args.retry_headroom,
        # what run_gate2_topk.py actually enforces by default: every planned run at its
        # worst case. A cap below this would refuse the experiment it was meant to guard.
        "runner_default_max_attempts": P.QUERIES_SELECTED * P.REPETITIONS * (P.PAIRS_PER_QUERY + 10),
        "runner_notes": "lower --max-physical-attempts to spend less; never raise it to rescue a run",
        "prompt_tokens_per_pair": {
            qid: {"min": min(toks), "max": max(toks), "mean": round(sum(toks) / len(toks), 2)}
            for qid, toks in prompt_tokens_per_pair.items()
        },
        "prompt_tokens_per_call_mean_overall": (
            round(total_prompt_tokens / (P.QUERIES_SELECTED * P.PAIRS_PER_QUERY), 2)
        ),
        "prompt_tokens_per_query_mean": per_query_prompt_mean,
        "prompt_tokens_total_all_reps": prompt_tokens_all_reps,
        "completion_tokens_total": completion_tokens_total,
        "completion_assumption": f"{completion_tokens_per_call} tokens/call (Gate 1B P0 measured ~3)",
        "cost_usd": cost,
        "runtime_estimate": {
            "seconds_per_call_assumption": seconds_per_call,
            "note": "sequential transport (oracle adapter max_batch_size=1), Gate 1B P0 latency",
            "total_seconds": runtime_seconds,
            "total_minutes": round(runtime_seconds / 60, 1),
        },
        "estimator": lm._token_estimator,
    }

    write_json(args.out, estimate)
    print(f"wrote {args.out}")
    print(f"planned calls : {calls} (cap {P.BUDGET_CAP}, retry headroom +{args.retry_headroom})")
    print(f"prompt tokens : {prompt_tokens_all_reps:,} total across repetitions")
    print(f"cost off-peak : ${cost['off_peak']['total_usd']:.6f}")
    print(f"cost peak     : ${cost['peak']['total_usd']:.6f}")
    print(f"runtime       : ~{runtime_seconds / 60:.1f} min (sequential, ~{seconds_per_call} s/call)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
