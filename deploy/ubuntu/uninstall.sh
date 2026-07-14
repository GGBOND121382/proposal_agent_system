#!/usr/bin/env bash
set -euo pipefail
KEEP_DATA="${KEEP_DATA:-false}"
sudo systemctl disable --now proposal-agent 2>/dev/null || true
sudo rm -f /etc/systemd/system/proposal-agent.service
sudo systemctl daemon-reload
sudo rm -rf /opt/proposal-agent /var/log/proposal-agent
if [ "$KEEP_DATA" != "true" ]; then sudo rm -rf /var/lib/proposal-agent /etc/proposal-agent; fi
echo "Proposal Agent removed."
