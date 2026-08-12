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
| `docs/context.md` | Prompt injection: attacker modifies the file to override ClaudIA's behavior | File permissions (0o600 — restored 2026-07-25, both files had drifted to 0644); SHA-256 hash logged at session start; file is never executable |
| `docs/principles.md` | Prompt injection: attacker weakens risk rules | Same mitigations as context.md; ClaudIA cannot modify this file |
| `docs/versions/*/` snapshots | Same content, weaker protection: the version snapshots hold verbatim copies of both documents | `_write_version_snapshot` chmods each file 0600 after writing (`Path.write_text` honours the umask, so the chmod must be explicit and unconditional). Never git-tracked — see §14 |
| Conversation history | Re-injection: past messages contained adversarial content that gets fed back | History loaded as structured `role:user/assistant` blocks, not raw system prompt injection |
| LLM / tool output rendered in the UI | XSS: injected HTML executes in the trading UI's origin, reading account data and forging chat content | All rendering goes through `claudia/panel_markdown.py` — see §9 |
| TradingView sidecar | Supply chain: `tradingview-mcp` npm package has full CDP access to TradingView Desktop | Accepted risk (personal local tool); subprocess env is a strict allowlist (`PATH`, `HOME`, `USER`, `TMPDIR`, `NODE_*`) — `ANTHROPIC_API_KEY`, GDrive tokens, and IBKR credentials are never passed; vendor archive provides known-good fallback; `docs/tradingview-mcp-recovery.md` covers incident response |

---

## 2. Order Execution Barriers

ClaudIA has **zero** tools for order execution. This is the most critical security property.

### What the LLM can do
- Call any of the 44 read-only `ClaudeToolkit` tools (positions, PnL, market data, backtests, etc.)
- Call `propose_order` / `propose_cancel` / `propose_modify` (`claudia/proposal_tools.py`).
  These are declaration-only: the handler records the proposal for the UI to render and
  returns a `tool_result`. They open no socket and touch no IBKR API — see CLAUDE.md Hard
  Rule 1, which forbids *staging* as an LLM capability, not *proposing*
- Call `preview_order` (whatif — read-only, no execution)

### What the LLM cannot do
- Call `place_order`, `modify_order`, `cancel_order`, or `reply_order`
- Initiate any network request to the IBKR gateway for write operations
- Access `IBKRClient` directly (only `ClaudeToolkit.execute()` is exposed to the agent loop)

### Order staging flow (human-in-the-loop)
1. ClaudIA calls a `propose_*` tool — this records a proposal and returns a `tool_result`, no
   side effects. The tools are declared with `strict: true`, so the **API itself** guarantees
   `tool_use.input` matches the schema in `claudia/proposal_tools.py` before the handler ever
   runs: `action` within `{BUY, SELL}`, `order_type`/`tif`/`sec_type` within their declared
   enums, `quantity` a true integer, prices numeric-or-null, no unknown keys, no missing keys
2. `agent.py`'s `_proposal_defect()` then checks the four contract terms a strict JSON Schema
   **cannot** express — `quantity > 0` (`exclusiveMinimum` is rejected by the tools endpoint),
   non-blank `symbol`, non-blank `order_id` on cancel/modify, and no duplicate `changes`
   entries. Only after both layers pass is the dict handed to the `MessageSink`
   (`send_order_proposal`); `PanelMessageSink` routes it to
   `panel_order_flow.render_order_proposal()`, which renders the button. A rejected proposal
   creates no button and returns an explicit `REJECTED — <reason>` `tool_result` stating that
   no button was created, so ClaudIA cannot appear to have staged an order it did not. **Both
   layers reject; neither repairs** — order parameters are immutable. At most one proposal is
   accepted per turn; a second is refused, not silently dropped
