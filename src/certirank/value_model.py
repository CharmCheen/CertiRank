"""Development-only empirical review value; no fabricated probability estimates.

The target is change in agreement with other independent *baseline* executions.
It is a one-step surrogate, not a claim about the complete policy's final D_EM.
"""

from dataclasses import dataclass
import json

from certirank.diagnostics import conditional_effect


def action_features(view, pair) -> str:
    status = conditional_effect(view.result, dict(view.visible), pair)["status"]
    distances = [event.node.bit_length() - 1 for event in view.result.events if event.pair == pair]
    distance = min(distances) if distances else view.result.n
    return json.dumps([view.result.n, view.result.k, status, distance], separators=(",", ":"))


@dataclass(frozen=True)
class ReviewValueModel:
    samples: dict[str, tuple[dict, ...]]
    training_query_groups: tuple[str, ...]
    training_source_ids: tuple[str, ...]
    settings_hash: str
    minimum_samples: int = 3

    def estimate(self, features: str, remaining: int) -> dict | None:
        samples = self.samples.get(features, ())
        if len(samples) < self.minimum_samples:
            return None
        feasible = [s["complete"] and s["cost"] <= remaining for s in samples]
        gains = [s["gain"] if ok else 0.0 for s, ok in zip(samples, feasible)]
        quality = [s["quality_delta"] if ok else 0.0 for s, ok in zip(samples, feasible)]
        expected_gain = sum(gains) / (len(samples) + 2)
        expected_cost = sum(min(s["cost"], remaining) for s in samples) / len(samples)
        score = expected_gain / max(2, expected_cost) if expected_gain > 0 and sum(quality) >= 0 else None
        return {"samples": len(samples), "estimated_gain": expected_gain, "estimated_cost": expected_cost,
                "p_vote_changed": sum(s["decision_changed"] for s in samples) / len(samples),
                "p_budget_complete": sum(feasible) / len(samples),
                "estimated_quality_delta": sum(quality) / len(samples), "score": score,
                "target": "development one-step proxy; complete-policy effect unverified"}

    def score(self, features: str, remaining: int) -> float | None:
        estimate = self.estimate(features, remaining)
        return estimate["score"] if estimate is not None else None

    def to_dict(self) -> dict:
        return {"schema_version": 1, "target": "one_step_agreement_with_other_baseline_groups",
                "samples": self.samples, "training_query_groups": self.training_query_groups,
                "training_source_ids": self.training_source_ids, "settings_hash": self.settings_hash,
                "minimum_samples": self.minimum_samples,
                "limitations": "empirical dev-only surrogate; unavailable features stop selection; complete-policy benefit unverified"}

    @classmethod
    def from_dict(cls, data: dict):
        if data.get("schema_version") != 1:
            raise ValueError("unsupported value model")
        return cls({key: tuple(values) for key, values in data["samples"].items()},
                   tuple(data["training_query_groups"]), tuple(data["training_source_ids"]),
                   data["settings_hash"], data["minimum_samples"])
