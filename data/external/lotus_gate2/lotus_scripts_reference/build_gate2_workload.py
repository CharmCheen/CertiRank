#!/usr/bin/env python
"""Freeze the Gate 2 workload: 3 hash-selected queries x Top-8 retrieval candidates.

Reads only existing artifacts and the local SciFact distribution — no network, no model
calls. Writes ``artifacts/gate2_workload.json``:

* the query set (``sorted(sha256(query_id))[:3]`` over the Gate 1B pool) and the rule;
* per query: the Top-8 candidates in retrieval-rank order with a candidate-set hash,
  the SciFact qrels, and the 28 canonical pairs with their **expected canonical request
  hash** computed through the production prompt path (df2multimodal_info -> nle2str ->
  get_match_prompt_binary). Gate 1's request hashes are deliberately NOT reused as
  evidence (different prompt path, D14); every Gate 2 request is verified independently
  against the hashes frozen here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.protocol import (  # noqa: E402
    repo_commit,
    write_json,
)
from experiments.lotus_nondeterminism.gate23.protocol import (  # noqa: E402
    RequestIndex,
    p0_hashing_oracle,
)
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.workload import (  # noqa: E402
    build_workload,
    select_gate2_queries,
)
from experiments.lotus_nondeterminism.paperpath.scifact_beir import load_paper_scifact  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(P.WORKLOAD_PATH), help="output workload JSON")
    args = parser.parse_args()

    queries = select_gate2_queries()
    print(f"query rule      : {P.QUERY_RULE}")
    print(f"selected queries: {queries}")
    print(f"candidate rule  : {P.CANDIDATE_RULE}")
    print(f"design          : {P.QUERIES_SELECTED} queries x {P.CANDIDATE_DEPTH} candidates "
          f"-> {P.PAIRS_PER_QUERY} pairs/query, K={P.K}, {P.REPETITIONS} repetitions")
    print(f"planned calls   : {P.PLANNED_CALLS}")

    print("loading SciFact via the BEIR loader ...")
    data = load_paper_scifact()
    request_index = RequestIndex(p0_hashing_oracle())

    workload = build_workload(data=data, request_index=request_index)

    # self-checks before writing
    assert len(workload["entries"]) == P.QUERIES_SELECTED
    for entry in workload["entries"]:
        assert len(entry["doc_ids"]) == P.CANDIDATE_DEPTH
        assert len(entry["pairs"]) == P.PAIRS_PER_QUERY
        assert all("request_hash" in pair for pair in entry["pairs"])
        unique = {pair["request_hash"] for pair in entry["pairs"]}
        assert len(unique) == P.PAIRS_PER_QUERY, (
            f"query {entry['query_id']}: expected {P.PAIRS_PER_QUERY} distinct request hashes, "
            f"got {len(unique)}"
        )
        print(
            f"query {entry['query_id']}: depth={entry['depth']} "
            f"hash={entry['candidate_set_hash'][:16]} qrels={len(entry['qrels'])}"
        )

    write_json(args.out, workload)
    print(f"wrote {args.out}")
    print(f"repo commit: {repo_commit()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
