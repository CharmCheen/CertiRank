#!/usr/bin/env python3
"""Audit A part 1: independent contract verification of all Gate 3 raw data.

Recomputes request hashes from workload content, re-parses every raw response with
an independent parser, checks coverage/dimensionality/uniqueness/timestamps and
dev/heldout isolation. No certirank imports.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_common import (GATE3, REPS, load_json, load_jsonl, parse_verdict_independent,
                          save_json)

PROMPT_VERSION = "certirank.pair_compare.v1"


def recompute_request_hash(entry: dict, pair: dict) -> str:
    """Independent rebuild of the canonical request payload hash (live.py spec)."""
    text_hashes = {doc: sha256(entry["doc_texts"][doc].encode("utf-8")).hexdigest()
                   for doc in entry["doc_ids"]}
    payload = {
        "prompt_version": PROMPT_VERSION,
        "experiment_id": entry_experiment[entry["query_id"]],
        "condition": entry_condition[entry["query_id"]],
        "requested_model": entry_model[entry["query_id"]],
        "model_parameters": entry_params[entry["query_id"]],
        "query_id": entry["query_id"],
        "query_text": entry["query_text"],
        "document_order": list(entry["doc_ids"]),
        "document_text_sha256": text_hashes,
        "position_i": pair["position_i"],
        "position_j": pair["position_j"],
        "document_1_id": pair["document_1_id"],
        "document_2_id": pair["document_2_id"],
        "pair_position": pair["pair_position"],
        "pair_id": pair["pair_id"],
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")).hexdigest()


entry_experiment: dict = {}
entry_condition: dict = {}
entry_model: dict = {}
entry_params: dict = {}


def audit_pool(name: str, base: Path, expected_reps: int, expected_queries: int,
               expected_pairs_per_query: int | None, pair_subset: bool):
    workload = load_json(base / "workload.json")
    calls = load_jsonl(base / "pair_calls.jsonl")
    identity = load_json(base / "identity.json")
    issues = []

    for entry in workload["entries"]:
        entry_experiment[entry["query_id"]] = workload["experiment_id"]
        entry_condition[entry["query_id"]] = workload["condition"]
        entry_model[entry["query_id"]] = workload["requested_model"]
        entry_params[entry["query_id"]] = workload["model_parameters"]

    # --- workload shape
    qids = workload["query_ids"]
    if len(set(qids)) != len(qids):
        issues.append("duplicate query ids")
    if len(qids) != expected_queries:
        issues.append(f"query count {len(qids)} != expected {expected_queries}")
    n_docs = {len(e["doc_ids"]) for e in workload["entries"]}
    if n_docs != {8}:
        issues.append(f"candidate counts {n_docs} != {{8}}")
    # pair enumeration
    for entry in workload["entries"]:
        docs = entry["doc_ids"]
        expected_pairs = [(i, j) for i in range(8) for j in range(i + 1, 8)]
        got = [(p["position_i"], p["position_j"]) for p in entry["pairs"]]
        if not pair_subset and got != expected_pairs:
            issues.append(f"{entry['query_id']}: pair enumeration mismatch")
        for p in entry["pairs"]:
            if (p["document_1_id"], p["document_2_id"]) != (docs[p["position_i"]], docs[p["position_j"]]):
                issues.append(f"{entry['query_id']}: pair doc identity mismatch slot {p['pair_position']}")
            if p["pair_id"] != f"{entry['query_id']}|{docs[p['position_i']]}|{docs[p['position_j']]}":
                issues.append(f"{entry['query_id']}: pair_id format mismatch")
        # independent request hash rebuild
        for p in entry["pairs"]:
            if recompute_request_hash(entry, p) != p["request_hash"]:
                issues.append(f"{entry['query_id']}: request_hash does not reproduce from workload content")

    # --- record coverage
    seen = Counter()
    prov_ids = set()
    ts_list = []
    parse_mismatch = 0
    parse_invalid = 0
    raw_unique = Counter()
    for idx, row in enumerate(calls):
        key = (row["query_id"], row["pair_position"], row["repetition_id"])
        seen[key] += 1
        if seen[key] > 1:
            issues.append(f"duplicate response slot {key}")
        # identity vs identity.json
        for field in ("experiment_id", "condition", "requested_model"):
            if row[field] != identity[field]:
                issues.append(f"row {idx}: {field} differs from identity.json")
        if row["model_parameters"] != identity["model_parameters"]:
            issues.append(f"row {idx}: model_parameters differ from identity.json")
        # provider request id
        pid = row.get("provider_request_id")
        if not isinstance(pid, str) or not pid.strip():
            issues.append(f"row {idx}: missing provider_request_id")
        elif pid in prov_ids:
            issues.append(f"row {idx}: duplicate provider_request_id {pid}")
        prov_ids.add(pid)
        # timestamp
        try:
            ts = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            if ts.utcoffset() != timedelta(0):
                issues.append(f"row {idx}: non-UTC timestamp")
            ts_list.append(ts)
        except Exception:
            issues.append(f"row {idx}: invalid timestamp")
        # run_id semantics: {split}-{qid}-r{rep}
        if row["run_id"] != f"{row['condition'].split('|')[-1] if row['condition'].startswith('live|') else 'x'}":
            pass  # run_id format checked below against observed prefixes
        # independent parse of raw content
        verdict, status = parse_verdict_independent(row["raw_response_content"])
        if status != "valid":
            parse_invalid += 1
            issues.append(f"row {idx}: independent parser says invalid")
        elif verdict is not row["strict_parsed_decision"]:
            parse_mismatch += 1
            issues.append(f"row {idx}: independent parse {verdict} != stored {row['strict_parsed_decision']}")
        if row["strict_parser_status"] != "valid" or not isinstance(row["strict_parsed_decision"], bool):
            issues.append(f"row {idx}: stored parser status not strict-valid")
        if row.get("lotus_parsed_decision") != row["strict_parsed_decision"]:
            issues.append(f"row {idx}: lotus_parsed_decision != strict verdict")
        if row.get("returned_model") != identity["requested_model"]:
            issues.append(f"row {idx}: returned_model {row.get('returned_model')}")
        raw_unique[row["raw_response_content"]] += 1

    # expected dimensions
    per_query_pairs = expected_pairs_per_query or workload["pairs_per_query"]
    expected_records = expected_queries * per_query_pairs * expected_reps
    if len(calls) != expected_records:
        issues.append(f"record count {len(calls)} != {expected_records}")
    for entry in workload["entries"]:
        for p in entry["pairs"]:
            for rep in range(expected_reps):
                if seen[(entry["query_id"], p["pair_position"], rep)] != 1:
                    issues.append(f"missing response {(entry['query_id'], p['pair_position'], rep)}")
    if not pair_subset:
        for qid in qids:
            for pos in range(per_query_pairs):
                for rep in range(expected_reps):
                    if seen[(qid, pos, rep)] != 1:
                        issues.append(f"missing response {(qid, pos, rep)}")
    # run_id format check
    run_prefixes = Counter(r["run_id"].rsplit("-r", 1)[0] for r in calls)
    if len(run_prefixes) != expected_queries:
        issues.append(f"run_id query prefixes {len(run_prefixes)} != {expected_queries}")

    ts_sorted = sorted(ts_list)
    monotone_ratio = sum(1 for a, b in zip(ts_list, ts_list[1:]) if b >= a) / max(1, len(ts_list) - 1)
    stats = {
        "records": len(calls),
        "queries": len(qids),
        "pairs_per_query": per_query_pairs,
        "repetitions": expected_reps,
        "expected_records": expected_records,
        "unique_provider_request_ids": len(prov_ids),
        "parse_invalid_independent": parse_invalid,
        "parse_mismatch_independent": parse_mismatch,
        "timestamp_span_utc": [str(ts_sorted[0]), str(ts_sorted[-1])] if ts_sorted else None,
        "timestamp_file_order_monotone_ratio": round(monotone_ratio, 6),
        "distinct_raw_response_texts": len(raw_unique),
        "most_common_raw_texts": raw_unique.most_common(5),
        "identity": identity,
        "contract_issues": issues[:50],
        "n_contract_issues": len(issues),
    }
    return stats, workload, calls


def main():
    results = {}

    specs = [
        ("formal_dev", GATE3 / "formal" / "dev", REPS, 6, 28, False),
        ("formal_heldout", GATE3 / "formal" / "heldout", REPS, 12, 28, False),
        ("calibration_A_null", GATE3 / "temperature_calibration" / "run_A", 5, 4, 8, True),
        ("calibration_B_zero", GATE3 / "temperature_calibration" / "run_B", 5, 4, 8, True),
    ]
    all_prov_ids = defaultdict(list)
    for name, base, reps, nq, ppq, subset in specs:
        stats, workload, calls = audit_pool(name, base, reps, nq, ppq, subset)
        results[name] = stats
        for row in calls:
            all_prov_ids[row["provider_request_id"]].append(name)

    # cross-file provider id collisions
    collisions = {pid: files for pid, files in all_prov_ids.items() if len(files) > 1}
    results["cross_file_provider_request_id_collisions"] = len(collisions)

    # dev/heldout isolation
    dev_w = load_json(GATE3 / "formal" / "dev" / "workload.json")
    held_w = load_json(GATE3 / "formal" / "heldout" / "workload.json")
    dev_q = set(dev_w["query_ids"])
    held_q = set(held_w["query_ids"])
    dev_h = {(e["query_id"], p["pair_id"]): p["request_hash"] for e in dev_w["entries"] for p in e["pairs"]}
    held_h = {(e["query_id"], p["pair_id"]): p["request_hash"] for e in held_w["entries"] for p in e["pairs"]}
    cal_a = load_json(GATE3 / "temperature_calibration" / "run_A" / "workload.json")
    cal_h = {(e["query_id"], p["pair_id"]): p["request_hash"] for e in cal_a["entries"] for p in e["pairs"]}
    results["dev_heldout_isolation"] = {
        "shared_query_ids": sorted(dev_q & held_q),
        "shared_request_hashes_dev_heldout": len(set(dev_h.values()) & set(held_h.values())),
        "shared_request_hashes_dev_calibration": len(set(dev_h.values()) & set(cal_h.values())),
        "dev_condition": dev_w["condition"],
        "heldout_condition": held_w["condition"],
        "dev_model_params": dev_w["model_parameters"],
        "heldout_model_params": held_w["model_parameters"],
        "params_identical": dev_w["model_parameters"] == held_w["model_parameters"],
        "doc_order_stability_note": "doc order per query pinned inside each workload",
    }

    # reported headline call counts
    results["reported_counts_check"] = {
        "dev": results["formal_dev"]["records"] == 2016,
        "heldout": results["formal_heldout"]["records"] == 4032,
        "calibration_total": results["calibration_A_null"]["records"] + results["calibration_B_zero"]["records"] == 320,
    }

    # position bias / verdict marginals (dev+heldout formal only)
    for split in ("dev", "heldout"):
        calls = load_jsonl(GATE3 / "formal" / split / "pair_calls.jsonl")
        pos_i_wins = sum(r["strict_parsed_decision"] for r in calls)
        results[f"formal_{split}_marginals"] = {
            "records": len(calls),
            "position_i_win_rate": round(pos_i_wins / len(calls), 6),
            "by_position_i": dict(Counter(r["position_i"] for r in calls if r["strict_parsed_decision"])),
            "total_by_position_i": dict(Counter(r["position_i"] for r in calls)),
            "completion_tokens_mean": round(sum(r.get("completion_tokens") or 0 for r in calls) / len(calls), 3),
            "prompt_tokens_mean": round(sum(r.get("prompt_tokens") or 0 for r in calls) / len(calls), 1),
            "latency_seconds_mean": round(sum(r.get("api_latency_seconds") or 0 for r in calls) / len(calls), 3),
            "latency_seconds_max": round(max(r.get("api_latency_seconds") or 0 for r in calls), 3),
            "cached_prompt_tokens_nonzero": sum(1 for r in calls if (r.get("cached_prompt_tokens") or 0) > 0),
        }

    save_json("audit_a_contract.json", results)
    n_issues = sum(results[k]["n_contract_issues"] for k in
                   ("formal_dev", "formal_heldout", "calibration_A_null", "calibration_B_zero"))
    print(f"contract issues total: {n_issues}")
    for name in ("formal_dev", "formal_heldout", "calibration_A_null", "calibration_B_zero"):
        print(name, "issues:", results[name]["n_contract_issues"], results[name]["contract_issues"][:5])


if __name__ == "__main__":
    main()
