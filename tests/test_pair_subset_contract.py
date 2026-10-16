"""Pair-subset contract and frozen-workload collection guards."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.live import build_workload, collect_pair_calls
from certirank.pair_pool import validate_pair_calls
from certirank.provider import ApiConfig


class Fake:
    n = 0

    def compare_pair(self, q, a, b):
        Fake.n += 1
        raw = "Document 1" if Fake.n % 2 else "Document 2"
        return {
            "raw_response_content": raw,
            "strict_parsed_decision": raw == "Document 1",
            "strict_parser_status": "valid",
            "provider_request_id": f"id-{Fake.n}",
        }


def mini_queries():
    return [{
        "query_id": "q1",
        "query_text": "which is about cats",
        "doc_ids": ["a", "b", "c", "d"],
        "doc_texts": {k: f"text {k}" for k in "abcd"},
        "qrels": {"a": 2, "c": 1},
    }]


def test_complete_enumeration_required_by_default():
    queries = mini_queries()
    workload = build_workload(queries, experiment_id="E", condition="C", model="m",
                              model_parameters={"temperature_requested": 0.0}, k=1)
    # 6 pairs for n=4
    assert workload["pairs_per_query"] == 6
    rows = collect_pair_calls(workload, client=Fake(), repetitions=2)
    v = validate_pair_calls(rows, workload, expected_repetitions=2)
    assert v["contract_ok"], v["issues"]


def test_pair_subset_contract_and_hash_stability():
    queries = mini_queries()
    params = {"temperature_requested": None}
    full = build_workload(queries, experiment_id="E", condition="gate3-calib|A_null", model="m",
                          model_parameters=params, k=3)
    # keep first 2 pairs by pair_id
    for entry in full["entries"]:
        entry["pairs"] = sorted(entry["pairs"], key=lambda p: p["pair_id"])[:2]
    full["pairs_per_query"] = 2
    full["pair_subset"] = True
    rows = collect_pair_calls(full, client=Fake(), repetitions=3)
    assert len(rows) == 2 * 3
    identity = {
        "experiment_id": "E",
        "condition": "gate3-calib|A_null",
        "requested_model": "m",
        "model_parameters": params,
    }
    v = validate_pair_calls(rows, full, expected_repetitions=3, expected_identity=identity)
    assert v["contract_ok"], v["issues"]

    # incomplete without pair_subset flag
    bad = json.loads(json.dumps(full))
    bad.pop("pair_subset")
    v2 = validate_pair_calls(rows, bad, expected_repetitions=3)
    assert not v2["contract_ok"]


def test_temperature_mismatch_between_api_and_workload():
    api = ApiConfig.from_dict({
        "base_url": "https://x.example", "model": "m",
        "api_key": "sk", "temperature": 0.0,
    })
    assert api.model_parameters()["temperature_requested"] == 0.0
    api_null = ApiConfig.from_dict({
        "base_url": "https://x.example", "model": "m",
        "api_key": "sk", "temperature": None,
    })
    assert api_null.model_parameters()["temperature_requested"] is None
    # request hashes must differ for the same pair under different temperature
    from certirank.live import build_workload
    w0 = build_workload(mini_queries(), experiment_id="E", condition="C", model="m",
                        model_parameters=api.model_parameters(), k=1)
    w1 = build_workload(mini_queries(), experiment_id="E", condition="C", model="m",
                        model_parameters=api_null.model_parameters(), k=1)
    h0 = w0["entries"][0]["pairs"][0]["request_hash"]
    h1 = w1["entries"][0]["pairs"][0]["request_hash"]
    assert h0 != h1
