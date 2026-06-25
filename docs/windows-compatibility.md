# Windows Compatibility — Known Hurdles

This document records platform-specific issues discovered during macOS development
that will require attention before ClaudIA can run on Windows. Nothing here is built
yet — this is a reference for future implementation.

Scope: both `claudia_ui` and `ibkr_core_mcp` (covered together since claudia_ui
is the integration layer).

---

## Summary

| Severity | Count | Notes |
|---|---|---|
| Blocking | 3 | Must fix before Windows works at all |
| Significant | 4 | Core features degrade without a fix |
| Minor | 3 | Workarounds exist or low-impact |

---

## Blocking Issues

### B1 — Touch ID / biometric gate (ibkr_core_mcp)

**File:** `ibkr_core_mcp/human_auth.py`

**Problem:** The order staging security gate uses macOS `LocalAuthentication` framework
(`Security` entitlement, `LAContext.evaluatePolicy`). This is macOS-only.

**Windows equivalent:** Windows Hello / `Windows.Security.Credentials.UI.UserConsentVerifier`
(WinRT API, available in Python via `pythonwinrt` or `pywin32`). Alternatively,
a password prompt dialog (tkinter) as a fallback — weaker but functional.

**Impact:** Without this, the biometric gate fails silently or errors on Windows.
The tkinter confirmation dialog (Gate 2) still works — only Gate 1 is broken.

---

### B2 — Docker `host.docker.internal` in tickler (ibkr_core_mcp)

**File:** `ibkr_core_mcp/gateway/manager.py` line ~154

**Problem:** The gateway container's built-in tickler calls:
```
https://host.docker.internal:{port}/v1/api/tickle
```
`host.docker.internal` is automatically available on Docker Desktop for macOS and
Windows. On **plain Linux Docker** (e.g. a future server deployment), it requires
an extra flag:
```python
"--add-host=host.docker.internal:host-gateway"
```
added to the `docker run` command in `GatewayManager.start()`.

**Windows Docker Desktop:** No change needed — `host.docker.internal` resolves natively.

**Impact:** Tickler silently fails on Linux, IBKR session times out without ClaudIA running.

**Fix:** Detect platform in `GatewayManager.start()` — add `--add-host` only on Linux.

---

### B3 — Shell scripts (claudia_ui + ibkr_core_mcp)

**Files:** `scripts/ibkr-keepalive.sh`, `start-claudia.sh`, `ibkr_core_mcp/gateway/run_gateway.sh`, `tickler.sh`, `healthcheck.sh`

**Problem:** `.sh` scripts do not run natively on Windows. The internal Docker
scripts (`run_gateway.sh`, `tickler.sh`, `healthcheck.sh`) run inside a Linux
container — they are fine. The host-side scripts (`start-claudia.sh`,
`ibkr-keepalive.sh`) need PowerShell equivalents.

**Note:** `ibkr-keepalive.sh` is largely redundant (Docker tickler handles it), so only
`start-claudia.sh` is truly blocking.

**Fix:** Write `start-claudia.ps1`. The Docker gateway and Chainlit commands are
cross-platform; only the shell syntax needs porting.

---

## Significant Issues

### S4 — TradingView Desktop launch (claudia_ui)

**File:** `claudia/tradingview.py` `launch_tradingview()`

**Problem:**
```python
subprocess.run(["open", "-a", "Trading View", "--args", "--remote-debugging-port=9222"])
```
`open -a` is macOS-only. Windows equivalent:
```python
subprocess.Popen([r"C:\...\TradingView.exe", "--remote-debugging-port=9222"])
```
The TradingView executable path on Windows is not standardized (varies by install location).

**Fix:** Detect `platform.system()` and use `subprocess.Popen` with a configurable
`TRADINGVIEW_EXE_PATH` env var on Windows. Add `start /D` fallback if path not set.

---

### S5 — Docker Desktop launch (ibkr_core_mcp)

**File:** `ibkr_core_mcp/gateway/manager.py` `ensure_docker_running()`

**Problem:**
```python
subprocess.run(["open", "-a", "Docker"], check=True)
```
`open -a` is macOS-only. Raises `GatewayError` on non-macOS if Docker is not already
running, which means the auto-launch feature is silently disabled on Windows.

**Fix:** Already partially handled — the code raises `GatewayError` on non-macOS.
Windows fix: `subprocess.run(["start", "", "Docker Desktop"])` or
`subprocess.run(["C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"])`.

