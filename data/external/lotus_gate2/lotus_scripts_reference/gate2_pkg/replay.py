"""Offline reconstruction of the naive sort, and the Gate 2B propagation analysis.

The replay is a re-implementation of exactly what ``lotus/sem_ops/sem_topk.py::llm_naive_sort``
does once the 28 booleans exist:

1. pairs are enumerated ``(i, j)`` with ``i < j`` over the candidate list in row order;
2. ``comparisons[idx] == True`` means *document i wins* (vote to ``i``), else vote to ``j``;
3. the final order is ``sorted(range(N), key=votes, reverse=True)`` — Python's stable
   sort, so **ties are broken by original row index, ascending**;
4. the Top-K is the first ``K`` entries; ``SemTopKDataframe.__call__`` reindexes the
   DataFrame by that order and takes ``head(K)``.

The correctness gate (:func:`replay_gate`) compares the replayed Top-K against the
Top-K LOTUS itself returned in the live run. They must agree for every run before any
propagation analysis is allowed to draw conclusions.

Counterfactual scope discipline: flipping verdicts here is a *deterministic relation-level*
analysis — it answers "if this judgment had come back the other way, what would the answer
have been *given all other judgments fixed*". It is not a probability statement about the
provider. Multi-point propagation only ever uses *real* verdict vectors observed across
repetitions; no synthetic combination is constructed, so nothing here masquerades as a
sample from the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Mapping, Sequence

__all__ = [
    "EFFECT_NONE",
    "EFFECT_BELOW_TOPK",
    "EFFECT_TOPK_ORDER",
    "EFFECT_TOPK_SET",
    "EFFECT_ORDER",
    "naive_replay",
    "replay_gate",
    "flip_one",
    "single_point_counterfactuals",
    "FlipEffect",
    "multi_point_propagation",
    "impact_concentration",
]


EFFECT_NONE = "none"  #: nothing changed at all: same full ranking, same Top-K, same order
EFFECT_BELOW_TOPK = "below_topk"  #: the tail moved, but the Top-K set and order did not
EFFECT_TOPK_ORDER = "topk_order"  #: same Top-K members, different order within the Top-K
EFFECT_TOPK_SET = "topk_set"  #: Top-K membership changes

#: every class in classification order
EFFECT_ORDER = (EFFECT_NONE, EFFECT_BELOW_TOPK, EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)


def naive_replay(verdicts: Sequence[bool], n_docs: int) -> dict[str, Any]:
    """Rebuild the naive sort from its verdict vector, with LOTUS's exact semantics."""
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
    indexes = sorted(range(n_docs), key=lambda i: win_counts[i], reverse=True)
    return {"indexes": indexes, "win_counts": win_counts}


def replay_gate(replay_indexes: Sequence[int], lotus_topk_positions: Sequence[int], k: int) -> dict[str, Any]:
    """Compare the replay against what LOTUS actually returned.

    ``lotus_topk_positions`` are row positions in the candidate list, in the order the
    returned DataFrame listed them.
    """
    replay_topk = list(replay_indexes)[:k]
    consistent = replay_topk == list(lotus_topk_positions)
    return {
        "replay_topk_positions": replay_topk,
        "lotus_topk_positions": list(lotus_topk_positions),
        "consistent": consistent,
    }


def flip_one(verdicts: Sequence[bool], position: int) -> list[bool]:
    """One counterfactual verdict vector: the single verdict at ``position`` inverted."""
    flipped = list(verdicts)
    flipped[position] = not flipped[position]
    return flipped


def _classify(base_topk: Sequence[Any], new_topk: Sequence[Any], base_ranking: Sequence[Any], new_ranking: Sequence[Any]) -> str:
    """Classify one flip by how far up the answer its effect reaches.

    Four classes, deliberately kept distinct — collapsing the last two into one hid real
    structure during the smoke run:

    * ``topk_set`` — one or more documents enter/leave the Top-K;
    * ``topk_order`` — same members, new order inside the Top-K;
    * ``below_topk`` — the full ranking moved, but only *outside* the Top-K: the
      query answer the user sees is byte-identical, so this is the class that answers
      "how much of the operator's instability is invisible downstream?";
    * ``none`` — the ranking did not move at all.
    """
    same_topk_list = list(base_topk) == list(new_topk)
    if not same_topk_list:
        return EFFECT_TOPK_ORDER if set(base_topk) == set(new_topk) else EFFECT_TOPK_SET
    if list(base_ranking) != list(new_ranking):
        return EFFECT_BELOW_TOPK
    return EFFECT_NONE


@dataclass
class FlipEffect:
    """What a single counterfactual verdict flip does to one query's answer."""

    pair_position: int
    pair_id: str | None = None
    document_1_id: str | None = None
    document_2_id: str | None = None
    effect_class: str = EFFECT_NONE
    full_ranking_changed: bool = False
    topk_set_changed: bool = False
    topk_order_changed: bool = False
    winner_before: str | None = None  #: "doc1" | "doc2" — who the base verdict favoured
    win_count_delta: dict[str, int] = field(default_factory=dict)
    rank_shift: dict[str, int] = field(default_factory=dict)  #: doc -> new_rank - old_rank

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


