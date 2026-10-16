"""Independent audit common utilities. Reads ONLY raw data files; never imports certirank.

Re-implementation of the documented Gate 3 semantics (protocol 0.3.0-research-pilot):
fixed-leaf tournament with winner removal, three-slot review groups (initial slot +
two probe slots, majority-of-three update), cached-replay repair mode, and the
dev-fitted ReviewValue value model. Every constant is re-derived from the frozen
config/workload/value-model files, not from the original code objects.
"""
from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE3 = ROOT / "data" / "gate3_nfcorpus"
OUT = ROOT / "audit_gate3_independent" / "outputs"

SLOT_GROUPS = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]  # configs/experiment.json formal.slot_groups
K = 3
BUDGETS = ("0.2", "0.4")
SEED = 17  # configs/experiment.json seed
REPS = 12  # formal.response_count


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_pool(split: str) -> dict:
    """Return pool[query_id] = {doc_ids, qrels, relevant_idx, table{(i,j): [12 bool]}, pair_meta}."""
    base = GATE3 / "formal" / split
    workload = load_json(base / "workload.json")
    calls = load_jsonl(base / "pair_calls.jsonl")
    pool = {}
    for entry in workload["entries"]:
        qid = entry["query_id"]
        pair_meta = {p["pair_position"]: p for p in entry["pairs"]}
        pool[qid] = {
            "query_text": entry["query_text"],
            "doc_ids": entry["doc_ids"],
            "qrels": {str(d): int(s) for d, s in entry["qrels"].items()},
            "relevant": frozenset(i for i, d in enumerate(entry["doc_ids"]) if entry["qrels"].get(d, 0) > 0),
            "pair_meta": pair_meta,
            "table": {},
        }
    for row in calls:
        qid = row["query_id"]
        pos = row["pair_position"]
        meta = pool[qid]["pair_meta"][pos]
        key = (meta["position_i"], meta["position_j"])
        pool[qid]["table"].setdefault(key, {})[row["repetition_id"]] = row["strict_parsed_decision"]
    for qid, entry in pool.items():
        entry["table"] = {pair: [votes[r] for r in range(REPS)] for pair, votes in sorted(entry["table"].items())}
    return workload, calls, pool


# ---------------------------------------------------------------- independent parser
_STANDALONE = re.compile(r"^(document\s*[12])$", re.IGNORECASE)
_ANY_MENTION = re.compile(r"document\s*([12])", re.IGNORECASE)


def parse_verdict_independent(text: str) -> tuple[bool | None, str]:
    """Independent parse rule, same documented spec: last standalone 'Document 1/2'
    line wins; otherwise the last mention anywhere. Case-tolerant regex variant."""
    if not isinstance(text, str) or not text.strip():
        return None, "invalid"
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip().rstrip(".").strip()
        if _STANDALONE.match(line):
            return line.lower().endswith("1"), "valid"
    mentions = _ANY_MENTION.findall(text)
    if not mentions:
        return None, "invalid"
    return mentions[-1] == "1", "valid"


# ---------------------------------------------------------------- independent tournament
class BudgetExhausted(Exception):
    pass


class ResponseUnavailable(Exception):
    pass


class AuditLedger:
    """Re-implementation of the three-slot execution ledger semantics."""

    def __init__(self, table: dict, slots: tuple[int, int, int]):
        self.table = table
        self.slots = tuple(slots)
        self.decisions: dict[tuple[int, int], bool] = {}
        self.answers: dict[tuple[int, int], list[bool]] = {}
        self.reviewed: set[tuple[int, int]] = set()
        self.base_calls = 0
        self.extra_calls = 0
        self.cache_hits = 0
        self.limit: int | None = None
        self.events: list[tuple] = []  # (pair, slot, phase, verdict, extra)
        self.failed = False

    @property
    def remaining(self) -> int:
        return 0 if self.limit is None else self.limit - self.extra_calls

    def _read(self, pair, index, phase):
        if self.limit is not None:
            if self.remaining <= 0:
                raise BudgetExhausted(pair)
            self.extra_calls += 1
        else:
            self.base_calls += 1
        if index >= len(self.slots):
            self.failed = True
            raise ResponseUnavailable(pair)
        slot = self.slots[index]
        try:
            value = self.table[pair][slot]
        except (KeyError, IndexError):
            self.failed = True
            raise ResponseUnavailable(pair)
        self.events.append((pair, slot, phase, value, self.limit is not None))
        return value

    def observe(self, i, j):
        pair = (i, j)
        if i >= j:
            raise ValueError("pair order must be i<j")
        if pair in self.decisions:
            self.cache_hits += 1
            return self.decisions[pair]
        value = self._read(pair, 0, "base" if self.limit is None else "repair")
        self.answers[pair] = [value]
        self.decisions[pair] = value
        return value

    def probe(self, pair):
        if self.limit is None or pair not in self.decisions or pair in self.reviewed:
            raise ValueError("probe preconditions")
        if self.remaining < 2:
            raise BudgetExhausted(pair)
        self.reviewed.add(pair)
        before = self.decisions[pair]
        votes = list(self.answers[pair])
        votes.append(self._read(pair, 1, "probe"))
        votes.append(self._read(pair, 2, "probe"))
        after = sum(votes) >= 2
        self.decisions[pair] = after
        return before, after, votes


