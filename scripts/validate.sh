#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python prompt_pack/tools/validate_pack.py
python -m pytest -q
