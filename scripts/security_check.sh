#!/usr/bin/env bash

set -euo pipefail

venv_bin="${VENV_BIN:-.venv/bin}"
pip_bin="${venv_bin}/pip"
safety_bin="${venv_bin}/safety"
bandit_bin="${venv_bin}/bandit"
requirements_file="$(mktemp /tmp/pullbox-requirements-audit.XXXXXX.txt)"

cleanup() {
  rm -f "${requirements_file}"
}

trap cleanup EXIT

status=0

echo "═══ Pullbox Security Check ═══"
echo ""
echo "── pip-audit (blocking) ──"
"${pip_bin}" freeze --exclude-editable | awk '!/^pullbox==/' > "${requirements_file}"
if ! "${venv_bin}/python" scripts/run_dependency_audit.py -r "${requirements_file}"; then
  status=1
fi
echo ""
echo "── safety (advisory) ──"
if ! "${safety_bin}" check --save-json safety-report.json; then
  echo "⚠ safety reported findings or exited non-zero. Review safety-report.json"
fi
echo ""
echo "── bandit (advisory) ──"
if ! "${bandit_bin}" -r src/pullbox/ -ll -f json -o bandit-report.json; then
  echo "⚠ bandit reported medium-or-higher findings. Review bandit-report.json"
fi
echo ""
echo "═══ Done ═══"

exit "${status}"
