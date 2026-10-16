"""Live pair-response acquisition against the OpenAI-compatible provider.

Builds C1-C8 pair_response_records and freezes request identity before any call.
Offline reproduction never imports this module's client.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable

from certirank.provider import ApiConfig, OpenAICompatibleClient

PROMPT_VERSION = "certirank.pair_compare.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_request_payload(
    *,
    experiment_id: str,
    condition: str,
    requested_model: str,
    model_parameters: dict[str, Any],
    query_id: str,
    query_text: str,
    document_order: list[str],
    document_texts: dict[str, str],
    position_i: int,
    position_j: int,
    document_1_id: str,
    document_2_id: str,
    pair_position: int,
    pair_id: str,
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, Any]:
    text_hashes = {doc: sha256(document_texts[doc].encode("utf-8")).hexdigest() for doc in document_order}
    return {
        "prompt_version": prompt_version,
        "experiment_id": experiment_id,
        "condition": condition,
        "requested_model": requested_model,
        "model_parameters": model_parameters,
        "query_id": query_id,
        "query_text": query_text,
        "document_order": list(document_order),
        "document_text_sha256": text_hashes,
        "position_i": position_i,
        "position_j": position_j,
        "document_1_id": document_1_id,
        "document_2_id": document_2_id,
        "pair_position": pair_position,
        "pair_id": pair_id,
    }


def request_hash(payload: dict[str, Any]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def all_pairs(n: int) -> list[tuple[int, int]]:
    return [(i, j) for i in range(n) for j in range(i + 1, n)]


def build_workload(
    queries: list[dict[str, Any]],
    *,
    experiment_id: str,
    condition: str,
    model: str,
    model_parameters: dict[str, Any],
    k: int,
) -> dict[str, Any]:
    """queries: [{query_id, query_text, doc_ids, doc_texts, qrels}]. Pair slots follow row order."""
    if not queries:
        raise ValueError("workload requires at least one query")
    entries = []
    query_ids = []
    for q in queries:
        qid = str(q["query_id"])
        if not qid or qid in query_ids:
            raise ValueError(f"query_id must be nonempty and unique: {qid!r}")
        query_ids.append(qid)
        docs = [str(d) for d in q["doc_ids"]]
        if len(docs) < 2 or len(set(docs)) != len(docs):
            raise ValueError(f"{qid}: doc_ids must be distinct with at least two entries")
        raw_texts = q.get("doc_texts")
        if not isinstance(raw_texts, dict) or any(d not in raw_texts or not str(raw_texts[d]).strip() for d in docs):
            raise ValueError(f"{qid}: doc_texts must provide nonempty text for every doc_id")
        document_texts = {d: str(raw_texts[d]) for d in docs}
        if not isinstance(q.get("qrels"), dict) or not q["qrels"]:
            raise ValueError(f"{qid}: independent qrels are required")
        text = str(q.get("query_text") or qid)
        pairs = []
        for pos, (i, j) in enumerate(all_pairs(len(docs))):
            pair_id = f"{qid}|{docs[i]}|{docs[j]}"
            payload = canonical_request_payload(
                experiment_id=experiment_id,
                condition=condition,
                requested_model=model,
                model_parameters=model_parameters,
                query_id=qid,
                query_text=text,
                document_order=docs,
                document_texts=document_texts,
                position_i=i,
                position_j=j,
                document_1_id=docs[i],
                document_2_id=docs[j],
                pair_position=pos,
                pair_id=pair_id,
            )
            pairs.append({
                "pair_position": pos,
                "position_i": i,
                "position_j": j,
                "document_1_id": docs[i],
                "document_2_id": docs[j],
                "pair_id": pair_id,
                "request_hash": request_hash(payload),
            })
        entries.append({
            "query_id": qid,
            "query_text": text,
            "doc_ids": docs,
            "doc_texts": document_texts,
            "qrels": {str(doc): int(score) for doc, score in q["qrels"].items()},
            "pairs": pairs,
        })
    pairs_per_query = len(entries[0]["pairs"])
    return {
        "schema_version": 1,
        "prompt_version": PROMPT_VERSION,
        "condition": condition,
        "experiment_id": experiment_id,
        "requested_model": model,
        "model_parameters": model_parameters,
        "query_ids": query_ids,
        "k": k,
        "pairs_per_query": pairs_per_query,
        "entries": entries,
    }


def identity_from_config(config: ApiConfig) -> dict[str, Any]:
    return {
        "experiment_id": config.experiment_id,
        "condition": config.condition,
        "requested_model": config.model,
        "model_parameters": config.model_parameters(),
    }


def _parse_verdict(raw: str) -> tuple[bool | None, str]:
    from certirank.pair_pool import parse_comparison_answer
    return parse_comparison_answer(raw)


def collect_pair_calls(
    workload: dict[str, Any],
    *,
    client: Any,
    repetitions: int,
    run_prefix: str = "live",
    limit_pairs: int | None = None,
    sleep_seconds: float = 0.0,
    logger: Callable[[str], None] | None = None,
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Call each workload pair `repetitions` times and return contract records.

    ``on_row`` is invoked after each successful call so callers can checkpoint.
    """
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    identity = {
        "experiment_id": workload["experiment_id"],
        "condition": workload["condition"],
        "requested_model": workload["requested_model"],
        "model_parameters": workload["model_parameters"],
    }
    rows: list[dict[str, Any]] = []
    pair_budget = limit_pairs
    for entry in workload["entries"]:
        qid = entry["query_id"]
        docs = entry["doc_ids"]
        texts = entry.get("doc_texts") or {d: d for d in docs}
        for pair in entry["pairs"]:
            if pair_budget is not None:
                if pair_budget <= 0:
                    return rows
                pair_budget -= 1
            for rep in range(repetitions):
                started = time.perf_counter()
                timestamp = utc_now_iso()
                result = client.compare_pair(
                    entry["query_text"],
                    texts[pair["document_1_id"]],
                    texts[pair["document_2_id"]],
                )
                latency = time.perf_counter() - started
                raw = result["raw_response_content"]
                parsed, status = result.get("strict_parsed_decision"), result.get("strict_parser_status")
                if status is None or parsed is None:
                    parsed, status = _parse_verdict(raw)
                usage = result.get("usage") or {}
                record = {
                    **identity,
                    "prompt_version": workload.get("prompt_version", PROMPT_VERSION),
                    "query_id": qid,
                    "pair_position": pair["pair_position"],
                    "position_i": pair["position_i"],
                    "position_j": pair["position_j"],
                    "document_1_id": pair["document_1_id"],
                    "document_2_id": pair["document_2_id"],
                    "pair_id": pair["pair_id"],
                    "request_hash": pair["request_hash"],
                    "document_order": list(docs),
                    "run_id": f"{run_prefix}-{qid}-r{rep}",
                    "repetition_id": rep,
                    "provider_request_id": result.get("provider_request_id") or f"{run_prefix}-{qid}-{pair['pair_position']}-{rep}",
                    "timestamp_utc": timestamp,
                    "raw_response_content": raw,
                    "strict_parsed_decision": parsed,
                    "strict_parser_status": status,
                    "lotus_parsed_decision": parsed,
                    "parse_status": status,
                    "returned_model": result.get("model") or identity["requested_model"],
                    "api_latency_seconds": latency,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_prompt_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "transport_attempts": 1,
                    "transport_retries": 0,
                    "source": "fresh",
                }
                if status != "valid" or not isinstance(parsed, bool):
                    raise ValueError(f"unparseable live response for {qid} pair {pair['pair_position']} rep {rep}: {raw!r}")
                rows.append(record)
                if on_row is not None:
                    on_row(record)
                if logger:
                    logger(f"{qid} pair {pair['pair_position']} rep {rep}: {raw!r} -> {parsed}")
                if sleep_seconds:
                    time.sleep(sleep_seconds)
    return rows


