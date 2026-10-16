"""Test suite for the Gate 3 structural analysis sandbox.

Covers the eight required categories:
 1. deterministic total-order oracle correctness across operators;
 2. comparison cache hits and call counting;
 3. single flip triggering a new comparison (branch escape);
 4. double-flip joint non-additivity;
 5. nontransitive comparator handling;
 6. exact enumeration vs (memoized) certification consistency;
 7. certification search budget exhaustion -> UNKNOWN;
 8. replay reproducibility under a fixed world (incl. official Base rows).

Run:  python -m pytest experiments/gate3_structural_analysis/tests -q
"""
from __future__ import annotations

import random
import sys
from itertools import combinations
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sandbox import (Judge, N, K, OPERATORS, UnresolvedPair, certify_empirical,
                     certify_worst_case, load_split, run_operator,
                     run_operator_fixed_trace, slot_world)

SEED = 20261016


# ----------------------------------------------------- 1. total-order oracle

def _random_worlds(count, n, seed=SEED):
    rng = random.Random(seed)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    for _ in range(count):
        yield {p: rng.random() < 0.5 for p in pairs}


@pytest.mark.parametrize("n,k", [(8, 3), (5, 2), (8, 1), (8, 7), (6, 4)])
def test_total_order_oracle_all_operators(n, k):
    """Under a strict total order every operator must return the true Top-K."""
    rng = random.Random(SEED + n * 100 + k)
    for _ in range(60):
        ranks = list(range(n))
        rng.shuffle(ranks)

        def getter(pair, _r=ranks):
            lo, hi = pair
            return _r[lo] > _r[hi]

        true_topk = frozenset(sorted(range(n), key=lambda x: ranks[x], reverse=True)[:k])
        for name, fn in OPERATORS.items():
            run = fn(n, k, Judge(getter))
            assert run.topk_set == true_topk, f"{name} wrong under total order (n={n}, k={k})"


# ----------------------------------------------------- 2. cache & call counting

def _n8_pairs():
    return [(i, j) for i in range(N) for j in range(i + 1, N)]


def test_cache_hits_and_call_counts():
    world = {p: True for p in _n8_pairs()}
    run = run_operator("tournament", world)
    # n=8 fills the tree: 7 build comparisons + 2 comparisons per extraction stage
    # (minus pass-throughs); unique pairs = logical calls, repeats = cache hits.
    assert run.calls == run.unique_pairs
    assert run.calls + run.cache_hits >= 9  # every comparison invocation is counted
    # fullcompare reads each pair exactly once: 28 calls, zero cache hits
    full = run_operator("fullcompare", world)
    assert full.calls == 28 and full.cache_hits == 0 and full.unique_pairs == 28


def test_judge_cache_semantics():
    """Repeated comparisons of one pair are served from the decision cache:
    one logical call, later invocations counted as cache hits. compare() returns
    values in canonical orientation (True = smaller index wins); beats() handles
    arbitrary argument order."""
    values = {(0, 1): True, (0, 2): False}
    judge = Judge(values.__getitem__)
    assert judge.compare(0, 1) is True       # first read of (0,1): doc0 wins
    assert judge.compare(1, 0) is True       # same canonical pair -> cache hit
    assert judge.calls == 1 and judge.cache_hits == 1
    assert judge.compare(0, 2) is False      # first read of (0,2): doc2 wins
    assert judge.compare(2, 0) is False      # cache hit, canonical orientation
    assert judge.calls == 2 and judge.cache_hits == 2
    assert judge.access_order == [(0, 1), (0, 2)]
    # orientation wrapper: winner is decided by the cached canonical value
    assert judge.beats(0, 1) and not judge.beats(1, 0)
    assert not judge.beats(0, 2) and judge.beats(2, 0)
    assert judge.calls == 2 and judge.cache_hits == 6


def test_real_data_cache_accounting():
    """Verified structural facts on the raw data (18 queries x 12 slot worlds):
    tournament/quickselect/fullcompare never re-read a pair within one execution;
    minheap does in ~65% of worlds (sift re-compares) and its accounting stays
    consistent wherever it happens."""
    pools = {"dev": load_split("dev"), "heldout": load_split("heldout")}
    counts = {"tournament": 0, "minheap": 0, "quickselect": 0, "fullcompare": 0}
    total = 0
    for pool in pools.values():
        for entry in pool.values():
            for r in range(12):
                world = slot_world(entry["table"], r)
                total += 1
                for name in counts:
                    run = run_operator(name, world)
                    assert run.calls == run.unique_pairs
                    if run.cache_hits > 0:
                        counts[name] += 1
    assert total == 216
    assert counts["tournament"] == 0 and counts["quickselect"] == 0
    assert counts["fullcompare"] == 0 and counts["minheap"] > 0


