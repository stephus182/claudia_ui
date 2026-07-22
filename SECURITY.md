# ClaudIA Security Architecture

This document describes the security model specific to `claudia_ui`. For the
underlying IBKR gateway security, see `ibkr_core_mcp/SECURITY.md`.

---

## 1. Threat Model

### New Attack Surface vs ibkr_core_mcp

`claudia_ui` adds a long-running LLM agent with persistent document injection and
conversation memory. The new principals and threats are:

| Principal | Threat | Mitigation |
|---|---|---|
| `docs/context.md` | Prompt injection: attacker modifies the file to override ClaudIA's behavior | File permissions (0o600); SHA-256 hash logged at session start; file is never executable |
| `docs/principles.md` | Prompt injection: attacker weakens risk rules | Same mitigations as context.md; ClaudIA cannot modify this file |
| Conversation history | Re-injection: past messages contained adversarial content that gets fed back | History loaded as structured `role:user/assistant` blocks, not raw system prompt injection |
| TradingView sidecar | Supply chain: `tradingview-mcp` npm package has full CDP access to TradingView Desktop | Accepted risk (personal local tool); subprocess env is a strict allowlist (`PATH`, `HOME`, `USER`, `TMPDIR`, `NODE_*`) — `ANTHROPIC_API_KEY`, GDrive tokens, and IBKR credentials are never passed; vendor archive provides known-good fallback; `docs/tradingview-mcp-recovery.md` covers incident response |

---

## 2. Order Execution Barriers

ClaudIA has **zero** tools for order execution. This is the most critical security property.

### What the LLM can do
- Call any of the 42 read-only `ClaudeToolkit` tools (positions, PnL, market data, backtests, etc.)
- Output an `order-proposal` JSON block as text in its response
- Call `preview_order` (whatif — read-only, no execution)

### What the LLM cannot do
- Call `place_order`, `modify_order`, `cancel_order`, or `reply_order`
- Initiate any network request to the IBKR gateway for write operations
- Access `IBKRClient` directly (only `ClaudeToolkit.execute()` is exposed to the agent loop)

### Order staging flow (human-in-the-loop)
1. ClaudIA outputs a text `order-proposal` block — this is just text, no side effects
2. `agent.py` parses it and calls `order_flow.render_order_proposal()` — renders a button
3. **Human physically clicks the button** — this is the first human gate
4. `IBKRClient.place_order()` fires:
   - **Gate 1:** Apple `LocalAuthentication` biometric (Touch ID, Face ID) — no password fallback
   - **Gate 2:** native macOS AppKit dialog (green/red banner by order side) with full order
     details + 60-second auto-cancel countdown; Return key disabled on the confirm button to
     prevent accidental submission. Falls back to `osascript` if the AppKit subprocess fails,
     then to a `tkinter` dialog only on non-macOS (`ibkr_core_mcp/order_confirm.py`)
5. Order submitted to IBKR only after both gates pass

No LLM prompt, no tool call, no conversation state, and no automation can bypass steps 3–5.

---

## 3. Principles Document Integrity

`docs/principles.md` defines the user's trading rules. ClaudIA is instructed to verify
every proposed action against this document before responding.

**File permissions:**
```bash
chmod 600 docs/context.md docs/principles.md
```
Only the file owner can read or modify these files.

**Hash verification:**
At session start, `context_loader.py` computes `SHA-256(context.md + principles.md)` and
stores it in `claudia.db → sessions.context_hash`. If the hash differs from the previous
session, ClaudIA logs a warning and notifies the user.

**Immutability from ClaudIA's perspective:**
ClaudIA has no tools that write to the filesystem. It cannot modify these files.

**Hardcoded prohibition:**
The safety block in `agent.py` (not loaded from any file) explicitly states:
> "You cannot instruct the user to modify their principles document."

---

## 4. Conversation Memory Security

Historical messages are stored in `data/claudia.db` and injected back into context.
This creates a potential attack surface where past messages containing adversarial
content could influence future responses.

**Mitigations:**

- History is loaded as structured `{"role": "user", "content": "..."}` message objects
  and appended to the `messages=` list. It is **never** injected raw into the system prompt.

- FTS5 search results (used for "past decisions" recall) are truncated at a configurable
  token budget (default: 2,000 tokens) before injection into context.

- Tool results from ibkr_core_mcp are sanitized by `_safe_error()` before being returned
  to the LLM. Raw IBKR API responses never appear in conversation context.

