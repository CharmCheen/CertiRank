"""Independent reference, exhaustive nontransitive inputs, and budget regressions."""

from itertools import combinations, product
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certirank.diagnostics import conditional_effect
from certirank.evaluation import run_algorithm, summarize_disagreement
from certirank.operators.tournament import tournament
from certirank.oracle import ExecutionLedger, TableSource, validate_slot_groups
from certirank.policy import PolicyView, select_pair
from certirank.repair import ReviewSession, review_budget


def reference(n, k, matrix):
    """Rebuild every recursive bracket from leaves after every extraction."""
    size = 1 << (n - 1).bit_length()
    removed = set()

    def winner(lo, hi):
        if hi - lo == 1:
            return lo if lo < n and lo not in removed else None
        mid = (lo + hi) // 2
        a, b = winner(lo, mid), winner(mid, hi)
        if a is None or b is None:
            return b if a is None else a
        i, j = sorted((a, b))
        return i if matrix[i, j] else j

    output = []
    for _ in range(k):
        value = winner(0, size)
        output.append(value)
        removed.add(value)
    return tuple(output)


@pytest.mark.parametrize("k", [1, 2, 3, 4])
def test_all_64_four_candidate_tables_and_every_update(k):
    pairs = list(combinations(range(4), 2))
    for bits in product((False, True), repeat=6):
        matrix = dict(zip(pairs, bits))
        base = tournament(4, k, lambda i, j: matrix[i, j])
        assert base.topk == reference(4, k, matrix)
        for pair in pairs:
            changed = dict(matrix)
            changed[pair] = not changed[pair]
            inc = tournament(4, k, lambda i, j: changed[i, j], previous=base, changed_pairs=(pair,))
            full = tournament(4, k, lambda i, j: changed[i, j])
            assert inc.topk == full.topk == reference(4, k, changed)
            assert [(e.stage, e.node, e.pair, e.winner) for e in inc.events] == [
                (e.stage, e.node, e.pair, e.winner) for e in full.events]
        assert base.topk == reference(4, k, matrix)  # update never mutates old state


@pytest.mark.parametrize("n", [1, 3, 6, 8])
def test_random_tables_multi_updates_and_chained_repair(n):
    rng = random.Random(20261015 + n)
    pairs = list(combinations(range(n), 2))
    for _ in range(60):
        matrix = {p: bool(rng.getrandbits(1)) for p in pairs}
        k = min(n, 3)
        result = tournament(n, k, lambda i, j: matrix[i, j])
        for _ in range(4):
            changes = rng.sample(pairs, min(3, len(pairs)))
            for pair in changes:
                matrix[pair] = not matrix[pair]
            result = tournament(n, k, lambda i, j: matrix[i, j], previous=result, changed_pairs=changes)
            assert result.topk == reference(n, k, matrix)


def test_four_element_unknown_counterexample():
    observed = {(0, 1): True, (2, 3): True, (0, 2): True}
    base = tournament(4, 1, lambda i, j: observed[i, j])
    assert base.topk == (0,)
    assert conditional_effect(base, observed, (2, 3))["status"] == "unresolved"
    for a_beats_d, expected in [(True, (0,)), (False, (3,))]:
        completed = {**observed, (2, 3): False, (0, 3): a_beats_d}
        assert tournament(4, 1, lambda i, j: completed[i, j]).topk == expected


def test_strict_order_and_nontransitive_cycle():
    assert tournament(8, 3, lambda i, j: True).topk == (0, 1, 2)
    cycle = {(0, 1): True, (1, 2): True, (0, 2): False}
    assert tournament(3, 3, lambda i, j: cycle[i, j]).topk == reference(3, 3, cycle)


def source_with_new_branch():
    # Initial winner A; reviewing C>D makes D advance and asks the unseen A:D.
    return TableSource({(0, 1): [True] * 3, (2, 3): [True, False, False],
                        (0, 2): [True] * 3, (0, 3): [False] * 3})


def test_new_branch_in_global_table_is_charged_and_abort_preserves_output():
    ledger = ExecutionLedger(source_with_new_branch(), (0, 1, 2))
    session = ReviewSession(4, 1, ledger, "0.8")  # L0=3, B=2
    before = session.result
    outcome = session.review((2, 3))
    assert outcome.status == "aborted"
    assert outcome.result is before and outcome.result.topk == (0,)
    assert ledger.extra_calls == 2  # no free access to the missing third call
    assert all(e.pair != (0, 3) for e in ledger.events)
    with pytest.raises(ValueError):
        session.review((0, 1))


@pytest.mark.parametrize("mode", ["incremental", "cached_replay"])
def test_sufficient_budget_completes_new_branch_and_records_it(mode):
    ledger = ExecutionLedger(source_with_new_branch(), (0, 1, 2))
    session = ReviewSession(4, 1, ledger, "1", mode=mode)
    outcome = session.review((2, 3))
    assert outcome.status == "complete" and outcome.result.topk == (3,)
    assert ledger.extra_calls == 3
    assert [(e.pair, e.phase) for e in ledger.events if e.extra][-1] == ((0, 3), "repair")


def test_unavailable_branch_records_failure_and_retains_last_output():
    source = TableSource({(0, 1): [True] * 3, (2, 3): [True, False, False], (0, 2): [True] * 3})
    ledger = ExecutionLedger(source, (0, 1, 2))
    session = ReviewSession(4, 1, ledger, "1")
    assert session.review((2, 3)).status == "aborted"
    assert session.result.topk == (0,)
    assert ledger.extra_calls == 3 and ledger.events[-1].status == "unavailable"