def write_acquisition(
    out_dir: str | Path,
    *,
    workload: dict[str, Any],
    rows: list[dict[str, Any]],
    identity: dict[str, Any],
    config: ApiConfig | None = None,
) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "workload": out / "workload.json",
        "pair_calls": out / "pair_calls.jsonl",
        "identity": out / "identity.json",
        "acquisition_meta.json": out / "acquisition_meta.json",
    }
    paths["workload"].write_text(json.dumps(workload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["pair_calls"].write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    paths["identity"].write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta = {
        "collected_at_utc": utc_now_iso(),
        "records": len(rows),
        "prompt_version": workload.get("prompt_version", PROMPT_VERSION),
        "requested_model": identity.get("requested_model"),
        "temperature_requested": (identity.get("model_parameters") or {}).get("temperature_requested"),
        "thinking_mode_requested": (identity.get("model_parameters") or {}).get("thinking_mode_requested"),
        "reasoning_effort_requested": (identity.get("model_parameters") or {}).get("reasoning_effort_requested"),
        "new_physical_calls": len(rows),
    }
    if config is not None:
        meta["api_config"] = {
            "base_url": config.base_url,
            "model": config.model,
            "temperature": config.temperature,
            "thinking_mode": config.thinking_mode,
            "reasoning_effort": config.reasoning_effort,
            "max_tokens": config.max_tokens,
        }
    paths["acquisition_meta.json"].write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


def build_manifest(
    *,
    dataset_id: str,
    source_kind: str,
    query_groups: list[dict[str, Any]],
    pools: dict[str, dict[str, Path]],
    label_source: str = "user-supplied independent labels",
) -> dict[str, Any]:
    """pools: split -> {workload, pair_calls, identity} paths."""
    pool_spec = {}
    for split, files in pools.items():
        hashes = {}
        names = {}
        for name in ("workload", "pair_calls", "identity"):
            path = Path(files[name])
            names[name] = path.name
            hashes[name] = sha256(path.read_bytes()).hexdigest()
        pool_spec[split] = {**names, "sha256": hashes}
    normalized = []
    for row in query_groups:
        normalized.append({
            "query_id": str(row["query_id"]),
            "query_group": str(row.get("query_group") or row["query_id"]),
            "split": row["split"],
            "label_source": row.get("label_source") or label_source,
        })
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "source_kind": source_kind,
        "query_groups": normalized,
        "pools": pool_spec,
        "acquisition": {
            "response_slots": "repetition count must match config formal.response_count",
            "request_order": "collection order is logged; time independence must be checked separately",
            "time_independence": "disjoint slots alone do not prove independence",
        },
    }
