#!/usr/bin/env bash
# Free gate for the app-v2 build: every commit runs it (via .githooks/pre-commit).
# No network, no paid call: both API keys are unset for the test run.
# FLOOR is raised only to a real new passing count, with the reason in BUILD_LEDGER.md.
set -euo pipefail
FLOOR=156
ROOT="$(git rev-parse --show-toplevel)"
PY="${VENV_PY:-/c/Code/refi-intel-desk/.venv/Scripts/python.exe}"
cd "$ROOT"

out="$(env -u ANTHROPIC_API_KEY -u FRED_API_KEY "$PY" -m pytest -q -p no:cacheprovider 2>&1 | tail -n 3)"
echo "$out"
passed="$(echo "$out" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo 0)"
if echo "$out" | grep -qE '[0-9]+ (failed|error)'; then
  echo "GATE FAIL: failing tests"; exit 1
fi
if [ "$passed" -lt "$FLOOR" ]; then
  echo "GATE FAIL: $passed passed, floor $FLOOR"; exit 1
fi

if [ -f src/api/app.py ]; then
  env -u ANTHROPIC_API_KEY -u FRED_API_KEY "$PY" -c "import sys, src.api.app; bad=[m for m in ('streamlit','plotly') if m in sys.modules]; sys.exit('GATE FAIL: API imports '+','.join(bad) if bad else 0)"
fi

if [ -f web/package.json ] && git diff --cached --name-only | grep -q '^web/'; then
  (cd web && npm run build --silent) || { echo "GATE FAIL: web build"; exit 1; }
fi
echo "GATE OK: $passed passed (floor $FLOOR)"
