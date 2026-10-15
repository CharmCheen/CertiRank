"""Naive comparison-sort replay and flip-impact classification.

CertiRank's own implementation of the aggregation semantics the LOTUS ``sem_topk``
``naive`` method uses, written independently from the raw rule so the Gate 2 audit is
a genuine recomputation rather than a re-run of the producer's code:

1. pairs are enumerated ``(i, j)`` with ``i < j`` over the candidate list in row order;
2. ``verdicts[idx] is True`` means the document at position ``i`` wins (one vote to
   ``i``), otherwise the vote goes to ``j``;
3. the full ranking is ``sorted(range(N), key=win_counts, reverse=True)`` — Python's
   stable sort, so win-count ties are broken by original row index, ascending;
4. the Top-K is the first ``K`` entries of that ranking.

Flip-impact classes (the answer is the *ordered* Top-K; set-equality alone would
misclassify a within-Top-K reorder as "cancelled"):

* ``none``       — the full ranking did not move at all;
* ``below_topk`` — the tail moved, Top-K set and order did not;
* ``topk_order`` — same Top-K members, different order inside the Top-K;
* ``topk_set``   — Top-K membership changed.

Counterfactual discipline (carried over from the Gate 2 protocol): a single-point flip
is a deterministic relation-level analysis on one fixed verdict matrix ("if this one
vote had come back the other way, all else fixed"), never a provider probability.
Multi-point propagation only ever compares *real* observed verdict vectors.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Sequence

__all__ = [
    "EFFECT_NONE",
    "EFFECT_BELOW_TOPK",
    "EFFECT_TOPK_ORDER",
    "EFFECT_TOPK_SET",
    "EFFECT_ORDER",
    "naive_replay",
    "flip_one",
    "classify_flip",
    "single_point_counterfactuals",
    "multi_point_propagation",
]

EFFECT_NONE = "none"
EFFECT_BELOW_TOPK = "below_topk"
EFFECT_TOPK_ORDER = "topk_order"
EFFECT_TOPK_SET = "topk_set"
EFFECT_ORDER = (EFFECT_NONE, EFFECT_BELOW_TOPK, EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)


def naive_replay(verdicts: Sequence[bool], n_docs: int) -> dict[str, Any]:
    """Rebuild the naive sort from a verdict vector with LOTUS's exact semantics."""
    if type(n_docs) is not int or n_docs < 1 or any(type(v) is not bool for v in verdicts):
        raise ValueError("require a positive integer document count and boolean verdicts")
    expected = n_docs * (n_docs - 1) // 2
    if len(verdicts) != expected:
        raise ValueError(f"verdict vector has {len(verdicts)} entries, expected {expected}")
    win_counts = [0] * n_docs
    idx = 0
    for i in range(n_docs):
        for j in range(i + 1, n_docs):
            if verdicts[idx]:
                win_counts[i] += 1
            else:
                win_counts[j] += 1
            idx += 1
    # Stable sort: win-count ties fall back to candidate row order (ascending index).
    indexes = sorted(range(n_docs), key=lambda i: win_counts[i], reverse=True)
    return {"indexes": indexes, "win_counts": win_counts}


def flip_one(verdicts: Sequence[bool], position: int) -> list[bool]:
    """One counterfactual verdict vector: the single verdict at ``position`` inverted."""
    if type(position) is not int or not 0 <= position < len(verdicts):
        raise ValueError(f"position {position} out of range for {len(verdicts)} verdicts")
    flipped = list(verdicts)
    flipped[position] = not flipped[position]
    return flipped


def _pair_indices(position: int, n_docs: int) -> tuple[int, int]:
    """Invert the naive enumeration: pair position -> ``(i, j)``."""
    idx = 0
    for i in range(n_docs):
        for j in range(i + 1, n_docs):
            if idx == position:
                return i, j
            idx += 1
    raise ValueError(f"position {position} out of range for {n_docs} documents")


def classify_flip(
    base_topk: Sequence[Any],
    new_topk: Sequence[Any],
    base_ranking: Sequence[Any],
    new_ranking: Sequence[Any],
) -> str:
    """Classify one flip by how far up the (ordered) answer its effect reaches."""
    if list(base_topk) != list(new_topk):
        return EFFECT_TOPK_ORDER if set(base_topk) == set(new_topk) else EFFECT_TOPK_SET
    if list(base_ranking) != list(new_ranking):
        return EFFECT_BELOW_TOPK
    return EFFECT_NONE


