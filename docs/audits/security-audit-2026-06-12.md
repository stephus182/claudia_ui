# Security Audit — claudia_ui + ibkr_core_mcp

**Date:** 2026-06-12  
**Scope:** All modules added or modified since the 2026-06-10 ibkr_core_mcp audit:
`claudia/gdrive_sync.py`, `claudia/tradingview.py`, `claudia/app.py`,
`ibkr_core_mcp/client.py`, `~/.tradingview-mcp/src/connection.js`  
**Method:** Multi-agent parallel static analysis (7 independent finder angles × 6 candidates → 1-vote verifier per candidate)  
**Status:** All 8 findings resolved. Commits: `3927dcd` (claudia_ui), `0162c94` (ibkr_core_mcp)

---

## Summary

| Severity | Found | Fixed | Remaining |
|---|---|---|---|
| High | 2 | 2 | 0 |
| Medium | 4 | 4 | 0 |
| Low | 2 | 2 | 0 |
| Refuted | 2 | — | — |

---

## Resolved Findings

### High

---

**H-1 — Full `os.environ` passed to tradingview-mcp Node subprocess** (`claudia/tradingview.py:186`)

`env = {**os.environ, ...}` copied every secret in the process environment —
`ANTHROPIC_API_KEY`, `GDRIVE_TOKEN_FILE`, `IBKR_FLEX_TOKEN`, all Drive and IBKR
credentials — into the Node.js sidecar's `process.env`. Any crash dump, `--inspect`
log, or compromised binary at the resolved path would exfiltrate all credentials.

**Fix:** Replaced with an explicit allowlist: `PATH`, `HOME`, `USER`, `TMPDIR`,
`TEMP`, `TMP`, `NODE_PATH`, `NODE_ENV`, `XDG_RUNTIME_DIR`, `CHROME_REMOTE_DEBUG_PORT`.

---

**H-2 — `tickle()` replaced authentication check with reachability-only check** (`claudia/app.py:452`)

The startup check was switched from `ping()` (which verifies `authenticated: true`
via `/iserver/auth/status`) to `tickle()` (which only checks that the HTTP process
returns 200) to work around an IBKR gateway quirk. This meant ClaudIA showed the
"connected" UI and launched three parallel IBKR data fetches without verifying the
user was actually authenticated.

**Fix:** Added a one-retry to `IBKRClient.ping()` in ibkr_core_mcp — on `authenticated: false`,
it calls `tickle()` to warm up the gateway and retries once after 1s. `app.py` restored to
`ping()`. The quirk is now handled at the right layer.

---

### Medium

---

**M-1 — `CHROME_REMOTE_DEBUG_PORT` env var silently ignored by sidecar** (`tradingview.py:186` / `connection.js:6`)

`~/.tradingview-mcp/src/connection.js` hardcoded `const CDP_PORT = 9222` and never
read environment variables. Setting `TRADINGVIEW_DEBUG_PORT=9333` in `.env` made
Python connect to 9333 but the Node sidecar still connected to 9222 — tool calls
failed silently because CDP was on the wrong port.

**Fix:** Patched `connection.js` to `parseInt(process.env.CHROME_REMOTE_DEBUG_PORT || '9222', 10)`.
Vendor archive re-created.

---

**M-2 — `os.chmod` missing after token file refresh** (`gdrive_sync.py:57`)

`os.open(path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)` only applies the `0o600` mode
when creating a new file. On an existing file (e.g., first written by google-auth-oauthlib
with default 0o644), `O_CREAT` is a no-op and permissions remain as-is after refresh.

**Fix:** Added `os.chmod(token_path, 0o600)` immediately after the `os.fdopen` write.

---

**M-3 — `read_text()` downloaded into an unbounded `io.BytesIO()` buffer** (`gdrive_sync.py:212`)

An oversized `context.md` or `principles.md` on Drive (malicious or accidental) would
be fully downloaded into memory on every session start, potentially causing an OOM kill.

**Fix:** Added a size check via Drive file metadata before downloading. Files larger than
1 MB are rejected with a warning and the local fallback is used instead.

---

**M-4 — `GDriveSync` not thread-safe under concurrent sessions** (`gdrive_sync.py`)

`GDriveSync` is a module-level singleton. `upload_db()` is called via `cl.make_async()`
(thread pool). Two sessions closing simultaneously could: (1) both find `file_id = None`
and both call `files().create()`, creating duplicate `claudia.db` entries in Drive;
(2) both call `creds.refresh()` and `os.open(O_TRUNC)` on the token file simultaneously,
racing on the write.

**Fix:** Added `threading.Lock` to `GDriveSync.__init__()`. The lock guards `_get_service()`
(token refresh + service build) and the `_find_file()` + create/update block in `upload_db()`.

---

### Low

---

**L-1 — `TRADINGVIEW_MCP_PATH` env var accepted with no validation** (`tradingview.py:57`)

The env-var fast path returned any value immediately, with no existence check and no
extension check — the only path of the 6-candidate discovery chain with zero implicit
validation. A broken symlink, directory path, or future refactor adding `shell=True`
could silently break or enable injection.

**Fix:** Added existence check (`Path.exists()`) and `.js` extension guard before
accepting the env-var value. Invalid values log a warning and fall through to the next
discovery candidate.

---

**L-2 — Binary selection had no audit trail** (`tradingview.py`)

The discovery function selected one of 6 candidate paths silently (only vendor-fallback
paths emitted a warning). There was no way to tell from logs which binary was actually
being executed.

**Fix:** `TradingViewBridge.start()` now logs `tradingview-mcp binary: <path>` at INFO
level unconditionally before starting the subprocess.

---

## Refuted Findings

**R-1 — Drive Files.list query injection via `_find_file()`**  
All `name` parameters are hardcoded string literals (`"claudia.db"`, `"context.md"`,
`"principles.md"`). The `folder_id` parameter comes from trusted config (env vars or Drive
API responses). No user-controlled data flows into the query string. Refuted.

**R-2 — `download_db()` truncates local file on partial download**  
`download_db()` already uses `tempfile.NamedTemporaryFile` + `shutil.move()` (atomic rename).
The local DB is only replaced after the download and `PRAGMA integrity_check` both succeed.
Refuted — the pattern was already correct.
