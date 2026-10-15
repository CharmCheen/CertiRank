#!/usr/bin/env python
"""certirank_pilot 历史成本情景估算；新运行的硬上限仍待确认。

数据源：Gate 2 live pilot（live_pilot_20261014T132233Z）的 420 条真实调用与
15 个 run 记录（data/external/lotus_gate2/，manifest SHA-256 校验的不可变副本）。

内容：
  1. 实测每次逻辑调用的 token / 延迟 / 传输尝试分布；
  2. 从 run 记录的 total_cost_usd（off_peak / peak 两档计价情景）反解单价，
     并在全部 15 个 run 上交叉验证；
  3. 按《两周执行计划》§3.2 的数据块推算 token 与金额情景。

边界：零 LLM 调用；不改冻结协议（configs/protocol.yaml）；金额为历史逻辑调用
情景（不含重试；表内比较按记账规则读取，不产生新物理调用）。
产物：experiments/certirank_pilot/budget_estimate.{md,json}
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_data_manifest import verify
from certirank.experiment import load_config, config_hash
DATA = ROOT / "data" / "external" / "lotus_gate2"
OUT_DIR = ROOT / "experiments" / "certirank_pilot"

SCENARIOS = ("off_peak", "peak")
PRICE_TOL_USD = 1e-5  # run 记录金额为 6 位小数舍入
LATENCY_OUTLIER_S = 1000  # A-2 悬置请求（~20,137 s）单独列示，不进均值


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def pctl(sorted_xs: list, p: float) -> float:
    return sorted_xs[min(len(sorted_xs) - 1, int(p * len(sorted_xs)))]


def usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def build_artifacts(config_path=None) -> dict[str, str]:
    config = load_config(config_path or ROOT / "configs/experiment.json")
    collection, formal = config["collection"], config["formal"]
    n, extended = formal["n"], collection["optional_n"]
    blocks = [("开发集", collection["dev_query_groups"], n * (n - 1) // 2, formal["response_count"]),
              ("查询留出集", collection["heldout_query_groups"], n * (n - 1) // 2, formal["response_count"]),
              (f"N={extended} 扩展（可选）", collection["optional_query_groups"], extended * (extended - 1) // 2, formal["response_count"])]
    manifest = json.loads((ROOT / "data/manifests/gate2_external_data.json").read_text())
    problems = verify(manifest, external_dir=DATA)
    if problems:
        raise ValueError(f"external data manifest verification failed: {problems[:5]}")
    calls = load_jsonl(DATA / "gate2_live_pair_calls.jsonl")
    runs = load_jsonl(DATA / "gate2_live_query_results.jsonl")
    if len(calls) != 420 or len(runs) != 15:
        raise SystemExit(f"unexpected data shape: {len(calls)} calls, {len(runs)} runs")

    # --- 1. 每次调用的实测分布 ----------------------------------------------
    for c in calls:  # provider 缓存命中 token 是 prompt token 的子集
        if c["cached_prompt_tokens"] > c["prompt_tokens"]:
            raise SystemExit(f"cached > prompt: {c['run_id']} pair {c['pair_position']}")

    prompt = sorted(c["prompt_tokens"] for c in calls)
    cached = sorted(c["cached_prompt_tokens"] for c in calls)
    comp = sorted(c["completion_tokens"] for c in calls)
    lat = sorted(c["api_latency_seconds"] for c in calls)
    lat_wo = [x for x in lat if x < LATENCY_OUTLIER_S]
    cached_ratio = sum(cached) / sum(prompt)
    comp_mean = statistics.mean(comp)
    retries = sum(c["transport_retries"] for c in calls)
    attempts = sorted({c["transport_attempts"] for c in calls})

    by_query: dict[str, list[int]] = {}
    for c in calls:
        by_query.setdefault(c["query_id"], []).append(c["prompt_tokens"])
    q_means = {q: statistics.mean(v) for q, v in sorted(by_query.items())}
    prof_low = min(q_means.values())
    prof_mid = statistics.mean(prompt)
    prof_high = max(q_means.values())

    # --- 2. 反解计价单价并在全部 run 上交叉验证 --------------------------------
    run_tokens: dict[str, list[int]] = {}
    for c in calls:
        t = run_tokens.setdefault(c["run_id"], [0, 0, 0])
        t[0] += c["prompt_tokens"]
        t[1] += c["cached_prompt_tokens"]
        t[2] += c["completion_tokens"]

    prices = {}
    for scen in SCENARIOS:
        pm, ph, po = [], [], []
        for r in runs:
            s_p, s_c, s_o = run_tokens[r["run_id"]]
            cost = r["total_cost_usd"][scen]
            if s_p - s_c > 0:
                pm.append(cost["cache_miss_input_usd"] / (s_p - s_c))
            if s_c > 0:
                ph.append(cost["cache_hit_input_usd"] / s_c)
            if s_o > 0:
                po.append(cost["output_usd"] / s_o)
        prices[scen] = (statistics.median(pm), statistics.median(ph), statistics.median(po))

    max_err = 0.0
    for r in runs:
        s_p, s_c, s_o = run_tokens[r["run_id"]]
        for scen in SCENARIOS:
            pm, ph, po = prices[scen]
            pred = (s_p - s_c) * pm + s_c * ph + s_o * po
            max_err = max(max_err, abs(pred - r["total_cost_usd"][scen]["total_usd"]))
    if max_err >= PRICE_TOL_USD:
        raise SystemExit(f"price cross-validation failed: max_err={max_err:.2e} USD")

    # --- 3. 数据块上限 ---------------------------------------------------------
    def block_cost(n_calls: int, per_call_prompt: float, scen: str) -> float:
        pm, ph, po = prices[scen]
        hit = per_call_prompt * cached_ratio
        return n_calls * ((per_call_prompt - hit) * pm + hit * ph + comp_mean * po)

    rows = []
    for name, nq, npair, nrep in blocks:
        n = nq * npair * nrep
        rows.append({
            "block": name,
            "spec": f"{nq} query × {npair} pair × {nrep} 次",
            "calls": n,
            "prompt_tokens_mid": round(n * prof_mid),
            "cost_usd": {
                scen: {
                    "low": round(block_cost(n, prof_low, scen), 6),
                    "mid": round(block_cost(n, prof_mid, scen), 6),
                    "high": round(block_cost(n, prof_high, scen), 6),
                }
                for scen in SCENARIOS
            },
        })

    def total_row(label: str, subset: list[dict]) -> dict:
        return {
            "block": label,
            "spec": "—",
            "calls": sum(r["calls"] for r in subset),
            "prompt_tokens_mid": sum(r["prompt_tokens_mid"] for r in subset),
            "cost_usd": {
                scen: {
                    k: round(sum(r["cost_usd"][scen][k] for r in subset), 6)
                    for k in ("low", "mid", "high")
                }
                for scen in SCENARIOS
            },
        }

    core_rows = [r for r in rows if "可选" not in r["block"]]
    table = rows + [total_row("核心合计（开发 + 留出）", core_rows),
                    total_row("全量合计（含 N=16 扩展）", rows)]

    payload = {
        "estimate_status": "historical_scenarios_only; live_hard_cap_pending",
        "generated_by": "scripts/pilot_budget.py",
        "settings_hash": config_hash(config),
        "source_run": "live_pilot_20261014T132233Z",
        "source_files": [
            "data/external/lotus_gate2/gate2_live_pair_calls.jsonl",
            "data/external/lotus_gate2/gate2_live_query_results.jsonl",
        ],
        "per_call": {
            "n": len(calls),
            "prompt_tokens": {"sum": sum(prompt), "mean": round(prof_mid, 1),
                              "median": prompt[len(prompt) // 2],
                              "p95": pctl(prompt, 0.95), "max": prompt[-1]},
            "cached_prompt_tokens": {"sum": sum(cached), "ratio": round(cached_ratio, 4)},
            "completion_tokens": {"sum": sum(comp), "mean": comp_mean},
            "api_latency_seconds": {
                "median": round(statistics.median(lat), 3),
                "p95": round(pctl(lat, 0.95), 3),
                "mean_all": round(statistics.mean(lat), 3),
                "mean_excl_suspended": round(statistics.mean(lat_wo), 3),
                "max": round(lat[-1], 1),
            },
            "transport_attempts_observed": attempts,
            "transport_retries_sum": retries,
        },
        "per_query_mean_prompt_tokens": {str(q): round(v, 1) for q, v in q_means.items()},
        "token_profiles_per_call": {
            "low": round(prof_low, 1), "mid": round(prof_mid, 1), "high": round(prof_high, 1)},
        "prices_usd_per_token": {
            scen: {"cache_miss_input": prices[scen][0],
                   "cache_hit_input": prices[scen][1],
                   "output": prices[scen][2]}
            for scen in SCENARIOS
        },
        "price_validation": {"runs_checked": len(runs) * len(SCENARIOS),
                             "max_abs_err_usd": float(f"{max_err:.2e}")},
        "blocks": table,
    }

    # --- 渲染 Markdown ---------------------------------------------------------
    qm = " / ".join(f"q{q} ≈ {v:.0f}" for q, v in q_means.items())
    lines = []
    a = lines.append
    a("# certirank_pilot 历史成本情景估算（付费硬上限待定）")
    a("")
    a("生成：`python scripts/pilot_budget.py`（零 LLM 调用，可重复生成）")
    a("")
    a("数据源：Gate 2 live pilot `live_pilot_20261014T132233Z` 的 420 条真实调用与 15 个 run")
    a("（不可变副本，SHA-256 见 `data/manifests/gate2_external_data.json`）。")
    a("依据：《两周执行计划》§3.2 数据块与 §7 成本纪律（付费运行前金额上限必须可计算）。")
    a("")
    a("## 1. 每次逻辑调用的实测分布（n=420）")
    a("")
    a("| 指标 | 均值 | 中位 | p95 | 最大 | 合计 |")
    a("|---|---|---|---|---|---|")
    a(f"| prompt tokens | {prof_mid:.1f} | {prompt[len(prompt)//2]} | {pctl(prompt, 0.95)} | {prompt[-1]} | {sum(prompt):,} |")
    a(f"| 其中 provider 缓存命中 | {statistics.mean(cached):.1f}（{cached_ratio:.1%}） | {cached[len(cached)//2]} | {pctl(cached, 0.95)} | {cached[-1]} | {sum(cached):,} |")
    a(f"| completion tokens | {comp_mean:.1f} | {comp[len(comp)//2]} | {pctl(comp, 0.95)} | {comp[-1]} | {sum(comp):,} |")
    a(f"| api 延迟（秒） | {statistics.mean(lat_wo):.2f}* | {statistics.median(lat):.2f} | {pctl(lat, 0.95):.2f} | {lat[-1]:,.0f} | {sum(lat):,.0f} |")
    a("")
    a(f"\\* 均值剔除唯一一条悬置请求（G2-51-r4、pair_position 25，约 {lat[-1]/3600:.1f} 小时，")
    a("见审计 A-2）；全样本均值 "
      f"{statistics.mean(lat):.1f} 秒被该请求主导。传输尝试 {attempts[0]} 次/调用，重试 {retries} 次。")
    a("")
    a(f"逐查询单次调用 prompt 均值：{qm} —— 新查询文档长度未知，")
    a("据此取低/中/高三档 token 档位做金额区间。")
    a("")
    a("## 2. 计价基准（由 run 记录反解，off_peak / peak 两档情景）")
    a("")
    a("| 单价（USD / 1M tokens） | off_peak | peak |")
    a("|---|---|---|")
    a(f"| 输入 · 缓存未命中 | ${prices['off_peak'][0]*1e6:.3f} | ${prices['peak'][0]*1e6:.3f} |")
    a(f"| 输入 · 缓存命中 | ${prices['off_peak'][1]*1e6:.3f} | ${prices['peak'][1]*1e6:.3f} |")
    a(f"| 输出 | ${prices['off_peak'][2]*1e6:.3f} | ${prices['peak'][2]*1e6:.3f} |")
    a("")
    a(f"交叉验证：反解单价在 15 run × 2 情景上重算 `total_usd`，最大误差 "
      f"{max_err:.1e} USD（记录值为 6 位小数舍入）。计价情景来自生产方 manifest")
    a("（Paratera 网关 DeepSeek-V4.1-Flash 的估算记录，非实际账单）；正式立项前按当期价格复核。")
    a("")
    a("## 3. 数据采集规模与成本情景（不含重试）")
    a("")
    a(f"单次调用假设：prompt 取档位值、缓存命中率 {cached_ratio:.1%}、completion {comp_mean:.0f} tokens。")
    a("")
    a("| 数据块 | 规格 | 逻辑调用 | prompt tokens（中档） | 金额 off_peak（低—高） | 金额 peak（低—高） |")
    a("|---|---|---|---|---|---|")
    for r in table:
        co, cp = r["cost_usd"]["off_peak"], r["cost_usd"]["peak"]
        a(f"| {r['block']} | {r['spec']} | {r['calls']:,} | ~{r['prompt_tokens_mid']:,} "
          f"| {usd(co['low'])} – {usd(co['high'])} | {usd(cp['low'])} – {usd(cp['high'])} |")
    a("")
    a("## 4. 口径与注意事项")
    a("")
    a("1. **逻辑调用口径**：表内比较（含复核 / 修复路径的新比较）从响应表读取、按计费规则")
    a("   记账，不产生新物理调用；表外比较才需实际调用（两周计划 §3.3）。")
    a("2. **不含重试**：实测 420 次调用传输尝试均为 1 次、重试 0 次；物理重试上限按")
    a("   run manifest 另行约定并单独记账（两周计划 §7）。")
    a("3. **N=16 假设**：每次比较仍为 2 篇文档，单次调用 token 分布与 N=8 近似。")
    a("4. **先导配置**：12 次重复 / 对为两周计划 §3.3 的先导配置，不支撑低违约率统计认证。")
    a(f"5. **串行时间参考**：中位 {statistics.median(lat):.2f} 秒 / 调用 → 核心合计 "
      f"{table[-2]['calls']:,} 次约 {table[-2]['calls']*statistics.median(lat)/3600:.1f} 小时；")
    a(f"   p95 {pctl(lat, 0.95):.1f} 秒 / 调用的情景约 {table[-2]['calls']*pctl(lat, 0.95)/3600:.1f} 小时。")
    a("   按计划 §3.3 以随机 / 交错顺序采集，分散时间漂移与服务版本变化。")
    a("")

    # Stress scenarios use historical prices only. Neither row supplies a live cap.
    stress = []
    for label, prompt_cap, output_cap, attempts_cap in [
        ("高档历史均值、无缓存", prof_high, comp_mean, 1),
        ("历史最大输入、输出128、最多3次尝试的假设", max(prompt), 128, 3),
    ]:
        pm, _, po = prices["peak"]
        stress.append({"scenario": label, "prompt_tokens_per_call": prompt_cap,
                       "output_tokens_per_call": output_cap, "cache_hit_ratio": 0,
                       "attempts_per_logical_call": attempts_cap,
                       "core_usd_historical_price": core_rows[0]["calls"] + core_rows[1]["calls"]})
        stress[-1]["core_usd_historical_price"] *= attempts_cap * (prompt_cap * pm + output_cap * po)
    payload["stress_scenarios"] = stress
    payload["live_hard_cap"] = None
    payload["missing_for_live_cap"] = ["actual new workload and input token limits", "current verified provider prices",
                                        "output/attempt limits and retry billing", "live smoke calls and cumulative money/deadline caps"]
    a("## 5. 缓存与 token 敏感性")
    a("")
    for row in stress:
        a(f"- {row['scenario']}：核心采集约 ${row['core_usd_historical_price']:.2f}。")
    a("")
    a("以上均按历史计价估算。高档输入采用历史查询均值，缓存和输出长度也可能变化；这些值无法构成新运行的金额上限。")
    a("第二个情景中的输入、输出和重试限制只是显式假设，尚未在真实适配器中实施。")
    a("付费运行仍需冻结实际 workload、当期计价、输入/输出限额、重试与累计金额上限、截止时间及授权；P6 live smoke 另列预算。")
    a("延迟情景没有尾部保证；历史已有约 5.6 小时的悬置请求。")
    a("")
    return {"budget_estimate.md": "\n".join(lines),
            "budget_estimate.json": json.dumps(payload, ensure_ascii=False, indent=2) + "\n"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/experiment.json")
    args = parser.parse_args()
    artifacts = build_artifacts(args.config)
    if args.check:
        failures = [name for name, text in artifacts.items() if not (OUT_DIR / name).exists() or (OUT_DIR / name).read_text() != text]
        if failures:
            print("FAIL stale/missing budget artifacts:", ", ".join(failures))
            return 1
        print("PASS historical budget scenarios reproduced; live hard cap pending")
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in artifacts.items():
            (OUT_DIR / name).write_text(text, encoding="utf-8")
        print("wrote historical budget scenarios; no live hard cap or paid authorization")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
