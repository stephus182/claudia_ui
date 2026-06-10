#!/bin/bash
# ClaudIA launcher — starts the IBKR gateway then Chainlit.
# Usage: ./start-claudia.sh

set -e
cd "$(dirname "$0")"

.venv/bin/python3 -c "
from ibkr_core_mcp.gateway import GatewayManager
import sys
gm = GatewayManager()
ok = gm.startup()
sys.exit(0)  # start ClaudIA regardless; ConnectivityChecker will show status
"

.venv/bin/chainlit run claudia/app.py
