# Gate 2 Live Experiment Report — Operator Nondeterminism → Query Instability

**Run ID:** `live_pilot_20261014T132233Z` (smoke: `live_smoke_20261014T131841Z`)
**Date:** 2026-10-14 (UTC)
**Git revision:** `136ae4f4a344a2f75d89f811e516dfcb0de30e46` (branch `research/dynamic-semantic-maintenance`)
**Status:** COMPLETE — 15/15 runs, 420/420 planned calls, 0 failures
**Condition:** P0 semantics on a **retargeted endpoint** (protocol deviation, recorded below)

---

## A. Execution Summary

| Item | Value |
|---|---|
| Planned calls (pilot) | 420 = 3 queries × 28 pairs × 5 repetitions |
| Live smoke (Phase 1) | 28 calls (query 636, rep 0), all verified before the pilot started |
| Model completions received | 420/420 (100%), 0 provider errors, 0 retries |
| Invalid parses | 0 (420/420 strict-valid) |
| Prompt / completion tokens | 327,530 / 1,260 |
| Provider-side cache-hit tokens | 262,016 (80.0%) — billing-layer prompt caching, see §A.1 |
| Cumulative physical attempts | 448 (420 pilot + 28 smoke) — cap 490 |
| Estimated cost (peak rate) | $0.0304 pilot, $0.0343 cumulative — cap $0.50 |
| Actual API time | ≈ 418 s of API latency across all runs (~1 s/call) |
| Wall time | ≈ 5.7 h — dominated by one hung request, see §A.2 |

**Endpoint deviation.** The frozen P0 condition (`deepseek-flash` @ `api.deepseek.com`) was
unavailable this session: the stored key was rejected by DeepSeek (401 invalid; evidence in
`live_smoke_20261014T114455Z/gate2_live_blocked_state.json`). The experiment was retargeted
via the single runtime config (`configs/providers/provider_runtime.yaml`) to:

* base_url `https://llmapi.paratera.com` (Paratera OpenAI-compatible gateway)
* model `DeepSeek-V4.1-Flash` (gateway's id for the same underlying model family)
* thinking disabled via `extra_body {"thinking":{"type":"disabled"}}` — confirmed emitted
* sampling parameters absent (P0 wire semantics preserved: no temperature/top_p/penalties/tools)
* max_tokens 128; LOTUS/litellm/oracle caching disabled as in P0

Request semantics therefore differ from the original P0 hashes in exactly three identity
fields (model id, base_url host, litellm routing string). The Gate 2 workload was rebuilt
offline (zero API calls) under the new endpoint before the pilot; the Gate 2B baseline
(pair selection, candidates, ground truth) is unchanged. Gate 1B's frozen artifacts were
never touched. **All conclusions below apply to this condition**, and this run must not be
pooled with the original-P0 Gate 1 data as if it were the same condition.

### A.1 Provider prompt caching did not suppress the measurement

The gateway billed 80% of prompt tokens as cache hits (262,016/327,530). This is prompt-prefix
caching for billing/compute reuse; it does not return cached *outputs*. Evidence: all 29 pairs
that flipped across repetitions had cache-hit billing on their later calls and still returned
different verdicts. The trace records `cached_prompt_tokens` per call and every row carries
`cache_hit: false` for the operator-level cache (LOTUS response cache was off).

### A.2 One hung request (recorded, not retried)

`G2-51-r4` pair-position 25: a single transport attempt returned after 20,137 s (~5.6 h) with
a normal response. The 120 s timeout did not fire (the gateway held the connection). No retry
occurred (attempts=1) and the response is valid, so the data point stands; wall-clock timings
in this report exclude nothing and the anomaly is preserved in the raw trace.

---

## B. Operator-Level Nondeterminism (per query)

| Metric | 636 | 327 | 51 | macro |
|---|---|---|---|---|
| Valid pairs | 28/28 | 28/28 | 28/28 | — |
| Ever-flipped pair ratio | 28.6% (8/28) | 32.1% (9/28) | 42.9% (12/28) | **34.5%** |
| Mean pairwise run disagreement | 13.6% | 15.7% | 20.7% | **16.7%** |
| Invalid parse rate | 0% | 0% | 0% | 0% |
| Provider error / retry rate | 0% | 0% | 0% | 0% |

**Ever-flipped ≠ run-pair disagreement.** 34.5% is "this pair produced ≥2 distinct valid
verdicts across the 5 runs"; 16.7% is "for two specific runs, that fraction of pairs
disagreed". They answer different questions (the second is the closer analogue of
P(two independent runs disagree)); the 10 run-pair values per query range 7.1%–25.0% and are
dependent samples (shared runs) — descriptive only.

