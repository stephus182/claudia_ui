# ClaudIA — Connectivity Guide

Three external services are monitored continuously. Each has a status light in the UI
header (green / red / gray), polled every 60 seconds by `ConnectivityChecker`.

---

## Overview

| Service | Light | Check method | What "green" means |
|---|---|---|---|
| IBKR Gateway | 🟢/🔴 | `GET /tickle` → parse `iserver.authStatus` | Session authenticated **and** connected to IBKR servers |
| Google Drive | 🟢/🔴 | `GDriveSync.ping()` → `files().list` API round-trip | OAuth token valid, Drive API reachable |
| TradingView | 🟢/🔴/⚫ | TCP connect to `localhost:9222` | Desktop app running with `--remote-debugging-port=9222` |

Gray (⚫) means the service is not configured for this session (TradingView sidecar not
started). Gray never sends a disconnect alert.

---

## IBKR Gateway

### Check process

`ConnectivityChecker.check_ibkr()` calls `GET /v1/api/tickle` (3s timeout, SSL verify
disabled for localhost self-signed cert). The IBKR gateway returns HTTP 200 regardless
of auth state, so the JSON body is parsed:

```json
{
  "iserver": {
    "authStatus": {
      "authenticated": true,
      "connected": true
    }
  }
}
```

Both `authenticated` and `connected` must be `true` for the check to pass. A gateway
that is running but not logged in returns `authenticated: false` and triggers a red light.

**Side effect:** `/tickle` resets the IBKR session inactivity timer. Polling every 60s
prevents automatic session expiry while ClaudIA is running.

### Session lifecycle (verified against official docs, 2026-07-17)

