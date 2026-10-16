#!/usr/bin/env python3
"""Pin acquisition files into a pool manifest for scripts/run_experiment.py.

Example:
  python scripts/build_pool_manifest.py \
    --dataset-id my-dataset --source-kind real \
    --dev-dir data/new_pilot/dev --heldout-dir data/new_pilot/heldout \
    --groups data/new_pilot/query_groups.json \
    --out data/new_pilot/pool_manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.live import build_manifest


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--source-kind", choices=("real", "synthetic"), required=True)
    parser.add_argument("--dev-dir", type=Path, required=True)
    parser.add_argument("--heldout-dir", type=Path, required=True)
    parser.add_argument("--groups", type=Path, required=True,
                        help="JSON list of {query_id,query_group,split,label_source}")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    groups = json.loads(args.groups.read_text(encoding="utf-8"))
    if not isinstance(groups, list):
        print("FAIL --groups must be a JSON list")
        return 1

    pools = {}
    for split, directory in (("dev", args.dev_dir), ("heldout", args.heldout_dir)):
        files = {
            "workload": directory / "workload.json",
            "pair_calls": directory / "pair_calls.jsonl",
            "identity": directory / "identity.json",
        }
        for name, path in files.items():
            if not path.is_file():
                print(f"FAIL missing {path}")
                return 1
        pools[split] = files

    manifest = build_manifest(
        dataset_id=args.dataset_id,
        source_kind=args.source_kind,
        query_groups=groups,
        pools=pools,
    )
    base = args.out.parent
    for split, files in pools.items():
        entry = {"sha256": manifest["pools"][split]["sha256"]}
        for name in ("workload", "pair_calls", "identity"):
            entry[name] = rel(files[name], base)
        manifest["pools"][split] = entry

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