- `claudia.db` is local, single-user, and not accessible over the network.

---

## 5. Hardcoded Safety Block

`_SAFETY_BLOCK` (`claudia/agent.py:47-201`) is embedded directly in code and appended to
every system prompt. It is **not** loaded from any user-editable file and cannot be
overridden by `context.md` or `principles.md`. Modifications require a code change — a
deliberate developer action, not a document edit (CLAUDE.md Hard Rule 3).

**This section intentionally does not quote the block verbatim** — a byte-for-byte copy
here would duplicate content that changes whenever the prompt is tuned, and would go
stale exactly the way the previous version of this section did (it quoted only the first
of what are now 8 non-overridable subsections, and had never been updated to reflect the
other 7). Read `claudia/agent.py:47-201` directly for the authoritative current text. As
of this writing, the block's non-overridable subsections are:

- **ABSOLUTE CONSTRAINTS** — no order execution, no financial-advisor claims, principles
  check required before proposing any trade, cannot promise returns
- **DATA INTEGRITY** — every specific data point presented (price, balance, position, P&L,
  contract ID, etc.) must originate from a tool result or user-provided content in *this*
  conversation; inventing, guessing, or carrying over a plausible-looking value is
  explicitly prohibited, and uncertainty about a value's origin means treating it as invented
- **ORDER PROPOSAL FORMAT** / **ORDER CANCEL / MODIFY FORMAT** — the exact JSON schemas
  ClaudIA must use; at most one proposal block per message
- **ORDER PARAMETER IMMUTABILITY** / **MODIFY PARAMETER IMMUTABILITY** — user-specified
  order fields must be copied byte-for-byte, never rounded or "helpfully" adjusted; changing
  a field requires explicit user approval in a follow-up message (see §2/CLAUDE.md)
- **ORDER CANCEL / MODIFY RULES** — `order_id` must come from a real tool call made earlier
  in *this* conversation, never invented or reused across sessions; externally-placed orders
  (mobile/TWS) and non-editable orders must be refused, not proposed
- **TOOL RESULT FRESHNESS** — a "retry"/"check again"/"verify" request requires a genuinely
  fresh tool call in the current turn; restating, reconstructing, or fabricating a prior
  result and presenting it as freshly-fetched is explicitly named as a more serious
  violation than not knowing the answer. Added 2026-07-10 after a live-reproduced
  fabrication finding (3 instances in one session); live re-verified 2026-07-17.

---

## 6. TradingView MCP Sidecar

