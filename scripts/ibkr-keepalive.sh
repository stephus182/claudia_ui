#!/usr/bin/env bash
# ibkr-keepalive.sh — keeps the IBKR Client Portal Gateway session alive
# independent of whether ClaudIA is running.
#
# The IBKR session lives in Docker (localhost:5055), not in ClaudIA.
# ClaudIA's GatewaySession reads /tickle every 60s, which prevents the
# ~5-6 min inactivity timeout as a side effect — but only while ClaudIA
# itself is running. (That read used to be ConnectivityChecker's; it moved
# to the session owner on 2026-08-06 and the checker now issues no HTTP.)
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
# THE SUSPEND LOCK — read this before changing the tick loop.
#
# This is the one renewer the Python side cannot reach: it runs under launchd
# (RunAtLoad + KeepAlive), outside every ClaudIA process. So while a login or a
# recovery is in progress, the ONLY thing that can stop it tickling is the lock
# file below.
#
# Why that matters: IBKR's own docs say "if the gateway has not received ANY
# requests for several minutes an open session will automatically timeout"
# (https://www.interactivebrokers.com/docs/web-api/v1/endpoints/session/ping-the-server.md).
# Renewal is therefore a side effect of every request, not a job this script uniquely
# performs — and on 2026-08-05 three ticklers renewing in the background made
# `POST /logout` unable to clear an unusable session at all. A session being
# established or cleared has to be left completely alone.
#
# Fail-open by design: a missing, malformed, or dead-PID lock means "not suspended".
# A lock that outlived its owner would silence this script forever, and the symptom
# (sessions quietly timing out) looks nothing like the cause.
#
# Written by claudia/gateway_session.py :: SuspendLock. Guarded by
# tests/test_gateway_ownership.py, which fails if this check is removed.
#
# Usage:
#   ./scripts/ibkr-keepalive.sh   # foreground; Ctrl-C to stop
#   ./scripts/ibkr-keepalive.sh --print-url   # the URL it would tickle; nothing contacted

set -euo pipefail

INTERVAL=55  # slightly under IBKR's recommended ~1 tickle/min
SUSPEND_LOCK="${HOME}/.ibkr_core/session.suspend"

# THE URL IS READ FROM ONE LINE OF .env, NEVER BY SOURCING THE FILE. Until 2026-10-16 this
# block did `set -a; source "$ENV_FILE"; set +a`: every line of the file — the Anthropic key,
# the Flex token, the Firecrawl key — became an exported variable of this permanent launchd
# process and of every `curl` and `caffeinate` it starts, and the file was executed as shell
# (gap #103, CWE-526). The script needs one value. It is read with grep and sed, by
# python-dotenv's grammar for a line (`export` prefix, spaces around `=`, single or double
# quotes, an inline comment on an unquoted value; the last line wins), and nothing else in
# the file is read.
#
# Both halves below matter — measured 2026-08-06, the earlier block had two ways to silently
# stop renewing anything:
#
#   1. The default was `https://localhost:5055` with NO `/v1/api`, so with no .env this
#      tickled `https://localhost:5055/tickle` — probed live, that returns **HTTP 302**,
#      not 200. The loop below would then log WARN forever AND release `caffeinate`,
#      letting the Mac sleep the session away. A keepalive that reports a problem it is
#      itself causing is worse than no keepalive.
#   2. The URL was computed BEFORE .env was read and then recomputed inside the `if`,
#      from `${IBKR_GATEWAY_URL%/}` — unset if .env exists but omits the var. Executed:
#      that yields the bare string `/tickle`, and `set -u` does NOT catch it because
#      `${var%pattern}` is not a plain expansion.
#
# Both failures are silent and look identical to "the gateway is down". Hence: read the one
# line first, default once with the suffix, then refuse to start on anything unusable.
ENV_FILE="$(dirname "$0")/../.env"

# Prints the value of the LAST `IBKR_GATEWAY_URL` line of "$1", unquoted; returns 1 when
# there is none. Nothing else in the file is read, and nothing is executed.
gateway_url_from_env_file() {
    local line value
    line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?IBKR_GATEWAY_URL[[:space:]]*=' "$1" 2>/dev/null | tail -n 1)" || true
    [ -n "$line" ] || return 1
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"   # leading blanks
    value="${value%"${value##*[![:space:]]}"}"   # trailing blanks
    # Quoted: the text up to the closing quote (a comment may follow it). Unquoted: up to
    # an inline ` #`. python-dotenv's rule in both cases.
    case "$value" in
        \"*) value="${value#\"}"; value="${value%%\"*}" ;;
        \'*) value="${value#\'}"; value="${value%%\'*}" ;;
        *)   value="${value%%[[:space:]]#*}"; value="${value%"${value##*[![:space:]]}"}" ;;
    esac
    printf '%s' "$value"
}

GATEWAY_URL="https://localhost:5055/v1/api"
if [ -f "$ENV_FILE" ]; then
    if url="$(gateway_url_from_env_file "$ENV_FILE")" && [ -n "$url" ]; then
        GATEWAY_URL="$url"
    fi
fi
TICKLE_URL="${GATEWAY_URL%/}/tickle"

case "$TICKLE_URL" in
    http://*|https://*) ;;
    *)
        echo "[ibkr-keepalive] FATAL: IBKR_GATEWAY_URL is set but unusable — the tickle" >&2
        echo "  URL came out as '${TICKLE_URL}'. Refusing to start: a keepalive that" >&2
        echo "  cannot reach the gateway would log WARN forever and release caffeinate," >&2
        echo "  which reads exactly like the gateway being down." >&2
        exit 1
        ;;
esac

# `--print-url`: what the daemon would tickle, then exit — the test seam, and a one-line
# diagnostic for the operator. Nothing is contacted.
if [ "${1:-}" = "--print-url" ]; then
    echo "$TICKLE_URL"
    exit 0
fi

# 0 (true) only when a LIVE process is holding the lock.
is_suspended() {
    [ -f "$SUSPEND_LOCK" ] || return 1
    local pid
    pid=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
          "$SUSPEND_LOCK" 2>/dev/null) || return 1
    [ -n "$pid" ] || return 1                 # malformed -> fail open
    kill -0 "$pid" 2>/dev/null || return 1    # dead owner -> stale -> fail open
    return 0
}

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
    # Suspended: a login or recovery owns the gateway. Do not tickle, and do not hold
    # the machine awake on behalf of a session that does not exist yet.
    if is_suspended; then
        if [ "$LAST_STATE" != "SUSPENDED" ]; then
            echo "${ts}  SUSPENDED  — a login or recovery is in progress; not tickling"
            LAST_STATE="SUSPENDED"
        fi
        return
    fi
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
