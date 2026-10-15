"""Pure comparison-aggregation operators and ranking metrics (no I/O, stdlib only)."""

from certirank.operators.metrics import (
    jaccard_at_k,
    kendall_tau_b,
    paper_dcg,
    paper_ndcg_at_k,
)
from certirank.operators.naive_sort import (
    EFFECT_BELOW_TOPK,
    EFFECT_NONE,
    EFFECT_ORDER,
    EFFECT_TOPK_ORDER,
    EFFECT_TOPK_SET,
    classify_flip,
    multi_point_propagation,
    naive_replay,
    single_point_counterfactuals,
)

__all__ = [
    "jaccard_at_k",
    "kendall_tau_b",
    "paper_dcg",
    "paper_ndcg_at_k",
    "EFFECT_BELOW_TOPK",
    "EFFECT_NONE",
    "EFFECT_ORDER",
    "EFFECT_TOPK_ORDER",
    "EFFECT_TOPK_SET",
    "classify_flip",
    "multi_point_propagation",
    "naive_replay",
    "single_point_counterfactuals",
]
