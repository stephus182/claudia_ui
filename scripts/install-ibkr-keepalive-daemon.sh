#!/usr/bin/env bash
# install-ibkr-keepalive-daemon.sh — runs ibkr-keepalive.sh as a launchd
# LaunchAgent so the IBKR Gateway session is protected (tickled + kept
# awake) independent of whether ClaudIA, a terminal, or a specific script
# is running. Install once; it survives ClaudIA restarts, terminal closes,
# and (via KeepAlive) crashes of the daemon itself.
#
# Usage:
#   ./scripts/install-ibkr-keepalive-daemon.sh              # install + load
#   ./scripts/install-ibkr-keepalive-daemon.sh --uninstall   # unload + remove
#
# Logs: ~/Library/Logs/claudia-ui/ibkr-keepalive.log (+ .err.log)

set -euo pipefail

LABEL="com.claudia-ui.ibkr-keepalive"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/claudia-ui"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_PATH="${REPO_DIR}/scripts/ibkr-keepalive.sh"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    echo "[install-ibkr-keepalive-daemon] Uninstalled ${LABEL}"
    exit 0
fi

mkdir -p "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_PATH}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/ibkr-keepalive.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/ibkr-keepalive.err.log</string>
</dict>
</plist>
EOF

# Reload cleanly if already installed. bootout is asynchronous — launchctl
# can return before the service is fully torn down, which makes an
# immediate bootstrap fail with "Input/output error" (race, not a real
# failure). Poll until the service is actually gone before reloading.
if launchctl print "gui/$(id -u)/${LABEL}" &>/dev/null; then
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    for _ in $(seq 1 20); do
        launchctl print "gui/$(id -u)/${LABEL}" &>/dev/null || break
        sleep 0.25
    done
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST"

# Verify it's actually running rather than trusting bootstrap's exit code alone.
sleep 1
if ! launchctl print "gui/$(id -u)/${LABEL}" &>/dev/null; then
    echo "[install-ibkr-keepalive-daemon] ERROR: bootstrap reported success but the service" >&2
    echo "  is not registered with launchd. Check ${LOG_DIR}/ibkr-keepalive.err.log" >&2
    exit 1
fi

echo "[install-ibkr-keepalive-daemon] Installed and loaded ${LABEL}"
echo "[install-ibkr-keepalive-daemon] Logs: ${LOG_DIR}/ibkr-keepalive.log"
echo "[install-ibkr-keepalive-daemon] Uninstall: ./scripts/install-ibkr-keepalive-daemon.sh --uninstall"
