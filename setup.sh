#!/usr/bin/env bash
# One-time setup on an Oracle Cloud VM (Oracle Linux or Ubuntu; x86_64 or Arm/Ampere).
# Installs uv, a Python 3.12 virtual environment in .venv, and RCAEval with the agent and converter extras.
set -euo pipefail
cd "$(dirname "$0")"

echo "== system packages (git, tmux, unzip)"
if command -v apt-get >/dev/null; then
  sudo apt-get update -y && sudo apt-get install -y git tmux unzip curl
elif command -v dnf >/dev/null; then
  sudo dnf install -y git tmux unzip curl
fi

echo "== uv (Python manager)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "== Python 3.12 virtual environment in .venv"
uv venv --python 3.12 .venv
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install -r requirements.txt

echo "== checks"
python -c "import RCAEval, pandas, numpy; print('RCAEval OK, pandas', pandas.__version__)"
rcaeval-agent --help >/dev/null && rcaeval-convert --help >/dev/null && echo "CLIs OK"
df -h . | tail -1 | awk '{print "free disk here: " $4 " (the pilot needs about 25G)"}'
free -g | awk '/Mem:/ {print "RAM: " $2 "G (16G or more is comfortable)"}'

if [ ! -f .env ]; then
  cp .env.example .env
  echo "== created .env: put your Anthropic API key in it before running the agent"
fi
echo "Done. Next:  source .venv/bin/activate && ./run_pilot.sh selftest"
