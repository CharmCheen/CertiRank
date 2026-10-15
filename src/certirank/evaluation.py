"""Complete-algorithm replay using reserved response groups and visible policies."""

from collections import Counter
from dataclasses import asdict
from fractions import Fraction
from itertools import combinations
import statistics
from time import process_time_ns

from certirank.operators.metrics import jaccard_at_k
from certirank.operators.tournament import tournament
from certirank.oracle import BudgetExhausted, ExecutionLedger, ResponseUnavailable, TableSource, validate_slot_groups
from certirank.policy import PolicyView, select_pair
from certirank.repair import ReviewSession, review_budget
from certirank.trace import ExecutionWork
from certirank.value_model import action_features


METHODS = ("Base", "Random", "SimpleTopology", "TraceSensitivity", "Vote3", "ReviewValue")


def run_algorithm(source: TableSource, n: int, k: int, slots: tuple[int, ...], method: str,
                  fraction: str, *, mode: str = "incremental", seed: int = 0,
                  query_id: str | None = None, model=None, absolute_budget: int | None = None,
                  measure_cpu: bool = False, schedule: tuple | None = None) -> dict:
    cpu_start = process_time_ns() if measure_cpu else 0
    if (method not in METHODS and not (method == "OracleSchedule" and schedule is not None)) or mode not in ("incremental", "cached_replay"):
        raise ValueError("unsupported method or repair mode")
    if type(seed) is not int or (query_id is not None and (not isinstance(query_id, str) or not query_id)):
        raise ValueError("seed must be integer; query_id must be a nonempty string when supplied")
    if method == "ReviewValue" and model is None:
        raise ValueError("ReviewValue is unavailable without a development-fitted model")
    if absolute_budget is not None and (type(absolute_budget) is not int or absolute_budget < 0):
        raise ValueError("invalid absolute diagnostic budget")
    review_budget(0, fraction)
    validate_slot_groups((slots,))
    ledger = ExecutionLedger(source, slots)
    work = ExecutionWork(measure_cpu=measure_cpu)
    reviews = []
    selections = []
    pending_pair = None
    budget_limit = None
    result = None
    try:
        if method in ("Base", "Vote3"):
            result = tournament(n, k, ledger.observe_vote3 if method == "Vote3" else ledger.observe, work=work)
            outcome_status, reason = "complete", "reference"
        else:
            session = ReviewSession(n, k, ledger, fraction, mode=mode, work=work, absolute_budget=absolute_budget)
            step = 0
            while True:
                value_estimate = None
                with work.phase("policy"):
                    if schedule is not None:
                        pair = schedule[step] if step < len(schedule) else None
                    else:
                        view = PolicyView(ledger.visible, ledger.reviewed, session.result, ledger.remaining)
                        pair = select_pair(view, method, seed=seed + step, model=model)
                        if method == "ReviewValue" and pair is not None:
                            value_estimate = model.estimate(action_features(view, pair), view.remaining)
                if pair is None:
                    break
                selections.append(pair)
                previous_output = session.result.topk
                start_calls, start_events = ledger.extra_calls, len(ledger.events)
                before = dict(ledger.visible).get(pair)
                outcome = session.review(pair)
                action_events = ledger.events[start_events:]
                probes = [e for e in action_events if e.phase == "probe" and e.status == "ok"]
                repair_events = [e for e in action_events if e.phase == "repair"]
                after = dict(ledger.visible).get(pair) if len(probes) == 2 else None
                reviews.append({"pair": list(pair), "value_estimate": value_estimate, "probe_completed": len(probes) == 2,
                                "before": before, "majority": after,
                                "decision_changed": after is not None and before != after,
                                "set_changed": set(previous_output) != set(session.result.topk),
                                "new_branch_needed": bool(repair_events) or outcome.blocked_pair is not None,
                                "repair_calls": len(repair_events), "cost": ledger.extra_calls - start_calls,
                                "status": outcome.status, "reason": outcome.reason,
                                "blocked_pair": list(outcome.blocked_pair) if outcome.blocked_pair else None})
                if outcome.status == "aborted":
                    break
                step += 1
            result = session.result
            outcome_status, reason = session.outcome.status, session.outcome.reason
            pending_pair = session.outcome.pending_pair
            budget_limit = ledger.extra_limit
    except (BudgetExhausted, ResponseUnavailable) as exc:
        # No complete baseline exists, so retain the failed row without inventing
        # an empty or partial Top-K. All earlier successful/failed reads stay charged.
        outcome_status, reason = "failed", str(exc)
    failure_pair = next((event.pair for event in reversed(ledger.events) if event.status != "ok"), None)
    record = {"method": method, "repair_mode": mode, "slots": list(slots), "budget_fraction": fraction,
            "source_id": source.source_id, "query_id": query_id, "n": n, "k": k,
            "operator_version": "fixed_leaf_tournament_v1", "policy_seed": seed,
            "budget_applies": method not in ("Base", "Vote3"), "absolute_budget_diagnostic": absolute_budget, "budget_limit": budget_limit, "topk": list(result.topk) if result is not None else None,
            "status": outcome_status, "reason": reason,
            "pending_pair": list(pending_pair) if pending_pair is not None else None,
            "failure_pair": list(failure_pair) if failure_pair is not None else None,
            "logical_calls": ledger.base_calls + ledger.extra_calls,
            "base_calls": ledger.base_calls, "extra_calls": ledger.extra_calls,
            "cache_hits": ledger.cache_hits, "selected_pairs": selections,
            "completed_reviews": sum(len([e for e in ledger.events if e.pair == pair and e.phase == "probe" and e.status == "ok"]) == 2 for pair in ledger.reviewed),
            "executor_node_evaluations_including_aborted": work.node_evaluations,
            "policy_cpu_time_measured": measure_cpu, "reviews": reviews,
            "cpu_ns": None,
            "ledger": [asdict(e) for e in ledger.events], "new_physical_calls": 0}
    if measure_cpu:
        record["cpu_ns"] = {**work.cpu_ns, "total": process_time_ns() - cpu_start}
    return record


