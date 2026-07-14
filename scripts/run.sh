#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
ARGS=(app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8080}" --workers "${APP_WORKERS:-1}")
if [ "${APP_RELOAD:-false}" = "true" ]; then ARGS+=(--reload); fi
exec uvicorn "${ARGS[@]}"