3. **Human physically clicks the button** — this is the first human gate
4. The matching `IBKRClient` entry point fires. There are **three**, one per proposal kind,
   and all three run both gates — the absence of an `_and_confirm` suffix on the cancel
   path is a naming artefact, not a missing gate (re-verified 2026-08-05, because it reads
   like one):

   | Click | Entry point | Where the gates run |
   |---|---|---|
   | Stage this order | `place_order_and_confirm()` | inside `place_order()`, plus a gated reply loop |
   | Cancel this order | `cancel_order()` | inside `cancel_order()` itself (`client.py`) |
   | Modify this order | `modify_order_and_confirm()` | inside `modify_order()`, plus a gated reply loop |

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
Only the file owner can read or modify these files. These two are hand-maintained —
`GDriveSync.read_text()` returns a string and never writes them — so the mode persists once
set. The `docs/versions/*/` snapshots are different: the app rewrites those on every new doc
version, which is why `_write_version_snapshot` chmods them explicitly (§1). Both source
files were found at 0644 during the 2026-07-25 audit and restored.

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

- Tool exceptions are sanitized before reaching the LLM: `ClaudeToolkit.execute()` wraps
  every handler in `try/except` and returns `_safe_error(name, exc)`. That function lives
  in **`ibkr_core_mcp/claude_tools.py`**, not in this repo — a grep of `claudia/` finds
  nothing and should not be read as the control having been removed (it was, briefly, in
  the 2026-08-05 audit).

- Raw IBKR responses **do** reach two places, and neither is the LLM's context: the order
  read-back sends `json.dumps(result, indent=2)` into chat (rendered through
  `safe_markdown` — §9), and stores it under `decisions.metadata["ibkr_response"]`.
  `decisions` has **no search path** — its FTS table was dropped as dead schema in the
  2026-07-03 review — so those blobs are never re-injected into a later conversation.
  Adding a decisions-recall tool would change that and requires re-reading this section.

- `claudia.db` is local, single-user, and not accessible over the network.

---

## 5. Hardcoded Safety Block

`_SAFETY_BLOCK` (`claudia/agent.py:58-195`) is embedded directly in code and appended to
every system prompt. It is **not** loaded from any user-editable file and cannot be
overridden by `context.md` or `principles.md`. Modifications require a code change — a
deliberate developer action, not a document edit (CLAUDE.md Hard Rule 3).

**This section intentionally does not quote the block verbatim** — a byte-for-byte copy
here would duplicate content that changes whenever the prompt is tuned, and would go
stale exactly the way the previous version of this section did (it quoted only the first
of what are now 7 non-overridable subsections, and had never been updated to reflect the
other 6). Read `claudia/agent.py:58-195` directly for the authoritative current text. As
of this writing, the block's non-overridable subsections are:

- **ABSOLUTE CONSTRAINTS** — no order execution, no financial-advisor claims, principles
  check required before proposing any trade, cannot promise returns
- **DATA INTEGRITY** — every specific data point presented (price, balance, position, P&L,
  contract ID, etc.) must originate from a tool result or user-provided content in *this*
  conversation; inventing, guessing, or carrying over a plausible-looking value is
  explicitly prohibited, and uncertainty about a value's origin means treating it as invented
- **ORDER PROPOSAL — USE THE TOOLS, NEVER PROSE** — a trade action is proposed by calling
  `propose_order`/`propose_cancel`/`propose_modify`; there is no text format, and writing
  *about* a proposal without calling the tool creates no button. At most one proposal tool
  call per response
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

### 5.1 Code-layer enforcement — the safety block is instruction, not enforcement

Everything above is text in the system prompt. A model can ignore text, and has: the same
failure class has now produced **five measured fabrications across two sessions**, where
ClaudIA asserted an action it never took. So the rules above are backed in code, in
`claudia/agent.py`. The split is deliberate — **the trigger is textual, the verdict is
evidence**: a detector keys on what the model wrote, but the ruling comes from persisted
rows, never from asking the model whether it was telling the truth.

