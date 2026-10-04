#!/usr/bin/env bash
# Render start command: one worker (the ladder cache and compute thread live in-process).
set -euo pipefail
cd "$(dirname "$0")/.."

exec uvicorn src.api.app:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers
