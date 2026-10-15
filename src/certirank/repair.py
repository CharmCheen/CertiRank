"""Review transactions retain the last complete output if repair cannot finish."""

from dataclasses import dataclass
from fractions import Fraction

from certirank.operators.tournament import tournament
from certirank.oracle import BudgetExhausted, ExecutionLedger, ResponseUnavailable
from certirank.trace import ExecutionWork, Pair, TournamentResult


def review_budget(base_calls: int, fraction: str) -> int:
    if type(base_calls) is not int or base_calls < 0 or not isinstance(fraction, str):
        raise ValueError("base_calls must be nonnegative integer; fraction must be an exact string")
    try:
        value = Fraction(fraction)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("invalid exact budget fraction") from exc
    if value < 0:
        raise ValueError("budget fraction must be nonnegative")
    return base_calls * value.numerator // value.denominator


@dataclass(frozen=True)
class ReviewOutcome:
    status: str
    reason: str
    result: TournamentResult
    pending_pair: Pair | None = None
    blocked_pair: Pair | None = None


class ReviewSession:
    def __init__(self, n: int, k: int, ledger: ExecutionLedger, fraction: str, *,
                 mode: str = "incremental", work: ExecutionWork | None = None,
                 absolute_budget: int | None = None):
        if mode not in ("incremental", "cached_replay"):
            raise ValueError("unknown repair mode")
        review_budget(0, fraction)  # configuration errors must not consume responses
        if absolute_budget is not None and (type(absolute_budget) is not int or absolute_budget < 0):
            raise ValueError("absolute diagnostic budget must be a nonnegative integer")
        if not ledger.pristine or len(ledger.slots) != 3:
            raise ValueError("a review session requires a fresh ledger with three reserved slots")
        self.ledger = ledger
        self.mode = mode
        self.work = work if work is not None else ExecutionWork()
        self.result = tournament(n, k, ledger.observe, work=self.work)
        ledger.start_review(review_budget(ledger.base_calls, fraction) if absolute_budget is None else absolute_budget)
        self.outcome = ReviewOutcome("complete", "base", self.result)

    def review(self, pair: Pair) -> ReviewOutcome:
        if self.outcome.status == "aborted":
            raise ValueError("an aborted session cannot continue")
        previous = self.result
        try:
            with self.work.phase("probe"):
                before, after = self.ledger.probe(pair)
            if before != after:
                self.result = tournament(
                    previous.n, previous.k, self.ledger.observe,
                    previous=previous if self.mode == "incremental" else None,
                    changed_pairs=(pair,),
                    work=self.work,
                )
        except (BudgetExhausted, ResponseUnavailable) as exc:
            self.outcome = ReviewOutcome("aborted", str(exc), previous, pair, getattr(exc, "pair", None))
        else:
            self.outcome = ReviewOutcome("complete", "reviewed", self.result)
        return self.outcome
