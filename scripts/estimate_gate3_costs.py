#!/usr/bin/env python3
"""Gate 3 Phase 3: auditable cost estimate. Does not invent provider unit prices.

Unit prices must be filled from Paratera's live billing page / invoice for GLM-5.3-Flash.
Until then, outputs token totals and formulas only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gate3_nfcorpus" / "cost_estimate.json"


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def pair_prompt_tokens(query_text: str, d1: str, d2: str, overhead: int = 80) -> int:
    return overhead + est_tokens(query_text) + est_tokens(d1) + est_tokens(d2)


def workload_prompt_stats(queries: list[dict]) -> dict:
    prompts = []
    for r in queries:
        docs = list(r["doc_texts"].values())
        n = len(docs)
        for i in range(n):
            for j in range(i + 1, n):
                prompts.append(pair_prompt_tokens(r["query_text"], docs[i], docs[j]))
    prompts = sorted(prompts)
    return {
        "n_queries": len(queries),
        "pairs_per_query": (len(queries[0]["doc_texts"]) * (len(queries[0]["doc_texts"]) - 1)) // 2 if queries else 0,
        "prompt_est_min": prompts[0],
        "prompt_est_p50": prompts[len(prompts) // 2],
        "prompt_est_mean": int(mean(prompts)),
        "prompt_est_max": prompts[-1],
        "prompt_tokens_per_full_rep_all_queries": sum(prompts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    # Unit prices: leave null unless user supplies. Never guess.
    parser.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    parser.add_argument("--price-cached-in", type=float, default=None, help="USD per 1M cached input tokens")
    parser.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    args = parser.parse_args()

    dev = json.loads((ROOT / "data" / "gate3_nfcorpus" / "dev_queries.json").read_text(encoding="utf-8"))
    held = json.loads((ROOT / "data" / "gate3_nfcorpus" / "heldout_queries.json").read_text(encoding="utf-8"))
    calib_plan = json.loads((ROOT / "data" / "gate3_nfcorpus" / "temperature_calibration" / "calibration_plan.json").read_text(encoding="utf-8"))

    dev_stats = workload_prompt_stats(dev)
    held_stats = workload_prompt_stats(held)

    # Calibration: 4 q x 8 pairs (subset), 5 reps x 2 lines
    calib_queries = [q for q in dev if q["query_id"] in calib_plan["design"]["queries"]]
    calib_prompts = []
    for r in calib_queries:
        docs = list(r["doc_texts"].values())
        pairs = [(i, j) for i in range(len(docs)) for j in range(i + 1, len(docs))]
        # same lexicographic pair_id rule roughly: use first 8 by document id order in pairs
        # recompute from plan is safer — use mean of full and note subset
        for i, j in pairs[:8]:
            calib_prompts.append(pair_prompt_tokens(r["query_text"], docs[i], docs[j]))
    calib_prompt_mean = int(mean(calib_prompts)) if calib_prompts else 0
    calib_prompt_sum_one_line = sum(calib_prompts)

    # Token budgets (estimates; completion is small — pair answers are short).
    # Observed on example set: prompt ~92, completion ~12. Medical pair answers typically 5–30.
    completion_obs_mean = 12
    completion_cons = 32  # conservative for thinking-style tails
    completion_hard_cap = 128  # max_tokens

    def phase(name: str, n_calls: int, prompt_mean: int, completion_mean: int) -> dict:
        return {
            "phase": name,
            "planned_logical_calls": n_calls,
            "prompt_tokens_est_total": n_calls * prompt_mean,
            "completion_tokens_est_total": n_calls * completion_mean,
            "prompt_mean_est": prompt_mean,
            "completion_mean_est": completion_mean,
        }

    phases = [
        phase("temperature_calibration", 320, calib_prompt_mean, completion_obs_mean),
        phase("formal_dev", 2016, dev_stats["prompt_est_mean"], completion_obs_mean),
        phase("formal_heldout", 4032, held_stats["prompt_est_mean"], completion_obs_mean),
    ]
    formal_calls = 2016 + 4032
    formal_prompt = 2016 * dev_stats["prompt_est_mean"] + 4032 * held_stats["prompt_est_mean"]
    formal_completion = formal_calls * completion_obs_mean

    prices = {
        "source": "NOT VERIFIED — fill from Paratera billing for GLM-5.3-Flash",
        "usd_per_1M_input": args.price_in,
        "usd_per_1M_cached_input": args.price_cached_in,
        "usd_per_1M_output": args.price_out,
        "retry_billing": "unknown until Paratera ToS/invoice confirms whether failed HTTP 4xx/5xx bill tokens",
    }

    def money(tokens: int, usd_per_1m: float | None) -> float | None:
        if usd_per_1m is None:
            return None
        return tokens / 1_000_000 * usd_per_1m

    def phase_cost(p: dict) -> dict:
        return {
            **p,
            "cost_input_usd": money(p["prompt_tokens_est_total"], args.price_in),
            "cost_output_usd": money(p["completion_tokens_est_total"], args.price_out),
        }

    payload = {
        "schema_version": 1,
        "status": "ESTIMATE_PENDING_PRICES",
        "provider": "Paratera OpenAI-compatible",
        "model": "GLM-5.3-Flash",
        "token_method": {
            "prompt_est": "len(text)//4 on NFCorpus title+text + 80 overhead (chars/4 heuristic)",
            "completion_est": {"observed_example_mean": completion_obs_mean,
                               "conservative": completion_cons,
                               "hard_cap_per_call": completion_hard_cap},
            "note": "Re-estimate after calibration with real usage; do not treat as invoice.",
        },
        "observed_reference": {
            "source": "data/new_pilot (synthetic short docs)",
            "prompt_mean": 92,
            "completion_mean": 12,
            "latency_mean_s": 0.95,
        },
        "workload": {"dev": dev_stats, "heldout": held_stats},
        "phases": [phase_cost(p) for p in phases],
        "formal_total": {
            "planned_logical_calls": 6048,
            "prompt_tokens_est_total": formal_prompt,
            "completion_tokens_est_total": formal_completion,
            "conservative_completion_tokens": formal_calls * completion_cons,
            "hard_cap_completion_tokens": formal_calls * completion_hard_cap,
        },
        "retry_allowance": {
            "recommended_physical_attempt_limit": int(6048 * 1.15),
            "recommended_retry_budget_pct": 15,
            "billing_if_retry_bills_tokens": "unknown — confirm with Paratera",
        },
        "prices": prices,
        "cost_formulas": {
            "input": "USD = prompt_tokens / 1e6 * price_in",
            "output": "USD = completion_tokens / 1e6 * price_out",
            "cached": "if provider bills cache hits lower, split prompt_tokens before multiply",
        },
        "authorization_gates": {
            "calibration": {"calls": 320, "authorized": False},
            "formal_dev": {"calls": 2016, "authorized": False},
            "formal_heldout": {"calls": 4032, "authorized": False},
            "monetary_limit_usd": None,
            "deadline": None,
        },
        "time_estimate": {
            "assumption_mean_latency_s": 1.0,
            "calibration_serial_s": 320 * 1.0,
            "formal_dev_serial_s": 2016 * 1.0,
            "formal_heldout_serial_s": 4032 * 1.0,
            "note": "serial estimate; concurrency=1 preserves request-order logs",
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("wrote", args.out)
    print("planned calls: calib=320 formal=6048")
    print("prompt est totals: calib~", 320 * calib_prompt_mean,
          "dev~", formal_prompt * 2016 / formal_prompt if formal_prompt else 0)
    print("dev prompt est", 2016 * dev_stats["prompt_est_mean"],
          "heldout prompt est", 4032 * held_stats["prompt_est_mean"])
    if args.price_in is None:
        print("PRICES MISSING — cannot output USD. Provide --price-in/--price-out from Paratera billing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
