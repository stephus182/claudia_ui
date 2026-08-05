#!/usr/bin/env bash
# gateway-reset.sh — clear a stuck IBKR gateway session so a login can succeed.
#
# For the one failure this repo has actually hit (2026-08-05): the gateway holds an SSO
# session issued to a *different* IBKR client — IBKR Mobile — so it cannot authenticate
# as itself, and the login page rejects a correct 2FA code however often it is retried.
# Full diagnosis: docs/connectivity.md § A borrowed session.
#
# WHY A RESTART RATHER THAN `POST /logout`
#
# /logout works, and the session comes straight back. Three independent ticklers renew it
# every ~60s — the gateway container's own tickler.sh, the host launchd keepalive, and
# ClaudIA's ConnectivityChecker — so a release loses the race against whichever fires
# first. Restarting the container drops the session with nothing to race: it is held in
# the gateway's local process memory, not re-served from IBKR. That is what finally
# worked after /logout had been tried repeatedly and reported `{"status": true}` each
# time. The ticklers do not need to be stopped for this to work; they simply tickle an
# empty gateway (HTTP 401) until the login lands.
#
# THIS DESTROYS A SESSION. It refuses to run against a healthy one — see the guard below.
#
# Usage:
#   ./scripts/gateway-reset.sh          # check, restart, re-check
#   ./scripts/gateway-reset.sh --force  # skip the healthy-session guard

set -euo pipefail

cd "$(dirname "$0")/.."

FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

# The preflight module supplies every verdict; this script only orchestrates. Keeping the
# session logic in one place means the two can never disagree about what "free" means.
PREFLIGHT="python -m claudia.gateway_preflight"
EXIT_READY=0
EXIT_FREE=2

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Sourced rather than hardcoded: ibkr_core_mcp owns the container's identity, and a name
# duplicated here would silently rot the day that repo renames it.
CONTAINER=$(python -c \
  'from ibkr_core_mcp.gateway import GatewayManager; print(GatewayManager.CONTAINER_NAME)')

# Likewise the URL: gateway_preflight.gateway_url() honours IBKR_GATEWAY_URL, so hardcoding
# localhost:5055 here would make this script poll an address nobody uses — and print the
# wrong one to log into — on any non-default port. The API base carries a /v1/api suffix
# that the browser URL must not have.
API_URL=$(python -c 'from claudia.gateway_preflight import gateway_url; print(gateway_url())')
LOGIN_URL=${API_URL%/v1/api}

echo "── Before ─────────────────────────────────────────────────────────"
set +e
$PREFLIGHT
BEFORE=$?
set -e
echo

# The guard that matters. A working session is the one thing this script must never throw
# away: re-logging-in needlessly is exactly what escalates into the IB Key challenge, so a
# tool built to fix a login must not be able to break one.
if [[ $BEFORE -eq $EXIT_READY && $FORCE == false ]]; then
  echo "REFUSING: the session is LIVE and healthy. Nothing to reset."
  echo "Re-run with --force only if you genuinely intend to destroy it."
  exit 1
fi

echo "── Restarting ${CONTAINER} ────────────────────────────────────────"
docker restart "$CONTAINER" >/dev/null
echo "restarted; waiting for it to answer…"

# The gateway's Java process takes a few seconds to bind. Poll rather than sleep a fixed
# guess: an unanswered check here would read as "still broken" and send someone chasing a
# problem that had merely not finished booting.
for _ in $(seq 1 30); do
  if curl -sk --max-time 2 "${API_URL}/tickle" -o /dev/null; then
    break
  fi
  sleep 2
done
echo

echo "── After ──────────────────────────────────────────────────────────"
set +e
$PREFLIGHT
AFTER=$?
set -e
echo

if [[ $AFTER -eq $EXIT_FREE ]]; then
  # Unquoted heredoc so LOGIN_URL expands. Nothing else in the text is expandable.
  cat <<EOF
The slot is clear. Log in NOW, while it is:

  1. Open ${LOGIN_URL}
  2. Complete the login through to "Client login succeeds"
  3. Keep IBKR Mobile logged out until you are done

Then confirm with:  python -m claudia.gateway_preflight
It should read [OK] Session is LIVE.
EOF
else
  echo "Still not free. Re-read the verdict above — if it names another CLIENT_APP, that"
  echo "app must be logged out from its own Log Out menu item (closing it is not enough)."
fi

exit $AFTER
