"""Live acquisition contract: request identity, collection, and pool pinning."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.live import (
    build_manifest,
    build_workload,
    collect_pair_calls,
    request_hash,
    write_acquisition,
)
from certirank.pair_pool import build_pair_pool, validate_pair_calls
from certirank.provider import ApiConfig


class FakeClient:
    def __init__(self):
        self.calls = 0

    def compare_pair(self, query, doc_a, doc_b):
        self.calls += 1
        raw = "Document 1" if self.calls % 2 else "Document 2"
        return {
            "raw_response_content": raw,
            "strict_parsed_decision": raw == "Document 1",
            "strict_parser_status": "valid",
            "model": "demo-model",
            "model_parameters": {"temperature_requested": 0.0},
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 0}},
            "provider_request_id": f"prov-{self.calls}",
        }


def sample_queries():
    return [
        {
            "query_id": "q1",
            "query_text": "what is alpha",
            "doc_ids": ["a", "b", "c"],
            "doc_texts": {"a": "alpha is the first letter", "b": "beta is second", "c": "gamma is third"},
            "qrels": {"a": 1},
        },
        {
            "query_id": "q2",
            "query_text": "what is beta",
            "doc_ids": ["x", "y", "z"],
            "doc_texts": {"x": "x-ray text", "y": "year of beta", "z": "zebra"},
            "qrels": {"y": 1},
        },
    ]


def test_request_hash_is_stable_and_order_sensitive():
    base = dict(
        experiment_id="E", condition="C", requested_model="m",
        model_parameters={"temperature_requested": 0.0},
        query_id="q", query_text="t", document_order=["a", "b"],
        document_texts={"a": "text-a", "b": "text-b"},
        position_i=0, position_j=1, document_1_id="a", document_2_id="b",
        pair_position=0, pair_id="q|a|b",
    )
    h1 = request_hash(base)
    assert h1 == request_hash({**base})
    assert h1 != request_hash({**base, "query_text": "other"})
    assert h1 != request_hash({**base, "model_parameters": {"temperature_requested": 0.2}})
    assert h1 != request_hash({**base, "document_texts": {"a": "changed", "b": "text-b"}})


def test_collect_then_contract_and_pool(tmp_path):
    workload = build_workload(
        sample_queries(),
        experiment_id="CertiRank|live",
        condition="live|dev",
        model="demo-model",
        model_parameters={"temperature_requested": 0.0, "thinking_mode_requested": "low"},
        k=1,
    )
    assert workload["pairs_per_query"] == 3  # C(3,2)
    client = FakeClient()
    rows = collect_pair_calls(workload, client=client, repetitions=4, run_prefix="dev")
    assert client.calls == 2 * 3 * 4
    assert len(rows) == 24
    validation = validate_pair_calls(rows, workload, expected_repetitions=4)
    assert validation["contract_ok"], validation["issues"]
    pool = build_pair_pool(rows)
    assert set(pool) == {"q1", "q2"}

    identity = {
        "experiment_id": workload["experiment_id"],
        "condition": workload["condition"],
        "requested_model": workload["requested_model"],
        "model_parameters": workload["model_parameters"],
    }
    paths = write_acquisition(tmp_path / "dev", workload=workload, rows=rows, identity=identity)
    assert paths["pair_calls"].read_text(encoding="utf-8").count("\n") == 24

    groups = [
        {"query_id": "q1", "query_group": "g1", "split": "dev"},
        {"query_id": "q2", "query_group": "g2", "split": "dev"},
    ]
    manifest = build_manifest(
        dataset_id="demo",
        source_kind="synthetic",
        query_groups=groups,
        pools={"dev": {k: paths[k] for k in ("workload", "pair_calls", "identity")}},
    )
    assert manifest["pools"]["dev"]["sha256"]["pair_calls"]
    assert manifest["query_groups"][0]["label_source"]


def test_limit_pairs_and_unparseable_response(tmp_path):
    workload = build_workload(
        sample_queries()[:1],
        experiment_id="E",
        condition="C",
        model="m",
        model_parameters={},
        k=1,
    )
    rows = collect_pair_calls(workload, client=FakeClient(), repetitions=2, limit_pairs=1)
    assert len(rows) == 2

    class Bad(FakeClient):
        def compare_pair(self, query, doc_a, doc_b):
            return {"raw_response_content": "maybe", "strict_parsed_decision": None, "strict_parser_status": None,
                    "provider_request_id": "x"}

    with pytest.raises(ValueError, match="unparseable"):
        collect_pair_calls(workload, client=Bad(), repetitions=1)
