"""Offline pairwise replay sandbox for the Gate 3 structural analysis study.

Zero imports from ``certirank``: every operator is re-implemented from the documented
Gate 3 semantics (protocol 0.3.0-research-pilot) so this study is an independent
recomputation, cross-validated against the official ``runs.jsonl`` (see
``validate_sandbox.py``). Reads only raw JSON/JSONL data; never touches the network.

Core objects
------------
- ``load_split``        : rebuild the judgment tensor Y[q][(i,j)][r] from raw files.
- ``Judge``             : decision-cached comparator over one perturbed world.
- Operators             : ``tournament_topk``, ``minheap_topk``, ``quickselect_topk``,
                          ``fullcompare_topk`` — all deterministic, pair-canonical,
                          call-counting plans with identical cache semantics.
- Counterfactual replay : frozen-world and fixed-trace modes.
- Certification search  : exact DFS over worst-case / empirical bounded-flip worlds.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[3]
GATE3_FORMAL = ROOT / "data" / "gate3_nfcorpus" / "formal"

REPS = 12
K = 3
N = 8
SLOT_GROUPS = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]
OFFICIAL_INITIAL_SLOTS = (0, 3, 6, 9)

# ---------------------------------------------------------------- data loading


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_split(split: str) -> dict:
    """Return {query_id: entry} with entry.table[(i,j)] = [12 bools]."""
    base = GATE3_FORMAL / split
    workload = json.loads((base / "workload.json").read_text(encoding="utf-8"))
    calls = _load_jsonl(base / "pair_calls.jsonl")
    pool: dict[str, dict] = {}
    for entry in workload["entries"]:
        qid = entry["query_id"]
        pair_meta = {p["pair_position"]: p for p in entry["pairs"]}
        pool[qid] = {
            "query_id": qid,
            "query_text": entry["query_text"],
            "doc_ids": list(entry["doc_ids"]),
            "qrels": {str(d): int(s) for d, s in entry["qrels"].items()},
            "relevant": frozenset(i for i, d in enumerate(entry["doc_ids"]) if entry["qrels"].get(d, 0) > 0),
            "pair_meta": pair_meta,
            "table": {},
        }
    for row in calls:
        qid = row["query_id"]
        meta = pool[qid]["pair_meta"][row["pair_position"]]
        key = (meta["position_i"], meta["position_j"])
        slot = row["repetition_id"]
        tbl = pool[qid]["table"].setdefault(key, {})
        if slot in tbl:
            raise ValueError(f"duplicate (query, pair, rep): {qid} {key} {slot}")
        if row["parse_status"] != "valid" or row["strict_parsed_decision"] is None:
            raise ValueError(f"unparseable verdict in raw data: {qid} {key} {slot}")
        tbl[slot] = bool(row["strict_parsed_decision"])
    for qid, entry in pool.items():
        if len(entry["table"]) != 28:
            raise ValueError(f"{qid}: expected 28 pairs, got {len(entry['table'])}")
        for pair, votes in entry["table"].items():
            if sorted(votes) != sorted(range(REPS)) or len(votes) != REPS:
                raise ValueError(f"{qid} {pair}: incomplete repetition coverage")
        entry["table"] = {pair: [votes[r] for r in range(REPS)] for pair, votes in sorted(entry["table"].items())}
    return pool


def load_all() -> dict[str, dict]:
    return {"dev": load_split("dev"), "heldout": load_split("heldout")}


def slot_world(table: dict, r: int) -> dict[tuple[int, int], bool]:
    """One frozen pairwise judgment world: repetition slot r's verdict for every pair."""
    return {pair: votes[r] for pair, votes in table.items()}


# ---------------------------------------------------------------- exceptions


class UnresolvedPair(Exception):
    """Fixed-trace counterfactual needs a comparison outside the original trace."""

    def __init__(self, pair: tuple[int, int]):
        super().__init__(f"pair {pair} not in original trace")
        self.pair = pair


class NeedBranch(Exception):
    """Worst-case certification search hit a pair whose value is not yet forced."""

    def __init__(self, pair: tuple[int, int]):
        super().__init__(f"pair {pair} needs branching")
        self.pair = pair


