#!/bin/bash
# ClaudIA launcher — starts the IBKR gateway then Chainlit.
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

.venv/bin/python3 -c "
from ibkr_core_mcp.gateway import GatewayManager
import sys
gm = GatewayManager()
ok = gm.startup()
sys.exit(0)  # start ClaudIA regardless; ConnectivityChecker will show status
"

.venv/bin/chainlit run claudia/app.py