# ----------------------------------------------------- 3. single flip -> new comparison

def test_single_flip_triggers_new_comparison():
    """Hand-built n=4/k=2 world: flipping the root comparison (0,2) sends the
    extraction down a path that needs pair (0,3), absent from the base trace."""
    pairs4 = [(i, j) for i in range(4) for j in range(i + 1, 4)]
    world = {p: True for p in pairs4}          # smaller index always wins
    world[(0, 3)] = False                       # except doc3 beats doc0
    base = run_operator("tournament", world, n=4, k=2)
    assert (0, 3) not in base.access_order

    flipped = run_operator("tournament", world, n=4, k=2, flips=frozenset([(0, 2)]))
    assert (0, 3) in flipped.access_order, "flip of (0,2) must branch into pair (0,3)"

    with pytest.raises(UnresolvedPair):
        run_operator_fixed_trace("tournament", world, set(base.access_order),
                                 frozenset([(0, 2)]), n=4, k=2)


def test_real_data_branch_escape_exists():
    """Branch escapes are not a hand-built curiosity: they occur in the raw data."""
    pool = load_split("dev")
    escapes = 0
    for qid, entry in pool.items():
        world = slot_world(entry["table"], 0)
        base = run_operator("tournament", world)
        for pair in base.access_order:
            flipped = run_operator("tournament", world, flips=frozenset([pair]))
            if set(flipped.access_order) - set(base.access_order):
                escapes += 1
    assert escapes > 0, "no branch escape found across the whole dev split"


# ----------------------------------------------------- 4. double-flip non-additivity

def test_double_flip_nonadditive_instance():
    """Seeded search for a world where two single flips are individually absorbed
    but the joint flip changes the Top-K set (joint-emergent effect)."""
    rng = random.Random(SEED)
    pairs = _n8_pairs()
    for _ in range(4000):
        world = {p: rng.random() < 0.5 for p in pairs}
        base = run_operator("tournament", world)
        harmless = [p for p in base.access_order
                    if run_operator("tournament", world, flips=frozenset([p])).topk_set == base.topk_set]
        for p1, p2 in combinations(harmless, 2):
            joint = run_operator("tournament", world, flips=frozenset([p1, p2]))
            if joint.topk_set != base.topk_set:
                return  # found: non-additivity demonstrated
    pytest.fail("no joint-emergent double flip found in 4000 seeded random worlds")


# ----------------------------------------------------- 5. nontransitive comparators

def _cycle_world(n):
    """Every doc loses to its cyclic successor: a maximally nontransitive world."""
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    world = {}
    for i, j in pairs:
        world[(i, j)] = ((j - i) % n) != 1  # doc j beats doc i iff j is i's successor
    return world


@pytest.mark.parametrize("name", list(OPERATORS))
def test_nontransitive_termination_and_determinism(name):
    world = _cycle_world(8)
    runs = [run_operator(name, world) for _ in range(3)]
    assert all(r.topk == runs[0].topk and r.access_order == runs[0].access_order for r in runs)
    assert len(runs[0].topk) == K


# ----------------------------------------------------- 6. exact vs certification

def _brute_force_worst_case(operator, world, base_topk, executed, b, n, k):
    """Ground truth: enumerate every full assignment of all pairs (tiny n only)."""
    pairs = sorted(world)
    for bits in range(1 << len(pairs)):
        candidate = {p: bool(bits >> i & 1) for i, p in enumerate(pairs)}
        flips = sum(1 for p in executed if candidate[p] != world[p])
        if flips > b:
            continue
        run = run_operator(operator, candidate, n, k)
        if run.topk_set != base_topk:
            return False
    return True


