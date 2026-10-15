#!/usr/bin/env python
"""Offline smoke for Gate 2A: the complete harness path, zero API calls.

Runs the real workload (3 queries x 5 repetitions) through the real LOTUS
``df.sem_topk(method="naive")`` with :class:`ScriptedOracle` — a local in-memory oracle
keyed on canonical request hashes. The oracle adapter, prompt path, parsing, tracing,
and replay gate all execute exactly as in a live run; only the transport is scripted.

Verified here, before any paid call can happen:

1. every run performs exactly 28 comparisons (3 x 28 x 5 = 420 total);
2. the same pair produces the *same canonical request hash* in every repetition, and it
   matches the expected hash frozen in the workload;
3. the offline replay reproduces LOTUS's Top-K for every run (15/15);
4. an identical script yields identical answers across repetitions (determinism);
5. scripted unparseable answers are accounted as invalid (strict) while LOTUS's
   documented default-True verdict is recorded alongside — never counted as a flip;
6. smoke artifacts go to their own files and never mix with scientific ones.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.protocol import write_json, write_jsonl  # noqa: E402
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.protocol import read_jsonl  # noqa: E402
from experiments.lotus_nondeterminism.gate2.runner import ScriptedOracle, execute_run  # noqa: E402
from experiments.lotus_nondeterminism.gate2.workload import load_workload  # noqa: E402
from experiments.lotus_nondeterminism.gate23.protocol import RequestIndex, p0_hashing_oracle  # noqa: E402
from experiments.lotus_nondeterminism.paperpath.scifact_beir import load_paper_scifact  # noqa: E402

#: scripted unparseable answers, to prove invalid-response accounting end to end
UNPARSEABLE = "I cannot decide which document is more relevant."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default=str(P.WORKLOAD_PATH))
    parser.add_argument("--out", default=str(P.SMOKE_PATH))
    parser.add_argument("--calls-out", default=str(P.SMOKE_CALLS_PATH))
    parser.add_argument("--runs-out", default=str(P.SMOKE_RUNS_PATH))
    args = parser.parse_args()

    workload = load_workload(args.workload)
    data = load_paper_scifact()
    request_index = RequestIndex(p0_hashing_oracle())
    config = p0_hashing_oracle().config

    # script: everything by hash parity, except two injected unparseable answers on
    # query 0 (positions chosen by rule: the 5th and 17th pair of the frozen order)
    injected: dict[str, str] = {}
    for entry in workload["entries"]:
        for pair in entry["pairs"]:
            if entry["query_id"] == workload["query_ids"][0] and pair["pair_position"] in (4, 16):
                injected[pair["request_hash"]] = UNPARSEABLE
    oracle = ScriptedOracle(config, responses=injected)

    all_trace: list[dict] = []
    all_runs: list[dict] = []
    hash_matches: list[bool] = []
    for repetition in range(P.REPETITIONS):
        for entry in workload["entries"]:
            outcome = execute_run(
                query_id=str(entry["query_id"]),
                repetition=repetition,
                candidates=entry,
                data=data,
                request_index=request_index,
                oracle=oracle,
            )
            expected = {pair["pair_position"]: pair["request_hash"] for pair in entry["pairs"]}
            for row in outcome.trace_records:
                hash_matches.append(row["request_hash"] == expected[row["pair_position"]])
            all_trace.extend(outcome.trace_records)
            all_runs.append(outcome.run_record)

    total_comparisons = sum(r["comparison_count"] for r in all_runs)
    replay_ok = [bool(r["replay_consistent_with_lotus"]) for r in all_runs]
    status_ok = [str(r["execution_status"]) == "ok" for r in all_runs]
    invalid_rows = [r for r in all_trace if str(r["strict_parser_status"]) != "valid"]
    invalid_lotus_default = [
        r for r in invalid_rows if r["parse_status"] == "invalid_lotus_default_true"
    ]

    # determinism: same script -> same verdict vector and same answer per (query, rep-pair)
    by_query: dict[str, dict[int, dict]] = {}
    for run in all_runs:
        by_query.setdefault(run["query_id"], {})[run["repetition"]] = run
    determinism_ok = all(
        len({tuple(runs[rep]["request_hashes"]) for rep in runs}) == 1
        and len({tuple(runs[rep]["lotus_ordered_topk_doc_ids"]) for rep in runs}) == 1
        for runs in by_query.values()
    )

    # invalid injections must be exactly the two scripted positions, on every repetition
    injected_pairs = {
        pair["pair_id"]
        for pair in workload["entries"][0]["pairs"]
        if pair["pair_position"] in (4, 16)
    }
    invalid_positions_ok = {r["pair_id"] for r in invalid_rows} == injected_pairs
    invalid_count_expected = 2 * P.REPETITIONS

    smoke = {
        "substudy": P.SUBSISTUDY,
        "mode": "offline_scripted_smoke",
        "oracle": "ScriptedOracle (local, zero API calls)",
        "workload": Path(args.workload).name,
        "runs": len(all_runs),
        "total_comparisons": total_comparisons,
        "expected_comparisons": P.PLANNED_CALLS,
        "comparisons_per_run_ok": all(r["comparison_count"] == P.PAIRS_PER_QUERY for r in all_runs),
        "request_hash_matches_workload": all(hash_matches),
        "replay_gate_all_runs": all(replay_ok),
        "replay_gate_pass_count": sum(replay_ok),
        "execution_status_all_ok": all(status_ok),
        "determinism_same_script_same_answer": determinism_ok,
        "invalid_rows": len(invalid_rows),
        "invalid_rows_expected": invalid_count_expected,
        "invalid_positions_ok": invalid_positions_ok,
        "invalid_lotus_default_true_rows": len(invalid_lotus_default),
        "scripted_injections": sorted(injected_pairs),
        "physical_api_calls": sum(r["physical_api_calls"] for r in all_runs),
        "scripted_oracle_calls": len(oracle.calls),
        "distinct_topk_per_query": {
            qid: len({tuple(runs[rep]["lotus_ordered_topk_doc_ids"]) for rep in runs})
            for qid, runs in by_query.items()
        },
        "verdicts_all_valid_strict_outside_injections": len(invalid_rows) == invalid_count_expected,
        "smoke_pass": (
            total_comparisons == P.PLANNED_CALLS
            and all(hash_matches)
            and all(replay_ok)
            and all(status_ok)
            and determinism_ok
            and invalid_positions_ok
            and len(invalid_rows) == invalid_count_expected
            and len(oracle.calls) == P.PLANNED_CALLS
        ),
    }

    write_jsonl(args.calls_out, all_trace)
    write_jsonl(args.runs_out, all_runs)
    write_json(args.out, smoke)

    for key, value in smoke.items():
        if key not in ("scripted_injections",):
            print(f"{key}: {value}")
    print("SMOKE_PASS" if smoke["smoke_pass"] else "SMOKE_FAIL")
    return 0 if smoke["smoke_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
