"""Real-export regression gates: pin the identity and re-parse original responses."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certirank.pair_pool import load_jsonl, load_workload, validate_pair_calls

ROOT = Path(__file__).resolve().parents[1]


def test_historical_contract_and_uniform_model_drift():
    data = ROOT / "data/external/lotus_gate2"
    calls = load_jsonl(data / "gate2_live_pair_calls.jsonl")
    workload = load_workload(data / "gate2_workload.json")
    identity = json.loads((ROOT / "configs/gate2_identity.json").read_text(encoding="utf-8"))
    assert validate_pair_calls(calls, workload, expected_identity=identity)["contract_ok"]
    changed = [{**row, "requested_model": "uniform-drift"} for row in calls]
    result = validate_pair_calls(changed, workload, expected_identity=identity)
    assert not result["contract_ok"]
    assert any("pinned identity" in issue for issue in result["issues"])


def test_historical_failure_probes_are_rejected():
    data = ROOT / "data/external/lotus_gate2"
    calls = load_jsonl(data / "gate2_live_pair_calls.jsonl")
    workload = load_workload(data / "gate2_workload.json")
    for updates in ({"condition": "other", "model_parameters": {}},
                    {"document_1_id": calls[0]["document_2_id"], "document_2_id": calls[0]["document_1_id"]},
                    {"raw_response_content": "Document 2"}, {"lotus_parsed_decision": None}):
        changed = [{**calls[0], **updates}, *calls[1:]]
        assert not validate_pair_calls(changed, workload)["contract_ok"]
