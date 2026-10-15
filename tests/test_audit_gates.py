"""Audit entry points must stop before consuming invalid input or writing success."""

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_failure_stops_before_contract_or_recompute(monkeypatch, tmp_path):
    module = load_script("audit_gate2")
    monkeypatch.setattr(sys, "argv", ["audit_gate2.py", "--check"])
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "step_manifest", lambda: ([{"id": "IMM-1", "ok": False, "description": "drift"}], {}))

    def should_not_run():
        pytest.fail("failed manifest must stop before data is consumed")

    monkeypatch.setattr(module, "step_contract", should_not_run)
    assert module.main() == 1
    assert list(tmp_path.iterdir()) == []


def test_contract_failure_returns_nonzero_without_recompute_or_writing(monkeypatch, tmp_path):
    module = load_script("audit_gate2")
    monkeypatch.setattr(sys, "argv", ["audit_gate2.py", "--check"])
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "step_manifest", lambda: ([{"id": "IMM-1", "ok": True, "description": "ok"}], {}))

    def invalid_contract():
        raise ValueError("invalid exported identity")

    monkeypatch.setattr(module, "step_contract", invalid_contract)
    assert module.main() == 1
    assert list(tmp_path.iterdir()) == []


def test_budget_estimator_rejects_a_drifted_copy(monkeypatch, tmp_path):
    module = load_script("pilot_budget")
    calls = ROOT / "data/external/lotus_gate2/gate2_live_pair_calls.jsonl"
    # Keep record count and JSON validity; byte-level provenance must still reject it.
    (tmp_path / calls.name).write_bytes(calls.read_bytes() + b"\n")
    monkeypatch.setattr(module, "DATA", tmp_path)
    with pytest.raises(ValueError, match="manifest"):
        module.build_artifacts()


def test_duplicate_query_run_cannot_overwrite_a_missing_run(monkeypatch):
    module = load_script("audit_gate2")
    original = module.load_jsonl

    def load_with_duplicate(path):
        rows = original(path)
        if path.name == "gate2_live_query_results.jsonl":
            rows[-1] = dict(rows[0])
        return rows

    monkeypatch.setattr(module, "load_jsonl", load_with_duplicate)
    with pytest.raises(ValueError, match="roster"):
        module.step_contract()
