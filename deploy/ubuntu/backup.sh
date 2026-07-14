#!/usr/bin/env bash
set -euo pipefail
DATA_ROOT="${DATA_ROOT:-/var/lib/proposal-agent}"
CONFIG_FILE="${CONFIG_FILE:-/etc/proposal-agent/proposal-agent.env}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/proposal-agent}"
STAMP="$(date +%Y%m%d_%H%M%S)"
sudo mkdir -p "$BACKUP_ROOT"
sudo tar -czf "$BACKUP_ROOT/proposal-agent-$STAMP.tar.gz" "$DATA_ROOT" "$CONFIG_FILE"
echo "$BACKUP_ROOT/proposal-agent-$STAMP.tar.gz"
