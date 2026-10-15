"""Fixed-leaf tournament with winner removal and bottom-up local repair.

Leaves follow frozen candidate row order, padded with empty leaves to a power of two.
The root winner is emitted and its leaf removed, then only its ancestors are updated.
Nontransitivity is allowed: each requested pair needs a direct comparator answer.
"""

from collections.abc import Callable, Iterable

from certirank.trace import ComparisonEvent, ExecutionWork, Pair, TournamentResult, validate_pair

Comparator = Callable[[int, int], bool]


def tournament(
    n: int,
    k: int,
    compare: Comparator,
    *,
    previous: TournamentResult | None = None,
    changed_pairs: Iterable[Pair] = (),
    work: ExecutionWork | None = None,
) -> TournamentResult:
    """Return a complete output, or propagate failure without mutating prior state.

    Incremental mode starts from the previous *initial* tree, invalidates changed
    comparisons and their ancestors, and repeats extraction. All changed decisions
    must be listed by the caller. Unchanged request results may be legally cached.
    """
    if type(n) is not int or type(k) is not int or not 1 <= k <= n:
        raise ValueError("require integers 1 <= k <= n")
    size = 1 << (n - 1).bit_length()
    changed_pairs = tuple(changed_pairs)
    for pair in changed_pairs:
        validate_pair(pair, n)
    changed = frozenset(changed_pairs)
    clock = work if work is not None else ExecutionWork()
    events: list[ComparisonEvent] = []
    evaluations = 0

    def evaluate(node: int, stage: int) -> None:
        nonlocal evaluations
        evaluations += 1
        if work is not None:
            work.node_evaluations += 1
        left, right = nodes[node * 2], nodes[node * 2 + 1]
        if left is None or right is None:
            nodes[node] = right if left is None else left
            return
        a, b = sorted((left, right))
        verdict = compare(a, b)
        if type(verdict) is not bool:
            raise ValueError("comparator must return a boolean")
        nodes[node] = a if verdict else b
        events.append(ComparisonEvent(stage, node, (a, b), nodes[node]))

    if previous is None:
        with clock.phase("state_copy"):
            nodes: list[int | None] = [None] * (size * 2)
            nodes[size:size + n] = range(n)
        with clock.phase("initial_update"):
            for node in range(size - 1, 0, -1):
                evaluate(node, 0)
    else:
        if (previous.n, previous.k) != (n, k):
            raise ValueError("previous execution has incompatible n/k")
        with clock.phase("state_copy"):
            nodes = list(previous.initial_nodes)
        with clock.phase("invalidation"):
            dirty: set[int] = set()
            for node in range(1, size):
                left, right = nodes[node * 2], nodes[node * 2 + 1]
                if left is not None and right is not None and tuple(sorted((left, right))) in changed:
                    current = node
                    while current:
                        dirty.add(current)
                        current //= 2
        with clock.phase("initial_update"):
            for node in sorted(dirty, reverse=True):
                evaluate(node, 0)
        # Policies need the full current logical trace, including nodes reused
        # without comparator work. Keep this separate from evaluation counters.
        with clock.phase("trace_build"):
            events.clear()
            for node in range(size - 1, 0, -1):
                left, right = nodes[node * 2], nodes[node * 2 + 1]
                if left is not None and right is not None:
                    events.append(ComparisonEvent(0, node, tuple(sorted((left, right))), nodes[node], node not in dirty))

    with clock.phase("extraction"):
        initial_nodes = tuple(nodes)
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
    return TournamentResult(n, k, tuple(output), initial_nodes, tuple(events), evaluations)
