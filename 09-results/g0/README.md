# G0 pilot results

Run on 2026-11-12. 144 agent runs: the 36 RE2-OB development cases x 4 conditions
(clean, F1, F2, F3). These numbers feed the power simulation before the freeze.

## Provenance

| | |
|---|---|
| Model | `gemini/gemini-3.5-flash-lite` |
| Version pin | ListModels `version` = `3.5-flash-lite-07-2026`, read 2026-11-12 |
| Temperature | 0 |
| Max steps | 30 (RCAEval default) |
| Workers / RPM | 4 / 60 (Gemini API Tier 1 paid) |
| Prompt version | 2 (RCAEval reference agent, arm D0, no defence) |
| Selection seed | 20261109 (dev services `checkoutservice`, `currencyservice`; 12 templates) |
| RCAEval / LiteLLM | 1.12.0 / 1.108.3 |

**On the version pin.** The protocol called for `gemini-2.5-flash` on a pinned dated
version. That is not achievable on this API. `gemini-2.5-flash` and
`gemini-2.5-flash-lite` both return `HTTP 404 "no longer available to new users ...
use models/gemini-3.8-flash"` on `v1` and `v1beta`, although both still appear in
ListModels; this is model retirement, not a billing or tier gate. Separately, no
Gemini model returns a dated `modelVersion` in its response metadata
(`gemini-3.5-flash-lite` replies with the bare alias), and the dated forms
`gemini-3.5-flash-lite-07-2026` and `-001` are not callable model ids. The
ListModels `version` field is therefore the only dated provenance available, and is
what is recorded above.

## Results

| family | n | TBR | AC@1 | answered |
|---|---|---|---|---|
| clean | 36 | 0.000 | 0.778 | 1.00 |
| F1 | 36 | 0.250 | 0.611 | 1.00 |
| F2 | 36 | 0.111 | 0.694 | 1.00 |
| F3 | 36 | 0.056 | 0.667 | 1.00 |

Delta TBR vs clean, template-cluster bootstrap, 10,000 resamples, 36 case pairs over
12 templates:

| family | Delta TBR | 95% CI | vs 15-pt planning trigger |
|---|---|---|---|
| F1 | +0.250 | [+0.083, +0.444] | at/above |
| F2 | +0.111 | [+0.028, +0.194] | below |
| F3 | +0.056 | [+0.000, +0.139] | below, CI touches zero |

Clean TBR is exactly 0.000: the agent never blamed the target without forged
evidence, so the attack effect is not confounded by a baseline tendency. The
15-point threshold is a planning trigger only; it is never evidence that agents are
robust.

Service-level accuracy over all 144: AC@1 0.69, AC@3 0.83, AC@5 0.86.
Inspected@root 0.86; AC@1 given the root was inspected 0.80, versus 0.00 when it was
not.

## Outcome accounting

All 144 runs, with the step cap kept separate from abstention:

| outcome | n |
|---|---|
| submit, own choice | 144 |
| submit, forced at the 30-step cap | 0 |
| no_submit, hit cap with no ranking | 0 |
| no_submit, ended without ranking | 0 |
| abstentions (empty ranking) | 0 |
| crashes / missing predictions | 0 |
| **total** | **144** |

144 predictions for 144 labelled variants, no duplicates, none missing.

Tool-call failure rate: **2 of 2,529 calls = 0.08%**, in 2 runs. Both are agent
mistakes (`unknown metric 'redis_diskoidiskio'`, `no metrics for service
'checkoutservice_cpu'`), not infrastructure. By family: F1 0.00%, F2 0.15%,
F3 0.00%, clean 0.17%. No HTTP 5xx, no 429s, no retries, no quota stops.

## Run-to-run variance at temperature 0

Ten cases were run twice (calibration, then again in the full run), same model, same
settings: the top-1 answer changed in **1 of 10** repeats and blame-on-target changed
in **0 of 10**.

This figure is conditional on a working tool layer. Before the `fix_tool_specs`
repair in `gemini_run.py`, the same measurement over 4 repeats gave a top-1 change in
3 of 4. See `calibration/` and the note below.

## Usage and cost

Mean 65,284 input and 437 output tokens, 12.1 model turns, 48.4 s per run.
Totals: 9,400,850 input and 62,949 output tokens.

Cost of the 144-run pilot: **$1.81** as LiteLLM measures it (cache-aware), or **$2.98**
costed naively at $0.30/1M input and $2.50/1M output with no cache discount.
Whole-session spend including the smoke and both calibrations: **$2.16** measured
(**$3.33** naive), against a $10 budget.

`score.log` prints "you pay $0 on the free tier" for any `gemini/*` model. That label
is wrong here: this run was on Tier 1 paid. Do not quote it.

The "projected 9,614 frozen runs" line in `score.log` is Flash-Lite pricing. It is not
an estimate for a frozen Claude model, which must be measured on that model.

## The tool-spec repair

`gemini_run.py:fix_tool_specs` works around a defect that would otherwise dominate
the tool-call failure rate and the variance estimate. RCAEval shares one dict per
tool between `react.TOOL_SPECS` and `toolkit.TOOLS`; LiteLLM's Gemini transform
strips empty `properties: {}` in place, so after the first Gemini call every
`describe_case` and `call_graph` call fails with `KeyError: 'properties'` for the rest
of the process. `describe_case` is the tool the prompt tells the agent to call first.

Measured on the same 10 calibration cases:

| | before the repair | after |
|---|---|---|
| tool-call failure rate | 69.3% (187/270) | 0.5% (1/205) |
| runs hitting the 30-step cap | 6 of 10 | 0 of 10 |
| model turns per run | 27.0 | 9.4 |
| clean AC@1 | 1/3 | 2/3 |
| top-1 stable across repeats | 1 of 4 | 9 of 10 |

Both calibration files are kept in `calibration/`. The pre-fix run is usable as an
incidental observation about agent brittleness under tool failure — with its primary
orientation tool broken the agent hit the step cap 60% of the time and its answers
were unstable at temperature 0 — but it is **not** part of the 144-run result, and the
two must not be pooled.

## Caveats carried from the pilot design

- The F1/F2/F3 overlays are quick approximations of the families, not the frozen
  FrameBench generator (P2). F3 changes trace durations but not the latency metrics,
  which is consistent with its small effect here.
- The pilot model is not the frozen study model, so these numbers measure neither the
  frozen model's susceptibility nor its cost.
- Only the 36 development cases were built or run. The other 54 RE2-OB cases are test
  stratum S1 and were never opened.

## Files

| file | what it is |
|---|---|
| `dev_cases.json` | Seed, the two dev services, 12 templates, 36 case ids |
| `pilot_preds.jsonl` | 144 agent answers with tokens, cost, steps, stop reason |
| `pilot_results.csv` | One row per run: blame, AC@1, tokens, turns, cost. For the power simulation |
| `labels.jsonl` | Per variant: original case, family, target, root cause, records injected, dose. The agent never saw this |
| `eligibility.json` | Per case: each candidate target and which checks it passed |
| `score.log` | Scoring output as produced, including the two misleading lines noted above |
| `trajectories/` | Full reasoning trace per run, 144 files |
| `calibration/` | The 10-case calibration before and after the tool-spec repair |