Consistent with Gate 1 (20/30 and 23/30 pairs flipping on the official endpoint), the
operator layer is nondeterministic here too: roughly a third of pairs flip across five
identical requests, with zero parse failures to explain it away.

## C. Query-Level Instability (per query, 5 runs each)

| Metric | 636 | 327 | 51 | macro |
|---|---|---|---|---|
| Distinct ordered Top-3 | 3 | 3 | 2 | — |
| Distinct Top-3 sets | 3 | 2 | 1 | — |
| Top-3 set exact agreement | 20% | 60% | 100% | **60%** |
| Ordered Top-3 exact agreement | 20% | 30% | 40% | **30%** |
| Mean Jaccard@3 | 0.60 | 0.80 | 1.00 | **0.80** |
| Mean Kendall τ-b (full 8-doc) | 0.743 | 0.714 | 0.786 | **0.748** |
| nDCG@3 per run | 1.0 ×5 | 1.0 ×5 | 1.0 ×5 | **1.0** |

All fifteen real Top-3s (rank 1 → 3):

* **636**: r0/r3 `[24294572, 15113221, 42708716]`; r1 `[24294572, 42708716, 19752008]`; r2/r4 `[24294572, 42708716, 14328288]` — membership churn at ranks 2–3 (three different documents occupy rank 2/3 across runs).
* **327**: r0 `[17997584, 22972632, 34469966]`; r1/r3/r4 `[17997584, 22972632, 5922085]`; r2 `[17997584, 5922085, 22972632]` — one membership change + one rank-2/3 swap.
* **51**: r0–r2 `[45638119, 16734530, 24341590]`; r3/r4 `[45638119, 24341590, 16734530]` — set stable, internal order flips.

**Nondeterminism propagates to the query answer.** Every query's five repetitions sent the
identical request set (request-hash sets equal across reps, verified), yet every query
produced ≥2 distinct ordered Top-3s. Set stability is not order stability: even query 51's
member set never changed, its rank-2/rank-3 order flipped in 3 of 10 run pairs.

## D. Propagation Analysis (Gate 2B, offline)

Replay gate: **15/15 runs** rebuilt from raw pair responses reproduce LOTUS's Top-K exactly.
No run was excluded; no metric below depends on a failed replay.

### D.1 Single-point sensitivity (flip each valid verdict alone, others fixed)

| Query | none | below_topk | order | membership | (of 28) |
|---|---|---|---|---|---|
| 636 | 5 | 8 | 2 | 13 | 46% of flips would change membership |
| 327 | 8 | 5 | 2 | 13 | 46% |
| 51 | 10 | 11 | 5 | 2 | 7% |

