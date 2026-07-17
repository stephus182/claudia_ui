#!/usr/bin/env bash
# ibkr-keepalive.sh — keeps the IBKR Client Portal Gateway session alive
# independent of whether ClaudIA is running.
#
# The IBKR session lives in Docker (localhost:5055), not in ClaudIA.
# ClaudIA's ConnectivityChecker calls /tickle every 60s to prevent the
# ~5-6 min inactivity timeout, but only while ClaudIA itself is running.
# This script provides the same protection independent of ClaudIA's
# process lifecycle — safe to run standalone (foreground, Ctrl-C to stop)
# or as a launchd daemon (see scripts/install-ibkr-keepalive-daemon.sh).
#
# It also holds a `caffeinate -i` sleep-prevention assertion for as long as
# the gateway is reachable, and releases it the moment the gateway goes
# unreachable — so a 24/7 launchd install doesn't keep the Mac permanently
# awake when Docker/the gateway container isn't even up.
#
# Session-timeout figures verified against IBKR's official Client Portal
# API docs, 2026-07-17: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#tickle
# (see docs/connectivity.md § Session lifecycle for the full breakdown).
#
# Usage:
#   ./scripts/ibkr-keepalive.sh   # foreground; Ctrl-C to stop

set -euo pipefail

GATEWAY_URL="${IBKR_GATEWAY_URL:-https://localhost:5055}"
INTERVAL=55  # slightly under IBKR's recommended ~1 tickle/min
TICKLE_URL="${GATEWAY_URL%/}/tickle"

# Load .env if present (for IBKR_GATEWAY_URL)
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    TICKLE_URL="${IBKR_GATEWAY_URL%/}/tickle"
fi

echo "[ibkr-keepalive] Starting — tickling ${TICKLE_URL} every ${INTERVAL}s"

CAFFEINATE_PID=""
LAST_STATE=""

cleanup() {
    if [ -n "$CAFFEINATE_PID" ]; then
        kill "$CAFFEINATE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Hold sleep-prevention only while the gateway is actually reachable —
# avoids keeping the Mac awake 24/7 when nothing needs protecting.
ensure_awake() {
    if [ -n "$CAFFEINATE_PID" ] && kill -0 "$CAFFEINATE_PID" 2>/dev/null; then
        return
    fi
    if command -v caffeinate &>/dev/null; then
        caffeinate -i &
        CAFFEINATE_PID=$!
    fi
}

release_awake() {
    if [ -n "$CAFFEINATE_PID" ]; then
        kill "$CAFFEINATE_PID" 2>/dev/null || true
        CAFFEINATE_PID=""
    fi
}

tick() {
    local ts http_code state
    ts="$(date '+%H:%M:%S')"
    # -k: skip SSL verification (self-signed cert), -s: silent, -o /dev/null: discard body
    # -w: capture HTTP status code
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "$TICKLE_URL" 2>/dev/null || echo "000")
    if [ "$http_code" = "200" ]; then
        state="OK"
        ensure_awake
    else
        state="WARN"
        release_awake
    fi
    # Only log on state transitions (plus the very first tick) — this loop
    # runs indefinitely under launchd, so logging every tick forever would
    # grow the log file unbounded.
    if [ "$state" != "$LAST_STATE" ]; then
        if [ "$state" = "OK" ]; then
            echo "${ts}  OK  (HTTP ${http_code})"
        else
            echo "${ts}  WARN  (HTTP ${http_code}) — gateway may be down or not authenticated"
        fi
        LAST_STATE="$state"
    fi
}

tick  # tick immediately on start so state is known right away

while true; do
    sleep "$INTERVAL"
    tick
done
