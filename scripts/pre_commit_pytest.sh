#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -x ".venv/bin/pytest" ]]; then
  exec ".venv/bin/pytest" tests/unit/ -x -q -m "not slow" --no-header
fi

exec pytest tests/unit/ -x -q -m "not slow" --no-header