def single_point_counterfactuals(
    *,
    base_verdicts: Sequence[bool],
    doc_ids: Sequence[str],
    pair_ids: Sequence[str] | None = None,
    k: int,
    flipped_positions: Sequence[int] | None = None,
) -> list[FlipEffect]:
    """Flip each valid verdict in turn, everything else fixed; classify the impact.

    ``flipped_positions`` restricts the analysis (e.g. to pairs with valid strict
    verdicts); by default every pair position is flipped.
    """
    n = len(doc_ids)
    base = naive_replay(base_verdicts, n)
    base_ranking = [doc_ids[i] for i in base["indexes"]]
    base_topk = base_ranking[:k]

    positions = list(flipped_positions) if flipped_positions is not None else range(len(base_verdicts))
    effects: list[FlipEffect] = []
    for position in positions:
        if not 0 <= position < len(base_verdicts):
            raise ValueError(f"pair position {position} out of range")
        cf = naive_replay(flip_one(base_verdicts, position), n)
        cf_ranking = [doc_ids[i] for i in cf["indexes"]]
        cf_topk = cf_ranking[:k]

        winner_before = "doc1" if base_verdicts[position] else "doc2"
        delta: dict[str, int] = {}
        i, j = _pair_indices(position, n)
        # a flip moves exactly one vote from one document to the other
        delta[doc_ids[i]] = int(cf["win_counts"][i]) - int(base["win_counts"][i])
        delta[doc_ids[j]] = int(cf["win_counts"][j]) - int(base["win_counts"][j])

        rank_before = {doc_ids[pos]: rank for rank, pos in enumerate(base["indexes"])}
        rank_after = {doc_ids[pos]: rank for rank, pos in enumerate(cf["indexes"])}
        shift = {doc: rank_after[doc] - rank_before[doc] for doc in doc_ids if rank_after[doc] != rank_before[doc]}

        effect = _classify(base_topk, cf_topk, base_ranking, cf_ranking)
        effects.append(
            FlipEffect(
                pair_position=position,
                pair_id=pair_ids[position] if pair_ids is not None else None,
                document_1_id=doc_ids[i],
                document_2_id=doc_ids[j],
                effect_class=effect,
                full_ranking_changed=base_ranking != cf_ranking,
                topk_set_changed=set(base_topk) != set(cf_topk),
                topk_order_changed=list(base_topk) != list(cf_topk),
                winner_before=winner_before,
                win_count_delta=delta,
                rank_shift=shift,
            )
        )
    return effects


def _pair_indices(position: int, n: int) -> tuple[int, int]:
    """Invert the naive enumeration: pair position -> ``(i, j)``."""
    idx = 0
    for i in range(n):
        for j in range(i + 1, n):
            if idx == position:
                return i, j
            idx += 1
    raise ValueError(f"position {position} out of range for {n} documents")


