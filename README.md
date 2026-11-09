# g0_pilot

G0 pilot for the MSc project *Verifying Telemetry Before Trusting It: Protecting LLM Root Cause Analysis Agents from Fake Evidence in Microservices* (proposal v6, build order guide phase P0, step 9).

It runs RCAEval's reference LLM agent (no defence, arm D0) on the 36 RE2-OB development cases. Each case is run four times: clean, and with three kinds of forged evidence about one innocent service (the *target*):

| Family | What is forged on the target, after the fault starts |
|---|---|
| clean | nothing |
| F1 | error log lines, about one per real log line it writes, reusing its own error messages where it has any |
| F2 | half of its server spans get gRPC status 14 (UNAVAILABLE) |
| F3 | half of its server spans become 5× longer |

That is 144 agent runs. The output is the attack effect (ΔTBR: how much more often the agent blames the target than on clean data), with 95% intervals that resample by template, and the **measured cost per run**. Both go into the G0 report; the pilot's variance feeds the power simulation before the freeze.

> These overlays are quick approximations of the families, not the frozen FrameBench generator (P2). For example, F3 changes trace durations but not the latency metrics. Say so in the G0 report.

## What you need

- An Oracle Cloud VM (or any Linux machine), x86_64 or Arm (Ampere A1), Oracle Linux or Ubuntu
- **4 OCPU and 16 GB RAM or more** (24 GB is better; the Always Free Ampere A1 shape allows up to 4 OCPU / 24 GB)
- **About 50 GB of boot volume** (the pilot needs roughly 25 GB: RE2-OB is a 1.2 GB zip that expands several times, and the pilot copies 36 cases four times)
- An Anthropic API key from console.anthropic.com with a few dollars of credit. A Claude Pro subscription does not cover API use.

Only outbound internet is used (PyPI, GitHub, Zenodo, the Anthropic API), so no ingress rules are needed.

## Run it

```bash
git clone https://github.com/SajeewaGarusinghe/g0_pilot.git
cd g0_pilot
./setup.sh                       # uv, Python 3.12 venv, RCAEval 1.12.0; creates .env
nano .env                        # paste your ANTHROPIC_API_KEY; set MODEL to the model you will freeze
source .venv/bin/activate

./run_pilot.sh selftest          # offline check on synthetic data: no download, no key, no cost

tmux new -s pilot                # keeps running if SSH drops (reattach: tmux attach -t pilot)
./run_pilot.sh export            # downloads RE2-OB and exports the blind task view
./run_pilot.sh select            # seeded draw of 2 of 5 root-cause services -> 12 templates, 36 cases
./run_pilot.sh build             # 144 variants in pilot_tasks/, labels sealed in pilot_sealed/
./run_pilot.sh smoke             # 4 cases only: checks the key and shows the real cost per run
./run_pilot.sh run               # all 144 cases; if it stops, rerun and finished cases are skipped
./run_pilot.sh score             # results table, ΔTBR with intervals, projected cost of 9,614 runs
```

`./run_pilot.sh all` runs export through score in one go.

Settings live in `.env`: `MODEL` (default `anthropic/claude-sonnet-5-5`, the model RCAEval measured at 2.6¢ per case), `TEMPERATURE` (0), `MAX_COST_USD` (0.50 per case, so the worst case for 144 runs is $72; expect a few dollars), and `WORKERS` (2 cases in parallel).

## Outputs

| File | What it is |
|---|---|
| `dev_cases.json` | Seed, the two development services, 12 templates, 36 case ids. Record in the G0 report |
| `pilot_sealed/labels.jsonl` | Per variant: original case, family, target, root cause, records injected, dose. The agent never sees this |
| `pilot_sealed/eligibility.json` | Per case: each candidate target and which checks it passed (eligible-target evidence for G0) |
| `pilot_preds.jsonl` and `pilot_preds.jsonl.trajectories/` | Agent answers with token use and cost; full reasoning traces |
| `pilot_sealed/pilot_results.csv` | One row per run: blame, AC@1, cost. Keep it for the power simulation |
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
- **`ANTHROPIC_API_KEY is not set`**: put it in `.env`, not in the shell history.
- **Killed / out of memory during build**: use a shape with more RAM, or run `build` alone with nothing else running.
- **Download stalls**: rerun `./run_pilot.sh export`. The data comes from Zenodo record 14590730.
- **A case says "no eligible target"** or **"nothing injected"**: this is expected for some cases and is G0 evidence. It is listed in the build log and in `eligibility.json`.