These are *deterministic relation-level* counterfactuals ("if this one vote were reversed,
what would the answer be"), **not** provider probabilities. In a 8-doc/28-pair naive sort,
a large fraction of pairs are load-bearing for the answer — the vote structure does not
insulate the Top-K.

### D.2 Multi-point propagation (only real observed verdict vectors compared)

* No jointly-emergent run pairs (0/30): every observed answer change was already visible as
  a single-flip impact from the base vector.
* **6 cancelling run pairs (6/30)**: individually-material flips whose observed combination
  left the ordered Top-K unchanged (e.g. 636 r0-vs-r3: 4 differing pairs, 3 individually
  material, combined answer identical).
* High-frequency-flip ≠ answer change: query 51 had the most flipped pairs (12/28) yet its
  Top-3 set never changed; queries 636/327 flipped less (8–9/28) but changed membership.

### D.3 Uncertainty–Impact Relationship (the central question)

| Query | high-uncertainty & high-impact | high-uncertainty & low-impact | low-uncertainty & high-impact |
|---|---|---|---|
| 636 | 6 | 2 | 9 |
| 327 | 6 | 3 | 9 |
| 51 | 2 | 10 | 5 |
| **total (84 pairs)** | **14 (17%)** | **15 (18%)** | **23 (27%)** |

* **Instability is a poor predictor of impact.** Of the 29 ever-flipped pairs, only 14 (48%)
  are single-flip high-impact; of the 46 single-flip high-impact pairs, 23 (50%) never
  flipped across five runs. A "watch what wobbles" strategy would miss half the material
  pairs and waste half its attention on inert ones.
* Query 51 is the absorption case: 10 of its 12 flipped pairs are inert (below_topk/none);
  the vote structure swallowed the operator noise without touching the answer set.
* With 5 repetitions, "ever-flipped" is a coarse estimator (a pair with p≈0.2 flip
  probability flips at least once in 5 runs only ~67% of the time, and one observed flip
  gives no rate). The counts above are descriptive; per-pair flip *rates* need far more
  repetitions to estimate.

## E. Research Interpretation

The observed pattern is **情况三: the query answer is unstable while ground-truth quality is
stable** — with a preceding 情况一-style propagation at the structural layer:

1. **Operator instability propagates.** Identical request sets produced 2–3 distinct ordered
   Top-3s per query (ordered agreement 30%). The naive sort's vote-counting does *not*
   absorb pairwise noise in general (46% of single flips change membership on 636/327).
2. **The propagation does not (here) reach quality.** Each SciFact query has exactly one
   relevant document, and it held rank 1 in all 15 runs — nDCG@3 = 1.0 throughout. The
   instability lives in ranks 2–3 and below, invisible to this metric at this K.
3. **Reproducibility ≠ quality.** Exact-match agreement (30% ordered / 60% set) and nDCG@3
   (1.0) point in opposite directions. A pipeline could look "healthy" on retrieval metrics
   while being unable to reproduce its own answer; the converse is equally possible. Any
   reliability claim must state which property it means.

**Is there enough evidence for Query-aware Selective Verification (Gate 3)?** Yes, as a
hypothesis to test, with one negative signal: impact is not concentrated where instability
is (§D.3 — 50% of material pairs were perfectly stable), so verifying only "unstable"
pairs would miss material changes, and caching only "inert" pairs is safer than it sounds
(18% of pairs were both unstable and low-impact, and query 51 shows whole queries can
absorb noise). The strongest Gate 3 targets are the 14 unstable-and-material pairs and the
membership boundary at ranks 2–3. This is pilot-level evidence (3 queries), not a
demonstration that selective verification beats any baseline.

## F. Limitations

* **3 queries is far too few** for any cross-query generalisation; every aggregate here is
  a macro over 3 items and should be read as a case study.
* **8 candidates, K=3, one relevant document per query**: nDCG@3's stability is partly an
  artifact of the single-relevant-doc design (the relevant doc sits comfortably at rank 1);
  membership churn at ranks 2–3 cannot lower nDCG when nothing relevant lives there.
* `sem_topk(naive)` only: nothing here transfers to semantic join, filter, or aggregate
  operators, or to other sort methods (quicksort pivoting, tie-voting).
* **One model, one endpoint, one config** (DeepSeek-V4.1-Flash via Paratera gateway,
  thinking disabled, max_tokens 128): no claim about other LLMs, providers, sampling
  settings, or thinking modes. The endpoint deviates from the frozen P0 (§A) — flip rates
  are not comparable to Gate 1B's official-endpoint numbers.
* Provider prompt-cache billing hit 80% of tokens; it demonstrably did not suppress output
  variation (§A.1), but billing-layer caching behaviour may differ by endpoint.
* No claim that a Query-aware cache beats any existing method — nothing was implemented or
  compared. Single-point counterfactuals are relation-level, not provider probabilities.
* One request hung 5.6 h (§A.2); excluding it would not change any metric (it completed
  normally), but transport reliability on this gateway is a practical caveat.

## G. Reproducibility

* **Git revision:** `136ae4f4a344a2f75d89f811e516dfcb0de30e46`; `git diff -- lotus/` empty
  (production code untouched; all instrumentation via the tested comparator/LM seams).
* **Config:** `configs/providers/provider_runtime.yaml` (endpoint/model/key-env; see §A) +
  `.env.oracle` (key, git-ignored). Workload rebuilt for this endpoint:
  `artifacts/gate2_workload.json` (previous frozen workload preserved as
  `gate2_workload.deepseek-official-frozen.json.bak`).
* **Raw artifacts** (this run): `gate2/artifacts/live_pilot_20261014T132233Z/` —
  `gate2_live_pair_calls.jsonl` (420 rows), `gate2_live_query_results.jsonl` (15 rows),
  `gate2_live_analysis.json`, `gate2_live_replay_analysis.json` (propagation),
  `gate2_live_artifact_verification.json`, `gate2_live_manifest.json` (budget accounting).
  Smoke: `live_smoke_20261014T131841Z/`. Blocked first attempt evidence:
  `live_smoke_20261014T114455Z/`.
* **Commands executed** (from `lotus-code/`, `V=.venv/Scripts/python.exe`):
  1. `python -m experiments.lotus_nondeterminism.scripts.build_gate2_workload` (offline, after retarget)
  2. `python -m experiments.lotus_nondeterminism.scripts.smoke_gate2_offline`
  3. `python -m experiments.lotus_nondeterminism.scripts.probe_model --env-file .env.oracle --condition P0 --output .../model_probe_paratera.json` → PROBE_PASS
  4. Smoke: `python -m ...scripts.run_gate2_topk --env-file .env.oracle --queries 636 --repetitions 0 --calls-out .../live_smoke_.../gate2_live_smoke_calls.jsonl --runs-out .../gate2_live_smoke_runs.jsonl --confirm-live 28 --max-physical-attempts 38`
  5. Pilot: `python -m ...scripts.run_gate2_topk --env-file .env.oracle --confirm-live 420 --calls-out .../gate2_live_calls.jsonl --runs-out .../gate2_live_runs.jsonl --budget-calls <smoke calls> --max-cumulative-attempts 490 --max-cumulative-cost-usd 0.50`
  6. Analysis (offline): `analyze_gate2_topk`, `replay_gate2`, `verify_gate2_artifacts --require-full-plan`
* **Offline recomputation** (zero API calls, from raw JSONL + frozen workload):
  ```bash
  V -m experiments.lotus_nondeterminism.scripts.analyze_gate2_topk \
      --calls .../live_pilot_20261014T132233Z/gate2_live_pair_calls.jsonl \
      --runs .../gate2_live_query_results.jsonl --out /tmp/gate2_analysis_check.json
  V -m experiments.lotus_nondeterminism.scripts.replay_gate2 \
      --calls .../gate2_live_pair_calls.jsonl --runs .../gate2_live_query_results.jsonl \
      --out /tmp/gate2_propagation_check.json
  ```
* **Verification status:** pilot artifacts pass every structural check (hashes, replay,
  counts, P0 wire condition, secrets scan) except the intentional
  "identical requests → identical answers" determinism check, which **fails by design on
  this data** — that failure *is* the experimental finding (the same check passes on
  scripted-oracle data, confirming the harness is deterministic and the variation is the
  model's).
* **Tests:** 465 passed (full suite, including 12 new tests for subset selection, cumulative
  budget guardrails, and provider retargeting).
