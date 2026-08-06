#!/bin/bash
# ClaudIA launcher — starts the IBKR gateway then the Panel UI.
# Usage: ./start-claudia.sh

set -e
cd "$(dirname "$0")"

# Prevent macOS system sleep while ClaudIA is running.
# caffeinate -i keeps the system awake; -w $$ exits automatically when this script exits.
# No-op on non-macOS (caffeinate is a macOS built-in).
if command -v caffeinate &>/dev/null; then
    caffeinate -i -w $$ &
    echo "Sleep prevention active (caffeinate PID $!)"
fi

# Gateway startup goes through claudia.gateway_launch, the same orchestration the
# in-chat "Start IBKR Gateway" button uses. It pre-flights the session BEFORE anything
# touches the container, so a live session is never re-authenticated for nothing and a
# session borrowed from another IBKR app is named instead of retried.
#
# This used to call GatewayManager.startup() directly, which ran no pre-flight at all and
# whose start() removes any existing container — throwing away a working session and
# forcing a fresh 2FA on every launch where the gateway was not already authenticated.
#
# The launcher BLOCKS until the session is confirmed. That is the structural fix,
# not a convenience. Without it this script opened the
# login page and started ClaudIA immediately, so the dashboard poller (15s), the
# ExecutionListener WebSocket and the keepalive all began hammering a gateway that was
# still mid-authentication. Captured in the gateway's own internal log during a real
# login attempt on 2026-08-06:
#
#     13:12:25  ws /v1/api/ws -> {"message":"waiting for session"}
#     13:12:26  GET /v1/api/portfolio/{accountId}/ledger,401
#     13:12:30  GET /v1/api/portfolio/{accountId}/positions/0,401
#
# Waiting here makes that impossible by construction: ClaudIA cannot poll a gateway it
# has not been started against yet.
#
# A non-zero exit is deliberately NOT fatal: a gateway problem must not stop the UI from
# coming up, because the UI is where the Start IBKR Gateway button lives and where the
# status dot explains what is wrong. Diagnose a failed login with:
#     .venv/bin/python3 -m claudia.gateway_launch --diagnose
.venv/bin/python3 -m claudia.gateway_launch || true

.venv/bin/python3 -m claudia.panel_app
