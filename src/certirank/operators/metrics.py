"""Pairwise ranking metrics used by the audit (reimplemented, stdlib only).

Discipline carried over from the Gate 2 protocol:

* ever-flipped ratio (a *pair* property) and run-pair disagreement (a *run pair*
  property) are different quantities and are never presented as each other;
* run pairs share runs — they are dependent samples, so all run-pair statistics are
  descriptive; no inferential test is built on them;
* agreement/Jaccard/Kendall measure whether runs reproduce each other; nDCG@K measures
  agreement with the dataset's qrels. Stability is not quality.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Hashable, Mapping, Sequence

__all__ = [
    "jaccard_at_k",
    "kendall_tau_b",
    "paper_dcg",
    "paper_ndcg_at_k",
    "operator_level_metrics",
    "query_level_metrics",
    "macro_aggregate",
]


def jaccard_at_k(list_a: Sequence[Hashable], list_b: Sequence[Hashable]) -> float:
    set_a, set_b = set(list_a), set(list_b)
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def kendall_tau_b(ranking_a: Sequence[Hashable], ranking_b: Sequence[Hashable]) -> float:
    """Kendall's tau-b between two complete rankings of the same document set.

    The naive sort emits total orders (stable sort over win counts), so tau-b reduces
    to ``(concordant - discordant) / total_pairs``.
    """
    if set(ranking_a) != set(ranking_b):
        raise ValueError("rankings must cover the same document set")
    if len(set(ranking_a)) != len(ranking_a):
        raise ValueError("ranking_a must be a total order over distinct documents")
    n = len(ranking_a)
    if n < 2:
        return 1.0
    pos_b = {doc: p for p, doc in enumerate(ranking_b)}
    concordant = discordant = 0
    for earlier in range(n):
        for later in range(earlier + 1, n):
            if pos_b[ranking_a[earlier]] < pos_b[ranking_a[later]]:
                concordant += 1
            else:
                discordant += 1
    total = n * (n - 1) // 2
    return (concordant - discordant) / total


def paper_dcg(values: Sequence[int]) -> float:
    """DCG exactly as the LOTUS paper's reranking script defines it: (2^rel - 1) / log2(rank + 2)."""
    return float(sum((2**value - 1) / math.log2(index + 2) for index, value in enumerate(values)))


def paper_ndcg_at_k(found_qrels_values: Sequence[int], ideal_qrels_values: Sequence[int], k: int) -> float:
    """nDCG@K = dcg(found[:k]) / dcg(sorted(ideal)[:k]) — the paper's formula, not sklearn's.

    With a single judged-relevant document this reduces to ``1 / log2(rank + 1)``, so it
    discriminates *where* the relevant document landed (sklearn's ``ndcg_score`` with a
    constant ideal returns 1.0 unconditionally and cannot discriminate).
    """
    found = list(found_qrels_values)[:k]
    ideal = sorted((int(v) for v in ideal_qrels_values), reverse=True)[:k]
    if not found:
        return 0.0
    idcg = paper_dcg(ideal)
    if idcg <= 0:
        return 0.0
    return paper_dcg(found) / idcg


def _valid(row: Mapping[str, Any]) -> bool:
    return str(row["strict_parser_status"]) == "valid"


