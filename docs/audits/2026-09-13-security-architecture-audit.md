# ClaudIA security architecture audit — 2026-09-13

**Status:** Phase 1 deliverable. Evidence-based audit and proposed implementation plan.
**No code, tests, CI, dependencies or tracked documentation were changed.** This file lives in
the git-ignored `docs/plans/` directory; after human review the point-in-time record is meant
to move to `docs/audits/security-architecture-audit-2026-09-13.md` unchanged, per Phase 7.

**Repos audited:** `claudia_ui` at `a2dc1bb` (clean) and `ibkr_core_mcp` at `d4b3347` (clean).
**Method:** eight parallel read-only investigations (one per domain), each returning
file:line evidence; the highest-consequence findings (A-1, A-2, A-3, the dispatcher, the sink,
the serve call, the capability registry) were re-read or re-executed by the coordinating
reviewer before being accepted. Every "measured" statement below names how it was measured.
Scratch artefacts (socket-recording pytest plugins, the XSS probe page, the pip-audit venv)
stayed in the session scratchpad and are not part of either repo.

**The question this audit answers:** for each of ClaudIA's safety properties, could a future
developer or coding agent make a perfectly linted, fully typed, fully tested change that
silently violates it? Where the answer is yes, the property is proposed for machine enforcement.

---

## 0. Two environment observations (not code findings, but they gate everything else)

