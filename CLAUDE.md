# ClaudIA UI — Developer Guide

ClaudIA is a Chainlit-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, and human-confirmed order staging.

---

## Architecture

```
Chainlit UI (localhost:8000)
    ↓
claudia/app.py              — session lifecycle, action callbacks, startup buttons
claudia/agent.py            — Anthropic SDK streaming loop, tool routing, prompt caching
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, doc_versions
claudia/gdrive_sync.py      — GDriveSync: download claudia.db at start / upload at stop
claudia/order_flow.py       — cl.Action order staging → ibkr_core_mcp biometric gates
claudia/status.py           — ConnectivityChecker: IBKR/GDrive/TV polling, TCP health
claudia/tradingview.py      — tradingview-mcp sidecar + CDP health + PineScript display
    ↓                               ↓
ibkr_core_mcp               tradingview-mcp (Node.js, stdio)
(local editable install)            ↓
    ↓                       TradingView Desktop (CDP, localhost:9222)
IBKR Client Portal Gateway
(Docker, localhost:5055)
```

`ibkr_core_mcp` is a direct Python import, not an MCP server — `ClaudeToolkit` tools drop
straight into the Anthropic SDK `tools=` parameter. TradingView tools come from a Node.js
sidecar. Full tool catalog: `ibkr_core_mcp/docs/tools-reference.md` (42 tools).

---

## Dev Setup

```bash
# 1. Clone and enter the project
cd /Users/steph/Claude_Projects/claudia_ui

# 2. Create venv
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Install claudia_ui + ibkr_core_mcp (editable)
pip install -e ".[dev]"
pip install -e "../ibkr_core_mcp"

# 4. Copy and fill in env vars
cp .env.example .env
# Edit .env — minimum required: ANTHROPIC_API_KEY. Full var reference: @docs/env-vars-reference.md

# 5. Create your personal documents
cp docs/context.example.md docs/context.md
cp docs/principles.example.md docs/principles.md
chmod 600 docs/context.md docs/principles.md

# 6. TradingView sidecar (optional — one-time install)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version to vendor/
# Full sidecar details, curated tool list, recovery: @docs/tradingview-reference.md

# 7. Run ClaudIA
./start-claudia.sh            # recommended: IBKR gateway + ClaudIA
# or:
chainlit run claudia/app.py   # ClaudIA only (in-chat "Start IBKR Gateway" button available)
# → Open http://localhost:8000
```

## Testing

```bash
pytest -m "not integration"   # unit tests, no IBKR connection needed
pytest                        # all tests, requires live IBKR gateway
```

---

## Conventions

- **API Docs First**: never assume endpoint behavior, error codes, or field names from
  memory. Always `WebFetch` the official doc before writing any error message, fix, or
  diagnosis. Cite the source URL in the error string and commit message. This rule exists
  because two production bugs went undetected for months and were caught instantly once
  docs were checked. Full source table: `docs/api-reference.md`
- `context.md` / `principles.md` define ClaudIA's persona and trading rules. Hot-reloaded
  mid-session, never commit either file. Loading/versioning mechanics: `docs/context-loading-reference.md`
- Prompt caching uses 3 breakpoints (tools → system → messages). Mechanics and live-verified
  numbers: `docs/context-loading-reference.md`. Design rationale and the three-round
  consistency review: `docs/plans/2026-07-03-prompt-caching-upgrade.md`

---

## Hard Rules for Developers

These rules must never be violated when extending ClaudIA:

1. **Never add a tool that calls `place_order`, `modify_order`, `cancel_order`, or `reply_order`.**
   Order staging is a UI-layer action triggered by a physical button click, not an LLM tool call.
2. **Never log or expose `ANTHROPIC_API_KEY`** in Chainlit output, logs, or error messages.
3. **Never modify the hardcoded safety block** in `claudia/agent.py` to weaken constraints.
4. **Never inject conversation history directly into the system prompt.** History must be
   added as `role: user/assistant` message objects to prevent prompt injection.
5. **ibkr_core_mcp is read-only from claudia_ui's perspective.** Never bypass `ClaudeToolkit`
   to call `IBKRClient` directly from within an LLM tool handler.

---

## Order Staging (safety-critical — summary only, full spec: `docs/order-api-reference.md`)

ClaudIA **cannot** place, modify, or cancel orders autonomously:
1. ClaudIA embeds an `order-proposal` JSON block in its response.
2. `agent.py` strips it, calls `order_flow.render_order_proposal()` →
   Chainlit message with a **"Stage this order"** button.
3. Click → `execute_staged_order()` → **Gate 1** (Touch ID) → **Gate 2** (AppKit dialog,
   green/red banner by side, **SEND TO IBKR** button, 60s auto-cancel, Return key disabled).
4. `IBKRClient.place_order()` fires only after both gates pass.

- **Order parameters are immutable**: ClaudIA must use the user's exact values (symbol,
  action, quantity, price, order type, TIF). No rounding or "helpful" adjustment. A risky
  parameter gets a text warning, never a silent change — changing a parameter requires
  explicit user approval in a follow-up message. Enforced in `claudia/agent.py` system
  prompt and in memory (`feedback-order-parameter-immutability.md`).
- Modify requests require the **full original order**, not a diff (IBKR API requirement).
- FUT/FOP require `conid` pre-resolved via `get_option_chain`/`get_futures` — no fallback
  symbol-based resolution for modify/cancel.

---

## ibkr_core_mcp Dependency

Local editable install: `pip install -e "../ibkr_core_mcp"` — re-run after ibkr_core_mcp
adds new tools. No Chainlit restart needed for tool definition changes; restart required
for Python module changes. Full tool catalog (40 core + 2 optional web-scraper = 42 total,
matching `_all_tools` in `claudia/agent.py`): `ibkr_core_mcp/docs/tools-reference.md` —
check there before adding/debugging a tool. Recent additions log: `ibkr_core_mcp/CHANGELOG.md`.

## Pointers

Plain file references below, not `@import`s — read on demand via normal file tools, not
loaded into every session's context automatically. Compliant with the official Claude Code
memory docs (verified 2026-07-10, https://code.claude.com/docs/en/memory): a bare `@path` is
a real import ("expanded and loaded into context at launch"); backtick-wrapping keeps it a
literal path instead. See `docs/plans/2026-07-10-claude-md-delink-imports.md` for
the fix that established this (75,480 → 2,910 tokens/session).

- Trade data sync (Flex vs live API, integrity checks): `docs/flex-query-setup.md` and `docs/trading-data-reference.md`
- Market calendar (20 exchanges, futures schedules): `docs/market-calendar-reference.md`
- GDrive sync (folder layout, error handling): `docs/gdrive-sync-reference.md`
- TradingView integration (sidecar, curated tools, recovery): `docs/tradingview-reference.md` and `docs/tradingview-mcp-recovery.md`
- Environment variables (full reference): `docs/env-vars-reference.md`
- Conversation memory schema: `docs/conversation-memory-reference.md`
- API source-of-truth URLs (IBKR, Anthropic, Drive, Chainlit, libraries): `docs/api-reference.md`
- Known gaps, live test log, project status: `docs/project-status.md`
- Full documentation catalog (every doc in `docs/`, categorized): `docs/README.md`