def operator_level_metrics(trace_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Operator-level metrics for one query from its pair response records.

    ``ever_flipped`` is computed over strict-valid verdicts only; invalid responses are
    never counted as semantic flips.
    """
    repetitions = sorted({int(r["repetition_id"]) for r in trace_rows})
    by_pair: dict[str, dict[int, Mapping[str, Any]]] = {}
    for row in trace_rows:
        by_pair.setdefault(str(row["pair_id"]), {})[int(row["repetition_id"])] = row

    ever_flipped = 0
    never_valid = 0
    invalid_responses = 0
    for rows in by_pair.values():
        verdicts = {bool(r["strict_parsed_decision"]) for r in rows.values() if _valid(r)}
        if not verdicts:
            never_valid += 1
        elif len(verdicts) > 1:
            ever_flipped += 1
        invalid_responses += sum(1 for r in rows.values() if not _valid(r))

    run_pair_disagreements: list[dict[str, Any]] = []
    for rep_a, rep_b in combinations(repetitions, 2):
        compared = disagreed = 0
        for rows in by_pair.values():
            a, b = rows.get(rep_a), rows.get(rep_b)
            if a is None or b is None or not (_valid(a) and _valid(b)):
                continue
            compared += 1
            disagreed += int(bool(a["strict_parsed_decision"]) != bool(b["strict_parsed_decision"]))
        run_pair_disagreements.append(
            {
                "run_a": rep_a,
                "run_b": rep_b,
                "compared_pairs": compared,
                "disagreed_pairs": disagreed,
                "disagreement_rate": round(disagreed / compared, 6) if compared else None,
            }
        )
    rates = [r["disagreement_rate"] for r in run_pair_disagreements if r["disagreement_rate"] is not None]

    return {
        "pairs_total": len(by_pair),
        "repetitions": len(repetitions),
        "ever_flipped_pair_count": ever_flipped,
        "ever_flipped_pair_ratio": round(ever_flipped / len(by_pair), 6) if by_pair else None,
        "never_valid_pair_count": never_valid,
        "invalid_response_count": invalid_responses,
        "invalid_response_rate": round(invalid_responses / len(trace_rows), 6) if trace_rows else None,
        "run_pair_disagreements": run_pair_disagreements,
        "mean_run_pair_disagreement": round(sum(rates) / len(rates), 6) if rates else None,
    }


def query_level_metrics(
    run_topks: Sequence[Sequence[str]],
    run_rankings: Sequence[Sequence[str]],
    *,
    k: int,
    qrels: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Query-level metrics across complete runs (Top-K lists + full rankings, recomputed)."""
    if len(run_topks) != len(run_rankings):
        raise ValueError("run_topks and run_rankings must have the same length")

    set_agreements: list[bool] = []
    order_agreements: list[bool] = []
    jaccards: list[float] = []
    taus: list[float] = []
    for a, b in combinations(range(len(run_topks)), 2):
        set_agreements.append(set(run_topks[a]) == set(run_topks[b]))
        order_agreements.append(list(run_topks[a]) == list(run_topks[b]))
        jaccards.append(jaccard_at_k(run_topks[a], run_topks[b]))
        taus.append(kendall_tau_b(run_rankings[a], run_rankings[b]))

    ndcg_values: list[float] = []
    if qrels:
        relevant = {str(d): int(s) for d, s in qrels.items()}
        ideal = list(relevant.values())
        for topk in run_topks:
            found = [int(relevant.get(d, 0)) for d in topk]
            ndcg_values.append(paper_ndcg_at_k(found, ideal, k))

    run_pairs = len(set_agreements)

    def _mean(values: Sequence[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    return {
        "runs": len(run_topks),
        "run_pairs": run_pairs,
        "distinct_topk_ordered": len({tuple(t) for t in run_topks}),
        "distinct_topk_sets": len({frozenset(t) for t in run_topks}),
        "topk_set_exact_agreement_rate": round(sum(set_agreements) / run_pairs, 6) if run_pairs else None,
        "topk_ordered_exact_agreement_rate": round(sum(order_agreements) / run_pairs, 6) if run_pairs else None,
        "mean_jaccard_at_k": _mean(jaccards),
        "min_jaccard_at_k": round(min(jaccards), 6) if jaccards else None,
        "mean_kendall_tau_b_full_ranking": _mean(taus),
        "min_kendall_tau_b_full_ranking": round(min(taus), 6) if taus else None,
        "ndcg_at_k_per_run": [round(v, 6) for v in ndcg_values],
        "ndcg_at_k_mean": _mean(ndcg_values),
        "ground_truth_available": bool(qrels),
    }


def macro_aggregate(per_query: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Macro averages over queries (each query contributes equally)."""

    def _mean(key_path: tuple[str, str]) -> float | None:
        values = [q[key_path[0]][key_path[1]] for q in per_query if q[key_path[0]][key_path[1]] is not None]
        return round(sum(values) / len(values), 6) if values else None

    return {
        "queries": len(per_query),
        "macro_ever_flipped_pair_ratio": _mean(("operator", "ever_flipped_pair_ratio")),
        "macro_mean_run_pair_disagreement": _mean(("operator", "mean_run_pair_disagreement")),
        "macro_invalid_response_rate": _mean(("operator", "invalid_response_rate")),
        "macro_topk_set_exact_agreement": _mean(("query", "topk_set_exact_agreement_rate")),
        "macro_topk_ordered_exact_agreement": _mean(("query", "topk_ordered_exact_agreement_rate")),
        "macro_mean_jaccard_at_k": _mean(("query", "mean_jaccard_at_k")),
        "macro_mean_kendall_tau_b": _mean(("query", "mean_kendall_tau_b_full_ranking")),
        "macro_ndcg_at_k_mean": _mean(("query", "ndcg_at_k_mean")),
    }
