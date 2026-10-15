"""Conditional single-update analysis; missing trace information stays unresolved."""

from collections.abc import Mapping

from certirank.operators.tournament import tournament
from certirank.trace import Pair, TournamentResult, validate_pair


class UnobservedComparison(RuntimeError):
    def __init__(self, pair: Pair):
        self.pair = pair


def conditional_effect(result: TournamentResult, observed: Mapping[Pair, bool], pair: Pair) -> dict:
    """Flip one observed decision while freezing all other observed decisions.

    Completing a replay proves a conditional effect only under these fixed decisions.
    The first missing comparison stops the analysis; no transitivity is assumed.
    """
    if pair not in observed:
        raise ValueError("only observed comparisons may be analyzed online")
    for key, verdict in observed.items():
        validate_pair(key, result.n)
        if type(verdict) is not bool:
            raise ValueError("conditional analysis requires boolean observed decisions")
    for event in result.events:
        if event.pair not in observed or (event.pair[0] if observed[event.pair] else event.pair[1]) != event.winner:
            raise ValueError("observations do not match the supplied complete execution trace")

    def compare(i: int, j: int) -> bool:
        key = (i, j)
        if key not in observed:
            raise UnobservedComparison(key)
        return not observed[key] if key == pair else observed[key]

    try:
        changed = tournament(result.n, result.k, compare)
    except UnobservedComparison as exc:
        return {"status": "unresolved", "first_missing_pair": list(exc.pair)}
    return {
        "status": "conditional_impact" if set(changed.topk) != set(result.topk) else "conditional_safe",
        "first_missing_pair": None,
    }