| Mechanism | Asserts |
| --- | --- |
| `_claims_completed_proposal` → `_emit_unbacked_claim_notice` | claimed a staged proposal ⇒ one was actually recorded (else a `proposal_claim_unbacked` decision) |
| `_claims_fresh_book_check` | claimed a live-book check ⇒ a book-reading tool really ran this turn (else `book_claim_unverified`) |
| `_emission_records` | replays which proposals genuinely rendered a button — excluding `proposal_render_failed`, so a failed render is never replayed as a success |
| `_completed_order_records` | replays order actions that reached IBKR, which leave no transcript at all (button click, no tool call) |
| `_called_tool_records` | names the tools called earlier and states their results are **gone** from context — added 2026-08-11 |

All of them are delivered by `_append_operator_message` as a single mid-conversation
`role: "system"` message. **That role is the security property**: anything the model can
write it can forge, and the failure being corrected is a model asserting something untrue
about its own output. Records carry **identity only — never values**: a tool name, an order
id, a symbol. A remembered-looking price or quantity would be a fabrication surface, and
order parameters are immutable (§2).

The last row closes a gap the others could not see. `_history_to_messages` deliberately
drops tool rows — the DB stores no Anthropic `tool_use_id`s, and orphaned `tool_result`
blocks are an API 400 — and its docstring assumed the assistant's own prose was an adequate
substitute. Measured 2026-08-11: it is not. Asked about chart settings its own prose had
recorded only by study *name*, ClaudIA invented colours, line widths, precision and band
levels — categories no tool it had called can return at all (`chart_get_state` yields
`{id, name}` per study and nothing else). The identical question in a fresh session, with
no prose to lean on, produced five tool calls and ten of ten fields correct against the
live chart read over CDP.

**Model requirement:** mid-conversation `system` messages are not supported on every model,
and since the ledger rides this channel an unsupported model now fails on the turn after any
tool call rather than only after an order proposal. `warn_if_model_lacks_operator_channel`
logs a loud startup error; there is deliberately **no** fallback to a `<system-reminder>` in
the user turn, because that channel is forgeable and silently downgrading a non-spoofable one
is worse than refusing the model. See `docs/env-vars-reference.md`.

**Known limit, stated rather than implied:** none of this covers a fabricated claim about a
*new* action with nothing in context to recycle — observed 2026-08-11 and still unguarded.

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
  state. This is intentional (required for its full tool set) and accepted for a personal local
  tool with no remote access.
- **Tool surface area reduced.** `_CURATED_TOOLS` in `tradingview.py` limits what Claude can call
  to 17 high-value tools, of the **84** the sidecar registers at `55534aa` (counted 2026-08-05;
  this figure grows with every upgrade and was stale at 78 for three of them). The sidecar
  process itself has full access regardless of this filter.
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
- `.env` itself must be `0600` (it holds `ANTHROPIC_API_KEY` and `IBKR_FLEX_TOKEN`).
  Found at 0644 and restored 2026-07-25.
- `ANTHROPIC_API_KEY` is never logged, never included in Panel chat output, and never
  passed to the LLM as text. Audited 2026-07-25: the name appears in `claudia/` only inside
  two comments in `tradingview.py`; `AsyncAnthropic()` reads it from the environment.
  Two indirect channels are worth knowing about, neither observed to carry the key (the
  Anthropic SDK redacts auth in `APIStatusError`): `panel_app`'s
  `f"**Session init failed:** {exc}"` send, and `ChatInterface.callback_exception`
  defaulting to `"summary"`, which renders `type(e).__name__: str(e)` into the feed.
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

| Service | Binding | Enforced by |
|---|---|---|
| Panel web UI | `127.0.0.1:8001` | `address="127.0.0.1"` in `panel_app.main()`'s `pn.serve` call |
| IBKR gateway | `127.0.0.1:5055` | gateway container config |
| TradingView debug port | `localhost:9222` | CDP default |

The UI port is `CLAUDIA_PANEL_PORT` (default `8001`). There is **no authentication layer** —
the loopback bind is the actual boundary, so it is load-bearing, not cosmetic.

