#!/usr/bin/env python3
"""Gate 3 Phase 1: freeze real NFCorpus (BEIR) query groups for CertiRank.

No LLM calls. Deterministic BM25 (stdlib) + pre-registered selection rules.
Dev split -> our dev; Test split -> our heldout. Never mix.

Outputs under data/gate3_nfcorpus/:
  dataset_manifest.json, dev_queries.json, heldout_queries.json,
  selection_audit.json, dataset_report.md, bm25_top8.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "data" / "nfcorpus" / "nfcorpus"
OUT = ROOT / "data" / "gate3_nfcorpus"

# Pre-registered BM25 (classic) — do not tune after seeing LLM behavior.
K1 = 1.2
B = 0.75
TOP_K = 8

# Pre-registered eligibility (applied before any LLM call).
MIN_QREL_DOCS_IN_CANDIDATES = 2
MIN_PERFECT_IN_CANDIDATES = 1  # score >= 2
NDCG_BAND = (0.20, 0.95)  # not trivial, not broken
MAX_QUERY_TOKEN_JACCARD = 0.80  # near-duplicate guard within selected set
HISTORICAL_EXPOSED = {"636", "327", "51"}  # beir/scifact Gate 2; no NFCorpus overlap expected

N_DEV = 6
N_HELDOUT = 12


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    with open(path, encoding="utf-8") as handle:
        header = handle.readline()
        for line in handle:
            q, d, s = line.rstrip("\n").split("\t")
            out[q][d] = int(s)
    return dict(out)


class BM25:
    def __init__(self, docs: dict[str, str]):
        self.doc_ids = list(docs)
        self.doc_len = {}
        self.tf = {}
        df = Counter()
        for doc_id in docs:
            tokens = tokenize(docs[doc_id])
            self.doc_len[doc_id] = len(tokens)
            counts = Counter(tokens)
            self.tf[doc_id] = counts
            df.update(counts.keys())
        self.n = len(self.doc_ids)
        self.avgdl = (sum(self.doc_len.values()) / self.n) if self.n else 0.0
        self.idf = {
            term: math.log(1 + (self.n - d + 0.5) / (d + 0.5))
            for term, d in df.items()
        }

    def score(self, query: str, doc_id: str) -> float:
        tokens = tokenize(query)
        if not tokens or self.doc_len.get(doc_id, 0) == 0:
            return 0.0
        tf = self.tf[doc_id]
        dl = self.doc_len[doc_id]
        s = 0.0
        for term in tokens:
            if term not in tf:
                continue
            freq = tf[term]
            s += self.idf[term] * (freq * (self.k1 + 1)) / (
                freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            )
        return s

    k1 = K1
    b = B

    def top(self, query: str, k: int = TOP_K) -> list[tuple[str, float]]:
        scored = [(doc_id, self.score(query, doc_id)) for doc_id in self.doc_ids]
        scored.sort(key=lambda x: (-x[1], x[0]))  # deterministic tie-break
        return scored[:k]


def dcg_at_k(gains: list[float], k: int) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(rank_grades: list[int], k: int = 3) -> float:
    gains = [float(g) for g in rank_grades]
    dcg = dcg_at_k(gains, k)
    ideal = sorted(gains, reverse=True)
    idcg = dcg_at_k(ideal, k)
    return (dcg / idcg) if idcg > 0 else 0.0


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def build_query_record(query_id: str, query_text: str, docs: dict[str, str],
                       top: list[tuple[str, float]], qrels: dict[str, int]) -> dict:
    doc_ids = [doc_id for doc_id, _ in top]
    doc_texts = {doc_id: docs[doc_id] for doc_id in doc_ids}
    # BEIR convention: unlabeled docs count as 0 for metrics; recorded as unlabeled here.
    graded = {doc_id: int(qrels.get(doc_id, 0)) for doc_id in doc_ids}
    labeled = {doc_id: score for doc_id, score in graded.items() if score > 0}
    rank_grades = [graded[d] for d in doc_ids]
    return {
        "query_id": query_id,
        "query_text": query_text,
        "doc_ids": doc_ids,
        "doc_texts": doc_texts,
        "qrels": labeled,  # only positive labels; missing = unlabeled (eval-as-zero by convention)
        "qrels_all_candidates": graded,
        "retrieval_scores": {doc_id: score for doc_id, score in top},
        "bm25_ndcg3": ndcg_at_k(rank_grades, 3),
        "n_labeled_in_candidates": len(labeled),
        "n_perfect_in_candidates": sum(1 for s in labeled.values() if s >= 2),
        "n_qrels_total": len(qrels),
        "n_perfect_total": sum(1 for s in qrels.values() if s >= 2),
    }


def eligible(rec: dict) -> tuple[bool, str]:
    if rec["query_id"] in HISTORICAL_EXPOSED:
        return False, "historical_exposed_query_id"
    if rec["n_qrels_total"] < 2:
        return False, "fewer_than_2_labeled_docs_in_full_qrels"
    if rec["n_labeled_in_candidates"] < MIN_QREL_DOCS_IN_CANDIDATES:
        return False, f"fewer_than_{MIN_QREL_DOCS_IN_CANDIDATES}_labeled_docs_in_top{TOP_K}"
    if rec["n_perfect_in_candidates"] < MIN_PERFECT_IN_CANDIDATES:
        return False, "no_perfect_grade_in_top8"
    lo, hi = NDCG_BAND
    if not (lo < rec["bm25_ndcg3"] < hi):
        return False, f"bm25_ndcg3_outside_[{lo},{hi}]"
    return True, "eligible"


def select(split_records: list[dict], n: int, taken_tokens: list[set[str]]) -> tuple[list[dict], list[dict]]:
    """Lexicographic query_id order among eligible; skip near-duplicates vs already selected."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for rec in sorted(split_records, key=lambda r: r["query_id"]):
        ok, reason = eligible(rec)
        if not ok:
            rejected.append({"query_id": rec["query_id"], "reason": reason})
            continue
        tokens = set(tokenize(rec["query_text"]))
        if any(jaccard(tokens, prev) > MAX_QUERY_TOKEN_JACCARD for prev in taken_tokens + [set(tokenize(r["query_text"])) for r in accepted]):
            rejected.append({"query_id": rec["query_id"], "reason": "near_duplicate_query_text"})
            continue
        accepted.append(rec)
        if len(accepted) >= n:
            break
    return accepted, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    corpus_dir = args.corpus
    args.out.mkdir(parents=True, exist_ok=True)

    corpus_path = corpus_dir / "corpus.jsonl"
    queries_path = corpus_dir / "queries.jsonl"
    dev_qrels_path = corpus_dir / "qrels" / "dev.tsv"
    test_qrels_path = corpus_dir / "qrels" / "test.tsv"
    zip_path = corpus_dir.parent / "nfcorpus.zip"

    raw_docs = load_jsonl(corpus_path)
    docs = {row["_id"]: ((row.get("title") or "").strip() + "\n" + (row.get("text") or "").strip()).strip()
            for row in raw_docs}
    raw_queries = load_jsonl(queries_path)
    queries = {row["_id"]: (row.get("text") or "").strip() for row in raw_queries}

    dev_qrels = load_qrels(dev_qrels_path)
    test_qrels = load_qrels(test_qrels_path)

    print(f"BM25 index: {len(docs)} docs, avgdl={sum(len(tokenize(d)) for d in docs.values())/len(docs):.1f}")
    bm25 = BM25(docs)

    def prepare(qrels: dict[str, dict[str, int]]) -> list[dict]:
        recs = []
        for qid, qrels_map in sorted(qrels.items()):
            if qid not in queries or not queries[qid]:
                continue
            top = bm25.top(queries[qid], TOP_K)
            recs.append(build_query_record(qid, queries[qid], docs, top, qrels_map))
        return recs

    dev_all = prepare(dev_qrels)
    test_all = prepare(test_qrels)
    print(f"prepared dev={len(dev_all)} test={len(test_all)}")

    # Independent selection: dev first (lexicographic among eligible), then heldout.
    dev_selected, dev_rejected = select(dev_all, N_DEV, [])
    heldout_selected, heldout_rejected = select(test_all, N_HELDOUT, [set(tokenize(r["query_text"])) for r in dev_selected])

    audit = {
        "schema_version": 1,
        "rule_frozen_before_llm": True,
        "dataset": "BEIR/nfcorpus",
        "bm25": {"k1": K1, "b": B, "top_k": TOP_K,
                 "tokenizer": "lowercase [a-z0-9]+",
                 "index_fields": "title + text",
                 "tie_break": "(-score, doc_id)"},
        "eligibility": {
            "min_qrel_docs_full": 2,
            "min_labeled_in_top8": MIN_QREL_DOCS_IN_CANDIDATES,
            "min_perfect_in_top8": MIN_PERFECT_IN_CANDIDATES,
            "bm25_ndcg3_band": list(NDCG_BAND),
            "max_query_token_jaccard": MAX_QUERY_TOKEN_JACCARD,
            "excluded_historical_ids": sorted(HISTORICAL_EXPOSED),
            "order": "lexicographic query_id among eligible",
        },
        "splits": {
            "dev_source": "qrels/dev.tsv",
            "heldout_source": "qrels/test.tsv",
            "dev_candidates_considered": len(dev_all),
            "heldout_candidates_considered": len(test_all),
        },
        "counts": {
            "dev_selected": len(dev_selected),
            "heldout_selected": len(heldout_selected),
            "dev_rejected": len(dev_rejected),
            "heldout_rejected": len(heldout_rejected),
        },
        "dev_rejected_reasons": Counter(r["reason"] for r in dev_rejected),
        "heldout_rejected_reasons": Counter(r["reason"] for r in heldout_rejected),
        "dev_selected_ids": [r["query_id"] for r in dev_selected],
        "heldout_selected_ids": [r["query_id"] for r in heldout_selected],
        "forbidden": [
            "select after observing LLM flips",
            "prefer queries favorable to ReviewValue",
            "mutate heldout after results",
            "insert relevant docs manually",
        ],
    }
    # Counter is not JSON-serializable
    audit["dev_rejected_reasons"] = dict(audit["dev_rejected_reasons"])
    audit["heldout_rejected_reasons"] = dict(audit["heldout_rejected_reasons"])

    def to_collection(accepted: list[dict]) -> list[dict]:
        return [{
            "query_id": r["query_id"],
            "query_text": r["query_text"],
            "doc_ids": r["doc_ids"],
            "doc_texts": r["doc_texts"],
            "qrels": r["qrels"],
            "retrieval_scores": r["retrieval_scores"],
            "bm25_ndcg3": r["bm25_ndcg3"],
            "n_labeled_in_candidates": r["n_labeled_in_candidates"],
            "n_perfect_in_candidates": r["n_perfect_in_candidates"],
        } for r in accepted]

    dataset_manifest = {
        "schema_version": 1,
        "dataset_id": "beir/nfcorpus",
        "dataset_version": "BEIR official zip (public.ukp.informatik.tu-darmstadt.de)",
        "source_url": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip",
        "source_kind": "real",
        "retriever": "deterministic BM25",
        "script": "scripts/prepare_nfcorpus_gate3.py",
        "files": {},
        "historical_exposed_queries": sorted(HISTORICAL_EXPOSED),
        "note": "Unlabeled candidates are evaluation-as-zero by BEIR convention, not human confirmed negatives.",
        "qrels_grades": {"1": "possibly relevant", "2": "perfectly relevant"},
    }
    for label, path in (("zip", zip_path), ("corpus", corpus_path), ("queries", queries_path),
                        ("qrels_dev", dev_qrels_path), ("qrels_test", test_qrels_path)):
        if path.is_file():
            dataset_manifest["files"][label] = {
                "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }

    (args.out / "dataset_manifest.json").write_text(
        json.dumps(dataset_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "dev_queries.json").write_text(
        json.dumps(to_collection(dev_selected), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "heldout_queries.json").write_text(
        json.dumps(to_collection(heldout_selected), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "selection_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "bm25_top8.json").write_text(
        json.dumps({"dev": dev_selected, "heldout": heldout_selected}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # Report
    lines = [
        "# Gate 3 NFCorpus Query Group Freeze",
        "",
        f"- Dataset: BEIR/nfcorpus ({dataset_manifest['files'].get('zip', {}).get('sha256', 'n/a')[:16]}…)",
        f"- Retriever: BM25 k1={K1} b={B}, tokenizer lowercase [a-z0-9]+, title+text, Top-{TOP_K}",
        f"- Dev source: qrels/dev.tsv → selected {len(dev_selected)}/{len(dev_all)}",
        f"- Heldout source: qrels/test.tsv → selected {len(heldout_selected)}/{len(test_all)}",
        "- Selection order: lexicographic query_id among pre-registered eligible",
        f"- Eligibility: ≥2 labeled in top{TOP_K}, ≥1 grade-2 in top{TOP_K}, BM25 nDCG@3 in {NDCG_BAND}",
        "",
        "## Dev queries",
        "",
        "| query_id | n_lab | n_perfect | nDCG@3 | query |",
        "|---|---:|---:|---:|---|",
    ]
    for r in dev_selected:
        qtext = r["query_text"][:60].replace("|", "/")
        lines.append(f"| {r['query_id']} | {r['n_labeled_in_candidates']} | {r['n_perfect_in_candidates']} | {r['bm25_ndcg3']:.3f} | {qtext} |")
    lines += [
        "",
        "## Heldout queries",
        "",
        "| query_id | n_lab | n_perfect | nDCG@3 | query |",
        "|---|---:|---:|---:|---|",
    ]
    for r in heldout_selected:
        qtext = r["query_text"][:60].replace("|", "/")
        lines.append(f"| {r['query_id']} | {r['n_labeled_in_candidates']} | {r['n_perfect_in_candidates']} | {r['bm25_ndcg3']:.3f} | {qtext} |")
    lines += [
        "",
        "## Boundaries",
        "",
        "- Unlabeled docs in candidates are evaluation-as-zero (BEIR convention), not human-confirmed negatives.",
        "- Heldout is frozen for static build/verify only; no LLM on heldout in this gate.",
        "- No selection based on LLM flip behavior.",
    ]
    (args.out / "dataset_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"dev selected: {[r['query_id'] for r in dev_selected]}")
    print(f"heldout selected: {[r['query_id'] for r in heldout_selected]}")
    print(f"wrote {args.out}")
    if len(dev_selected) < N_DEV or len(heldout_selected) < N_HELDOUT:
        print("WARNING: fewer groups than protocol minimum")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
