"""Validated experiment settings shared by historical, development and heldout runs."""

from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path

from certirank.oracle import validate_slot_groups
from certirank.repair import review_budget


def config_hash(config: dict) -> str:
    return sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_config(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(config)
    return config


def validate_config(config: dict) -> None:
    from certirank.evaluation import METHODS
    if config.get("schema_version") != 1 or type(config.get("k")) is not int or config["k"] < 1:
        raise ValueError("unsupported experiment schema/k")
    budgets = config.get("budgets", [])
    for budget in budgets:
        review_budget(0, budget)
    if not budgets or len(set(map(Fraction, budgets))) != len(budgets) or config.get("primary_budget") not in budgets:
        raise ValueError("budgets must be unique and include the primary budget")
    if config.get("policy_repair_mode") not in ("incremental", "cached_replay"):
        raise ValueError("unknown shared policy repair mode")
    methods = config.get("methods", [])
    if len(set(methods)) != len(methods) or not {"Base", "Random", "SimpleTopology", "Vote3"} <= set(methods) or not set(methods) <= set(METHODS):
        raise ValueError("missing/duplicate/unsupported methods")
    if config.get("candidate_method") not in METHODS:
        raise ValueError("unknown candidate method")
    if type(config.get("seed")) is not int:
        raise ValueError("seed must be an integer")
    h = config["historical"]
    if type(h["response_count"]) is not int or h["response_count"] < 3:
        raise ValueError("historical response count must be >=3")
    slots = h["baseline_slots"]
    if not slots or len(set(slots)) != len(slots) or any(type(s) is not int or not 0 <= s < h["response_count"] for s in slots):
        raise ValueError("invalid historical baseline slots")
    validate_slot_groups((h["smoke_group"],))
    if max(h["smoke_group"]) >= h["response_count"]:
        raise ValueError("smoke group is outside historical responses")
    formal = config["formal"]
    if type(formal.get("response_count")) is not int or formal["response_count"] < 6:
        raise ValueError("formal response count must provide at least two triples")
    if type(formal.get("n")) is not int or formal["n"] < config["k"]:
        raise ValueError("formal N must be >=K")
    groups = validate_slot_groups(formal["slot_groups"])
    if len(groups) < 2 or max(s for g in groups for s in g) >= formal["response_count"]:
        raise ValueError("formal evaluation requires disjoint complete groups")
    for key in ("minimum_dev_groups", "minimum_heldout_groups", "bootstrap_samples"):
        if type(formal[key]) is not int or formal[key] < 2:
            raise ValueError("invalid formal sample requirement")
    collection = config["collection"]
    for key in ("dev_query_groups", "heldout_query_groups", "optional_n", "optional_query_groups"):
        if type(collection[key]) is not int or collection[key] < 1:
            raise ValueError("invalid collection plan")
    if collection["dev_query_groups"] < formal["minimum_dev_groups"] or collection["heldout_query_groups"] < formal["minimum_heldout_groups"]:
        raise ValueError("collection plan falls below the frozen sample requirements")
    for cap in config["diagnostics"]["absolute_budgets"]:
        if type(cap) is not int or cap < 0:
            raise ValueError("diagnostic budgets must be nonnegative integers")
    for key in ("oracle_state_cap", "oracle_joint_cap"):
        if type(config["diagnostics"][key]) is not int or config["diagnostics"][key] < 1:
            raise ValueError("invalid oracle search cap")
    for n in config["diagnostics"]["synthetic_sizes"] + config["benchmark"]["sizes"]:
        if type(n) is not int or n < config["k"]:
            raise ValueError("diagnostic N must be >=K")
    for key in ("repetitions", "warmups"):
        if type(config["benchmark"][key]) is not int or config["benchmark"][key] < 1:
            raise ValueError("invalid benchmark repetition count")


def balanced_triples(response_count: int) -> list[tuple[int, int, int]]:
    """Every initial slot receives C(R-1,2) pairs of review slots; still correlated."""
    from itertools import combinations
    return [(initial, *review) for initial in range(response_count)
            for review in combinations([s for s in range(response_count) if s != initial], 2)]


def frozen_settings(config: dict) -> dict:
    result = deepcopy(config)
    # Operational freeze path does not change the settings being frozen.
    result["formal"]["method_freeze"] = None
    return result