def tournament(n: int, k: int, compare):
    """Fixed-leaf tournament: leaves in frozen row order padded to a power of two;
    emit root winner, remove its leaf, update only its ancestors. Returns
    (topk, events) where events = [(node, (a, b), winner, stage)]."""
    size = 1 << (n - 1).bit_length()
    nodes: list[int | None] = [None] * (size * 2)
    nodes[size:size + n] = range(n)
    events: list[tuple] = []

    def evaluate(node, stage):
        left, right = nodes[node * 2], nodes[node * 2 + 1]
        if left is None or right is None:
            nodes[node] = right if left is None else left
            return
        a, b = sorted((left, right))
        verdict = compare(a, b)
        if not isinstance(verdict, bool):
            raise ValueError("nonboolean verdict")
        nodes[node] = a if verdict else b
        events.append((node, (a, b), nodes[node], stage))

    for node in range(size - 1, 0, -1):
        evaluate(node, 0)
    output = []
    for stage in range(k):
        winner = nodes[1]
        if winner is None:
            raise RuntimeError("tournament exhausted")
        output.append(winner)
        if stage + 1 < k:
            node = size + winner
            nodes[node] = None
            node //= 2
            while node:
                evaluate(node, stage + 1)
                node //= 2
    return output, events


# ---------------------------------------------------------------- ReviewValue model (frozen input)
class FrozenValueModel:
    """Re-implementation of the frozen ReviewValueModel scoring semantics."""

    def __init__(self, path: Path):
        data = load_json(path)
        assert data["schema_version"] == 1
        self.samples = {key: list(values) for key, values in data["samples"].items()}
        self.training_query_groups = tuple(data["training_query_groups"])
        self.training_source_ids = tuple(data["training_source_ids"])
        self.minimum_samples = data["minimum_samples"]

    def estimate(self, features: str, remaining: int):
        samples = self.samples.get(features, ())
        if len(samples) < self.minimum_samples:
            return None
        feasible = [s["complete"] and s["cost"] <= remaining for s in samples]
        gains = [s["gain"] if ok else 0.0 for s, ok in zip(samples, feasible)]
        quality = [s["quality_delta"] if ok else 0.0 for s, ok in zip(samples, feasible)]
        expected_gain = sum(gains) / (len(samples) + 2)
        expected_cost = sum(min(s["cost"], remaining) for s in samples) / len(samples)
        score = expected_gain / max(2, expected_cost) if expected_gain > 0 and sum(quality) >= 0 else None
        return None if score is None else {
            "samples": len(samples), "estimated_gain": expected_gain, "estimated_cost": expected_cost,
            "score": score}

    def score(self, features: str, remaining: int):
        est = self.estimate(features, remaining)
        return None if est is None else est["score"]


def conditional_status(decisions: dict, n: int, k: int, pair, base_topk):
    """Re-implementation of diagnostics.conditional_effect: flip one decision, freeze
    the rest, replay the tournament; 'unresolved' if a new comparison is needed."""
    observed = dict(decisions)

    class Missing(Exception):
        pass

    def compare(i, j):
        key = (i, j)
        if key not in observed:
            raise Missing(key)
        return (not observed[key]) if key == pair else observed[key]

    try:
        topk, _ = tournament(n, k, compare)
    except Missing:
        return "unresolved"
    return "conditional_impact" if set(topk) != set(base_topk) else "conditional_safe"