class SearchBudgetExceeded(Exception):
    """Certification search exceeded its node budget -> verdict must be UNKNOWN."""


# ---------------------------------------------------------------- judge


class Judge:
    """Decision-cached comparator over one (possibly perturbed) frozen world.

    ``world_getter(pair) -> bool`` supplies values for first reads; it may raise
    UnresolvedPair (fixed-trace mode) or NeedBranch (worst-case search mode).
    ``True`` means the *smaller* index of the canonical pair wins — identical to
    Gate 3's ``strict_parsed_decision`` (Document 1 = position_i).
    """

    def __init__(self, world_getter: Callable[[tuple[int, int]], bool]):
        self._world_getter = world_getter
        self.decisions: dict[tuple[int, int], bool] = {}
        self.access_order: list[tuple[int, int]] = []
        self.calls = 0
        self.cache_hits = 0

    def compare(self, a: int, b: int) -> bool:
        """True -> the smaller of (a, b) wins this comparison."""
        if a == b:
            raise ValueError("self comparison")
        lo, hi = (a, b) if a < b else (b, a)
        pair = (lo, hi)
        if pair in self.decisions:
            self.cache_hits += 1
            return self.decisions[pair]
        value = self._world_getter(pair)  # may raise
        self.decisions[pair] = value
        self.access_order.append(pair)
        self.calls += 1
        return value

    def beats(self, x: int, y: int) -> bool:
        """Orientation-free wrapper: True -> x beats y."""
        if x == y:
            return False
        verdict = self.compare(x, y)
        winner = min(x, y) if verdict else max(x, y)
        return winner == x


def frozen_world_getter(world: dict, flips: frozenset = frozenset()):
    def getter(pair):
        return world[pair] != (pair in flips)
    return getter


def fixed_trace_getter(world: dict, allowed: set, flips: frozenset = frozenset()):
    def getter(pair):
        if pair not in allowed:
            raise UnresolvedPair(pair)
        return world[pair] != (pair in flips)
    return getter


def worst_case_getter(world: dict, executed: frozenset, forced: dict):
    """Worst-case mode: forced pairs use their forced value, everything else branches."""
    def getter(pair):
        if pair in forced:
            return forced[pair]
        raise NeedBranch(pair)
    return getter


# ---------------------------------------------------------------- operators


@dataclass
class PlanRun:
    operator: str
    topk: tuple[int, ...]          # ordered where the plan defines an order
    topk_set: frozenset[int]
    calls: int                     # unique pairs read (logical LLM calls)
    cache_hits: int
    access_order: tuple[tuple[int, int], ...]
    events: tuple = ()             # operator-specific trace (tournament: (node, pair, winner, stage))

    @property
    def unique_pairs(self) -> int:
        return len(self.access_order)


def tournament_topk(n: int, k: int, judge: Judge) -> PlanRun:
    """Fixed-leaf tournament (operator_version fixed_leaf_tournament_v1).

    Leaves in frozen row order padded with empty leaves to a power of two; initial
    tree built bottom-up over all internal nodes; extraction emits the root winner,
    removes its leaf and re-evaluates only its ancestors. Same-pair revisits inside
    one execution hit the decision cache (Gate 3 ledger semantics).
    """
    size = 1 << (n - 1).bit_length()
    nodes: list[int | None] = [None] * (size * 2)
    nodes[size:size + n] = range(n)
    events: list[tuple] = []

    def evaluate(node: int, stage: int) -> None:
        left, right = nodes[node * 2], nodes[node * 2 + 1]
        if left is None or right is None:
            nodes[node] = right if left is None else left
            return
        a, b = sorted((left, right))
        verdict = judge.compare(a, b)
        nodes[node] = a if verdict else b
        events.append((node, (a, b), nodes[node], stage))

    for node in range(size - 1, 0, -1):
        evaluate(node, 0)
    output: list[int] = []
    for stage in range(k):
        winner = nodes[1]
        if winner is None:
            raise RuntimeError("tournament exhausted before k outputs")
        output.append(winner)
        if stage + 1 < k:
            node = size + winner
            nodes[node] = None
            node //= 2
            while node:
                evaluate(node, stage + 1)
                node //= 2
    return PlanRun("tournament", tuple(output), frozenset(output), judge.calls,
                   judge.cache_hits, tuple(judge.access_order), tuple(events))


