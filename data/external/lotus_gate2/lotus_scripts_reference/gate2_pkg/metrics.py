"""Gate 2A evaluation metrics — operator level and query level.

Discipline encoded here:

* **Ever-flipped ratio vs run-pair disagreement are different quantities.** The first is
  "did this pair ever produce two distinct valid verdicts across the R runs"; the second
  is "for two specific runs, how often did they disagree". Both are reported; neither is
  presented as the other.
* **Invalid responses are never counted as semantic flips.** Pair-level flip statistics
  use only strict-valid verdicts; runs may disagree on *which* comparisons are valid, and
  the denominators reflect that.
* **Run pairs are dependent.** The C(5,2) = 10 run pairs of one query share runs, so they
  are not independent samples. All run-pair metrics are reported as descriptive
  statistics (per-pair values + macro mean over run pairs, and per-query macro over
  queries); no p-value is computed from them.
* **Answer reproducibility is not answer quality.** Top-K agreement/Jaccard/Kendall
  measure whether runs reproduce each other; nDCG@K measures agreement with SciFact's
  qrels. A run set can be perfectly stable and perfectly wrong, and vice versa.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping, Sequence

__all__ = [
    "kendall_tau_b",
    "jaccard_at_k",
    "operator_level_metrics",
    "query_level_metrics",
    "aggregate_metrics",
]


def kendall_tau_b(ranking_a: Sequence[Any], ranking_b: Sequence[Any]) -> float:
    """Kendall's tau-b between two full rankings (tie-corrected).

    Both inputs are the *complete* orderings the naive sort emits (all 8 documents).
    A stable sort over distinct win-count groups yields a total order — a permutation —
    and for permutations tau-b reduces to ``(concordant - discordant) / total_pairs``.
    Ties in *win counts* do not create order ties, because the stable sort breaks them.
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


def jaccard_at_k(list_a: Sequence[Any], list_b: Sequence[Any]) -> float:
    set_a, set_b = set(list_a), set(list_b)
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def _pair_key(row: Mapping[str, Any]) -> str:
    return str(row["pair_id"])


