#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/dist/proposal-agent-ubuntu-offline}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APT_PACKAGES=(python3 python3-venv python3-pip chromium fonts-noto-cjk libreoffice-writer poppler-utils)

rm -rf "$OUT"
mkdir -p "$OUT/source" "$OUT/wheelhouse" "$OUT/debs/partial"

# Copy reproducible application source without runtime data or VCS state.
tar -C "$ROOT" \
  --exclude='.git' --exclude='.venv' --exclude='data' --exclude='dist' \
  --exclude='__pycache__' --exclude='*.pyc' \
  -cf - . | tar -C "$OUT/source" -xf -

"$PYTHON_BIN" -m pip download \
  --dest "$OUT/wheelhouse" \
  --requirement "$ROOT/requirements.txt"

if command -v apt-get >/dev/null 2>&1; then
  echo "Downloading Ubuntu packages and dependencies into the bundle..."
  sudo apt-get update
  sudo apt-get \
    -o "Dir::Cache::archives=$OUT/debs" \
    -o "Dir::Cache::archives::partial=$OUT/debs/partial" \
    --download-only install -y "${APT_PACKAGES[@]}"
else
  echo "WARNING: apt-get not found; the bundle will require system Python, Chromium and fonts on the target host." >&2
fi
rm -rf "$OUT/debs/partial"

cp "$ROOT/deploy/ubuntu/install_offline.sh" "$OUT/install.sh"
cp "$ROOT/deploy/ubuntu/proposal-agent.service" "$OUT/proposal-agent.service"
cp "$ROOT/deploy/ubuntu/backup.sh" "$OUT/backup.sh"
cp "$ROOT/deploy/ubuntu/restore.sh" "$OUT/restore.sh"
cp "$ROOT/deploy/ubuntu/uninstall.sh" "$OUT/uninstall.sh"
cp "$ROOT/deploy/common/verify_manifest.py" "$OUT/verify_manifest.py"
cat > "$OUT/BUNDLE_INFO.txt" <<INFO
Proposal Agent Ubuntu offline bundle
Built at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Build host: $(uname -a)
Python: $($PYTHON_BIN --version 2>&1)
Target requirement: same Ubuntu release and CPU architecture as the build host.
INFO

"$PYTHON_BIN" "$ROOT/deploy/common/write_manifest.py" "$OUT"
tar -C "$(dirname "$OUT")" -czf "$OUT.tar.gz" "$(basename "$OUT")"
echo "Created: $OUT.tar.gz"
