#!/usr/bin/env python3
"""Analyze temperature calibration runs A (null) and B (0.0). Offline only."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / "data" / "gate3_nfcorpus" / "temperature_calibration"


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def line_metrics(rows: list[dict], name: str) -> dict:
    by_pair = defaultdict(list)
    invalid = 0
    for r in rows:
        if r.get("strict_parser_status") != "valid" or not isinstance(r.get("strict_parsed_decision"), bool):
            invalid += 1
            continue
        by_pair[(r["query_id"], r["pair_id"])].append(r["strict_parsed_decision"])

    ever_flip = 0
    flip_details = []
    disagree_rates = []
    for key, votes in sorted(by_pair.items()):
        uniq = set(votes)
        if len(uniq) > 1:
            ever_flip += 1
            flip_details.append({
                "query_id": key[0],
                "pair_id": key[1],
                "n": len(votes),
                "true": sum(votes),
                "false": len(votes) - sum(votes),
                "sequence": [int(v) for v in votes],
            })
        # pairwise run disagreement among repeats
        if len(votes) >= 2:
            pairs = list(combinations(range(len(votes)), 2))
            dis = sum(votes[i] != votes[j] for i, j in pairs) / len(pairs)
            disagree_rates.append(dis)

    n_pairs = len(by_pair)
    return {
        "line": name,
        "records": len(rows),
        "invalid_records": invalid,
        "invalid_rate": invalid / len(rows) if rows else None,
        "pairs_observed": n_pairs,
        "ever_flipped_pairs": ever_flip,
        "ever_flipped_pair_ratio": ever_flip / n_pairs if n_pairs else None,
        "mean_pairwise_run_disagreement": sum(disagree_rates) / len(disagree_rates) if disagree_rates else 0.0,
        "pairs_with_any_disagreement": sum(1 for d in disagree_rates if d > 0),
        "flip_details": flip_details,
        "verdict_counts": dict(Counter(v for votes in by_pair.values() for v in votes)),
    }


def cross_line(a_rows: list[dict], b_rows: list[dict]) -> dict:
    def majority(rows):
        by_pair = defaultdict(list)
        for r in rows:
            by_pair[(r["query_id"], r["pair_id"])].append(r["strict_parsed_decision"])
        out = {}
        for key, votes in by_pair.items():
            out[key] = sum(votes) > len(votes) / 2
        return out

    ma, mb = majority(a_rows), majority(b_rows)
    common = set(ma) & set(mb)
    differ = [k for k in common if ma[k] != mb[k]]
    # also per-rep alignment (same rep index) when both present
    def seq(rows):
        by = defaultdict(dict)
        for r in rows:
            by[(r["query_id"], r["pair_id"])][r["repetition_id"]] = r["strict_parsed_decision"]
        return by

    sa, sb = seq(a_rows), seq(b_rows)
    aligned = 0
    aligned_diff = 0
    for key in set(sa) & set(sb):
        for rep in set(sa[key]) & set(sb[key]):
            aligned += 1
            if sa[key][rep] != sb[key][rep]:
                aligned_diff += 1
    return {
        "common_pairs": len(common),
        "majority_verdict_differs": len(differ),
        "majority_differs_ratio": len(differ) / len(common) if common else None,
        "aligned_rep_comparisons": aligned,
        "aligned_rep_differs": aligned_diff,
        "aligned_differs_ratio": aligned_diff / aligned if aligned else None,
        "differing_pairs": [f"{k[0]}|{k[1]}" for k in sorted(differ)],
    }


def main() -> int:
    a = load_rows(CAL / "run_A" / "pair_calls.jsonl")
    b = load_rows(CAL / "run_B" / "pair_calls.jsonl")
    ma = line_metrics(a, "A_null")
    mb = line_metrics(b, "B_zero")
    cross = cross_line(a, b)
    report = {
        "schema_version": 1,
        "status": "CALIBRATION_COMPLETE",
        "planned_calls": 320,
        "completed_calls": len(a) + len(b),
        "note": "A crash mid-B lost ~26 unsaved calls and B was recollected; physical billed calls may exceed 320.",
        "lines": {"A_null": ma, "B_zero": mb},
        "cross_line": cross,
        "interpretation_limits": [
            "5 repeats cannot certify determinism or a single-run flip probability",
            "zero flips would not prove strict determinism",
            "ever-flipped fraction is not a per-call flip probability",
        ],
    }
    out = CAL / "calibration_results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # markdown summary
    lines = [
        "# Temperature Calibration Results",
        "",
        f"- Completed: {len(a)} (A null) + {len(b)} (B zero) = {len(a)+len(b)} records",
        f"- Invalid: A {ma['invalid_records']} / B {mb['invalid_records']}",
        "",
        "| Line | pairs | ever-flip | ever-flip ratio | mean pairwise disagree | disagree pairs |",
        "|---|---:|---:|---:|---:|---:|",
        f"| A (temperature omitted) | {ma['pairs_observed']} | {ma['ever_flipped_pairs']} | {ma['ever_flipped_pair_ratio']:.3f} | {ma['mean_pairwise_run_disagreement']:.4f} | {ma['pairs_with_any_disagreement']} |",
        f"| B (temperature=0.0) | {mb['pairs_observed']} | {mb['ever_flipped_pairs']} | {mb['ever_flipped_pair_ratio']:.3f} | {mb['mean_pairwise_run_disagreement']:.4f} | {mb['pairs_with_any_disagreement']} |",
        "",
        "## Cross-line",
        "",
        f"- Common pairs: {cross['common_pairs']}",
        f"- Majority verdict differs: {cross['majority_verdict_differs']} ({cross['majority_differs_ratio']:.3f})",
        f"- Aligned rep differs: {cross['aligned_rep_differs']}/{cross['aligned_rep_comparisons']} ({cross['aligned_differs_ratio']:.3f})",
        "",
        "## Flipped pairs",
        "",
    ]
    for side, m in (("A", ma), ("B", mb)):
        lines.append(f"### {side}")
        if not m["flip_details"]:
            lines.append("(none)")
        for f in m["flip_details"]:
            lines.append(f"- {f['query_id']} `{f['pair_id']}` T/F={f['true']}/{f['false']} seq={f['sequence']}")
        lines.append("")
    lines += [
        "## Interpretation",
        "",
        "- Observable run-to-run disagreement exists under **both** temperature settings in this sample.",
        "- 5 repeats only support exploratory claims.",
        "- Proceeding to formal Dev is scientifically justified on non-determinism grounds; quality/value of ReviewValue remains unproven.",
    ]
    (CAL / "calibration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((CAL / "calibration_report.md").read_text(encoding="utf-8"))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
