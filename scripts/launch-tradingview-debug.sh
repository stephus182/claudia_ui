#!/usr/bin/env bash
# Launch (or relaunch) TradingView Desktop with the CDP remote-debugging port
# open, so the tradingview-mcp sidecar can drive it.
#
# WHY THIS EXISTS: the debug port (--remote-debugging-port=9222) can only be set
# at launch, so a TradingView already running without it must be quit and
# relaunched. Since 2026-09-04 ClaudIA's TradingView button does this same
# quit → relaunch → wait-for-CDP cycle itself (claudia/tradingview.py
# launch_tradingview mirrors this file; keep the two in step). This script is
# the Terminal route: for when ClaudIA is not running, or was started from a
# shell that cannot launch desktop apps (measured 2026-09-04: `open -a` from a
# Claude Code tool shell returns 0 and launches nothing).
#
# macOS only (TradingView Desktop + `open -a`). Safe to re-run: if CDP is
# already up it exits immediately without touching the running app.
#
# Usage:  ./scripts/launch-tradingview-debug.sh
# Exit:   0 = CDP port is up (ready);  1 = failed to open the port in time.
#
# TradingView auto-saves chart layouts to your account, so the quit below does
# not lose your workspace. Source for the debug-port approach:
# https://github.com/tradesdontlie/tradingview-mcp/blob/main/SETUP_GUIDE.md

set -euo pipefail

APP_NAME="TradingView"
DEBUG_PORT="${TV_DEBUG_PORT:-9222}"
WAIT_SECONDS="${TV_WAIT_SECONDS:-30}"

cdp_up() {
    # 0 if something is accepting connections on the CDP port, 1 otherwise.
    # nc -z is the most portable TCP probe available by default on macOS.
    nc -z localhost "$DEBUG_PORT" >/dev/null 2>&1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "✕ This helper is macOS-only. On other platforms launch TradingView with" >&2
    echo "  --remote-debugging-port=$DEBUG_PORT yourself." >&2
    exit 1
fi

if cdp_up; then
    echo "✅ TradingView CDP port $DEBUG_PORT already open — nothing to do."
    exit 0
fi

if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    echo "▶ TradingView is running without the debug port — quitting it first…"
    # Graceful quit (lets TradingView flush state); fall back to a hard kill.
    osascript -e "quit app \"$APP_NAME\"" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
        pgrep -x "$APP_NAME" >/dev/null 2>&1 || break
        sleep 1
    done
    if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
        echo "  …still running, forcing quit."
        pkill -x "$APP_NAME" >/dev/null 2>&1 || true
        sleep 2
    fi
fi

# Braced because an ellipsis follows. Measured 2026-08-12: bash 3.2 running this
# FILE from an automation shell (LANG=C.UTF-8) folds the multibyte `…` into the
# variable name and dies on `set -u` with "DEBUG_PORT…: unbound variable" — after
# the script had already quit TradingView, the worst possible place to stop. The
# same construct passed via `bash -c` on a command line, so this is not a pure
# locale story; the braces close it under every invocation tried (repro + fix both
# verified against the exact failing invocation). Interactive Terminal runs never hit it.
echo "▶ Launching TradingView with --remote-debugging-port=${DEBUG_PORT}…"
# Guarded so a not-installed TradingView gives a clear message instead of
# aborting on `set -e` before the timeout hint below.
open -a "$APP_NAME" --args --remote-debugging-port="$DEBUG_PORT" \
    || { echo "✕ Could not launch $APP_NAME — is TradingView Desktop installed?" >&2; exit 1; }

echo "▶ Waiting up to ${WAIT_SECONDS}s for the CDP port to come up…"
for _ in $(seq 1 "$WAIT_SECONDS"); do
    if cdp_up; then
        echo "✅ TradingView CDP port $DEBUG_PORT is ready."
        exit 0
    fi
    sleep 1
done

echo "✕ TradingView did not open CDP port $DEBUG_PORT within ${WAIT_SECONDS}s." >&2
echo "  Make sure TradingView Desktop is installed and try again." >&2
exit 1
