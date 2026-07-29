# ClaudIA UI — Developer Guide

ClaudIA is a Panel-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, an external candlestick chart pane, and human-confirmed order staging. (It was migrated from Chainlit to Panel — see `docs/plans/2026-07-22-panel-migration.md`.)

---

## Architecture

```
Panel UI (localhost:8001 — native pn.serve Tornado, no FastAPI/uvicorn)
    ↓
claudia/panel_app.py        — pn.serve entry: session lifecycle, status dots, startup action buttons
claudia/panel_sink.py       — PanelMessageSink: agent output → pn.chat.ChatInterface (MessageSink protocol)
claudia/panel_order_flow.py — order/cancel/modify proposal buttons → order_flow.py cores
claudia/panel_pinescript.py — ```pine copy (real client-side clipboard) / inject buttons
claudia/panel_chart.py      — external Bokeh candlestick chart pane (STK, cache-backed)
claudia/agent.py            — Anthropic SDK streaming loop, tool routing, prompt caching (UI-agnostic via MessageSink)
claudia/proposal_tools.py   — strict-schema propose_order/propose_cancel/propose_modify declarations (no execution)
claudia/message_sink.py     — MessageSink / ToolStepHandle protocols (the UI-decoupling seam)
claudia/order_flow.py       — framework-agnostic order-execution cores → ibkr_core_mcp biometric gates
claudia/opening_status.py   — UI-free opening-status builders
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, doc_versions
claudia/execution_listener.py — WebSocket execution/P&L capture, live-ledger fallback
claudia/gdrive_sync.py      — GDriveSync: download claudia.db at start / upload at stop
claudia/session_reporter.py — auto-generated Markdown session report (tool calls, decisions)
claudia/status.py           — ConnectivityChecker: IBKR/GDrive/TV polling, TCP health
claudia/tradingview.py      — tradingview-mcp sidecar + CDP health + TradingViewBridge
    ↓                               ↓
ibkr_core_mcp               tradingview-mcp (Node.js, stdio)
(local editable install)            ↓
    ↓                       TradingView Desktop (CDP, localhost:9222)
IBKR Client Portal Gateway
(Docker, localhost:5055)
```

`ibkr_core_mcp` is a direct Python import, not an MCP server — `ClaudeToolkit` tools drop
straight into the Anthropic SDK `tools=` parameter. TradingView tools come from a Node.js
sidecar. Full tool catalog: `ibkr_core_mcp/docs/tools-reference.md` (43 tools).

---

## Dev Setup

```bash
# 1. Clone and enter the project
cd /Users/steph/Claude_Projects/claudia_ui

# 2. Create venv
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Install claudia_ui + ibkr_core_mcp (editable)
pip install -e ".[dev]"
pip install -e "../ibkr_core_mcp" --config-settings editable_mode=strict
# strict mode required for mypy — the default "lazy" editable install registers a
# meta-path finder mypy's static import resolution can't see (confirmed 2026-07-21:
# mypy reports "Cannot find implementation or library stub for module named
# ibkr_core_mcp" without it, despite the package shipping py.typed and importing
# fine at runtime). Strict mode still uses real symlinks to the source — genuinely
# editable, not a frozen copy.

# 4. Copy and fill in env vars
cp .env.example .env
# Edit .env — minimum required: ANTHROPIC_API_KEY. Full var reference: @docs/env-vars-reference.md

# 5. Personal documents (git-ignored; define ClaudIA's persona + trading rules)
# If GOOGLE_DRIVE_FOLDER_ID is set, both download from Drive automatically at session
# start — nothing to create. Otherwise create them by hand:
touch docs/context.md docs/principles.md   # then write persona / trading rules
chmod 600 docs/context.md docs/principles.md
# Loading/versioning mechanics: docs/context-loading-reference.md

# 6. TradingView sidecar (optional — one-time install)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version to vendor/
# Full sidecar details, curated tool list, recovery: @docs/tradingview-reference.md

# 7. Run ClaudIA
./start-claudia.sh            # recommended: IBKR gateway + ClaudIA
# or:
python -m claudia.panel_app   # ClaudIA only (in-chat "Start IBKR Gateway" button available)
# → Open http://localhost:8001
```

## Testing

```bash
pytest        # full suite — all unit, no IBKR gateway needed (757 tests as of 2026-07-28)
ruff check claudia/ tests/ && mypy claudia/   # lint + type gates, both must be clean

