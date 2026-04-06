#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set before running."
  echo "Example:"
  echo "  export TELEGRAM_TOKEN='...'; export TELEGRAM_CHAT_ID='...'"
  exit 1
fi

INTERVAL_SECONDS="${INTERVAL_SECONDS:-150}"
JITTER_SECONDS="${JITTER_SECONDS:-30}"

python3 local_runner.py --interval-seconds "$INTERVAL_SECONDS" --jitter-seconds "$JITTER_SECONDS"