| Observation | Evidence | Consequence |
|---|---|---|
| **The ClaudIA venv cannot import `ibkr_core_mcp` right now.** | `.venv/lib/python3.11/site-packages/__editable__.ibkr_core_mcp-1.2.2.pth` points at `ibkr_core_mcp/build/__editable__.ibkr_core_mcp-1.2.2-py3-none-any`, which no longer exists (`build/` mtime 2026-09-13 14:47, during the core repo's audit remediation). `.venv/bin/python -c "import ibkr_core_mcp"` → `ModuleNotFoundError`. | The whole ClaudIA suite fails at collection, the pre-push hook refuses every push (fails closed, correctly), and ClaudIA will not start from this venv. This is the strict-editable trap CLAUDE.md § Dev Setup step 3 documents, in its most complete form: not a missing module but a missing snapshot. Every measurement in this audit used `PYTHONPATH=/Users/steph/Claude_Projects/ibkr_core_mcp` instead of repairing the install. Repair is one command (Dev Setup step 3); it is left to the implementation phase because the environment was not to be touched. `claudia/install_check.py` compares module sets and cannot see this case — the pth target is absent, so there is nothing to compare. |
| **The installed venv is behind the resolved floors on the network-facing dependency.** | `pip-audit` over `pip freeze` of `.venv` (throwaway audit venv, nothing installed into the project): `tornado 6.5.7` carries three advisories fixed in 6.5.8 (event-loop stall, multipart memory amplification, cookie attribute injection); `nltk 3.10.0` 21 advisories fixed in 3.10.3; `cryptography 49.0.0` one fixed in 50.0.0; `h2 4.4.0` orphan. A fresh resolve of `.[dev]` + `../ibkr_core_mcp[scraper]` picks up every fix and leaves one finding (`nltk` PYSEC-2026-3740, no fixed release, already in the core's ignore file). | The floors are fine; the machine is stale; nothing in `claudia_ui` would have said so. This is the failure class a `pip-audit` gate detects (§ 5). |

---

## 1. Phase 1 — Trust and capability map

### 1.1 Principals

| Principal | Trusted for | NOT trusted for | Enters ClaudIA via | Privileged sinks it can influence |
|---|---|---|---|---|
| Human operator | Configuration, `.env`, editing `context.md`/`principles.md`, the click on each proposal, Touch ID and Gate 2 | Unattended automation of writes | Browser tab on loopback, keyboard, filesystem | Everything, by design |
| LLM (Claude) | Reads, analysis, proposing orders, Pine text | Executing anything, asserting what happened, reading credentials | API response content blocks (`tool_use`, `text`) | Tool dispatch (toolkit / TV bridge / local handlers), a proposal → a rendered button, Pine → Copy/Inject buttons, chat prose, the ChatStep title (§ 3.6) |
| Current user message | The user's intent | Facts about the account | `ChatInterface` callback | Model context; stored as a `user` row |
| Conversation history (`claudia.db`) | Continuity | Authority beyond its stored role; tool payloads | `get_history` → `_history_to_messages` | Model context only. Never `system=` (Hard Rule 4 holds, § 3.7) |
| `docs/context.md`, `docs/principles.md` | Persona and trading rules | — they **are** the system prompt: whoever writes the file writes the prompt | `context_loader` → `system=` | System prompt |
| Versioned snapshots (`docs/versions/`, `doc_versions`) | Historical record | Being current rules | `get_doc_version` tool result | Model context as a `tool_result` |
| IBKR gateway | Account and market data, order state | Rendering safety of its strings | `ibkr_core_mcp` client, `IBKRWebSocket` | Tool results → model; dashboard tables; Gate 2 rows; the System log (fill headline) |
| `ibkr_core_mcp` | Gate 1 and Gate 2 at the innermost write site; read tools; redaction of its own errors | ClaudIA-layer invariants: the human click, display correspondence, ClaudIA's own error text | Direct Python import (strict editable) | IBKR writes (through ClaudIA's cores), Drive, SQLite, browser cookies |
| Google Drive | Persistence of `claudia.db`, the two documents, the Flex archive | — a Drive-side edit becomes the next session's system prompt with a collapsed-card warning (§ 3.14) | `GDriveSync` at session start, by **filename** | System prompt; conversation memory; the hash baseline for its own tamper detector |
| TradingView Desktop | Charts, Pine editor | Nothing else; CDP has no authentication by design | Sidecar over CDP on 9222 | Model context (TV results); UI |
| TradingView MCP sidecar (Node) | The 17 curated tools | Anything else — it runs as the user, unsandboxed | `stdio_client` subprocess | Model context; if compromised, the user account (§ 3.10) |
| Web / tool output | Nothing | Anything — the prompt-injection vector | `fetch_web_page`, `firecrawl_*`, `crawl_site` → `tool_result` | Model context; the ChatStep body (escaped) |
| Panel/Bokeh browser client | Rendering the human's view; clicks | Authority beyond the loopback origin allowlist | Tornado on `127.0.0.1:8001` | Every button callback: stage, cancel, modify, reconnect, launch TradingView, end session, upload image |
| Local filesystem | `.env`, DB, documents, tokens | — | `Config.from_env`, `load_dotenv`, `context_loader` | Everything |
| SQLite (`claudia.db`) | Sessions, messages, decisions, doc versions, withdrawals | Being a trust upgrade for content | `conversation_store` | The operator channel (derived records), history replay, FTS recall |
| Future coding agent | Ordinary changes under CI | Silent violation of any invariant in § 11 | `git push` | Any |

### 1.2 Capability taxonomy

The core's registry is the machine form and ClaudIA should consume it rather than restate it.
Measured 2026-09-13 by importing `ibkr_core_mcp.claude_tools` from source: **44 definitions**;
vocabulary `ACCOUNT_STATE, COMPUTE, DATABASE, GOOGLE_DRIVE, LOCAL_IO, NETWORK, ORDER_PREVIEW,
READ_ONLY, SANDBOX_EXECUTION, WEB_FETCH`; `ORDER_EXECUTION` has no spelling. `READ_ONLY` 24 ·
`ACCOUNT_STATE` 4 (the alert writers: ungated IBKR server-side writes) · `DATABASE` 6 ·
`GOOGLE_DRIVE` 5 · `LOCAL_IO` 4 · `WEB_FETCH` 3 · `COMPUTE` 3 · `NETWORK` 2 ·
`SANDBOX_EXECUTION` 1 · `ORDER_PREVIEW` 1. **ClaudIA reads the `capabilities` field nowhere**
(`grep -ri capabilit claudia/ tests/` → 0).

ClaudIA adds these capabilities on top, all in `claudia/`:

| Capability | Where | Who can trigger |
|---|---|---|
| MODEL_CONTEXT (system prompt) | `context_loader`, `_build_system_prompt` | operator files, Drive |
| PERSISTENT_MEMORY | `conversation_store` | agent loop, order cores, `panel_app` |
| UI_RENDER | `panel_*` | model, tools, IBKR, TV, errors |
| NETWORK (LLM-directed) | `fetch_web_page` (`agent.py`) | model, SSRF-guarded per hop |
| TRADINGVIEW_CDP | `tradingview.py` via sidecar | model (17 curated tools) |
| SUBPROCESS | `tradingview.py`, `gateway_launch.py`, scripts, keepalive daemon | operator buttons, startup |
| GOOGLE_DRIVE | `gdrive_sync.py` | startup, session end, shutdown |
| ORDER_PROPOSAL | `propose_*` handlers | model |
| HUMAN_CONFIRMATION (ClaudIA's) | the rendered button + click | human only (§ 3.2) |
| ORDER_EXECUTION_REQUEST | `_execute_*_core` → three gated `IBKRClient` entry points | the click closures only (§ 3.2) |
| IBKR_ORDER_EXECUTION | inside `ibkr_core_mcp.client` after Gate 1 + Gate 2 | owned by the core |

### 1.3 Flows (SOURCE → validation → component → boundary → sink)

1. **User request → model → tool.** `_on_user_input` → `user` row → `handle_message` → API →
   `tool_use` → the dispatcher (`agent.py:2217-2234`): `_LOCALLY_HANDLED` → `_handle_local_tool`;
   TV names → `TradingViewBridge.execute`; `_ibkr_unavailable()` → refusal string; else
   `ClaudeToolkit.execute` (a literal 44-entry dict; unknown name → "Unknown tool"). Four sinks,
   no `getattr`, no registry keyed by model text.
2. **Model → `propose_*`.** API strict schema → `_record_proposal`: pending-proposal check,
   modify-needs-`get_order_status`-this-turn check, `_proposal_defect` → `_pending_proposal =
   (kind, inputs)` **by reference** → acceptance string. No I/O.
3. **Proposal → sink → render.** `send_*_proposal` → `panel_order_flow.render_*` →
   read-only IBKR call for the contract label (`proposal_contract_label`) → `safe_markdown`
   summary → two `Button`s whose `on_click` closures close over **the same dict**.
4. **Click → core → core gates.** `_on_stage` disables both buttons (does not check them) →
   `_execute_staged_order_core` builds a new `order_body` → `IBKRClient(...)` constructed inside
   `order_flow` → `place_order_and_confirm` / `cancel_order` / `modify_order_and_confirm` →
   Gate 1 → Gate 2 → POST → `_read_back` → decision row.
5. **History → model.** `_history_to_messages`: `user`/`assistant` only, withdrawn assistant
   rows dropped, tool rows dropped; then one `role: system` operator message rebuilt per turn
   from decision rows (identity fields) and the called-tool ledger.
6. **Documents → system prompt.** `version note + context.md + principles.md + trade lines +
   _SAFETY_BLOCK`, safety block last. Nothing from the DB except the version label and date.
7. **Tool output → model.** `result_text` → `step.output` (`escape_markup` → ChatStep) →
   `tool_result` block → stored `tool` row with raw `tool_result_json`.
8. **Model/tool output → UI.** `chat.send(str)` → `renderers=[safe_markdown]`; Pine fences →
   Copy (`js_on_click` with a serialised arg, no interpolation) / Inject buttons; the System
   log → `safe_text` (**broken**, § 3.6); toasts → `pn.state.notifications` (raw innerHTML).
9. **TV tool → sidecar → CDP.** `execute()` → MCP stdio → `chrome-remote-interface` →
   `127.0.0.1:9222` → post-processed result (fails open to raw on parse failure).
10. **Drive → local state.** By filename, first match; freshness by mtime; `integrity_check`
    for the DB; 1 MB cap for the documents; hash-change warning to the collapsed System log.
11. **Exceptions.** `claudia-session.log` (third-party loggers pinned to WARNING), the System
    log card (`f"… {exc}"` at six sites), the chat (`callback_exception="summary"`), tool
    results (`_safe_error` for IBKR tools; raw `{exc}` for the two local tools; a fixed sentence
    for TV tools), decision metadata. `redact_error` from the core has zero call sites in
    `claudia/`.
12. **Pine → TradingView.** `extract_pine_blocks` → Inject → `bridge.execute("pine_set_source",
    {"source": code})`; Copy → browser clipboard.

---

## 2. Phase 2 — Cross-repo contract, verified from code

| Question | Measured answer | Source |
|---|---|---|
| Which core tools are truly read-only? | 24 of 44 carry `READ_ONLY`. | `claude_tools.TOOL_DEFINITIONS[*]["capabilities"]` |
| Which tools mutate IBKR account state? | `create_price_alert`, `modify_price_alert`, `delete_alert`, `activate_alert` — POST/DELETE with **no gate** (`client.py:1733-1755`). ClaudIA exposes all four to the model. | registry + `client.py` |
| SQLite / Drive / external writers | DATABASE 6 (`get_trades`, `sync_flex_archive`, `import_flex_file`, `verify_flex_import`, `sync_flex_trades`, `run_backtest`); GOOGLE_DRIVE 5 (`fetch_market_data`, `sync_flex_trades`, `delete_cache`, `firecrawl_search`, `crawl_site`); NETWORK 2; WEB_FETCH 3; SANDBOX 1. | registry |
| Core methods that preview / place / modify / cancel / reply | `get_order_preview` (ungated, `/orders/whatif`); `place_order`, `modify_order`, `cancel_order`, `reply_order`, `_resolve_one_reply` (each gated before its first network call); `place_order_and_confirm`, `modify_order_and_confirm` (one Touch ID, an `OrderWriteAuthorization` bound to the body hash, 300 s, every reply still dialogued). | `client.py:158-178, 1366-1692` |
| Human gates today | Gate 1 `require_touch_id` with **`LAPolicyDeviceOwnerAuthentication`** (biometrics *with* Apple's device-password fallback, deliberately — `human_auth.py:108-111`), 60 s, fails closed on pyobjc missing / `canEvaluatePolicy` false / denial / timeout. Gate 2 AppKit subprocess (JSON on stdin) → osascript fallback on non-`HumanAuthError` exceptions → tkinter only off macOS → `HumanAuthError` when nothing is available. `HumanAuthError` (a user decision) never falls back. | `human_auth.py:96-130`, `order_confirm.py:427-464` |
| Any env var disabling a gate? | **None.** `grep os.environ|getenv` over `client.py`, `human_auth.py`, `order_confirm.py`, `_order_dialog.py` → no matches. | measured |
| Does ClaudIA call only the intended gated entry points? | Yes: `place_order_and_confirm` (`order_flow.py:1511`), `cancel_order` (`:1707`), `modify_order_and_confirm` (`:1967`). Never `place_order`/`modify_order`/`reply_order` directly; never passes `authorization=`; never imports `OrderWriteAuthorization`, `_authorize_order_write`, `require_touch_id`. | grep over `claudia/`, `scripts/` |
| Can ClaudIA reach lower-level write primitives? | `IBKRClient` is constructed in `order_flow.py` (five sites, three inside the cores, two for read-only labels) and `panel_app.py` (the shared toolkit). `_post`/`_session` are never referenced. In-process code *could* call `client._post` — the core names this residual itself (`human_auth.py:73-78`); it is the process, not the model. | grep |
| Is the core's structural test scope enough for ClaudIA? | **No.** `test_order_write_boundary.py` reads `claude_tools.py` and `mcp_server.py` only (`MODEL_LAYER`, line 30) and `package_sources()` is `ibkr_core_mcp/**`. ClaudIA's `agent.py`, `order_flow.py`, `panel_order_flow.py`, `panel_sink.py`, `execution_listener.py`, `scripts/replay_eval.py` are read by no structural test in either repo. | both test suites |
| Does ClaudIA's documentation describe the current contract? | Partly stale: "no password fallback" (false since the 2026-09-11 policy), "44 read-only tools" (20 are not), "cannot initiate any network request … for write operations" (alerts), the `_and_confirm` reply-loop wording (predates the authorization model), `scrape_fallback.py` (file gone). Full table § 4. | § 4 |
| Private-symbol imports | `_DOCKER_DIR` (`tests/test_install_check.py:158`, test-only). Module-public but not in `__all__`: `load_or_refresh_credentials`, `change_value_text`, `TOOL_DEFINITIONS`. Everything else is in the core's `__all__`. | grep |

**Ownership recommendation.** The core owns: the gate policy, the write-endpoint set, the
capability registry, the redaction functions, the SSRF guard's layer 2, the sandbox, the
transport rules. ClaudIA owns: the model-layer dispatch, the click boundary, proposal validation,
display correspondence, the markup allowlist, history authority, the claim-evidence framework,
its own test isolation, its subprocesses and the sidecar, the Panel server exposure, and Drive
handling. Cross-checked by both: the three entry points ClaudIA calls (one AST test each side),
the capability registry (ClaudIA computes its tool-capability sentence from it), and the pinned
core revision CI runs against.

---

## 3. Phase 3 — The candidate invariants, audited

Each row: the hypothesis, the evidence, the verdict on the question "structural or convention?".

### 3.1 CLA-SEC-001 — The model cannot initiate order execution

**Evidence.** Dispatcher has four sinks (§ 1.3 flow 1). `agent.py` imports no `order_flow`,
no `IBKRClient`, no `subprocess`. `_handle_local_tool` is a fixed if-chain over eight names. TV
tools are the 17 curated names (no order-shaped tool; the sidecar source at
`~/.tradingview-mcp/src` has no broker tool). The toolkit's dict has no write method.

**What existing tests prove.** `test_local_tool_names_excludes_order_write_tools` and
`test_locally_handled_tools_exclude_order_write_tools` check constants the author controls;
`test_proposal_handlers_cannot_reach_execution` does a substring search over
**`proposal_tools.py`** — the declarations, not the handler in `agent.py`.

**Verdict:** core side STRUCTURALLY PREVENTED (the core's AST tests); ClaudIA side
**PREVENTED BY CONVENTION ONLY.** A fifth dispatcher branch, or
`from claudia.order_flow import _execute_staged_order_core` in `agent.py`, keeps every test green.

**Not orders, but model-reachable IBKR writes exist:** the four alert tools. This makes two
ClaudIA sentences false (§ 4 rows 1, 3). It is a documentation problem, not a control problem —
but a sentence that says "no writes" is how an agent later assumes there is nothing to declare.

### 3.2 CLA-SEC-002 — Human UI interaction is required before execution starts

**Evidence.** Every caller of the three cores: the three `on_click` closures in
`panel_order_flow.py` (`:95, :148, :209`) plus tests. `replay_eval.py`'s sink raises on every
render and every tool step (`:85-104`) and never imports `order_flow`. `execution_listener`,
`dashboard_*`, `panel_app` import no order path (`panel_dashboard` is the one module with an AST
test saying so). No code in `claudia/` writes `.clicks`, calls `param.trigger`, or extracts a
watcher. `show_rerun/undo/clear=False`. A stored proposal is never re-rendered: `panel_app` never
reads messages into the feed, and `_pending_proposal` is cleared at the top of every turn.

**Residuals.** (1) The click handler is not re-entrant: invoking the callback twice dispatched
`place_order_and_confirm` twice (measured by calling the watcher twice under a mocked client);
the browser-side `disabled` is the only barrier and Gate 1 + Gate 2 would run again. (2) Any
local process can open its own session and click (§ 3.12) — the gates then prompt on the
operator's desktop with a proposal the operator did not author.

**Verdict:** PREVENTED BY CONVENTION ONLY, with the core's gates as the backstop. Not asserted
over `panel_app.py`, `panel_sink.py`, `execution_listener.py`, `dashboard_poller.py`, `scripts/`.

### 3.3 CLA-SEC-003 — Proposal tools are declaration-only

**Evidence.** `_record_proposal` (`agent.py:2799-2842`): three checks, one attribute assignment,
one string. `_proposal_defect` reads six keys. No network, no client, no store write. The
*render* step does a read-only IBKR call for the contract label and `_log_proposal` writes a
decision row — neither is an order write, but "reaches nothing" is true of the handler, not the
whole pre-click path.

**Verdict:** PREVENTED BY CONVENTION ONLY (behaviourally true; the guard that claims it reads
the wrong file).

### 3.4 CLA-SEC-004 — Proposal parameters remain immutable

**Evidence (transformation table, condensed; the full one is in the parameter agent's record).**
Symbol, action, quantity, order_type, tif, conid (when given), outside_rth, order_id: verbatim
from the strict-validated input to the POST body; `int(qty)` and `float(price)` are
value-preserving on schema-typed values; the client copies the dict (`dict(order)`), shows the
copy, strips `_` keys and POSTs the copy. Derived, not defaulted: `cOID`, `manualIndicator`,
FUT conid at click time (front month by `min(expirationDate or 0)`). Defaulted: `acctId =
accounts[0]` (shown in Gate 2); `tif or "DAY"` (unreachable from the strict path). Silently
dropped: a price that does not fit the order type (MKT+limit, LMT+stop) — not shown, not sent.
No writer to the proposal dict exists anywhere in `claudia/` (grep for every store/`update`/
`setdefault`/`pop` form: none).

**Verdict:** TRUE, pinned behaviourally on one payload each (`test_validator_never_mutates_the_proposal`,
`test_accepted_proposal_is_recorded_by_reference_not_reshaped`, `test_rejection_never_repairs_the_proposal`).
No structural test forbids a future `inputs[...] =`. The FUT front-month rule diverges from the
toolkit's (`_sorted_with_front_month` never flags a dateless row; `order_flow` would pick it) —
PLAUSIBLE-UNPROVEN whether `/trsrv/futures` ever emits one.

### 3.5 CLA-SEC-005 — What the human sees corresponds to what may execute

This is where the demonstrated gaps are.

| Gap | Measured | Layer |
|---|---|---|
| **Both human surfaces round prices to 2 dp; the body does not.** `_price_suffix("LMT", 4.1235)` → `" @ 4.12 limit"`; `_order_rows` Price → `"4.12 USD"`; body `price: 4.1235`. Two distinct proposals render identically. Affects every sub-cent tick: NG 0.001, 6E 0.00005, ZN/ZB 1/64 (`110.171875` shows `110.17`), sub-dollar equities. | `order_flow.py:100-102`; `ibkr_core_mcp/order_confirm.py:106,199` (`change_value_text`) | both repos |
| **A priced order type with a null price reaches Gate 2 as `Price: MARKET` / `Total: Market` beside `Order Type: LMT`.** The schema allows `null` (nullable by design for MKT); `_proposal_defect` does not cross-check price against type; the card prints "NO LIMIT PRICE GIVEN"; the core omits `price`; the dialog's `price is not None else "MARKET"` then names an order type the body does not carry. IBKR's outcome (reject vs accept-with-default) is **unproven**. | `agent.py:1218-1286`, `order_flow.py:1493-1501`, `order_confirm.py:106` | both repos |
| **The card is not a snapshot.** Render → mutate the dict → click: card said `BUY 10 AAPL LMT 100.00`, body sent `quantity 999, price 1.0` (measured). No writer exists today; the invariant rests on absence of writers, the same class the core closed one layer down on 2026-09-13 (`dict(order)` at method entry). | `panel_order_flow.py:70-113` | claudia_ui |
| **Bare-root FUT: the card names no contract.** `proposal_contract_label` returns `None` when `conid` is null, so the summary has no `Contract:` line despite its own docstring; the conid is chosen at click time; Gate 2 (via `_futures_contract_facts`) is the only pre-send naming. A roll between render and click changes the contract with nothing on the card to contradict. | `order_flow.py:155-157, 1387-1402, 445-447` | claudia_ui |
| **Modify has no click-time reconciliation.** The only live read before dispatch is `_current_order_description` (one free-text row); `conid`, side, quantity, type, tif, prices in the proposal are never compared with the live order; `changes[].previous_value` is model-authored and never verified. Freshness is enforced at *proposal* time (`get_order_status` this turn), not at click time. | `order_flow.py:229-242, 1247-1333, 1865-1967` | claudia_ui |
| **`conid` is shown nowhere** (card or dialog). A ticker/conid mismatch trades the conid; a failed `get_contract_info` read leaves the bare ticker with no sign the read failed. | `order_confirm.py:123-144`, `order_flow.py:280-284` | both |
| No proposal expiry; earlier cards' closures stay live across turns (the 300 s TTL starts at Touch ID). | `agent.py:1880`, `panel_order_flow.py` | claudia_ui |
| Cancel dialog built from the proposal when the status read fails, unlabelled as such. | `order_flow.py:369-386` | claudia_ui |

**Verdict:** VIOLATED on precision and on the null-price case; WEAK on snapshot, FUT naming,
modify reconciliation.

### 3.6 CLA-SEC-006 — Untrusted content cannot execute in the UI

**How Panel renders (verified in the venv, panel 1.9.3 / bokeh 3.9.2).** Every HTML-model pane
(Markdown, HTML, **Str**) gets one server-side transport escape; the client undoes it once
(`panel.js:18302 html_decode(this.model.text)`), assigns `innerHTML` (`:18246`) and re-creates
`<script>` nodes (`run_scripts`, `:18248`, model default `run_scripts=True`). Single-escaped
text = live markup; double-escaped = literal. `safe_markdown` double-escapes because markdown-it
with `html: False` emits entities itself.

**Measured model text for `<img src=x onerror=alert(1)>`** (reproduced by the coordinating
reviewer):

```
default Markdown   run_scripts=True text='&lt;img src=x onerror=alert(1)&gt;'
safe_markdown      run_scripts=True text='&lt;p&gt;&amp;lt;img src=x onerror=alert(1)&amp;gt;&lt;/p&gt'
safe_text (Str)    run_scripts=True text='&lt;pre&gt;&lt;img src=x onerror=alert(1)&gt;&lt;/pre&gt;'
```

`safe_text` is byte-for-byte the vulnerable shape. Browser confirmation (Playwright against a
throwaway `pn.serve` page, `window.__hits`): **executed** — `safe_text`, the System log line,
default Markdown, a bare string in a `Column`, `Card.title`, `Number.label`, ChatStep title,
ChatStep exception body, `ChatMessage.user`, toasts. **Literal** — `safe_markdown`, `chat.send`
strings, Tabulator cells.

**What reaches the broken sinks today.** The System log receives: the IBKR fill headline
(`panel_app.py:448`, contract string from the WebSocket), `check_flex_coverage` tool output and
the Flex-sync exception (`:721-728`), gateway preflight `result.detail` (carries gateway JSON
fields), reconnect exceptions (`:594`), any session-init exception (`:1273`). Toasts receive the
same strings at warning/error level and the poller's error text (`panel_dashboard.py:1444`).
The ChatStep title receives the model's `tool_use.name` before any lookup
(`panel_sink.py:187-190`).

**The existing tests assert the wrong polarity.** `test_safe_text_renders_a_payload_as_literal_text`
asserts `"&lt;img" in model.text` — the exact signature the same file's `_decodes_to_markup`
defines as vulnerable. `test_panel_system_log.py:60-64` likewise. The structural guard is a
regex over three constructor spellings and misses `pn.panel(`, bare strings in layouts,
`ChatStep(...)`, `Card(title=)`, `Number(label=)`, HTML formatters, `notifications.*`.

**Verdict:** VIOLATED for the System log and toasts (demonstrated); the chat feed, proposal
summaries, dashboard panes, Tabulator cells, ChatStep bodies and the Pine copy path are
ALREADY MITIGATED. Attacker control over the broken sinks today is low (IBKR- and
gateway-sourced strings, local tool output, exception text) — no web content reaches them —
which is why severity is Medium, not High. The control is nonetheless broken and its test is
inverted.

### 3.7 CLA-SEC-007 — Persistent history never gains unintended authority

**Evidence.** Role mapping is literal and closed (`agent.py:1513-1549`); the store's CHECK
constraint refuses `role='system'` (measured: `IntegrityError`); tool rows are dropped, not
reconstructed; withdrawn rows are excluded from replay (measured on a throwaway DB); the
operator channel is rebuilt each turn from decision rows and is never stored; nothing from the
DB enters `system=` except the version label and date; the safety block is appended last.

**The demonstrated gap.** `search_past_conversations` (`conversation_store.py:527-533`) joins
neither `message_withdrawals` nor a role filter. Measured: a withdrawn fabricated "Cancel
staged — button's above for order 777" came back as a `tool_result` with no withdrawal marker.
The safety block's DATA INTEGRITY section names tool results a "guaranteed source", so a
withdrawn fabrication is re-licensed one turn later.

**Weaknesses.** Guardrail notices are stored as `assistant` and rendered with the model's author
label, so a model-written "⚠️ That message described an action that never happened" is
visually identical to a real correction (the markup cannot fake an avatar, but the text can).
Real fills leave no message row — only an `execution_reported` decision — so in the transcript
a real fill and an invented one look the same. `get_history(limit=40)` counts tool and withdrawn
rows before filtering.

**Verdict:** Hard Rule 4 TRUE and pinned; withdrawal boundary VIOLATED through FTS.

### 3.8 CLA-SEC-008 — Claims about actions require evidence

**Instruction vs detection vs evidence vs blocking.** The evidence source is always server-side:
`called_tools` accumulated from stream `tool_use` blocks and `_pending_proposal` set only by the
API-validated handler. No detector reads model text *as evidence*; model text is only the thing
being checked. Runtime blocking exists (withdraw-before-display + one retry) but only on
zero-tool turns or read-only-plus-proposal turns; otherwise it is a ⚠️ notice, a withdrawal row
and an operator note (post-hoc). Persisted records: the raw `tool_result_json` per call is the
trustworthy discriminator; decision rows carry every outcome after the button except the
pre-dispatch refusals (§ 3.14).

**False negatives (executed against the four shapes):** "I've staged the cancel… click the button
to confirm", "Cancel proposal submitted", "Order 123 has been cancelled at IBKR", "I checked IBKR
and…", "That's straight from the API, untouched", "Done — switched to TSLA" — all MISS. Any real
tool call in the turn clears the action and payload detectors entirely (documented give-up).
**False positives (executed):** definitional sentences ("Staged — in this app — means a button
was rendered…", "The button below is the Launch TradingView action, not an order button") fire
on a zero-tool turn, withdraw an honest answer and force a `tool_choice: any` retry. The retry
is not restricted to `_RETRY_SAFE_READS`, so the forced pass may call a Drive or alert writer.
The corpus-precision tests skip in CI (git-ignored `data/claudia.db`).

**Verdict:** the *evidence* property is TRUE (detectors read the API, not the model);
*prevention* is narrow by design and is documented as detection. The framework is not
bypassable by the model in the sense of forging evidence; it is bypassable by phrasing. That
should be stated as the contract: "claims are detected for four shapes; the record is
complete; nothing is prevented outside those shapes".

### 3.9 CLA-SEC-009 — Unit tests cannot touch live systems

**Mechanism today:** none. `pytest-socket` is not installed; `tests/conftest.py` holds two
button helpers; isolation is per-test mocking. Measured with a socket-recording plugin over the
full suite (1,828 collected, two runs, identical): **4 tests do real DNS** (`example.com`,
through `_validate_public_url`'s `gethostbyname`), **4 tests open a real TCP connection to
`127.0.0.1:5055`** (`panel_app.main()` → `warn_if_session_borrowed` unpatched; they pass offline
because `read_state` swallows `Exception`; with the gateway up they issue `GET /tickle`, the
IBKR session keepalive), and `import claudia.panel_app` runs `load_dotenv` at module level so
**the real `ANTHROPIC_API_KEY` and `IBKR_FLEX_TOKEN` are present in the pytest process**
(probe inside a test: `sk-ant-` prefix, length 108). The suite is green with every secret
stripped and `load_dotenv` no-op'd, so nothing depends on them. `live_api` tests are gated by
an env var, not a socket block.

**Verdict:** VIOLATED (demonstrated, low consequence today).

### 3.10 CLA-SEC-010 — Sidecars receive minimum privilege

**Sidecar.** List-form spawn via `mcp.stdio_client` → `anyio.open_process` (exec, not shell).
Our env dict: `PATH HOME USER TMPDIR TEMP TMP NODE_PATH NODE_ENV XDG_RUNTIME_DIR` + the three
CDP-port names. mcp then merges its own defaults (`HOME LOGNAME PATH SHELL TERM USER`). No secret
reaches the child; nothing logs env or argv. `cwd=None` → ClaudIA's cwd (the repo root);
stderr is ClaudIA's fd 2; own session/pgid; `stop()` only on the TradingView reconnect path,
never at server exit (relies on stdin EOF). `node` is PATH-resolved with two user-writable
directories ahead of Homebrew. `TRADINGVIEW_MCP_PATH` accepts any existing `.js` file. Port and
binary are resolved at **import time**, before `panel_app` calls `load_dotenv`, so a `.env`-only
port or path is ignored (invisible today because `.env` holds the default port and an empty
path). **Measured consequence of the cwd:** the curated `tv_health_check` runs `git rev-parse
HEAD` and reads `remote.origin.url` with no `cwd` (`src/core/health.js:17-30`) — so it reports
`claudia_ui`'s commit and queries `api.github.com/repos/stephus182/claudia_ui` for updates.

**Everything else** (`docker exec`, `pgrep`, `pkill`, `osascript`, `open -a`, `git`, the Gate 2
dialog subprocess in the core) passes `env=None` → the full environment including
`ANTHROPIC_API_KEY`, contrary to SECURITY.md's "any new subprocess uses an env allowlist". The
Docker socket is user-owned. **The keepalive LaunchAgent** (`scripts/ibkr-keepalive.sh:68-74`)
does `set -a; source "$ENV_FILE"` to obtain one URL and therefore holds every `.env` secret
24/7 and hands it to `curl` and `caffeinate`.

**Tests:** patch `StdioServerParameters` and assert two names absent from *our* dict — they
would stay green if mcp started merging `os.environ`.

**Verdict:** environment isolation TRUE at the sidecar (by convention, measured at the wrong
seam); capability isolation NIL (an unsandboxed same-user process); non-sidecar spawns
inherit everything; the daemon is the longest-lived secret holder on the machine.

### 3.11 CLA-SEC-011 — TradingView sidecar provenance is verifiable

**Measured.** Live clone `~/.tradingview-mcp` at upstream `55534aab…`, remote correct, `src/`
unmodified, but **`package-lock.json` modified and uncommitted** (+61/−31: `@modelcontextprotocol/sdk`
1.27.1→1.30.0, `@hono/node-server` 1.19.14→2.0.12, `body-parser`, `js-yaml` … — an `npm install`
on 2026-07-31 re-resolved the carets). `vendor/tradingview-mcp/src` is byte-identical to the
live `src`; its lockfile is the *drifted* one, not the commit's; `ARCHIVE_INFO` records commit,
date, node version and `du` size — **no hash**. Only `ARCHIVE_INFO` and `package.json` are
tracked; the "known-good fallback" exists on this machine only. `archive-tv-mcp.sh` does not
refuse a dirty tree and runs `npm ci` (a network fetch) at archive time. No install scripts, no
native addons in either `node_modules`; the lockfile carries integrity hashes. Runtime
provenance signal: `git rev-parse --short HEAD`, blind to uncommitted edits, absent for vendor.
Today the runtime selects `~/.tradingview-mcp/src/server.js`.

**Verdict:** NOT REPRODUCIBLE from a fresh clone; SUPPLY-CHAIN WEAKNESS, not a demonstrated
compromise.

### 3.12 CLA-SEC-012 — External/browser origins cannot gain unintended authority

**Measured from Bokeh/Tornado source in the venv.** Bind `127.0.0.1`, origin allowlist exactly
`{localhost:8001, 127.0.0.1:8001}`; a browser `Origin` outside it is refused (`ws.py:93-122`);
DNS rebinding therefore cannot attach a WebSocket. **But:** an absent `Origin` is not checked at
all (`tornado/websocket.py:265-268`); `GET /` checks nothing and serving it runs the full session
init; session tokens are **unsigned** (`sign_sessions=False` → `check_token_signature` returns
`True`), so any local client can mint a token for its own session; once attached, `PATCH-DOC`
sets any model property, including `Button.clicks`. `BOKEH_ALLOW_WS_ORIGIN` in the environment
*replaces* the allowlist (`ws.py:112-113`) and `panel_app` loads `.env` — nothing forbids it.
`?theme=` is the only query parameter read and is whitelisted.

**Honest boundary statement.** The trust boundary is "every process on this Mac", not "this
user": the 0600 file modes protect the documents and DB from other uids on the file path; the
network path bypasses that entirely. Gate 1 and Gate 2 stand between a rogue session and IBKR,
as *consent* gates on a proposal the operator did not author. A phantom `GET /` (any local
process; a hostile page as a subresource, subject to the browser's private-network policy) costs
Drive reads, IBKR reads, a `sessions` row, and — after Bokeh's unused-session timeout — a
session report and a `claudia.db` upload. The user's own live session id (261 bits, not logged,
not stored) is a REJECTED CONCERN.

**Verdict:** cross-origin browser attack ALREADY MITIGATED and pinned; local-process authority
an ACKNOWLEDGED ARCHITECTURAL WEAKNESS whose documentation understates the boundary;
`GET /` side effects a weakness.

### 3.13 CLA-SEC-013 — Secrets cannot reach model, UI, logs, sidecars, or persistence

**Measured (counts only).** `claudia-session.log`: `sk-ant-` 0, `?t=` 0, `Bearer` 0, `ya29.` 0,
`Cookie` 0, one `U\d{7}` account-id line (`dashboard_poller.py:529`). `claudia.db` (1,132
message rows, 467 with `tool_result_json`): every secret shape 0; `U\d{7}` in 71 tool results,
17 message contents, 59 decision metadata rows (expected — it is uploaded to Drive and is 0600
locally). 173 session reports: 0. The IBKR gateway's own cookie-logging line is reachable only
through the `gateway_launch` CLI's stdout. Google token/credential files are never enumerated or
uploaded (fixed names `claudia.db`, `store.db`). The Flex `?t=` path is covered indirectly by
the core's `_safe_error` because every Flex call goes through `toolkit.execute`.

**Gaps.** `redact_error` has zero call sites in `claudia/`; 74 raw `{exc}` log sites and the
System-log/chat exception paths are safe only by the *type* of exception each can see. The
gateway preflight logs the IBKR **username** at ERROR level. `.env` is loaded into every test
process (§ 3.9) and into the keepalive daemon (§ 3.10). Anthropic API exceptions render
verbatim in chat via `callback_exception="summary"` (the SDK does not include the key;
acknowledged in SECURITY.md).

**Verdict:** no leak demonstrated; the guarantee is by exception type, unenforced.

### 3.14 CLA-SEC-014 — Fail closed on security-sensitive ambiguity

| Condition | Behaviour | Class |
|---|---|---|
| Proposal defect; second proposal in a turn; modify without `get_order_status` this turn | `REJECTED — …` tool result, nothing rendered, dict untouched | SAFE-REJECT |
| Non-FUT without `conid`; FUT with empty `get_futures`; modify missing `order_id`/`conid` | status line, `return` from inside the `try`, **no decision row** — contradicts CLAUDE.md "every outcome after the button leaves a decision row" (gap #50 closed the exception paths only) | SAFE-REJECT, unrecorded |
| FUT row with missing `expirationDate` | sorts first and is selected | SILENT-DEFAULT (changes contract choice; Gate 2 label derived from the chosen conid mitigates) |
| LMT/STP with null price | proceeds to Gate 2 as `MARKET` (§ 3.5) | PROCEED — **changes what the human believes** |
| `outside_rth` null | key omitted, IBKR default; futures-stop warning on the card | SILENT-DEFAULT, documented |
| Render failure in the sink | tool result had already said "will be rendered"; `proposal_render_failed` row + notice + operator note | WARN-CONTINUE with correction |
| Model without operator channel | ERROR at startup, then a 400 per turn | fails closed by accident |
| Read-back failure after dispatch | `*_dispatched_unverified`, `dispatched: True` | correct |
| TV result unparsable | fails open to the raw payload; `claudia_warning` on success-over-empty | correct direction for a reporting seam |
| Drive download failure | warn, run on local/empty | WARN-CONTINUE |
| Both documents missing | `FileNotFoundError` → "Setup required", no agent | SAFE-REJECT |
| Drive-only documents, then a local file event | overrides cleared, next turn raises unexplained | fails closed, unexplained |
| Document hash changed vs previous session | warning to the **collapsed** System log; session continues; baseline read from the newest `sessions` row of a DB that is itself Drive-sourced (`integrity_check` only) — a writer who replaces both files with a consistent hash produces no warning | WARN-CONTINUE — **the system prompt changed** |
| Stale editable install (module set) | loud ERROR, continues; the missing-snapshot case (§ 0) is a hard `ModuleNotFoundError` | mixed |
| Connectivity `UNKNOWN` | initial state only; nothing on the order path consults it | neutral |

---

## 4. Phase 4 — Documentation truthfulness

T = true · O = overstated · S = stale · U = unverifiable. Owner = who should hold the fact.

| # | Claim | Where | Verdict | Evidence | Owner |
|---|---|---|---|---|---|
| 1 | "44 read-only `ClaudeToolkit` tools" | SECURITY.md:31 | **O/S** | 44 definitions; 20 carry a non-`READ_ONLY` capability; 4 write IBKR account state ungated; ClaudIA never reads the registry | core owns the registry; ClaudIA computes, never hand-writes, the adjective |
| 2 | Zero tools for order execution; cannot call `place_order`… | SECURITY.md:28-41, CLAUDE.md HR1 | **T** | § 3.1 | both |
| 3 | "cannot initiate any network request to the IBKR gateway for write operations" | SECURITY.md:42 | **O** | alert tools POST/DELETE | claudia_ui (narrow to "order writes") |
| 4 | "only `ClaudeToolkit.execute()` is exposed to the agent loop" | SECURITY.md:43 | **T** for the loop | UI-layer code uses `IBKRClient` directly, disclosed in §9 | claudia_ui |
| 5 | Gate 1 "no password fallback" | SECURITY.md:73 | **S — false** | `LAPolicyDeviceOwnerAuthentication`, device password after a failed scan, by design since 2026-09-11; the core corrected its own file | core owns; ClaudIA must point, not copy |
| 6 | Three entry points; cancel gated inside `cancel_order()` | SECURITY.md:67-71 | **T**, incomplete | the one-authorization-per-write model is absent from the wording | core |
| 7 | "No LLM prompt, tool call, conversation state, or automation can bypass steps 3–5" | SECURITY.md:80 | **T** with the core's stated limit (in-process code) and one ClaudIA caveat (any local process can create a session and click, § 3.12) | both |
| 8 | "ClaudIA has no tools that write to the filesystem" | SECURITY.md:105 | **O** | DATABASE tools write `store.db`; the sandbox runs model code | claudia_ui |
| 9 | History as structured role blocks, never in the system prompt | SECURITY.md:22,121 | **T** | § 3.7 | claudia_ui |
| 10 | FTS results "truncated at a configurable token budget (default 2,000 tokens)" | SECURITY.md:124 | **U/S** | `max_results=5`, 300-char snippets; no token budget exists | claudia_ui |
| 11 | "`decisions` has no search path … never re-injected" | SECURITY.md:136 | **O** | identity fields are replayed on the operator channel by design | claudia_ui |
| 12 | "All rendering goes through `claudia/panel_markdown.py`" | SECURITY.md:23,461 | **O — and one of its two helpers is broken** | § 3.6 | claudia_ui |
| 13 | `_SAFETY_BLOCK` at `agent.py:58-195` | SECURITY.md:146 | **S** | now `:77-233` | claudia_ui (line numbers do not belong in a security doc) |
| 14 | §5.1 detector/record table | SECURITY.md | **partially S** | 2026-09-11 layers absent | claudia_ui |
| 15 | Sidecar env "`PATH, HOME, USER, TMPDIR, NODE_*`" / "`connection.js` reads `CHROME_REMOTE_DEBUG_PORT`" | SECURITY.md:22, 276-282 | **T abbreviated / S** | 9 names + 3 port names; mcp merges 6 more; `connection.js` reads `TV_CDP_PORT|CDP_PORT` (the code comment knows; the doc does not) | claudia_ui |
| 16 | "Binary path validated" | SECURITY.md:283 | **O** | exists + endswith `.js` | claudia_ui |
| 17 | "Any new subprocess call uses an env allowlist — never `env=None`" | SECURITY.md:716 | **checklist no existing site meets** | § 3.10 | claudia_ui |
| 18 | Doc hash change → "a visible WARNING message in chat" | SECURITY.md §10.1 | **S** | collapsed System log since 2026-09-03 | claudia_ui |
| 19 | `.env`, both docs, snapshots 0o600 | §1, §3, §7 | **T today** | stat | claudia_ui |
| 20 | `pn.serve` loopback + origin list | §8 | **T** | pinned | claudia_ui |
| 21 | SSRF layer 2 in `ibkr_core_mcp/scrape_fallback.py` | §8 | **S path** | file gone; `local_browser.py` | core |
| 22 | `verify=False` "scoped to two calls in `status.py`"; `/tickle` "the only state-changing call" | §9, §11 | **S** | five sites in `gateway_preflight.py`/`gateway_session.py`, including `POST /logout` | claudia_ui |
| 23 | Lights are `BooleanStatus` widgets | §9, §11 | **S** | action-bar buttons since 2026-09-03 | claudia_ui |
| 24 | Tabulator guard "over **every** `Tabulator`" | CLAUDE.md, SECURITY.md §9 | **O** | `_positions` has the handler assertion; `_orders` has only `disabled is True` — the surface the prose says matters most | claudia_ui |
| 25 | 53 security-regression tests | §13 | **S** | 58 collected | claudia_ui |
| 26 | Open items owned by core: `store.db` 0644, `credential.json` 0644 | §13 | **S — resolved** | `store.py:206-210`; file absent | core |
| 27 | Drive scope, 1 MB guard, `integrity_check`, `RLock` | §7, §10 | **T** | code | both |
| 28 | `_proposal_defect` four checks; one proposal per turn | §2 | **T** | code + tests | claudia_ui |
| 29 | CLAUDE.md: 44 tools, 17 curated, 5 local, "1,725 tests as of 2026-09-08" | CLAUDE.md | **T, dated** | 44 / 17 / 5 / 3; 1,828 collected today | claudia_ui |
| 30 | "every outcome after the button leaves a decision row" | CLAUDE.md § Order Staging | **O** | three pre-dispatch refusals leave none | claudia_ui |

What `tests/test_docs_claims.py` checks today: backticked repo paths exist; `docs/README.md`
lists every flat doc. No count, policy or mechanism claim is machine-checked.

---

## 5. Phase 5 — CI and supply chain

### 5.1 Gate comparison

| Gate | ibkr_core_mcp | claudia_ui |
|---|---|---|
| ruff check · ruff format --check · mypy strict · pytest (3.11, 3.12) | yes | yes, same order |
| Resolved-version print of floating deps | yes | no |
| pip-audit (requirements mode, `--strict`, OSV, reasoned ignore file, weekly cron) | yes, blocking | **no** |
| gitleaks (`.gitleaks.toml`, full history) | yes, blocking | **no** |
| `tests/security/` structural suite | 10 files | `tests/test_security_regressions.py` (58 tests, some regex-based, one inverted) |
| CodeQL default setup | yes | yes (2026-09-08) |
| Dependabot alerts | enabled | **disabled** |
| Action pinning | tags | tags |
| Cross-repo checkout | n/a | `stephus182/ibkr_core_mcp` at **floating `main`**, no `ref` |

### 5.2 pip-audit

**Verdict: ClaudIA needs its own gate.** The combined resolved tree is 156 packages; the core's
`[dev,server,scraper]` tree is 139. **19 packages are ClaudIA-only** and audited by nobody:
`bokeh, panel, tornado, holoviews, hvplot, param, pyviz-comms, panel-material-ui, colorcet,
contourpy, html2text, linkify-it-py, markdown, mdit-py-plugins, narwhals, nh3, watchdog,
xyzservices` (+ `claudia-ui`). `tornado` is the HTTP listener. A requirements-mode audit of
`.[dev]` + `../ibkr_core_mcp[scraper]` today yields exactly one finding, the `nltk` no-fix
advisory the core already ignores with a re-check date; ClaudIA's ignore file should carry the
same line (or read the core's). Runtime ≈1–2 min, resolve only.

### 5.3 gitleaks and history

`gitleaks` is not installed locally; a manual full-history scan (`git log -p --all`, counts
only): `sk-ant-api` 0 · `AIza` 0 · `ya29.` 0 · `"refresh_token"` 0 · Firecrawl `fc-` 0 ·
GitHub token shapes 0 · `PRIVATE KEY` 0 · `client_secret` 11 (historical plan docs and one
`.claude/settings.json` line naming a client-**id** filename in a permission allowlist — not a
secret, but that file is tracked). `U\d{7}`: 154 hits, 3 distinct values — one placeholder
family, two shapes that look like real account ids in history, classified with the accepted
screenshot residue (blobs reachable at `63030bd`, the post-rewrite equivalent of `f434312`;
**accepted per user decision 2026-07-25, not re-raised**). Refs: `main`, two tags, no stashes.

**Custom rules ClaudIA would need beyond defaults and the core's two:** `IBKR_FLEX_TOKEN=\s*\d{15,}`
(long numeric, no default rule matches); `\bU\d{7}\b` scoped to non-doc paths with placeholders
allowlisted; Google OAuth shapes (`"client_secret":"GOCSPX-`, `"refresh_token":"1//`). Run once
over full history locally before enabling, to size the account-id rule's noise.

### 5.4 GitHub Actions

Tags (`checkout@v5`, `setup-python@v6`) in both repos. **Hardening**, not a concrete path:
SHA-pin in both or neither, and enable Dependabot alerts on `claudia_ui` (free; keeps SHAs current).

### 5.5 Cross-repo checkout design

Today: floating `main`. A green `claudia_ui` commit is not reproducible (a re-run may install a
different core) and a core push can turn ClaudIA red with no ClaudIA commit. There is no measured
drift today (the venv's snapshot was `d4b3347`, the core's HEAD). Recommended: a **required lane
on a pinned core ref** (a one-line `ibkr_core_mcp.ref` file or the workflow, bumped deliberately
in a commit that says why) and an **informational lane on core `main`** (`continue-on-error:
true`) that reports forward-compatibility and security-contract drift with a date and an owner.
The pinned ref is also the natural place to record "the core revision whose security contract
ClaudIA's cross-repo test was written against".

### 5.6 TradingView Node supply chain

Based on the measured runtime selection (§ 3.11): track the vendor `package-lock.json` (small,
text, integrity-bearing) so the dependency set is reproducible from a fresh clone; make
`archive-tv-mcp.sh` refuse a dirty tree, archive the lockfile from the commit
(`git show HEAD:package-lock.json`), and record `src_sha256` and `lock_sha256` in
`ARCHIVE_INFO`; a test recomputes both (skipped when `vendor/src` is absent, as on CI — the
same pattern as the git-ignored fixtures); a second test asserts the live clone matches the
archive (red today on the lockfile — that is the finding). Runtime: hash `src/` under either
known root and log a loud ERROR on mismatch (TOFU; an upgrade re-blesses via the archive
script). `npm audit` is optional and informational — no install scripts, no native addons, and
the lockfile already carries integrity hashes; the exposure is the caret ranges at resolve time,
which the lockfile pin closes.

---

## 6. Phase 6 — What belongs in `tests/security/`

Proposed layout, each with the invariant, the failure it detects, style, FP risk, cost.
Every structural test carries a "guard on the guard": a deliberately bad snippet the detector
must flag (the core's `test_order_write_boundary.py:101-121` idiom).

| File | Invariant | Detects | Style | FP risk | Cost |
|---|---|---|---|---|---|
| `test_model_cannot_execute_orders.py` | CLA-SEC-001 | `agent.py`, `proposal_tools.py`, `panel_sink.py`, `execution_listener.py`, `scripts/**` referencing any of `place_order, modify_order, cancel_order, reply_order, *_and_confirm, _resolve_one_reply, _authorize_order_write, OrderWriteAuthorization, require_touch_id, _post, _session, IBKRClient` or importing `claudia.order_flow`/`panel_order_flow`; the dispatcher's callee set growing beyond `{_handle_local_tool, execute}` on `{_tv_bridge, _toolkit}` | AST | low (docstrings excluded by walking nodes, not text) | low |
| `test_human_click_execution_boundary.py` | CLA-SEC-002 | a core called anywhere but a nested async def in `panel_order_flow.py` later passed to `.on_click`; any `.clicks` store or `param.trigger`/`.trigger(` in `claudia/`; the three gated client calls outside `order_flow.py`; any `authorization=` kwarg or `patch(...require_touch_id...)` outside `tests/` | AST | low | low |
| `test_proposal_tools_are_side_effect_free.py` | CLA-SEC-003 | `_record_proposal`/`_proposal_defect` referencing any `self.*` other than `_pending_proposal`, `_called_tools_this_turn`; any `Call` to a client/network/thread/subprocess name; any `Subscript(ctx=Store)` or `update/setdefault/pop/clear` on `inputs`/`proposal` there or in `render_*_proposal` | AST + the existing behavioural trio | low | low |
| `test_display_corresponds_to_execution.py` | CLA-SEC-004 | render → mutate dict → click → body ≠ rendered values (red today); stage callback twice → `assert_called_once` (red today); `_price_suffix`/`_format_*_summary` on `4.1235`, `110.171875` must contain the exact decimal and two NG prices must differ (red today); `_proposal_defect` returns a defect for LMT/STP/STOP_LIMIT with the required price null (red today); bare-root FUT card must name the resolved contract or the resolution rule; a dateless `get_futures` row is never chosen | behavioural | low | low; the core-side twin for `_order_rows` (no `MARKET` beside `LMT`; exact decimals) lives in the core |
| `test_ui_rendering_boundary.py` | CLA-SEC-005 | AST allowlist keyed `(file, callee)` over `pane.Markdown/HTML/Str/Alert/JSON`, `pn.panel`, `ChatMessage`, `ChatStep`, `Card(title=)`, `indicators.Number`, `Tabulator(formatters=/header_tooltips=)`, `state.notifications.*`, and bare `str` positional args to layouts; behavioural canaries per allowed sink asserting the **double**-escaped model text (`&amp;lt;`), including `safe_text`, `SystemLog.say`, the toast argument, the ChatStep title, the ChatStep exception body, `ChatMessage.user`; Tabulator column formatter is `StringFormatter`; the inverted tests corrected | AST + behavioural | medium at first (the allowlist must name today's sites) | medium |
| `test_history_authority_boundary.py` | CLA-SEC-006 | a stored role replayed as a higher role; `add_message(role="system")` succeeding; a withdrawn row returned by `search_messages` (red today) or by `_history_to_messages`; any string from the store concatenated into `system=` beyond the version note (AST over `_build_system_prompt`'s inputs) | behavioural + AST | low | low |
| `test_action_claim_evidence.py` | CLA-SEC-007 | a detector reading anything but `called_tools`/`_pending_proposal` as evidence; the retry's `called_tools ⊄ _RETRY_SAFE_READS`; mutation tests that pin the `n't` veto, `_BUTTON_IS_HERE`, `_PAST_TURN` left-of-match; the definitional-sentence FPs frozen as accepted or vetoed | corpus + mutation | medium (regex shapes) | medium |
| `test_no_live_io.py` | CLA-SEC-008 | DNS resolves; TCP connects; a secret-prefixed variable visible; `import claudia.panel_app` leaving `os.environ` dirty | fixture (pytest-socket markers at collection + configure-time `load_dotenv` no-op) | low; 4 named DNS exemptions | low |
| `test_sidecar_environment.py` | CLA-SEC-009 | spawn outside `{tradingview.py, gateway_launch.py}`; non-literal argv; `shell=`; the **merged** child env (patched at `anyio.open_process`/mcp's process factory, not at `StdioServerParameters`) ≠ allowlist ∪ mcp defaults ∪ port names; `cwd` ≠ the sidecar dir; `scripts/ibkr-keepalive.sh` containing `source "$ENV_FILE"`; `_TV_DEBUG_PORT`/`_TV_MCP_BIN` resolved before `load_dotenv` | AST + behavioural + script grep | low | low |
| `test_tradingview_provenance.py` | CLA-SEC-010 | `ARCHIVE_INFO` without `src_sha256`/`lock_sha256`; vendor hashes ≠ `ARCHIVE_INFO` (skip when absent); live clone ≠ archive (skip when absent; red today) | fixture | low | low |
| `test_server_exposure.py` | CLA-SEC-011 | `pn.serve` kwargs drifting (exact origin list, no wildcard); `BOKEH_ALLOW_WS_ORIGIN`/`BOKEH_SIGN_SESSIONS` present after `load_dotenv` without refusing; opt-in loopback probe documenting the absent-Origin/unsigned-token behaviour so it flips red the day signing is enabled; a session with no user message uploading `claudia.db` on destroy | behavioural, one opt-in | low | low |
| `test_cross_repo_contract.py` | CLA-SEC-012 | ClaudIA importing a non-`__all__`/private core symbol outside a declared list; the three gated entry points' names and keyword set (`reply_log=`, `order_details=`) changing; the capability registry losing a name ClaudIA's doc-claim test computes from; the "read-only" adjective appearing in a living doc against the 44-tool set | AST + registry import + docs grep | low | low |

---

## 7. Phase 7 — Documentation structure

Adopt the core's three layers; it fits, and it is the only way the "point, don't copy" rule
stops the drift measured in § 4.

1. **`SECURITY.md`** — rewrite as a control inventory and disclosure page. Remove every fact the
   core owns (gate policy, endpoint set, redaction, SSRF layer 2, sandbox) and replace with a
   pointer to `ibkr_core_mcp/SECURITY.md` and `docs/security-architecture.md`; remove line
   numbers and counts that are not computed by a test; narrow the absolutes to threat-model
   statements ("the model has no order-execution tool; it has four ungated alert writers, which
   the core declares `ACCOUNT_STATE`"). Keep vulnerability reporting.
2. **`docs/security-architecture.md`** — new, living: § 1 principals (from § 1.1), § 2 ClaudIA's
   capability additions (§ 1.2), § 3 the trust-boundary map (§ 1.3), § 4 the twelve invariants
   with mechanism / enforcement / to-change (§ 11), § 5 the cross-repo contract table (§ 12),
   § 6 subsystem designs (click boundary, display correspondence, markup allowlist, history
   authority, claim evidence, Drive), § 7 CI as a security instrument, § 8 decision log (start
   with the decisions this audit forces: proposal dict snapshot vs identity test; exact price
   display; null-price rejection at the proposal layer; `safe_text` replacement; FTS withdrawal
   join; pytest-socket; pinned core ref), § 9 known limits (local-process authority; Drive as a
   remote prompt author; sidecar capability isolation nil), § 10 change recipes.
3. **`docs/audits/security-architecture-audit-2026-09-13.md`** — this document, moved after
   review, immutable thereafter except for labelled addenda.

---

## 8. Phase 8 — Findings

### A. Confirmed vulnerabilities

| ID | Title | Sev | Conf | Invariant | Path | Scenario | Why current controls fail | Smallest remediation | Regression test |
|---|---|---|---|---|---|---|---|---|---|
| **A-1** | The System log's text pane and the toasts execute markup | Medium | High (model text reproduced; browser execution measured by the UI agent) | CLA-SEC-005 | `panel_system_log.py:108 safe_text` → `pn.pane.Str` (single transport escape, `run_scripts=True`); `:117,119` and `panel_dashboard.py:1444` → `pn.state.notifications` (`innerHTML`) | An IBKR contract string, a gateway JSON field, a tool's error text or an exception body containing `<img onerror=…>` reaches `syslog.say()` and runs in the trading UI's origin (read the page, forge chat, click buttons). Attacker control over those sources is low today — no web content reaches the System log — hence Medium. | `panel_markdown.safe_text` was verified once (2026-09-04) at the server-side escape and not at the client decode; `test_safe_text_renders_a_payload_as_literal_text` asserts the vulnerable signature (`&lt;img` present) as success; the structural regex only sees three constructor spellings | Make `safe_text` produce a double-escaped model (e.g. `safe_markdown` of the escaped body in a fenced/monospace style, or `Str(escape_markup(text))` after confirming the client decodes once); wrap `notifications.*` in one escaping helper in `panel_markdown.py`; flip the two inverted tests | `test_ui_rendering_boundary.py` canaries for `safe_text`, `SystemLog.say`, the toast argument |
| **A-2** | A priced order type with a null price reaches Gate 2 labelled `MARKET` | Medium | High for the display; IBKR outcome unproven | CLA-SEC-004 | `proposal_tools.py:90` (`_PRICE` nullable) → `agent.py:_proposal_defect` (no type/price cross-check) → `order_flow.py:1493-1501` (price omitted) → `order_confirm.py:106` (`"MARKET"`) | The model proposes `LMT` with `limit_price: null`; the card says "NO LIMIT PRICE GIVEN"; the operator reaches the last screen, which says `Price: MARKET`, `Total: Market`, `Order Type: LMT`; Touch ID + SEND. Either IBKR rejects (likely, unproven) or accepts something the operator did not read. | The schema cannot express the conditional; `_proposal_defect` enumerates four checks and this is a fifth; the dialog's fallback string was written for MKT | Add the fifth check in `_proposal_defect`: LMT needs `limit_price`, STP needs `stop_price`, STOP_LIMIT needs both — reject whole, never repair. Core-side twin: `_order_rows` must never print `MARKET` when `orderType != "MKT"` | `test_display_corresponds_to_execution.py` (ClaudIA); a `_order_rows` test in the core |
| **A-3** | Both human surfaces round prices to two decimals; the body does not | Medium | High (measured) | CLA-SEC-004 | `order_flow.py:100-102` (`:,.2f`); `order_confirm.py:106,199` via `change_value_text` | A `6E` limit at `1.08455` shows `1.08` on the card and `1.08 USD` in Gate 2; a ZN at `110.171875` shows `110.17`; two different proposals are indistinguishable to the human; the body carries the full value | Formatting chosen for readability; no test on precision | Render the exact decimal (`Decimal(str(v))`, or `repr`-based with thousands separators) on both surfaces | parametrised precision test in each repo |
| **A-4** | Withdrawn fabrications re-enter the model as tool output | Medium | High (executed on a throwaway DB) | CLA-SEC-006 | `conversation_store.py:527-533 search_messages` (no join on `message_withdrawals`, no role filter) → `search_past_conversations` tool result → "guaranteed source" per `_SAFETY_BLOCK` | Turn N: the model narrates a staged cancel; the detector withdraws it. Turn N+k: the user asks "what happened with order 777?"; the model searches; the withdrawn line returns as `[date] assistant: Cancel staged — button's above…` inside a tool result; the model repeats it with the authority of tool output | The withdrawal was implemented on the replay path only | `LEFT JOIN message_withdrawals … WHERE withdrawn_at IS NULL` in `search_messages` (or return the row with an explicit `withdrawn` marker) | `test_history_authority_boundary.py` |
| **A-5** | Ordinary unit tests do real DNS and connect to the live gateway, with the real `.env` loaded | Low | High (measured, two runs) | CLA-SEC-008 | `agent.py:2950 gethostbyname` (4 tests); `panel_app.py:1473 warn_if_session_borrowed` unpatched in 4 `main()` tests → `GET /tickle`; `panel_app.py:86 load_dotenv` at import | With the gateway up, the unit suite tickles the IBKR session; a stray `CLAUDIA_LIVE_SCHEMA_CHECK=1` bills the real key; a future test that accidentally hits a live service passes silently | No socket block; isolation is per-test mocking | pytest-socket with collection-time markers + configure-time `load_dotenv` no-op (the core's design); patch `warn_if_session_borrowed` in the four `main()` tests; 4 named DNS exemptions | `test_no_live_io.py` |
| **A-6** | The stage/cancel/modify click handlers are not re-entrant | Low | High for the code path; the browser race is unproven | CLA-SEC-002 | `panel_order_flow.py:92-95` sets `disabled`, never checks it | Two `clicks` events reaching the server before the disabled patch lands → two `place_order_and_confirm` calls (measured by invoking the watcher twice). Gate 1 and Gate 2 run again each time, so this is not a bypass; it is a docstring ("at most once") the code does not enforce | One-shot enforced client-side only | `if stage_btn.disabled: return` (or a closure flag) as the first line of every handler | `test_display_corresponds_to_execution.py` |

### B. Architectural weaknesses

| ID | Desired invariant | Current behaviour | Why it could silently regress | Suggested enforcement | Cx | Value |
|---|---|---|---|---|---|---|
| B-1 | The model layer never references order execution (ClaudIA side) | True by convention; the "cannot reach execution" test reads the wrong file | a fifth dispatcher branch or an `order_flow` import in `agent.py` stays green | AST test (§ 6 row 1) | low | high |
| B-2 | The three cores are called only from `on_click` closures; no code fires `clicks` | True; unasserted over `panel_app`, `panel_sink`, `execution_listener`, `dashboard_poller`, `scripts/` | a "convenient" call from a fill subscriber or a replay script | AST test (§ 6 row 2) | low | high |
| B-3 | The proposal handler is I/O-free and never writes the input | True; behavioural tests on one payload each | a `setdefault("tif","DAY")` in `_record_proposal` | AST test (§ 6 row 3) | low | medium |
| B-4 | The rendered card is a snapshot of the body | Same dict by reference; no writer today | any future writer (a sink normalisation, a history replay) | `copy.deepcopy` at the top of each `render_*_proposal`, and a render→mutate→click test; keep the identity test on the handler (both can hold) | low | high |
| B-5 | Bare-root FUT: the card names the contract | No `Contract:` line when `conid` is null; resolved at click | contract roll between render and click | resolve at render and pin the conid into the closure, or print the resolution rule; never choose a dateless row | low | medium |
| B-6 | Modify reconciles against the live order at click | Only a description row is read | partial fill / TWS-side edit / model copy error between read and click | click-time compare of `conid, side, quantity, orderType, tif, prices` → refuse with `modify_refused` stage `stale_read` | medium | high |
| B-7 | `conid` visible on the last human surface | never shown | ticker/conid mismatch trades the conid | a `Conid` row in `_order_rows` (core) and on the card; mark a failed name read | low | medium |
| B-8 | Every markup sink is allowlisted and double-escapes | regex over three spellings; ChatStep title takes the model's tool name raw; `Card.title`, `Number.label`, header tooltips raw-but-static | a `pn.Column("…")`, a `ChatStep(default_title=…)`, an HTML formatter | AST allowlist + per-sink canaries (§ 6 row 5); `escape_markup(name)` or refuse unknown names before `tool_step` | medium | high |
| B-9 | Guardrail notices are visibly app-authored | stored as `assistant`, rendered with the model's label | a model-written ⚠️ is indistinguishable | render notices with a distinct `user=` (e.g. "System") while still storing the row; or a code-path-derived badge | low | medium |
| B-10 | Real fills are distinguishable from narrated fills in the transcript | fills leave no message row | an invented "FILLED:" line looks identical | record the fill as a non-model row, or replay `execution_reported` on the operator channel | low | medium |
| B-11 | The forced retry cannot reach a writer | `tool_choice: any` with no restriction | the retry pass calls `delete_cache` or an alert writer | restrict the retry's `tools=` to `_RETRY_SAFE_READS` or fall through to the correction path when `called_tools ⊄` it | low | medium |
| B-12 | Pre-dispatch refusals leave a decision row | `_needs_conid_text`, empty `get_futures`, missing modify ids `return` unrecorded | contradicts CLAUDE.md's guarantee; an operator replay misses them | `_record_refusal` on those three paths, stage named | low | medium |
| B-13 | Drive cannot silently rewrite the system prompt | name-only `files[0]` match; freshness ≠ trust; warning to a collapsed card; the hash baseline lives in the Drive-sourced DB | duplicate-name substitution; a consistent DB+docs replacement produces no warning | pin the Drive file id after first sight; keep the last-seen hash in a local 0600 sidecar (like the Flex verdict) and compare against it; surface the change in chat as well as the card | medium | high |
| B-14 | `GET /` has no side effects until a human is present | full session init per request; report + Drive upload on destroy | phantom sessions from any local process or hostile subresource | gate cleanup and upload on "received at least one user message" | low | medium |
| B-15 | Local-process authority is stated correctly | documented as "the user's machine" | a reader assumes uid isolation | one paragraph in the architecture doc; `BOKEH_ALLOW_WS_ORIGIN`/`BOKEH_SIGN_SESSIONS` guard at startup | low | low |
| B-16 | Error text through one redaction function | `redact_error` unused; 74 raw `{exc}` sites; username at ERROR | a new `requests` call outside the toolkit puts a `?t=` URL in the log | route `{exc}` at the System-log and chat sites through `redact_error`; drop the username from the preflight ERROR | low | medium |
| B-17 | Sidecar child env measured at the spawn seam; cwd is the sidecar dir; config after dotenv | measured at our dict; cwd is the repo; import-time resolution | mcp merging `os.environ`; the health tool naming the user's repo to GitHub | patch the process factory in the test; `cwd=`; lazy port/path resolution | low | medium |
| B-18 | No long-lived process holds `.env` | the keepalive daemon sources it all | — | `grep` one variable in the script; a test that the script contains no `source "$ENV_FILE"` | low | medium |
| B-19 | Sidecar stopped at server exit | stop only on reconnect | a wedged node outliving ClaudIA | `stop()` on the `pn.serve`-returns path | low | low |
| B-20 | The order-book Tabulator is asserted click-free | only `disabled is True` | a click handler on the surface that matters most | extend the handler assertion to every Tabulator in the composed root | low | medium |

### C. Cross-repo contract inconsistencies

| Inconsistency | Truth owner | Fix |
|---|---|---|
| "44 read-only tools" vs 20 non-read-only, 4 ungated account writers | core (registry) | ClaudIA computes the sentence from `capabilities`; a test forbids the hand-written adjective |
| "no password fallback" vs `LAPolicyDeviceOwnerAuthentication` | core | ClaudIA points to `ibkr_core_mcp/SECURITY.md § Two-Gate System`; deletes its own copy |
| `_and_confirm` "gated reply loop" wording vs the one-authorization model | core | pointer |
| `scrape_fallback.py` path | core | pointer to `local_browser.py` |
| The core's structural boundary test covers two core files; ClaudIA's model layer is uncovered | claudia_ui | § 6 rows 1–2 |
| CI checks out core `main` unpinned | claudia_ui | pinned ref + informational lane |
| `_DOCKER_DIR` private import (test-only); three non-`__all__` imports | claudia_ui (declare) / core (export or stabilise) | a declared import list in `test_cross_repo_contract.py`; ask the core to export `change_value_text`, `load_or_refresh_credentials`, `TOOL_DEFINITIONS` in `__all__` |
| Price formatting differs in intent across the two human surfaces (both round) | both | one precision rule, tested in both |
| `outside_rth` null on a **stock** modify drops the attribute silently (measured for futures only) | claudia_ui (warn) / IBKR (unproven semantics) | warning line when the live status reports the attribute and the proposal omits it |
| The venv's editable snapshot lives in the *other* repo's `build/` and was deleted by that repo's work | both (documented trap) | a `test_install_check` case for "pth target absent"; Dev Setup note |

### D. Supply-chain weaknesses

| Layer | Weakness | Class |
|---|---|---|
| Python | no `pip-audit`; 19 ClaudIA-only packages incl. `tornado` unaudited; the local venv was three tornado advisories behind | gap (blocking gate recommended) |
| GitHub Actions | tag-pinned; Dependabot alerts disabled | hardening |
| Node / TradingView | caret ranges; upstream untagged; live lockfile drifted and uncommitted; vendor lockfile is the drifted one; no hash in `ARCHIVE_INFO`; archive script accepts a dirty tree and fetches at archive time | supply-chain weakness, not exploitable per se |
| Vendored source | only `ARCHIVE_INFO` + `package.json` tracked; the fallback exists on one machine | reproducibility gap |
| Runtime binary selection | `node` PATH-resolved behind user-writable dirs; `TRADINGVIEW_MCP_PATH` any `.js`; `shutil.which("tradingview-mcp")` executed directly; `git rev-parse` blind to uncommitted edits | hygiene (same-user boundary) |
| Cross-repo | core checked out at floating `main` | determinism gap |

### E. Defense-in-depth improvements

- Escape the ChatStep title (model-chosen tool name) and refuse unknown names before rendering.
- Show notices and fills with a code-path-derived author, never the model's label.
- Proposal expiry (a TTL from render) and disable earlier cards when a new one renders.
- A `Conid` row on both human surfaces; "(name unverified)" when the contract read fails.
- Route the six System-log `{exc}` sites and the two local-tool error strings through `redact_error`.
- Restrict the forced retry to `_RETRY_SAFE_READS`.
- Startup refusal if `BOKEH_ALLOW_WS_ORIGIN` is set.
- Explicit sidecar `stop()` at server exit; `cwd=` the sidecar dir.
- `tests/test_docs_claims.py`: computed counts (tools, curated, local, security tests) and a ban on "read-only" against the 44-tool set.

### F. Rejected concerns / already mitigated (do not re-raise)

| Concern | Why it is adequately controlled |
|---|---|
| Model reaching a write through the dispatcher | four fixed sinks, a literal dict, no `getattr`; the core's AST test on its model layer |
| A replayed or reloaded proposal button | `panel_app` never reads messages into the feed; `_pending_proposal` cleared per turn; `show_rerun/undo/clear=False` |
| `replay_eval.py` executing a tool or a core | its sink raises on every render and step; no `order_flow` import |
| An env var disabling Gate 1 or Gate 2 | none exists in any of the four core modules |
| Gate 1 failing open when biometrics are unavailable | every unavailable path raises `HumanAuthError` |
| ClaudIA minting or forwarding an `OrderWriteAuthorization` | zero references |
| Proposal handler mutating the input | zero writes; three behavioural tests; the dict is stored by identity |
| Client-side body normalisation | the core copies at entry, shows the copy, strips `_` keys, hashes the canonical body |
| Read-back spoofable by model text | `_read_back` consumes `get_order_status`/`get_live_orders` only |
| History in the system prompt (Hard Rule 4) | `_build_system_prompt` concatenates docs, trade lines and the safety block only; safety block last |
| `role: system` stored in the DB | CHECK constraint refuses it (measured `IntegrityError`) |
| The operator channel forgeable from model output | it is a mid-conversation `system` message the model cannot produce |
| Chat feed, proposal summaries, dashboard panes, ChatStep bodies, Tabulator cells, Pine copy | `safe_markdown` via `renderers`, `escape_markup`, Bokeh `StringFormatter` DOM serialisation, `js_on_click` args (no interpolation) — all measured literal in the browser |
| Cross-origin WebSocket / DNS rebinding | origin allowlist on top of the loopback bind; pinned |
| Guessing the user's live Bokeh session id | 261 bits, not logged, not stored |
| Hostile page reading live account data cross-origin | no CORS headers on `DocHandler`; data only over the origin-checked WS |
| Secrets in the session log, the DB, the reports | every shape 0 (counts measured) |
| Google token/credential files uploaded to Drive | fixed-name uploads only |
| The sidecar receiving `ANTHROPIC_API_KEY` or tokens | allowlisted dict; mcp adds only non-secret defaults |
| Any `shell=True` / `os.system` in either package | none |
| `?theme=` injection | whitelisted against `THEMES` |
| The Flex token in a tool error | every Flex call is inside `toolkit.execute` → `_safe_error` |
| The screenshot blobs in history | accepted residual, user decision 2026-07-25 |
| The five Flex coverage gaps | genuine inactivity, per memory — not a security matter |

---

## 9. Phase 9 — Prioritized implementation plan

| Priority | Finding | Risk | Effort | Proposed control | Test |
|---|---|---|---|---|---|
| **P0** | A-1 System log / toasts execute markup; inverted tests | Medium | S | double-escaping `safe_text`; escaping toast helper; flip the two tests | `test_ui_rendering_boundary.py` canaries |
| **P0** | A-2 null price on a priced order type reaches Gate 2 as MARKET | Medium | S (ClaudIA) + S (core) | fifth `_proposal_defect` check; core `_order_rows` never prints MARKET for non-MKT | precision/null tests both repos |
| **P0** | A-3 two-decimal display vs full-precision body | Medium | S (both) | exact decimal on card and dialog | parametrised precision tests both repos |
| **P0** | § 0 venv cannot import the core | n/a | XS | re-run Dev Setup step 3; add the "pth target absent" case to `install_check` | `test_install_check.py` |
| **P1** | B-1, B-2, B-3 model-layer / click-boundary / proposal-handler structure | High consequence, latent | S | three AST tests with guard-on-the-guard snippets | § 6 rows 1–3 |
| **P1** | B-4 card snapshot; A-6 re-entrancy | Medium | XS | `deepcopy` at render; `if disabled: return` | render→mutate→click; double-click |
| **P1** | A-4 withdrawn text via FTS | Medium | XS | withdrawal join in `search_messages` | `test_history_authority_boundary.py` |
| **P1** | A-5 test isolation and `.env` in tests | Low today, class-level | S | pytest-socket markers at collection; configure-time `load_dotenv` no-op; patch the four `main()` tests; 4 DNS exemptions | `test_no_live_io.py` |
| **P1** | D Python: no `pip-audit` | Medium | S | blocking job mirroring the core's, `.[dev]` + `../ibkr_core_mcp[scraper]`, shared ignore line, weekly cron | CI |
| **P1** | C stale/overstated security claims (§ 4 rows 1, 3, 5, 12, 17, 18, 22–26, 30) | Medium (agents copy them) | M | SECURITY.md rewrite as a pointer-based inventory; computed counts in `test_docs_claims.py` | docs gate |
| **P2** | B-6 modify click-time reconciliation; B-12 unrecorded refusals; B-7 conid row | Medium | M | compare and refuse; `_record_refusal`; rows | `test_display_corresponds_to_execution.py`; refusal-row tests |
| **P2** | B-8 markup allowlist (AST) + ChatStep title | Medium | M | allowlist keyed `(file, callee)`; `escape_markup(name)` | § 6 row 5 |
| **P2** | B-13 Drive substitution and self-referential hash baseline | Medium | M | pinned file id; local 0600 baseline sidecar; chat-visible warning | Drive tests (red first) |
| **P2** | B-17, B-18, B-19 sidecar seam / keepalive / stop | Low–Medium | S | measure the merged env; `cwd=`; lazy config; one-variable keepalive; `stop()` at exit | `test_sidecar_environment.py` |
| **P2** | D gitleaks with three custom rules | Low | S | job + `.gitleaks.toml`; size the account-id rule locally first | CI |
| **P2** | D pinned core ref + informational `main` lane; `test_cross_repo_contract.py` | Medium (determinism) | S | `ibkr_core_mcp.ref`; declared import list; registry-computed doc sentence | CI + test |
| **P3** | B-9, B-10, B-11 notice author, fill row, retry restriction | Low–Medium | S | as listed | `test_action_claim_evidence.py` |
| **P3** | B-14, B-15 phantom sessions; boundary statement; Bokeh env guard | Low | S | cleanup gated on a user message; startup refusal | `test_server_exposure.py` |
| **P3** | D Node provenance: tracked lockfile, hashes in `ARCHIVE_INFO`, dirty-tree refusal, runtime TOFU check | Low | M | as § 5.6 | `test_tradingview_provenance.py` |
| **P3** | D SHA-pin actions; enable Dependabot alerts; B-20 order-book Tabulator assertion; B-16 `redact_error` at the six sites | Low | S | as listed | existing suites |

Sequencing note for the implementation phase: P0 first as three small commits each with its
red-then-green test; then the three structural tests (they pin the boundary before anything
else moves); then isolation and CI; then the medium items. Every item touching the core is a
separate commit in that repo, one repo at a time, with the strict editable reinstall between
(user rule 2026-09-10).

---

## 10. Phase 10 — Proposed CI design

| # | Gate | Unique failure class | Mode | Runtime | FP risk | Overlap |
|---|---|---|---|---|---|---|
| 1 | ruff check (incl. `S`) | known-bad calls | blocking | s | low | — |
| 2 | ruff format | — | blocking | s | none | — |
| 3 | mypy strict (`claudia/` + `tests/`) | typed-wrong | blocking | ~1 min | low | — |
| 4 | pytest unit (`-m "not integration and not live_api"`, under pytest-socket) | behaviour; **now also**: any live I/O, any secret in the process | blocking | ~40 s | low; 4 named DNS exemptions | — |
| 5 | `tests/security/` (part of 4, also `-m security` for a 10 s run) | the twelve invariants, structural and canary | blocking | ~10 s | medium at first for the markup allowlist | none with the core — different files |
| 6 | pip-audit (requirements mode, `.[dev]` + `../ibkr_core_mcp[scraper]`, `--strict`, ignore file, weekly cron) | a vulnerable *resolved* dependency in the 19 ClaudIA-only packages | blocking (no-fix findings via the ignore file) | 1–2 min | low | partial with the core; not redundant (tornado) |
| 7 | gitleaks (full history on push/PR; three custom rules) | a committed secret or account id | blocking | <1 min | medium for the account-id rule until allowlisted | none |
| 8 | cross-repo contract test (inside 5) + **pinned core ref** for lanes 1–7 | the core changing a symbol, an entry point, or the registry under ClaudIA; non-reproducible greens | blocking | 0 | low | — |
| 9 | core-`main` compatibility lane (same steps, `continue-on-error`) | forward drift, dated | informational | one extra matrix run | none by construction | — |
| 10 | Node/TradingView validation (`ARCHIVE_INFO` parses and hashes present; optional `npm audit --omit=dev` against the tracked lockfile) | provenance record missing; a known-vulnerable pinned Node dep | informational (vendor tree absent on CI; the hash test skips) | s | low | none |

Not recommended: exact parity with the core's ten invariants (four of them — sandbox, SSRF layer
2, transport, endpoint templates — are the core's to hold); Semgrep (every class here is a
30-line `ast` test); a second CodeQL configuration (blind to tool inputs, already on).

---

## 11. ClaudIA Security Constitution

The properties CI should make structurally impossible to violate silently.

| ID | Statement | Owner | Enforcement | Regression test | Depends on a core contract |
|---|---|---|---|---|---|
| **CLA-SEC-001** | The model-facing layer (`agent.py`, `proposal_tools.py`, `panel_sink.py`, `execution_listener.py`, `scripts/`) never references an order-write name, `IBKRClient`, `_post`, `_session`, or an authorization, and never imports `order_flow`/`panel_order_flow`; the dispatcher has exactly four sinks | claudia_ui | AST over named modules + dispatcher callee set | `test_model_cannot_execute_orders.py` | yes — core invariant 1 (the four gated methods and their AST test) |
| **CLA-SEC-002** | The three execution cores are called only from `on_click` closures in `panel_order_flow.py`; no code in `claudia/` or `scripts/` fires `clicks` or triggers the param; the three gated client calls exist only in `order_flow.py`; ClaudIA never mints or forwards an authorization; each handler runs at most once | claudia_ui | AST + behavioural double-click | `test_human_click_execution_boundary.py` | yes — Gate 1/2 inside the client |
| **CLA-SEC-003** | `_record_proposal` and `_proposal_defect` perform no I/O, touch no attribute but the pending proposal and the turn's tool set, and never write to the input; rejection never repairs | claudia_ui | AST + the existing behavioural trio | `test_proposal_tools_are_side_effect_free.py` | no |
| **CLA-SEC-004** | What the human reads is what is sent: the card is a snapshot equal to the body; prices render exactly; a priced order type without its price is rejected at the proposal layer; a bare-root future names its resolved contract; the dialog never labels a non-MKT order `MARKET` | both | behavioural | `test_display_corresponds_to_execution.py` + a core `_order_rows` test | yes — `_order_rows`/`change_value_text` are the core's |
| **CLA-SEC-005** | Every markup-rendering construction site is on an explicit allowlist, and every allowed site yields a double-escaped model for untrusted input (Str, toasts, ChatStep title and body, message author included) | claudia_ui | AST allowlist + per-sink canaries | `test_ui_rendering_boundary.py` | no |
| **CLA-SEC-006** | Stored content replays at the same or lower role; a withdrawn row re-enters through no channel (replay or search); `system=` is built only from the documents, the trade lines, the version note and the safety block, with the safety block last | claudia_ui | behavioural + AST over `_build_system_prompt` | `test_history_authority_boundary.py` | no |
| **CLA-SEC-007** | No claim detector reads model text as evidence — evidence is the API's `tool_use` blocks and the handler-set pending proposal; the forced retry cannot reach a writer; every withdrawal and refusal is persisted | claudia_ui | mutation tests + AST over the detectors' inputs | `test_action_claim_evidence.py` | no |
| **CLA-SEC-008** | Unit tests open no sockets, resolve no names outside a named exemption list, and see no real secret; importing any `claudia` module leaves the environment unchanged | claudia_ui | pytest-socket markers at collection; configure-time `load_dotenv` no-op | `test_no_live_io.py` | no (mirrors core invariant 7) |
| **CLA-SEC-009** | Processes are spawned only from `tradingview.py` and `gateway_launch.py`, list-form, never through a shell; the sidecar's **merged** child environment is exactly the allowlist plus mcp's defaults; no script or daemon sources `.env` wholesale | claudia_ui | AST + spawn-seam measurement + script grep | `test_sidecar_environment.py` | no (mirrors core invariant 8) |
| **CLA-SEC-010** | The selected TradingView sidecar copy is verifiable against `ARCHIVE_INFO` (source and lockfile hashes); the archive is reproducible from a clean upstream commit | claudia_ui | hash comparison (skips when the tree is absent); runtime TOFU log | `test_tradingview_provenance.py` | no |
| **CLA-SEC-011** | The server binds loopback with an exact origin allowlist that no environment variable can widen; a session with no human message has no side effects | claudia_ui | kwargs pin + startup guard + behavioural | `test_server_exposure.py` | no |
| **CLA-SEC-012** | ClaudIA calls exactly `place_order_and_confirm`, `cancel_order`, `modify_order_and_confirm`; imports only declared core symbols; states tool capabilities only as computed from the core's registry; CI runs against a pinned core revision | both | AST + registry import + docs grep + workflow ref | `test_cross_repo_contract.py` | yes — this **is** the contract |

---

## 12. Cross-repo security contract

| Contract | Owner | ClaudIA assumption | Machine checked? | Drift risk |
|---|---|---|---|---|
| Gate 1 + Gate 2 run inside each of the four write methods before the first network call | core | assumed, correctly | core AST test; ClaudIA: none | low; ClaudIA's doc restates policy wrongly (fallback) |
| Gate 1 policy `LAPolicyDeviceOwnerAuthentication` (password fallback by design) | core | "no password fallback" (stale) | no | already drifted |
| One Touch ID per write; `OrderWriteAuthorization` body-bound, frame-local; every reply dialogued | core | "gated reply loop" | core tests | low |
| The three entry points and their keyword arguments (`reply_log=`, `order_details=`) | core (API) | hard-coded in `order_flow.py` | no | medium |
| The client copies the body, strips `_` keys, shows the copy, POSTs the copy | core | display keys are display-only | core test | low |
| `_order_rows` draws from the body; formatting via `change_value_text` | core | two-decimal display acceptable | no | the A-2/A-3 gap |
| `ClaudeToolkit.execute` dispatch dict has no order write; `ORDER_EXECUTION` unspellable | core | "44 read-only" (false) | core tests; ClaudIA: none | ClaudIA's adjective drifts with every registry change |
| The capability registry (`capabilities` on every definition) | core | not consumed | no | ClaudIA is blind to a new writer |
| `_safe_error` for every toolkit exception; `redact_error` available | core | relied on for Flex `?t=`; `redact_error` unused | core test | ClaudIA's own error sites unguarded |
| `_validate_public_url` layer 1 + layer 2 for browser/httpx fetches | core (tools) / ClaudIA (`fetch_web_page` has layer 1 only, per-hop) | ClaudIA duplicates layer 1 | ClaudIA test | two copies of one guard |
| `IBKRWebSocket` / `TradeExecution` shapes | core | fills parsed from them | core tests | medium |
| `GatewayManager` construction and login-page ownership | core / ClaudIA | one owner (`gateway_session`) | ClaudIA AST test | low |
| Public API surface (`__all__`) | core | three non-`__all__` names imported | no | medium |
| Editable-install snapshot in the core's `build/` | both | present | `install_check` (module set only) | the § 0 incident |
| Core revision CI runs against | claudia_ui | `main` | no | non-reproducible greens |

---

## 13. Recommended next action

Human review of the following before any implementation:

1. **The three P0s** (A-1 markup execution in the System log/toasts with the inverted tests;
   A-2 null-price → `MARKET` on Gate 2; A-3 two-decimal display) — each is a small change with a
   red-first test; A-2 and A-3 each need a **core-side** commit too and are therefore two-repo
   changes to be sequenced one repo at a time.
2. **The design decisions this audit forces** (they belong in the new architecture doc's
   decision log, and the choice is the reviewer's, not the implementer's):
   - proposal dict **snapshot at render** while keeping the handler's identity test;
   - exact price display everywhere vs a per-instrument tick format;
   - rejecting a priced type without its price at the proposal layer (immutability says reject,
     never repair — this is consistent);
   - whether `search_past_conversations` hides withdrawn rows or returns them with a marker;
   - pytest-socket adoption with four named DNS exemptions;
   - a pinned core ref for CI (and who bumps it);
   - `SECURITY.md` rewritten as a pointer-based inventory with the twelve invariants; the stale
     claims in § 4 corrected only once the corresponding control exists (implementation rule 11).
3. **Scope confirmations:** B-13 (Drive substitution and the self-referential hash baseline)
   and B-6 (modify click-time reconciliation) are the two medium-effort items with the highest
   value; confirm they are in scope for the implementation phase or deferred with a date.
4. **Environment:** the venv repair (Dev Setup step 3) is a prerequisite for running anything;
   it is one command and out of scope for this phase by instruction.

Nothing in the repository was changed by this audit.
