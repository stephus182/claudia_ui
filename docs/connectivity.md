# ClaudIA — Connectivity Guide

Three external services are monitored continuously. Each has a status light in the UI
header (green / red / gray), polled every 60 seconds by `ConnectivityChecker` — except
IBKR, whose reading comes from `GatewaySession`, the session owner, and is only *displayed*
by the checker (see below).

---

## Overview

| Service | Light | Check method | What "green" means |
|---|---|---|---|
| IBKR Gateway | 🟢/🔴 | `GatewaySession` phase — `/tickle` + `/sso/validate` + a `/portfolio/accounts` confirmation | Phase is `LIVE`: authenticated, connected **and** confirmed serving data |
| Google Drive | 🟢/🔴 | `GDriveSync.ping()` → `files().list` API round-trip | OAuth token valid, Drive API reachable |
| TradingView | 🟢/🔴/⚫ | TCP connect to `localhost:9222` | Desktop app running with `--remote-debugging-port=9222` |

Gray (⚫) means the service is not configured for this session (TradingView sidecar not
started). Gray never sends a disconnect alert.

### ⚠ IBKR lives in its own document now

The IBKR brokerage session — its eight phases, the suspend protocol, the login runbook, the
borrowed-session and IB Key failures, and the container image trap — **moved to
`docs/ibkr-gateway.md` on 2026-08-06.** It is not duplicated here.

It was split out because it is a different kind of thing from the other two: Google Drive
and TradingView answer *is it up?*, while IBKR is a state machine with a shared brokerage
session and a login that can fail in ways no retry fixes. The drift record makes the case —
of the fourteen stale claims found in this file on 2026-08-06, **every one was an IBKR
claim.**

What stays here: the status-dot mechanics above, and the two services below.

One IBKR fact belongs on this page because it is about the **dot**, not the session:
the light is green only when `GatewaySession`'s phase is `LIVE` — authenticated, connected
**and** confirmed against a real data endpoint. `check_ibkr()` performs no HTTP; it reads
the owner's cached state. `/portfolio/*` and `/iserver/*` are separate subsystems that have
been observed diverging, so a red dot does **not** mean account data is unavailable —
`docs/ibkr-gateway.md` § "The IBKR light is about the brokerage session" has the measured
three-state table.

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
claudia/status.py          — ConnectivityChecker: check_gdrive(), check_tradingview(), and
                             the alert-on-transition loop. check_ibkr() is a cached lookup
                             of GatewaySession, not a probe — it issues no HTTP
claudia/gdrive_sync.py     — GDriveSync.ping(), upload_db(), download_db()
claudia/tradingview.py     — CDP health probe behind check_tradingview()
claudia/panel_app.py       — ConnectivityChecker construction (passes gdrive_sync=);
                             pn.indicators.BooleanStatus dots updated in-session via a
                             periodic callback
# IBKR gateway modules are listed in docs/ibkr-gateway.md, not here.
# Post Phase-11 cutover: status is shown by in-session Panel BooleanStatus indicators.
# The Chainlit custom.js status bar polling GET /api/status was removed (no such HTTP route
# in panel_app — the dots are pushed over Panel's own websocket).
```
