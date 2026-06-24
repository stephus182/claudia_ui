# ClaudIA UI — Developer Guide

ClaudIA is a Chainlit-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, and human-confirmed order staging.

---

## Architecture

```
Chainlit UI (localhost:8000)
    ↓
claudia/app.py              — session lifecycle, action callbacks, startup buttons
claudia/agent.py            — Anthropic SDK streaming loop, tool routing
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, relationships, doc_versions
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

**ibkr_core_mcp** is a direct Python import — not an MCP server. The `ClaudeToolkit`
exposes IBKR tools that drop straight into the Anthropic SDK `tools=` parameter.
TradingView tools are merged in from the `tradingview-mcp` Node.js sidecar (curated
15-tool subset by default — see `_CURATED_TOOLS` in `claudia/tradingview.py`).

---

## Trade Data Architecture

Two complementary sources — each covers what the other cannot:

| Source | Tool | Coverage | Latency |
|---|---|---|---|
| IBKR Flex Web Service | `sync_flex_trades` / `get_trades source='store'` | Full history (years), settled trades | T+1 — yesterday at best |
| IBKR Client Portal REST API | `get_trades source='live'` | Last 6 days, today's intraday | Real-time |

Flex never has today's trades. The live API fills that gap.

**Startup sync decision** (in `app.py → _background_flex_sync`):
1. `days_since_newest <= 1` → skip (Flex can't give anything newer than yesterday)
2. Last `flex_sync` log entry < 4h ago → skip (recent attempt, avoid API lockout)
3. Otherwise → sync, log result, back up `store.db` to Drive `account_data/`

**Data stores:**
- `~/.ibkr_core/store.db` — SQLite, all Flex-synced trades (1029 rows, 2020-present)
- `data/claudia.db` — SQLite, conversation history, sessions, decisions
- Drive `market_data/` — Parquet OHLCV cache
- Drive `account_data/` — Flex XML archives, `store.db` backup, `trade_coverage.json`

See [`docs/flex-query-setup.md`](docs/flex-query-setup.md) for full setup and troubleshooting.

---

## GDrive Sync

`claudia/gdrive_sync.py` — `GDriveSync` class, auto-enabled when `GOOGLE_DRIVE_FOLDER_ID` is set. No new env vars required.

### What syncs

| File | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (first session per process, before DB opens) |
| `claudia.db` | local → Drive | Session stop (after `close_session`, with WAL checkpoint) |
| `context.md` | Drive → memory | Every session start (overrides local file if present on Drive) |
| `principles.md` | Drive → memory | Every session start (overrides local file if present on Drive) |

### Drive folder layout

```
<GOOGLE_DRIVE_FOLDER_ID>/              ← root ClaudIA folder
  context.md                           ← ClaudIA persona (optional, upload manually)
  principles.md                        ← trading rules (optional, upload manually)
  db/                                  ← GDRIVE_DB_FOLDER_ID (auto-created by GDriveSync)
    claudia.db                         ← conversation history
  market_data/                         ← GDRIVE_CACHE_FOLDER_ID (auto-created by GDriveCache)
    manifest.json                      ← market data index
    AAPL_1D_1Y_2026-01-01.parquet      ← OHLCV cache
    ...
