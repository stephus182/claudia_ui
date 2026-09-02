# ClaudIA UI — Developer Guide

ClaudIA is a Panel-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, an external candlestick chart pane, a live account dashboard (KPI strip · positions · working orders · realised P&L), and human-confirmed order staging. (It was migrated from Chainlit to Panel — see `docs/plans/2026-07-22-panel-migration.md`.)

---

## Architecture

```
Panel UI (localhost:8001 — native pn.serve Tornado, no FastAPI/uvicorn)
    ↓
claudia/panel_app.py        — pn.serve entry: session lifecycle, status dots, startup action buttons
claudia/panel_sink.py       — PanelMessageSink: agent output → pn.chat.ChatInterface (MessageSink protocol)
claudia/panel_order_flow.py — order/cancel/modify proposal buttons → order_flow.py cores
claudia/panel_pinescript.py — Pine-fence copy (real clipboard) / inject buttons
                              (matches pine | pinescript | pine-script, any case)
claudia/panel_chart.py      — external HoloViews candlestick chart pane (STK, cache-backed)
claudia/panel_theme.py      — session theme (CLAUDIA_THEME + ?theme=), user label, ClaudIA's avatar
claudia/dashboard_data.py   — live dashboard: pure data (ledger, positions, realised windows), no panel import
claudia/dashboard_poller.py — live dashboard: process-wide 15s poller caching one DashboardSnapshot
claudia/panel_dashboard.py  — live dashboard: KPI strip + Tabs(Chart/Positions/Orders/P&L), no IBKR and no SQL
claudia/agent.py            — Anthropic SDK streaming loop, tool routing, prompt caching (UI-agnostic via MessageSink)
claudia/proposal_tools.py   — strict-schema propose_order/propose_cancel/propose_modify declarations (no execution)
claudia/message_sink.py     — MessageSink / ToolStepHandle protocols (the UI-decoupling seam)
claudia/order_flow.py       — framework-agnostic order-execution cores → ibkr_core_mcp biometric gates
claudia/opening_status.py   — UI-free opening-status builders (session state + trade line; no account figures)
claudia/flex_sync.py        — session-start dataset validation + the "did this pull change anything" gate
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, doc_versions
claudia/execution_listener.py — WebSocket execution/P&L capture, live-ledger fallback
claudia/gdrive_sync.py      — GDriveSync: download claudia.db at start / upload at stop
claudia/session_reporter.py — auto-generated Markdown session report (tool calls, decisions)
claudia/status.py           — ConnectivityChecker: IBKR/GDrive/TV polling, TCP health
claudia/tradingview.py      — tradingview-mcp sidecar + CDP health + TradingViewBridge
                              (execute() post-processes every result before the model sees it)
    ↓                               ↓
ibkr_core_mcp               tradingview-mcp (Node.js, stdio)
(local editable install)            ↓
    ↓                       TradingView Desktop (CDP, localhost:9222)
IBKR Client Portal Gateway
(Docker, localhost:5055)
```

`ibkr_core_mcp` is a direct Python import, not an MCP server — `ClaudeToolkit` tools drop
straight into the Anthropic SDK `tools=` parameter. TradingView tools come from a Node.js
sidecar. Full tool catalog: `ibkr_core_mcp/docs/tools-reference.md` (44 tools).

---

## Dev Setup

