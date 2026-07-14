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
