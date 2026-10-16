#!/usr/bin/env python3
"""Collect live pair responses (authorized paid calls).

Modes:
  --workload FROZEN.json   Use a frozen workload (request_hash already pinned).
  --queries QUERIES.json   Build a workload from queries (optional --pairs-per-query subset).

Examples:
  python scripts/collect_pair_responses.py --workload data/gate3_nfcorpus/temperature_calibration/workload_A_null.json \
    --out RUN_DIR --repetitions 5 --split dev

  python scripts/collect_pair_responses.py --queries MY.json --split dev --out DIR \
    --repetitions 12 --query-prefix dev- --pairs-per-query 28
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.live import (
    build_workload,
    collect_pair_calls,
    identity_from_config,
    write_acquisition,
)
from certirank.pair_pool import validate_pair_calls
from certirank.provider import ApiConfig, OpenAICompatibleClient, ProviderConfigError


def subset_pairs(workload: dict, pairs_per_query: int | None) -> dict:
    """Keep lexicographic first N pairs per query; mark pair_subset only if incomplete."""
    if pairs_per_query is None:
        return workload
    out = json.loads(json.dumps(workload))
    for entry in out["entries"]:
        ranked = sorted(entry["pairs"], key=lambda p: p["pair_id"])
        entry["pairs"] = ranked[:pairs_per_query]
        for i, pair in enumerate(entry["pairs"]):
            pair["subset_index"] = i
    out["pairs_per_query"] = pairs_per_query
    # only a true subset breaks C(n,2) completeness
    full = (len(out["entries"][0]["doc_ids"]) * (len(out["entries"][0]["doc_ids"]) - 1)) // 2
    out["pair_subset"] = pairs_per_query < full
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--workload", type=Path, help="frozen workload JSON (preferred for calibration)")
    src.add_argument("--queries", type=Path, help="JSON list of {query_id,query_text,doc_ids,doc_texts,qrels}")
    parser.add_argument("--split", choices=("dev", "heldout", "calibration"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api-config", type=Path, default=ROOT / "configs" / "api_config.json")
    parser.add_argument("--experiment-config", type=Path, default=ROOT / "configs" / "experiment.json")
    parser.add_argument("--repetitions", type=int, default=None)
    parser.add_argument("--k", type=int, default=None)
    parser.add_argument("--limit-pairs", type=int, default=None)
    parser.add_argument("--query-prefix", default="")
    parser.add_argument("--pairs-per-query", type=int, default=None,
                        help="keep lexicographic first N pairs (calibration subset)")
    parser.add_argument("--condition", default=None,
                        help="override condition string (default: from workload or api.condition|split)")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="keep existing pair_calls.jsonl rows and only fill missing (query,pair,rep)")
    args = parser.parse_args()

    try:
        api = ApiConfig.load(args.api_config)
    except ProviderConfigError as exc:
        print(f"FAIL {exc}")
        return 1
    experiment = json.loads(args.experiment_config.read_text(encoding="utf-8"))
    reps = args.repetitions if args.repetitions is not None else experiment["formal"]["response_count"]
    k = args.k if args.k is not None else experiment["k"]

    if args.workload:
        workload = json.loads(args.workload.read_text(encoding="utf-8"))
        if args.query_prefix:
            print("FAIL --query-prefix is only for --queries mode")
            return 1
        if args.pairs_per_query is not None:
            workload = subset_pairs(workload, args.pairs_per_query)
    else:
        queries = json.loads(args.queries.read_text(encoding="utf-8"))
        if not isinstance(queries, list):
            print("FAIL --queries must be a JSON list")
            return 1
        if args.query_prefix:
            queries = [{**q, "query_id": f"{args.query_prefix}{q['query_id']}"} for q in queries]
        condition = args.condition or f"{api.condition}|{args.split}"
        params = api.model_parameters()
        workload = build_workload(
            queries,
            experiment_id=api.experiment_id,
            condition=condition,
            model=api.model,
            model_parameters=params,
            k=k,
        )
        if args.pairs_per_query is not None:
            workload = subset_pairs(workload, args.pairs_per_query)

    if args.condition and args.workload:
        # allow only if it matches frozen workload; never rewrite frozen identity
        if workload.get("condition") != args.condition:
            print(f"FAIL --condition {args.condition!r} conflicts with frozen workload condition {workload.get('condition')!r}")
            return 1

    identity = {
        "experiment_id": workload["experiment_id"],
        "condition": workload["condition"],
        "requested_model": workload["requested_model"],
        "model_parameters": workload["model_parameters"],
    }
    # Cross-check live API model identity vs frozen workload (temperature may differ by design line).
    if api.model != workload["requested_model"]:
        print(f"FAIL api model {api.model!r} != workload model {workload['requested_model']!r}")
        return 1
    api_temp = api.model_parameters().get("temperature_requested")
    work_temp = workload["model_parameters"].get("temperature_requested")
    if api_temp != work_temp:
        print(
            f"FAIL api temperature {api_temp!r} != workload temperature {work_temp!r}. "
            f"Set configs/api_config.json temperature to match the frozen line "
            f"(null for A / 0.0 for B)."
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        write_acquisition(args.out, workload=workload, rows=[], identity=identity, config=api)
        print(f"DRY_RUN wrote workload condition={workload['condition']} pairs={workload['pairs_per_query']}; zero calls")
        return 0

    try:
        api.require_api_key()
        client = OpenAICompatibleClient(api)
    except ProviderConfigError as exc:
        print(f"FAIL {exc}")
        return 1

    out_jsonl = args.out / "pair_calls.jsonl"
    rows: list[dict] = []
    have: set[tuple] = set()
    if out_jsonl.is_file():
        from certirank.pair_pool import load_jsonl
        existing = load_jsonl(out_jsonl)
        if not args.resume and existing:
            print(f"FAIL {out_jsonl} exists with {len(existing)} rows; pass --resume to continue")
            return 1
        rows.extend(existing)
        have = {(r.get("query_id"), r.get("pair_id"), r.get("repetition_id")) for r in existing}
        print(f"resume: loaded {len(existing)} rows")

    print(
        f"LIVE collection: model={api.model} temperature={api.temperature} "
        f"thinking_mode={api.thinking_mode} reps={reps} "
        f"queries={len(workload['entries'])} pairs/query={workload['pairs_per_query']} "
        f"condition={workload['condition']} pair_subset={workload.get('pair_subset', False)} "
        f"already={len(rows)}"
    )

    def checkpoint(row: dict) -> None:
        args.out.mkdir(parents=True, exist_ok=True)
        with out_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    for rep in range(reps):
        remaining = json.loads(json.dumps(workload))
        for entry in remaining["entries"]:
            entry["pairs"] = [
                p for p in entry["pairs"]
                if (entry["query_id"], p["pair_id"], rep) not in have
            ]
        if sum(len(e["pairs"]) for e in remaining["entries"]) == 0:
            print(f"checkpoint rep {rep}: already complete", flush=True)
            continue

        def on_row(row: dict, _rep=rep) -> None:
            row["repetition_id"] = _rep
            row["run_id"] = f"{args.split}-{row['query_id']}-r{_rep}"
            key = (row["query_id"], row["pair_id"], _rep)
            if key not in have:
                have.add(key)
                rows.append(row)
                checkpoint(row)

        collect_pair_calls(
            remaining,
            client=client,
            repetitions=1,
            run_prefix=args.split,
            limit_pairs=args.limit_pairs,
            sleep_seconds=args.sleep,
            logger=lambda msg: print(" ", msg, flush=True),
            on_row=on_row,
        )
        print(f"checkpoint rep {rep}: total {len(rows)}", flush=True)

    validation = validate_pair_calls(rows, workload, expected_repetitions=reps, expected_identity=identity)
    if not validation["contract_ok"]:
        print("FAIL contract", validation["issues"][:8])
        return 1
    paths = write_acquisition(args.out, workload=workload, rows=rows, identity=identity, config=api)
    print(f"wrote {len(rows)} pair responses -> {paths['pair_calls']}")
    print(f"contract_ok={validation['contract_ok']} new_physical_calls={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
