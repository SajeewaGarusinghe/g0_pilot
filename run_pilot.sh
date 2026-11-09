#!/usr/bin/env bash
# G0 pilot driver. Each step can be run on its own and is safe to repeat.
#
#   ./run_pilot.sh selftest   offline check on synthetic data (no download, no API key, no cost)
#   ./run_pilot.sh export     download RE2-OB (1.2 GB zip) and export the blind task view
#   ./run_pilot.sh select     seeded choice of the 12 development templates (36 cases)
#   ./run_pilot.sh build      clean + F1/F2/F3 variants -> pilot_tasks/, labels -> pilot_sealed/
#   ./run_pilot.sh smoke      run the agent on 4 cases only (a few cents) to check the key and setup
#   ./run_pilot.sh run        run the agent on all 144 cases (resumable: rerun to continue)
#   ./run_pilot.sh score      target blame rate, Delta TBR with intervals, measured cost per run
#   ./run_pilot.sh all        export, select, build, smoke, run, score
#
# Settings can be overridden in .env or the environment:
#   MODEL (default anthropic/claude-sonnet-5-5)  TEMPERATURE (0)  MAX_COST_USD (0.50 per case)  WORKERS (2)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
[ -d .venv ] && . .venv/bin/activate

MODEL="${MODEL:-anthropic/claude-sonnet-5-5}"
TEMPERATURE="${TEMPERATURE:-0}"
MAX_COST_USD="${MAX_COST_USD:-0.50}"
WORKERS="${WORKERS:-2}"
LOG=logs; mkdir -p "$LOG"

need_key() {
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [[ "$MODEL" == anthropic/* ]]; then
    echo "ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example)." >&2; exit 1
  fi
}

step_selftest() {
  local d=selftest
  rm -rf -- "./$d"
  python tests/make_fake_export.py "$d"
  python g0_pilot.py select --tasks "$d/agent_tasks/re2-ob" --out "$d/dev_cases.json"
  python g0_pilot.py build --tasks "$d/agent_tasks/re2-ob" --dev "$d/dev_cases.json" \
    --out "$d/pilot_tasks" --sealed "$d/pilot_sealed"
  python tests/fake_predictions.py "$d/pilot_sealed/labels.jsonl" "$d/preds.jsonl"
  python g0_pilot.py score --preds "$d/preds.jsonl" --sealed "$d/pilot_sealed"
  rcaeval-agent evaluate --tasks "$d/pilot_tasks/pilot" --predictions "$d/preds.jsonl" | head -6
  echo "Self-test passed (numbers above are synthetic)."
}

step_export() {
  if [ -d agent_tasks/re2-ob ]; then echo "agent_tasks/re2-ob exists, skipping export"; return; fi
  rcaeval-agent export --dataset re2-ob --out agent_tasks 2>&1 | tee "$LOG/export.log"
}

step_select() { python g0_pilot.py select --tasks agent_tasks/re2-ob --out dev_cases.json | tee "$LOG/select.log"; }

step_build() {
  if [ -d pilot_tasks/pilot ]; then echo "pilot_tasks/pilot exists; delete it to rebuild"; return; fi
  python g0_pilot.py build --tasks agent_tasks/re2-ob --dev dev_cases.json 2>&1 | tee "$LOG/build.log"
}

run_agent() {  # $1 = predictions file, rest = extra args
  need_key
  local out="$1"; shift
  rcaeval-agent run --tasks pilot_tasks/pilot --model "$MODEL" --temperature "$TEMPERATURE" \
    --max-cost-usd "$MAX_COST_USD" --workers "$WORKERS" --out "$out" "$@" 2>&1 | tee -a "$LOG/run.log"
}

step_smoke() {
  local ids
  ids=$(python -c "import json;print(' '.join(json.load(open('pilot_tasks/pilot/manifest.json'))['case_ids'][:4]))")
  run_agent smoke_preds.jsonl --cases $ids
  python g0_pilot.py score --preds smoke_preds.jsonl || true
  echo "Smoke run done. Check the cost per run above, then: ./run_pilot.sh run"
}

step_run() { run_agent pilot_preds.jsonl; }

step_score() {
  python g0_pilot.py score --preds pilot_preds.jsonl | tee "$LOG/score.log"
  rcaeval-agent evaluate --tasks pilot_tasks/pilot --predictions pilot_preds.jsonl | tee -a "$LOG/score.log"
}

case "${1:-}" in
  selftest|export|select|build|smoke|run|score) "step_$1" ;;
  all) step_export; step_select; step_build; step_smoke; step_run; step_score ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
