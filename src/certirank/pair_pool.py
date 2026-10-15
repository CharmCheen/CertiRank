"""The Pair Response Pool — data contract, loading, and validation.

A *pair response record* is the atomic unit of every CertiRank experiment: one LLM
comparison answer for one candidate pair, in one call instance. The contract below is
normative (mirrored in ``configs/protocol.yaml``); "same pair_id" alone never makes two
records interchangeable — reuse requires the full request identity to match.

Contract fields (→ the field names used by the LOTUS Gate 2 trace, our first instance):

====================  ==============================  ====================================
contract field        trace field                     role
====================  ==============================  ====================================
query_id              ``query_id``                    which query the comparison belongs to
candidate_1/2_id      ``document_1_id``/``_2_id``     the two candidates, in input order
in-pair positions     ``position_i``/``position_j``   candidate-list positions of the pair
pair slot             ``pair_position``/``pair_id``   slot in the naive enumeration
request identity      ``request_hash``                exported hash plus separately checked
                                                      metadata and manifest lineage
model & prompt ver.   ``requested_model``,            P0 wire condition; the prompt text
                      ``model_parameters``,           itself is pinned by ``request_hash``
                      ``experiment_id``/``condition``
input order           ``document_order``              full candidate list, retrieval order
call instance         ``run_id``, ``repetition_id``,  which execution, which repetition
                      ``provider_request_id``,
                      ``timestamp_utc``
raw answer            ``raw_response_content``        verbatim completion (parser input)
normalized verdict    ``strict_parsed_decision`` +    True = candidate 1 wins
                      ``strict_parser_status``
====================  ==============================  ====================================

The *pool* is the (query_id, pair_position, request_hash) → list of responses table.
It is reusable for offline tournament execution only when every response under one key
shares the same exported identity and passes the metadata checks below. The original
wire request cannot be reconstructed from this export alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "CONTRACT_REQUIRED_FIELDS",
    "load_jsonl",
    "load_workload",
    "validate_pair_calls",
    "build_pair_pool",
    "pool_summary",
]

#: contract field -> the exact key a compliant trace record must carry
CONTRACT_REQUIRED_FIELDS: dict[str, str] = {
    "query_id": "query_id",
    "candidate_1_id": "document_1_id",
    "candidate_2_id": "document_2_id",
    "position_i": "position_i",
    "position_j": "position_j",
    "pair_position": "pair_position",
    "pair_id": "pair_id",
    "request_hash": "request_hash",
    "requested_model": "requested_model",
    "model_parameters": "model_parameters",
    "experiment_id": "experiment_id",
    "condition": "condition",
    "document_order": "document_order",
    "run_id": "run_id",
    "repetition_id": "repetition_id",
    "provider_request_id": "provider_request_id",
    "timestamp_utc": "timestamp_utc",
    "raw_response_content": "raw_response_content",
    "strict_parsed_decision": "strict_parsed_decision",
    "strict_parser_status": "strict_parser_status",
    "lotus_parsed_decision": "lotus_parsed_decision",
}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - corrupt input
                raise ValueError(f"{path}:{line_number}: invalid JSON ({exc})") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(record)
    return records


def load_workload(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate_pair_calls(
    pair_calls: list[Mapping[str, Any]],
    workload: Mapping[str, Any],
    *,
    expected_repetitions: int = 5,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check C1–C8 without trusting a copied hash as proof of field consistency.

    Historical prompts are not included in the export, so their hashes cannot be
    independently rebuilt here. Endpoint provenance remains manifest-level evidence.
    An optional pinned identity prevents uniform model/config drift across the file.
    """
    if type(expected_repetitions) is not int or expected_repetitions < 1:
        raise ValueError("expected_repetitions must be a positive integer")
    issues: list[str] = []
    query_ids = [str(q) for q in workload["query_ids"]]
    if not query_ids or len(set(query_ids)) != len(query_ids) or any(not q for q in query_ids):
        issues.append("C3 workload: query IDs must be nonempty and unique")
    expected_pairs = int(workload["pairs_per_query"])
    entries = {str(e["query_id"]): e for e in workload["entries"]}
    if len(entries) != len(workload["entries"]) or set(entries) != set(query_ids):
        issues.append("C3 workload: duplicate/missing/extra query entries")
    workload_pairs = {}
    for q in query_ids:
        entry = entries.get(q, {"pairs": [], "doc_ids": []})
        pairs = entry["pairs"]
        if (not entry["doc_ids"] or len(set(entry["doc_ids"])) != len(entry["doc_ids"])
                or any(not isinstance(d, str) or not d for d in entry["doc_ids"])):
            issues.append(f"C3 workload {q}: candidate IDs must be nonempty distinct strings")
        workload_pairs[q] = {p["pair_position"]: p for p in pairs}
        n = len(entry["doc_ids"])
        if len(pairs) != expected_pairs or len(workload_pairs[q]) != expected_pairs:
            issues.append(f"C6 workload {q}: incorrect pair count or duplicate slots")
        expected_positions = [(i, j) for i in range(n) for j in range(i + 1, n)]
        if len(expected_positions) != expected_pairs:
            issues.append(f"C3 workload {q}: candidate/pair count mismatch")
        for pos, (i, j) in enumerate(expected_positions):
            slot = workload_pairs[q].get(pos, {})
            if (slot.get("position_i"), slot.get("position_j")) != (i, j):
                issues.append(f"C3 workload {q} slot {pos}: invalid pair positions")
            if (slot.get("document_1_id"), slot.get("document_2_id")) != (entry["doc_ids"][i], entry["doc_ids"][j]):
                issues.append(f"C3 workload {q} slot {pos}: candidate identity mismatch")

    identities: set[str] = set()
    by_slot: dict[tuple[str, int], dict[int, list[Mapping[str, Any]]]] = {}
    instance_ids: set[str] = set()
    run_ids: dict[tuple[str, int], set[str]] = {}
    for index, row in enumerate(pair_calls):
        where = f"row {index}"
        missing = [name for name, key in CONTRACT_REQUIRED_FIELDS.items() if row.get(key) is None]
        if missing:
            issues.append(f"C1 {where}: missing contract fields {missing}")
        for key in ("query_id", "request_hash", "pair_id", "document_1_id", "document_2_id",
                    "requested_model", "experiment_id", "condition", "run_id", "timestamp_utc"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                issues.append(f"C1 {where}: {key} must be a nonempty string")
        try:
            timestamp = datetime.fromisoformat(str(row.get("timestamp_utc")).replace("Z", "+00:00"))
            if timestamp.utcoffset() != timedelta(0):
                raise ValueError("UTC offset required")
        except ValueError:
            issues.append(f"C1 {where}: invalid/unzoned timestamp_utc")
        identity = {k: row.get(k) for k in ("experiment_id", "condition", "requested_model", "model_parameters")}
        identities.add(json.dumps(identity, sort_keys=True))
        if not isinstance(row.get("model_parameters"), dict):
            issues.append(f"C2 {where}: model_parameters must be an object")
        if "condition" in workload and row.get("condition") != workload["condition"]:
            issues.append(f"C2 {where}: condition mismatch vs workload")
        if expected_identity:
            for key, value in expected_identity.items():
                if json.dumps(row.get(key), sort_keys=True) != json.dumps(value, sort_keys=True):
                    issues.append(f"C2 {where}: {key} mismatch vs pinned identity")
        q = str(row.get("query_id"))
        pos, rep = row.get("pair_position"), row.get("repetition_id")
        if type(pos) is not int or type(rep) is not int:
            issues.append(f"C6 {where}: pair_position/repetition_id must be integers")
            continue
        by_slot.setdefault((q, pos), {}).setdefault(rep, []).append(row)
        run_ids.setdefault((q, rep), set()).add(str(row.get("run_id")))
        instance = row.get("provider_request_id")
        if not isinstance(instance, str) or not instance.strip() or instance in instance_ids:
            issues.append(f"C1 {where}: missing/duplicate provider_request_id")
        instance_ids.add(str(instance))
        slot = workload_pairs.get(q, {}).get(pos)
        if slot is None:
            issues.append(f"C3 {where}: unknown query/pair slot {(q, pos)}")
            continue
        for key in ("request_hash", "pair_id", "document_1_id", "document_2_id", "position_i", "position_j"):
            if type(row.get(key)) is not type(slot.get(key)) or row.get(key) != slot.get(key):
                issues.append(f"C3 {where}: {key} mismatch vs workload")
        if row.get("document_order") != entries[q]["doc_ids"]:
            issues.append(f"C4 {where}: document_order mismatch vs workload")
        verdict = row.get("strict_parsed_decision")
        if row.get("strict_parser_status") != "valid" or type(verdict) is not bool:
            issues.append(f"C5 {where}: not a strict-valid boolean verdict")
        raw = row.get("raw_response_content")
        parsed = {"Document 1": True, "Document 2": False}.get(raw.strip()) if isinstance(raw, str) else None
        if parsed is None or parsed is not verdict:
            issues.append(f"C5 {where}: raw answer does not independently parse to stored verdict")
        other = row.get("lotus_parsed_decision")
        if type(other) is not bool or other is not verdict:
            issues.append(f"C8 {where}: missing/nonboolean/disagreeing lotus parser verdict")
    if len(identities) != 1:
        issues.append("C2: expected one experiment/condition/model/parameter identity")
    if any(len(values) != 1 for values in run_ids.values()):
        issues.append("C1: run_id differs within a query/repetition")
    if len({next(iter(v)) for v in run_ids.values() if len(v) == 1}) != len(run_ids):
        issues.append("C1: run_id reused across query/repetition groups")
    expected_slots = {(q, pos) for q in query_ids for pos in workload_pairs[q]}
    for key in sorted(expected_slots):
        reps = by_slot.get(key, {})
        for rep in range(expected_repetitions):
            if len(reps.get(rep, [])) != 1:
                issues.append(f"C6 {key} rep {rep}: expected exactly one response")
        if set(reps) - set(range(expected_repetitions)):
            issues.append(f"C6 {key}: unexpected repetitions")
        hashes = {str(row.get("request_hash")) for rows in reps.values() for row in rows}
        if len(hashes) > 1:
            issues.append(f"C7 {key}: request identity differs across repetitions")
    for key in sorted(set(by_slot) - expected_slots):
        issues.append(f"C6 {key}: response outside the frozen workload")
    if len(pair_calls) != len(query_ids) * expected_pairs * expected_repetitions:
        issues.append("C6: record count does not match declared workload dimensions")
    return {
        "records": len(pair_calls),
        "strict_valid_records": sum(row.get("strict_parser_status") == "valid" and type(row.get("strict_parsed_decision")) is bool for row in pair_calls),
        "queries": sorted(query_ids), "pairs_per_query": expected_pairs,
        "repetitions": expected_repetitions,
        "expected_records": len(query_ids) * expected_pairs * expected_repetitions,
        "issues": issues, "contract_ok": not issues,
    }


def _entry_doc_ids(workload: Mapping[str, Any], query_id: str) -> list[str]:
    for entry in workload["entries"]:
        if str(entry["query_id"]) == query_id:
            return [str(d) for d in entry["doc_ids"]]
    return []


def build_pair_pool(
    pair_calls: list[Mapping[str, Any]],
    *,
    require_valid: bool = True,
) -> dict[str, Any]:
    """Assemble the Pair Response Pool from validated records.

    Structure returned::

        {
          query_id: {
            "request_hashes": {pair_position: request_hash},   # one per slot (C7-checked)
            "verdicts": {pair_position: {repetition_id: bool}},
            "raw_answers": {pair_position: {repetition_id: str}},
          },
          ...
        }
    """
    pool: dict[str, dict[str, Any]] = {}
    for row in pair_calls:
        if type(row.get("strict_parsed_decision")) is not bool:
            raise ValueError("pool never coerces nonboolean verdicts")
        if any(type(row.get(key)) is not int or row[key] < 0 for key in ("pair_position", "repetition_id")):
            raise ValueError("pool positions/repetitions must be nonnegative integers")
        if require_valid and (row.get("strict_parser_status") != "valid" or type(row.get("strict_parsed_decision")) is not bool):
            raise ValueError("pool requires strict-valid boolean records")
        query_id = str(row["query_id"])
        position = int(row["pair_position"])
        repetition = int(row["repetition_id"])
        entry = pool.setdefault(query_id, {"request_hashes": {}, "verdicts": {}, "raw_answers": {}})
        if repetition in entry["verdicts"].get(position, {}):
            raise ValueError("duplicate response instance in pool")
        if position in entry["request_hashes"] and entry["request_hashes"][position] != str(row["request_hash"]):
            raise ValueError("request identity drift in pool")
        entry["request_hashes"][position] = str(row["request_hash"])
        entry["verdicts"].setdefault(position, {})[repetition] = row["strict_parsed_decision"]
        entry["raw_answers"].setdefault(position, {})[repetition] = row["raw_response_content"]
    for entry in pool.values():
        entry["request_hashes"] = dict(sorted(entry["request_hashes"].items()))
        for key in ("verdicts", "raw_answers"):
            entry[key] = {pos: dict(sorted(inner.items())) for pos, inner in sorted(entry[key].items())}
    return dict(sorted(pool.items()))


def pool_summary(pool: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Shape summary of a built pool (used by the audit's pool-usability section)."""
    slots = 0
    responses = 0
    per_query: dict[str, dict[str, int]] = {}
    for query_id, entry in pool.items():
        q_slots = len(entry["verdicts"])
        q_responses = sum(len(v) for v in entry["verdicts"].values())
        slots += q_slots
        responses += q_responses
        per_query[query_id] = {"slots": q_slots, "responses": q_responses}
    return {
        "queries": len(pool),
        "pair_slots": slots,
        "responses": responses,
        "per_query": per_query,
    }
