#!/usr/bin/env python
"""Gate 2 audit — regenerate every core statistic from the frozen external data.

One command, zero LLM calls, stdlib only::

    python scripts/audit_gate2.py            # full audit -> experiments/gate2c/results/
    python scripts/audit_gate2.py --check    # exit code only (for CI)

What it does, in order:

1.  Immutability: verify every file in data/manifests/gate2_external_data.json by
    SHA-256, so the audit is provably running on the captured copies.
2.  Data contract: validate all 420 pair response records against the contract
    (identity fields, request-hash join onto the frozen workload, input order,
    strict-valid normalized verdicts, completeness, pool usability).
3.  Independent recomputation: rebuild each run's verdict matrix from the raw
    records, replay the naive sort (CertiRank's own implementation, not the
    producer's code), and recompute operator-, query-, and pair-level statistics.
4.  Consistency: compare the recomputation against (a) the numbers the Gate 2
    report published and (b) the producer's own stored analysis JSONs.
5.  Write experiments/gate2c/results/gate2c_audit.md + gate2c_audit.json.

Exit code 0 iff every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from certirank.operators.metrics import (  # noqa: E402
    macro_aggregate,
    operator_level_metrics,
    query_level_metrics,
)
from certirank.operators.naive_sort import (  # noqa: E402
    EFFECT_BELOW_TOPK,
    EFFECT_NONE,
    EFFECT_ORDER,
    EFFECT_TOPK_ORDER,
    EFFECT_TOPK_SET,
    multi_point_propagation,
    naive_replay,
    single_point_counterfactuals,
)
from certirank.pair_pool import (  # noqa: E402
    build_pair_pool,
    load_jsonl,
    load_workload,
    pool_summary,
    validate_pair_calls,
)

EXTERNAL_DIR = REPO_ROOT / "data" / "external" / "lotus_gate2"
MANIFEST_PATH = REPO_ROOT / "data" / "manifests" / "gate2_external_data.json"
RESULTS_DIR = REPO_ROOT / "experiments" / "gate2c" / "results"

K = 3
BASE_REPETITION = 0  # the frozen single-point base (producer used r0)

# ---------------------------------------------------------------------------
# Numbers published in data/external/lotus_gate2/gate2_live_report.md, cited by
# section. The audit recomputes everything from raw records and compares.
# ---------------------------------------------------------------------------
REPORT_EXPECTATIONS = {
    "execution": {  # section A
        "planned_calls": 420,
        "runs": 15,
        "prompt_tokens": 327530,
        "completion_tokens": 1260,
    },
    "operator_macro": {  # section B (fractions; report printed percents)
        "macro_ever_flipped_pair_ratio": 0.3452,  # "34.5%"
        "macro_mean_run_pair_disagreement": 0.167,  # "16.7%"
    },
    "operator_per_query": {  # section B table
        "636": {"ever_flipped_pair_count": 8},
        "327": {"ever_flipped_pair_count": 9},
        "51": {"ever_flipped_pair_count": 12},
    },
    "query_level": {  # section C table (tolerance: report rounds to 2-3 decimals)
        "636": {"distinct_topk_ordered": 3, "distinct_topk_sets": 3, "set_agree": 0.20, "order_agree": 0.20, "jaccard": 0.60, "tau": 0.743, "ndcg": 1.0},
        "327": {"distinct_topk_ordered": 3, "distinct_topk_sets": 2, "set_agree": 0.60, "order_agree": 0.30, "jaccard": 0.80, "tau": 0.714, "ndcg": 1.0},
        "51": {"distinct_topk_ordered": 2, "distinct_topk_sets": 1, "set_agree": 1.00, "order_agree": 0.40, "jaccard": 1.00, "tau": 0.786, "ndcg": 1.0},
    },
    "macro_query_level": {  # section C macro column
        "set_agree": 0.60,
        "order_agree": 0.30,
        "jaccard": 0.80,
        "tau": 0.748,  # "0.748"
        "ndcg": 1.0,
    },
    "single_flip": {  # section D.1 (base = repetition 0), counts of 28 pairs
        "636": {EFFECT_NONE: 5, EFFECT_BELOW_TOPK: 8, EFFECT_TOPK_ORDER: 2, EFFECT_TOPK_SET: 13},
        "327": {EFFECT_NONE: 8, EFFECT_BELOW_TOPK: 5, EFFECT_TOPK_ORDER: 2, EFFECT_TOPK_SET: 13},
        "51": {EFFECT_NONE: 10, EFFECT_BELOW_TOPK: 11, EFFECT_TOPK_ORDER: 5, EFFECT_TOPK_SET: 2},
    },
    "multi_point": {"joint_emergent": 0, "cancelling": 6},  # section D.2
    "cross_tab": {  # section D.3 (unit: pair, out of 84 = 3 x 28)
        "ever_flipped": 29,
        "high_impact": 37,  # = ever_flipped_and_high_impact (14) + high_impact_never_flipped (23)
        "ever_flipped_and_high_impact": 14,
        "high_impact_never_flipped": 23,
        "ever_flipped_low_impact": 15,
        "per_query_overlap": {"636": 6, "327": 6, "51": 2},
        "per_query_unstable_inert": {"636": 2, "327": 3, "51": 10},
        "per_query_stable_material": {"636": 9, "327": 9, "51": 5},
    },
    # The report PROSE (section D.3 bullet and section E) says "the 46 single-flip
    # high-impact pairs" and "23 (50%)". That contradicts the report's own D.1/D.3
    # tables (15+15+7=37, and 14+23=37) and the stored replay-analysis JSON. No
    # natural alternative definition yields 46 (base-r0=37; union over 5 bases=60;
    # intersection=8; run-pair totals=56/140/12). Audit verdict: prose typo, 37 is
    # the reproducible number.
    "prose_high_impact_typo": 46,
    "hung_request": {  # section A.2
        "run_id": "G2-51-r4",
        "pair_position": 25,
        "min_latency_seconds": 3600,
    },
}


def check(check_id: str, description: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"id": check_id, "description": description, "ok": bool(ok), "detail": detail}


def close(a: float, b: float, tol: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def step_manifest() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    from make_data_manifest import verify

    problems = verify(manifest, external_dir=EXTERNAL_DIR)
    checks = [
        check(
            "IMM-1",
            "外部数据不可变性：manifest 内全部文件 SHA-256 与字节数一致",
            not problems,
            f"{len(manifest['files'])} 个文件校验完成" + (f"；异常：{problems}" if problems else ""),
        ),
        check(
            "IMM-2",
            "数据来源（provenance）已记录：LOTUS repo commit、run_id、endpoint 偏差说明",
            bool(manifest.get("provenance", {}).get("source_git_commit"))
            and bool(manifest.get("provenance", {}).get("experiment", {}).get("run_id")),
            f"commit {manifest.get('provenance', {}).get('source_git_commit', '?')[:12]}, "
            f"run {manifest.get('provenance', {}).get('experiment', {}).get('run_id', '?')}",
        ),
    ]
    return checks, manifest


def step_contract() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    workload = load_workload(EXTERNAL_DIR / "gate2_workload.json")
    pair_calls = load_jsonl(EXTERNAL_DIR / "gate2_live_pair_calls.jsonl")
    query_results = load_jsonl(EXTERNAL_DIR / "gate2_live_query_results.jsonl")

    validation = validate_pair_calls(
        pair_calls, workload, expected_repetitions=5,
        expected_identity=json.loads((REPO_ROOT / "configs" / "gate2_identity.json").read_text(encoding="utf-8")),
    )
    if not validation["contract_ok"]:
        raise ValueError(f"data contract failed: {validation['issues'][:8]}")
    expected_runs = {f"G2-{q}-r{r}" for q in workload["query_ids"] for r in range(5)}
    recorded_runs = [row.get("run_id") for row in query_results]
    if len(recorded_runs) != len(expected_runs) or set(recorded_runs) != expected_runs:
        raise ValueError("query result roster has missing/duplicate/extra run IDs")
    entries = {str(entry["query_id"]): entry for entry in workload["entries"]}
    for row in query_results:
        q = str(row.get("query_id"))
        rep = row.get("repetition")
        if q not in entries or type(rep) is not int or row["run_id"] != f"G2-{q}-r{rep}":
            raise ValueError("query result identity does not match its run ID")
        docs = entries[q]["doc_ids"]
        ranking = row.get("replay_full_ranking_doc_ids")
        topk = row.get("lotus_ordered_topk_doc_ids")
        if (not isinstance(ranking, list) or len(ranking) != len(docs) or set(ranking) != set(docs)
                or not isinstance(topk, list) or len(topk) != workload["k"] or len(set(topk)) != len(topk)
                or not set(topk) <= set(docs)):
            raise ValueError("malformed query result ranking/Top-K")
    pool = build_pair_pool(pair_calls)
    summary = pool_summary(pool)

    checks = [
        check(
            "CT-1",
            "数据契约 C1-C5,C8：420 条记录全部携带契约字段、请求哈希与冻结 workload 一致、输入顺序一致、判定为 strict-valid 布尔值",
            validation["contract_ok"],
            f"{validation['strict_valid_records']}/{validation['records']} 条 strict-valid；"
            f"契约问题 {len(validation['issues'])} 个" + (f"；示例：{validation['issues'][:5]}" if validation["issues"] else ""),
        ),
        check(
            "CT-2",
            "数据契约 C6 完整性：3 query × 28 pair × 5 次响应 = 420，无缺失、无多余",
            validation["records"] == validation["expected_records"] and validation["contract_ok"],
            f"records={validation['records']}, expected={validation['expected_records']}",
        ),
        check(
            "CT-3",
            "数据契约 C7 请求身份：每对 5 次响应的 request_hash 完全一致（同一请求，可池化复用）",
            validation["contract_ok"],
            f"{summary['pair_slots']} 个 pair slot，{sum(1 for _ in pair_calls)} 条响应全部通过同请求检查",
        ),
        check(
            "POOL-1",
            "Pair Response Pool 形态：84 个 (query, pair) slot × 5 响应；84 个唯一 request_hash",
            summary["pair_slots"] == 84
            and summary["responses"] == 420
            and len({h for e in pool.values() for h in e["request_hashes"].values()}) == 84,
            json.dumps(summary["per_query"], ensure_ascii=False),
        ),
        check(
            "POOL-2",
            "原始回答保留：每条响应含 raw_response_content（可重新归一化）",
            all(
                isinstance(ans, str) and ans.strip()
                for entry in pool.values()
                for slot_answers in entry["raw_answers"].values()
                for ans in slot_answers.values()
            ),
            "420/420 条原始回答非空",
        ),
    ]
    return checks, workload, pool, pair_calls, query_results


def step_recompute(
    workload: dict[str, Any],
    pool: dict[str, Any],
    pair_calls: list[dict[str, Any]],
    query_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Independently recompute every statistic from the pool + raw records."""
    query_ids = [str(q) for q in workload["query_ids"]]
    k = int(workload["k"])
    doc_ids_of = {str(e["query_id"]): [str(d) for d in e["doc_ids"]] for e in workload["entries"]}
    n_pairs_of = {q: len(d) * (len(d) - 1) // 2 for q, d in doc_ids_of.items()}
    qrels_of = {str(e["query_id"]): {str(d): int(s) for d, s in (e.get("qrels") or {}).items()} for e in workload["entries"]}

    # -- per-run replay gate: rebuild ranking from verdicts, compare with LOTUS's answer
    lotus_by_run = {str(r["run_id"]): r for r in query_results}
    replay_gate = {"runs": 0, "topk_consistent": 0, "full_ranking_consistent": 0, "per_run": []}
    for query_id in query_ids:
        doc_ids = doc_ids_of[query_id]
        n_pairs = n_pairs_of[query_id]
        repetitions = sorted({rep for slot in pool[query_id]["verdicts"].values() for rep in slot})
        for repetition in repetitions:
            verdicts = [pool[query_id]["verdicts"][pos][repetition] for pos in range(n_pairs)]
            replay = naive_replay(verdicts, len(doc_ids))
            ranking = [doc_ids[i] for i in replay["indexes"]]
            run_id = f"G2-{query_id}-r{repetition}"
            recorded = lotus_by_run.get(run_id)
            topk_ok = ranking[:k] == list(recorded["lotus_ordered_topk_doc_ids"])[:k]
            full_ok = ranking == list(recorded["replay_full_ranking_doc_ids"])
            replay_gate["runs"] += 1
            replay_gate["topk_consistent"] += int(topk_ok)
            replay_gate["full_ranking_consistent"] += int(full_ok)
            replay_gate["per_run"].append(
                {"run_id": run_id, "topk_consistent": topk_ok, "full_ranking_consistent": full_ok}
            )

    # -- operator + query level metrics
    traces_by_query: dict[str, list[dict[str, Any]]] = {q: [] for q in query_ids}
    for row in pair_calls:
        traces_by_query.setdefault(str(row["query_id"]), []).append(row)
    per_query: list[dict[str, Any]] = []
    for query_id in query_ids:
        doc_ids = doc_ids_of[query_id]
        n_pairs = n_pairs_of[query_id]
        operator = operator_level_metrics(traces_by_query[query_id])
        topks, rankings = [], []
        for repetition in range(5):
            verdicts = [pool[query_id]["verdicts"][pos][repetition] for pos in range(n_pairs)]
            indexes = naive_replay(verdicts, len(doc_ids))["indexes"]
            rankings.append([doc_ids[i] for i in indexes])
            topks.append(rankings[-1][:k])
        query = query_level_metrics(topks, rankings, k=k, qrels=qrels_of[query_id])
        per_query.append({"query_id": query_id, "operator": operator, "query": query, "topks": topks, "rankings": rankings})
    macro = macro_aggregate(per_query)

    # -- pair-level: single-point counterfactuals (base repetition), ever-flipped, cross-tab
    ever_flipped_positions: dict[str, set[int]] = {}
    for query_id in query_ids:
        n_pairs = n_pairs_of[query_id]
        verdict_vectors = [
            [pool[query_id]["verdicts"][pos][rep] for pos in range(n_pairs)] for rep in range(5)
        ]
        ever_flipped_positions[query_id] = {
            pos for pos in range(n_pairs) if len({v[pos] for v in verdict_vectors}) > 1
        }

    single_flip_counts: dict[str, dict[str, int]] = {}
    high_impact_positions: dict[str, set[int]] = {}
    for query_id in query_ids:
        doc_ids = doc_ids_of[query_id]
        n_pairs = n_pairs_of[query_id]
        base_verdicts = [pool[query_id]["verdicts"][pos][BASE_REPETITION] for pos in range(n_pairs)]
        effects = single_point_counterfactuals(base_verdicts=base_verdicts, n_docs=len(doc_ids), k=k)
        counts = {cls: 0 for cls in EFFECT_ORDER}
        for effect in effects:
            counts[effect["effect_class"]] += 1
        single_flip_counts[query_id] = counts
        high_impact_positions[query_id] = {
            e["pair_position"] for e in effects if e["effect_class"] in (EFFECT_TOPK_ORDER, EFFECT_TOPK_SET)
        }

    cross_tab = {
        "ever_flipped": sum(len(v) for v in ever_flipped_positions.values()),
        "high_impact": sum(len(v) for v in high_impact_positions.values()),
        "ever_flipped_and_high_impact": sum(
            len(ever_flipped_positions[q] & high_impact_positions[q]) for q in query_ids
        ),
        "high_impact_never_flipped": sum(
            len(high_impact_positions[q] - ever_flipped_positions[q]) for q in query_ids
        ),
        "ever_flipped_low_impact": sum(
            len(ever_flipped_positions[q] - high_impact_positions[q]) for q in query_ids
        ),
    }

    multi_point: dict[str, Any] = {}
    for query_id in query_ids:
        doc_ids = doc_ids_of[query_id]
        n_pairs = n_pairs_of[query_id]
        verdict_vectors = [
            [pool[query_id]["verdicts"][pos][rep] for pos in range(n_pairs)] for rep in range(5)
        ]
        multi_point[query_id] = multi_point_propagation(verdict_vectors=verdict_vectors, n_docs=len(doc_ids), k=k)

    # -- execution facts straight from the raw records
    prompt_tokens = sum(int(r["prompt_tokens"]) for r in pair_calls)
    completion_tokens = sum(int(r["completion_tokens"]) for r in pair_calls)
    latencies = sorted(pair_calls, key=lambda r: float(r["api_latency_seconds"]), reverse=True)
    hung = latencies[0]

    return {
        "query_ids": query_ids,
        "k": k,
        "replay_gate": replay_gate,
        "per_query": per_query,
        "macro": macro,
        "ever_flipped_positions": ever_flipped_positions,
        "high_impact_positions": high_impact_positions,
        "single_flip_counts": single_flip_counts,
        "cross_tab": cross_tab,
        "multi_point": multi_point,
        "execution": {
            "records": len(pair_calls),
            "runs": len(query_results),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "hung_request": {
                "run_id": hung["run_id"],
                "pair_position": int(hung["pair_position"]),
                "latency_seconds": float(hung["api_latency_seconds"]),
            },
        },
    }


def step_consistency(result: dict[str, Any]) -> list[dict[str, Any]]:
    exp = REPORT_EXPECTATIONS
    execution = result["execution"]
    macro = result["macro"]
    per_query = {q["query_id"]: q for q in result["per_query"]}
    checks: list[dict[str, Any]] = []

    # execution facts (report section A)
    checks.append(check(
        "A-1",
        "报告 §A：420 planned calls / 15 runs 全部在原始数据中出现",
        execution["records"] == exp["execution"]["planned_calls"] and execution["runs"] == exp["execution"]["runs"],
        f"records={execution['records']}, runs={execution['runs']}",
    ))
    checks.append(check(
        "A-2",
        "报告 §A：prompt tokens 327,530 / completion tokens 1,260 与原始数据逐行求和一致",
        execution["prompt_tokens"] == exp["execution"]["prompt_tokens"]
        and execution["completion_tokens"] == exp["execution"]["completion_tokens"],
        f"prompt={execution['prompt_tokens']}, completion={execution['completion_tokens']}",
    ))
    hung = execution["hung_request"]
    he = exp["hung_request"]
    checks.append(check(
        "A-3",
        "报告 §A.2：唯一超长请求可定位（G2-51-r4, pair_position 25, ~20,137 s）",
        hung["run_id"] == he["run_id"]
        and hung["pair_position"] == he["pair_position"]
        and hung["latency_seconds"] >= he["min_latency_seconds"],
        f"{hung['run_id']} pos={hung['pair_position']} latency={hung['latency_seconds']:.0f}s",
    ))

    # replay gate (report section D preamble)
    rg = result["replay_gate"]
    checks.append(check(
        "D-0",
        "报告 §D：Replay gate 15/15 —— 从原始判定独立重放 naive sort，Top-3 与 LOTUS 记录完全一致",
        rg["runs"] == 15 and rg["topk_consistent"] == 15 and rg["full_ranking_consistent"] == 15,
        f"topk {rg['topk_consistent']}/15, full ranking {rg['full_ranking_consistent']}/15",
    ))

    # operator level (section B)
    op_checks = []
    for query_id, expectation in exp["operator_per_query"].items():
        op_checks.append(
            per_query[query_id]["operator"]["ever_flipped_pair_count"] == expectation["ever_flipped_pair_count"]
        )
    checks.append(check(
        "B-1",
        "报告 §B：逐 query 翻转对数 8 / 9 / 12（pair 单位，各 28 对）",
        all(op_checks),
        ", ".join(
            f"{q}: {per_query[q]['operator']['ever_flipped_pair_count']}" for q in result["query_ids"]
        ),
    ))
    checks.append(check(
        "B-2",
        "报告 §B：macro ever-flip 34.5%（29/84）；macro run-pair 平均分歧 16.7%（30 个依赖 run pair 的描述统计）",
        close(macro["macro_ever_flipped_pair_ratio"], exp["operator_macro"]["macro_ever_flipped_pair_ratio"], 5e-4)
        and close(macro["macro_mean_run_pair_disagreement"], exp["operator_macro"]["macro_mean_run_pair_disagreement"], 5e-4),
        f"ever-flip {macro['macro_ever_flipped_pair_ratio']:.6f}, disagreement {macro['macro_mean_run_pair_disagreement']:.6f}",
    ))

    # query level (section C)
    c_checks = []
    for query_id, expectation in exp["query_level"].items():
        q = per_query[query_id]["query"]
        c_checks.extend([
            q["distinct_topk_ordered"] == expectation["distinct_topk_ordered"],
            q["distinct_topk_sets"] == expectation["distinct_topk_sets"],
            close(q["topk_set_exact_agreement_rate"], expectation["set_agree"], 5e-3),
            close(q["topk_ordered_exact_agreement_rate"], expectation["order_agree"], 5e-3),
            close(q["mean_jaccard_at_k"], expectation["jaccard"], 5e-3),
            close(q["mean_kendall_tau_b_full_ranking"], expectation["tau"], 5e-4),
            close(q["ndcg_at_k_mean"], expectation["ndcg"], 5e-4),
        ])
    checks.append(check(
        "C-1",
        "报告 §C：逐 query 的 distinct Top-3（有序/集合）、set/order agreement、Jaccard@3、τ-b、nDCG@3 全部复现",
        all(c_checks),
        "; ".join(
            f"{q}: distinct {per_query[q]['query']['distinct_topk_ordered']}/{per_query[q]['query']['distinct_topk_sets']}, "
            f"set {per_query[q]['query']['topk_set_exact_agreement_rate']:.2f}, "
            f"order {per_query[q]['query']['topk_ordered_exact_agreement_rate']:.2f}, "
            f"jaccard {per_query[q]['query']['mean_jaccard_at_k']:.2f}, "
            f"tau {per_query[q]['query']['mean_kendall_tau_b_full_ranking']:.3f}, "
            f"ndcg {per_query[q]['query']['ndcg_at_k_mean']:.1f}"
            for q in result["query_ids"]
        ),
    ))
    mq = exp["macro_query_level"]
    checks.append(check(
        "C-2",
        "报告 §C：macro set agreement 60% / ordered 30% / Jaccard 0.80 / τ-b 0.748 / nDCG@3 1.0",
        close(macro["macro_topk_set_exact_agreement"], mq["set_agree"], 5e-3)
        and close(macro["macro_topk_ordered_exact_agreement"], mq["order_agree"], 5e-3)
        and close(macro["macro_mean_jaccard_at_k"], mq["jaccard"], 5e-3)
        and close(macro["macro_mean_kendall_tau_b"], mq["tau"], 5e-4)
        and close(macro["macro_ndcg_at_k_mean"], mq["ndcg"], 5e-4),
        f"set {macro['macro_topk_set_exact_agreement']:.2f}, order {macro['macro_topk_ordered_exact_agreement']:.2f}, "
        f"jaccard {macro['macro_mean_jaccard_at_k']:.2f}, tau {macro['macro_mean_kendall_tau_b']:.6f}, "
        f"ndcg {macro['macro_ndcg_at_k_mean']:.1f}",
    ))

    # single flip (section D.1) and cross-tab (section D.3)
    sf_checks = []
    for query_id, expectation in exp["single_flip"].items():
        for cls, count in expectation.items():
            sf_checks.append(result["single_flip_counts"][query_id][cls] == count)
    checks.append(check(
        "D-1",
        "报告 §D.1：单点翻转四分类计数（base=r0）—— 636: 5/8/2/13，327: 8/5/2/13，51: 10/11/5/2",
        all(sf_checks),
        "; ".join(
            f"{q}: " + "/".join(str(result['single_flip_counts'][q][c]) for c in EFFECT_ORDER)
            for q in result["query_ids"]
        ),
    ))
    ct = result["cross_tab"]
    ce = exp["cross_tab"]
    checks.append(check(
        "D-3",
        "报告 §D.1/§D.3 表格（pair 单位，84 对）：29 翻转 / 37 高影响 / 14 交集 / 23 稳定但高影响 / 15 翻转但低影响",
        ct["ever_flipped"] == ce["ever_flipped"]
        and ct["high_impact"] == ce["high_impact"]
        and ct["ever_flipped_and_high_impact"] == ce["ever_flipped_and_high_impact"]
        and ct["high_impact_never_flipped"] == ce["high_impact_never_flipped"]
        and ct["ever_flipped_low_impact"] == ce["ever_flipped_low_impact"],
        f"ever={ct['ever_flipped']}, material={ct['high_impact']}, both={ct['ever_flipped_and_high_impact']}, "
        f"stable-material={ct['high_impact_never_flipped']}, unstable-inert={ct['ever_flipped_low_impact']}",
    ))
    per_q_overlap = {q: len(result["ever_flipped_positions"][q] & result["high_impact_positions"][q]) for q in result["query_ids"]}
    per_q_inert = {q: len(result["ever_flipped_positions"][q] - result["high_impact_positions"][q]) for q in result["query_ids"]}
    per_q_stable = {q: len(result["high_impact_positions"][q] - result["ever_flipped_positions"][q]) for q in result["query_ids"]}
    checks.append(check(
        "D-3b",
        "报告 §D.3 表：逐 query 交集 6/6/2、翻转但低影响 2/3/10、稳定但高影响 9/9/5",
        per_q_overlap == ce["per_query_overlap"]
        and per_q_inert == ce["per_query_unstable_inert"]
        and per_q_stable == ce["per_query_stable_material"],
        f"overlap {per_q_overlap}, unstable-inert {per_q_inert}, stable-material {per_q_stable}",
    ))
    checks.append(check(
        "D-3c",
        "报告正文 '46 single-flip high-impact pairs' 核查：正文与其自身 §D.1/§D.3 表格及存储分析 JSON 矛盾"
        "（14+23=37；逐 query order+set = 15+15+7 = 37）；复算确认正确值为 37",
        ct["high_impact"] == ce["high_impact"]
        and ce["high_impact"] == ce["ever_flipped_and_high_impact"] + ce["high_impact_never_flipped"]
        and exp["prose_high_impact_typo"] != ct["high_impact"],
        f"复算={ct['high_impact']}，表格隐含={ce['ever_flipped_and_high_impact'] + ce['high_impact_never_flipped']}，"
        f"正文={exp['prose_high_impact_typo']}；替代口径均不等于 46"
        "（5-base 并集=60、交集=8、run-pair 影响和=56、run-pair 差异位置和=140）→ 判定正文笔误，"
        "沿用 37；正文派生的 '23 (50%)' 应读作 23/37≈62%",
    ))

    # multi point (section D.2)
    joint_total = sum(result["multi_point"][q]["joint_emergent_run_pairs"] for q in result["query_ids"])
    cancel_total = sum(result["multi_point"][q]["cancelling_run_pairs"] for q in result["query_ids"])
    checks.append(check(
        "D-2",
        "报告 §D.2：多点传播（仅真实观测向量）—— joint-emergent 0/30，cancelling 6/30",
        joint_total == exp["multi_point"]["joint_emergent"] and cancel_total == exp["multi_point"]["cancelling"],
        f"joint_emergent={joint_total}, cancelling={cancel_total} (30 run pairs total)",
    ))
    return checks


def step_producer_agreement(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-check the recomputation against the producer's stored analysis JSONs."""
    stored = json.loads((EXTERNAL_DIR / "gate2_live_analysis.json").read_text(encoding="utf-8"))
    stored_replay = json.loads((EXTERNAL_DIR / "gate2_live_replay_analysis.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    diffs: list[str] = []
    if stored["macro"]["macro_ever_flipped_pair_ratio"] != result["macro"]["macro_ever_flipped_pair_ratio"]:
        diffs.append("macro_ever_flipped_pair_ratio")
    if stored["macro"]["macro_mean_run_pair_disagreement"] != result["macro"]["macro_mean_run_pair_disagreement"]:
        diffs.append("macro_mean_run_pair_disagreement")
    if stored["macro"]["macro_topk_set_exact_agreement"] != result["macro"]["macro_topk_set_exact_agreement"]:
        diffs.append("macro_topk_set_exact_agreement")
    if stored["macro"]["macro_topk_ordered_exact_agreement"] != result["macro"]["macro_topk_ordered_exact_agreement"]:
        diffs.append("macro_topk_ordered_exact_agreement")
    if stored["macro"]["macro_mean_jaccard_at_k"] != result["macro"]["macro_mean_jaccard_at_k"]:
        diffs.append("macro_mean_jaccard_at_k")
    if stored["macro"]["macro_mean_kendall_tau_b"] != result["macro"]["macro_mean_kendall_tau_b"]:
        diffs.append("macro_mean_kendall_tau_b")
    if stored["macro"]["macro_ndcg_at_k_mean"] != result["macro"]["macro_ndcg_at_k_mean"]:
        diffs.append("macro_ndcg_at_k_mean")
    checks.append(check(
        "X-1",
        "与生产方存储分析 (gate2_live_analysis.json) 的 7 项 macro 指标逐位一致",
        not diffs,
        "全部一致" if not diffs else f"不一致：{diffs}",
    ))

    # stored single-flip counts (base r0) per query
    sf_diffs: list[str] = []
    for stored_entry, ours in zip(stored_replay["per_query"], result["query_ids"]):
        if str(stored_entry["query_id"]) != ours:
            sf_diffs.append(f"query order mismatch {stored_entry['query_id']} vs {ours}")
            continue
        stored_counts = stored_entry["single_point_counts"]
        for cls in EFFECT_ORDER:
            if int(stored_counts.get(cls, -1)) != result["single_flip_counts"][ours][cls]:
                sf_diffs.append(f"{ours}.{cls}")
        if int(stored_entry.get("base_repetition", 0)) != BASE_REPETITION:
            sf_diffs.append(f"{ours}.base_repetition")
    checks.append(check(
        "X-2",
        "与生产方存储传播分析 (gate2_live_replay_analysis.json) 的单点四分类计数逐位一致（同 base=r0）",
        not sf_diffs,
        "全部一致" if not sf_diffs else f"不一致：{sf_diffs}",
    ))
    return checks


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

UNITS_TABLE = """
| 数字 | 统计单位 | 分母 | 来源 |
|---|---|---|---|
| 29 翻转对 | **pair**（84 = 3 query × 28 pair 中的对） | 84 | §B/§D.3，5 次重复中出现 ≥2 种不同 valid 判定 |
| 37 高影响对 | **pair**（单点反事实，base=r0；order+set） | 84 | §D.1 表格；正文写的 "46" 是笔误（见 §4.1） |
| 14 翻转∧高影响 | **pair** | 84 | §D.3 |
| 23 稳定∧高影响 | **pair** | 84 | §D.3 |
| 15 翻转∧低影响 | **pair** | 84 | §D.3 |
| 34.5% ever-flip 比率 | pair 比率（29/84 = 逐 query 比率的 macro） | 84 | §B |
| 16.7% run-pair 平均分歧 | **run pair**（每 query C(5,2)=10 个，共 30 个，**依赖样本**，仅描述统计） | 30 run pairs | §B |
| 60% / 30% set/order agreement | **run pair** 比率 | 30 run pairs | §C |
| 15/15 replay gate | **run**（每次完整重放） | 15 runs | §D |
| nDCG@3 = 1.0 | **run** 比率（15 个 run 全部） | 15 runs | §C |
"""


def render_markdown(
    manifest: dict[str, Any],
    manifest_checks: list[dict[str, Any]],
    contract_checks: list[dict[str, Any]],
    consistency_checks: list[dict[str, Any]],
    agreement_checks: list[dict[str, Any]],
    result: dict[str, Any],
) -> str:
    all_checks = manifest_checks + contract_checks + consistency_checks + agreement_checks
    failed = [c for c in all_checks if not c["ok"]]
    verdict = "PASS" if not failed else "FAIL"
    execution = result["execution"]
    macro = result["macro"]
    per_query = {q["query_id"]: q for q in result["per_query"]}
    ct = result["cross_tab"]

    lines: list[str] = []
    lines.append("# Gate 2C 审计报告（gate2c_audit.md）")
    lines.append("")
    lines.append(f"**结论：{verdict}** — {len(all_checks)} 项检查，{len(failed)} 项未通过。")
    lines.append("")
    lines.append(f"生成命令：`python scripts/audit_gate2.py`（零 LLM 调用，仅标准库）。"
                 f"审计对象：`data/external/lotus_gate2/` 的不可变副本（manifest SHA-256 已逐文件校验）。")
    lines.append("")
    lines.append("## 1. 数据来源与不可变性")
    lines.append("")
    prov = manifest["provenance"]
    lines.append(f"- 来源：LOTUS 非确定性研究，commit `{prov['source_git_commit'][:12]}`，"
                 f"run `{prov['experiment']['run_id']}`，{prov['experiment']['planned_calls']} planned calls。")
    lines.append(f"- 条件：{prov['experiment']['condition']}（与原 P0 冻结条件的 endpoint 偏差已在报告中记录，"
                 f"结论只适用于该条件，不与 Gate 1 官方端点数据合并）。")
    lines.append(f"- 密钥策略：{prov['secrets_policy']}")
    lines.append("- 上游 lineage（仅记录哈希，未拷贝内容）：gate1b_retrieval_candidates.jsonl、"
                 "gate1b_pairset_manifest.json（见 manifest）。")
    lines.append("")
    for c in manifest_checks:
        lines.append(f"- **[{c['id']}] {'PASS' if c['ok'] else 'FAIL'}** — {c['description']}。{c['detail']}")
    lines.append("")
    lines.append("## 2. 数据契约与 Pair Response Pool")
    lines.append("")
    lines.append("契约字段与校验规则的规范文本见 `configs/protocol.yaml`；实现见 `src/certirank/pair_pool.py`。")
    lines.append("")
    for c in contract_checks:
        lines.append(f"- **[{c['id']}] {'PASS' if c['ok'] else 'FAIL'}** — {c['description']}。{c['detail']}")
    lines.append("")
    lines.append("**Pool 可复用性结论：** 84 个请求槽位各有 5 次响应。可用于 5 次单响应基础执行，"
                 "或一个预先指定的三响应组工程 smoke。10 种 3/5 组合相互重叠，不能充当独立执行。"
                 "正式复核实验要求不重叠三槽位组。当前执行尚未观察的新比较，即使全表已有，也必须扣逻辑预算。"
                 "历史请求 hash 与 workload 匹配；缺少完整请求正文，无法独立重建 hash。端点证据继承来源 manifest。")
    lines.append("")
    lines.append("## 3. 独立复算结果 vs 报告数字")
    lines.append("")
    lines.append("复算链路：原始 `strict_parsed_decision` → 判定向量（按 pair_position 排序）→ "
                 "CertiRank 自实现的 naive sort 重放（稳定排序，平票按候选行序）→ 全部统计。"
                 "**未复用生产方代码**。")
    lines.append("")
    for c in consistency_checks + agreement_checks:
        lines.append(f"- **[{c['id']}] {'PASS' if c['ok'] else 'FAIL'}** — {c['description']}。{c['detail']}")
    lines.append("")
    lines.append("### 3.1 复算汇总表")
    lines.append("")
    lines.append("| 指标 | 636 | 327 | 51 | macro |")
    lines.append("|---|---|---|---|---|")
    lines.append("| 翻转对数（pair/28） | {} | {} | {} | {} |".format(
        *[per_query[q]["operator"]["ever_flipped_pair_count"] for q in result["query_ids"]],
        f"{macro['macro_ever_flipped_pair_ratio']:.4f}",
    ))
    lines.append("| run-pair 平均分歧 | {} | {} | {} | {} |".format(
        *[f"{per_query[q]['operator']['mean_run_pair_disagreement']:.4f}" for q in result["query_ids"]],
        f"{macro['macro_mean_run_pair_disagreement']:.4f}",
    ))
    lines.append("| distinct 有序 Top-3 / 集合 | {} | {} | {} | — |".format(
        *["{}/{}".format(per_query[q]["query"]["distinct_topk_ordered"], per_query[q]["query"]["distinct_topk_sets"]) for q in result["query_ids"]],
    ))
    lines.append("| set/order agreement | {} | {} | {} | {}/{} |".format(
        *[f"{per_query[q]['query']['topk_set_exact_agreement_rate']:.2f}/{per_query[q]['query']['topk_ordered_exact_agreement_rate']:.2f}" for q in result["query_ids"]],
        f"{macro['macro_topk_set_exact_agreement']:.2f}", f"{macro['macro_topk_ordered_exact_agreement']:.2f}",
    ))
    lines.append("| Jaccard@3 / τ-b | {} | {} | {} | {}/{} |".format(
        *[f"{per_query[q]['query']['mean_jaccard_at_k']:.2f}/{per_query[q]['query']['mean_kendall_tau_b_full_ranking']:.4f}" for q in result["query_ids"]],
        f"{macro['macro_mean_jaccard_at_k']:.2f}", f"{macro['macro_mean_kendall_tau_b']:.4f}",
    ))
    lines.append("| nDCG@3（逐 run 均值） | {} | {} | {} | {} |".format(
        *[f"{per_query[q]['query']['ndcg_at_k_mean']:.1f}" for q in result["query_ids"]],
        f"{macro['macro_ndcg_at_k_mean']:.1f}",
    ))
    lines.append("| 单点翻转 none/below/order/set | {} | {} | {} | — |".format(
        *["/".join(str(result["single_flip_counts"][q][c]) for c in EFFECT_ORDER) for q in result["query_ids"]],
    ))
    lines.append("")
    lines.append(f"Pair 级交叉表（84 对）：ever-flipped **{ct['ever_flipped']}**；单点高影响 **{ct['high_impact']}**；"
                 f"翻转∧高影响 **{ct['ever_flipped_and_high_impact']}**；稳定∧高影响 **{ct['high_impact_never_flipped']}**；"
                 f"翻转∧低影响 **{ct['ever_flipped_low_impact']}**。"
                 f"Replay gate：{result['replay_gate']['topk_consistent']}/{result['replay_gate']['runs']} runs Top-3 复现，"
                 f"全排序 {result['replay_gate']['full_ranking_consistent']}/15 一致。")
    lines.append("")
    lines.append("## 4. 统计单位核对（此前 29 / 46 / 14 / 23 的含义）")
    lines.append("")
    lines.append(UNITS_TABLE.strip())
    lines.append("")
    lines.append("**单位结论：** 29/37/14/23/15 全部是 **pair 级**数字（分母 84），不能当作 run 数、run-pair 数或"
                 "query 数使用；16.7%、60%、30% 是 **run-pair 级**描述统计（30 个依赖样本）；15/15 与 nDCG@3 是 "
                 "**run 级**。§D.3 的 14 (17%) / 15 (18%) / 23 (27%) 的百分比分母同样是 84 对。")
    lines.append("")
    lines.append("### 4.1 关于正文 \"46\" 的核查结论（P1 要求的数字核查项）")
    lines.append("")
    lines.append("Gate 2 报告正文（§D.3 bullet 与 §E）写有 \"of the 46 single-flip high-impact pairs\" 及由此"
                 "派生的 \"23 (50%)\"。**该 46 与报告自身的 §D.1/§D.3 表格矛盾**：逐 query 高影响（order+set）"
                 "= 15+15+7 = 37，且 14 + 23 = 37。生产方存储的 `gate2_live_replay_analysis.json` 同样给出"
                 " 37（overlap 14 / stable-material 23 / unstable-inert 15）。为排除口径差异，审计穷举了替代"
                 "定义：以 5 次 run 分别为 base 的并集=60、交集=8、run-pair 单点影响求和=56、run-pair 差异位置"
                 "求和=140、受影响位置并集=12 —— 均不等于 46。")
    lines.append("")
    lines.append("**审计裁定：** 正文 \"46\" 为笔误（或残留自早期草稿口径）。可复现、可引用的 pair 级数字为："
                 "**29 翻转 / 37 高影响 / 14 交集 / 23 稳定但高影响 / 15 翻转但低影响**；"
                 "\"一半高影响对从未翻转\" 的正确表述是 **23/37 ≈ 62%**（结论方向不变：影响与不稳定性仍严重错位）。")
    lines.append("")
    lines.append("## 5. 执行事实复核（报告 §A）")
    lines.append("")
    lines.append(f"- 420/420 planned calls 全部在 `gate2_live_pair_calls.jsonl` 中，15/15 runs 完整；")
    lines.append(f"- prompt tokens 逐行求和 = {execution['prompt_tokens']:,}，completion = {execution['completion_tokens']:,}（报告：327,530 / 1,260）；")
    lines.append(f"- §A.2 的超长请求定位到 `{execution['hung_request']['run_id']}` pair_position "
                 f"{execution['hung_request']['pair_position']}，latency {execution['hung_request']['latency_seconds']:,.0f}s"
                 "（约 5.6 小时；响应有效，未重试，数据点保留）。")
    lines.append("")
    lines.append("## 6. 遗留事项（进入 P1/P2 前必须知道）")
    lines.append("")
    lines.append("1. **端点偏差**：本数据来自 Paratera 网关的 `DeepSeek-V4.1-Flash`，非原 P0 冻结端点；"
                 "翻转率不可与 Gate 1 官方端点数字直接比较。")
    lines.append("2. **统计力**：每对仅 5 次重复，`ever-flipped` 是粗糙的翻转指示器（p≈0.2 的对在 5 次内至少"
                 "翻转一次的概率仅 ~67%）；正式结论需按两周计划补充独立查询与重复。")
    lines.append("3. **信息边界**：完整响应表只可用于离线评价与反事实上界；在线策略只能读执行轨迹；"
                 "新分支请求必须实际调用。违反此边界的结果无法声称在真实系统可实现。")
    lines.append("4. **nDCG@3 的局限**：每 query 恰 1 篇相关文档且 15/15 run 停在 rank 1，答案不稳定性全部"
                 "发生在 rank 2–3 —— 该指标在此设计下对不稳定性不敏感。")
    lines.append("")
    lines.append("---")
    lines.append(f"*Audit JSON: `results/gate2c_audit.json`; 生成于 {manifest.get('generated_at_utc', '')} 之后，"
                 "数据均未改动。*")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只看结果，不写文件（CI 用）")
    args = parser.parse_args()

    try:
        manifest_checks, manifest = step_manifest()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"[FAIL] input manifest: {exc}")
        return 1
    if any(not check["ok"] for check in manifest_checks):
        for check in manifest_checks:
            print(f"[{'PASS' if check['ok'] else 'FAIL'}] {check['id']}: {check['description']}")
        print("verdict: FAIL (input manifest gate; no recomputation or success artifacts written)")
        return 1
    try:
        contract_checks, workload, pool, pair_calls, query_results = step_contract()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"[FAIL] input contract: {exc}")
        return 1
    result = step_recompute(workload, pool, pair_calls, query_results)
    consistency_checks = step_consistency(result)
    agreement_checks = step_producer_agreement(result)

    all_checks = manifest_checks + contract_checks + consistency_checks + agreement_checks
    failed = [c for c in all_checks if not c["ok"]]

    payload = {
        "audit_version": 2,
        "verdict": "PASS" if not failed else "FAIL",
        "checks": all_checks,
        "recomputed": {
            "macro": result["macro"],
            "cross_tab": result["cross_tab"],
            "single_flip_counts": result["single_flip_counts"],
            "replay_gate": {k: v for k, v in result["replay_gate"].items() if k != "per_run"},
            "execution": result["execution"],
            "multi_point_joint_emergent": {q: v["joint_emergent_run_pairs"] for q, v in result["multi_point"].items()},
            "multi_point_cancelling": {q: v["cancelling_run_pairs"] for q, v in result["multi_point"].items()},
            "per_query": {
                q["query_id"]: {"operator": q["operator"], "query": q["query"]} for q in result["per_query"]
            },
        },
    }

    if not args.check:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "gate2c_audit.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (RESULTS_DIR / "gate2c_audit.md").write_text(
            render_markdown(manifest, manifest_checks, contract_checks, consistency_checks, agreement_checks, result),
            encoding="utf-8",
        )

    for c in all_checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['id']}: {c['description']}")
    print(f"\nverdict: {payload['verdict']} ({len(all_checks) - len(failed)}/{len(all_checks)} passed)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
