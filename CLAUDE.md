# ClaudIA UI — Developer Guide

ClaudIA is a Chainlit-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, and human-confirmed order staging.

---

## Architecture

```
Chainlit UI (localhost:8000)
    ↓
claudia/app.py              — session lifecycle, action callbacks
claudia/agent.py            — Anthropic SDK streaming loop, tool routing
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, relationships
claudia/order_flow.py       — cl.Action order staging → ibkr_core_mcp biometric gates
claudia/alert_manager.py    — background price alert monitor
claudia/tradingview.py      — tradingview-mcp sidecar + PineScript display
    ↓
ibkr_core_mcp (local editable install)
    ↓
IBKR Client Portal Gateway (https://localhost:5055)
```

**ibkr_core_mcp** is a direct Python import — not an MCP server. The `ClaudeToolkit`
exposes 22 IBKR tools that drop straight into the Anthropic SDK `tools=` parameter.
TradingView tools are merged in from the `tradingview-mcp` Node.js sidecar.

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
# Edit .env — minimum required: ANTHROPIC_API_KEY

# 5. Create your personal documents (see below)
cp docs/context.example.md docs/context.md
cp docs/principles.example.md docs/principles.md
# Edit both files to configure ClaudIA's persona and your trading rules

# 6. Run ClaudIA
chainlit run claudia/app.py
# → Open http://localhost:8000
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `IBKR_GATEWAY_URL` | ✅ | IBKR Client Portal Gateway URL |
| `GOOGLE_DRIVE_FOLDER_ID` | ✅ | GDrive folder for market data cache |
| `GDRIVE_TOKEN_FILE` | ✅ | OAuth2 token file path |
| `GDRIVE_CREDENTIALS_FILE` | ✅ | OAuth2 credentials file path |
| `IBKR_SQLITE_PATH` | ✅ | ibkr_core_mcp SQLite store path |
| `IBKR_FLEX_TOKEN` | optional | For full trade history sync |
| `IBKR_FLEX_QUERY_ID` | optional | For full trade history sync |
| `CLAUDIA_MODEL` | optional | Claude model (default: `claude-opus-4-8`) |
| `CLAUDIA_DOCS_PATH` | optional | Path to context.md / principles.md (default: `docs/`) |
| `CLAUDIA_DB_PATH` | optional | ClaudIA SQLite DB path (default: `data/claudia.db`) |
| `CLAUDIA_VOICE_ENABLED` | optional | Enable TTS output (Phase 2) |
| `TRADINGVIEW_MCP_PATH` | optional | Path to `tradingview-mcp` binary |
| `TRADINGVIEW_DEBUG_PORT` | optional | Chrome debugging port (default: `9222`) |

---

## context.md and principles.md

These two documents define ClaudIA's entire behavior. They are loaded at session start
and injected as the system prompt. **Never commit these files** — they contain your
personal trading rules.

- `docs/context.md` — Who ClaudIA is: its role, persona, areas of expertise, communication style.
- `docs/principles.md` — Your trading rules: risk limits, preferred strategies, instruments, position sizing, red lines.

**Hot-reload:** Edit and save either file while a session is running. ClaudIA will notify
you in chat and apply the new content from the next message onwards.

**Integrity:** On every session start, a SHA-256 hash of both documents is stored in
`claudia.db → sessions.context_hash`. If the hash changes, it is logged.

---

## Conversation Memory

All interactions are stored in `data/claudia.db` (separate from ibkr_core_mcp's `~/.ibkr_core/store.db`).

| Table | Contents |
|---|---|
| `sessions` | One row per Chainlit session, with start/end time and document hash |
| `messages` | Full message history (user, assistant, tool calls and results) |
| `decisions` | Extracted key moments: trade proposals, backtests run, alerts set |
| `relationships` | Accumulated symbol-level observations built over time |

**Search:** ClaudIA uses SQLite FTS5 to search past decisions. Ask: *"What did I decide about NVDA last month?"*

---

## Order Staging Flow

ClaudIA **cannot** place orders autonomously. When ClaudIA suggests a trade:

1. ClaudIA outputs an `order-proposal` JSON block in its response.
2. `agent.py` strips the block and calls `order_flow.render_order_proposal()`.
3. A Chainlit message appears with full order details + **"Stage this order"** button.
4. You click the button — `IBKRClient.place_order()` is called directly.
5. **Gate 1:** Apple Touch ID / biometric authentication (macOS LocalAuthentication).
6. **Gate 2:** tkinter modal dialog with order details + 60-second countdown. Enter key disabled.
7. Order is submitted to IBKR only after both gates pass.

---

## TradingView Integration

**Phase 1 — Screenshot analysis (always available):**  
Drag or paste any TradingView chart screenshot into the chat. ClaudIA receives it as a
Claude vision content block and analyzes indicators, patterns, and price action.

**Phase 1 — Live integration (requires TradingView Desktop):**
```bash
npm install -g @mxstbr/tradingview-mcp
# Open TradingView Desktop, then start it with remote debugging:
# On macOS: open -a "Trading View" --args --remote-debugging-port=9222
chainlit run claudia/app.py   # sidecar starts automatically
```

**PineScript:** ClaudIA generates PineScript v5 directly. Use the **"Inject into TradingView"** 
button to paste it directly into the Pine Editor (requires live integration).

---

## Testing

```bash
# Unit tests (no IBKR connection needed)
pytest -m "not integration"

# All tests (requires live IBKR gateway)
pytest
```

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

## ibkr_core_mcp Dependency

`ibkr_core_mcp` is installed as a local editable package:
```
pip install -e "../ibkr_core_mcp"
```

When updating ibkr_core_mcp (e.g. after adding new tools), re-run `pip install -e "../ibkr_core_mcp"`.
No restart of the Chainlit app is needed for tool definition changes; restart required for
Python module changes.

Tools added in claudia_ui's plan:
- `preview_order` — read-only whatif order preview (in `ibkr_core_mcp/claude_tools.py`)
- `get_pnl` — real-time partitioned P&L (in `ibkr_core_mcp/claude_tools.py`)