Source: [IBKR Client Portal API — session lifecycle FAQ](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#tickle)
(scraped via Firecrawl — `interactivebrokers.com` 403s a direct `WebFetch`).

Two independent, non-overlapping timeout mechanisms:

| Mechanism | Threshold | Prevented by |
|---|---|---|
| Inactivity timeout | ~5–6 min without a request / `/tickle` | `ConnectivityChecker`'s 60s poll (well inside the window) |
| **Absolute session cap** | **24h, resets at midnight NY/Zug/HK** (whichever region the gateway connects to) | **Nothing — unavoidable.** A fresh browser + 2FA login is required at least once every 24h no matter how well the inactivity timer is serviced. Accepted as a known, permanent constraint — not a bug to chase. |

Daily IBKR server maintenance can also force a disconnect earlier than the 24h mark; IBKR's own
guidance is to restart the gateway after the maintenance window rather than expect continuity
through it.

**Soft-timeout recovery (implemented 2026-07-17, unit-tested — not yet live-verified):** when the
inactivity timer lapses, `/iserver/auth/status` returns `connected:true, authenticated:false` — a
state distinct from a hard disconnect. `ConnectivityChecker._run_checks()` detects this exact
signature, but only on a transition from a previously-confirmed `OK` state (never from `UNKNOWN`,
which covers the fragile first-seconds-after-login window, and never from `ERROR`) — and calls
`_attempt_soft_recovery()`, which POSTs `POST /iserver/auth/ssodh/init` (`publish:true,
compete:false`) to silently re-establish the session. If recovery succeeds, the disconnect is
invisible to the user — no alert fires, since the service never visibly left `OK` (the
transition-detection loop re-checks `check_ibkr()` before comparing states). If it fails,
behavior falls back exactly to the pre-existing manual browser+2FA flow — one normal disconnect
alert, identical to today. `compete` is hardcoded `false` and must never be changed — `true`
would force-evict a concurrent IBKR Mobile/TWS session. This is the current, non-deprecated
endpoint — `POST /iserver/reauthenticate` is explicitly marked **Deprecated** by IBKR and remains
banned from proactive use (it disrupts fresh logins — see `ibkr_core_mcp/client.py`'s
`reauthenticate()` docstring), unaffected by this change since it's a different endpoint.
Implementation: `claudia/status.py` — `_last_ibkr_auth_status` (auth-detail capture),
`_attempt_soft_recovery()` (the recovery call), wired into `_run_checks()`. 12 dedicated unit
tests cover every safety-relevant branch: never fires from `UNKNOWN`/`ERROR`/hard-disconnect,
successful recovery suppresses the alert, failed recovery (including a recovery that "succeeds"
but the re-check still fails differently) produces exactly one normal disconnect alert. **Not yet
live-verified** — needs a live-test protocol that deliberately lets an authenticated session idle
past ~6 minutes; see `docs/plans/2026-07-17-ibkr-soft-timeout-recovery.md` Task 5 for the
safety-scoped protocol (framed so the worst case is identical to today's status quo, since the
soft-timeout has already occurred naturally by the time recovery is tested).

**Competing sessions:** IBKR's own gateway walkthrough states you *"cannot be logged into the
account you are authenticating with anywhere else before you authenticate"* and that merely
closing another IBKR window/app (instead of using its "Log Out") *"may cause a stale login
session"* — confirming `check_ibkr()`'s `authStatus.competing` warning
(`claudia/status.py:102-103`) reflects a real, IBKR-documented failure mode: opening IBKR
Mobile/TWS/another browser tab during a live ClaudIA session can force-kick the gateway session.
Source: [Launching and Authenticating the Gateway](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/).

### ibkr_core_mcp ping

`IBKRClient.ping()` (used by tools, not the UI) uses a different endpoint:
`GET /iserver/auth/status`. It checks only `authenticated` (not `connected`) and
has a one-retry logic for the first-request IBKR quirk — the gateway returns
`authenticated: false` on the very first request of a new session even when fully
logged in. It calls `tickle()` and retries once after a 1-second pause.

The two pings serve different purposes:
- `ConnectivityChecker.check_ibkr()` — UI light, runs every 60s, keepalive side effect
- `IBKRClient.ping()` — pre-tool guard, runs on demand, handles startup quirk

### Reconnection process

**Automatic (session timeout):**
1. Status light turns red; in-chat alert: *"⚠️ IBKR Gateway disconnected"*
2. Open `https://localhost:5055` in your browser
3. Complete IBKR login + 2FA
4. `ConnectivityChecker` polls within 60s → detects `authenticated: true` → light turns green → in-chat alert: *"✅ IBKR Gateway reconnected"*

**Gateway container stopped:**
1. Status light turns red (connection refused, not HTTP 200)
2. Click **"Start IBKR Gateway"** button in the ClaudIA welcome message
   — or run `./start-claudia.sh` in a new terminal
3. Container starts; gateway Java process comes up (~30s)
4. Browser opens `https://localhost:5055` automatically
5. Complete login + 2FA; `ConnectivityChecker` detects reconnect

**Docker Desktop not running:**
Same as above but step 2 also launches Docker Desktop automatically (macOS only).

### Always-on keepalive daemon (shipped 2026-07-17)

`ConnectivityChecker`'s 60s tickle and `start-claudia.sh`'s `caffeinate` only protect the
session while ClaudIA's own process is running — the gap between stopping ClaudIA (e.g. a dev
restart) and starting it again was previously unprotected unless someone remembered to run
`scripts/ibkr-keepalive.sh` manually in a separate terminal.

`scripts/install-ibkr-keepalive-daemon.sh` installs `scripts/ibkr-keepalive.sh` as a macOS
LaunchAgent (`~/Library/LaunchAgents/com.claudia-ui.ibkr-keepalive.plist`, `RunAtLoad` +
`KeepAlive`), so the gateway is tickled every 55s and the Mac is kept awake **independent of
ClaudIA, terminals, or dev restarts** — install once, it survives logouts/crashes/reboots.
It only holds the `caffeinate -i` sleep-prevention assertion while the gateway actually responds
to `/tickle`, and releases it the moment the container goes unreachable, so it doesn't keep the
Mac permanently awake when nothing needs protecting.

```bash
./scripts/install-ibkr-keepalive-daemon.sh              # install + load
./scripts/install-ibkr-keepalive-daemon.sh --uninstall   # unload + remove
```

Logs: `~/Library/Logs/claudia-ui/ibkr-keepalive.log` (+ `.err.log`). Only logs on OK/WARN state
transitions, not every tick, to keep the log bounded over a long-running install.

Redundant with `ConnectivityChecker`'s own tickle when ClaudIA is running (both are idempotent
`GET /tickle` calls, well inside IBKR's `1 req/sec` pacing limit for that endpoint) — that's
intentional defense in depth, not a conflict.

---

## Google Drive

### Check process

`ConnectivityChecker.check_gdrive()` calls `GDriveSync.ping()`, which calls:

```python
svc.files().list(pageSize=1, fields="files(id)").execute()
```

This is the lightest valid Drive API call. It confirms:
- The OAuth token is present and not expired (auto-refreshed if needed)
- The Drive API is reachable over the network
- The Google account still has access