def action_features(decisions, topk, topk_events, n, k, pair) -> str:
    """Re-implementation of value_model.action_features."""
    status = conditional_status(decisions, n, k, pair, topk)
    distances = [node.bit_length() - 1 for node, pr, winner, stage in topk_events if pr == pair]
    distance = min(distances) if distances else n
    return json.dumps([n, k, status, distance], separators=(",", ":"))


# ---------------------------------------------------------------- execution engine
def execute(table, n, k, slots, method, fraction, seed, model=None, record=None):
    """Independent re-execution of one complete algorithm run.

    method: Base | Random | ReviewValue. cached_replay semantics for repairs.
    Returns dict with topk, calls, reviews and a full decision/probe trace.
    """
    ledger = AuditLedger(table, slots)
    topk, events = tournament(n, k, ledger.observe)
    base_calls = ledger.base_calls
    if method == "Base":
        return {"topk": topk, "status": "complete", "reason": "reference", "base_calls": base_calls,
                "extra_calls": 0, "logical_calls": base_calls, "reviews": [], "selected": [],
                "cache_hits": ledger.cache_hits, "budget_limit": None}
    limit = base_calls * Fraction(fraction).numerator // Fraction(fraction).denominator
    ledger.limit = limit
    reviews, selected = [], []
    status, reason = "complete", "budget_or_candidates_exhausted"
    step = 0
    while True:
        candidates = sorted(p for p in ledger.decisions if p not in ledger.reviewed)
        if ledger.remaining < 2 or not candidates:
            break
        if method == "Random":
            import random as _random
            pair = _random.Random(seed + step).choice(candidates)
            estimate = None
        elif method == "ReviewValue":
            scores = []
            for cand in candidates:
                feats = action_features(dict(ledger.decisions), topk, events, n, k, cand)
                scores.append((model.score(feats, ledger.remaining), cand, feats))
            eligible = [(s, p, f) for s, p, f in scores if s is not None]
            if not eligible:
                reason = "no_positive_value_estimate_stopped"
                break
            best = min(eligible, key=lambda item: (-item[0], item[1]))
            pair, estimate, feats_used = best[1], {"score": best[0]}, best[2]
        else:
            raise ValueError(method)
        selected.append(pair)
        prev_topk = topk
        before = ledger.decisions[pair]
        try:
            b, after, votes = ledger.probe(pair)
        except BudgetExhausted:
            status, reason = "aborted", "budget"
            break
        changed = before != after
        if changed:
            try:
                topk, events = tournament(n, k, ledger.observe)  # cached_replay: full rebuild, decisions cached
            except BudgetExhausted:
                status, reason = "aborted", "budget_during_repair"
                topk = prev_topk
                break
        reviews.append({
            "step": step, "pair": pair, "before": before, "after": after, "votes": votes,
            "decision_changed": changed, "set_changed": set(prev_topk) != set(topk),
            "estimate": estimate, "pre_topk": list(prev_topk), "post_topk": list(topk),
        })
        if record is not None:
            record(pair, before, after, prev_topk, topk)
        step += 1
    return {"topk": topk, "status": status, "reason": reason, "base_calls": base_calls,
            "extra_calls": ledger.extra_calls, "logical_calls": base_calls + ledger.extra_calls,
            "reviews": reviews, "selected": selected, "cache_hits": ledger.cache_hits,
            "budget_limit": limit, "decisions_final": dict(ledger.decisions)}


def d_em(topks) -> float:
    """Mean set-inequality over all execution pairs (protocol D_EM)."""
    from itertools import combinations
    pairs = list(combinations(range(len(topks)), 2))
    return sum(set(topks[a]) != set(topks[b]) for a, b in pairs) / len(pairs)


def recall_at_k(topk, relevant):
    return len(set(topk) & set(relevant)) / len(relevant) if relevant else None


def bootstrap_group_means(values, samples=2000, seed=17):
    import random as _random
    import statistics
    rng = _random.Random(seed)
    out = []
    for _ in range(samples):
        draw = [rng.choice(values) for _ in values]
        out.append(statistics.mean(draw))
    out.sort()
    lo = out[int(0.025 * len(out))]
    hi = out[min(len(out) - 1, int(0.975 * len(out)))]
    return lo, hi


def save_json(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT / name}")
