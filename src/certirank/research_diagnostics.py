"""Privileged, budget-faithful development diagnostics. Never an online policy."""

from itertools import combinations, product
from math import prod

from certirank.evaluation import run_algorithm
from certirank.operators.metrics import jaccard_at_k
from certirank.oracle import ExecutionLedger, validate_slot_groups
from certirank.operators.tournament import tournament
from certirank.policy import PolicyView
from certirank.value_model import ReviewValueModel, action_features


def recall(output, relevant):
    return len(set(output) & set(relevant)) / len(relevant) if relevant else None


def disagreement(outputs):
    pairs = list(combinations(outputs, 2))
    if not pairs:
        return {"set_d_em": None, "jaccard_distance": None}
    return {"set_d_em": sum(set(a) != set(b) for a, b in pairs) / len(pairs),
            "jaccard_distance": sum(1 - jaccard_at_k(a, b) for a, b in pairs) / len(pairs)}


def reachable_outputs(source, n, k, slots, fraction, *, state_cap=3000, absolute_budget=None):
    """Enumerate legal review schedules, including stop, with actual voting/repair.

    Search is exact only when the entire queue is exhausted without missing responses.
    A capped search yields achieved clairvoyant outcomes, never a global upper bound.
    """
    queue = [()]
    outputs, explored, unavailable = {}, 0, False
    while queue and explored < state_cap:
        schedule = queue.pop()
        run = run_algorithm(source, n, k, slots, "OracleSchedule", fraction,
                            schedule=schedule, absolute_budget=absolute_budget)
        explored += 1
        if run["status"] == "failed":
            unavailable = True
            continue
        key = tuple(sorted(run["topk"]))
        if key not in outputs or run["logical_calls"] < outputs[key]["logical_calls"]:
            outputs[key] = {"topk": run["topk"], "schedule": [list(pair) for pair in schedule],
                            "logical_calls": run["logical_calls"], "extra_calls": run["extra_calls"],
                            "status": run["status"]}
        if run["failure_pair"] is not None:
            unavailable = True
        if run["status"] != "complete" or run["budget_limit"] - run["extra_calls"] < 2:
            continue
        reviewed = set(schedule)
        visible = {tuple(event["pair"]) for event in run["ledger"] if event["status"] == "ok"}
        queue.extend(schedule + (pair,) for pair in sorted(visible - reviewed, reverse=True))
    return {"options": list(outputs.values()), "states_explored": explored,
            "search_complete": not queue and not unavailable, "missing_responses": unavailable,
            "remaining_schedules": len(queue)}


def budgeted_oracle(source, n, k, groups, fraction, relevant, *, state_cap=3000,
                    joint_cap=200000, quality_tolerance=0.02, absolute_budget=None):
    groups = validate_slot_groups(groups)
    bases = [run_algorithm(source, n, k, group, "Base", fraction) for group in groups]
    if any(run["status"] == "failed" for run in bases):
        return {"scope": "privileged budgeted review schedules", "deployable": False,
                "oracle": None, "search_complete": False, "reason": "baseline response unavailable",
                "base": {"set_d_em": None, "jaccard_distance": None}, "reachability": []}
    searches = [reachable_outputs(source, n, k, group, fraction, state_cap=state_cap,
                                 absolute_budget=absolute_budget) for group in groups]
    result = {"scope": "privileged budgeted review schedules; actual reserved votes and repair",
              "deployable": False, "groups": [list(g) for g in groups],
              "reachability": searches, "base": disagreement([r["topk"] for r in bases])}
    if len(groups) < 2:
        return {**result, "oracle": None, "search_complete": all(s["search_complete"] for s in searches),
                "reason": "one response group cannot measure complete-algorithm disagreement"}
    if any(not search["options"] for search in searches):
        return {**result, "oracle": None, "search_complete": False, "reason": "unavailable response group"}
    baseline_quality = sum(recall(r["topk"], relevant) for r in bases) / len(bases) if relevant else None
    best, selected, examined = None, None, 0
    total = prod(len(search["options"]) for search in searches)
    for assignment in product(*(search["options"] for search in searches)):
        if examined >= joint_cap:
            break
        examined += 1
        quality = sum(recall(r["topk"], relevant) for r in assignment) / len(assignment) if relevant else None
        if quality is not None and quality < baseline_quality - quality_tolerance - 1e-12:
            continue
        metrics = disagreement([r["topk"] for r in assignment])
        score = (metrics["set_d_em"], metrics["jaccard_distance"], sum(r["logical_calls"] for r in assignment))
        if best is None or score < best:
            best, selected = score, assignment
    exact = all(s["search_complete"] for s in searches) and examined == total
    return {**result, "oracle": ({"set_d_em": best[0], "jaccard_distance": best[1],
                                  "mean_calls": best[2] / len(groups), "selected": selected} if best is not None else None),
            "search_complete": exact, "joint_assignments_examined": examined,
            "joint_assignments_total": total, "quality_constraint": "measured" if relevant else "unavailable",
            "interpretation": "exact empirical clairvoyant optimum only if search_complete; otherwise achieved optimistic reference"}


def fit_review_value(cases, groups, fraction, settings_hash):
    """cases are development queries only; each has >=2 disjoint response groups."""
    groups = validate_slot_groups(groups)
    if len(groups) < 2:
        raise ValueError("value fitting requires independent complete execution groups")
    samples, query_groups, source_ids = {}, set(), set()
    for case in cases:
        if case["split"] != "dev":
            raise ValueError("heldout data must never fit a review value model")
        source, n, k, relevant = case["source"], case["n"], case["k"], case["relevant"]
        if not relevant:
            raise ValueError("value fitting requires independent task labels")
        query_groups.add(case["query_group"])
        source_ids.add(source.source_id)
        bases = []
        for group in groups:
            ledger = ExecutionLedger(source, group)
            result = tournament(n, k, ledger.observe)
            from certirank.repair import review_budget
            view = PolicyView(ledger.visible, ledger.reviewed, result, review_budget(ledger.base_calls, fraction))
            bases.append((view, result.topk))
        for index, (view, base_output) in enumerate(bases):
            peers = [output for j, (_, output) in enumerate(bases) if j != index]
            before = sum(set(base_output) != set(output) for output in peers) / len(peers)
            for pair, _ in view.visible:
                run = run_algorithm(source, n, k, groups[index], "OracleSchedule", fraction, schedule=(pair,), absolute_budget=2 + n * (n - 1) // 2)
                if run["status"] == "failed":
                    raise ValueError("development response pool is incomplete")
                after = sum(set(run["topk"]) != set(output) for output in peers) / len(peers)
                key = action_features(view, pair)
                samples.setdefault(key, []).append({"gain": before - after,
                    "quality_delta": recall(run["topk"], relevant) - recall(base_output, relevant),
                    "cost": run["extra_calls"], "complete": run["status"] == "complete",
                    "decision_changed": bool(run["reviews"] and run["reviews"][0]["decision_changed"]),
                    "new_branch_needed": bool(run["reviews"] and run["reviews"][0]["new_branch_needed"])})
    return ReviewValueModel({key: tuple(values) for key, values in samples.items()},
                            tuple(sorted(query_groups)), tuple(sorted(source_ids)), settings_hash)
