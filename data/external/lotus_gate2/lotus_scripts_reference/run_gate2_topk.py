#!/usr/bin/env python
"""Run the Gate 2A live experiment: complete naive Top-K executions, repeated.

This is the only Gate 2 script that spends API budget. It refuses to start without an
explicit ``--confirm-live`` plus the exact planned call count *of the selected subset*,
re-checks the frozen workload, counts physical attempts against hard caps before every
run, and resumes without duplicating an interrupted ``(query, repetition)``.

Execution policy (recorded in the manifest): queries in frozen workload order,
repetitions 0..R-1 outer, so every query is measured once per repetition before the next
repetition begins. The comparator calls inside one run are sequential (the oracle
adapter sends one request at a time, in naive-enumeration order).

Subset selection (``--queries`` / ``--repetitions``) exists so a live smoke test can run
exactly one (query, repetition) — 28 calls — without pretending a lower
``--max-physical-attempts`` achieves that. The confirmation number always equals the
selection's planned calls, so an authorised smoke is 28 and the full gate is 420.

Cumulative budget guardrails (``--budget-calls`` + ``--max-cumulative-attempts`` /
``--max-cumulative-cost-usd``) span *phases*: the pilot counts the smoke's already-spent
attempts and tokens by pointing ``--budget-calls`` at the smoke's calls file. The cap is
enforced at run granularity: a new run starts only if the cumulative attempts plus its
28 planned comparisons still fit, so a pathological run can overshoot the cap by at most
its own retry headroom (recorded, never silently absorbed).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.lotus_nondeterminism.gate1.protocol import (  # noqa: E402
    CONDITIONS_OVERRIDES,
    append_jsonl,
    read_jsonl,
    write_json,
)
from experiments.lotus_nondeterminism.gate1.pricing import cost_scenarios  # noqa: E402
from experiments.lotus_nondeterminism.gate2 import protocol as P  # noqa: E402
from experiments.lotus_nondeterminism.gate2.runner import execute_run  # noqa: E402
from experiments.lotus_nondeterminism.gate2.workload import load_workload  # noqa: E402
from experiments.lotus_nondeterminism.gate23.protocol import RequestIndex, p0_hashing_oracle  # noqa: E402
from experiments.lotus_nondeterminism.oracle.config import load_oracle_config  # noqa: E402
from experiments.lotus_nondeterminism.oracle.env_file import load_env_file  # noqa: E402
from experiments.lotus_nondeterminism.oracle.openai_compatible import OpenAICompatibleOracle  # noqa: E402
from experiments.lotus_nondeterminism.paperpath.scifact_beir import load_paper_scifact  # noqa: E402

#: a single run can never exceed its planned comparison count (28) plus retry headroom
PER_RUN_CEILING = P.PAIRS_PER_QUERY + 10

#: the default attempt cap, derived from the plan rather than picked by hand: the planned
#: run count times the worst case a single run can burn. It must be >= PLANNED_CALLS or the
#: gate below would refuse its own experiment — an earlier default of
#: ``PLANNED_CALLS + 40`` was smaller than 15 runs x 38 and blocked the first run.
DEFAULT_MAX_ATTEMPTS = P.QUERIES_SELECTED * P.REPETITIONS * PER_RUN_CEILING


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finished_keys(runs_path: Path) -> set[tuple[str, int]]:
    done: set[tuple[str, int]] = set()
    if not runs_path.is_file():
        return done
    for row in read_jsonl(runs_path):
        done.add((str(row["query_id"]), int(row["repetition"])))
    return done


def _physical_attempts_so_far(calls_path: Path) -> int:
    if not calls_path.is_file():
        return 0
    return sum(int(row.get("transport_attempts") or 1) for row in read_jsonl(calls_path))


def parse_query_selection(arg: str | None, workload: dict[str, Any]) -> list[str]:
    """The query ids to run this invocation, in frozen workload order.

    ``None``/empty selects all. Unknown ids are refused — a typo must never silently
    drop a query from the plan.
    """
    allowed = [str(e["query_id"]) for e in workload["entries"]]
    if not arg:
        return list(allowed)
    wanted = {q.strip() for q in arg.split(",") if q.strip()}
    unknown = sorted(wanted - set(allowed))
    if unknown:
        raise ValueError(f"--queries names ids not in the frozen workload: {unknown}")
    return [q for q in allowed if q in wanted]


def parse_repetition_selection(arg: str | None) -> list[int]:
    """The repetition indices to run this invocation, ascending. ``None`` selects all."""
    if not arg:
        return list(range(P.REPETITIONS))
    wanted: set[int] = set()
    for token in arg.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if not 0 <= value < P.REPETITIONS:
            raise ValueError(f"repetition index {value} out of range 0..{P.REPETITIONS - 1}")
        wanted.add(value)
    if not wanted:
        raise ValueError("--repetitions selected nothing")
    return sorted(wanted)


def planned_calls_for(queries: Sequence[str], repetitions: Sequence[int]) -> int:
    """Planned calls of a selection: len(queries) x 28 x len(repetitions)."""
    return len(queries) * P.PAIRS_PER_QUERY * len(repetitions)


def parse_budget_calls(arg: str | None) -> list[str]:
    """Extra calls JSONL files counted toward the cumulative budget."""
    if not arg:
        return []
    return [token.strip() for token in arg.split(",") if token.strip()]


def cumulative_attempts(paths: Sequence[str | Path]) -> int:
    """Total transport attempts across calls files (missing files count zero)."""
    total = 0
    for path in paths:
        total += _physical_attempts_so_far(Path(path))
    return total


def cumulative_cost_usd(paths: Sequence[str | Path]) -> dict[str, Any]:
    """Token-summed cost across calls files, both rate scenarios."""
    prompt = completion = cached = 0
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        for row in read_jsonl(p):
            prompt += int(row.get("prompt_tokens") or 0)
            completion += int(row.get("completion_tokens") or 0)
            cached += int(row.get("cached_prompt_tokens") or 0)
    return cost_scenarios(
        input_tokens=prompt, output_tokens=completion, cached_input_tokens=cached
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-config", default=None, help="provider YAML (default: frozen deepseek.yaml)")
    parser.add_argument("--env-file", default=None, help=".env file holding DEEPSEEK_API_KEY")
    parser.add_argument("--workload", default=str(P.WORKLOAD_PATH))
    parser.add_argument("--calls-out", default=str(P.CALLS_PATH))
    parser.add_argument("--runs-out", default=str(P.RUNS_PATH))
    parser.add_argument("--manifest-out", default=str(P.ARTIFACTS / "gate2_execution_manifest.json"))
    parser.add_argument("--queries", default=None,
                        help="comma-separated subset of frozen query ids (default: all 3)")
    parser.add_argument("--repetitions", default=None,
                        help=f"comma-separated subset of repetition indices 0..{P.REPETITIONS - 1} (default: all)")
    parser.add_argument("--budget-calls", default=None,
                        help="comma-separated extra calls JSONL files counted toward the cumulative budget "
                             "(the --calls-out file is always counted)")
    parser.add_argument("--max-cumulative-attempts", type=int, default=None,
                        help="hard cap on cumulative transport attempts across all budget files; "
                             "a new run starts only if its 28 planned calls still fit under the cap")
    parser.add_argument("--max-cumulative-cost-usd", type=float, default=None,
                        help="hard cap on cumulative estimated cost (peak scenario) across all budget files")
    parser.add_argument("--max-physical-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                        help=("hard cap on transport attempts; default is the plan's worst case "
                              f"({DEFAULT_MAX_ATTEMPTS}). Lower it to spend less; never raise it "
                              "to rescue a run that hit the cap."))
    parser.add_argument("--confirm-live", metavar="N",
                        help="type the selection's planned call count to authorise spending")
    args = parser.parse_args()

    workload = load_workload(args.workload)
    if workload["planned_calls"] != P.PLANNED_CALLS:
        print(f"workload plan mismatch: file says {workload['planned_calls']}", file=sys.stderr)
        return 2

    try:
        selected_queries = parse_query_selection(args.queries, workload)
        selected_repetitions = parse_repetition_selection(args.repetitions)
    except ValueError as exc:
        print(f"REFUSING TO START: invalid selection — {exc}", file=sys.stderr)
        return 2
    planned = planned_calls_for(selected_queries, selected_repetitions)

    if args.confirm_live != str(planned):
        print(
            f"REFUSING TO START: no live authorisation.\n"
            f"This selection ({len(selected_queries)} query/queries x {P.PAIRS_PER_QUERY} pairs x "
            f"{len(selected_repetitions)} repetition(s)) is planned at {planned} physical API calls.\n"
            f"Pass --confirm-live {planned} once the budget is approved.",
            file=sys.stderr,
        )
        return 2

    calls_path = Path(args.calls_out)
    runs_path = Path(args.runs_out)
    budget_paths: list[Path] = [calls_path] + [Path(p) for p in parse_budget_calls(args.budget_calls)]
    done = _finished_keys(runs_path)
    used = _physical_attempts_so_far(calls_path)
    cum_attempts = cumulative_attempts(budget_paths)
    cum_cost = cumulative_cost_usd(budget_paths)

    selected_set = set(selected_queries)
    remaining_runs = [
        (entry, repetition)
        for repetition in selected_repetitions
        for entry in workload["entries"]
        if str(entry["query_id"]) in selected_set
        and (str(entry["query_id"]), repetition) not in done
    ]
    if not remaining_runs:
        print("nothing to do: every selected (query, repetition) is already recorded")
        return 0

    print(
        f"{len(done)} run(s) already recorded; {len(remaining_runs)} to go; "
        f"{used} attempts in this calls file; {cum_attempts} cumulative attempts across "
        f"{len(budget_paths)} budget file(s)"
    )

    stop_reason: str | None = None
    if used + len(remaining_runs) * PER_RUN_CEILING > int(args.max_physical_attempts):
        print(
            f"STOP: {used} attempts already spent and the remaining {len(remaining_runs)} runs "
            f"could cost up to {len(remaining_runs) * PER_RUN_CEILING} more; that would exceed the "
            f"{args.max_physical_attempts} cap.",
            file=sys.stderr,
        )
        return 2
    if args.max_cumulative_attempts is not None:
        projected = cum_attempts + planned_calls_for(
            selected_queries, selected_repetitions
        )
        if projected > int(args.max_cumulative_attempts):
            print(
                f"STOP: {cum_attempts} cumulative attempts already spent; the {planned} planned "
                f"calls of this selection would reach {projected}, over the "
                f"{args.max_cumulative_attempts} cumulative cap.",
                file=sys.stderr,
            )
            return 2
    if args.max_cumulative_cost_usd is not None:
        peak_so_far = float(cum_cost["peak"]["total_usd"])
        if peak_so_far >= float(args.max_cumulative_cost_usd):
            print(
                f"STOP: cumulative estimated cost already {peak_so_far:.4f} USD (peak scenario), "
                f"at/over the {args.max_cumulative_cost_usd} cap.",
                file=sys.stderr,
            )
            return 2

    if args.env_file:
        applied = load_env_file(args.env_file)
        print(f"loaded {len(applied)} allow-listed variable(s) from {args.env_file}: {', '.join(sorted(applied))}")
    config = load_oracle_config(
        provider_config=args.provider_config or str(P.P0_ORACLE_CONFIG),
        cli_overrides=dict(CONDITIONS_OVERRIDES),
        env=dict(os.environ),
    )
    config.require_api_key()
    oracle = OpenAICompatibleOracle(config)

    print("loading SciFact via the BEIR loader ...")
    data = load_paper_scifact()
    request_index = RequestIndex(p0_hashing_oracle())

    started_at = _utc_now()
    failures = 0
    for entry, repetition in remaining_runs:
        used = _physical_attempts_so_far(calls_path)
        if used + PER_RUN_CEILING > int(args.max_physical_attempts):
            stop_reason = (
                f"per-invocation attempt cap {args.max_physical_attempts} reached before "
                f"(query {entry['query_id']}, repetition {repetition})"
            )
            print(f"STOP before (query {entry['query_id']}, repetition {repetition}): {stop_reason}",
                  file=sys.stderr)
            break
        cum_attempts = cumulative_attempts(budget_paths)
        if args.max_cumulative_attempts is not None and (
            cum_attempts + P.PAIRS_PER_QUERY > int(args.max_cumulative_attempts)
        ):
            stop_reason = (
                f"cumulative attempt cap {args.max_cumulative_attempts} would be exceeded by the "
                f"next run's {P.PAIRS_PER_QUERY} planned calls (at {cum_attempts})"
            )
            print(f"STOP before (query {entry['query_id']}, repetition {repetition}): {stop_reason}",
                  file=sys.stderr)
            break
        cum_cost = cumulative_cost_usd(budget_paths)
        if args.max_cumulative_cost_usd is not None and (
            float(cum_cost["peak"]["total_usd"]) >= float(args.max_cumulative_cost_usd)
        ):
            stop_reason = (
                f"cumulative cost cap {args.max_cumulative_cost_usd} USD reached "
                f"(at {cum_cost['peak']['total_usd']:.4f} peak)"
            )
            print(f"STOP before (query {entry['query_id']}, repetition {repetition}): {stop_reason}",
                  file=sys.stderr)
            break

        outcome = execute_run(
            query_id=str(entry["query_id"]),
            repetition=repetition,
            candidates=entry,
            data=data,
            request_index=request_index,
            oracle=oracle,
        )
        run = outcome.run_record
        # independent verification against the workload's frozen hashes (never Gate 1's)
        expected = {pair["pair_position"]: pair["request_hash"] for pair in entry["pairs"]}
        hash_ok = [row["request_hash"] == expected[row["pair_position"]] for row in outcome.trace_records]
        run["request_hashes_match_workload"] = all(hash_ok) and len(hash_ok) == P.PAIRS_PER_QUERY

        for row in outcome.trace_records:
            append_jsonl(calls_path, row)
        append_jsonl(runs_path, run)
        print(
            f"[{run['run_id']}] status={run['execution_status']} "
            f"calls={run['physical_api_calls']} hashes_ok={run['request_hashes_match_workload']} "
            f"replay_ok={run['replay_consistent_with_lotus']} "
            f"topk={run['lotus_ordered_topk_doc_ids']} "
            f"invalid={run['invalid_comparisons']}"
        )
        if run["execution_status"] != "ok" or not run["replay_consistent_with_lotus"]:
            failures += 1

    cum_attempts_after = cumulative_attempts(budget_paths)
    cum_cost_after = cumulative_cost_usd(budget_paths)
    if args.max_cumulative_attempts is not None and cum_attempts_after > int(args.max_cumulative_attempts):
        # only reachable if a single run's retries overshot the cap despite the pre-check
        stop_reason = stop_reason or (
            f"CUMULATIVE CAP BREACHED WITHIN A RUN: {cum_attempts_after} > "
            f"{args.max_cumulative_attempts} attempts (recorded, not silently absorbed)"
        )
        print(f"WARNING: {stop_reason}", file=sys.stderr)

    summary = {
        "substudy": P.SUBSISTUDY,
        "started_at_utc": started_at,
        "finished_at_utc": _utc_now(),
        "selection": {
            "queries": selected_queries,
            "repetitions": selected_repetitions,
            "planned_calls_this_invocation": planned,
        },
        "max_physical_attempts": int(args.max_physical_attempts),
        "budget": {
            "files": [str(p) for p in budget_paths],
            "max_cumulative_attempts": args.max_cumulative_attempts,
            "max_cumulative_cost_usd": args.max_cumulative_cost_usd,
            "cumulative_attempts_after": cum_attempts_after,
            "cumulative_cost_after": cum_cost_after,
            "stop_reason": stop_reason,
        },
        "runs_recorded": 0,  # filled below from the runs file (a budget stop may cut the loop short)
        "failed_runs": failures,
        "execution_order": "repetitions outer, workload query order inner",
        "calls_path": str(calls_path),
        "runs_path": str(runs_path),
    }
    runs_now = len(_finished_keys(runs_path))
    summary["runs_recorded"] = runs_now - len(done)
    summary["runs_remaining_unexecuted"] = max(0, len(remaining_runs) - summary["runs_recorded"])
    write_json(Path(args.manifest_out), summary)
    print(
        f"done: {summary['runs_recorded']} runs, {failures} failure(s)"
        + (f"; stopped early: {stop_reason}" if stop_reason else "")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