# Opt-in only — bills real Anthropic API calls, skipped by default (3 tests):
CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest -m live_api
```

**The `live_api` marker exists because local validation cannot prove API acceptance.** During
the 2026-07-27 guardrail work, three separate defects passed a docs read *and* a green suite
and would each have returned a 400 on every request: `exclusiveMinimum` and
`additionalProperties: true` in the tool schemas, and a `role: "system"` message placed after
an assistant turn. `jsonschema` validates that our schema is valid JSON Schema; it says
nothing about what the endpoint accepts. **Probe the live API before adding any JSON Schema
keyword or changing a message-role placement** — the published support list has been wrong in
both directions (`minLength` is accepted despite being documented as unsupported). Evidence
table: `claudia/proposal_tools.py` module docstring.

No test carries the `integration` marker (registered in `pyproject.toml`, meaning "live IBKR
gateway") — live IBKR verification is done manually and recorded in
`docs/project-status.md` § Live Test Log. ibkr_core_mcp's own integration suite lives in
that repo.

---

## Conventions

- **API Docs First**: never assume endpoint behavior, error codes, or field names from
  memory. Always `WebFetch` the official doc before writing any error message, fix, or
  diagnosis. Cite the source URL in the error string and commit message. This rule exists
  because two production bugs went undetected for months and were caught instantly once
  docs were checked. Full source table: `docs/api-reference.md`
- **All plans live in `docs/plans/`, and the directory is git-ignored** (dated
  `YYYY-MM-DD-<topic>.md` filenames — designs, implementation plans, and workflow-executed
  plans alike). Plans are personal working documents: kept local + Google Drive, never
  committed (user rule 2026-07-24). Never create a `docs/superpowers/` directory — this
  overrides any skill's default plan location. (The 2026-07-14 docs reorg dissolved
  `docs/superpowers/` into `docs/plans/`; a skill default recreated it on 2026-07-22 and it
  was re-dissolved on 2026-07-24, same day the directory went git-ignored.) `docs/plans/...`
  paths in tracked docs are pointers into the local archive, not repo files.
- `context.md` / `principles.md` define ClaudIA's persona and trading rules. Hot-reloaded
  mid-session, never commit either file. Loading/versioning mechanics: `docs/context-loading-reference.md`
- Prompt caching uses 3 breakpoints (tools → system → messages). Mechanics and live-verified
  numbers: `docs/context-loading-reference.md`. Design rationale and the three-round
  consistency review: `docs/plans/2026-07-03-prompt-caching-upgrade.md`

---

## Hard Rules for Developers

These rules must never be violated when extending ClaudIA:

1. **Never add a tool that calls `place_order`, `modify_order`, `cancel_order`, or
   `reply_order`.** *Proposing* is a tool call (`propose_order` / `propose_cancel` /
   `propose_modify` — schema-validated, local handlers, no IBKR reachability). *Staging* is
   a UI-layer action triggered by a physical button click. The rule forbids the second as an
   LLM capability, not the first.
2. **Never log or expose `ANTHROPIC_API_KEY`** in UI output, logs, or error messages.
3. **Never modify the hardcoded safety block** in `claudia/agent.py` to weaken constraints.
4. **Never inject conversation history directly into the system prompt.** History must be
   added as `role: user/assistant` message objects to prevent prompt injection.
5. **ibkr_core_mcp is read-only from claudia_ui's perspective.** Never bypass `ClaudeToolkit`
   to call `IBKRClient` directly from within an LLM tool handler.

---

## Order Staging (safety-critical — summary only, full spec: `docs/order-api-reference.md`)

ClaudIA **cannot** place, modify, or cancel orders autonomously:
1. ClaudIA calls `propose_order` (or `propose_cancel` / `propose_modify`) — a `strict: true`
   tool declared in `claudia/proposal_tools.py`. It reaches nothing: the handler records the
   proposal and returns a `tool_result`. There is no text format for a proposal.
2. `agent.py` hands the recorded `tool_use.input` to the `MessageSink`
   (`send_order_proposal`); `PanelMessageSink` routes it to
   `panel_order_flow.render_order_proposal()` → a Panel message with a
   **"Stage this order"** button.
3. Click → `panel_order_flow`'s handler → `order_flow._execute_staged_order_core()` →
   **Gate 1** (Touch ID) → **Gate 2** (AppKit dialog, green/red banner by side,
   **SEND TO IBKR** button, 60s auto-cancel, Return key disabled).
4. `IBKRClient.place_order()` fires only after both gates pass.

- **Order parameters are immutable**: ClaudIA must use the user's exact values (symbol,
  action, quantity, price, order type, TIF). No rounding or "helpful" adjustment. A risky
  parameter gets a text warning, never a silent change — changing a parameter requires
  explicit user approval in a follow-up message. Enforced in `claudia/agent.py` system
  prompt and in memory (`feedback-order-parameter-immutability.md`).
- Modify requests require the **full original order**, not a diff (IBKR API requirement).
  `propose_modify` carries the replacement order in its top-level fields plus a `changes`
  array of `{field, previous_value}` objects, used only to render the before/after diff.
- FUT/FOP require `conid` pre-resolved via `get_option_chain`/`get_futures` — no fallback
  symbol-based resolution for modify/cancel.
- `strict: true` enforces types, `enum`s, required keys and closed objects at the API
  boundary. Four guarantees it cannot express — positive quantity, non-blank `symbol`,
  non-blank `order_id`, no duplicate `changes` entries — are checked by `_proposal_defect()`
  in `claudia/agent.py`. A defective proposal is **rejected whole and never repaired**: the
  model gets an honest `tool_result` saying no button was created.

---

## ibkr_core_mcp Dependency

Local editable install: see Dev Setup step 3 above for the exact command (strict editable
mode required for `mypy` to resolve it) — re-run after ibkr_core_mcp adds new tools. No
Panel restart needed for tool definition changes; restart required for Python module
changes. Full tool catalog (40 core + 3 optional web-scraper = 43 total, verified against
`ClaudeToolkit.tools` 2026-07-28): `ibkr_core_mcp/docs/tools-reference.md` — check there
before adding/debugging a tool. Recent additions log: `ibkr_core_mcp/CHANGELOG.md`.

`self._all_tools` in `claudia/agent.py` is **not** just that catalog: it is the toolkit's 43,
plus the TradingView extras when the sidecar is up (16 curated), plus 5 local utility tools
(`_LOCAL_TOOLS`) and the 3 `PROPOSAL_TOOLS`, both declared in claudia_ui. The proposal tools
are appended last so the tools cache breakpoint on the final entry stays stable.

No extras (e.g. `[server]`) are needed for the install above. `websockets` — the sole
runtime dependency of `IBKRWebSocket`, which `claudia/execution_listener.py` uses
unconditionally for live P&L/execution tracking — is a base dependency of ibkr_core_mcp, not
gated behind an extra. (It briefly wasn't: a bare install used to leave `websockets` missing
and `ExecutionListener` would silently retry-loop forever on `ModuleNotFoundError`. Fixed
ibkr_core_mcp-side by moving `websockets` out of `[server]` into base `dependencies`, since
`IBKRWebSocket`/`AlertManager` are core public API, not server-only.)

## Pointers

Plain file references below, not `@import`s — read on demand via normal file tools, not
loaded into every session's context automatically. Compliant with the official Claude Code
memory docs (verified 2026-07-10, https://code.claude.com/docs/en/memory): a bare `@path` is
a real import ("expanded and loaded into context at launch"); backtick-wrapping keeps it a
literal path instead. See `docs/plans/2026-07-10-claude-md-delink-imports.md` for
the fix that established this (75,480 → 2,910 tokens/session).

- Connectivity (IBKR/GDrive/TV status dots, check logic, reconnection flows): `docs/connectivity.md`
- Panel implementation (serving model, session lifecycle, MessageSink seam, widget gotchas,
  headless button testing): `docs/panel/panel-reference.md`
- Panel UI design & styling (no-styling baseline, shadow-DOM constraint, scraped styling
  surface, proposed restyle direction): `docs/panel/ui-design-reference.md`
- Panel data surfaces — graphs/tables/indicators not yet built (Tabulator, Trend/Number,
  ECharts, `pn.extension()` gate, side windows, stream/patch + connectivity, chatbot-piloted
  vs. independent sketch): `docs/panel/data-surfaces-reference.md`
- Panel folder hub (both references + dated research + smoke screenshots): `docs/panel/README.md`
- Startup flow, phase by phase (diagnose startup failures): `docs/startup-flow.md`
- Trade data sync (Flex vs live API, integrity checks): `docs/flex-query-setup.md` and `docs/trading-data-reference.md`
- Market calendar (20 exchanges, futures schedules): `docs/market-calendar-reference.md`
- GDrive sync (folder layout, error handling): `docs/gdrive-sync-reference.md`
- TradingView integration (sidecar, curated tools, recovery): `docs/tradingview-reference.md` and `docs/tradingview-mcp-recovery.md`
- Environment variables (full reference): `docs/env-vars-reference.md`
- Conversation memory schema: `docs/conversation-memory-reference.md`
- API source-of-truth URLs (IBKR, Anthropic, Drive, Panel, libraries): `docs/api-reference.md`
- Known gaps, live test log, project status: `docs/project-status.md`
- Full documentation catalog (every doc in `docs/`, categorized): `docs/README.md`
