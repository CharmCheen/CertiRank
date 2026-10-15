"""Build the frozen Gate 2 workload: 3 hash-selected queries x Top-8 retrieval candidates.

Everything is derived offline from artifacts that already exist:

* the 20-query Gate 1B pool (``gate1b_pairset_manifest.json``);
* the frozen Gate 1B retrieval (``gate1b_retrieval_candidates.jsonl``) — the retrieval
  itself is never re-run;
* the SciFact corpus for passage text (the same BEIR loader the paper path uses).

The workload file records, for every one of the 3 x 28 pairs, the **expected canonical
request hash** computed through the *production* prompt path (``df2multimodal_info`` ->
``nle2str`` -> ``get_match_prompt_binary``). This is what the live run is checked against:
Gate 1's request hashes are never reused as evidence (they were built through a different
prompt path — see D14), and every Gate 2 call is verified independently.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from lotus.nl_expression import nle2str

from ..gate23.queries import load_gate1b_query_pool
from ..paperpath.scifact_beir import SciFactPaperData, sort_instruction
from .protocol import (
    CANDIDATE_DEPTH,
    CANDIDATE_RULE,
    K,
    METHOD,
    PAIRS_PER_QUERY,
    PAIR_ID_RULE,
    PLANNED_CALLS,
    QUERIES_SELECTED,
    QUERY_RULE,
    REPETITIONS,
    GATE1B_RETRIEVAL_PATH,
    pair_id,
    pair_positions,
    query_set_sha256,
    repo_commit,
)

__all__ = [
    "select_gate2_queries",
    "production_inputs",
    "build_workload",
    "load_workload",
    "workload_pairs",
]


def select_gate2_queries(*, pool: Sequence[str] | None = None) -> list[str]:
    """The frozen Gate 2 query set: the 3 hash-smallest ids of the Gate 1B pool.

    Same rule family as Gate 2/3's ``[:8]``, so this set is a strict prefix of that one.
    A pure function of the query ids — no flip counts, no retrieval scores, no model
    output can influence it.
    """
    queries = list(pool) if pool is not None else load_gate1b_query_pool()
    ranked = sorted(queries, key=lambda qid: hashlib.sha256(qid.encode("utf-8")).hexdigest())
    if len(ranked) < QUERIES_SELECTED:
        raise ValueError(f"only {len(ranked)} eligible queries, need {QUERIES_SELECTED}")
    return ranked[:QUERIES_SELECTED]


def production_inputs(candidates: Mapping[str, Any], data: SciFactPaperData) -> dict[str, Any]:
    """Build exactly what ``df.sem_topk`` builds, without executing the operator.

    Mirrors ``SemTopKDataframe.__call__``: the DataFrame (pid, passage with the
    benchmark's ``"passage: "`` prefix), ``df2multimodal_info`` over the parsed column,
    and the instruction in **both** forms it takes on the way to the prompt:

    * ``instruction`` — the user-facing NL expression, ``"{passage}"`` intact. This is
      what is handed to ``df.sem_topk``, exactly as a LOTUS caller would;
    * ``formatted_instruction`` — what the comparator actually puts in the prompt, after
      LOTUS's own ``nle2str(user_instruction, col_li)`` step (``sem_topk.py:792``)
      rewrites ``"{passage}"`` to ``"Passage"``.

    The distinction is load-bearing: building the expected request hash from the *raw*
    instruction yields hashes that differ from every real call, and — worse — look
    plausible, because only one word changes. The comparator sees the *formatted* column
    text (``[Passage]: «...»``), so the text -> doc-id map uses the same formatter.
    """
    from lotus.templates.task_instructions import df2multimodal_info

    doc_ids = [str(d) for d in candidates["doc_ids"]]
    from ..paperpath.scifact_beir import PASSAGE_PREFIX

    passages = [PASSAGE_PREFIX + data.corpus_text[d] for d in doc_ids]
    dataframe = pd.DataFrame({"pid": doc_ids, "passage": passages}).rename_axis(None)
    multimodal = df2multimodal_info(dataframe, ["passage"])
    text_to_doc = {str(m["text"]): doc for m, doc in zip(multimodal, doc_ids)}
    instruction = sort_instruction(data.queries[str(candidates["query_id"])])
    formatted_instruction = nle2str(instruction, ["passage"])
    return {
        "dataframe": dataframe,
        "multimodal": multimodal,
        "doc_ids": doc_ids,
        "text_to_doc": text_to_doc,
        "instruction": instruction,
        "formatted_instruction": formatted_instruction,
    }


def workload_pairs(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The 28 canonical pairs of one query's candidate list, in naive-enumeration order."""
    doc_ids = [str(d) for d in entry["doc_ids"]]
    query_id = str(entry["query_id"])
    pairs = []
    for position, (i, j) in enumerate(pair_positions(len(doc_ids))):
        pairs.append(
            {
                "pair_position": position,
                "position_i": i,
                "position_j": j,
                "document_1_id": doc_ids[i],
                "document_2_id": doc_ids[j],
                "pair_id": pair_id(query_id, doc_ids[i], doc_ids[j]),
            }
        )
    return pairs


def build_workload(
    *,
    data: SciFactPaperData,
    request_index: Any,
    retrieval_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the frozen workload record (never writes to disk; the script does that)."""
    from ..gate23.protocol import candidate_dict, candidate_set_hash

    if retrieval_rows is None:
        from .protocol import read_jsonl

        retrieval_rows = read_jsonl(GATE1B_RETRIEVAL_PATH)

    query_ids = select_gate2_queries()
    # one real LM instance over the offline hashing oracle; `get_match_prompt_binary` only
    # touches it for ZS_COT (never here), but a genuine LM keeps the call signature honest
    from ..oracle.lotus_adapter import build_oracle_lm
    from ..gate23.protocol import p0_hashing_oracle

    hashing_lm = build_oracle_lm(p0_hashing_oracle(), run_id="G2-workload-build")
    entries: list[dict[str, Any]] = []
    for query_id in query_ids:
        entry = candidate_dict(query_id, retrieval_rows, depth=CANDIDATE_DEPTH)
        entry["candidate_set_hash"] = candidate_set_hash(entry)
        entry["query_text"] = data.queries[str(query_id)]
        entry["query_text_sha256"] = hashlib.sha256(entry["query_text"].encode("utf-8")).hexdigest()
        entry["qrels"] = {str(d): int(s) for d, s in data.qrels.get(str(query_id), {}).items()}

        inputs = production_inputs(entry, data)
        pairs = workload_pairs(entry)
        hashes: list[str] = []
        for pair in pairs:
            doc1 = inputs["multimodal"][pair["position_i"]]
            doc2 = inputs["multimodal"][pair["position_j"]]
            messages = _build_pair_messages(doc1, doc2, inputs["formatted_instruction"], hashing_lm)
            hashes.append(request_index.hash_of(messages))
        for pair, request_hash in zip(pairs, hashes):
            pair["request_hash"] = request_hash
        entry["pairs"] = pairs
        entry["pair_request_hash_set_sha256"] = hashlib.sha256(
            "|".join(hashes).encode("utf-8")
        ).hexdigest()
        entries.append(entry)

    return {
        "schema_version": 1,
        "gate": "G2",
        "substudy": "Gate2-NondeterminismPropagation",
        "condition": "P0",
        "repo_commit": repo_commit(),
        "query_selection_rule": QUERY_RULE,
        "candidate_rule": CANDIDATE_RULE,
        "pair_id_rule": PAIR_ID_RULE,
        "query_ids": query_ids,
        "query_set_sha256": query_set_sha256(query_ids),
        "k": K,
        "method": METHOD,
        "candidate_depth": CANDIDATE_DEPTH,
        "pairs_per_query": PAIRS_PER_QUERY,
        "repetitions": REPETITIONS,
        "planned_calls": PLANNED_CALLS,
        "retrieval_source": str(GATE1B_RETRIEVAL_PATH.name),
        "entries": entries,
    }


def _build_pair_messages(
    doc1: Mapping[str, Any], doc2: Mapping[str, Any], instruction: str, model: Any
) -> list[dict[str, Any]]:
    """The production prompt for one pair — the same function the comparator will call."""
    from lotus.sem_ops.sem_topk import get_match_prompt_binary

    return get_match_prompt_binary(dict(doc1), dict(doc2), instruction, strategy=None, model=model)


def load_workload(path: str | Path | None = None) -> dict[str, Any]:
    """Read the frozen workload file."""
    import json

    from .protocol import WORKLOAD_PATH

    target = Path(path) if path is not None else WORKLOAD_PATH
    return json.loads(target.read_text(encoding="utf-8"))