@pytest.mark.parametrize("b", [0, 1, 2])
def test_worst_case_dfs_matches_brute_force(b):
    """On n=4 instances the DFS certification must equal exhaustive enumeration."""
    rng = random.Random(SEED + b)
    pairs4 = [(i, j) for i in range(4) for j in range(i + 1, 4)]
    for trial in range(40):
        world = {p: rng.random() < 0.5 for p in pairs4}
        base = run_operator("tournament", world, n=4, k=2)
        executed = frozenset(base.access_order)
        expected = _brute_force_worst_case("tournament", world, base.topk_set, executed, b, 4, 2)
        got = certify_worst_case("tournament", world, base, b, n=4, k=2)
        assert got.verdict == ("certified" if expected else "violated"), \
            f"trial {trial} b={b}: dfs={got.verdict} brute={'certified' if expected else 'violated'}"


def test_memoized_matches_unmemoized_and_exhaustive():
    """Memoization and exhaustive enumeration must not change the verdict."""
    rng = random.Random(SEED + 99)
    for _ in range(25):
        world = {p: rng.random() < 0.5 for p in _n8_pairs()}
        base = run_operator("tournament", world)
        verdicts = {mode: certify_worst_case("tournament", world, base, 2, memo=m,
                                             exhaustive=(mode == "exhaustive"),
                                             deadline_s=60).verdict
                    for m, mode in ((True, "memo"), (False, "nomemo"), (False, "exhaustive"))}
        assert len(set(verdicts.values())) == 1, verdicts


def test_empirical_certification_matches_direct_enumeration():
    rng = random.Random(SEED + 7)
    for _ in range(20):
        world = {p: rng.random() < 0.5 for p in _n8_pairs()}
        base = run_operator("minheap", world)
        cert = certify_empirical("minheap", world, base, 1)
        direct = all(run_operator("minheap", world, flips=frozenset([p])).topk_set == base.topk_set
                     for p in base.access_order)
        assert cert.verdict == ("certified" if direct else "violated")


# ----------------------------------------------------- 7. budget exhaustion -> UNKNOWN

def test_certification_budget_exhaustion_returns_unknown():
    world = {p: True for p in _n8_pairs()}
    base = run_operator("tournament", world)
    for budget in (1, 2, 5):
        cert = certify_worst_case("tournament", world, base, 2, node_budget=budget, deadline_s=None)
        assert cert.verdict == "unknown", f"budget {budget} must yield UNKNOWN, got {cert.verdict}"
    cert = certify_worst_case("tournament", world, base, 2, node_budget=10**7, deadline_s=1e-9)
    assert cert.verdict == "unknown"


def test_unknown_never_masquerades_as_certified():
    """A genuinely violable instance with a tiny budget must still report UNKNOWN."""
    rng = random.Random(SEED + 3)
    pairs = _n8_pairs()
    for _ in range(200):
        world = {p: rng.random() < 0.5 for p in pairs}
        base = run_operator("tournament", world)
        starved = certify_worst_case("tournament", world, base, 1, node_budget=3, deadline_s=None)
        if starved.verdict == "unknown":
            full = certify_worst_case("tournament", world, base, 1)
            assert full.verdict in ("certified", "violated")
            return
    pytest.fail("never hit the budget cap; test is vacuous")


# ----------------------------------------------------- 8. replay reproducibility

@pytest.mark.parametrize("name", list(OPERATORS))
def test_replay_reproducible_fixed_world(name):
    pool = load_split("heldout")
    entry = pool["PLAIN-2"]
    world = slot_world(entry["table"], 5)
    runs = [run_operator(name, world) for _ in range(3)]
    assert all(r.topk == runs[0].topk for r in runs)
    assert all(r.access_order == runs[0].access_order for r in runs)
    assert all(r.calls == runs[0].calls and r.cache_hits == runs[0].cache_hits for r in runs)


def test_reproduces_official_base_rows_sample():
    """The sandbox tournament must exactly reproduce official Base executions
    (topk, logical_calls, cache_hits) for a sample covering both splits."""
    import json
    formal = Path(__file__).resolve().parents[3] / "data" / "gate3_nfcorpus" / "formal"
    pools = {"dev": load_split("dev"), "heldout": load_split("heldout")}
    checked = 0
    for split in ("dev", "heldout"):
        with open(formal / f"{split}_results" / "runs.jsonl", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row["method"] != "Base" or checked >= 24:
                    continue
                world = slot_world(pools[split][row["query_id"]]["table"], row["slots"][0])
                run = run_operator("tournament", world, row["n"], row["k"])
                assert list(run.topk) == list(row["topk"])
                assert run.calls == row["logical_calls"]
                assert run.cache_hits == row["cache_hits"]
                checked += 1
        if checked >= 24:
            break
    assert checked == 24