def multi_point_propagation(
    *,
    verdict_vectors: Sequence[Sequence[bool]],
    doc_ids: Sequence[str],
    k: int,
    labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Propagation across *real* repetition verdict vectors — no synthetic combinations.

    For every unordered pair of runs: which pair positions differ, and does the answer
    (Top-K membership / order / full ranking) differ with them? Then, restricted to each
    run pair's *actually observed* difference set: joint-emergence (every single flip in
    the difference set is inert, but the observed combination moves the Top-K) and
    cancellation (individually material flips whose observed combination leaves the
    Top-K unchanged).
    """
    n = len(doc_ids)
    runs = [naive_replay(v, n) for v in verdict_vectors]
    rankings = [[doc_ids[i] for i in r["indexes"]] for r in runs]
    topks = [ranking[:k] for ranking in rankings]

    pair_reports: list[dict[str, Any]] = []
    for a, b in combinations(range(len(verdict_vectors)), 2):
        diff_positions = [p for p, (va, vb) in enumerate(zip(verdict_vectors[a], verdict_vectors[b])) if va != vb]
        report: dict[str, Any] = {
            "run_a": labels[a] if labels is not None else f"r{a}",
            "run_b": labels[b] if labels is not None else f"r{b}",
            "differing_pair_positions": diff_positions,
            "differing_pair_count": len(diff_positions),
            "topk_set_equal": set(topks[a]) == set(topks[b]),
            "topk_order_equal": list(topks[a]) == list(topks[b]),
            "full_ranking_equal": rankings[a] == rankings[b],
        }

        if diff_positions:
            base = list(verdict_vectors[a])
            # single-flip effects from run a's verdicts, restricted to the observed diff
            singles = single_point_counterfactuals(
                base_verdicts=base, doc_ids=doc_ids, k=k, flipped_positions=diff_positions
            )
            report["single_flip_effects"] = [e.effect_class for e in singles]
            # "material" must mean *the answer changed*. The answer is the ordered Top-K,
            # so below_topk does not qualify: the user cannot see it. An earlier version
            # used `!= EFFECT_NONE` here and also compared only Top-K *sets*, which made a
            # pure within-Top-K reorder look like cancellation.
            answer_impact = [
                e for e in singles if e.effect_class in (EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)
            ]
            report["single_flip_answer_impact_count"] = len(answer_impact)
            report["single_flip_material_count"] = len(answer_impact)

            combined = list(base)
            for p in diff_positions:
                combined[p] = not combined[p]
            combined_replay = naive_replay(combined, n)
            combined_topk = [doc_ids[i] for i in combined_replay["indexes"]][:k]
            report["combined_topk_set_equal"] = set(topks[a]) == set(combined_topk)
            # the observed combination moved the answer iff the ordered Top-K differs
            combined_answer_changed = list(topks[a]) != list(combined_topk)
            report["combined_answer_changed"] = combined_answer_changed
            # joint emergence: no single flip touches the answer, but together they do
            report["joint_emergent"] = not answer_impact and combined_answer_changed
            # cancellation: individually material flips whose combination is a no-op
            report["cancelling"] = bool(answer_impact) and not combined_answer_changed
        pair_reports.append(report)

    return {
        "run_count": len(verdict_vectors),
        "run_pair_reports": pair_reports,
        "distinct_topk_ordered": len({tuple(t) for t in topks}),
        "distinct_topk_sets": len({frozenset(t) for t in topks}),
    }


def impact_concentration(
    *,
    base_verdicts: Sequence[bool],
    verdict_vectors: Sequence[Sequence[bool]],
    doc_ids: Sequence[str],
    pair_ids: Sequence[str] | None,
    k: int,
    flipped_positions: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Where does the influence sit? Descriptive statistics only.

    * per-pair single-flip effect class (from one fixed complete run, the base);
    * ever-flipped pairs across the real repetitions;
    * the overlap between high-instability pairs and high-impact pairs;
    * unstable-but-inert pairs: verdicts that change, answers that do not.

    ``flipped_positions`` restricts the flips, exactly as in
    :func:`single_point_counterfactuals`. It must be passed, and it must be the *same*
    position set the single-point layer used: an earlier version defaulted to flipping all
    28 positions here while the single-point layer flipped only the 26 with valid strict
    verdicts, so the two halves of the same report disagreed about how many pairs were
    even eligible. The n shown in every share below is the restricted set.
    """
    n = len(doc_ids)
    effects = single_point_counterfactuals(
        base_verdicts=base_verdicts,
        doc_ids=doc_ids,
        pair_ids=pair_ids,
        k=k,
        flipped_positions=flipped_positions,
    )
    # ever-flipped is an *empirical* property of the repetitions, so it is computed over
    # every pair position — including ones excluded from the flip analysis. Shares below
    # are still taken over the flip-eligible set, so numerator and denominator match.
    ever_flipped: set[int] = set()
    distinct_verdicts: dict[int, set[bool]] = {}
    for position in range(len(base_verdicts)):
        seen = {bool(v[position]) for v in verdict_vectors}
        distinct_verdicts[position] = seen
        if len(seen) > 1:
            ever_flipped.add(position)

    counts = {cls: 0 for cls in EFFECT_ORDER}
    for effect in effects:
        counts[effect.effect_class] += 1

    eligible = {e.pair_position for e in effects}
    below_only = {e.pair_position for e in effects if e.effect_class == EFFECT_BELOW_TOPK}
    # "impact" is scored against the *answer* — the ordered Top-K. A pair that only moves
    # the tail below position K is invisible to whoever issued the query, so it is not an
    # impact pair; it is counted separately as below_only.
    high_impact = {
        e.pair_position for e in effects if e.effect_class in (EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)
    }
    topk_impact = {e.pair_position for e in effects if e.effect_class == EFFECT_TOPK_SET}

    return {
        "pairs_total": len(base_verdicts),
        "flip_eligible_pairs": len(effects),
        "flip_excluded_positions": sorted(set(range(len(base_verdicts))) - eligible),
        "single_flip_effect_counts": counts,
        "single_flip_topk_set_share": round(len(topk_impact) / len(effects), 6) if effects else 0.0,
        "single_flip_order_only_share": round(counts[EFFECT_TOPK_ORDER] / len(effects), 6) if effects else 0.0,
        "single_flip_below_topk_share": round(counts[EFFECT_BELOW_TOPK] / len(effects), 6) if effects else 0.0,
        "single_flip_inert_share": round(counts[EFFECT_NONE] / len(effects), 6) if effects else 0.0,
        "below_topk_pair_positions": sorted(below_only),
        "ever_flipped_pair_positions": sorted(ever_flipped),
        "ever_flipped_count": len(ever_flipped),
        "high_impact_pair_positions": sorted(high_impact),
        "overlap_ever_flipped_and_high_impact": sorted(ever_flipped & high_impact),
        "overlap_ever_flipped_and_topk_set": sorted(ever_flipped & topk_impact),
        "unstable_but_inert_pair_positions": sorted(ever_flipped - high_impact),
        "stable_but_material_pair_positions": sorted(high_impact - ever_flipped),
        "per_pair": [e.as_record() for e in effects],
    }
