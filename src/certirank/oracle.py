"""Offline response source and budgeted per-execution ledger. No network support.

Only the ledger owns the source. Policies receive immutable visible state. Reading a
previously unseen pair costs a logical call even when a global offline table has it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json

from certirank.trace import Pair, validate_pair


class BudgetExhausted(RuntimeError):
    def __init__(self, message: str, pair: Pair | None = None):
        super().__init__(message)
        self.pair = pair


class ResponseUnavailable(RuntimeError):
    pass


def validate_slot_groups(groups: Sequence[Sequence[int]]) -> tuple[tuple[int, ...], ...]:
    """Disjoint triples reserve initial + two explicit review responses per execution."""
    result = tuple(tuple(g) for g in groups)
    flat = [slot for g in result for slot in g]
    if not result or any(len(g) != 3 for g in result):
        raise ValueError("each execution must reserve exactly three response slots")
    if any(type(slot) is not int or slot < 0 for slot in flat) or len(set(flat)) != len(flat):
        raise ValueError("response slots must be nonnegative and disjoint across executions")
    return result


class TableSource:
    def __init__(self, responses: Mapping[Pair, Sequence[bool]], *, context_id: str = "synthetic"):
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("context_id must identify the query/request configuration")
        for pair in responses:
            validate_pair(pair)
        self._responses = {pair: tuple(values) for pair, values in responses.items()}
        if any(type(value) is not bool for values in self._responses.values() for value in values):
            raise ValueError("response table must contain boolean answers")
        payload = [context_id, [(pair, values) for pair, values in sorted(self._responses.items())]]
        self._source_id = sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    @property
    def source_id(self) -> str:
        return self._source_id

    def read(self, pair: Pair, slot: int) -> bool:
        validate_pair(pair)
        if type(slot) is not int or slot < 0:
            raise ValueError("response slot must be a nonnegative integer")
        try:
            return self._responses[pair][slot]
        except (KeyError, IndexError) as exc:
            raise ResponseUnavailable(f"missing response for {pair}, slot {slot}") from exc


@dataclass(frozen=True)
class LedgerEvent:
    pair: Pair
    slot: int
    phase: str
    status: str
    verdict: bool | None
    extra: bool


class ExecutionLedger:
    def __init__(self, source: TableSource, slots: Sequence[int]):
        slots = tuple(slots)
        if len(slots) not in (1, 3) or len(set(slots)) != len(slots) or any(type(s) is not int or s < 0 for s in slots):
            raise ValueError("reserve one baseline slot or three distinct review slots")
        self._source = source
        self._slots = slots
        self._answers: dict[Pair, list[bool]] = {}
        self._decisions: dict[Pair, bool] = {}
        self._events: list[LedgerEvent] = []
        self._reviewed: set[Pair] = set()
        self.extra_limit: int | None = None
        self.base_calls = 0
        self.extra_calls = 0
        self.cache_hits = 0
        self._mode: str | None = None
        self._failed = False

    @property
    def pristine(self) -> bool:
        return self._mode is None and self.extra_limit is None and not self._events and not self._decisions

    @property
    def slots(self) -> tuple[int, ...]:
        return self._slots

    def _claim_mode(self, mode: str) -> None:
        if self._failed:
            raise ValueError("a response-failed ledger is terminal")
        if self._mode is not None and self._mode != mode:
            raise ValueError("single-response and Vote3 execution cannot share a ledger")
        self._mode = mode

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    @property
    def visible(self) -> tuple[tuple[Pair, bool], ...]:
        return tuple(sorted(self._decisions.items()))

    @property
    def reviewed(self) -> frozenset[Pair]:
        return frozenset(self._reviewed)

    @property
    def remaining(self) -> int:
        return 0 if self.extra_limit is None else self.extra_limit - self.extra_calls

    def start_review(self, extra_limit: int) -> None:
        if self.extra_limit is not None or type(extra_limit) is not int or extra_limit < 0:
            raise ValueError("set the nonnegative review budget once after baseline execution")
        if len(self._slots) != 3:
            raise ValueError("review requires three reserved response slots")
        self._claim_mode("single")
        self.extra_limit = extra_limit

    def _read(self, pair: Pair, index: int, phase: str) -> bool:
        extra = self.extra_limit is not None
        if extra:
            if self.remaining <= 0:
                raise BudgetExhausted("review/repair budget exhausted", pair)
            self.extra_calls += 1
        else:
            self.base_calls += 1
        try:
            if index >= len(self._slots):
                raise ResponseUnavailable("review requires a reserved three-slot group")
            slot = self._slots[index]
            value = self._source.read(pair, slot)
        except ResponseUnavailable:
            self._failed = True
            self._events.append(LedgerEvent(pair, self._slots[index] if index < len(self._slots) else -1, phase, "unavailable", None, extra))
            raise
        self._events.append(LedgerEvent(pair, slot, phase, "ok", value, extra))
        return value

    def observe(self, i: int, j: int) -> bool:
        pair = (i, j)
        validate_pair(pair)
        self._claim_mode("single")
        if pair in self._decisions:
            self.cache_hits += 1
            return self._decisions[pair]
        value = self._read(pair, 0, "base" if self.extra_limit is None else "repair")
        self._answers[pair] = [value]
        self._decisions[pair] = value
        return value

    def probe(self, pair: Pair) -> tuple[bool, bool]:
        validate_pair(pair)
        if self.extra_limit is None or pair not in self._decisions or pair in self._reviewed:
            raise ValueError("probe requires an observed, not previously reviewed pair")
        self._claim_mode("single")
        if self.remaining < 2:
            raise BudgetExhausted("two calls are required to complete a review")
        self._reviewed.add(pair)
        before = self._decisions[pair]
        votes = self._answers[pair]
        votes.append(self._read(pair, 1, "probe"))
        votes.append(self._read(pair, 2, "probe"))
        after = sum(votes) >= 2
        self._decisions[pair] = after
        return before, after

    def observe_vote3(self, i: int, j: int) -> bool:
        """High-cost reference: vote on every encountered request, count all calls."""
        validate_pair((i, j))
        if self.extra_limit is not None:
            raise ValueError("Vote3 must run as a separate reference execution")
        if len(self._slots) != 3:
            raise ValueError("Vote3 requires three reserved response slots")
        self._claim_mode("vote3")
        pair = (i, j)
        if pair in self._decisions:
            if len(self._answers[pair]) != 3:
                raise ValueError("cannot mix single-response and Vote3 execution")
            self.cache_hits += 1
            return self._decisions[pair]
        votes = [self._read(pair, index, "vote3") for index in range(3)]
        self._answers[pair] = votes
        self._decisions[pair] = sum(votes) >= 2
        return self._decisions[pair]
