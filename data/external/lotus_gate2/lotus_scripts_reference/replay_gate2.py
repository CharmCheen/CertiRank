#!/usr/bin/env python
"""Gate 2B: offline propagation analysis on the Gate 2A traces. Zero API calls.

Three layers, in the order the correctness gate demands:

1. **Replay gate** — every run's 28 verdicts are pushed through the offline naive-sort
   reconstruction; the replayed Top-K must equal the Top-K LOTUS returned. Any mismatch
   aborts the analysis (exit 3) rather than producing numbers built on a broken replay.
2. **Single-point counterfactuals** — from each query's repetition-0 verdict matrix, each
   valid verdict is flipped in isolation; the effect on the full ranking, Top-K
   membership, and Top-K order is classified per pair.
3. **Multi-point propagation + impact concentration** — only *real* verdict vectors
   observed across the repetitions are compared; no synthetic combination is
   constructed, so nothing is presented as a provider probability sample.

Writes ``gate2_propagation_analysis.json``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.protocol import write_json  # noqa: E402
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.protocol import read_jsonl  # noqa: E402
from experiments.lotus_nondeterminism.gate2.replay import (  # noqa: E402
    EFFECT_ORDER,
    impact_concentration,
    multi_point_propagation,
    naive_replay,
    single_point_counterfactuals,
)
from experiments.lotus_nondeterminism.gate2.workload import load_workload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", default=str(P.CALLS_PATH))
    parser.add_argument("--runs", default=str(P.RUNS_PATH))
    parser.add_argument("--workload", default=str(P.WORKLOAD_PATH))
    parser.add_argument("--out", default=str(P.PROPAGATION_PATH))
    args = parser.parse_args()

    calls = read_jsonl(args.calls)
    runs = read_jsonl(args.runs)
    workload = load_workload(args.workload)

    entries = {str(e["query_id"]): e for e in workload["entries"]}

    # 1 -- replay gate over every recorded run
    replay_results: list[dict] = []
    for run in runs:
        query_id = str(run["query_id"])
        entry = entries[query_id]
        position_of = {str(d): pos for pos, d in enumerate(entry["doc_ids"])}
        rows = sorted(
            [r for r in calls if str(r["query_id"]) == query_id and int(r["repetition_id"]) == int(run["repetition"])],
            key=lambda r: int(r["pair_position"]),
        )
        # the production verdict vector is what the sort actually consumed
        verdicts = [bool(r["lotus_parsed_decision"]) for r in rows]
        replay = naive_replay(verdicts, len(entry["doc_ids"]))
        lotus_positions = [position_of[d] for d in run["lotus_ordered_topk_doc_ids"]]
        gate_ok = replay["indexes"][: P.K] == lotus_positions
        # cross-check against the verdicts LOTUS itself returned, if recorded
        recorded_ok = run.get("replay_consistent_with_lotus")
        replay_results.append(
            {
                "run_id": run["run_id"],
                "query_id": query_id,
                "repetition": run["repetition"],
                "replay_topk_ok": gate_ok,
                "recorded_replay_flag": recorded_ok,
                "agree": gate_ok == bool(recorded_ok) if recorded_ok is not None else True,
            }
        )
    gate_all = all(r["replay_topk_ok"] for r in replay_results)
    if not gate_all:
        bad = [r["run_id"] for r in replay_results if not r["replay_topk_ok"]]
        print(f"ABORT: replay gate failed for {bad}; fix before analysing propagation", file=sys.stderr)
        return 3
    print(f"replay gate: {len(replay_results)}/{len(replay_results)} runs reproduce LOTUS's Top-K")

    # 2 + 3 -- per query (queries without any recorded run in this artifact set —
    # e.g. a single-run live smoke — are skipped: propagation needs >= 1 run, and
    # a run-pair comparison needs >= 2)
    per_query: list[dict] = []
    for query_id, entry in entries.items():
        doc_ids = [str(d) for d in entry["doc_ids"]]
        pair_ids = [pair["pair_id"] for pair in entry["pairs"]]
        query_calls = [r for r in calls if str(r["query_id"]) == query_id]
        query_runs = sorted(
            [r for r in runs if str(r["query_id"]) == query_id], key=lambda r: int(r["repetition"])
        )
        if not query_runs:
            continue

        vectors: list[list[bool]] = []
        labels: list[str] = []
        valid_positions_by_rep: dict[int, list[int]] = {}
        for run in query_runs:
            rep = int(run["repetition"])
            rows = sorted(
                [r for r in query_calls if int(r["repetition_id"]) == rep],
                key=lambda r: int(r["pair_position"]),
            )
            if len(rows) != P.PAIRS_PER_QUERY:
                print(
                    f"ABORT: query {query_id} rep {rep} has {len(rows)} trace rows, expected {P.PAIRS_PER_QUERY}",
                    file=sys.stderr,
                )
                return 3
            vectors.append([bool(r["lotus_parsed_decision"]) for r in rows])
            labels.append(f"r{rep}")
            valid_positions_by_rep[rep] = [
                int(r["pair_position"]) for r in rows if str(r["strict_parser_status"]) == "valid"
            ]

        base = vectors[0]
        base_valid = valid_positions_by_rep[int(query_runs[0]["repetition"])]

        counterfactuals = single_point_counterfactuals(
            base_verdicts=base, doc_ids=doc_ids, pair_ids=pair_ids, k=P.K,
            flipped_positions=base_valid,
        )
        multipoint = multi_point_propagation(
            verdict_vectors=vectors, doc_ids=doc_ids, k=P.K, labels=labels
        )
        concentration = impact_concentration(
            base_verdicts=base,
            verdict_vectors=vectors,
            doc_ids=doc_ids,
            pair_ids=pair_ids,
            k=P.K,
            flipped_positions=base_valid,
        )

        # restrict the ever-flipped/high-impact overlap to valid observations
        per_query.append(
            {
                "query_id": query_id,
                "base_repetition": int(query_runs[0]["repetition"]),
                "base_valid_pair_count": len(base_valid),
                "single_point_counterfactuals": [e.as_record() for e in counterfactuals],
                "single_point_counts": {
                    cls: sum(1 for e in counterfactuals if e.effect_class == cls)
                    for cls in EFFECT_ORDER
                },
                "multi_point": multipoint,
                "impact_concentration": concentration,
            }
        )

    joint_emergent_pairs = sum(
        1
        for q in per_query
        for report in q["multi_point"]["run_pair_reports"]
        if report.get("joint_emergent")
    )
    cancelling_pairs = sum(
        1
        for q in per_query
        for report in q["multi_point"]["run_pair_reports"]
        if report.get("cancelling")
    )

    analysis = {
        "substudy": P.SUBSISTUDY,
        "gate": "G2B",
        "sources": {"calls": Path(args.calls).name, "runs": Path(args.runs).name},
        "replay_gate": {
            "runs_checked": len(replay_results),
            "all_pass": gate_all,
            "per_run": replay_results,
        },
        "counterfactual_scope_note": (
            "Single-point flips are deterministic relation-level analysis on one fixed "
            "run's verdict matrix (others held fixed); they are not provider probabilities."
        ),
        "multi_point_scope_note": (
            "Multi-point propagation compares only REAL verdict vectors observed across "
            "repetitions; no synthetic combination was constructed."
        ),
        "joint_emergent_run_pairs": joint_emergent_pairs,
        "cancelling_run_pairs": cancelling_pairs,
        "per_query": per_query,
    }

    write_json(args.out, analysis)
    print(f"wrote {args.out}")
    for q in per_query:
        counts = q["single_point_counts"]
        conc = q["impact_concentration"]
        print(
            f"query {q['query_id']}: single-flip (of {q['base_valid_pair_count']} valid) "
            f"inert/below-topk/order/set = "
            f"{counts['none']}/{counts['below_topk']}/{counts['topk_order']}/{counts['topk_set']} | "
            f"ever-flipped pairs {conc['ever_flipped_count']}, "
            f"overlap(topk-set impact) {len(conc['overlap_ever_flipped_and_topk_set'])}, "
            f"unstable-but-inert {len(conc['unstable_but_inert_pair_positions'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