```bash
# 1. Clone and enter the project
cd /Users/steph/Claude_Projects/claudia_ui

# 2. Create venv
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Install claudia_ui + ibkr_core_mcp (editable)
pip install -e ".[dev]"
pip install -e "../ibkr_core_mcp[scraper]" --config-settings editable_mode=strict
# [scraper] is NOT optional in practice — it is what installs crawl4ai, and without it all
# four web tools (fetch_page, crawl_site, search_site, firecrawl_search) are dark. Every
# scraper import is lazy, so ClaudIA starts perfectly and each tool fails only when the
# model calls it. This exact omission already shipped once: it was found in the environment
# on 2026-07-28, fixed there, and left in these instructions, so every clean setup since
# would have reintroduced it. Corrected 2026-07-30. Fixing the environment is not fixing
# the bug — the instructions are what the next install actually runs.
#
# strict mode required for mypy — the default "lazy" editable install registers a
# meta-path finder mypy's static import resolution can't see. Re-confirmed 2026-07-30
# against mypy 2.3.0: a non-strict install produces 14 "Cannot find implementation or
# library stub for module named ibkr_core_mcp" errors, despite the package shipping
# py.typed and importing fine at runtime. So strict is not optional.
#
# THE COST, and it has bitten three times: strict mode snapshots a symlink farm under
# build/__editable__…/ of the modules that existed AT INSTALL TIME. Add, rename or
# delete a module in ibkr_core_mcp and this project keeps resolving the old set.
# Because every scraper import is lazy, ClaudIA starts perfectly and fails only when
# the affected tool is called. RE-RUN THIS COMMAND after any module is added, renamed
# or removed — not just after a new tool is added.
#
# Guarded since 2026-07-30: claudia/install_check.py compares the snapshot against the
# real source tree. panel_app logs a loud ERROR naming the modules and this command at
# startup, and tests/test_install_check.py fails in the ordinary pytest run. You should
# never have to diagnose this from a bare ModuleNotFoundError again.

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
source .venv/bin/activate   # every command below needs it — a bare `pytest` resolves to
                            # system Python and dies on `ModuleNotFoundError: panel`
pytest        # full suite — all unit, no IBKR gateway needed (1,613 tests as of 2026-09-02)
ruff check claudia/ tests/ && mypy claudia/   # lint + type gates, both must be clean

# Opt-in only — bills real Anthropic API calls, skipped by default (4 tests):
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
- **Placement resolves no symbols. Every sec_type except FUT requires a `conid` already in
  the proposal** (`order_flow._needs_conid_text`, 2026-08-05). The order path used to fall
  back to `search_contract` → `contracts[0]`, which is `/iserver/secdef/search`: no `isUS`,
  no currency, and an undocumented result order, so `contracts[0]` for IGV is the *Mexican*
  listing — the defect ibkr_core_mcp had already removed from every read path. FUT is the
  one exception, resolved by front month via `get_futures`, unambiguous by construction.
  This costs nothing: the model gets its conid from `get_market_snapshot`/`preview_order`,
  which route through the authoritative resolver, and **every real placement proposal since
  2026-07-10 already carried one** (measured over the full order history 2026-08-05). Do not
  "restore" symbol resolution here — re-implementing it would put a second, drifting
  definition next to the authoritative one.
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
changes. Full tool catalog (40 core + 4 optional web-scraper = 44 total, verified against
`TOOL_DEFINITIONS` 2026-07-30): `ibkr_core_mcp/docs/tools-reference.md` — check there
before adding/debugging a tool. Recent additions log: `ibkr_core_mcp/CHANGELOG.md`.

`self._all_tools` in `claudia/agent.py` is **not** just that catalog: it is the toolkit's 44,
plus the TradingView extras when the sidecar is up (17 curated), plus 5 local utility tools
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

- Connectivity (IBKR/GDrive/TV status dots, check logic, reconnection flows): `docs/connectivity.md`.
  **Before opening the IBKR login page — from a script, a button, or by hand — run
  `python -m claudia.gateway_preflight`** (read-only: two GETs, never a write). Only one
  brokerage session exists per username across Client Portal, TWS and IBKR Mobile, so a
  needless re-login is what escalates into the IB Key challenge/response, and some login
  failures *cannot* be fixed by retrying. The case that cost days on 2026-08-05: the gateway
  held an SSO session issued to **IBKR Mobile**, so it could not authenticate as itself —
  visible only in `/sso/validate`'s `CLIENT_APP`, while `/tickle` showed `userId` populated,
  `ssoExpires` renewing and `competing` *false*. `POST /logout` could not clear it (three
  ticklers renewed it every 60s); `docker restart` could. Recovery: `./scripts/gateway-reset.sh`,
  which refuses to run against a healthy session. Full diagnosis + verdict table:
  `docs/connectivity.md` § A borrowed session / § Runbook
- Panel implementation (serving model, session lifecycle, MessageSink seam, widget gotchas,
  headless button testing): `docs/panel/panel-reference.md`
- **UI customisation** (what is set and how to change it — theme default + per-tab URL
  override, ClaudIA's avatar, user label, Send-only footer, no reaction icons; the costed menu
  of next easy changes; phase-2 candidates): `docs/panel/ui-customisation-reference.md`.
  The theme is set **per session**, never on `pn.extension()` — a global theme silences the
  `?theme=` override (Panel reads the global slot first). Panel's only built-in theme switch
  (Fast template) is a page reload, i.e. a new ClaudIA session — do not add it as a "toggle".
- Panel UI design & styling (no-styling baseline, shadow-DOM constraint, scraped styling
  surface, proposed restyle direction): `docs/panel/ui-design-reference.md`
- Panel component model (object taxonomy, the real class hierarchy, the Param foundation,
  the four interactivity APIs and how they rank, and the four routes to building a component
  of our own): `docs/panel/component-model-reference.md`
- Panel data surfaces — Tabulator/Number/ECharts, the `pn.extension()` gate, side windows,
  stream/patch + connectivity, and 27 measured gotchas (16 onwards found live against the
  account): `docs/panel/data-surfaces-reference.md`
- **Live dashboard** (KPI strip · Positions · P&L, shipped 2026-08-04): the three modules in
  the diagram above. Read `claudia/dashboard_data.py`'s module docstring first — it carries the
  realised-P&L rule, the T+1 gap, and the source table for every figure. Two invariants that
  must not be relaxed: a failed poll republishes the previous `as_of` (so staleness stays
  visible instead of being masked by a fresh timestamp), and **every** `Tabulator` —
  positions and the working-order book — is `disabled=True` with **no** click/edit handler
  bound (Hard Rule 1, asserted over all of them in tests, not over a fixed one). The order
  book is where that matters most: it is the one surface where a click could plausibly be
  wired to "cancel this", and cancelling stays behind `propose_cancel` and both gates.
  `DashboardSnapshot.orders` is `tuple | None` because `()` ("nothing resting") and a
  failed lookup are opposite claims — never render an empty book for an unknown one.
  **The two realised figures on that screen are different quantities — never add them or
  "fix" one to match the other.** Ledger `realizedpnl` is today only; the week/month/YTD
  windows are Flex, which is T+1 and never includes today. They also use different day
  boundaries: Flex on IBKR's session date, the ledger on IBKR's **accounting** roll —
  measured 2026-08-05 as late-ET-evening **at an hour that varies**, and **not midnight**
  in ET or UTC, so late in the evening the "Realised today" tile can already be on
  tomorrow. (It was documented as a calendar day until that measurement.) Midnight ET,
  midnight UTC and a fixed clock hour were each killed by a reading, the last by a
  37-read watch that ended exactly on the surviving bracket's bound with the field
  unmoved. **Do not put a specific time in user-facing text** — a claim the user can
  check against the clock is worse than a vague one if it is wrong. Those two differences
  are why they disagree on screen. Futures realised there is **entry → exit at the traded prices, not
  settlement-relative** — a lot opened at 80.84 and closed the next trade date across a
  75.77 settlement still reports against 80.84.
  They are additionally defined on different cost bases — ledger on IBKR's real-time
  `avgCost`, Flex on the statement basis — but **that divergence was measured on 2026-08-05
  and did not appear**: the CRM close read −2,810.47 on both, to the cent, and the earlier
  ≈−252.60 counter-figure was a projection, not a measurement. Do not restate it. Evidence
  at `dashboard_data.REALISED_LEDGER_WINDOW` and `RealisedWindow`.
  The positions table leads with **"Avg entry"** — the average price of
  the open lots, FIFO over the account's own fills (`dashboard_data.economic_entries`) —
  and shows IBKR's basis beside it: a basis is a fiscal figure, and a trader sizing an exit
  needs the level actually traded at. The reconstruction publishes a number **only when it
  independently reproduces IBKR's own position quantity**, and a blank cell means it declined.
  That check was called the whole safety argument until **2026-08-10 disproved it**: CL closed
  both open lots and reopened two more inside one session, so a book that stopped at the last
  statement reproduced IBKR's `2` exactly and certified 77.185 for lots bought at 82.05 — and
  the pane turned the gap into a **+9,734.72 USD** claim that the unrealised P&L was "basis
  rather than market". Same quantity, different lots; no quantity comparison can see it. The
  input is what closes it: the book is Flex through `flex_coverage().through` plus
  `/iserver/account/trades` after it, keyed on **conid** (never symbol), and **fills that
  could not be read (`None`, as against `()`) blank the column rather than certify the stored
  history**
- Panel folder hub (both references + dated research + smoke screenshots): `docs/panel/README.md`
- Startup flow, phase by phase (diagnose startup failures): `docs/startup-flow.md`
- Trade data sync (Flex vs live API, integrity checks): `docs/flex-query-setup.md` and
  `docs/trading-data-reference.md`. **Realised P&L = `SUM(flex_trade.fifo_pnl_realized)` over
  ALL trades — no open/close filter** (settled 2026-08-04 against IBKR's own annual statements,
  6/6 years exact). `flex_lot` is pre-wash-sale detail and must not be summed instead;
  `Trade == Lot + WashSale`. Both traps were live in this repo and are gated now.
- Market calendar (20 exchanges, futures schedules): `docs/market-calendar-reference.md`
- GDrive sync (folder layout, error handling): `docs/gdrive-sync-reference.md`
- TradingView integration (sidecar, curated tools, recovery): `docs/tradingview-reference.md` and
  `docs/tradingview-mcp-recovery.md`. **Since 2026-08-11 `execute()` post-processes every sidecar
  result**, so a payload in `claudia.db` need not match what the sidecar emitted: epoch fields gain
  a `<key>_utc` ISO sibling, a tool reporting success over its own empty result gains a
  `claudia_warning`, and oversized Pine `text` blobs become `<omitted: N chars>`. The seam is inert
  otherwise — 12 of 16 real payloads pass through byte-identical — and it fails open on anything it
  cannot parse. **Do not set `ensure_ascii=False` there**: it turns a JS-escaped lone surrogate into
  a string that cannot be UTF-8 encoded and crashes the `conversation_store` insert (tried and
  reverted 2026-08-11). Full table and rationale: `docs/tradingview-reference.md` § Result
  post-processing
- Web scraping — the 4 tools ClaudIA can call (`fetch_page`, `crawl_site`, `search_site`,
  `firecrawl_search`), paywalled-site logins, and what a blocked page looks like:
  `ibkr_core_mcp/docs/web-scraper-reference.md`. Two things that bite from ClaudIA's side:
  the tools need the `[scraper]` extra (`pip install "ibkr_core_mcp[scraper]"`) and fail only
  at call time without it, and **a fetch of a domain with a saved login profile opens a real
  browser window** — required, not a bug (§6 has the evidence).
- Environment variables (full reference): `docs/env-vars-reference.md`
- Conversation memory schema: `docs/conversation-memory-reference.md`
- API source-of-truth URLs (IBKR, Anthropic, Drive, Panel, libraries): `docs/api-reference.md`
- Known gaps, live test log, project status: `docs/project-status.md`
- Full documentation catalog (every doc in `docs/`, categorized): `docs/README.md`
