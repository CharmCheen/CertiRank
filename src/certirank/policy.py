"""Offline-ready policies consume immutable visible state, never a response source.

TraceSensitivity is an unfrozen development candidate; its usefulness is untested.
"""

from dataclasses import dataclass
import random

from certirank.diagnostics import conditional_effect
from certirank.trace import Pair, TournamentResult
from certirank.value_model import ReviewValueModel, action_features


@dataclass(frozen=True)
class PolicyView:
    visible: tuple[tuple[Pair, bool], ...]
    reviewed: frozenset[Pair]
    result: TournamentResult
    remaining: int


def select_pair(view: PolicyView, method: str, *, seed: int = 0, model: ReviewValueModel | None = None) -> Pair | None:
    if method not in ("Random", "SimpleTopology", "TraceSensitivity", "ReviewValue"):
        raise ValueError("unknown policy (Confidence unavailable without logprobs)")
    if method == "ReviewValue" and model is None:
        raise ValueError("ReviewValue requires a model fitted on independent development data")
    candidates = sorted(pair for pair, _ in view.visible if pair not in view.reviewed)
    if view.remaining < 2 or not candidates:
        return None
    if method == "ReviewValue":
        scores = [(model.score(action_features(view, pair), view.remaining), pair) for pair in candidates]
        eligible = [(score, pair) for score, pair in scores if score is not None]
        return min(eligible, key=lambda item: (-item[0], item[1]))[1] if eligible else None
    if method == "Random":
        return random.Random(seed).choice(candidates)
    distances: dict[Pair, int] = {}
    for event in view.result.events:
        distance = event.node.bit_length() - 1
        distances[event.pair] = min(distances.get(event.pair, distance), distance)

    def topology(pair: Pair) -> tuple[int, Pair]:
        return distances.get(pair, view.result.n), pair

    if method == "SimpleTopology":
        return min(candidates, key=topology)
    # CPU-only trace replays can recognize a few conditional effects. Missing
    # comparisons receive an explicit intermediate priority, never a safe label.
    observed = dict(view.visible)
    priority = {"conditional_impact": 0, "unresolved": 1, "conditional_safe": 2}
    return min(candidates, key=lambda pair: (
        priority[conditional_effect(view.result, observed, pair)["status"]], topology(pair),
    ))
