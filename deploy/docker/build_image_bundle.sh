#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/dist/proposal-agent-docker-offline}"
MODE="${2:-offline}"
IMAGE="proposal-agent:0.5.0-offline"
SEARXNG_IMAGE="${SEARXNG_IMAGE:-searxng/searxng:latest}"
rm -rf "$OUT"
mkdir -p "$OUT"

docker build -f "$ROOT/deploy/docker/Dockerfile.offline" -t "$IMAGE" "$ROOT"
docker save "$IMAGE" | gzip -9 > "$OUT/proposal-agent-image.tar.gz"
if [ "$MODE" = "hybrid" ]; then
  docker pull "$SEARXNG_IMAGE"
  docker save "$SEARXNG_IMAGE" | gzip -9 > "$OUT/searxng-image.tar.gz"
fi
cp "$ROOT/deploy/docker/docker-compose.offline.yml" "$OUT/"
cp "$ROOT/deploy/docker/docker-compose.hybrid.yml" "$OUT/"
cp "$ROOT/deploy/docker/load_and_run.sh" "$OUT/"
cp "$ROOT/deploy/common/verify_manifest.py" "$OUT/"
cp -a "$ROOT/prompt_pack" "$OUT/prompt_pack"
cp "$ROOT/.env.example" "$OUT/proposal-agent.env.example"
{
  echo "Application image: $IMAGE"
  docker image inspect "$IMAGE" --format 'Application image ID: {{.Id}}'
  echo "Bundle mode: $MODE"
  if [ "$MODE" = "hybrid" ]; then docker image inspect "$SEARXNG_IMAGE" --format 'SearXNG image ID: {{.Id}}'; fi
  echo "Built at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT/BUNDLE_INFO.txt"
python3 "$ROOT/deploy/common/write_manifest.py" "$OUT"
tar -C "$(dirname "$OUT")" -czf "$OUT.tar.gz" "$(basename "$OUT")"
echo "Created $OUT.tar.gz"
