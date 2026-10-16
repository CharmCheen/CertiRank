#!/usr/bin/env python3
"""Unified offline development/heldout entry. Never contacts a model provider.

Dev: --manifest POOLS.json --split dev --out DIR
Heldout additionally requires --model MODEL.json --freeze FREEZE.json.
Freeze creation requires --create-freeze --decision proceed after development.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.campaign import load_cases, run_cases, make_freeze, paired_group_comparison, research_verdict
from certirank.experiment import load_config, config_hash, frozen_settings
from certirank.research_diagnostics import fit_review_value
from certirank.value_model import ReviewValueModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/experiment.json")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "heldout"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--create-freeze", action="store_true")
    parser.add_argument("--decision", choices=("proceed", "hold", "stop"))
    args = parser.parse_args()
    config = load_config(args.config)
    if args.split == "heldout" and args.create_freeze:
        parser.error("freeze must be created from development results")
    model = ReviewValueModel.from_dict(json.loads(args.model.read_text(encoding="utf-8"))) if args.model else None
    freeze = json.loads(args.freeze.read_text(encoding="utf-8")) if args.freeze else None
    manifest, cases = load_cases(args.manifest, config, args.split, freeze=freeze, model=model)
    if args.split == "dev":
        if args.model or args.freeze:
            parser.error("development fitting cannot reuse a heldout/frozen model implicitly")
        model = fit_review_value(cases, config["formal"]["slot_groups"], config["primary_budget"], config_hash(frozen_settings(config)))
    rows, metrics = run_cases(cases, config, model=model)
    if args.create_freeze:
        freeze = make_freeze(config, manifest, model, metrics, decision=args.decision)
    comparator = freeze["comparator"] if freeze else "SimpleTopology"
    comparisons = {budget: paired_group_comparison(metrics, config["candidate_method"], comparator, budget,
        bootstrap_samples=config["formal"]["bootstrap_samples"], seed=config["seed"]) for budget in config["budgets"]}
    summary = {"settings_hash": config_hash(config), "split": args.split, "source_kind": manifest["source_kind"],
        "comparator": comparator, "comparator_status": "frozen" if freeze else "development_reference_only",
        "comparisons": comparisons, "verdict": research_verdict(comparisons[config["primary_budget"]], config,
            split=args.split, source_kind=manifest["source_kind"]), "new_physical_calls": 0,
        "quality": {"metric": "Recall@K from supplied independent labels", "confidence_signal": "unavailable",
                    "single_relevant_queries": sum(len(c["relevant"]) == 1 for c in cases),
                    "mean_recall_step_if_single_positive_no_variants": 1 / (len(cases) * len(config["formal"]["slot_groups"]))
                        if all(len(c["relevant"]) == 1 for c in cases) and len({c["query_group"] for c in cases}) == len(cases) else None}}
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in (("summary.json", summary), ("query_metrics.json", metrics), ("resolved_config.json", config),
                         ("value_model.json", model.to_dict())):
        (args.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "runs.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    if args.create_freeze:
        (args.out / "freeze_manifest.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} complete-algorithm records; verdict={summary['verdict']['verdict']}; zero provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