def minheap_topk(n: int, k: int, judge: Judge) -> PlanRun:
    """Bounded min-heap selection: keep k candidates, min-heap ordered by the
    comparator (root = weakest kept). Each new element is tested against the root;
    if it beats the root, it replaces it. Ordered output = pop-min sequence
    reversed (weakest-first extraction, so the reversed list is strongest-first)."""
    heap: list[int] = []

    def sift_up(i: int) -> None:
        while i > 0:
            parent = (i - 1) // 2
            # min-heap over "strength": parent must stay weaker than the child;
            # violation = parent beats child -> child moves up.
            if judge.beats(heap[parent], heap[i]):
                heap[i], heap[parent] = heap[parent], heap[i]
                i = parent
            else:
                break

    def sift_down(i: int) -> None:
        while True:
            left, right = 2 * i + 1, 2 * i + 2
            weakest = i
            if left < len(heap) and judge.beats(heap[weakest], heap[left]):
                weakest = left
            if right < len(heap) and judge.beats(heap[weakest], heap[right]):
                weakest = right
            if weakest == i:
                return
            heap[i], heap[weakest] = heap[weakest], heap[i]
            i = weakest

    for x in range(n):
        if len(heap) < k:
            heap.append(x)
            sift_up(len(heap) - 1)
        elif judge.beats(x, heap[0]):
            heap[0] = x
            sift_down(0)

    pop_order: list[int] = []
    work = list(heap)

    def sift_down_local(arr: list[int], i: int) -> None:
        while True:
            left, right = 2 * i + 1, 2 * i + 2
            weakest = i
            if left < len(arr) and judge.beats(arr[weakest], arr[left]):
                weakest = left
            if right < len(arr) and judge.beats(arr[weakest], arr[right]):
                weakest = right
            if weakest == i:
                return
            arr[i], arr[weakest] = arr[weakest], arr[i]
            i = weakest

    while work:
        pop_order.append(work[0])
        last = work.pop()
        if work:
            work[0] = last
            sift_down_local(work, 0)
    ordered = tuple(reversed(pop_order))
    return PlanRun("minheap", ordered, frozenset(ordered), judge.calls,
                   judge.cache_hits, tuple(judge.access_order))


def quickselect_topk(n: int, k: int, judge: Judge) -> PlanRun:
    """Lomuto-partition quickselect: after selection the first k array slots hold
    the plan's Top-K (set semantics only — slot order within [0, k) is not a
    meaningful ranking under a nontransitive comparator)."""
    a = list(range(n))
    target = k - 1
    lo, hi = 0, n - 1
    while lo < hi:
        i = lo - 1
        for j in range(lo, hi):
            if judge.beats(a[j], a[hi]):
                i += 1
                a[i], a[j] = a[j], a[i]
        a[i + 1], a[hi] = a[hi], a[i + 1]
        p = i + 1
        if p == target:
            break
        if p < target:
            lo = p + 1
        else:
            hi = p - 1
    topk = tuple(sorted(a[:k]))
    return PlanRun("quickselect", topk, frozenset(topk), judge.calls,
                   judge.cache_hits, tuple(judge.access_order))


def fullcompare_topk(n: int, k: int, judge: Judge) -> PlanRun:
    """Reference plan: read every pair once, rank by win count; stable sort breaks
    ties by ascending row index (LOTUS naive semantics from the Gate 2 audit).
    Well-defined ordered output; ranking semantics stated, not assumed canonical."""
    win = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            if judge.compare(i, j):
                win[i] += 1
            else:
                win[j] += 1
    order = sorted(range(n), key=lambda idx: win[idx], reverse=True)
    topk = tuple(order[:k])
    return PlanRun("fullcompare", topk, frozenset(topk), judge.calls,
                   judge.cache_hits, tuple(judge.access_order), tuple(win))