Falls back to token-file existence check if `GDriveSync` was not wired (i.e.
`GOOGLE_DRIVE_FOLDER_ID` is not set in `.env`).

### Reconnection process

Drive credentials do not expire during normal use (offline refresh tokens). If the light
goes red it means one of:

| Cause | Fix |
|---|---|
| Network unreachable | Restore internet connectivity; checker auto-recovers |
| Token file deleted | Re-run `ibkr_core_mcp` GDriveCache OAuth flow to regenerate `token_ibkr_core_mcp.json` |
| OAuth app revoked | Re-authorize via Google Account → Security → Third-party apps |

The checker auto-detects recovery on the next poll cycle.

### What syncs via Drive

| File | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (once per process) |
| `claudia.db` | local → Drive | Session stop |
| `context.md` | Drive → memory | Every session start |
| `principles.md` | Drive → memory | Every session start |
| `store.db` | local → Drive `account_data/` | After each successful Flex sync |

---

## TradingView

### Check process

`ConnectivityChecker.check_tradingview()` opens a TCP connection to
`localhost:9222` (Chrome DevTools Protocol port) with a 1-second timeout.

- TCP connects → TradingView Desktop is running with `--remote-debugging-port=9222`
- Connection refused / timeout → Desktop not running or launched without the flag

The check is independent of the MCP sidecar process. If the sidecar crashes but
TradingView Desktop is still open, the light stays green — which is correct, since
the sidecar can be restarted without restarting TradingView.

Status is `UNKNOWN` (gray) when `TradingViewBridge` was never started (e.g. sidecar
failed at session start and no bridge was created).

### Reconnection process

1. Status light turns red or gray
2. Click **"Launch TradingView"** button in the ClaudIA welcome message
   — or manually: `open -a "Trading View" --args --remote-debugging-port=9222`
3. ClaudIA polls CDP port for up to 30s
4. On success: sidecar restarts, tools become available, light turns green

**If sidecar fails after a TradingView update:** see
[`docs/tradingview-mcp-recovery.md`](tradingview-mcp-recovery.md) for the error
catalog and step-by-step recovery.

---

## Live Test Results

### 2026-06-24 — Connectivity audit

All three checks verified against live services:

| Test | Method | Result |
|---|---|---|
| IBKR authenticated session | `check_ibkr()` on live gateway after login | ✅ `True` |
| IBKR unauthenticated (manual disconnect) | `check_ibkr()` after logout | ✅ `False` (HTTP 200 but `authenticated: false`) |
| GDrive `ping()` | `GDriveSync.ping()` API round-trip | ✅ `True` |
| GDrive `upload_db()` without deadlock | `upload_db()` while session active | ✅ Complete in <2s |
| GDrive `download_db()` + integrity check | Round-trip download + `PRAGMA integrity_check` | ✅ 626KB, `ok` |
| `ConnectivityChecker.check_gdrive()` via ping | End-to-end through checker | ✅ `True` |

### Bugs found and fixed during audit

| Bug | Symptom | Fix | Commit |
|---|---|---|---|
| `check_ibkr()` checked HTTP status only | Green light when not logged in | Parse `iserver.authStatus` JSON | `3bb3302` |
| `check_gdrive()` checked token file only | Green light when API unreachable | `GDriveSync.ping()` round-trip | `04a59b4` |
| `upload_db()` deadlocked | Hung indefinitely when session active | `Lock` → `RLock` | `096e05b` |
| `upload_db()` WAL checkpoint blocked | Same hang, different cause | Remove checkpoint | `096e05b` |

Commit hashes updated 2026-07-14 — the originals (`ee49b9b`/`3170595`/`9780963`) no longer
resolve after the 2026-07-10 `git-filter-repo` history rewrite; these are the same fixes under
their new hashes.

---

## Implementation Reference

```
claudia/status.py          — ConnectivityChecker, check_ibkr(), check_gdrive(), check_tradingview()
claudia/gdrive_sync.py     — GDriveSync.ping(), upload_db(), download_db()
ibkr_core_mcp/client.py   — IBKRClient.ping(), tickle(), get_auth_status()
claudia/app.py             — ConnectivityChecker construction (passes gdrive_sync=)
claudia/assets/custom.js   — 5s status bar poll (POLL_MS) → GET /api/status → lights update
claudia/app.py:273-278     — GET /api/status route + response shape
```