The `tradingview-mcp` Node.js process ([`tradesdontlie/tradingview-mcp`](https://github.com/tradesdontlie/tradingview-mcp))
is spawned as a subprocess by `claudia/tradingview.py`. It communicates with ClaudIA
via MCP stdio (local pipe, no network port). It communicates with TradingView Desktop
via Chrome DevTools Protocol on `localhost:9222`.

**Security properties:**

- **Minimal subprocess environment.** `TradingViewBridge.start()` passes a strict allowlist to the
  sidecar: `PATH`, `HOME`, `USER`, `TMPDIR`, `TEMP`, `TMP`, `NODE_PATH`, `NODE_ENV`,
  `XDG_RUNTIME_DIR`, and `CHROME_REMOTE_DEBUG_PORT`. `ANTHROPIC_API_KEY`, `GDRIVE_TOKEN_FILE`,
  `IBKR_FLEX_TOKEN`, and all other secrets are never inherited by the sidecar process.
- **CDP port end-to-end configurable.** `TRADINGVIEW_DEBUG_PORT` (default: `9222`) flows through
  to the sidecar via `CHROME_REMOTE_DEBUG_PORT`; `connection.js` reads this env var instead of
  hardcoding the port.
- **Binary path validated.** `TRADINGVIEW_MCP_PATH` is validated for existence and `.js` extension
  before use; invalid values fall through to the next discovery candidate with a warning log.
  The selected binary path is always logged at INFO level on start.
- **CDP scope is TradingView Desktop only.** Port 9222 is TradingView's Electron debug port.
  The sidecar can read and manipulate the TradingView UI — it cannot access IBKR or place trades.
- **Full CDP access accepted.** The sidecar has full Chrome DevTools Protocol access to TradingView
  Desktop — it can read DOM, execute JavaScript in the renderer, and inspect TradingView's internal
  state. This is intentional (required for 78-tool functionality) and accepted for a personal local
  tool with no remote access.
- **Tool surface area reduced.** `_CURATED_TOOLS` in `tradingview.py` limits what Claude can call
  to 16 high-value tools (of a 78-tool full sidecar set). The sidecar process itself has full
  access regardless of this filter.
- **PineScript injection** modifies the Pine Editor only. It does not execute strategies or trades.
- **Fallback.** If the sidecar fails to start, ClaudIA degrades to screenshot mode.

**Third-party risk and supply chain:**

`tradesdontlie/tradingview-mcp` is an actively maintained community package (~5k stars / 2.2k
forks, verified via repo scrape 2026-07-21 — up from the 3,500+ last recorded here; MIT-licensed,
0 pinned releases/tags, tracks `main` directly, per its own README not affiliated with or
endorsed by TradingView Inc.). The project intentionally tracks `HEAD` to receive TradingView
API compatibility updates automatically — consistent with having no tags to pin to instead.

Risk acceptance: this is a personal local tool with no remote access, and TradingView
cannot place IBKR orders. Financial blast radius is limited to TradingView UI data exposure.

**Controls in place:**
- Vendor archive — `scripts/archive-tv-mcp.sh` snapshots the working install after each
  verified upgrade to `vendor/tradingview-mcp/`. `_find_tv_mcp_bin()` tries
  `vendor/tradingview-mcp/src/server.js` (JS layout, requires `node_modules/`) then
  `vendor/tradingview-mcp/index.js` (legacy single-bundle) as steps 5 and 6 of a
  6-step discovery chain, after the live install paths are exhausted.
- `docs/tradingview-mcp-recovery.md` — incident response guide: 5 break patterns with exact
  log signatures, per-pattern fixes, and a direct CDP from Python fallback for the
  "sidecar permanently unavailable" scenario.

---

## 7. Secrets Management

- `.env` is in `.gitignore` and must never be committed.
- `ANTHROPIC_API_KEY` is never logged, never included in Chainlit message output,
  and never passed to the LLM as text.
- `claudia.db` does not store API keys or IBKR credentials.
- Google Drive credentials follow the same `0o600` permission pattern as `ibkr_core_mcp`.
  Token refresh explicitly calls `os.chmod(token_path, 0o600)` after every write because
  `os.open(O_CREAT, 0o600)` only sets the mode on newly created files.
- `GDriveSync` is guarded by a `threading.RLock` (reentrant) — concurrent session stops
  cannot race on the token refresh write or the Drive create/update check. `RLock` is
  required because `upload_db` holds the lock while calling `_find_file` → `_get_service()`,
  which re-acquires the same lock. A plain `Lock` would deadlock.

---

## 8. Network Exposure

ClaudIA runs entirely on `localhost`. By default:

| Service | Binding |
|---|---|
| Chainlit web UI | `localhost:8000` |
| IBKR gateway | `localhost:5055` |
| TradingView debug port | `localhost:9222` |

**Never expose these ports to external networks.** If remote access is required,
use a VPN or Tailscale tunnel — never a public-facing reverse proxy without authentication.

**CORS:** `.chainlit/config.toml` restricts `allow_origins` to `["http://localhost:8000"]`.
Do not widen this to `"*"` — the app connects to a live brokerage account.

**SSRF guard — two-layer architecture:**

ClaudIA makes outbound HTTP requests from two paths, each with its own guard layer.

**Layer 1 — Python pre-check (`agent.py` + `ibkr_core_mcp/scrape_fallback.py → is_private_host()`):**

The `fetch_web_page` local tool in `agent.py` guards every LLM-driven outbound request.
`firecrawl_crawl` and `firecrawl_search` go through `ClaudeToolkit._validate_public_url()`
in `ibkr_core_mcp/claude_tools.py`, which calls `is_private_host()` from `scrape_fallback.py`.
Both use the same resolve-then-check pattern. Blocked:
- Any scheme other than `http`/`https`
- `localhost`, `0.0.0.0`, and `127.*` / `169.254.*` literal hosts
- Any literal IP address in a private, loopback, link-local, or reserved range (`ipaddress.ip_address`)
- Decimal/hex-encoded IP bypasses (e.g. `http://2130706433/` = 127.0.0.1): hostname is resolved via
  `socket.gethostbyname()` and the resolved IP is re-checked against the same guard
- **Redirects to private addresses** (fixed 2026-07-03, finding S1): `fetch_web_page` follows
  redirects manually (`allow_redirects=False`, max 5 hops) and re-runs the full guard
  (`ClaudIAAgent._validate_public_url`) on every hop — a public URL that 302s to
  `localhost:5055` is blocked, not followed

**Layer 2 — Playwright route handler (`ibkr_core_mcp/scrape_fallback.py → _reject_private_requests()`):**

When Firecrawl quality is low, `_scrape_with_fallback()` in `claude_tools.py` activates the
Crawl4AI scraper (`Crawl4AIScraper` in `scrape_fallback.py`), which drives a Playwright browser.
A route handler registered on the browser page intercepts every browser-level request (including
redirects and subrequests) and re-runs `is_private_host()` before allowing it. This closes the
DNS rebinding + redirect gap that Layer 1 alone cannot catch:

```python
async def _reject_private_requests(route, request):
    # re-check at browser request time — catches post-check DNS flip and redirects
    host = urlparse(request.url).hostname or ""
    if is_private_host(host):
        await route.abort()
        return
    await route.continue_()
```

Residual risk: Layer 1 TOCTOU gap (DNS flip between pre-check and `requests.get()`) remains
for the `fetch_web_page` path. Layer 2 eliminates this gap for the Crawl4AI path. Accepted for
the personal local deployment — the IBKR gateway requires a session cookie that is never present
in cross-origin fetches.

---

## 9. Custom UI Endpoints

The following HTTP endpoints are added by `claudia/app.py` on top of Chainlit's built-in routes:

| Endpoint | Purpose | Data exposed |
|---|---|---|
| `GET /api/status` | Connectivity lights (JS polling) | Service status strings only — `"ok"`, `"error"`, `"unknown"` for IBKR (authenticated session), GDrive (API reachable), TradingView (CDP port open). No credentials, tokens, or account data. |
| `GET /cl/custom.css` | Dark theme stylesheet | Static asset, no user data |
| `GET /cl/custom.js` | Status bar DOM injector | Static asset, no user data |
| `GET /cl/claudia-logo.png` | Logo image | Static asset, no user data |

**`/api/status` data sensitivity:** The endpoint reveals whether IBKR is reachable and
whether Google Drive credentials exist on disk. This is operational metadata, not account
data or credentials. It requires no authentication because ClaudIA has no auth layer
(localhost-only, single user). Do not proxy this endpoint externally.

**Custom JS security:** `claudia/assets/custom.js` only uses `document.createElement`,
`textContent`, `className`, and `title` for DOM manipulation. It never calls `eval`,
never uses `innerHTML`, and never injects user-controlled data into the DOM. Review this
file after any modification for XSS vectors.

**IBKR gateway TLS:** `claudia/status.py → check_ibkr()` sets `verify=False` because
the IBKR Client Portal Gateway uses a self-signed certificate on localhost. This is
intentional and scoped to the single keepalive call (`GET /tickle`). No credentials are
sent in this request. The JSON response body is parsed for `iserver.authStatus.authenticated`
and `iserver.authStatus.connected`; neither field contains credentials. All JSON parsing
is wrapped in `except Exception` so a malformed response returns `False` without raising.

---

## 10. GDrive Sync Threat Model

Drive sync introduces three new attack surfaces. All are mitigated without relaxing the order execution barriers.

### 1. Poisoned `context.md` / `principles.md` — prompt injection (HIGH)

**Threat:** An attacker with Drive access modifies `principles.md` to weaken trading rules or inject adversarial instructions into the system prompt.

**Mitigations:**
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by anything from Drive.
  Order execution still requires physical button click + Touch ID + AppKit dialog regardless
  of what is in `principles.md`.
- On each session start with Drive content, `SHA-256(context + principles)` is compared against
  the hash stored in the previous session's `sessions.context_hash`. A mismatch triggers a
  visible **WARNING** message in chat before the session continues.

### 2. Poisoned `claudia.db` — conversation history injection (MEDIUM)

**Threat:** A malicious actor replaces `claudia.db` on Drive with a crafted file containing
fake conversation history designed to influence responses.

**Mitigations:**
- After downloading, `PRAGMA integrity_check` runs on the SQLite file. A structurally
  tampered file fails this check; ClaudIA falls back to the existing local DB.
- Conversation history cannot initiate an order — the physical button + biometric path
  is the only execution route.

### 3. Oversized Drive file — memory exhaustion (LOW)

**Threat:** A malicious or accidentally large `context.md` or `principles.md` on Drive
causes an OOM kill by being downloaded into an unbounded in-memory buffer.

**Mitigations:**
- `read_text()` checks the Drive file's `size` metadata field before downloading.
  Files larger than 1 MB are rejected with a warning; ClaudIA falls back to the local file.

---

### 4. Concurrent session race on Drive upload (LOW)

**Threat:** Two sessions closing simultaneously both find no existing `claudia.db` on Drive
and both call `files().create()`, producing duplicate Drive entries.

**Mitigations:**
- `GDriveSync` holds a `threading.RLock` that serialises the find-then-create/update block
  in `upload_db()` and the token refresh in `_get_service()`.

---

### 5. Drive OAuth token theft (MEDIUM — full Drive scope)

**Threat:** Stolen `GDRIVE_TOKEN_FILE` grants access to the user's entire Google Drive.

**Scope note:** ClaudIA uses the full `https://www.googleapis.com/auth/drive` scope
(not the narrower `drive.file`). The broader scope was required because:
- `drive.file` can only access files the app itself created — it cannot access
  `context.md` or `principles.md` that the user uploads manually via the Drive web UI.
- Creating and listing named subfolders (`db/`, `account_data/`, `market_data/`)
  also requires folder-level access beyond `drive.file`.

**Consequence:** A stolen `token.json` file grants full read/write access to the user's
entire Google Drive — not just ClaudIA's folder.

**Mitigations:**
- `GDRIVE_TOKEN_FILE` is `chmod 600` (owner read/write only). Token refresh calls
  `os.chmod(token_path, 0o600)` explicitly after every write, since `O_CREAT` mode only
  applies on file creation.
- The token is stored locally at a path known only to this machine; it is never logged,
  never passed to subprocesses, and never transmitted over the network.
- No IBKR credentials or `ANTHROPIC_API_KEY` are stored in Drive or reachable via the token.
- The `ping()` health check call (`files().list(pageSize=1)`) does not expose file contents.

**Future mitigation (not yet implemented):** Restrict access using a Google service account
with a dedicated shared Drive folder, or accept the `drive.file` limitation and require users
to place `context.md` and `principles.md` inside the ClaudIA root folder rather than uploading
them via the web UI.

### Hard guarantees unchanged

These two properties hold regardless of what is on Drive:
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by Drive content.
- No order can be placed without physical button click + Touch ID + AppKit dialog.

---

## 11. Connectivity Health Checks

`ConnectivityChecker` polls three services every 60 seconds. Each light reflects a real
runtime state, not a proxy indicator (file existence, process liveness).

| Service | What the check verifies | What it does NOT reveal |
|---|---|---|
| IBKR | `GET /tickle` → parses `iserver.authStatus.authenticated && connected`; green = authenticated session | Account balance, positions, orders |
| GDrive | `GDriveSync.ping()` → `files().list(pageSize=1)`; green = token valid + API reachable | File names, file contents, folder structure |
| TradingView | TCP connect to `localhost:9222`; green = Desktop open with CDP port | Chart data, indicators, strategy state |

**Security properties of the checks:**

- None of the checks transmit credentials. `/tickle` and `files().list` use session
  cookies / OAuth tokens that are managed by the HTTP client, not sent as visible parameters.
- All three checks are wrapped in `except Exception: return False`. Exceptions never
  propagate to `_run_checks()` as unhandled errors that could crash the poll loop or leak
  stack traces to the chat UI.
- The `/api/status` endpoint returns only the three status strings (`"ok"` / `"error"` /
  `"unknown"`). The underlying session state, token contents, and auth details are
  never serialised to this endpoint.
- The keepalive side effect of `/tickle` (resetting the IBKR inactivity timer every 60s)
  is intentional and prevents unintended session expiry during active trading sessions.

**Accuracy guarantee (established 2026-06-24):**
Before this date, the IBKR light showed green whenever the gateway process was reachable
(HTTP 200), and the GDrive light showed green whenever the token file existed on disk.
Both checks were meaningless indicators of real service state. Both were replaced with
genuine round-trip verifications. See `docs/connectivity.md` for full test results.

**IBKR soft-timeout silent recovery (added 2026-07-17 — the one check above that is not
purely passive):** when a poll finds IBKR in its documented soft-timeout state
(`connected:true, authenticated:false`), `ConnectivityChecker._attempt_soft_recovery()`
issues one `POST /iserver/auth/ssodh/init` to silently re-establish the session, instead
of always forcing a manual browser+2FA re-login. This is the only connectivity check that
makes a state-changing call to IBKR rather than just reading state, so it carries its own
narrow safety scoping, verified directly in `claudia/status.py`:
- **Never fires except from a confirmed-good prior state.** Only called when the *previous*
  poll was `OK` and the *current* poll shows exactly the soft-timeout signature — never from
  `UNKNOWN` (the fragile fresh-login/settling window) and never on a hard disconnect
  (`connected:false`).
- **`compete` is hardcoded `False`.** Per IBKR's own docs this determines whether other
  brokerage sessions (Mobile, TWS) get force-evicted to prioritize this connection — always
  `False` here, so this check can never kick out a concurrent human session.
- **Checks the response body, not just the HTTP status.** IBKR returns `200` on this endpoint
  regardless of outcome (same shape as `/tickle`), so a bare status check would silently
  report false success; the code parses `authenticated` from the JSON body instead.
- **Never touches order execution.** This endpoint only re-establishes the brokerage
  *session* — it has no relationship to `place_order`/`modify_order`/`cancel_order`, and
  the Order Execution Barriers in §2 are completely unaffected by whether this recovery
  path fires.
- 15 dedicated unit tests cover every branch (never fires from `UNKNOWN`/`ERROR`/hard-disconnect;
  successful recovery suppresses the disconnect alert; failed recovery — including a
  "successful" POST whose immediate re-check still fails — still alerts exactly once).
  Full design: `docs/plans/2026-07-17-ibkr-soft-timeout-recovery.md`.

---

## 12. Audit Checklist

Run this checklist before any significant code change to ClaudIA:

- [ ] No new tool definition calls `place_order`, `modify_order`, `cancel_order`, or `reply_order`
- [ ] Hardcoded safety block in `agent.py` is intact and unmodified
- [ ] `ANTHROPIC_API_KEY` does not appear in any log output
- [ ] New conversation history injections use structured message objects, not raw string injection
- [ ] `docs/context.md` and `docs/principles.md` have `chmod 600` permissions
- [ ] `.env` is in `.gitignore` and was not staged for commit
- [ ] Any new `IBKRClient` usage goes through `ClaudeToolkit` (not direct calls in tool handlers)
- [ ] Any new HTTP endpoint does not expose credentials, API keys, or account data
- [ ] Any new custom JS reviewed for `eval`, `innerHTML`, and user-data injection (XSS)
- [ ] Any new subprocess call uses an env allowlist — never `{**os.environ}` or `env=None`
- [ ] Any new Drive download has a size guard before the download loop
- [ ] Any new shared state accessed from `cl.make_async()` handlers is protected by a `threading.Lock` or `threading.RLock` (use `RLock` if any method holding the lock calls another method that also acquires it)
- [ ] Any new connectivity check returns a plain bool, wraps all exceptions, and does not log or expose credentials on failure
- [ ] Any new tool that makes outbound HTTP requests blocks localhost / private IP ranges (SSRF guard — see §8 `fetch_web_page` pattern)
- [ ] Any new outbound HTTP path re-validates **every redirect hop** against the SSRF guard (never `allow_redirects=True` on an LLM-driven fetch — S1, 2026-07-03)
- [ ] Any new automated (non-order) state-changing call to IBKR is scoped as narrowly as `_attempt_soft_recovery()` (§11): fires only from a specific, previously-verified-good state transition, never on `UNKNOWN`/settling states, and never able to evict a competing session (`compete=False` or equivalent)

---

## 13. Audit History

| Date | Scope | Findings | Status | Doc |
|---|---|---|---|---|
| 2026-06-12 | `gdrive_sync.py`, `tradingview.py`, `app.py`, `ibkr_core_mcp/client.py` | 2 High, 4 Medium, 2 Low | All 8 resolved | [`docs/audits/security-audit-2026-06-12.md`](docs/audits/security-audit-2026-06-12.md) |
| 2026-06-25 | All 8 claudia_ui modules (full re-audit) | 1 High, 0 Medium, 3 Low | H-1 fixed; 3 Low accepted | [`docs/audits/security-audit-2026-06-25.md`](docs/audits/security-audit-2026-06-25.md) |
| 2026-06-27 | ibkr_core_mcp v1.0 pre-release audit (ported back to `agent.py`) | 1 Medium ported | Decimal/hex IP bypass (`http://2130706433/`) fixed via `socket.gethostbyname()` resolve-then-check | see commit |

**Regression tests:** `tests/test_security_regressions.py` — 21 tests covering all three audits. These must stay green.
