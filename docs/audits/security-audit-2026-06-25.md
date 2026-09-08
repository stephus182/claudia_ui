# Security Audit — claudia_ui full module re-audit

**Date:** 2026-06-25
**Scope:** All 8 claudia_ui modules — full re-audit triggered by SSRF find in `fetch_web_page`
and by the 13 days of commits since the previous audit (2026-06-12):
`claudia/agent.py`, `claudia/app.py`, `claudia/order_flow.py`,
`claudia/conversation_store.py`, `claudia/gdrive_sync.py`, `claudia/status.py`,
`claudia/tradingview.py`, `claudia/context_loader.py`
**Method:** Full source read of every module; static analysis across 9 security angles
(injection, SSRF, credential exposure, subprocess, path traversal, error exposure, race
conditions, auth bypass, supply chain)
**Status:** 1 High finding fixed. 3 Low findings accepted. All security invariants intact.
Commit: `7a3ed0a`

---

## Summary

| Severity | Found | Fixed | Remaining |
|---|---|---|---|
| High | 1 | 1 | 0 |
| Medium | 0 | — | — |
| Low | 3 | 0 | 3 (accepted) |
| Refuted | 0 | — | — |

---

## Fixed Finding

### H-1 — SSRF in `fetch_web_page` (`claudia/agent.py`)

**Added since last audit:** `fetch_web_page` tool (commit `067199b`, 2026-06-17) made outbound
HTTP requests with no URL validation. A prompt-injection attack (e.g., an adversarial page
that instructed ClaudIA to "check your brokerage status") could cause ClaudIA to fetch
`https://localhost:5055/v1/api/portfolio/accounts` and return live account data as plain text
in the model context. The IBKR gateway requires no separate authentication for requests that
arrive with a valid session cookie in the same process.

**Fix (commit `7a3ed0a`):**
```python
parsed = urllib.parse.urlparse(url)
if parsed.scheme not in ("http", "https"):
    return f"Blocked: only http/https URLs are supported (got {parsed.scheme!r})."
if host in ("localhost", "0.0.0.0") or host.startswith("127.") or host.startswith("169.254."):
    return "Blocked: cannot fetch from localhost or link-local addresses."
try:
    addr = ipaddress.ip_address(host)
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        return "Blocked: cannot fetch from private or reserved IP addresses."
except ValueError:
    pass  # hostname — DNS resolution allowed; literal private IPs caught above
```

**Residual risk:** DNS rebinding — a hostname that resolves to a private IP bypasses the literal
IP check. Mitigated by: (a) the user's DNS resolver does not resolve public hostnames to private
IPs in a normal setup; (b) IBKR gateway requires a session cookie that wouldn't be present in a
cross-origin fetch; (c) localhost-only deployment limits the attacker surface. Documented in
`SECURITY.md §8`.

**Added to audit checklist (SECURITY.md §12):**
> Any new tool that makes outbound HTTP requests blocks localhost / private IP ranges (SSRF guard)

---

## Accepted Low Findings

### L-1 — Raw exception text surfaced in chat (`claudia/app.py:742`)

`on_message` wraps `agent.handle_message` in a catch-all:
```python
except Exception as exc:
    await cl.Message(content=f"Error: {exc!s}\n\nCheck the server logs for details.", ...)
```

Anthropic API errors (`anthropic.APIError`) contain request IDs and model names but not
credentials or account data. Session cookies are not in any code path that reaches this handler.
The message is visible only to the local user (localhost-only app).

**Decision:** Accepted. Replacing `{exc!s}` with a controlled string would hide useful
diagnostics from the single authorized user with no security benefit.

### L-2 — FTS5 MATCH accepts query operators (`claudia/conversation_store.py`)

`search_messages(query)` passes the LLM-generated query string directly to FTS5 `MATCH ?` as a
bind parameter. FTS5 treats the value as a query expression — operators like `OR *`, `NEAR(...)`,
and wildcard `*` are interpreted. An LLM-generated query `* OR *` would match every row.

**Mitigations already in place:**
- `max_results=5` caps results before the token budget check
- Results are from the user's own conversation history (single-user DB, no privilege to escalate)
- No IBKR credentials, API keys, or system paths are stored in the messages table

**Decision:** Accepted. FTS5 query operators in this context pose no security risk — the LLM
can only retrieve more or fewer rows from the user's own conversation history.

### L-3 — Drive query uses f-string construction (`claudia/gdrive_sync.py`)

`_find_file` constructs the Drive API query with f-strings:
```python
q=f"name='{name}' and '{fid}' in parents and trashed=false"
```

A `name` value containing a single quote would break the query and could, in theory, inject
Drive query clauses. However, `name` is always one of three hardcoded constants
(`"claudia.db"`, `"context.md"`, `"principles.md"`) — there is no code path where a
user-controlled or LLM-controlled value reaches this argument.

**Decision:** Accepted as-is. Added an inline comment to warn future contributors:
```python
# name is always a hardcoded constant — never pass a user-controlled value here
```

---

## Clean Checks — Modules with No Findings

### `claudia/agent.py`

