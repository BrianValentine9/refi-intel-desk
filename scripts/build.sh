#!/usr/bin/env bash
# Render build step: Python deps, then the static front end. Fails loudly if the export is missing.
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -r requirements.txt
(cd web && npm ci && npm run build)

if [ ! -f web/out/index.html ]; then
  echo "BUILD FAIL: web/out/index.html is missing after the front-end build" >&2
  exit 1
fi
echo "Build OK: web/out/index.html present"