```

Both subfolders are auto-created on first use. Set `GDRIVE_DB_FOLDER_ID` or
`GDRIVE_CACHE_FOLDER_ID` explicitly to point to pre-existing folders instead.

### First-time setup on a new machine

1. Create (or reuse) a Google Drive folder for ClaudIA. Get its ID from the URL:
   `drive.google.com/drive/folders/<FOLDER_ID>`
2. Set `GOOGLE_DRIVE_FOLDER_ID=<FOLDER_ID>` in `.env`
3. Start ClaudIA — it downloads `claudia.db` (from the `db/` subfolder) on session start.
   Both `db/` and `market_data/` subfolders are auto-created on first use.
4. To enable Drive context/principles: upload `docs/context.md` and `docs/principles.md`
   to the **root** folder via the Drive web UI (not inside `db/`)

### Hot-reload behaviour

Drive texts are fetched once per session start. The watchdog still watches local files — editing `docs/context.md` while a session runs clears the Drive override and uses the local file from the next message.

### Error handling

All Drive operations are non-fatal. On any failure (no token, network error, tampered file):

| Operation | On failure |
|---|---|
| `download_db` at start | Log warning; use existing local `claudia.db` |
| `upload_db` at stop | Log warning; local copy preserved; syncs next session |
| `read_text` for context/principles | Log warning; fall back to local `docs/` files |

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
chmod 600 docs/context.md docs/principles.md

# 6. TradingView sidecar (optional)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version to vendor/
# Launch TradingView Desktop with CDP debug port:
# open -a "Trading View" --args --remote-debugging-port=9222

# 7. Run ClaudIA
./start-claudia.sh            # recommended: IBKR gateway + ClaudIA
# or:
chainlit run claudia/app.py   # ClaudIA only (in-chat "Start IBKR Gateway" button available)
# → Open http://localhost:8000
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `IBKR_GATEWAY_URL` | ✅ | IBKR Client Portal Gateway URL |
| `GOOGLE_DRIVE_FOLDER_ID` | ✅ | Root Drive folder — parent of `db/` and `market_data/` subfolders |
| `GDRIVE_DB_FOLDER_ID` | optional | Drive folder for claudia.db (auto-created as `db/` inside root if unset) |
| `GDRIVE_CACHE_FOLDER_ID` | optional | Drive folder for Parquet cache (auto-created as `market_data/` inside root if unset) |
| `GDRIVE_TOKEN_FILE` | ✅ | OAuth2 token file path |
| `GDRIVE_CREDENTIALS_FILE` | ✅ | OAuth2 credentials file path |
| `IBKR_SQLITE_PATH` | ✅ | ibkr_core_mcp SQLite store path |
| `IBKR_FLEX_TOKEN` | optional | For full trade history sync |
| `IBKR_FLEX_QUERY_ID` | optional | For full trade history sync |
| `CLAUDIA_MODEL` | optional | Claude model (default: `claude-opus-4-8`) |
| `CLAUDIA_DOCS_PATH` | optional | Path to context.md / principles.md (default: `docs/`) |
| `CLAUDIA_DB_PATH` | optional | ClaudIA SQLite DB path (default: `data/claudia.db`) |
| `CLAUDIA_VOICE_ENABLED` | optional | Enable TTS output (Phase 2) |
| `TRADINGVIEW_MCP_PATH` | optional | Path to `tradingview-mcp` entry point (`src/server.js`); auto-discovered if unset |
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

**Versioning:** On every session start, a SHA-256 hash of both documents is computed. If
the hash is new, it is automatically registered as the next version (`v1`, `v2`, …) in
`claudia.db → doc_versions`, and a human-readable snapshot is written to
`docs/versions/{label}/`. ClaudIA's system prompt always includes the active version label
so it knows which rules are in effect. If the version changed since the last session, a
`WARNING: v1 → v2` alert appears in chat.

ClaudIA has two tools to reason about version history:
- `list_doc_versions` — enumerate registered versions with dates
- `get_doc_version("v1")` — retrieve the full content of any past version to check for contradictions with current rules

Past decisions retrieved from memory always include which version was active when they
were made, so ClaudIA can flag if a prior decision conflicts with updated principles.

---

## Conversation Memory

All interactions are stored in `data/claudia.db` (separate from ibkr_core_mcp's `~/.ibkr_core/store.db`).

| Table | Contents |
|---|---|
| `sessions` | One row per Chainlit session, with start/end time, document hash, and `doc_version` |
| `messages` | Full message history (user, assistant, tool calls and results) |
| `decisions` | Extracted key moments: trade proposals, backtests run — each tagged with `doc_version` |
| `relationships` | Accumulated symbol-level observations built over time |
| `doc_versions` | Versioned snapshots of `context.md` + `principles.md` — full text, hash, date |

**Search:** ClaudIA uses SQLite FTS5 to search past decisions. Ask: *"What did I decide about NVDA last month?"* Results include the doc version active at decision time.

**Version snapshots** are also written to `docs/versions/{label}/` as human-readable files for reference.

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

## Price Alerts

Alerts are managed exclusively through IBKR's native server-side alert system — they fire even when ClaudIA is not running, and appear on the IBKR mobile app.

ClaudIA has four alert tools (via `ibkr_core_mcp.ClaudeToolkit`):

| Tool | What it does |
|---|---|
| `create_price_alert` | Resolves symbol → conid, posts alert to IBKR server |
| `get_alerts` | List all configured alerts with status |
| `delete_alert` | Remove an alert by ID |
| `activate_alert` | Toggle an alert on/off without deleting it |

There is no background polling loop in claudia_ui — IBKR delivers the notification directly to the mobile app and desktop.

---

## IBKR Gateway Startup

`start-claudia.sh` is the recommended launcher for a fresh session — it calls
`GatewayManager.startup()` then starts Chainlit.

If you launch `chainlit run claudia/app.py` directly and the gateway is offline,
the welcome message shows a **"Start IBKR Gateway"** action button. Clicking it:

1. Ensures Docker Desktop is running (launches it on macOS if needed)
2. Starts the gateway container
3. Waits up to 120s for the Java process to be reachable
4. Opens `https://localhost:5055` in your browser
5. You complete the IBKR login + 2FA; `ConnectivityChecker` sends the "reconnected" alert

