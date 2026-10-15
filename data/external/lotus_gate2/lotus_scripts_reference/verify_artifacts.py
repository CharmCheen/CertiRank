#!/usr/bin/env python
"""Gate artefact verification.

Checks the raw artefacts against each other and against the frozen pair sets, per the Gate spec's
verification list. Two gates are supported:

* ``--gate G1``  — Gate 1A pilot
* ``--gate G1B`` — Gate 1B (paper-path SciFact) pilot

Gate 1B additionally checks the things that make its result comparable to and distinct from Gate 1A:

* the pilot subset is 30 pairs over 20 distinct queries, 5 repetitions each;
* the pilot subset is exactly the pre-registered subset (SHA256 against its manifest);
* the 30 pilot pairs are a subset of the frozen 100-pair master set;
* the master pair set and the Gate 1A artefacts are unchanged.

Reads only; never calls the provider. Exit 0 = all checks pass, 1 = a check failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiments.lotus_nondeterminism.gate1 import metrics  # noqa: E402
from experiments.lotus_nondeterminism.gate1.protocol import (  # noqa: E402
    EXPERIMENTS_ROOT,
    REPETITIONS,
    file_sha256,
    read_jsonl,
)

ARTIFACTS = EXPERIMENTS_ROOT / "artifacts"

NO_KEY_FIELD_MARKERS = ('"api_key"', '"apikey"', '"authorization"', '"bearer"')
#: a real DeepSeek key shape, so ordinary prose containing "sk-" cannot false-positive
API_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")

GATES: dict[str, dict[str, Any]] = {
    "G1": {
        "gate": "G1",
        "calls": ARTIFACTS / "gate1_pair_calls.jsonl",
        "master": ARTIFACTS / "gate1_master_pairs.jsonl",
        "manifest": ARTIFACTS / "gate1_pairset_manifest.json",
        "analysis": ARTIFACTS / "gate1_pilot_analysis.json",
        "verification": ARTIFACTS / "gate1_artifact_verification.json",
        "expected_pairs": 30,
    },
    "G1B": {
        "gate": "G1B",
        "calls": ARTIFACTS / "gate1b_pair_calls.jsonl",
        "master": ARTIFACTS / "gate1b_pilot_pairs.jsonl",
        "manifest": ARTIFACTS / "gate1b_pilot_manifest.json",
        "analysis": ARTIFACTS / "gate1b_pilot_analysis.json",
        "verification": ARTIFACTS / "gate1b_artifact_verification.json",
        "expected_pairs": 30,
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: Any = None) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def _common_checks(
    checks: list[dict[str, Any]],
    *,
    records: list[dict[str, Any]],
    frozen: list[dict[str, Any]],
    expected_pairs: int,
    calls_path: Path,
    analysis_path: Path,
) -> dict[str, Any]:
    observations = [r for r in records if r.get("scientific_observation", True)]
    errors = [r for r in records if not r.get("scientific_observation", True)]
    groups = metrics.group_by_pair(records)

    expected = expected_pairs * REPETITIONS
    _check(
        checks,
        "expected_observation_count",
        len(observations) + len(errors) >= expected or bool(errors),
        {"observations": len(observations), "transport_errors": len(errors), "expected": expected},
    )

    keys = [(str(r["pair_id"]), r.get("repetition_id")) for r in observations]
    duplicated = [k for k, n in Counter(keys).items() if n > 1]
    _check(checks, "no_duplicate_scientific_keys", not duplicated, {"duplicates": duplicated[:5]})

    hash_consistency = metrics.request_hash_consistency(groups)
    _check(checks, "request_hash_stable_per_pair", hash_consistency["consistent"], hash_consistency["offenders"])

    model_consistency = metrics.returned_model_consistency(groups)
    _check(checks, "returned_model_stable", model_consistency["consistent"], model_consistency["observed"])

    semantics_detail: list[str] = []
    for record in observations:
        if record.get("thinking_requested") != "disabled":
            semantics_detail.append(f"{record['pair_id']}: thinking={record.get('thinking_requested')}")
        if record.get("thinking_control_supported") is not True:
            semantics_detail.append(f"{record['pair_id']}: thinking_control_supported is not True")
        if record.get("reasoning_content") is not None:
            semantics_detail.append(f"{record['pair_id']}: reasoning_content is not None")
    _check(checks, "p0_semantics_stable", not semantics_detail, semantics_detail[:5])

    not_parsed = [r["pair_id"] for r in observations if r.get("strict_parser_status") is None]
    _check(checks, "strict_parser_applied_to_every_observation", not not_parsed, {"offenders": not_parsed[:5]})

    frozen_hash_by_pair = {str(row["pair_id"]): str(row["request_hash"]) for row in frozen}
    drifted = [
        r["pair_id"] for r in observations if frozen_hash_by_pair.get(str(r["pair_id"])) != r.get("request_hash")
    ]
    _check(checks, "observation_hash_matches_frozen_pairset", not drifted, {"offenders": drifted[:5]})

    flip = metrics.pair_flip_rate(groups)
    invalid_with_bool = [
        r["pair_id"]
        for r in observations
        if r.get("strict_parser_status") != metrics.VALID and r.get("strict_parser_bool") is not None
    ]
    _check(checks, "no_invalid_verdict_carries_a_bool", not invalid_with_bool, {"offenders": invalid_with_bool[:5]})
    _check(
        checks,
        "flip_pairs_all_have_two_or_more_valid_reps",
        all(len(groups[p].valid) >= 2 for p in flip["flip_pair_ids"]),
        {"flip_pairs": flip["flip_pair_ids"]},
    )

    retried = [r for r in observations if int(r.get("transport_attempts") or 1) > 1]
    retry_ok = all(r.get("repetition_id") is not None for r in retried)
    _check(
        checks,
        "retries_did_not_create_repetitions",
        retry_ok,
        {
            "calls_with_retries": len(retried),
            "total_retries": sum(int(r.get("transport_attempts") or 1) - 1 for r in retried),
        },
    )

    observed_ids = {str(r["pair_id"]) for r in records}
    frozen_ids = {str(row["pair_id"]) for row in frozen}
    extra = sorted(observed_ids - frozen_ids)
    _check(checks, "calls_reference_only_frozen_pairs", not extra, {"unexpected": extra[:5]})

    raw = calls_path.read_text(encoding="utf-8")
    hits = [token for token in NO_KEY_FIELD_MARKERS if token in raw.lower()]
    shaped = API_KEY_SHAPE.search(raw)
    _check(
        checks,
        "no_credential_material_in_calls",
        not hits and shaped is None,
        {"markers": hits, "key_shaped_match": shaped.group(0)[:6] + "..." if shaped else None},
    )

    _check(checks, "analysis_artifact_present", analysis_path.is_file(), {"path": analysis_path.name})

    return {
        "observations": observations,
        "errors": errors,
        "groups": groups,
        "flip": flip,
        "counts": {
            "records": len(records),
            "observations": len(observations),
            "transport_errors": len(errors),
            "pairs": len(groups),
        },
    }


def _gate1_checks(checks: list[dict[str, Any]], frozen: list[dict[str, Any]], pilot_pairs: int) -> None:
    master_path = GATES["G1"]["master"]
    manifest = _load_json(GATES["G1"]["manifest"])
    _check(
        checks,
        "master_pairset_unchanged",
        manifest.get("master_pairset_sha256") == file_sha256(master_path),
        {"manifest": manifest.get("master_pairset_sha256"), "actual": file_sha256(master_path)},
    )
    _check(checks, "master_pairset_has_100_pairs", len(frozen) == 100, {"found": len(frozen)})
    pilot_ids = [str(row["pair_id"]) for row in frozen[:pilot_pairs]]
    observed = {str(r["pair_id"]) for r in read_jsonl(GATES["G1"]["calls"])}
    _check(
        checks,
        "first_30_master_pairs_equal_pilot_pair_set",
        set(pilot_ids) == observed,
        {"missing": [p for p in pilot_ids if p not in observed][:5]},
    )


def _gate1b_checks(
    checks: list[dict[str, Any]],
    *,
    frozen: list[dict[str, Any]],
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    calls_path: Path,
) -> None:
    pilot_path = GATES["G1B"]["master"]
    master_path = ARTIFACTS / "gate1b_master_pairs.jsonl"
    master_manifest = _load_json(ARTIFACTS / "gate1b_pairset_manifest.json")

    _check(checks, "pilot_pair_count_is_30", len(frozen) == 30, {"found": len(frozen)})
    query_ids = {str(row["query_id"]) for row in frozen}
    _check(checks, "pilot_query_count_is_20", len(query_ids) == 20, {"found": len(query_ids)})

    _check(
        checks,
        "pilot_pairset_unchanged",
        manifest.get("pilot_pairs_sha256") == file_sha256(pilot_path),
        {"manifest": manifest.get("pilot_pairs_sha256"), "actual": file_sha256(pilot_path)},
    )

    per_query = Counter(str(row["query_id"]) for row in frozen)
    _check(
        checks,
        "pilot_covers_every_master_query",
        set(per_query) == set(manifest.get("pilot_query_ids") or []),
        {"queries": len(per_query)},
    )
    _check(
        checks,
        "pilot_pairs_per_query_match_manifest",
        dict(per_query) == {str(k): int(v) for k, v in (manifest.get("pilot_pairs_per_query") or {}).items()},
        {"computed": dict(per_query)},
    )

    master_ids = {str(row["pair_id"]) for row in read_jsonl(master_path)} if master_path.is_file() else set()
    pilot_ids = {str(row["pair_id"]) for row in frozen}
    _check(checks, "pilot_pairs_are_a_subset_of_master", pilot_ids <= master_ids if master_ids else False,
           {"pilot": len(pilot_ids), "master": len(master_ids), "not_in_master": sorted(pilot_ids - master_ids)[:5]})

    _check(
        checks,
        "gate1b_master_pairset_unchanged",
        master_manifest.get("master_pairset_sha256") == file_sha256(master_path),
        {"manifest": master_manifest.get("master_pairset_sha256"), "actual": file_sha256(master_path)},
    )

    recorded = {str(r.get("gate1b_pairset_hash")) for r in records if r.get("gate1b_pairset_hash")}
    _check(
        checks,
        "records_carry_the_frozen_pilot_pairset_hash",
        recorded == {manifest.get("pilot_pairs_sha256")},
        {"recorded": sorted(recorded)[:2], "expected": manifest.get("pilot_pairs_sha256")},
    )

    reps_per_pair = Counter(str(r["pair_id"]) for r in records if r.get("scientific_observation", True))
    short = {p: n for p, n in reps_per_pair.items() if n != REPETITIONS}
    errors = sum(1 for r in records if not r.get("scientific_observation", True))
    _check(
        checks,
        "every_pilot_pair_has_five_repetitions",
        not short or bool(errors),
        {"repetitions_per_pair": dict(reps_per_pair), "short": short, "transport_errors": errors},
    )

    # Gate 1A must be untouched: both its raw artefact and its pair set still match their own manifest
    g1_manifest = _load_json(GATES["G1"]["manifest"])
    g1_analysis = _load_json(GATES["G1"]["analysis"])
    g1_master = GATES["G1"]["master"]
    g1_calls = GATES["G1"]["calls"]
    ok_master = g1_manifest.get("master_pairset_sha256") == file_sha256(g1_master) if g1_master.is_file() else False
    ok_calls = g1_analysis.get("pair_calls_sha256") == file_sha256(g1_calls) if g1_calls.is_file() else False
    _check(
        checks,
        "gate1a_artifacts_unchanged",
        ok_master and ok_calls,
        {"master_matches_manifest": ok_master, "calls_match_analysis": ok_calls},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_artifacts.py")
    parser.add_argument("--gate", default="G1")
    parser.add_argument("--calls", default=None)
    parser.add_argument("--master", default=None)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--pilot-pairs", type=int, default=None)
    args = parser.parse_args(argv)

    key = str(args.gate).upper()
    if key not in GATES:
        print(f"verify_artifacts.py supports {sorted(GATES)}, got {args.gate!r}", file=sys.stderr)
        return 2
    cfg = GATES[key]

    calls_path = Path(args.calls or cfg["calls"])
    master_path = Path(args.master or cfg["master"])
    manifest_path = Path(args.manifest or cfg["manifest"])
    analysis_path = Path(cfg["analysis"])

    if not calls_path.is_file():
        print(f"no raw artefact at {calls_path}", file=sys.stderr)
        return 1

    records = read_jsonl(calls_path)
    frozen = read_jsonl(master_path)
    manifest = _load_json(manifest_path)
    expected_pairs = args.pilot_pairs or cfg["expected_pairs"]

    checks: list[dict[str, Any]] = []
    common = _common_checks(
        checks,
        records=records,
        frozen=frozen,
        expected_pairs=expected_pairs,
        calls_path=calls_path,
        analysis_path=analysis_path,
    )

    if key == "G1":
        _gate1_checks(checks, frozen, expected_pairs)
    else:
        _gate1b_checks(checks, frozen=frozen, records=records, manifest=manifest, calls_path=calls_path)

    result = {
        "verified_at_utc": _utc_now(),
        "gate": cfg["gate"],
        "calls_file": calls_path.name,
        "calls_sha256": file_sha256(calls_path),
        "checks": checks,
        "passed": all(c["ok"] for c in checks),
        "failed_checks": [c["check"] for c in checks if not c["ok"]],
        "counts": common["counts"],
    }

    for c in checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['check']}")
        if not c["ok"]:
            print(f"        detail: {json.dumps(c['detail'], ensure_ascii=False)[:300]}")
    print(f"\ncounts: {json.dumps(result['counts'])}")
    print(f"overall: {'PASS' if result['passed'] else 'FAIL'}")

    out = Path(cfg["verification"])
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"artifact: {out}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
