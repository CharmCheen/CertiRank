"""Synthetic correctness tests for metrics and the Pair Response Pool contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certirank.operators.metrics import (  # noqa: E402
    jaccard_at_k,
    kendall_tau_b,
    operator_level_metrics,
    paper_dcg,
    paper_ndcg_at_k,
    query_level_metrics,
)
from certirank.pair_pool import (  # noqa: E402
    build_pair_pool,
    pool_summary,
    validate_pair_calls,
)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_tau_identity_and_reversal():
    ranking = ["a", "b", "c", "d"]
    assert kendall_tau_b(ranking, list(ranking)) == 1.0
    assert kendall_tau_b(ranking, list(reversed(ranking))) == -1.0


def test_tau_one_swap():
    # one adjacent swap among 4 docs: 5 concordant, 1 discordant -> 4/6
    assert kendall_tau_b(["a", "b", "c", "d"], ["b", "a", "c", "d"]) == pytest.approx(4 / 6)


def test_tau_rejects_mismatched_documents():
    with pytest.raises(ValueError):
        kendall_tau_b(["a", "b"], ["a", "c"])


def test_jaccard_bounds():
    assert jaccard_at_k(["a", "b"], ["b", "a"]) == 1.0
    assert jaccard_at_k(["a"], ["b"]) == 0.0
    assert jaccard_at_k(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


def test_paper_dcg_and_ndcg_single_relevant():
    assert paper_dcg([1]) == pytest.approx(1.0)  # (2^1-1)/log2(2)
    # single relevant doc at rank 1 -> 1.0; at rank 3 -> 1/log2(4)
    assert paper_ndcg_at_k([1, 0, 0], [1], 3) == pytest.approx(1.0)
    assert paper_ndcg_at_k([0, 0, 1], [1], 3) == pytest.approx(1.0 / 2.0)
    assert paper_ndcg_at_k([0, 0, 0], [1], 3) == 0.0


def _trace_row(pair_id, repetition, verdict, status="valid"):
    return {
        "pair_id": pair_id,
        "repetition_id": repetition,
        "strict_parsed_decision": verdict,
        "strict_parser_status": status,
    }


def test_operator_level_ever_flip_vs_disagreement():
    # 2 pairs; pair A flips between r0 and r1, pair B never does.
    rows = [
        _trace_row("A", 0, True), _trace_row("A", 1, False),
        _trace_row("B", 0, True), _trace_row("B", 1, True),
    ]
    metrics = operator_level_metrics(rows)
    assert metrics["ever_flipped_pair_count"] == 1
    assert metrics["ever_flipped_pair_ratio"] == 0.5
    assert metrics["run_pair_disagreements"][0]["disagreement_rate"] == 0.5


def test_operator_level_invalid_never_counts_as_flip():
    rows = [
        _trace_row("A", 0, True), _trace_row("A", 1, True, status="invalid_json"),
        _trace_row("B", 0, True), _trace_row("B", 1, True),
    ]
    metrics = operator_level_metrics(rows)
    assert metrics["ever_flipped_pair_count"] == 0
    assert metrics["invalid_response_rate"] == pytest.approx(0.25)
    # the run pair compares only strict-valid pairs
    assert metrics["run_pair_disagreements"][0]["compared_pairs"] == 1


def test_query_level_agreement_and_distinct_answers():
    topks = [["a", "b", "c"], ["a", "b", "c"], ["a", "b", "d"]]
    rankings = [["a", "b", "c", "d"], ["a", "b", "c", "d"], ["a", "b", "d", "c"]]
    metrics = query_level_metrics(topks, rankings, k=3)
    assert metrics["distinct_topk_ordered"] == 2
    assert metrics["distinct_topk_sets"] == 2
    assert metrics["topk_set_exact_agreement_rate"] == pytest.approx(1 / 3)
    assert metrics["topk_ordered_exact_agreement_rate"] == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# pair pool contract
# ---------------------------------------------------------------------------

QUERIES = ["q1", "q2"]
DOCS = {q: [f"{q}-d0", f"{q}-d1", f"{q}-d2"] for q in QUERIES}


def _workload(entries):
    return {
        "query_ids": list(QUERIES),
        "pairs_per_query": 3,
        "k": 2,
        "entries": entries,
    }


def _entry(query_id, request_hash_seq):
    docs = list(DOCS[query_id])
    pairs = []
    idx = 0
    for i in range(3):
        for j in range(i + 1, 3):
            pairs.append(
                {
                    "pair_position": idx,
                    "position_i": i,
                    "position_j": j,
                    "document_1_id": docs[i],
                    "document_2_id": docs[j],
                    "pair_id": f"{query_id}|{docs[i]}|{docs[j]}",
                    "request_hash": request_hash_seq[idx],
                }
            )
            idx += 1
    return {"query_id": query_id, "doc_ids": docs, "pairs": pairs, "qrels": {docs[0]: 1}}


def _record(workload, query_id, pair_position, repetition, verdict, **overrides):
    slot = next(
        p for e in workload["entries"] if e["query_id"] == query_id for p in e["pairs"]
        if p["pair_position"] == pair_position
    )
    row = {
        "query_id": query_id,
        "document_1_id": slot["document_1_id"],
        "document_2_id": slot["document_2_id"],
        "position_i": slot["position_i"],
        "position_j": slot["position_j"],
        "pair_position": pair_position,
        "pair_id": slot["pair_id"],
        "request_hash": slot["request_hash"],
        "requested_model": "test-model",
        "model_parameters": {"max_tokens_requested": 128},
        "experiment_id": "test|cond",
        "condition": "cond",
        "document_order": list(DOCS[query_id]),
        "run_id": f"R-{query_id}-{repetition}",
        "repetition_id": repetition,
        "provider_request_id": f"prov-{query_id}-{pair_position}-{repetition}",
        "timestamp_utc": "2026-10-15T00:00:00+00:00",
        "raw_response_content": "Document 1" if verdict else "Document 2",
        "strict_parsed_decision": verdict,
        "strict_parser_status": "valid",
        "lotus_parsed_decision": verdict,
    }
    row.update(overrides)
    return row


def _complete_records(workload, reps=2):
    records = []
    for query_id in QUERIES:
        for position in range(3):
            for repetition in range(reps):
                records.append(
                    _record(workload, query_id, position, repetition, verdict=bool((position + repetition) % 2))
                )
    return records


def test_valid_synthetic_pool_passes_contract():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert validation["contract_ok"], validation["issues"]
    assert validation["records"] == validation["expected_records"]

    pool = build_pair_pool(records)
    summary = pool_summary(pool)
    assert summary["queries"] == 2
    assert summary["pair_slots"] == 6
    assert summary["responses"] == 12
    assert pool["q1"]["verdicts"][0][0] is False and pool["q1"]["verdicts"][0][1] is True


def test_missing_repetition_breaks_completeness():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    del records[3]  # one response dropped
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert not validation["contract_ok"]
    assert any("C6" in issue for issue in validation["issues"])


def test_request_hash_drift_across_reps_breaks_pool_usability():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    # rewrite one response's request identity: same pair, different request
    records[3]["request_hash"] = "hash-q1-0-DRIFT"
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert not validation["contract_ok"]
    assert any("C3" in issue or "C7" in issue for issue in validation["issues"])


def test_same_pair_id_with_different_identity_is_not_reusable():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    # same pair_id but the document order inside the pair swapped -> different request
    records[5]["document_1_id"], records[5]["document_2_id"] = (
        records[5]["document_2_id"], records[5]["document_1_id"]
    )
    records[5]["position_i"], records[5]["position_j"] = records[5]["position_j"], records[5]["position_i"]
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert not validation["contract_ok"]
    assert any("C3" in issue for issue in validation["issues"])


@pytest.mark.parametrize("overrides", [
    {"condition": "other"},
    {"requested_model": "other-model"},
    {"model_parameters": {}},
    {"raw_response_content": "Document 1"},
    {"lotus_parsed_decision": None},
    {"pair_position": None},
    {"repetition_id": "0"},
])
def test_inconsistent_record_is_rejected(overrides):
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    records[0].update(overrides)  # original answer is Document 2 / False
    result = validate_pair_calls(records, workload, expected_repetitions=2)
    assert not result["contract_ok"]


def test_workload_entry_order_does_not_change_query_identity():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    workload["entries"].reverse()
    assert validate_pair_calls(records, workload, expected_repetitions=2)["contract_ok"]


def test_pool_rejects_duplicate_call_instead_of_overwriting():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    with pytest.raises(ValueError):
        build_pair_pool(records + [records[0]])


def test_missing_contract_field_is_flagged():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    del records[0]["raw_response_content"]
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert any("C1" in issue for issue in validation["issues"])


def test_parser_disagreement_is_flagged():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    records[2]["lotus_parsed_decision"] = not records[2]["strict_parsed_decision"]
    validation = validate_pair_calls(records, workload, expected_repetitions=2)
    assert any("C8" in issue for issue in validation["issues"])


def test_jsonl_loader_roundtrip(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text(
        "\n".join(json.dumps({"i": i}) for i in range(3)) + "\n", encoding="utf-8"
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from certirank.pair_pool import load_jsonl

    assert [row["i"] for row in load_jsonl(path)] == [0, 1, 2]


def test_duplicate_workload_queries_cannot_pass_contract():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    workload["query_ids"].append("q1")
    result = validate_pair_calls(records, workload, expected_repetitions=2)
    assert not result["contract_ok"]


@pytest.mark.parametrize("field,value", [("timestamp_utc", ""), ("timestamp_utc", "2026-10-15"),
                                         ("run_id", ""), ("requested_model", "")])
def test_empty_or_unzoned_provenance_is_rejected(field, value):
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    for row in records:
        row[field] = value
    assert not validate_pair_calls(records, workload, expected_repetitions=2)["contract_ok"]


def test_nested_boolean_numeric_identity_drift_is_rejected():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    for row in records:
        row["model_parameters"] = {"thinking": 1}
    result = validate_pair_calls(records, workload, expected_repetitions=2,
                                 expected_identity={"model_parameters": {"thinking": True}})
    assert not result["contract_ok"]


def test_relaxed_pool_status_never_coerces_nonboolean_verdicts():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    records[0]["strict_parsed_decision"] = "False"
    with pytest.raises(ValueError):
        build_pair_pool(records, require_valid=False)


def test_jsonl_record_must_be_an_object(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('[1, 2, 3]\n')
    from certirank.pair_pool import load_jsonl
    with pytest.raises(ValueError, match="object"):
        load_jsonl(path)


def test_mutating_one_record_does_not_mutate_shared_fixtures():
    workload = _workload([_entry(q, [f"hash-{q}-{p}" for p in range(3)]) for q in QUERIES])
    records = _complete_records(workload)
    before = list(workload["entries"][0]["doc_ids"])
    records[0]["document_order"].reverse()
    assert workload["entries"][0]["doc_ids"] == before
    assert records[1]["document_order"] == before
    assert not validate_pair_calls(records, workload, expected_repetitions=2)["contract_ok"]
