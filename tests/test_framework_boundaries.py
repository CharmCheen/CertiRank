"""Adversarial review regressions at the public execution/evaluation boundaries."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certirank.evaluation import run_algorithm, summarize_disagreement
from certirank.diagnostics import conditional_effect
from certirank.operators.tournament import tournament
from certirank.operators.metrics import kendall_tau_b, operator_level_metrics, query_level_metrics
from certirank.operators.naive_sort import naive_replay
from certirank.oracle import ExecutionLedger, TableSource
from certirank.repair import ReviewSession, review_budget


@pytest.mark.parametrize("fraction", ["-1", "bad", 0.6])
def test_invalid_budget_is_rejected_before_reading_any_response(fraction):
    ledger = ExecutionLedger(TableSource({(0, 1): [True] * 3}), (0, 1, 2))
    with pytest.raises(ValueError):
        ReviewSession(2, 1, ledger, fraction)
    assert ledger.base_calls == 0 and ledger.events == ()


@pytest.mark.parametrize("base_calls,fraction", [(-1, "0.4"), (True, "0.4"), (5, 0.6)])
def test_budget_rejects_noncanonical_inputs(base_calls, fraction):
    with pytest.raises(ValueError):
        review_budget(base_calls, fraction)
    assert review_budget(5, "0.6") == 3


def test_review_session_requires_pristine_ledger_and_three_slots():
    ledger = ExecutionLedger(TableSource({(0, 1): [True] * 3}), (0, 1, 2))
    ledger.observe(0, 1)
    with pytest.raises(ValueError):
        ReviewSession(2, 1, ledger, "0.4")
    assert ledger.cache_hits == 0
    baseline_only = ExecutionLedger(TableSource({(0, 1): [True]}), (0,))
    with pytest.raises(ValueError):
        ReviewSession(2, 1, baseline_only, "0.4")
    assert baseline_only.base_calls == 0


@pytest.mark.parametrize("action", ["start_review", "observe"])
def test_vote3_ledger_cannot_reuse_review_slots_or_mix_modes(action):
    ledger = ExecutionLedger(TableSource({(0, 1): [False, True, False], (0, 2): [True] * 3}), (0, 1, 2))
    assert ledger.observe_vote3(0, 1) is False
    with pytest.raises(ValueError):
        ledger.start_review(2) if action == "start_review" else ledger.observe(0, 2)
    assert ledger.base_calls == 3 and ledger.extra_calls == 0


def test_single_response_mode_cannot_switch_to_vote3_on_a_new_pair():
    ledger = ExecutionLedger(TableSource({(0, 1): [True] * 3, (0, 2): [True] * 3}), (0, 1, 2))
    ledger.observe(0, 1)
    with pytest.raises(ValueError):
        ledger.observe_vote3(0, 2)
    assert ledger.base_calls == 1


@pytest.mark.parametrize("method", ["Base", "Vote3", "Random", "SimpleTopology", "TraceSensitivity"])
def test_base_failure_returns_a_row_with_spent_calls_and_no_fabricated_output(method):
    result = run_algorithm(TableSource({}), 2, 1, (0, 1, 2), method, "0.4")
    assert result["status"] == "failed" and result["topk"] is None
    assert result["logical_calls"] == 1
    assert result["ledger"][-1]["status"] == "unavailable"
    assert result["failure_pair"] == [0, 1]


def test_partial_vote3_failure_keeps_successful_and_failed_attempts():
    result = run_algorithm(TableSource({(0, 1): [True]}), 2, 1, (0, 1, 2), "Vote3", "0.4")
    assert result["status"] == "failed" and result["topk"] is None
    assert result["logical_calls"] == 2
    assert [e["status"] for e in result["ledger"]] == ["ok", "unavailable"]


def two_runs():
    source = TableSource({(0, 1): [True, True, True, False, False, False]})
    return [run_algorithm(source, 2, 1, group, "Base", "0.4") for group in ((0, 1, 2), (3, 4, 5))]


def test_evaluation_rejects_cross_query_and_cross_source_pooling():
    a, b = two_runs()
    a["query_id"], b["query_id"] = "q1", "q2"
    with pytest.raises(ValueError):
        summarize_disagreement([a, b])
    a, _ = two_runs()
    other = run_algorithm(TableSource({(0, 1): [True] * 6}), 2, 1, (3, 4, 5), "Base", "0.4")
    with pytest.raises(ValueError):
        summarize_disagreement([a, other])


@pytest.mark.parametrize("bad_topk", [[], [0, 0], [2], [False], None])
def test_invalid_completed_outputs_are_not_treated_as_stable(bad_topk):
    a, b = two_runs()
    b["topk"] = bad_topk
    with pytest.raises(ValueError):
        summarize_disagreement([a, b])


def test_failed_run_is_retained_and_blocks_a_partial_metric():
    a, b = two_runs()
    b.update(status="failed", topk=None, reason="missing initial response")
    summary = summarize_disagreement([a, b])
    assert summary["executions"] == 2 and summary["failed_executions"] == 1
    assert summary["set_d_em"] is None


def test_aborted_valid_fallback_stays_in_disagreement_denominator():
    a, b = two_runs()
    b["status"] = "aborted"
    summary = summarize_disagreement([a, b])
    assert summary["aborted_executions"] == 1
    assert summary["dependent_run_pairs"] == 1 and summary["set_d_em"] == 1


@pytest.mark.parametrize("slot", [-1, True, 0.5])
def test_response_source_rejects_invalid_slots(slot):
    with pytest.raises(ValueError):
        TableSource({(0, 1): [True, False]}).read((0, 1), slot)


@pytest.mark.parametrize("pair", [(False, 1), (0.0, 1), (-1, 1), (1, 0)])
def test_ledger_rejects_invalid_pairs_before_call_accounting(pair):
    ledger = ExecutionLedger(TableSource({(0, 1): [True] * 3}), (0, 1, 2))
    with pytest.raises(ValueError):
        ledger.observe(*pair)
    assert ledger.base_calls == 0


def test_kendall_rejects_duplicate_in_second_ranking():
    with pytest.raises(ValueError):
        kendall_tau_b(["a", "b"], ["b", "a", "b"])


def test_nonboolean_verdict_is_invalid_in_operator_metrics_and_naive_replay():
    rows = [{"pair_id": "p", "repetition_id": 0, "strict_parser_status": "valid", "strict_parsed_decision": False},
            {"pair_id": "p", "repetition_id": 1, "strict_parser_status": "valid", "strict_parsed_decision": "False"}]
    metrics = operator_level_metrics(rows)
    assert metrics["ever_flipped_pair_count"] == 0 and metrics["invalid_response_count"] == 1
    with pytest.raises(ValueError):
        naive_replay(["False"], 2)


def test_duplicate_operator_record_cannot_silently_overwrite_a_response():
    row = {"pair_id": "p", "repetition_id": 0, "strict_parser_status": "valid", "strict_parsed_decision": True}
    with pytest.raises(ValueError):
        operator_level_metrics([row, deepcopy(row)])


@pytest.mark.parametrize("topk", [["a", "a"], ["a"], ["b", "a"]])
def test_query_metrics_reject_malformed_or_mismatched_topk(topk):
    with pytest.raises(ValueError):
        query_level_metrics([topk], [["a", "b", "c"]], k=2)


def test_conditional_analysis_rejects_stale_decisions_and_nonboolean_values():
    result = tournament(2, 1, lambda i, j: True)
    for observed in ({(0, 1): False}, {(0, 1): "True"}):
        with pytest.raises(ValueError):
            conditional_effect(result, observed, (0, 1))


def test_aborted_review_exports_pending_pair_and_last_complete_output():
    source = TableSource({(0, 1): [True] * 3, (2, 3): [True, False, False], (0, 2): [True] * 3})
    result = run_algorithm(source, 4, 1, (0, 1, 2), "Random", "1", seed=5)
    assert result["selected_pairs"][0] == (2, 3)
    assert result["status"] == "aborted" and result["topk"] == [0]
    assert result["pending_pair"] == [2, 3] and result["failure_pair"] == [0, 3]
    assert result["logical_calls"] == 6


def test_vote3_requires_three_reserved_slots_before_first_read():
    ledger = ExecutionLedger(TableSource({(0, 1): [True]}), (0,))
    with pytest.raises(ValueError):
        ledger.observe_vote3(0, 1)
    assert ledger.base_calls == 0 and ledger.pristine
