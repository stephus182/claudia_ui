#!/bin/bash
# ClaudIA full-stack launcher.
# Starts the IBKR Client Portal Gateway (via IB_MCP docker-compose),
# guides through browser login, then starts the Chainlit UI.
#
# Usage: ./start-claudia.sh
#
# Requires:
#   - Docker Desktop installed
#   - IB_MCP repo cloned; IBKR_GATEWAY_COMPOSE_PATH set in .env
#   - .venv active (or run from the claudia_ui directory)

set -e
cd "$(dirname "$0")"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

GATEWAY_COMPOSE_PATH="${IBKR_GATEWAY_COMPOSE_PATH:-}"
GATEWAY_URL="${IBKR_GATEWAY_URL:-https://localhost:5055/v1/api}"
# Strip trailing /v1/api to get the base URL for health-check and login page
GATEWAY_BASE=$(echo "$GATEWAY_URL" | sed 's|/v1/api||')

# ── 1. Verify IB_MCP path is configured ──────────────────────────────────────
if [ -z "$GATEWAY_COMPOSE_PATH" ]; then
  echo "✕ IBKR_GATEWAY_COMPOSE_PATH is not set in .env"
  echo "  Clone IB_MCP and set: IBKR_GATEWAY_COMPOSE_PATH=/path/to/IB_MCP"
  exit 1
fi
if [ ! -f "$GATEWAY_COMPOSE_PATH/docker-compose.yml" ]; then
  echo "✕ No docker-compose.yml found at: $GATEWAY_COMPOSE_PATH"
  exit 1
fi

# ── 2. Ensure Docker Desktop is running ──────────────────────────────────────
if ! docker info > /dev/null 2>&1; then
  echo "▶ Docker Desktop not running — starting it..."
  open -a Docker
  echo "  Waiting for Docker to be ready..."
  until docker info > /dev/null 2>&1; do
    printf "."
    sleep 2
  done
  echo ""
  echo "  Docker is ready."
fi

# ── 3. Start the IBKR gateway container ──────────────────────────────────────
cd "$GATEWAY_COMPOSE_PATH"
if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "api_gateway"; then
  echo "▶ IBKR gateway already running — restarting for clean session..."
  docker compose restart api_gateway
else
  echo "▶ Starting IBKR gateway..."
  docker compose up -d api_gateway
fi
cd - > /dev/null
echo ""

# ── 4. Wait for gateway process to be reachable ──────────────────────────────
echo "▶ Waiting for IBKR gateway at $GATEWAY_BASE ..."
for i in $(seq 1 30); do
  STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$GATEWAY_BASE/v1/api/iserver/auth/status" 2>/dev/null)
  if echo "$STATUS" | grep -qE '^[2-5]'; then
    break
  fi
  printf "."
  sleep 2
done
echo ""

# ── 5. Open login page in Chrome ─────────────────────────────────────────────
echo "▶ Opening IBKR login page in Chrome..."
open -a "Google Chrome" "$GATEWAY_BASE"
echo ""
echo "  Complete login in Chrome:"
echo "    1. Enter your IBKR username and password"
echo "    2. Complete 2FA (challenge code → IBKR Mobile → response code)"
echo "    3. Wait for 'Client login succeeds'"
echo ""

# Flush any buffered stdin
read -r -t 0.1 _discard 2>/dev/null || true
printf "Press Enter once Chrome shows 'Client login succeeds'... "
read -r
echo ""

# ── 6. Verify authentication ─────────────────────────────────────────────────
echo "▶ Verifying IBKR session..."
AUTHED=0
for i in $(seq 1 15); do
  RESULT=$(.venv/bin/python3 - <<'PYEOF' 2>/dev/null
from dotenv import load_dotenv
load_dotenv()
import os
from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
cfg = Config.from_env()
ibkr = IBKRClient(config=cfg, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")))
print("ok" if ibkr.ping() else "fail")
PYEOF
)
  if [ "$RESULT" = "ok" ]; then
    AUTHED=1
    break
  fi
  printf "."
  sleep 2
done
echo ""

if [ "$AUTHED" = "0" ]; then
  echo "  ✕ Session not verified — gateway responded but ping failed."
  echo "    Go back to Chrome, reload $GATEWAY_BASE, log in again,"
  echo "    then press Enter."
  echo ""
  read -r -t 0.1 _discard 2>/dev/null || true
  printf "Press Enter to retry... "
  read -r
  RESULT=$(.venv/bin/python3 - <<'PYEOF' 2>/dev/null
from dotenv import load_dotenv
load_dotenv()
import os
from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
cfg = Config.from_env()
ibkr = IBKRClient(config=cfg, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")))
print("ok" if ibkr.ping() else "fail")
PYEOF
)
  if [ "$RESULT" = "ok" ]; then
    AUTHED=1
  else
    echo "  ✕ Still not authenticated."
    echo "    Starting ClaudIA anyway — IBKR tools will show an error until you log in."
    echo ""
  fi
fi

[ "$AUTHED" = "1" ] && echo "  ✔ IBKR session active."
echo ""

# ── 7. Start ClaudIA ──────────────────────────────────────────────────────────
echo "▶ Starting ClaudIA..."
echo ""
.venv/bin/chainlit run claudia/app.py
