#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-offline}"
python3 "$ROOT/verify_manifest.py" "$ROOT"
gzip -dc "$ROOT/proposal-agent-image.tar.gz" | docker load
if [ -f "$ROOT/searxng-image.tar.gz" ]; then gzip -dc "$ROOT/searxng-image.tar.gz" | docker load; fi
mkdir -p "$ROOT/data" "$ROOT/searxng"
if [ ! -f "$ROOT/proposal-agent.env" ]; then
  cp "$ROOT/proposal-agent.env.example" "$ROOT/proposal-agent.env"
  sed -i 's|^APP_DATA_DIR=.*|APP_DATA_DIR=/var/lib/proposal-agent|' "$ROOT/proposal-agent.env"
  sed -i 's|^PROMPT_PACK_DIR=.*|PROMPT_PACK_DIR=/app/prompt_pack|' "$ROOT/proposal-agent.env"
  sed -i 's|^MERMAID_BROWSER_EXECUTABLE=.*|MERMAID_BROWSER_EXECUTABLE=/usr/bin/chromium|' "$ROOT/proposal-agent.env"
  echo "Edit $ROOT/proposal-agent.env, then rerun this command." >&2
  exit 2
fi
if [ "$MODE" = "hybrid" ]; then
  docker compose -f "$ROOT/docker-compose.hybrid.yml" up -d
else
  docker compose -f "$ROOT/docker-compose.offline.yml" up -d
fi
sleep 5
curl --fail http://127.0.0.1:8080/api/health
echo