---

### S6 — BrowserCookieAuth cookie extraction (ibkr_core_mcp)

**File:** `ibkr_core_mcp/auth.py` `BrowserCookieAuth`

**Problem:** Uses `browser_cookie3` to read Chrome/Firefox cookies for `localhost`.
The library works on Windows but the cookie database path differs, and Chrome's
cookie encryption on Windows uses DPAPI (Windows Data Protection API) rather than
macOS Keychain.

**Status:** `browser_cookie3` claims Windows support, but DPAPI decryption has been
unreliable in some versions. Needs explicit testing.

**Fix:** Test `browser_cookie3` on Windows with a live IBKR gateway session. If
cookie extraction fails, fall back to `NoAuth` (gateway session is already established
via browser login — cookies may not be strictly required for all endpoints).

---

### S7 — File permission model (claudia_ui)

**File:** `CLAUDE.md` setup instructions

**Problem:** `chmod 600 docs/context.md docs/principles.md` has no equivalent on
Windows. Windows uses ACLs, not Unix permissions.

**Impact:** The setup instruction silently does nothing on Windows. `context.md` and
`principles.md` remain world-readable.

**Fix:** Document the Windows equivalent in setup:
```powershell
icacls docs\context.md /inheritance:r /grant:r "$env:USERNAME:(R,W)"
icacls docs\principles.md /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```
Or rely on NTFS user-profile directory permissions (files under `%USERPROFILE%`
are already user-private by default).

---

## Minor Issues

### M8 — Default file paths (ibkr_core_mcp)

**File:** `ibkr_core_mcp/config.py` (and env vars)

**Problem:** Default paths use Unix conventions:
- `IBKR_SQLITE_PATH` defaults to `~/.ibkr_core/store.db`
- `GDRIVE_TOKEN_FILE`, `GDRIVE_CREDENTIALS_FILE` — typically set to `~/.config/...`

**Impact:** `~` expands correctly via `Path.expanduser()` on Windows (`C:\Users\name`),
so this is mostly fine. Verify all path construction uses `pathlib.Path` (not string
concatenation with `/`).

**Status:** Low risk — `pathlib` is already used throughout. Spot-check on Windows.

---

### M9 — `watchdog` file monitoring (claudia_ui)

**File:** `claudia/context_loader.py`

**Problem:** `watchdog` uses `inotify` on Linux and `FSEvents` on macOS. On Windows
it falls back to `ReadDirectoryChangesW`. Performance is fine; behavior is equivalent.

**Status:** Officially supported. No changes needed. Mention in Windows setup docs
that antivirus software can interfere with `watchdog` on Windows.

---

### M10 — Console / terminal colors and emoji (claudia_ui)

**Problem:** Log output and some terminal messages use ANSI escape codes and Unicode
symbols (`✅`, `⚠️`, `▶`). Windows Command Prompt does not support ANSI by default;
PowerShell and Windows Terminal do.

**Status:** Not blocking. Chainlit's web UI is unaffected. Terminal output may look
garbled in cmd.exe. No fix needed unless cmd.exe support is a requirement.

---

## Not an Issue (confirmed cross-platform)

| Component | Reason |
|---|---|
| Chainlit web server | Pure Python, platform-agnostic |
| Anthropic SDK | Platform-agnostic |
| SQLite / claudia.db | stdlib, works everywhere |
| Docker gateway container | Linux container, identical on all hosts |
| `tickler.sh` / `run_gateway.sh` | Run inside Linux container |
| `tkinter` confirmation dialog | Ships with CPython on all platforms |
| `requests` / HTTP calls | Platform-agnostic |
| GDrive OAuth | Platform-agnostic |
| HMDS retry logic | Platform-agnostic |

---

## Implementation Order (when the time comes)

1. **B3** — `start-claudia.ps1` (unblocks basic startup)
2. **B2** — `--add-host` flag for Linux (low-risk one-liner)
3. **S5** — Windows Docker launch in `GatewayManager`
4. **S4** — Windows TradingView launch + `TRADINGVIEW_EXE_PATH` env var
5. **S6** — Test `BrowserCookieAuth` on Windows, document fallback
6. **B1** — Windows Hello biometric gate (most complex — defer until needed)
7. **S7 / M8 / M9 / M10** — documentation and minor polish

*Last updated: 2026-06-25*