OPERATORS: dict[str, Callable[[int, int, Judge], PlanRun]] = {
    "tournament": tournament_topk,
    "minheap": minheap_topk,
    "quickselect": quickselect_topk,
    "fullcompare": fullcompare_topk,
}

# Plans whose output order is a defined part of their semantics.
ORDERED_OPERATORS = ("tournament", "minheap", "fullcompare")


def run_operator(name: str, world: dict, n: int = N, k: int = K,
                 flips: frozenset = frozenset()) -> PlanRun:
    judge = Judge(frozen_world_getter(world, flips))
    return OPERATORS[name](n, k, judge)


def run_operator_fixed_trace(name: str, world: dict, allowed: set,
                             flips: frozenset, n: int = N, k: int = K) -> PlanRun:
    judge = Judge(fixed_trace_getter(world, allowed, flips))
    return OPERATORS[name](n, k, judge)


# ---------------------------------------------------------------- certification search


@dataclass
class CertResult:
    verdict: str            # "certified" | "violated" | "unknown"
    base_topk: frozenset
    counterexample: list | None = None   # forced-values dict of a violating branch
    nodes: int = 0                        # search() invocations
    leaves: int = 0                       # completed plan executions
    pruned_by_memo: int = 0
    seconds: float = 0.0


def _search_worst_case(operator: str, n: int, k: int, world: dict,
                       executed: frozenset, base_topk: frozenset,
                       b: int, *, memo: dict | None, node_budget: int,
                       counter: dict, deadline: float | None,
                       exhaustive: bool = False) -> bool:
    """DFS over all worlds in the worst-case bounded-flip model.

    Returns True iff every completable branch reproduces base_topk (violation found
    -> False). Raises SearchBudgetExceeded when the node budget or deadline is hit.
    ``exhaustive=True`` disables both memoization and short-circuiting so branch
    counts measure the full search tree (for pruning-ratio reporting).

    Branching at a first read of pair p:
      - p executed in the base trace: branches w0(p) [free] and not w0(p) [costs 1];
      - p not executed: branches True / False [free] (unconstrained new comparison).
    """
    counter["nodes"] += 1

    def run(forced: dict) -> PlanRun | NeedBranch:
        judge = Judge(worst_case_getter(world, executed, forced))
        try:
            return OPERATORS[operator](n, k, judge)
        except NeedBranch as branch:
            return branch

    def search(forced: dict, budget: int, frozen_forced) -> bool:
        counter["nodes"] += 1
        if counter["nodes"] > node_budget:
            raise SearchBudgetExceeded(f"node budget {node_budget}")
        if deadline is not None and time.monotonic() > deadline:
            raise SearchBudgetExceeded("deadline")
        key = (frozen_forced, budget)
        if memo is not None and not exhaustive:
            if key in memo:
                counter["pruned_by_memo"] += 1
                return memo[key]
        outcome = run(forced)
        if isinstance(outcome, PlanRun):
            counter["leaves"] += 1
            result = outcome.topk_set == base_topk
        else:
            pair = outcome.pair
            branches: list[tuple[dict, int, frozenset]] = []
            if pair in executed:
                keep = dict(forced)
                keep[pair] = world[pair]
                branches.append((keep, budget, frozen_forced + ((pair, world[pair]),)))
                if budget >= 1:
                    flip = dict(forced)
                    flip[pair] = not world[pair]
                    branches.append((flip, budget - 1,
                                     frozen_forced + ((pair, not world[pair]),)))
            else:
                for value in (True, False):
                    child = dict(forced)
                    child[pair] = value
                    branches.append((child, budget, frozen_forced + ((pair, value),)))
            result = all(search(br, bud, frz) for br, bud, frz in branches)
        if memo is not None:
            memo[key] = result
        return result

    ok = search({}, b, ())
    return ok


