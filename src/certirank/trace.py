"""Immutable execution events; pair keys always preserve frozen presentation order."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from time import process_time_ns

Pair = tuple[int, int]


def validate_pair(pair: Pair, n: int | None = None) -> None:
    if (not isinstance(pair, tuple) or len(pair) != 2
            or any(type(index) is not int for index in pair)
            or not 0 <= pair[0] < pair[1]
            or (n is not None and pair[1] >= n)):
        raise ValueError("pair must contain two integer indices in frozen i < j order")


@dataclass
class ExecutionWork:
    node_evaluations: int = 0
    measure_cpu: bool = False
    cpu_ns: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str):
        start = process_time_ns() if self.measure_cpu else 0
        try:
            yield
        finally:
            if self.measure_cpu:
                self.cpu_ns[name] = self.cpu_ns.get(name, 0) + process_time_ns() - start


@dataclass(frozen=True)
class ComparisonEvent:
    stage: int
    node: int
    pair: Pair
    winner: int
    reused_node: bool = False


@dataclass(frozen=True)
class TournamentResult:
    n: int
    k: int
    topk: tuple[int, ...]
    initial_nodes: tuple[int | None, ...]
    events: tuple[ComparisonEvent, ...]
    node_evaluations: int