def summarize_disagreement(runs: list[dict]) -> dict:
    """One source/query/config only; aborted valid fallbacks remain in the denominator.

    A baseline failure has no valid output and blocks the aggregate estimate. Its row
    and costs are still retained; no success-only metric silently replaces the target.
    """
    validate_slot_groups([r["slots"] for r in runs])
    settings = set()
    counts = Counter()
    for run in runs:
        required = ("source_id", "query_id", "n", "k", "operator_version", "method", "budget_fraction", "repair_mode", "status", "topk")
        if any(key not in run for key in required) or not isinstance(run["source_id"], str) or not run["source_id"]:
            raise ValueError("run provenance/configuration metadata is missing")
        n, k = run["n"], run["k"]
        if type(n) is not int or type(k) is not int or not 1 <= k <= n:
            raise ValueError("invalid output dimensions")
        review_budget(0, run["budget_fraction"])
        settings.add((run["source_id"], run["query_id"], n, k, run["operator_version"],
                      run["method"], Fraction(run["budget_fraction"]), run["repair_mode"], run.get("absolute_budget_diagnostic")))
        status = run["status"]
        if status not in ("complete", "aborted", "failed"):
            raise ValueError("unknown execution status")
        counts[status] += 1
        output = run["topk"]
        if status == "failed":
            if output is not None:
                raise ValueError("a baseline failure cannot have a fabricated output")
        elif (not isinstance(output, (tuple, list)) or len(output) != k
              or any(type(index) is not int or not 0 <= index < n for index in output)
              or len(set(output)) != k):
            raise ValueError("completed/fallback outputs must contain k distinct valid candidates")
    if len(settings) != 1:
        raise ValueError("aggregate only one source/query/operator/method/budget/repair configuration")
    summary = {"executions": len(runs), "complete_executions": counts["complete"],
               "aborted_executions": counts["aborted"], "failed_executions": counts["failed"],
               "dependent_run_pairs": 0, "set_d_em": None, "jaccard_distance": None}
    if counts["failed"]:
        return {**summary, "reason": "baseline failure retained; no complete-output metric can be computed for all runs"}
    pairs = list(combinations(runs, 2))
    if not pairs:
        return {**summary, "reason": "fewer than two disjoint complete-algorithm response groups"}
    return {**summary, "dependent_run_pairs": len(pairs),
            "set_d_em": statistics.mean(set(a["topk"]) != set(b["topk"]) for a, b in pairs),
            "jaccard_distance": statistics.mean(1 - jaccard_at_k(a["topk"], b["topk"]) for a, b in pairs)}
