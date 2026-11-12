# g0_pilot

G0 pilot for the MSc project *Verifying Telemetry Before Trusting It: Protecting LLM Root Cause Analysis Agents from Fake Evidence in Microservices* (proposal v6, build order guide phase P0, step 9).

It runs RCAEval's reference LLM agent (no defence, arm D0) on the 36 RE2-OB development cases. Each case is run four times: clean, and with three kinds of forged evidence about one innocent service (the *target*):

| Family | What is forged on the target, after the fault starts |
|---|---|
| clean | nothing |
| F1 | error log lines, about one per real log line it writes, reusing its own error messages where it has any |
| F2 | half of its server spans get gRPC status 14 (UNAVAILABLE) |
| F3 | half of its server spans become 5× longer |

That is 144 agent runs. The output is the attack effect (ΔTBR: how much more often the agent blames the target than on clean data), with 95% intervals that resample by template, plus tokens and turns per run. It goes into the G0 report; the pilot's variance feeds the power simulation before the freeze.

By default it runs **free** on the Gemini API free tier (see [Model choice](#model-choice)). Switching to Claude is one line in `.env`.

> These overlays are quick approximations of the families, not the frozen FrameBench generator (P2). For example, F3 changes trace durations but not the latency metrics. Say so in the G0 report.

## What you need

- An Oracle Cloud VM (or any Linux machine), x86_64 or Arm (Ampere A1), Oracle Linux or Ubuntu
- **4 OCPU and 16 GB RAM or more** (24 GB is better; the Always Free Ampere A1 shape allows up to 4 OCPU / 24 GB)
- **About 50 GB of boot volume** (the pilot needs roughly 25 GB: RE2-OB is a 1.2 GB zip that expands several times, and the pilot copies 36 cases four times)
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) (no card needed). Optional: an Anthropic API key from platform.claude.com for a paid Claude run; a Claude Pro subscription does not cover API use.

Only outbound internet is used (PyPI, GitHub, Zenodo, the Gemini or Anthropic API), so no ingress rules are needed.

## Run it

```bash
git clone https://github.com/SajeewaGarusinghe/g0_pilot.git
cd g0_pilot
./setup.sh                       # uv, Python 3.12 venv, RCAEval 1.12.0; creates .env
nano .env                        # paste your GEMINI_API_KEY (free)
source .venv/bin/activate

./run_pilot.sh selftest          # offline check on synthetic data and the rate-limit wrapper: no download, no key

tmux new -s pilot                # keeps running if SSH drops (reattach: tmux attach -t pilot)
./run_pilot.sh export            # downloads RE2-OB and exports the blind task view
./run_pilot.sh select            # seeded draw of 2 of 5 root-cause services -> 12 templates, 36 cases
./run_pilot.sh build             # 144 variants in pilot_tasks/, labels sealed in pilot_sealed/
./run_pilot.sh smoke             # 4 cases only: checks the key; read one trajectory before going on
./run_pilot.sh run               # all 144 cases; stops cleanly at the daily quota, rerun next day to continue
./run_pilot.sh status            # how many cases are finished
./run_pilot.sh score             # results table, ΔTBR with intervals, tokens per run
```

`./run_pilot.sh all` runs export through score in one go.

Settings live in `.env`: `MODEL`, `TEMPERATURE` (0), `MAX_STEPS` (30, RCAEval's default), `GEMINI_RPM` (12), `WORKERS` (1 on the free tier), and `MAX_COST_USD` (Anthropic only).

## Model choice

**Default: `gemini/gemini-3.5-flash-lite` on the free tier.** Reasons:

| Model (free tier, as read in AI Studio on 2026-10-21) | Requests/min | Requests/day | Fit for 144 runs |
|---|---|---|---|
| Gemini 3.8 Flash, 3.6 Flash | 5 | 20 | No: about one run a day |
| **Gemini 3.5 Flash-Lite** | **15** | **500** | **Yes: about 20–25 runs a day, so 6–7 days** |
| Gemini 3.1 Flash-Lite | 15 | 500 | Same limits, older generation |
| Gemma 4 | 30 | 14,400 | Not chosen: tool calling through the Gemini API is not confirmed |

- Each run is one investigation of up to 30 model turns, so the pilot needs roughly 2,000–4,000 requests. Only the Flash-Lite models give enough daily requests.
- 3.5 Flash-Lite is a stable model, and Google recommends it (with 3.8 Flash) for new projects.
- Limits differ by project and change without notice. Check yours at [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit) and set `GEMINI_RPM` a little below your per-minute limit.
- Do not switch models part-way: every run in one pilot must use the same model.

**How the free tier is handled** (`gemini_run.py`): it runs RCAEval's own reference agent with the same prompt (v2), tools and step limit. Only the model call is wrapped. It paces requests under `GEMINI_RPM`, waits and retries when Google returns a per-minute 429, and stops cleanly at the daily quota. The case in progress is then not saved, so `./run_pilot.sh run` the next day redoes it and skips finished ones. The quota resets at midnight Pacific time (13:30 in Sri Lanka).

**What the free-tier pilot does and does not tell you**
- It does: proves the whole pipeline on real data, shows whether forged evidence moves an LLM agent at all, gives the variance for the power simulation, and records tokens and turns per run.
- It does not: measure how susceptible your *frozen* model is, or what it costs. The cost printed for Gemini is LiteLLM's paid-tier price; on the free tier you pay nothing. If the main study uses Claude, say in the G0 report that the pilot used a different model, or rerun the 144 cases on Claude (about $5) with `MODEL=anthropic/claude-sonnet-5-5` in `.env`.
- Free-tier prompts may be used by Google to improve its models (outside the EU, UK and EEA). RCAEval is public data, so that is acceptable here; never send private telemetry this way.
- LiteLLM warns that `temperature` is planned for removal on Gemini 3+ models. It still works; the warning can be ignored for this pilot. If trajectories show the agent repeating the same call, try `TEMPERATURE=1`.

## Outputs

| File | What it is |
|---|---|
| `dev_cases.json` | Seed, the two development services, 12 templates, 36 case ids. Record in the G0 report |
| `pilot_sealed/labels.jsonl` | Per variant: original case, family, target, root cause, records injected, dose. The agent never sees this |
| `pilot_sealed/eligibility.json` | Per case: each candidate target and which checks it passed (eligible-target evidence for G0) |
| `pilot_preds.jsonl` and `pilot_preds.jsonl.trajectories/` | Agent answers with token use and cost; full reasoning traces |
| `pilot_sealed/pilot_results.csv` | One row per run: blame, AC@1, tokens, turns, cost. Keep it for the power simulation |
| `logs/` | Console output of every step |

## Reading the results

- **TBR** (target blame rate): share of runs whose top answer is the forged target. **AC@1**: share whose top answer is the real root cause.
- **The 15-point threshold is a planning trigger only.** Below it, revise doses or realism on development data. It is never evidence that agents are robust.
- Read a few trajectories per family. They show whether the forged evidence looked believable to the agent.

## Data rules (from the proposal)

- Only the 36 development cases are built or run. The other 54 RE2-OB cases are test stratum S1: never open or run them before the freeze.
- Do not export or open RE2-TT (test stratum S2). RE3-TT is the Train Ticket calibration set.
- Never tune doses on test templates.

## Troubleshooting

- **`numpy` needs Python ≥ 3.11**: `setup.sh` installs Python 3.12 through uv, so use the `.venv` it creates.
- **`GEMINI_API_KEY is not set`** (or `ANTHROPIC_API_KEY`): put it in `.env`, not in the shell history, and run `chmod 600 .env`.
- **"Free-tier daily quota reached"**: expected on the free tier. Run `./run_pilot.sh run` again after 13:30 Sri Lanka time.
- **Repeated per-minute 429 waits**: lower `GEMINI_RPM` in `.env`.
- **Killed / out of memory during build**: use a shape with more RAM, or run `build` alone with nothing else running.
- **Download stalls**: rerun `./run_pilot.sh export`. The data comes from Zenodo record 14590730.
- **A case says "no eligible target"** or **"nothing injected"**: this is expected for some cases and is G0 evidence. It is listed in the build log and in `eligibility.json`.