def operator_level_metrics(trace_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Operator-level metrics for one query: flip ratio, run disagreement, invalid rate.

    ``trace_rows`` are that query's §4.3 trace records across all repetitions.
    """
    repetitions = sorted({int(r["repetition_id"]) for r in trace_rows})
    by_pair: dict[str, dict[int, Mapping[str, Any]]] = {}
    for row in trace_rows:
        by_pair.setdefault(_pair_key(row), {})[int(row["repetition_id"])] = row

    pairs_total = len(by_pair)
    ever_flipped_pairs = 0
    never_valid_pairs = 0
    invalid_responses = 0
    for rows in by_pair.values():
        verdicts = {
            bool(r["strict_parsed_decision"])
            for r in rows.values()
            if str(r["strict_parser_status"]) == "valid"
        }
        if not verdicts:
            never_valid_pairs += 1
        elif len(verdicts) > 1:
            ever_flipped_pairs += 1
        invalid_responses += sum(1 for r in rows.values() if str(r["strict_parser_status"]) != "valid")

    run_pair_disagreements: list[dict[str, Any]] = []
    for rep_a, rep_b in combinations(repetitions, 2):
        compared = disagreed = 0
        for rows in by_pair.values():
            a, b = rows.get(rep_a), rows.get(rep_b)
            if a is None or b is None:
                continue
            if str(a["strict_parser_status"]) != "valid" or str(b["strict_parser_status"]) != "valid":
                continue
            compared += 1
            if bool(a["strict_parsed_decision"]) != bool(b["strict_parsed_decision"]):
                disagreed += 1
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
        "pairs_total": pairs_total,
        "repetitions": len(repetitions),
        "ever_flipped_pair_count": ever_flipped_pairs,
        "ever_flipped_pair_ratio": round(ever_flipped_pairs / pairs_total, 6) if pairs_total else None,
        "never_valid_pair_count": never_valid_pairs,
        "invalid_response_count": invalid_responses,
        "invalid_response_rate": round(invalid_responses / len(trace_rows), 6) if trace_rows else None,
        "run_pair_disagreements": run_pair_disagreements,
        "mean_run_pair_disagreement": round(sum(rates) / len(rates), 6) if rates else None,
        "run_pair_dependence_note": (
            "the C(R,2) run pairs share runs and are NOT independent samples; "
            "values are descriptive only"
        ),
    }


def query_level_metrics(
    run_records: Sequence[Mapping[str, Any]],
    *,
    k: int,
    qrels: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Query-level metrics for one query across its complete Top-K runs."""
    from ..gate23.runner import paper_ndcg_at_k

    runs = sorted(run_records, key=lambda r: int(r["repetition"]))
    ok_runs = [r for r in runs if str(r["execution_status"]) == "ok"]
    topks = [list(r["lotus_ordered_topk_doc_ids"])[:k] for r in ok_runs]
    rankings = [list(r["replay_full_ranking_doc_ids"]) for r in ok_runs]

    set_agreements: list[bool] = []
    order_agreements: list[bool] = []
    jaccards: list[float] = []
    taus: list[float] = []
    for a, b in combinations(range(len(ok_runs)), 2):
        set_agreements.append(set(topks[a]) == set(topks[b]))
        order_agreements.append(list(topks[a]) == list(topks[b]))
        jaccards.append(jaccard_at_k(topks[a], topks[b]))
        taus.append(kendall_tau_b(rankings[a], rankings[b]))

    ndcg_values: list[float] = []
    if qrels:
        relevant = {str(d): int(s) for d, s in qrels.items()}
        ideal = list(relevant.values())
        for topk in topks:
            found = [int(relevant.get(d, 0)) for d in topk]
            ndcg_values.append(paper_ndcg_at_k(found, ideal, k))

    count = len(ok_runs)
    run_pairs = len(set_agreements)
    return {
        "runs_total": len(runs),
        "runs_ok": count,
        "run_pairs": run_pairs,
        "distinct_topk_ordered": len({tuple(t) for t in topks}),
        "distinct_topk_sets": len({frozenset(t) for t in topks}),
        "topk_set_exact_agreement_rate": (
            round(sum(set_agreements) / run_pairs, 6) if run_pairs else None
        ),
        "topk_ordered_exact_agreement_rate": (
            round(sum(order_agreements) / run_pairs, 6) if run_pairs else None
        ),
        "mean_jaccard_at_k": round(sum(jaccards) / run_pairs, 6) if run_pairs else None,
        "min_jaccard_at_k": min(jaccards) if jaccards else None,
        "mean_kendall_tau_b_full_ranking": round(sum(taus) / run_pairs, 6) if run_pairs else None,
        "min_kendall_tau_b_full_ranking": min(taus) if taus else None,
        "ndcg_at_k_per_run": [round(v, 6) for v in ndcg_values],
        "ndcg_at_k_mean": round(sum(ndcg_values) / len(ndcg_values), 6) if ndcg_values else None,
        "ndcg_at_k_range": (
            [round(min(ndcg_values), 6), round(max(ndcg_values), 6)] if ndcg_values else None
        ),
        "ground_truth_available": bool(qrels),
        "ever_topk_set_change": bool(run_pairs and not all(set_agreements)),
        "ever_topk_order_change": bool(run_pairs and not all(order_agreements)),
        "run_pair_dependence_note": (
            "the C(R,2) run pairs share runs and are NOT independent samples; "
            "values are descriptive only"
        ),
    }


def aggregate_metrics(per_query: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Macro averages over queries (each query contributes equally)."""
    import statistics

    def _mean(values: Sequence[Any]) -> float | None:
        real = [v for v in values if v is not None]
        return round(statistics.fmean(real), 6) if real else None

    return {
        "queries": len(per_query),
        "macro_ever_flipped_pair_ratio": _mean([q["operator"]["ever_flipped_pair_ratio"] for q in per_query]),
        "macro_mean_run_pair_disagreement": _mean(
            [q["operator"]["mean_run_pair_disagreement"] for q in per_query]
        ),
        "macro_invalid_response_rate": _mean([q["operator"]["invalid_response_rate"] for q in per_query]),
        "macro_topk_set_exact_agreement": _mean(
            [q["query"]["topk_set_exact_agreement_rate"] for q in per_query]
        ),
        "macro_topk_ordered_exact_agreement": _mean(
            [q["query"]["topk_ordered_exact_agreement_rate"] for q in per_query]
        ),
        "macro_mean_jaccard_at_k": _mean([q["query"]["mean_jaccard_at_k"] for q in per_query]),
        "macro_mean_kendall_tau_b": _mean(
            [q["query"]["mean_kendall_tau_b_full_ranking"] for q in per_query]
        ),
        "macro_ndcg_at_k_mean": _mean([q["query"]["ndcg_at_k_mean"] for q in per_query]),
        "macro_ndcg_at_k_range": None,  # filled by the caller from per-query ranges when present
    }
