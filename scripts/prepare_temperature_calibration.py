#!/usr/bin/env python3
"""Gate 3 Phase 2: freeze temperature calibration request plan (no paid calls).

Lines:
  A: temperature=null  (field omitted on the wire)
  B: temperature=0.0   (explicit zero)
Design: 4 dev queries x 8 pairs x 5 reps x 2 lines = 320 planned calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys_path_src = str(ROOT / "src")
import sys
sys.path.insert(0, sys_path_src)

from certirank.live import build_workload, collect_pair_calls, request_hash, canonical_request_payload
from certirank.provider import ApiConfig

OUT = ROOT / "data" / "gate3_nfcorpus" / "temperature_calibration"
N_QUERIES = 4
N_PAIRS = 8
N_REPS = 5


class DryClient:
    """Deterministic offline stand-in; never contacts a provider."""

    def __init__(self, line: str):
        self.line = line
        self.calls = 0

    def compare_pair(self, query: str, doc_a: str, doc_b: str) -> dict:
        self.calls += 1
        # force parseable placeholder; not model evidence
        raw = "Document 1" if (self.calls + len(self.line)) % 2 == 0 else "Document 2"
        return {
            "raw_response_content": raw,
            "strict_parsed_decision": raw == "Document 1",
            "strict_parser_status": "valid",
            "model": "DRY_RUN",
            "model_parameters": {"dry_run": True},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "provider_request_id": f"dry-{self.line}-{self.calls}",
        }


def pick_pairs(pairs: list[dict], n: int) -> list[dict]:
    """Deterministic: lexicographic pair_id, first n."""
    return sorted(pairs, key=lambda p: p["pair_id"])[:n]


def build_line_workload(queries: list[dict], api: ApiConfig, line: str, temperature) -> dict:
    model_params = api.model_parameters()
    model_params["temperature_requested"] = temperature
    # collect into a temp workload builder via queries with fixed pair subset
    # We build full workload then slice pairs.
    full = build_workload(
        queries,
        experiment_id=api.experiment_id,
        condition=f"gate3-calib|{line}",
        model=api.model,
        model_parameters=model_params,
        k=3,
    )
    for entry in full["entries"]:
        entry["pairs"] = pick_pairs(entry["pairs"], N_PAIRS)
        # reindex pair_position for the frozen subset
        for i, pair in enumerate(entry["pairs"]):
            pair = dict(pair)
            # keep original pair_position in pair_id/request_hash; add subset_index
            pair["subset_index"] = i
            entry["pairs"][i] = pair
    full["pairs_per_query"] = N_PAIRS
    full["pair_subset"] = True
    full["temperature_line"] = line
    full["temperature_requested"] = temperature
    return full


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=ROOT / "data" / "gate3_nfcorpus" / "dev_queries.json")
    parser.add_argument("--api-config", type=Path, default=ROOT / "configs" / "api_config.json")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()

    api = ApiConfig.load(args.api_config)
    all_queries = json.loads(args.queries.read_text(encoding="utf-8"))
    selected = all_queries[:N_QUERIES]
    if len(selected) < N_QUERIES:
        print(f"FAIL need {N_QUERIES} dev queries, got {len(selected)}")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    plan_rows = []
    summaries = {}

    for line, temp in (("A_null", None), ("B_zero", 0.0)):
        workload = build_line_workload(selected, api, line, temp)
        # freeze request identities
        for entry in workload["entries"]:
            texts = entry["doc_texts"]
            for pair in entry["pairs"]:
                payload = canonical_request_payload(
                    experiment_id=workload["experiment_id"],
                    condition=workload["condition"],
                    requested_model=workload["requested_model"],
                    model_parameters=workload["model_parameters"],
                    query_id=entry["query_id"],
                    query_text=entry["query_text"],
                    document_order=entry["doc_ids"],
                    document_texts=texts,
                    position_i=pair["position_i"],
                    position_j=pair["position_j"],
                    document_1_id=pair["document_1_id"],
                    document_2_id=pair["document_2_id"],
                    pair_position=pair["pair_position"],
                    pair_id=pair["pair_id"],
                )
                rh = request_hash(payload)
                assert rh == pair["request_hash"], (rh, pair["request_hash"])
                for rep in range(N_REPS):
                    plan_rows.append({
                        "line": line,
                        "temperature_requested": temp,
                        "query_id": entry["query_id"],
                        "pair_id": pair["pair_id"],
                        "pair_position": pair["pair_position"],
                        "subset_index": pair["subset_index"],
                        "repetition_id": rep,
                        "request_hash": rh,
                        "document_1_id": pair["document_1_id"],
                        "document_2_id": pair["document_2_id"],
                    })

        # dry-run collection to validate parse path
        rows = collect_pair_calls(workload, client=DryClient(line), repetitions=N_REPS, run_prefix=f"calib-{line}")
        assert len(rows) == N_QUERIES * N_PAIRS * N_REPS
        (args.out / f"workload_{line}.json").write_text(
            json.dumps(workload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / f"dryrun_rows_{line}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        summaries[line] = {
            "temperature_requested": temp,
            "wire_behavior": "omit temperature field" if temp is None else "send temperature: 0.0",
            "queries": [e["query_id"] for e in workload["entries"]],
            "pairs_per_query": N_PAIRS,
            "repetitions": N_REPS,
            "planned_calls": N_QUERIES * N_PAIRS * N_REPS,
            "request_hash_unique": len({p["request_hash"] for e in workload["entries"] for p in e["pairs"]}),
            "dry_run_rows": len(rows),
        }

    # cross-line: same pair must have different request_hash
    by_pair = {}
    for row in plan_rows:
        key = (row["query_id"], row["pair_id"])
        by_pair.setdefault(key, set()).add(row["request_hash"])
    same_hash = sum(1 for v in by_pair.values() if len(v) == 1)
    cross_line_distinct = all(len(v) == 2 for v in by_pair.values())

    plan = {
        "schema_version": 1,
        "status": "FROZEN_PLAN_DRY_RUN_ONLY",
        "paid_calls_authorized": False,
        "design": {
            "queries_from": "data/gate3_nfcorpus/dev_queries.json (first 4 lexicographic)",
            "queries": [q["query_id"] for q in selected],
            "pairs_per_query": N_PAIRS,
            "pair_selection": "lexicographic pair_id first 8 of 28",
            "repetitions": N_REPS,
            "lines": summaries,
            "planned_calls_total": len(plan_rows),
        },
        "cross_line": {
            "same_pair_request_hash_distinct": cross_line_distinct,
            "pairs_with_identical_hash_across_lines": same_hash,
        },
        "metrics_to_compute_after_paid_run": [
            "ever_flipped_pair_ratio",
            "pairwise_run_disagreement",
            "invalid_response_rate",
            "cross_line_result_difference",
        ],
        "interpretation_limits": [
            "5 repeats cannot certify determinism or a flip probability",
            "zero flips in 5 reps is not proof of strict determinism",
            "flipped-pair fraction is not a single-run flip probability",
        ],
        "plan_rows": plan_rows,
    }
    (args.out / "calibration_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("CALIBRATION PLAN FROZEN")
    print(" queries:", plan["design"]["queries"])
    print(" planned_calls:", plan["design"]["planned_calls_total"])
    print(" cross_line_distinct_hashes:", cross_line_distinct)
    print(" dry-run validated both lines")
    print(" PAID CALLS NOT EXECUTED — awaiting budget authorization")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