def certify_worst_case(operator: str, world: dict, base_run: PlanRun,
                       b: int, *, memo: bool = True, node_budget: int = 200_000,
                       deadline_s: float | None = 30.0,
                       n: int = N, k: int = K, exhaustive: bool = False) -> CertResult:
    """Certify Top-K set invariance over the worst-case bounded-flip model."""
    executed = frozenset(base_run.access_order)
    counter = {"nodes": 0, "leaves": 0, "pruned_by_memo": 0}
    memo_dict: dict | None = {} if (memo and not exhaustive) else None
    start = time.monotonic()
    deadline = start + deadline_s if deadline_s is not None else None
    try:
        ok = _search_worst_case(operator, n, k, world, executed, base_run.topk_set,
                                b, memo=memo_dict, node_budget=node_budget,
                                counter=counter, deadline=deadline, exhaustive=exhaustive)
        verdict = "certified" if ok else "violated"
        counterexample = None
        if not ok:
            counterexample = _find_counterexample(operator, n, k, world, executed,
                                                  base_run.topk_set, b, node_budget, deadline)
    except SearchBudgetExceeded:
        verdict, counterexample = "unknown", None
    return CertResult(verdict, base_run.topk_set, counterexample,
                      counter["nodes"], counter["leaves"], counter["pruned_by_memo"],
                      time.monotonic() - start)


def _find_counterexample(operator: str, n: int, k: int, world: dict, executed: frozenset,
                         base_topk: frozenset, b: int, node_budget: int,
                         deadline: float | None) -> list | None:
    """Re-run the search, recording the forced values of the first violating branch."""
    def run(forced: dict):
        judge = Judge(worst_case_getter(world, executed, forced))
        try:
            return OPERATORS[operator](n, k, judge)
        except NeedBranch as branch:
            return branch

    def search(forced: dict, budget: int):
        if deadline is not None and time.monotonic() > deadline:
            raise SearchBudgetExceeded("deadline")
        outcome = run(forced)
        if isinstance(outcome, PlanRun):
            return None if outcome.topk_set == base_topk else sorted(forced.items())
        pair = outcome.pair
        branches = []
        if pair in executed:
            keep = dict(forced)
            keep[pair] = world[pair]
            branches.append((keep, budget))
            if budget >= 1:
                flip = dict(forced)
                flip[pair] = not world[pair]
                branches.append((flip, budget - 1))
        else:
            for value in (True, False):
                child = dict(forced)
                child[pair] = value
                branches.append((child, budget))
        for br, bud in branches:
            found = search(br, bud)
            if found is not None:
                return found
        return None

    try:
        return search({}, b)
    except SearchBudgetExceeded:
        return None


def certify_empirical(operator: str, world: dict, base_run: PlanRun, b: int,
                      *, n: int = N, k: int = K) -> CertResult:
    """Certify over empirical frozen-world bounded flips: every world that differs
    from w0 on at most b *executed* judgments (all other pairs keep w0 values;
    newly read pairs resolve from the same frozen world)."""
    executed = list(base_run.access_order)
    start = time.monotonic()
    counterexample = None
    leaves = 0
    violated = False
    if b == 0:
        leaves = 1
    elif b == 1:
        for pair in executed:
            run = run_operator(operator, world, n, k, flips=frozenset([pair]))
            leaves += 1
            if run.topk_set != base_run.topk_set:
                violated = True
                counterexample = [[pair, not world[pair]]]
                break
    elif b == 2:
        from itertools import combinations
        done = set()
        for p1, p2 in combinations(executed, 2):
            key = frozenset((p1, p2))
            if key in done:
                continue
            done.add(key)
            run = run_operator(operator, world, n, k, flips=key)
            leaves += 1
            if run.topk_set != base_run.topk_set:
                violated = True
                counterexample = [[p, not world[p]] for p in sorted(key)]
                break
    else:
        raise ValueError("b > 2 not implemented (budget scope)")
    verdict = "violated" if violated else "certified"
    return CertResult(verdict, base_run.topk_set, counterexample, leaves, leaves,
                      0, time.monotonic() - start)


# ---------------------------------------------------------------- metrics


def pairwise_set_disagreement(topks: list[frozenset]) -> float:
    """D_EM analog: mean set-inequality over all unordered pairs of executions."""
    from itertools import combinations
    pairs = list(combinations(range(len(topks)), 2))
    if not pairs:
        return 0.0
    return sum(topks[a] != topks[b] for a, b in pairs) / len(pairs)


def jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    return 1.0 if not union else len(a & b) / len(union)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
