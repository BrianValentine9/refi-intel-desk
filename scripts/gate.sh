#!/usr/bin/env bash
# Free gate for the app-v2 build: every commit runs it (via .githooks/pre-commit).
# No network, no paid call: both API keys are unset for the test run.
# FLOOR is raised only to a real new passing count, with the reason in BUILD_LEDGER.md.
# Python: VENV_PY if set, else the worktree's .venv-api (from U1b), else the shared venv (read-only use).
set -uo pipefail
FLOOR=156
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
if [ -n "${VENV_PY:-}" ]; then PY="$VENV_PY"
elif [ -x .venv-api/Scripts/python.exe ]; then PY=.venv-api/Scripts/python.exe
else PY=/c/Code/refi-intel-desk/.venv/Scripts/python.exe; fi

log="$(mktemp)"
env -u ANTHROPIC_API_KEY -u FRED_API_KEY "$PY" -m pytest -q -p no:cacheprovider >"$log" 2>&1
rc=$?
tail -n 15 "$log"
passed="$(tail -n 3 "$log" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo 0)"
rm -f "$log"
if [ "$rc" -ne 0 ]; then echo "GATE FAIL: pytest exit $rc"; exit 1; fi
if [ "$passed" -lt "$FLOOR" ]; then echo "GATE FAIL: $passed passed, floor $FLOOR"; exit 1; fi

# The committed seed must never change by accident (a WAL pragma or a write would flip it).
if ! git diff --quiet -- data/seed.db; then
  echo "GATE FAIL: data/seed.db changed in the working tree"; exit 1
fi
if git diff --cached --name-only | grep -qx 'data/seed.db' && [ "${SEED_REFRESH:-}" != "1" ]; then
  echo "GATE FAIL: data/seed.db staged; set SEED_REFRESH=1 only for the planned U6 refresh"; exit 1
fi

if [ -f src/api/app.py ]; then
  env -u ANTHROPIC_API_KEY -u FRED_API_KEY "$PY" -c "import sys, src.api.app; bad=[m for m in ('streamlit','plotly') if m in sys.modules]; sys.exit('GATE FAIL: API imports '+','.join(bad) if bad else 0)" || exit 1
fi

if [ -f web/package.json ] && git diff --cached --name-only | grep -q '^web/'; then
  (cd web && npm run build --silent) || { echo "GATE FAIL: web build"; exit 1; }
fi
echo "GATE OK: $passed passed (floor $FLOOR)"
