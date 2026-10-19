#!/bin/bash
# Usage: scripts/run.sh <command...>   e.g. scripts/run.sh pytest -q
# Runs a command inside the project venv from the repo root (WSL dev helper).
cd "$(dirname "$0")/.."
export PATH="${MME_VENV:-$HOME/MME_TELEPHONY/.venv}/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.
exec "$@"