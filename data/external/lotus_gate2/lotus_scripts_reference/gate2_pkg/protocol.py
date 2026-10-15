"""Frozen constants, selection rules, and artifact paths for Gate 2 (propagation study).

Everything that defines the workload is fixed here, before any Gate 2 output exists:

* **Query rule** — ``sorted(sha256(query_id))[:3]`` over the 20 queries Gate 1B already
  used. A pure function of the query ids: it cannot be influenced by Gate 1 flip rates,
  retrieval quality, or any model output. It is also a strict prefix of the Gate 2/3
  ``[:8]`` selection, so the two substudies stay comparable.
* **Candidate rule** — retrieval ranks 0..7 (the Top-8) of the frozen Gate 1B retrieval
  (``gate1b_retrieval_candidates.jsonl``), in retrieval-rank order. The retrieval itself is
  never re-run, so the input distribution is the paper's, pinned.
* **Pair rule** — the naive sort enumerates ``(i, j)`` with ``i < j`` over the candidate
  list in its frozen initial order; ``pair_id`` reuses Gate 1's ``query|doc1|doc2`` format
  (row-order, i.e. the order the documents are actually presented to the model in).

The model condition is P0, unchanged from Gate 1: DeepSeek official API,
``deepseek-flash``, ``thinking={"type": "disabled"}``, ``max_tokens=128``, and every
sampling parameter absent (never sent). Single source: ``oracle/cli.py::CONDITIONS``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from ..gate1.protocol import (
    CONDITIONS_OVERRIDES,
    EXPERIMENTS_ROOT,
    P0_ORACLE_CONFIG,
    PLACEHOLDER_API_KEY,
    append_jsonl,
    file_sha256,
    read_jsonl,
    repo_commit,
    write_json,
    write_jsonl,
)
from ..gate23.protocol import RequestIndex, candidate_dict, candidate_set_hash, p0_hashing_oracle  # noqa: F401

__all__ = [
    "ARTIFACTS",
    "BUDGET_CAP",
    "CALLS_PATH",
    "CANDIDATE_DEPTH",
    "CANDIDATE_RULE",
    "CONDITION",
    "EXPERIMENT_ID",
    "GATE",
    "K",
    "METHOD",
    "PAIRS_PER_QUERY",
    "PAIR_ID_RULE",
    "PLANNED_CALLS",
    "QUERIES_SELECTED",
    "QUERY_RULE",
    "REPETITIONS",
    "RUNS_PATH",
    "SMOKE_CALLS_PATH",
    "SMOKE_RUNS_PATH",
    "SUBSISTUDY",
    "WORKLOAD_PATH",
    "ANALYSIS_PATH",
    "PROPAGATION_PATH",
    "BUDGET_PATH",
    "SMOKE_PATH",
    "VERIFICATION_PATH",
    "OPERATOR_ID",
    "GATE1B_RETRIEVAL_PATH",
    "GATE1B_PAIRSET_MANIFEST",
    "pair_id",
    "pair_positions",
    "run_id",
    "query_set_sha256",
    # re-exported helpers
    "RequestIndex",
    "candidate_dict",
    "candidate_set_hash",
    "p0_hashing_oracle",
    "CONDITIONS_OVERRIDES",
    "P0_ORACLE_CONFIG",
    "PLACEHOLDER_API_KEY",
    "EXPERIMENTS_ROOT",
    "append_jsonl",
    "file_sha256",
    "read_jsonl",
    "repo_commit",
    "write_json",
    "write_jsonl",
]

GATE = "G2"
SUBSISTUDY = "Gate2-NondeterminismPropagation"
CONDITION = "P0"
EXPERIMENT_ID = f"{SUBSISTUDY}|{CONDITION}"

#: the operator under study, recorded on every trace row
OPERATOR_ID = "lotus.sem_topk[method=naive]"

ARTIFACTS = EXPERIMENTS_ROOT / "artifacts"

# -- frozen design parameters -----------------------------------------------------------
#: Top-K size the paper path would return for a small candidate pool
K = 3
#: candidates per query (the naive sort compares all C(8,2) = 28 pairs)
CANDIDATE_DEPTH = 8
#: complete Top-K executions per query
REPETITIONS = 5
#: queries in the workload
QUERIES_SELECTED = 3
#: LOTUS sort method under study
METHOD = "naive"

PAIRS_PER_QUERY = CANDIDATE_DEPTH * (CANDIDATE_DEPTH - 1) // 2  # 28
PLANNED_CALLS = QUERIES_SELECTED * PAIRS_PER_QUERY * REPETITIONS  # 420

#: protocol-level cap for a full gate; the planned 420 calls sit well inside it
BUDGET_CAP = 3000

QUERY_RULE = (
    f"sorted(sha256(query_id))[:{QUERIES_SELECTED}] over the 20 queries used by Gate 1B "
    "(gate1b_pairset_manifest.json::selected_query_ids)"
)
CANDIDATE_RULE = (
    f"retrieval ranks 0..{CANDIDATE_DEPTH - 1} (Top-{CANDIDATE_DEPTH}) of the frozen Gate 1B retrieval "
    "(gate1b_retrieval_candidates.jsonl), initial order = retrieval-rank order"
)
PAIR_ID_RULE = 'f"{query_id}|{doc1_id}|{doc2_id}" in row order (i < j), the Gate 1 format'

# -- artifact paths ----------------------------------------------------------------------
WORKLOAD_PATH = ARTIFACTS / "gate2_workload.json"
CALLS_PATH = ARTIFACTS / "gate2_calls.jsonl"
RUNS_PATH = ARTIFACTS / "gate2_runs.jsonl"
ANALYSIS_PATH = ARTIFACTS / "gate2_analysis.json"
PROPAGATION_PATH = ARTIFACTS / "gate2_propagation_analysis.json"
BUDGET_PATH = ARTIFACTS / "gate2_budget_estimate.json"
SMOKE_PATH = ARTIFACTS / "gate2_offline_smoke.json"
SMOKE_CALLS_PATH = ARTIFACTS / "gate2_smoke_calls.jsonl"
SMOKE_RUNS_PATH = ARTIFACTS / "gate2_smoke_runs.jsonl"
VERIFICATION_PATH = ARTIFACTS / "gate2_artifact_verification.json"

#: Gate 1B artifacts Gate 2 reads (never writes)
GATE1B_RETRIEVAL_PATH = ARTIFACTS / "gate1b_retrieval_candidates.jsonl"
GATE1B_PAIRSET_MANIFEST = ARTIFACTS / "gate1b_pairset_manifest.json"

# -- identity helpers --------------------------------------------------------------------


def pair_positions(n: int = CANDIDATE_DEPTH) -> list[tuple[int, int]]:
    """The naive sort's pair enumeration: ``(i, j)`` with ``i < j``, lexicographic.

    Verified against ``lotus/sem_ops/sem_topk.py::llm_naive_sort``:
    ``for i in range(N): for j in range(i+1, N): pairs.append((docs[i], docs[j]))``.
    """
    return [(i, j) for i in range(n) for j in range(i + 1, n)]


def pair_id(query_id: str, doc1_id: str, doc2_id: str) -> str:
    """Canonical pair identity, reusing Gate 1's format (row order, not doc-id order)."""
    return f"{query_id}|{doc1_id}|{doc2_id}"


def run_id(query_id: str, repetition: int) -> str:
    return f"{GATE}-{query_id}-r{repetition}"


def query_set_sha256(query_ids: Sequence[str]) -> str:
    payload = json.dumps(list(query_ids), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