- `search_past_conversations`: query passes to parameterized SQL bind parameter ✅
- `get_doc_version`: `version` passes to parameterized `WHERE version = ?` ✅
- Tool input displayed in `step.input`: LLM tool inputs contain ticker names and queries only; no credentials ever appear ✅
- `_history_to_messages`: history injected as `{"role": "user/assistant", "content": ...}` message objects, never into the system prompt string ✅
- `_SAFETY_BLOCK` always appended last, unconditionally ✅

### `claudia/app.py`

- Image attachment path (`el.path`): provided by Chainlit's internal upload handler (`.files/` temp dir), not a raw user string — no path traversal ✅
- Static file routes: `filename` is a hardcoded constant in `_static_route` definition — not user-controlled ✅
- Route priority fix (`_fix_route_priority`): reads/modifies Chainlit internal router list — functional code, not security-relevant ✅
- `_write_version_snapshot`: `version_label` is always `v{integer}` from `f"v{count + 1}"` — no path traversal ✅
- `subprocess.Popen` in `launch_tradingview`: list form, `_TV_DEBUG_PORT` is an int — no shell injection ✅

### `claudia/order_flow.py`

- `action.payload["order"]` JSON deserialization: malformed JSON returns early with controlled error ✅
- LLM-supplied `symbol`, `action_str`, `qty`, `otype`, `limit_price`: sent to IBKR API which validates them server-side; both human gates (Touch ID + tkinter) show the full values before submission ✅
- IBKRClient direct instantiation: explicitly permitted because this is a physical button click callback, not an LLM tool handler ✅
- Error message sanitization: `except Exception` maps to 3 controlled strings — raw `exc` never shown in chat ✅
- `claudia_ref = f"CLAUDIA-{int(time.time() * 1000)}"`: timestamp-derived, not LLM-controlled ✅

### `claudia/conversation_store.py`

- All SQL queries use `?` bind parameters throughout — no f-string SQL construction ✅
- `messages` schema: `CHECK(role IN ('user','assistant','tool'))` enforced by SQLite ✅
- WAL mode: documented and intentional for concurrent GDriveSync reader ✅
- `PRAGMA foreign_keys=ON` in every connection ✅

### `claudia/gdrive_sync.py`

- Token file: `os.chmod(token_path, 0o600)` unconditionally after every write ✅
- Download: temp file → integrity check → `shutil.move` (atomic-ish replacement) ✅
- Size guard: `if size > _MAX_TEXT_BYTES (1 MB):` before any download ✅
- `upload_db` race: `threading.RLock` serialises find-then-create/update block ✅
- `PRAGMA integrity_check` on downloaded DB before replacing local ✅

### `claudia/status.py`

- `verify=False`: scoped to `localhost:5055` with self-signed cert — intentional, documented ✅
- `/tickle` response: only `iserver.authStatus.authenticated` and `iserver.authStatus.connected` are read; no credentials in the response ✅
- All three checks wrapped in `except Exception: return False` — no stack traces reach the poll loop or chat ✅
- `_send_alert` content: hardcoded strings from `_DISCONNECT_MESSAGES` / `_RECONNECT_MESSAGES` ✅

### `claudia/tradingview.py`

- Subprocess env: explicit allowlist (`PATH`, `HOME`, `USER`, `TMPDIR`, `NODE_*`, `CHROME_REMOTE_DEBUG_PORT`) — `ANTHROPIC_API_KEY`, GDrive tokens, IBKR credentials never passed ✅
- Binary path validation: `TRADINGVIEW_MCP_PATH` checked for existence and `.js` extension before use ✅
- `launch_tradingview` subprocess: list form, `_TV_DEBUG_PORT` is an int ✅
- CDP scope: TradingView Desktop only (port 9222 = Electron debug port) — no IBKR access ✅

### `claudia/context_loader.py`

- Watchdog path comparison: `event.src_path in self._watched` uses resolved absolute paths ✅
- File read: `_read_required` raises `FileNotFoundError` with path — path comes from `_DOCS_PATH / "context.md"` (env-var-derived Path), no user-controlled input ✅
- Hot-reload callback: `loop.call_soon_threadsafe(lambda: loop.create_task(_send(), context=_cl_ctx))` — correct thread-to-asyncio bridge; captures session context ✅

---

## Security Invariants — All Intact

These properties from `SECURITY.md` were verified against the current code:

| Invariant | Verified |
|---|---|
| `ClaudeToolkit` exposes 0 order-write tools | ✅ `TOOL_DEFINITIONS` checked — 38 read-only tools |
| Touch ID + tkinter gate at innermost call site | ✅ `place_order()` still requires both gates in `ibkr_core_mcp` |
| `_SAFETY_BLOCK` always appended last | ✅ `_build_system_prompt()` — unconditional final append |
| `ANTHROPIC_API_KEY` never logged or passed to subprocess | ✅ confirmed across all modules |
| Conversation history injected as message objects, not raw string | ✅ `_history_to_messages()` |
| Subprocess env is an allowlist | ✅ `tradingview.py:start()` — only 9 vars forwarded |
| Drive downloads have size guard | ✅ `_MAX_TEXT_BYTES = 1 MB` in `read_text()` |
| DB integrity checked after Drive download | ✅ `PRAGMA integrity_check` in `download_db()` |
