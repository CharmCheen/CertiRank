#!/usr/bin/env python
"""Regenerate the external-data manifest for the frozen LOTUS Gate 2 artifacts.

The manifest pins SHA-256, byte size, and origin for every file under
``data/external/lotus_gate2/`` so the audit can prove the inputs are the immutable
copies captured from the LOTUS project (and detect any post-capture mutation).

Usage (repo root)::

    python scripts/make_data_manifest.py            # rewrite data/manifests/gate2_external_data.json
    python scripts/make_data_manifest.py --check    # verify only, no rewrite (exit 1 on drift)

Stdlib only; never touches the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

EXTERNAL_DIR = REPO_ROOT / "data" / "external" / "lotus_gate2"
MANIFEST_PATH = REPO_ROOT / "data" / "manifests" / "gate2_external_data.json"

#: role of each captured file, keyed by path relative to the external dir
ROLES = {
    "gate2_live_pair_calls.jsonl": "primary raw data: 420 pair response records (== gate2_live_calls.jsonl)",
    "gate2_live_query_results.jsonl": "primary raw data: 15 run records (== gate2_live_runs.jsonl)",
    "gate2_live_analysis.json": "producer-side Gate 2A metrics (cross-check target)",
    "gate2_live_replay_analysis.json": "producer-side Gate 2B propagation analysis (== gate2_live_propagation_analysis.json)",
    "gate2_live_report.md": "producer-side experiment report (numbers audited against)",
    "gate2_live_manifest.json": "producer-side run/budget accounting",
    "gate2_live_verification.json": "producer-side artifact verification result",
    "gate2_live_artifact_verification.json": "producer-side artifact verification result (second copy)",
    "gate2_workload.json": "frozen workload: pairs, pinned request hashes, candidate sets, qrels",
    "provider_runtime.yaml": "endpoint/model config actually used (env-var names only, no secrets)",
    "live_smoke_20261014T131841Z/": "pre-pilot live smoke (28 calls) that gated the pilot",
    "live_smoke_20261014T114455Z/": "blocked-state evidence for the endpoint deviation",
    "lotus_scripts_reference/": "read-only provenance copies of the LOTUS scripts that produced the data",
}

#: upstream files referenced by the workload: hash recorded for lineage, content not copied
UPSTREAM_LINEAGE = {
    "gate1b_retrieval_candidates.jsonl": {
        "sha256": "652afb02b5a96cbe8690cc7137214b383d3341bc582e835b239f3cedac02da5a",
        "bytes": 7362299,
        "note": "frozen Gate 1B retrieval; source of the Top-8 candidate lists (hash only, not copied)",
    },
    "gate1b_pairset_manifest.json": {
        "sha256": "b322116994b00133066f61c6a4261764d68e2831224f4d666980238693287d2f",
        "bytes": 5682,
        "note": "Gate 1B pairset manifest; dataset lineage (BEIR/SciFact), hash only",
    },
}

PROVENANCE = {
    "source_project": "LOTUS nondeterminism study",
    "source_repo_path_at_capture": "D:/FDU/Experiment/Lotus/lotus-code (checkout of lotus-data/lotus)",
    "source_git_commit": "136ae4f4a344a2f75d89f811e516dfcb0de30e46",
    "source_git_branch": "research/dynamic-semantic-maintenance",
    "captured_at_utc_date": "2026-10-15",
    "experiment": {
        "substudy": "Gate2-NondeterminismPropagation",
        "gate": "G2",
        "condition": "P0 (retargeted endpoint, see report section A)",
        "run_id": "live_pilot_20261014T132233Z",
        "planned_calls": 420,
        "runs": 15,
        "model": "DeepSeek-V4.1-Flash via https://llmapi.paratera.com (thinking disabled, max_tokens 128)",
    },
    "secrets_policy": (
        "No API keys, .env files, or non-public query content are stored here. "
        "provider_runtime.yaml contains environment-variable NAMES only."
    ),
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest() -> dict:
    files = []
    for path in sorted(EXTERNAL_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(EXTERNAL_DIR).as_posix()
        role = None
        for prefix, text in ROLES.items():
            if rel == prefix.rstrip("/") or rel.startswith(prefix):
                role = text
                break
        files.append({"path": rel, "sha256": sha256_of(path), "bytes": path.stat().st_size, "role": role})
    return {
        "manifest_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "external_dir": "data/external/lotus_gate2",
        "provenance": PROVENANCE,
        "upstream_lineage": UPSTREAM_LINEAGE,
        "files": files,
    }


def verify(manifest: dict) -> list[str]:
    problems = []
    seen = set()
    for entry in manifest["files"]:
        path = EXTERNAL_DIR / entry["path"]
        seen.add(entry["path"])
        if not path.is_file():
            problems.append(f"missing: {entry['path']}")
            continue
        actual = sha256_of(path)
        if actual != entry["sha256"]:
            problems.append(f"sha256 drift: {entry['path']}")
        if path.stat().st_size != entry["bytes"]:
            problems.append(f"size drift: {entry['path']}")
    for path in sorted(EXTERNAL_DIR.rglob("*")):
        if path.is_file() and path.relative_to(EXTERNAL_DIR).as_posix() not in seen:
            problems.append(f"uncatalogued file: {path.relative_to(EXTERNAL_DIR).as_posix()}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing manifest only")
    args = parser.parse_args()

    if args.check or MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        problems = verify(manifest)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}")
            return 1
        print(f"OK {len(manifest['files'])} files verified against {MANIFEST_PATH.relative_to(REPO_ROOT)}")
        return 0

    manifest = build_manifest()
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST_PATH} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
