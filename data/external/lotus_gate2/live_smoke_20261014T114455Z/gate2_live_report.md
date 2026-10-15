# Gate 2 Live Execution — Blocked-State Report

**Run ID:** `live_smoke_20261014T114455Z`
**Date:** 2026-10-14 (UTC)
**Status:** `BLOCKED — PROVIDER CREDENTIAL INVALID`
**Git revision:** `136ae4f4a344a2f75d89f811e516dfcb0de30e46` (branch `research/dynamic-semantic-maintenance`)

---

## A. Execution Summary

| Item | Value |
|---|---|
| Planned calls this invocation | 28 (query 636, repetition 0) |
| Model completions received | **0** |
| Physical HTTP attempts | 1 (the authentication request itself) |
| Provider errors | 1 × `OracleAuthError` (fatal, non-retryable by design) |
| Tokens consumed | 0 |
| Estimated cost | $0.00 |
| Wall time | 0.5 s (excluding SciFact load) |
| Exit code | 1 (failed run recorded, not silently dropped) |

The single HTTP attempt was the first comparison's Chat Completions request. DeepSeek's
gateway rejected it with:

```
HTTP 401  {"error": {"message": "Authentication Fails, Your api key: ****c424 is invalid",
           "type": "authentication_error"}}
```

(The key suffix `c424` is DeepSeek's own masking of the error echo; no secret material
exists in any artifact — verified by scan.)

## B. Failure Diagnosis

Three independent probes, all before or besides the experiment run, converge on the same
conclusion — **the frozen `DEEPSEEK_API_KEY` in `.env.oracle` is genuinely invalid now**:

1. **Raw HTTPS, no SDK** — `GET https://api.deepseek.com/models` with `Authorization:
   Bearer <key>` → `401 Authentication Fails ... ****c424 is invalid`. This excludes
   litellm/OpenAI-client/payload-shape explanations: the endpoint rejects the credential
   itself.
2. **Key format is genuine DeepSeek** — `sk-` + 32 lowercase hex, 35 chars, ASCII, no
   surrounding whitespace. Not a truncated/corrupted value; a well-formed key that the
   server rejects.
3. **The credential worked before** — Gate 1B completed 150 real calls on 2026-09-18
   (`gate1b_pilot_state.json`: status COMPLETED, 0 provider errors) and the Gate 0 probe
   passed the same day (`model_probe.json`: PROBE_PASS, returned model `deepseek-flash`).
   The key has since been revoked, deleted, or expired at the provider side.

Alternate paths were checked and are **not** usable:

* The OpenCode Go gateway (`https://opencode.ai/zen/go/v1`, the coding agent's own
  credential) lists `deepseek-flash` but returns `403 An active OpenCode Go subscription
  is required` for chat completions — and using the agent credential would violate the
  frozen protocol regardless (the oracle layer deliberately never reads
  `OPENCODE_API_KEY`; see `tests/test_missing_key_fails_cleanly.py`).
* No `OPENROUTER_API_KEY` or other DeepSeek credential exists in any `.env` file, the
  shell environment, or git history.

The protocol freezes P0 to the DeepSeek official API (`configs/providers/deepseek.yaml`),
so switching provider or endpoint is not an option within this experiment's scope.

## C. What Was Completed Before the Block (Phase 0 audit)

| Check | Result |
|---|---|
| `git diff -- lotus/` | empty — production code untouched (re-verified after all changes) |
| Full test suite | **461 passed** (447 pre-existing + 14 new; 0 failures) |
| Offline smoke (`smoke_gate2_offline`) | SMOKE_PASS — 15/15 runs, 420/420 comparisons, all request hashes match |
| Artifact verification (`verify_gate2_artifacts`) | ALL PASS |
| Frozen workload | queries 636/327/51, Top-8 candidates each, 28 pairs each, matches spec exactly |
| Provider config | `api.deepseek.com`, `deepseek-flash`, thinking disabled, `max_tokens=128`, sampling params absent, `cache_enabled: false`, `max_retries: 3` |
| Cache disabling | three independent layers confirmed in code (LOTUS `enable_cache=False`, `OracleLM` context forcing cache off per call, `litellm.disable_cache()`) |
| Gate 1 artifacts | digests unchanged (`gate1b_retrieval_candidates.jsonl` `652afb02…`, `gate1b_pairset_manifest.json` `b3221169…`); never written |

### CLI gap found and fixed (pre-flight, before any paid call)

The audit uncovered the exact trap the task warned about: the README's documented smoke
command (`--confirm-live 420 --max-physical-attempts 84`) cannot work — the runner's own
pre-check demands headroom for all 15 runs (15 × 38 = 570 > 84) and refuses to start. A
low attempt cap can never substitute for subset selection. Implemented and tested
(`scripts/run_gate2_topk.py`, 12 new tests):

* `--queries` / `--repetitions` — explicit subset selection; unknown query ids and
  out-of-range repetitions are refused; `--confirm-live` must equal the **selection's**
  planned calls (28 for one run, 420 for the full gate).
