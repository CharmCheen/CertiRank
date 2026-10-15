"""Synthetic correctness tests for the naive-sort replay core.

These tests are the audit engine's own gate: the replay/classification semantics are
checked against hand-constructed verdict matrices with known answers, independent of
the LOTUS data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certirank.operators.naive_sort import (  # noqa: E402
    EFFECT_BELOW_TOPK,
    EFFECT_NONE,
    EFFECT_TOPK_ORDER,
    EFFECT_TOPK_SET,
    classify_flip,
    flip_one,
    multi_point_propagation,
    naive_replay,
    single_point_counterfactuals,
)


# ---------------------------------------------------------------------------
# naive_replay
# ---------------------------------------------------------------------------

def test_replay_rejects_wrong_vector_length():
    with pytest.raises(ValueError):
        naive_replay([True, False], 4)


def test_replay_win_counts_and_order():
    # 4 docs, all True: doc0 beats everyone -> wins (3,2,1,0), ranking [0,1,2,3]
    replay = naive_replay([True] * 6, 4)
    assert replay["win_counts"] == [3, 2, 1, 0]
    assert replay["indexes"] == [0, 1, 2, 3]


def test_replay_ties_broken_by_row_order():
    # p3=(1,2) flipped: wins (3,1,2,0) -> no ties. Instead make a genuine tie:
    # flip p0=(0,1): wins (2,3,1,0) -> [1,0,2,3]. For a tie, flip p1=(0,2):
    # wins (2,2,2,0) -> docs 0,1,2 tie at 2 and MUST come out in row order [0,1,2].
    replay = naive_replay([True, False, True, True, True, True], 4)
    assert replay["win_counts"] == [2, 2, 2, 0]
    assert replay["indexes"][:3] == [0, 1, 2]


# ---------------------------------------------------------------------------
# single-point classification: all four classes from one base (4 docs, K=2)
# ---------------------------------------------------------------------------

BASE_4 = [True] * 6  # pairs: p0=(0,1) p1=(0,2) p2=(0,3) p3=(1,2) p4=(1,3) p5=(2,3)


@pytest.mark.parametrize(
    "position,expected_class",
    [
        (1, EFFECT_NONE),  # (0,2): win counts (2,2,2,0) tie back to row order = base ranking
        (5, EFFECT_BELOW_TOPK),  # (2,3): ranking [0,1,3,2], Top-2 [0,1] untouched
        (0, EFFECT_TOPK_ORDER),  # (0,1): Top-2 [1,0] — same members, new order
        (3, EFFECT_TOPK_SET),  # (1,2): Top-2 [0,2] — membership changed
    ],
)
def test_single_flip_four_classes(position, expected_class):
    effects = single_point_counterfactuals(base_verdicts=BASE_4, n_docs=4, k=2)
    by_pos = {e["pair_position"]: e for e in effects}
    assert by_pos[position]["effect_class"] == expected_class


def test_classify_flip_prefers_order_over_set():
    # same members but reordered -> topk_order, not topk_set
    assert classify_flip([1, 2, 3], [2, 1, 3], [1, 2, 3], [2, 1, 3]) == EFFECT_TOPK_ORDER
    assert classify_flip([1, 2, 3], [1, 2, 4], [1, 2, 3], [1, 2, 4]) == EFFECT_TOPK_SET


def test_flip_one_inverts_only_target():
    flipped = flip_one([True, True, False], 1)
    assert flipped == [True, False, False]


# ---------------------------------------------------------------------------
# multi-point propagation: the 8-doc all-True construction with known answers
# (carried over from the LOTUS Gate 2 test suite: votes [7,6,5,4,3,2,1,0])
# ---------------------------------------------------------------------------

N = 8
K = 3
BASE_8 = [True] * (N * (N - 1) // 2)


def _vector_with_flips(positions):
    vector = list(BASE_8)
    for position in positions:
        vector[position] = not vector[position]
    return vector


def _positions_for_doc_pairs(doc_pairs):
    position_of = {}
    idx = 0
    for i in range(N):
        for j in range(i + 1, N):
            position_of[(i, j)] = idx
            idx += 1
    return [position_of[pair] for pair in doc_pairs]


def test_cancelling_pair_returns_to_base_answer():
    # (1,2)+(2,3): each flip alone changes the ordered Top-3, together they cancel.
    positions = _positions_for_doc_pairs([(1, 2), (2, 3)])
    report = multi_point_propagation(
        verdict_vectors=[BASE_8, _vector_with_flips(positions)], n_docs=N, k=K
    )
    pair_report = report["run_pair_reports"][0]
    assert pair_report["single_flip_answer_impact_count"] == 2
    assert pair_report["cancelling"] is True
    assert pair_report["joint_emergent"] is False


def test_joint_emergent_pair_moves_answer_only_together():
    # (0,2)+(0,3): each flip alone is inert from the base, together they produce
    # [d1,d2,d0]. The first vector is the single-flip base, as in the real analysis.
    positions = _positions_for_doc_pairs([(0, 2), (0, 3)])
    report = multi_point_propagation(
        verdict_vectors=[BASE_8, _vector_with_flips(positions)], n_docs=N, k=K
    )
    pair_report = report["run_pair_reports"][0]
    assert pair_report["single_flip_answer_impact_count"] == 0
    assert pair_report["joint_emergent"] is True
    assert pair_report["combined_answer_changed"] is True


def test_identical_vectors_have_no_differences():
    report = multi_point_propagation(verdict_vectors=[BASE_8, BASE_8], n_docs=N, k=K)
    pair_report = report["run_pair_reports"][0]
    assert pair_report["differing_pair_count"] == 0
    assert pair_report["topk_order_equal"] is True
    assert report["distinct_topk_ordered"] == 1
