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
request identity      ``request_hash``                binds prompt text + model + params +
                                                      input order + endpoint identity
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
shares the same request identity; that is exactly what :func:`validate` checks.
"""

from __future__ import annotations

import json
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
}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - corrupt input
                raise ValueError(f"{path}:{line_number}: invalid JSON ({exc})") from exc
    return records


def load_workload(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate_pair_calls(
    pair_calls: list[Mapping[str, Any]],
    workload: Mapping[str, Any],
    *,
    expected_repetitions: int = 5,
) -> dict[str, Any]:
    """Validate raw pair response records against the data contract.

    Checks (each returns concrete violations, never silently coerces):

    C1  every record carries all contract fields;
    C2  one ``experiment_id``/``condition`` everywhere (no pooling across conditions);
    C3  ``(query_id, pair_position)`` joins 1:1 onto the frozen workload, and the record's
        ``request_hash`` equals the workload's pinned hash for that slot (per-call request
        identity, not just "same pair_id");
    C4  ``document_order`` matches the workload's candidate list for the query;
    C5  strict-parse status is ``valid`` and the verdict is a bool (raw answer retained);
    C6  completeness: queries × pairs × repetitions all present, nothing extra;
    C7  pool usability: for every (query, pair) slot the request-hash set is identical
        across repetitions — i.e. the 5 responses answer the *same request*;
    C8  ``lotus_parsed_decision`` agrees with ``strict_parsed_decision`` (both parsers
        recorded; divergence would flag parser ambiguity rather than model flip).
    """
    issues: list[str] = []
    query_ids = [str(q) for q in workload["query_ids"]]
    expected_pairs = int(workload["pairs_per_query"])
    expected_reps = int(expected_repetitions)
    workload_pairs: dict[str, dict[int, Mapping[str, Any]]] = {
        q: {int(p["pair_position"]): p for p in entry["pairs"]}
        for q, entry in zip(query_ids, workload["entries"])
    }

    # C1/C2/C3/C4/C5 per record
    experiment_ids: set[str] = set()
    for index, row in enumerate(pair_calls):
        where = f"row {index}"
        missing = [contract for contract, key in CONTRACT_REQUIRED_FIELDS.items() if row.get(key) is None]
        if missing:
            issues.append(f"C1 {where}: missing contract fields {missing}")
        experiment_ids.add(str(row.get("experiment_id")))
        query_id = str(row.get("query_id"))
        if query_id not in workload_pairs:
            issues.append(f"C3 {where}: unknown query_id {query_id!r}")
            continue
        slot = workload_pairs[query_id].get(row.get("pair_position"))
        if slot is None:
            issues.append(f"C3 {where}: pair_position {row.get('pair_position')!r} not in workload")
            continue
        if str(row.get("request_hash")) != str(slot["request_hash"]):
            issues.append(f"C3 {where}: request_hash mismatch vs workload")
        if str(row.get("pair_id")) != str(slot["pair_id"]):
            issues.append(f"C3 {where}: pair_id mismatch vs workload")
        if list(row.get("document_order") or []) != [str(d) for d in _entry_doc_ids(workload, query_id)]:
            issues.append(f"C4 {where}: document_order mismatch vs workload")
        if str(row.get("strict_parser_status")) != "valid" or not isinstance(row.get("strict_parsed_decision"), bool):
            issues.append(f"C5 {where}: not a strict-valid boolean verdict")
        if isinstance(row.get("strict_parsed_decision"), bool) and isinstance(row.get("lotus_parsed_decision"), bool):
            if row["strict_parsed_decision"] != row["lotus_parsed_decision"]:
                issues.append(f"C8 {where}: strict vs lotus parser disagree")
    if len(experiment_ids) > 1:
        issues.append(f"C2: multiple experiment_id values in one file: {sorted(experiment_ids)}")

    # C6/C7 completeness and pool usability
    by_slot: dict[tuple[str, int], dict[int, list[Mapping[str, Any]]]] = {}
    for row in pair_calls:
        key = (str(row.get("query_id")), int(row.get("pair_position", -1)))
        by_slot.setdefault(key, {}).setdefault(int(row.get("repetition_id", -1)), []).append(row)

    expected_slots = {(q, pos) for q in query_ids for pos in workload_pairs[q]}
    for key in sorted(expected_slots):
        reps = by_slot.get(key, {})
        query_id, position = key
        for rep in range(expected_reps):
            rows = reps.get(rep, [])
            if len(rows) != 1:
                issues.append(f"C6 {key} rep {rep}: expected 1 response, found {len(rows)}")
        extra_reps = set(reps) - set(range(expected_reps))
        if extra_reps:
            issues.append(f"C6 {key}: unexpected repetitions {sorted(extra_reps)}")
        hashes = {str(rows[0]["request_hash"]) for rows in reps.values() if rows}
        if len(hashes) > 1:
            issues.append(f"C7 {key}: request identity differs across repetitions: {sorted(hashes)}")
    for key in sorted(set(by_slot) - expected_slots):
        issues.append(f"C6 {key}: response outside the frozen workload")

    valid_rows = [row for row in pair_calls if str(row.get("strict_parser_status")) == "valid"]
    return {
        "records": len(pair_calls),
        "strict_valid_records": len(valid_rows),
        "queries": sorted(query_ids),
        "pairs_per_query": expected_pairs,
        "repetitions": expected_reps,
        "expected_records": len(query_ids) * expected_pairs * expected_reps,
        "issues": issues,
        "contract_ok": not issues,
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
        if require_valid and str(row["strict_parser_status"]) != "valid":
            continue
        query_id = str(row["query_id"])
        position = int(row["pair_position"])
        repetition = int(row["repetition_id"])
        entry = pool.setdefault(query_id, {"request_hashes": {}, "verdicts": {}, "raw_answers": {}})
        entry["request_hashes"][position] = str(row["request_hash"])
        entry["verdicts"].setdefault(position, {})[repetition] = bool(row["strict_parsed_decision"])
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
