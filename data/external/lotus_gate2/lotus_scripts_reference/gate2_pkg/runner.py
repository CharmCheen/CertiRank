"""Execute one complete naive ``sem_topk`` run and trace everything it touches.

The call is LOTUS's own:

```python
df.sem_topk(instruction, K=3, method="naive", return_stats=True)
```

with one substitution made *around* it, never inside it: ``sem_topk.compare_batch_binary``
is rebound to the Gate 2/3 ``ComparatorHarness`` for the duration of the run. The naive
sort itself — pair enumeration, vote counting, the stable-sort tie-break, the Top-K
truncation — is untouched production code, and ``lotus/`` is never modified.

Two transports are possible:

* **live** — an :class:`OracleClient` (DeepSeek P0). Each of the 28 comparisons is one
  physical request; the oracle adapter forces every cache layer off.
* **scripted** — :class:`ScriptedOracle`, a local in-memory oracle that answers from a
  request-hash-keyed script. Used by the offline smoke and the tests: the full harness
  path (adapter, records, parsing, tracing) runs with zero API calls.

Every run emits two things: 28 **trace records** (one per comparison, §4.3 schema) and
one **run record** (the final query result, §4.4 schema). The runner also runs the
replay correctness gate itself — the offline reconstruction from the 28 verdicts must
reproduce the Top-K LOTUS returned — and records the verdict, so a run whose trace
cannot be replayed is flagged in the artifact, never silently accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pandas as pd

import lotus

from ..gate1.metrics import normalise_status
from ..gate1.pricing import cost_scenarios
from ..gate23.comparator import (
    FRESH_MODE,
    ComparatorHarness,
    install_comparator,
)
from ..gate23.runner import ForbiddenOracle
from ..oracle.client import OracleClient, OracleResponse, utc_now_iso
from ..strict_parser import parse_strict
from .protocol import (
    CANDIDATE_DEPTH,
    CONDITION,
    EXPERIMENT_ID,
    GATE,
    K,
    METHOD,
    OPERATOR_ID,
    PAIRS_PER_QUERY,
    SUBSISTUDY,
    pair_id,
    run_id,
)
from .replay import naive_replay, replay_gate
from .workload import production_inputs

__all__ = ["ScriptedOracle", "Gate2RunOutcome", "execute_run"]


class ScriptedOracle(OracleClient):
    """A local, in-memory oracle: the full harness path with zero API calls.

    Answers are keyed by canonical request hash. Two knobs:

    * ``responses`` — an explicit ``request_hash -> content`` script (e.g. to inject
      unparseable answers and test invalid-response accounting);
    * the deterministic fallback — hash parity, so the same pair always gets the same
      answer and the same answer everywhere it appears.

    The request hash is *not* recomputed here. It is obtained from a real
    :class:`OpenAICompatibleOracle` via ``audit_request``, i.e. the same code path a live
    call uses. Building the semantics by hand (even from the same config) silently
    produced different hashes in early development, because the authoritative thinking
    plan is ``build_thinking_control(config).as_dict()`` rather than just its style.
    Delegating removes that class of error by construction.
    """

    def __init__(self, config: Any, *, responses: Mapping[str, str] | None = None) -> None:
        super().__init__(config)
        self.responses = dict(responses or {})
        self.calls: list[dict[str, Any]] = []
        from ..oracle.openai_compatible import OpenAICompatibleOracle

        self._hasher = OpenAICompatibleOracle(config)

    def complete(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> OracleResponse:
        audit = self._hasher.audit_request(list(messages))
        rhash = audit["request_hash"]
        semantics = audit["request_semantics"]
        self.calls.append({"request_hash": rhash, "messages": list(messages), "request_id": request_id})
        content = self.responses.get(rhash)
        if content is None:
            content = "Document 1" if int(rhash[-1], 16) % 2 == 0 else "Document 2"
        return OracleResponse(
            content=content,
            reasoning_content=None,
            requested_model=self._config.model or "",
            returned_model=self._config.model,
            provider=self._config.provider,
            provider_request_id=f"scripted_{len(self.calls)}",
            prompt_tokens=10,
            completion_tokens=3,
            cached_prompt_tokens=0,
            finish_reason="stop",
            request_semantics=semantics,
            response_metadata={"thinking_control_supported": True, "cache_enabled": False},
            request_hash=rhash,
            thinking_mode_requested=self._config.thinking_mode,
            thinking_control_supported=True,
            reasoning_effort_requested=self._config.reasoning_effort,
            temperature_requested=self._config.temperature,
            top_p_requested=self._config.top_p,
            presence_penalty_requested=self._config.presence_penalty,
            frequency_penalty_requested=self._config.frequency_penalty,
            max_tokens_requested=self._config.max_tokens,
            tools_present=False,
            transport_attempts=1,
            retries=0,
            latency_seconds=0.01,
            timestamp_utc=utc_now_iso(),
            request_id=request_id,
        )


@dataclass
class Gate2RunOutcome:
    """One complete Top-K execution: the run record, its trace, and its replay gate."""

    run_record: dict[str, Any]
    trace_records: list[dict[str, Any]]
    verdicts: list[bool] = field(default_factory=list)


def _trace_record(
    *,
    observation: Any,
    record: Any,
    query_id: str,
    repetition: int,
    pair_position: int,
    position_i: int,
    position_j: int,
    doc_ids: Sequence[str],
) -> dict[str, Any]:
    """One comparator call in the §4.3 schema, joining harness observation + oracle record."""
    strict = parse_strict(observation.content)
    strict_status = normalise_status(strict.status)
    lotus_bool = observation.original_parser_bool
    if strict_status == "valid":
        parse_status = "valid"
    elif lotus_bool is None:
        parse_status = "unresolved"
    else:
        parse_status = f"invalid_lotus_default_{str(lotus_bool).lower()}"

    model_parameters = {
        "thinking_mode_requested": getattr(record, "thinking_mode_requested", None),
        "thinking_control_supported": bool(getattr(record, "thinking_control_supported", False)),
        "reasoning_effort_requested": getattr(record, "reasoning_effort_requested", None),
        "temperature_requested": getattr(record, "temperature_requested", None),
        "top_p_requested": getattr(record, "top_p_requested", None),
        "presence_penalty_requested": getattr(record, "presence_penalty_requested", None),
        "frequency_penalty_requested": getattr(record, "frequency_penalty_requested", None),
        "max_tokens_requested": getattr(record, "max_tokens_requested", None),
        "tools_present": bool(getattr(record, "tools_present", False)),
        "provider": getattr(record, "provider", None),
    }

    return {
        "experiment_id": EXPERIMENT_ID,
        "gate": GATE,
        "substudy": SUBSISTUDY,
        "condition": CONDITION,
        "run_id": run_id(query_id, repetition),
        "query_id": str(query_id),
        "repetition_id": repetition,
        "operator_id": OPERATOR_ID,
        "pair_id": pair_id(str(query_id), str(observation.doc1_id), str(observation.doc2_id)),
        "pair_position": pair_position,
        "position_i": position_i,
        "position_j": position_j,
        "document_1_id": str(observation.doc1_id),
        "document_2_id": str(observation.doc2_id),
        "request_hash": observation.request_hash,
        "requested_model": getattr(record, "requested_model", None),
        "returned_model": observation.returned_model,
        "model_parameters": model_parameters,
        "raw_response_content": observation.content,
        "reasoning_content": observation.reasoning_content,
        "strict_parsed_decision": strict.verdict if strict_status == "valid" else None,
        "strict_parser_status": strict_status,
        "lotus_parsed_decision": lotus_bool,
        "parse_status": parse_status,
        "provider_request_id": observation.provider_request_id,
        "prompt_tokens": observation.prompt_tokens,
        "completion_tokens": observation.completion_tokens,
        "cached_prompt_tokens": observation.cached_prompt_tokens,
        "cache_hit": False,
        "cache_note": "lotus.settings.enable_cache=False (model + operator cache off); "
        "cached_prompt_tokens reports provider-side prompt caching only",
        "api_latency_seconds": observation.latency_seconds,
        "transport_attempts": int(observation.transport_attempts or 1),
        "transport_retries": max(0, int(observation.transport_attempts or 1) - 1),
        "timestamp_utc": observation.timestamp_utc,
        "source": observation.source,
        "document_order": list(doc_ids),
    }


def execute_run(
    *,
    query_id: str,
    repetition: int,
    candidates: Mapping[str, Any],
    data: Any,
    request_index: Any,
    oracle: OracleClient | None = None,
    comparator_mode: str | None = None,
    harness_factory: Any = None,
) -> Gate2RunOutcome:
    """One complete naive Top-K execution. Never writes to disk; returns the outcome.

    ``comparator_mode`` defaults to FRESH. Pass ``STUB_MODE`` with a harness whose
    ``stub_verdict`` is set for the fully-deterministic offline path (no oracle contact).
    """
    from ..gate23.protocol import candidate_set_hash

    stored_hash = candidates.get("candidate_set_hash")
    recomputed_hash = candidate_set_hash(candidates)
    if stored_hash and stored_hash != recomputed_hash:
        raise ValueError(
            f"candidate set for query {query_id} does not match its frozen hash "
            f"(stored {str(stored_hash)[:16]}, recomputed {recomputed_hash[:16]})"
        )

    inputs = production_inputs(candidates, data)
    doc_ids: list[str] = inputs["doc_ids"]
    text_to_doc = inputs["text_to_doc"]
    instruction = inputs["instruction"]
    dataframe: pd.DataFrame = inputs["dataframe"]

    mode = comparator_mode or FRESH_MODE
    live = mode == FRESH_MODE
    if live and oracle is None:
        raise ValueError("a live run needs an oracle")
    run_tag = run_id(query_id, repetition)

    from ..oracle.lotus_adapter import build_oracle_lm
    from ..gate23.protocol import p0_hashing_oracle

    if live:
        lm = build_oracle_lm(oracle, run_id=run_tag)
    else:
        lm = build_oracle_lm(ForbiddenOracle(p0_hashing_oracle().config), run_id=run_tag)

    factory = harness_factory or ComparatorHarness
    harness = factory(
        mode=mode,
        request_index=request_index,
        frozen_map=None,
        oracle=oracle,
        lm=lm,
        doc_ids=text_to_doc,
        strategy=None,
    )

    position_of: dict[str, int] = {doc: pos for pos, doc in enumerate(doc_ids)}
    started = time.time()
    status, error = "ok", None
    lotus_topk: list[str] = []
    stats: dict[str, Any] = {}
    previous_lm = lotus.settings.lm
    previous_cache = lotus.settings.enable_cache
    try:
        lotus.settings.lm = lm
        lotus.settings.enable_cache = False
        with install_comparator(harness):
            result_df, stats = dataframe.sem_topk(
                instruction, K=K, method=METHOD, return_stats=True
            )
        lotus_topk = [str(v) for v in result_df["pid"].tolist()][:K]
    except Exception as exc:  # noqa: BLE001 - recorded as a run-level failure, never swallowed
        status, error = "error", f"{type(exc).__name__}: {exc}"[:500]
    finally:
        lotus.settings.lm = previous_lm
        lotus.settings.enable_cache = previous_cache
    runtime = time.time() - started

    # -- verdict vector in naive-enumeration order ---------------------------------------
    verdicts = [bool(v) for v in harness.verdicts]
    comparisons_ok = len(verdicts) == PAIRS_PER_QUERY

    # -- replay correctness gate: offline reconstruction must equal LOTUS's answer -------
    replay_result: dict[str, Any] = {}
    replay_consistent: bool | None = None
    full_ranking: list[str] = []
    win_counts: dict[str, int] = {}
    if comparisons_ok:
        replay_result = naive_replay(verdicts, len(doc_ids))
        full_ranking = [doc_ids[i] for i in replay_result["indexes"]]
        win_counts = {doc_ids[pos]: int(count) for pos, count in enumerate(replay_result["win_counts"])}
        lotus_positions = [position_of[d] for d in lotus_topk] if lotus_topk else []
        gate_result = replay_gate(replay_result["indexes"], lotus_positions, K)
        replay_consistent = bool(gate_result["consistent"])
        if not replay_consistent and status == "ok":
            status, error = "replay_mismatch", (
                f"offline replay produced {[gate_result['replay_topk_positions']]} but LOTUS "
                f"returned {lotus_positions}"
            )[:500]
    elif status == "ok":
        status, error = "comparison_count_mismatch", (
            f"expected {PAIRS_PER_QUERY} comparisons, observed {len(verdicts)}"
        )[:500]

    # -- trace records: harness observation joined with the oracle call record -----------
    records = list(getattr(lm, "records", []) or [])
    trace_records: list[dict[str, Any]] = []
    for pair_position, observation in enumerate(harness.observations):
        record = records[pair_position] if pair_position < len(records) else None
        i, j = _positions_for_pair(pair_position)
        trace_records.append(
            _trace_record(
                observation=observation,
                record=record,
                query_id=str(query_id),
                repetition=repetition,
                pair_position=pair_position,
                position_i=i,
                position_j=j,
                doc_ids=doc_ids,
            )
        )

    # -- accounting -----------------------------------------------------------------------
    prompt_tokens = sum(int(r.prompt_tokens or 0) for r in records)
    completion_tokens = sum(int(r.completion_tokens or 0) for r in records)
    cached_prompt_tokens = sum(int(r.cached_prompt_tokens or 0) for r in records)
    physical_calls = harness.physical_calls or sum(int(r.transport_attempts or 1) for r in records)
    transport_retries = sum(max(0, int(r.transport_attempts or 1) - 1) for r in records)
    invalid = harness.invalid_count
    valid = len(verdicts) - invalid

    returned_models: dict[str, int] = {}
    for record in records:
        key = str(getattr(record, "returned_model", None))
        returned_models[key] = returned_models.get(key, 0) + 1

    cost = cost_scenarios(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cached_input_tokens=cached_prompt_tokens,
    )

    strict_status_counts: dict[str, int] = {}
    for row in trace_records:
        key = str(row["strict_parser_status"])
        strict_status_counts[key] = strict_status_counts.get(key, 0) + 1

    run_record = {
        "run_id": run_tag,
        "experiment_id": EXPERIMENT_ID,
        "gate": GATE,
        "substudy": SUBSISTUDY,
        "condition": CONDITION,
        "query_id": str(query_id),
        "repetition": repetition,
        "method": METHOD,
        "k": K,
        "candidate_depth": CANDIDATE_DEPTH,
        "candidate_doc_ids": doc_ids,
        "candidate_set_hash": recomputed_hash,
        "lotus_ordered_topk_doc_ids": lotus_topk,
        "replay_full_ranking_doc_ids": full_ranking,
        "replay_win_counts": win_counts,
        "comparison_count": len(verdicts),
        "valid_comparisons": valid,
        "invalid_comparisons": invalid,
        "strict_status_counts": strict_status_counts,
        "request_hashes": list(harness.request_hashes),
        "replay_consistent_with_lotus": replay_consistent,
        "execution_status": status,
        "error": error,
        "physical_api_calls": physical_calls,
        "transport_retries": transport_retries,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "total_cost_usd": cost,
        "execution_time_seconds": round(runtime, 6),
        "llm_returned_models": returned_models,
        "reasoning_content_present": sum(1 for r in records if r.reasoning_content),
        "comparator_mode": mode,
        "lotus_stats": {
            "total_llm_calls": stats.get("total_llm_calls"),
            "total_tokens": stats.get("total_tokens"),
        },
        "timestamp_utc": utc_now_iso(),
    }

    return Gate2RunOutcome(run_record=run_record, trace_records=trace_records, verdicts=verdicts)


def _positions_for_pair(pair_position: int, n: int = CANDIDATE_DEPTH) -> tuple[int, int]:
    idx = 0
    for i in range(n):
        for j in range(i + 1, n):
            if idx == pair_position:
                return i, j
            idx += 1
    raise ValueError(f"pair position {pair_position} out of range for {n} candidates")