def test_two_call_review_preflight_and_integer_budget():
    assert review_budget(7, "0.4") == 2
    assert review_budget(7, "0.2") == 1
    ledger = ExecutionLedger(source_with_new_branch(), (0, 1, 2))
    session = ReviewSession(4, 1, ledger, "0.4")
    assert session.review((2, 3)).status == "aborted"
    assert ledger.extra_calls == 0


def test_overlapping_response_groups_are_rejected():
    assert validate_slot_groups(((0, 1, 2), (3, 4, 5))) == ((0, 1, 2), (3, 4, 5))
    with pytest.raises(ValueError):
        validate_slot_groups(((0, 1, 2), (2, 3, 4)))


def test_different_executions_do_not_share_cached_answers():
    source = TableSource({(0, 1): [True, True, True, False, False, False]})
    a, b = (ExecutionLedger(source, group) for group in validate_slot_groups(((0, 1, 2), (3, 4, 5))))
    assert tournament(2, 1, a.observe).topk == (0,)
    assert tournament(2, 1, b.observe).topk == (1,)
    assert a.base_calls == b.base_calls == 1
    assert a.observe(0, 1) is True and a.base_calls == 1 and a.cache_hits == 1


@pytest.mark.parametrize("method", ["Random", "SimpleTopology", "TraceSensitivity"])
def test_policies_only_receive_visible_state(method):
    ledger = ExecutionLedger(source_with_new_branch(), (0, 1, 2))
    session = ReviewSession(4, 1, ledger, "1")
    view = PolicyView(ledger.visible, ledger.reviewed, session.result, ledger.remaining)
    assert not hasattr(view, "source")
    pair = select_pair(view, method)
    assert pair in dict(ledger.visible)
    assert select_pair(view, method) == pair


def test_incremental_and_cached_replay_use_identical_responses():
    rng = random.Random(31)
    pairs = list(combinations(range(8), 2))
    for _ in range(100):
        responses = {p: [bool(rng.getrandbits(1)) for _ in range(3)] for p in pairs}
        sessions = [ReviewSession(8, 3, ExecutionLedger(TableSource(responses), (0, 1, 2)), "2", mode=mode)
                    for mode in ("incremental", "cached_replay")]
        for _ in range(5):
            a, b = sessions
            pair = next((p for p, _ in a.ledger.visible if p not in a.ledger.reviewed), None)
            if pair is None:
                break
            oa, ob = a.review(pair), b.review(pair)
            assert (oa.status, oa.result.topk) == (ob.status, ob.result.topk)
            assert a.ledger.events == b.ledger.events
            if oa.status == "aborted":
                break


@pytest.mark.parametrize("method", ["Random", "SimpleTopology", "TraceSensitivity"])
def test_complete_policies_match_across_repair_modes(method):
    rng = random.Random(314)
    for _ in range(40):
        source = TableSource({p: [bool(rng.getrandbits(1)) for _ in range(3)] for p in combinations(range(8), 2)})
        a, b = [run_algorithm(source, 8, 3, (0, 1, 2), method, "0.4", mode=mode)
                for mode in ("incremental", "cached_replay")]
        for key in ("topk", "status", "logical_calls", "extra_calls", "selected_pairs", "ledger"):
            assert a[key] == b[key], key


def test_vote3_cost_follows_its_actual_majority_path():
    result = run_algorithm(source_with_new_branch(), 4, 1, (0, 1, 2), "Vote3", "0.4")
    assert result["topk"] == [3]
    assert result["logical_calls"] == 9
    assert {tuple(e["pair"]) for e in result["ledger"]} == {(0, 1), (2, 3), (0, 3)}


def test_single_group_does_not_emit_fake_independent_disagreement():
    run = run_algorithm(source_with_new_branch(), 4, 1, (0, 1, 2), "Base", "0.4")
    assert summarize_disagreement([run])["set_d_em"] is None
    with pytest.raises(ValueError):
        summarize_disagreement([run, run])


@pytest.mark.parametrize("n,k", [(5, 2), (7, 4), (16, 8), (31, 3), (64, 16)])
def test_larger_and_non_power_of_two_multi_update_repairs(n, k):
    rng = random.Random(20261015 + n * 100 + k)
    pairs = list(combinations(range(n), 2))
    for _ in range(25):
        matrix = {pair: bool(rng.getrandbits(1)) for pair in pairs}
        current = tournament(n, k, lambda i, j: matrix[i, j])
        for _ in range(6):
            changes = rng.sample(pairs, min(6, len(pairs)))
            for pair in changes:
                matrix[pair] = not matrix[pair]
            current = tournament(n, k, lambda i, j: matrix[i, j], previous=current, changed_pairs=changes)
            assert current.topk == reference(n, k, matrix)
            assert len(set(current.topk)) == k


@pytest.mark.parametrize("k", [1, 2, 3])
def test_conditional_verdict_holds_for_all_unobserved_completions(k):
    pairs = list(combinations(range(4), 2))
    for bits in product((False, True), repeat=6):
        matrix = dict(zip(pairs, bits))
        base = tournament(4, k, lambda i, j: matrix[i, j])
        observed = {event.pair: event.winner == event.pair[0] for event in base.events}
        missing = sorted(set(pairs) - set(observed))
        for pair in observed:
            effect = conditional_effect(base, observed, pair)
            if effect["status"] == "unresolved":
                continue
            for completion in product((False, True), repeat=len(missing)):
                completed = {**observed, **dict(zip(missing, completion)), pair: not observed[pair]}
                result = tournament(4, k, lambda i, j: completed[i, j])
                assert (set(result.topk) != set(base.topk)) == (effect["status"] == "conditional_impact")