**Never expose these ports to external networks.** If remote access is required,
use a VPN or Tailscale tunnel — never a public-facing reverse proxy without authentication.

**Bind address (the control that replaced Chainlit-era CORS):**

`pn.serve` must always be called with `address="127.0.0.1"`. Omitting it passes
`address=None` to bokeh, which calls tornado's `bind_sockets(port, None)` — documented as
*"None to listen on all available interfaces"* — so the server binds `*:8001` on IPv4 and
IPv6 and the whole UI becomes reachable from the LAN. This was the live state until
2026-07-25 (verified by `lsof` on the running process); guarded now by
`test_pn_serve_binds_loopback_only`.

Source: https://www.tornadoweb.org/en/stable/netutil.html#tornado.netutil.bind_sockets

**`websocket_origin` is defence-in-depth, not a boundary.** It is set to
`["localhost:8001", "127.0.0.1:8001"]` and does genuinely stop cross-origin browser attacks
including DNS rebinding. It does **not** substitute for the bind address:

- It gates only the websocket upgrade. `GET /` (bokeh's `DocHandler`) is not origin-checked
  at all, and serving that page creates a **full ClaudIA session** — Drive download, IBKR
  account calls, a `create_session` row, possibly a live Flex sync; and on disconnect,
  session-report generation plus a `claudia.db` upload to Drive.
- Tornado skips `check_origin` entirely when the request carries no `Origin` header ("we
  assume it did not come from a browser"), so any non-browser client simply omits it.
- `Origin` is a client-supplied header and trivially forged in any case.

**What a reachable session would expose:** as of 2026-08-04 this is materially more than
it was, and the change ran in the opposite direction from where you would look for it.
Chat stopped rendering account figures at startup (`gather_status_block` →
`gather_session_state`, commit `2829789`) — but the **live dashboard** replaced them with
something continuous. Serving `GET /` now paints, and keeps repainting every 5 seconds:
net liquidation, cash, unrealised P&L, realised P&L windows, the full positions table, the
working-order book, and the realised-P&L history. No user action is required; it is the
page. Add the ability to drive every widget and the agent itself.

Order placement would still require physical Touch ID and the local AppKit dialog, so the
§2 barriers hold. The exposure is **read of live account data — now continuous rather than
one opening payload — and control of the agent.** This is what makes the loopback bind
load-bearing rather than cosmetic.

**Never enable `--dev` / `autoreload`** on this app (currently off, and `start-claudia.sh`
passes no flags): it starts a filesystem watcher that re-executes app source.

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

## 9. UI Surface (Panel)

ClaudIA adds **no HTTP endpoints of its own.** The Chainlit-era routes this section used to
describe (`/api/status`, `/cl/custom.css`, `/cl/custom.js`, `/cl/claudia-logo.png`) were
removed with the Chainlit entry point (app.py, deleted at the Phase 11 cutover); `claudia/assets/` now holds only an
orphan logo that no route serves. Everything the browser receives arrives over the
Bokeh/Tornado session websocket, and the only routes are Panel's own (`/`, `/ws`,
`/autoload.js`, `/metadata`, `/static/*`, `/components/*`). Connectivity lights are
`pn.indicators.BooleanStatus` widgets repainted in-process by a 5-second periodic callback —
still only `(bool, colour)` on the wire, but no longer an unauthenticated HTTP route.

### Markdown rendering — the XSS control

**All chat rendering must go through `claudia/panel_markdown.py`.** Panel's Markdown pane is
not safe by default, verified end-to-end against panel 1.9.3 / bokeh 3.9.1:

1. `MarkdownIt('gfm-like').options['html']` is `True` — raw HTML passes the parser.
2. The bokeh `HTML` model defaults to `run_scripts = True`.
3. The client calls `html_decode(model.text)`, assigns `innerHTML`, then `run_scripts()`
   **re-creates every `<script>` node via `document.createElement`** — which executes it.

Untrusted text reaches those panes from the LLM, from IBKR and TradingView responses, from
exception strings, and — the most exposed sink, needing no LLM cooperation — from raw tool
results: a page fetched by `fetch_web_page`/`firecrawl_*` is streamed into the tool step
verbatim. An attacker who lands script here can read the DOM (positions, P&L, account IDs),
exfiltrate it, and forge ClaudIA messages and order-proposal UI. The §2 gates still prevent a
silent order, but the on-screen context around a Touch ID prompt would be attacker-controlled.

Two helpers, because there are two rendering paths:

| Helper | Covers | Mechanism |
|---|---|---|
| `safe_markdown()` | Every `chat.send()` (installed as `ChatInterface(renderers=[...])`) and every directly-constructed pane | `renderer_options={"html": False}` — double-escapes, so the client's single `html_decode` yields text |
| `escape_markup()` | Text streamed into a `pn.chat.ChatStep` | `html.escape(quote=False)` — `ChatStep` has no `renderers` parameter and builds its own panes, so the feed-level hook cannot reach it |

Two things that are **not** adequate substitutes, both tested: wrapping untrusted text in a
```` ``` ```` fence (content carrying its own closing fence escapes it), and `run_scripts=False`
(stops `<script>` but not `onerror=`/`onload=`). Markdown-it's `validateLink` does already
reject `javascript:`, `data:` and `vbscript:` link schemes while allowing `https:`.

Guarded by `test_safe_markdown_neutralises_html`,
`test_escape_markup_neutralises_chatstep_stream`,
`test_no_unguarded_markdown_panes_in_package` (structural — fails if any module constructs
`pn.pane.Markdown/HTML/Str` directly), and `test_unsafe_markdown_would_be_vulnerable` (fails
if Panel's default ever changes, so the control is re-evaluated rather than silently becoming
a no-op).

### Client-side JS

The only JS ClaudIA injects is the PineScript copy button
(`panel_pinescript.py` → `js_on_click(args={"code": code}, code="navigator.clipboard.writeText(code)")`).
This is injection-safe by construction: the pine source is a **named CustomJS argument**
bound as a JS function parameter, never interpolated into the code string — probe-verified
with a breakout payload, which survived intact as data. Keep it that way; never build the
`code=` string by interpolation.

### IBKR gateway TLS

`claudia/status.py` sets `verify=False` because the IBKR Client Portal Gateway uses a
self-signed certificate on localhost. This is intentional and scoped to **two** calls, both
against `127.0.0.1:5055`:

| Site | Call | Notes |
|---|---|---|
| `check_ibkr()` | `GET /tickle` | Keepalive. No credentials sent. Response parsed for `iserver.authStatus.authenticated` / `.connected` — neither field contains credentials |
| `_attempt_soft_recovery()` | `POST /iserver/auth/ssodh/init` | Added 2026-07-17. Body-parsed, not status-parsed; `compete=False` hardcoded so it can never evict a competing session — see §11 |

All JSON parsing is wrapped in `except Exception` so a malformed response returns `False`
without raising.

### Live dashboard (shipped 2026-08-04)

Three modules — `dashboard_data.py` (pure data), `dashboard_poller.py` (one process-wide
15s poller), `panel_dashboard.py` (widgets only). Audited 2026-08-05; four properties are
security-relevant and two of them are load-bearing controls, not incidental.

**Both `Tabulator`s are `disabled=True` with no `on_click`/`on_edit` handler bound
anywhere in the module.** Panel's `Tabulator` cells are **editable by default**, so this is
an active control, not a default. It matters most on the working-order book: that is the
one surface in this app where a click could plausibly be wired to "cancel this row", and
cancelling must stay behind `propose_cancel` and both §2 gates. The test suite asserts the
handler set is empty across **every** `Tabulator`, not a named one, so a third table
inherits the guarantee instead of silently escaping it. `header_filters`, sorting and
pagination are permitted — they change what is displayed and reach no order path.

**The dashboard is a direct `IBKRClient` consumer, deliberately outside `ClaudeToolkit`.**
This is the one place that is true, and it does not weaken Hard Rule 5: that rule forbids
bypassing the toolkit *from inside an LLM tool handler*, and this is a UI-layer background
poller with no LLM reachability. The toolkit is bypassed because it returns rendered
markdown and the dashboard needs data. The calls are `get_accounts`, `fetch_ledger`,
`fetch_positions`, `fetch_orders` — **read-only, no write API reachable from this path.**

**The trade store is opened read-only.** `dashboard_data.connect()` uses
`sqlite3.connect("file:…?mode=ro", uri=True)` against the same `store.db` the Flex sync is
writing. A display surface cannot corrupt the trade archive, and the handle cannot be
turned into a write path by a later edit.

**No SQL is built by interpolation** in `dashboard_data.py`, `flex_sync.py`, or
`conversation_store.py` — every `execute()` is a literal or parameterised, verified by
grep at the 2026-08-05 audit.

One non-security note that keeps being mistaken for one: a failed poll republishes the
**previous** `as_of` rather than stamping `now`. That is a correctness invariant (staleness
must stay visible), and it must not be "fixed".

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
- Only the three `ServiceStatus` values reach the browser, as `BooleanStatus` widget
  state over the session websocket — `(bool, colour)` and nothing else. The underlying
  session state, token contents, and auth details are never serialised to the UI. (Before
  the Panel cutover this was an unauthenticated `GET /api/status` route; that route no
  longer exists — see §9.)
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
- 12 dedicated unit tests cover every branch (never fires from `UNKNOWN`/`ERROR`/hard-disconnect;
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
- [ ] `.env` and both private docs are `0600`; no private document is git-tracked (`git ls-files -i -c --exclude-standard` prints nothing)
- [ ] `pn.serve` is still called with a loopback `address=` — `websocket_origin` alone is not a boundary (§8)
- [ ] Any new Panel content rendered into a session goes through `safe_markdown()` / `escape_markup()` — never a bare `pn.pane.Markdown/HTML/Str` (§9). This replaces the old "new HTTP endpoint" check: the app authors no HTTP routes, so everything reaching the browser goes over the session websocket
- [ ] Any new client-side JS passes untrusted values as **named CustomJS args**, never interpolated into the `code=` string (§9)
- [ ] Any new file written by the app that contains private-document or account content is `chmod 0600` immediately after the write, **unconditionally** — `Path.write_text` honours the umask *and* leaves an existing file's mode untouched, so a create-only chmod leaves every pre-existing file wrong forever. Two writers missed this until 2026-08-05 (session reports, the Flex verdict); "it's only a summary" is not an exemption — the summary named symbol, side, quantity and limit price
- [ ] Any new read-only consumer of live data opens SQLite with `mode=ro` and calls no write API — the dashboard pattern (§9), not a fresh `IBKRClient` write path
- [ ] Any new `Tabulator` is `disabled=True` with no `on_click`/`on_edit` handler (§9). Cells are editable by default; the test asserts this over *all* tables, so a new one is covered automatically — do not add an exemption
- [ ] Any new `IBKRClient` usage goes through `ClaudeToolkit` (not direct calls in tool handlers)
- [ ] Any new subprocess call uses an env allowlist — never `{**os.environ}` or `env=None`
- [ ] Any new Drive download has a size guard before the download loop
- [ ] Any new shared state touched from a worker thread (`asyncio.to_thread`, the watchdog observer) is protected by a `threading.Lock` or `threading.RLock` (use `RLock` if any method holding the lock calls another method that also acquires it). Cross-thread UI updates go through `loop.call_soon_threadsafe`
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
| 2026-07-25 | Post-Panel-migration full audit — all 18 `claudia/` modules (7 had never been audited), `SECURITY.md`, git hygiene | 3 High, 3 Medium, 3 Low | All High + Medium fixed; Lows recorded | [`docs/audits/security-audit-2026-07-25.md`](docs/audits/security-audit-2026-07-25.md) |
| 2026-08-05 | Post-dashboard — everything changed since `a8bfdf0` (33 files, ~13.9k insertions; 5 modules never audited), plus re-verification of every `SECURITY.md` invariant | 0 High, 0 Medium, 2 Low + 5 doc defects | Both Lows fixed and regression-tested; all 5 doc defects corrected here | [`docs/audits/security-audit-2026-08-05.md`](docs/audits/security-audit-2026-08-05.md) |

**Regression tests:** `tests/test_security_regressions.py` — **53** tests (counted by
`pytest --collect-only`, 2026-08-05; the figure here read 46 against an actual 50) covering
all five audits, plus `test_chat_interface_installs_the_safe_markdown_renderer` in
`tests/test_panel_app.py` (needs that module's session scaffolding). These must stay green.

**Two items are open and belong to `ibkr_core_mcp`, not here** (2026-08-05): the trade
store `~/.ibkr_core/store.db` is world-readable at `0644` — the fix belongs in
`SQLiteStore`'s constructor, since a one-off `chmod` is undone by the next recreate — and
`~/.ibkr_core/credential.json` is a superseded OAuth client secret at `0644` that nothing
references and that should be deleted rather than chmod'd.

---

## 14. Private Documents and Git

`docs/context.md`, `docs/principles.md`, and every `docs/versions/*/` snapshot contain
ClaudIA's persona and the user's trading rules. **None may ever be committed.** The repo is
public.

`.gitignore` lists all three paths, but **`.gitignore` does not untrack a file that is
already tracked** — it only prevents new ones from being added. That gap is how
`docs/versions/v1/{context,principles}.md` stayed tracked and publicly readable on
`origin/main` from 2026-06-11 until 2026-07-25: the 2026-07-10 `git-filter-repo` scrub was
path-scoped to `docs/context.md` and `docs/principles.md` and never covered the version
snapshots.

Both were untracked and then scrubbed from history on 2026-07-25. **Two lessons from that
scrub, because the first one had already been learned the hard way:**

1. **Enumerate the content, not the paths you remember.** Scan every blob in every ref for
   the documents' distinctive headers before choosing what to filter. Doing so found the
   same content on `refs/heads/panel-migration` (still live on the remote — only the local
   branch had been deleted) and inside both tags. A `main`-only scrub would have missed all
   three.
2. **A force push does not purge GitHub.** Unreachable commits still served their blobs over
   `raw.githubusercontent.com` afterwards. Removing cached views requires a GitHub Support
   request; that request was **deliberately not filed** and the residual is accepted (user
   decision, 2026-07-25 — persona/trading text, not credentials; blobs unreachable from
   every ref). Do not re-raise. Detail: `docs/audits/security-audit-2026-07-25.md` H-2.

**Standing rule (user, 2026-07-25): keep private data and all plans out of git; older
leftovers are acceptable.** Prevention, not removal, is the control from here. Every class
below is guarded by `test_private_content_is_not_git_tracked` (not in the index) *and*
`test_every_private_path_is_gitignored` (covered by an ignore rule — the index check is the
symptom, the ignore rule is the control):

| Class | Path |
|---|---|
| Secrets | `.env` |
| Persona / trading rules | `docs/context.md`, `docs/principles.md` |
| Verbatim snapshots of both | `docs/versions/` |
| Personal working documents | `docs/plans/` |
| Live account data in images | `docs/panel/screenshots/` |
| Conversation DB, Flex archive, session reports | `data/` |

Writing that second test exposed a real gap: `.gitignore` had enumerated `data/` subpaths
individually, so any *new* `data/` file would have been unignored — the same shape as the
`docs/versions/` leak. Now a wholesale `data/` rule.

**Branches:** `main` only, local and remote. `panel-migration` was deleted 2026-07-25 after
confirming 0 unique commits.

Check with the structural command, not by eye:

```bash
git ls-files -i -c --exclude-standard   # must print nothing
```

Guarded by `test_private_documents_are_not_git_tracked` and
`test_no_tracked_but_ignored_files`.
