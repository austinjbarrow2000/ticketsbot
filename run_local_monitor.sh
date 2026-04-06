#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set before running."
  echo "Example:"
  echo "  export TELEGRAM_TOKEN='...'; export TELEGRAM_CHAT_ID='...'"
  exit 1
fi

python3 local_runner.py --interval-seconds 60