This uses `ibkr_core_mcp.gateway.GatewayManager` — no changes to ibkr_core_mcp needed.

---

## TradingView Integration

**Screenshot analysis (always available):**
Drag or paste any TradingView chart screenshot into the chat. ClaudIA receives it as a
Claude vision content block and analyzes indicators, patterns, and price action.

**Live integration (requires TradingView Desktop):**

The sidecar is [`tradesdontlie/tradingview-mcp`](https://github.com/tradesdontlie/tradingview-mcp)
(78 tools, actively maintained). ClaudIA exposes a curated 15-tool subset by default
to control token cost; the full set is available via `bridge.get_all_tools()`.

Binary discovery order (`_find_tv_mcp_bin()`):
1. `TRADINGVIEW_MCP_PATH` env var (validated: file must exist and end in `.js`)
2. `tradingview-mcp` on PATH
3. `~/.tradingview-mcp/src/server.js` (pure JS layout — current)
4. `~/.tradingview-mcp/build/index.js` (TypeScript build output — legacy)
5. `vendor/tradingview-mcp/src/server.js` (archived fallback, needs `node_modules/`)
6. `vendor/tradingview-mcp/index.js` (legacy single-bundle archive)

If TradingView Desktop is not running at session start, the welcome message shows a
**"Launch TradingView"** action button. Clicking it runs `launch_tradingview()` (macOS
`open -a "Trading View" --args --remote-debugging-port=9222`), polls for CDP port 9222
for up to 30s, then reconnects the MCP sidecar.

**PineScript:** ClaudIA generates PineScript v5 directly. Use the **"Inject into TradingView"**
button to paste it into the Pine Editor via the `pine_set_source` MCP tool.

**Curated 15-tool subset** (`_CURATED_TOOLS` in `claudia/tradingview.py`):

| Category | Tools |
|---|---|
| Chart reading | `chart_get_state`, `quote_get`, `data_get_ohlcv`, `data_get_study_values` |
| Chart control | `chart_set_symbol`, `chart_set_timeframe`, `indicator_set_inputs` |
| Pine Script IDE | `pine_set_source`, `pine_smart_compile`, `pine_get_errors`, `pine_get_source` |
| Strategy results | `data_get_strategy_results`, `data_get_equity_curve` |
| Utility | `tv_health_check`, `capture_screenshot` |

**Break recovery:** If the sidecar breaks after a TradingView or npm update, see
[`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) for the error
signature catalog and recovery steps.

**Vendor archive:** Run `./scripts/archive-tv-mcp.sh` after every verified install to snapshot
the working version to `vendor/tradingview-mcp/`. For the JS layout it copies `src/` + installs
prod deps; for legacy TS it copies the single bundle. ClaudIA automatically falls back to this
archive if the live install at `~/.tradingview-mcp/` is missing or broken.

---

## Connectivity Status

`claudia/status.py` — `ConnectivityChecker` polls three services every 60s:

| Service | Check method | Transitions |
|---|---|---|
| IBKR | HTTP GET `/tickle` | Sends "reconnected" / "disconnected" chat alert |
| GDrive | Token file exists | Sends alert on state change |
| TradingView | TCP connect to port 9222 | Sends alert on state change |

The cached status is served by `GET /api/status` (used by the UI connectivity lights).
TradingView status is `UNKNOWN` (gray) when no bridge is configured, `ERROR` (red) when
the bridge exists but CDP port 9222 is unreachable.

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

Tools planned for ibkr_core_mcp:
- `preview_order` — read-only whatif order preview (in `ibkr_core_mcp/claude_tools.py`)
- `get_pnl` — real-time partitioned P&L (in `ibkr_core_mcp/claude_tools.py`)