def single_point_counterfactuals(
    *,
    base_verdicts: Sequence[bool],
    n_docs: int,
    k: int,
    flipped_positions: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """Flip each verdict in turn (everything else fixed); classify the impact.

    Returns one record per flipped position: ``pair_position``, the ``(i, j)`` document
    positions, ``effect_class``, ``full_ranking_changed``, ``topk_set_changed``,
    ``topk_order_changed``, ``winner_before`` (``"doc1"``/``"doc2"``) and the vote delta.
    """
    if type(k) is not int or not 1 <= k <= n_docs:
        raise ValueError("require integer 1 <= k <= n_docs")
    base = naive_replay(base_verdicts, n_docs)
    base_ranking = base["indexes"]
    base_topk = base_ranking[:k]

    positions = list(flipped_positions) if flipped_positions is not None else range(len(base_verdicts))
    effects: list[dict[str, Any]] = []
    for position in positions:
        cf = naive_replay(flip_one(base_verdicts, position), n_docs)
        cf_ranking = cf["indexes"]
        cf_topk = cf_ranking[:k]
        i, j = _pair_indices(position, n_docs)
        effects.append(
            {
                "pair_position": position,
                "position_i": i,
                "position_j": j,
                "effect_class": classify_flip(base_topk, cf_topk, base_ranking, cf_ranking),
                "full_ranking_changed": base_ranking != cf_ranking,
                "topk_set_changed": set(base_topk) != set(cf_topk),
                "topk_order_changed": list(base_topk) != list(cf_topk),
                "winner_before": "doc1" if base_verdicts[position] else "doc2",
                "win_count_delta": {
                    i: cf["win_counts"][i] - base["win_counts"][i],
                    j: cf["win_counts"][j] - base["win_counts"][j],
                },
            }
        )
    return effects


def multi_point_propagation(
    *,
    verdict_vectors: Sequence[Sequence[bool]],
    n_docs: int,
    k: int,
) -> dict[str, Any]:
    """Propagation across *real* observed verdict vectors — no synthetic combinations.

    For every unordered pair of runs: which positions differ, did the ordered Top-K
    differ, and is the observed difference joint-emergent (no single flip in the
    difference set touches the answer, yet the combination does) or cancelling
    (individually material flips whose combination leaves the ordered Top-K unchanged)?
    """
    if type(k) is not int or not 1 <= k <= n_docs:
        raise ValueError("require integer 1 <= k <= n_docs")
    runs = [naive_replay(v, n_docs) for v in verdict_vectors]
    topks = [r["indexes"][:k] for r in runs]

    reports: list[dict[str, Any]] = []
    joint_emergent = cancelling = 0
    for a, b in combinations(range(len(verdict_vectors)), 2):
        diff_positions = [p for p, (va, vb) in enumerate(zip(verdict_vectors[a], verdict_vectors[b])) if va != vb]
        report: dict[str, Any] = {
            "run_a": a,
            "run_b": b,
            "differing_pair_positions": diff_positions,
            "differing_pair_count": len(diff_positions),
            "topk_set_equal": set(topks[a]) == set(topks[b]),
            "topk_order_equal": list(topks[a]) == list(topks[b]),
            "single_flip_answer_impact_count": 0,
            "combined_answer_changed": False,
            "joint_emergent": False,
            "cancelling": False,
        }
        if diff_positions:
            singles = single_point_counterfactuals(
                base_verdicts=verdict_vectors[a], n_docs=n_docs, k=k, flipped_positions=diff_positions
            )
            answer_impact = [e for e in singles if e["effect_class"] in (EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)]
            combined = list(verdict_vectors[a])
            for p in diff_positions:
                combined[p] = not combined[p]
            combined_topk = naive_replay(combined, n_docs)["indexes"][:k]
            combined_answer_changed = list(topks[a]) != list(combined_topk)
            report["single_flip_answer_impact_count"] = len(answer_impact)
            report["combined_answer_changed"] = combined_answer_changed
            report["joint_emergent"] = not answer_impact and combined_answer_changed
            report["cancelling"] = bool(answer_impact) and not combined_answer_changed
            joint_emergent += int(report["joint_emergent"])
            cancelling += int(report["cancelling"])
        reports.append(report)

    return {
        "run_count": len(verdict_vectors),
        "run_pair_reports": reports,
        "joint_emergent_run_pairs": joint_emergent,
        "cancelling_run_pairs": cancelling,
        "distinct_topk_ordered": len({tuple(t) for t in topks}),
        "distinct_topk_sets": len({frozenset(t) for t in topks}),
    }
