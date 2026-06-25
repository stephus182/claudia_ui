#!/usr/bin/env bash
# ibkr-keepalive.sh — keeps the IBKR Client Portal Gateway session alive
# while ClaudIA is stopped between restarts (testing, debugging, etc.)
#
# The IBKR session lives in Docker (localhost:5055), not in ClaudIA.
# ClaudIA's ConnectivityChecker calls /tickle every 60s to prevent timeout.
# This script replaces that keepalive when ClaudIA is not running.
#
# Usage:
#   ./scripts/ibkr-keepalive.sh            # default gateway URL from .env
#   ./scripts/ibkr-keepalive.sh --stop     # write a stop file and exit
#
# Run in a separate terminal before restarting ClaudIA. Ctrl-C to stop.

set -euo pipefail

GATEWAY_URL="${IBKR_GATEWAY_URL:-https://localhost:5055}"
INTERVAL=55  # slightly under 60s to stay ahead of IBKR's keepalive timer
TICKLE_URL="${GATEWAY_URL%/}/tickle"

# Load .env if present (for IBKR_GATEWAY_URL)
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
    TICKLE_URL="${IBKR_GATEWAY_URL%/}/tickle"
fi

echo "[ibkr-keepalive] Starting — tickling ${TICKLE_URL} every ${INTERVAL}s"
echo "[ibkr-keepalive] Press Ctrl-C to stop when ClaudIA is back up."
echo ""

tick() {
    local ts
    ts="$(date '+%H:%M:%S')"
    local result
    # -k: skip SSL verification (self-signed cert), -s: silent, -o /dev/null: discard body
    # -w: capture HTTP status code
    local http_code
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "$TICKLE_URL" 2>/dev/null || echo "000")
    if [ "$http_code" = "200" ]; then
        echo "${ts}  OK  (HTTP ${http_code})"
    else
        echo "${ts}  WARN  (HTTP ${http_code}) — gateway may be down or not authenticated"
    fi
}

# Tick immediately on start so you see status right away
tick

while true; do
    sleep "$INTERVAL"
    tick
done
