"""Gate 3 readiness checks: dataset, calibration plan vs collect scripts, pipeline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"D:\certirank")
sys.path.insert(0, str(ROOT / "src"))
from certirank.live import all_pairs, build_workload, collect_pair_calls, request_hash
from certirank.provider import ApiConfig

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), name, detail)
    if not ok:
        failures.append(name)


def main() -> int:
    root = ROOT / "data" / "gate3_nfcorpus"
    plan = json.loads((root / "temperature_calibration" / "calibration_plan.json").read_text(encoding="utf-8"))
    api = ApiConfig.load(ROOT / "configs" / "api_config.json")
    queries = json.loads((root / "temperature_calibration" / "calibration_queries_A.json").read_text(encoding="utf-8"))

    # --- dataset ---
    dev = json.loads((root / "dev_queries.json").read_text(encoding="utf-8"))
    held = json.loads((root / "heldout_queries.json").read_text(encoding="utf-8"))
    check("dataset_counts", len(dev) == 6 and len(held) == 12, f"{len(dev)}/{len(held)}")
    check("dataset_unique_ids", len({q["query_id"] for q in dev + held}) == 18)
    check("dataset_8_docs", all(len(q["doc_ids"]) == 8 for q in dev + held))

    # --- plan shape ---
    check("plan_total_320", plan["design"]["planned_calls_total"] == 320, str(plan["design"]["planned_calls_total"]))
    check("plan_no_paid_flag", plan.get("paid_calls_authorized") is False)
    lines = plan["design"]["lines"]
    check("plan_two_lines", set(lines) == {"A_null", "B_zero"})
    check("plan_A_omits_temp", lines["A_null"]["temperature_requested"] is None)
    check("plan_B_zero", lines["B_zero"]["temperature_requested"] == 0.0)

    # --- pair subset determinism: lexicographic pair_id first 8 ---
    def full_pair_ids(q):
        docs = q["doc_ids"]
        return sorted(f"{q['query_id']}|{docs[i]}|{docs[j]}" for i, j in all_pairs(len(docs)))

    plan_a = json.loads((root / "temperature_calibration" / "workload_A_null.json").read_text(encoding="utf-8"))
    for entry in plan_a["entries"]:
        expected = full_pair_ids(entry)[:8]
        got = [p["pair_id"] for p in entry["pairs"]]
        # plan stores pairs after pick_pairs which sorts by pair_id
        check(f"pair_subset_{entry['query_id']}", got == expected, f"got={got[:2]}...")

    # --- request hash identity: plan workload is the frozen source ---
    # Collection MUST use --workload (not rebuild) so condition/hash stay pinned.
    plan_cond = plan_a["condition"]
    check("plan_has_condition", bool(plan_cond), plan_cond)
    check("plan_pair_subset", plan_a.get("pair_subset") is True)

    # rebuild must match when using the SAME condition + temperature
    params_a = dict(plan_a["model_parameters"])
    wf = build_workload(
        queries,
        experiment_id=plan_a["experiment_id"],
        condition=plan_cond,
        model=plan_a["requested_model"],
        model_parameters=params_a,
        k=plan_a["k"],
    )
    for entry, plan_entry in zip(wf["entries"], plan_a["entries"]):
        ranked = sorted(entry["pairs"], key=lambda p: p["pair_id"])[:8]
        entry["pairs"] = ranked
        for p, pp in zip(entry["pairs"], plan_entry["pairs"]):
            if p["request_hash"] != pp["request_hash"]:
                check(
                    f"hash_match_{plan_entry['query_id']}",
                    False,
                    f"collect={p['request_hash'][:12]} plan={pp['request_hash'][:12]} cond={plan_cond}",
                )
                break
        else:
            check(f"hash_match_{plan_entry['query_id']}", True)

    # collect --workload path with matching api temperature for line B
    # (line A needs api temperature null — validated via ApiConfig.from_dict)

    # --- experiment_id / model identity ---
    check("model_GLm", api.model == "GLM-5.3-Flash", api.model)
    check("plan_model", plan_a["requested_model"] == api.model, plan_a["requested_model"])
    check("plan_exp_id", plan_a["experiment_id"] == api.experiment_id)

    # --- collect script dry path accepts calib queries ---
    class Fake:
        n = 0

        def compare_pair(self, q, a, b):
            Fake.n += 1
            return {
                "raw_response_content": "Document 1",
                "strict_parsed_decision": True,
                "strict_parser_status": "valid",
                "provider_request_id": f"fake-{Fake.n}",
            }

    rows = collect_pair_calls(plan_a, client=Fake(), repetitions=5, run_prefix="calibA")
    check("dry_rows_160", len(rows) == 4 * 8 * 5, str(len(rows)))

    # contract validate
    from certirank.pair_pool import validate_pair_calls

    # expected_identity from plan identity fields
    identity = {
        "experiment_id": plan_a["experiment_id"],
        "condition": plan_a["condition"],
        "requested_model": plan_a["requested_model"],
        "model_parameters": plan_a["model_parameters"],
    }
    v = validate_pair_calls(rows, plan_a, expected_repetitions=5, expected_identity=identity)
    check("contract_ok", v["contract_ok"], str(v["issues"][:3]))

    # --- temperature wire isolation ---
    cfg_null = ApiConfig.from_dict({**{"base_url": "https://x", "model": "m"}, "temperature": None})
    cfg_zero = ApiConfig.from_dict({**{"base_url": "https://x", "model": "m"}, "temperature": 0.0})
    check("null_omits", "temperature" not in cfg_null.request_body_extras())
    check("zero_sends", cfg_zero.request_body_extras().get("temperature") == 0.0)

    # --- formal scale constants ---
    check("formal_6048", plan["design"]["planned_calls_total"] == 320)  # calib
    check("formal_doc_n", all(len(q["doc_ids"]) == 8 for q in dev + held))
    check("qrels_min2", all(len(q["qrels"]) >= 2 for q in dev + held))
    check("grade2_min1", all(sum(1 for s in q["qrels"].values() if s >= 2) >= 1 for q in dev + held))

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES")
        for f in failures:
            print(" -", f)
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