* `--budget-calls a.jsonl,b.jsonl` + `--max-cumulative-attempts N` +
  `--max-cumulative-cost-usd X` — cross-phase cumulative guardrails (the pilot counts the
  smoke's spend). Checked before every run; a run starts only if its 28 planned calls fit
  under the cap.
* `verify_gate2_artifacts` now verifies internally-complete subset artifacts (28 rows per
  group) and only demands all 15 runs under `--require-full-plan`.

A first-attempt bug in the selection filter (it still iterated all 3 queries) was caught
by the run's own pre-flight STOP message — zero paid calls were spent on it — and fixed;
all 461 tests pass after the fix.

## D. Operator / Query / Propagation Results

**Not produced.** Gate 2A live data (Phases 1–2), the Gate 2A analysis (Phase 3), and the
Gate 2B propagation analysis (Phase 4) all require real model responses. None were
simulated, substituted, or fabricated. The offline smoke results (deterministic
ScriptedOracle) say nothing about model nondeterminism by construction and are not
presented as evidence.

## E. Research Interpretation

No new scientific claim is supportable from this session. Gate 1's prior findings
(20/30 and 23/30 pairs flipping across five repeats under an identical request hash)
remain the standing evidence for operator-level nondeterminism; whether it propagates to
Top-K instability is exactly the open question Gate 2A was designed to answer and remains
unanswered.

## F. Limitations

Unchanged from the experiment plan (3 queries, 8 candidates, single relevant document per
query, `sem_topk` only, single model/configuration) — and this session adds: **zero live
Gate 2 data collected**, so nothing here constrains the answer either way.

## G. Reproducibility & Recovery Plan

### Reproduce this blocked state (zero cost)

```bash
V=./.venv/Scripts/python.exe
$V -m pytest experiments/lotus_nondeterminism/tests/ -q          # 461 passed
$V -m experiments.lotus_nondeterminism.scripts.verify_gate2_artifacts   # ALL PASS
# the auth failure replays with any HTTP client against api.deepseek.com/models
```

### Unblock (requires user action: a valid DeepSeek API key)

1. Create/enable a key at platform.deepseek.com and write it to `lotus-code/.env.oracle`:
   `DEEPSEEK_API_KEY=sk-...` (file already git-ignored; never commit it).
2. Zero-cost validity check: `GET https://api.deepseek.com/models` with
   `Authorization: Bearer <key>` must return 200 and list `deepseek-flash`.
3. **Phase 1** — live smoke in a **fresh run-id directory** (this blocked directory stays
   as evidence; the runner's resume treats any recorded run — including a failed one — as
   done, so never point `--runs-out` back at this file):
   ```bash
   RUN=<new run id>
   $V -m experiments.lotus_nondeterminism.scripts.run_gate2_topk \
      --env-file .env.oracle --queries 636 --repetitions 0 \
      --calls-out experiments/lotus_nondeterminism/gate2/artifacts/live_smoke_${RUN}/calls.jsonl \
      --runs-out  experiments/lotus_nondeterminism/gate2/artifacts/live_smoke_${RUN}/runs.jsonl \
      --manifest-out experiments/lotus_nondeterminism/gate2/artifacts/live_smoke_${RUN}/manifest.json \
      --confirm-live 28 --max-physical-attempts 38
   ```
   Success criteria: 28/28 request hashes match the frozen workload, replay consistent,
   Top-3 returned, verify passes on the subset artifacts.
4. **Phase 2** — full pilot (420 calls) with the cumulative guardrails counting the smoke:
   ```bash
   $V -m experiments.lotus_nondeterminism.scripts.run_gate2_topk \
      --env-file .env.oracle --confirm-live 420 \
      --budget-calls experiments/lotus_nondeterminism/gate2/artifacts/live_smoke_${RUN}/calls.jsonl \
      --max-cumulative-attempts 490 --max-cumulative-cost-usd 0.50 \
      --max-physical-attempts 490
   ```
5. **Phases 3–4** — offline, no further calls:
   `analyze_gate2_topk`, `replay_gate2`, `verify_gate2_artifacts --require-full-plan`.

### Artifacts produced this session

| Path | Content |
|---|---|
| `live_smoke_20261014T114455Z/gate2_live_smoke_runs.jsonl` | 1 run record: status `error`, error preserved verbatim, 0 calls |
| `live_smoke_20261014T114455Z/gate2_live_smoke_manifest.json` | invocation manifest incl. budget accounting ($0.00) |
| `live_smoke_20261014T114455Z/gate2_live_report.md` | this report |
| `live_smoke_20261014T114455Z/gate2_live_blocked_state.json` | machine-readable diagnostic evidence |
| `scripts/run_gate2_topk.py` | subset selection + cumulative budget guardrails (new) |
| `scripts/verify_gate2_artifacts.py` | subset-aware verification (new `--require-full-plan`) |
| `tests/test_gate2.py` | +14 tests covering the above |
| `gate2/README.md` | corrected smoke command (the old one could never run) |
