#!/usr/bin/env bash
set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/proposal-agent}"
DATA_ROOT="${DATA_ROOT:-/var/lib/proposal-agent}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/proposal-agent}"
LOG_ROOT="${LOG_ROOT:-/var/log/proposal-agent}"
SERVICE_USER="${SERVICE_USER:-proposal-agent}"

python3 "$BUNDLE_ROOT/verify_manifest.py" "$BUNDLE_ROOT"

if compgen -G "$BUNDLE_ROOT/debs/*.deb" >/dev/null; then
  sudo mkdir -p /var/cache/apt/archives
  sudo cp "$BUNDLE_ROOT"/debs/*.deb /var/cache/apt/archives/
  sudo dpkg -i "$BUNDLE_ROOT"/debs/*.deb || true
  sudo apt-get --no-download -f install -y
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --home "$DATA_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
sudo mkdir -p "$INSTALL_ROOT" "$DATA_ROOT/uploads" "$DATA_ROOT/exports" "$DATA_ROOT/research_archive" "$DATA_ROOT/diagram_artifacts" "$CONFIG_ROOT" "$LOG_ROOT"
sudo rsync -a --delete "$BUNDLE_ROOT/source/" "$INSTALL_ROOT/" 2>/dev/null || {
  sudo rm -rf "$INSTALL_ROOT"/*
  sudo cp -a "$BUNDLE_ROOT/source/." "$INSTALL_ROOT/"
}

sudo python3 -m venv "$INSTALL_ROOT/.venv"
sudo "$INSTALL_ROOT/.venv/bin/pip" install --no-index --find-links "$BUNDLE_ROOT/wheelhouse" -r "$INSTALL_ROOT/requirements.txt"

if [ ! -f "$CONFIG_ROOT/proposal-agent.env" ]; then
  sudo cp "$INSTALL_ROOT/.env.example" "$CONFIG_ROOT/proposal-agent.env"
  sudo sed -i \
    -e "s|^APP_DATA_DIR=.*|APP_DATA_DIR=$DATA_ROOT|" \
    -e "s|^PROMPT_PACK_DIR=.*|PROMPT_PACK_DIR=$INSTALL_ROOT/prompt_pack|" \
    -e 's|^MODEL_RUNTIME_MODE=.*|MODEL_RUNTIME_MODE=REPLAY|' \
    -e 's|^MERMAID_BROWSER_EXECUTABLE=.*|MERMAID_BROWSER_EXECUTABLE=/usr/bin/chromium|' \
    "$CONFIG_ROOT/proposal-agent.env"
  echo "Created $CONFIG_ROOT/proposal-agent.env. Configure model endpoints before using LIVE mode."
fi

sudo cp "$BUNDLE_ROOT/proposal-agent.service" /etc/systemd/system/proposal-agent.service
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT" "$DATA_ROOT" "$LOG_ROOT"
sudo chmod 600 "$CONFIG_ROOT/proposal-agent.env"
sudo systemctl daemon-reload
sudo systemctl enable --now proposal-agent
sleep 2
sudo systemctl --no-pager --full status proposal-agent || true
curl --fail --silent http://127.0.0.1:8080/api/health || {
  echo "Health check failed. Inspect: journalctl -u proposal-agent -n 200" >&2
  exit 1
}
echo
echo "Proposal Agent installed at $INSTALL_ROOT"
