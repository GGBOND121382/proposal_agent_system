#!/usr/bin/env bash
set -euo pipefail
[ "$#" -eq 1 ] || { echo "Usage: sudo bash restore.sh <backup.tar.gz>" >&2; exit 2; }
systemctl stop proposal-agent || true
tar -xzf "$1" -C /
systemctl start proposal-agent
curl --fail --silent http://127.0.0.1:8080/api/health
echo
