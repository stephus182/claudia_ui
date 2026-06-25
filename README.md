# ClaudIA — AI Trading Assistant

ClaudIA is a Chainlit-based trading assistant that gives you a persistent, principle-guided AI for market analysis, strategy work, and human-confirmed order staging. It connects to Interactive Brokers via `ibkr_core_mcp` and to TradingView Desktop via the `tradingview-mcp` Node.js sidecar.

---

## Features

- **Conversational IBKR access** — positions, P&L, live orders, account summary, market data, backtests, price alerts — all via natural language
- **Full trade history** — 7-year backfill via IBKR Flex Queries; `sync_flex_trades` keeps it current; `get_trades source='store'` queries with no date limit
- **Human-confirmed order staging** — ClaudIA proposes trades; you click a button → Touch ID → confirmation dialog. The LLM has no order-execution tools
- **TradingView live integration** — reads your active chart, sets symbols/timeframes, compiles and injects PineScript directly into the Pine Editor
- **Screenshot analysis** — paste any TradingView chart into chat for vision-based analysis (no Desktop required)
- **Principle-guided responses** — your personal `docs/principles.md` is loaded as a system prompt; ClaudIA refuses proposals that violate your rules
- **Persistent memory** — all sessions, decisions, and symbol observations stored in SQLite with FTS5 search ("what did I decide about NVDA last month?")
- **GDrive sync** — `claudia.db` and context/principles docs auto-sync to Google Drive; pick up any session from any machine
- **Hot-reload documents** — edit `context.md` or `principles.md` while a session is open; changes apply from the next message
- **In-chat startup buttons** — "Start IBKR Gateway" and "Launch TradingView" action buttons appear when services are offline at session start
- **Connectivity status bar** — live IBKR / GDrive / TradingView lights in the UI header, polled every 60s
- **Session reports** — auto-generated Markdown report at session end: tools called, decisions, errors, connectivity state

---

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.11+ | ClaudIA runtime |
| `ibkr_core_mcp` | IBKR tools, gateway management, SQLite store |
| Docker Desktop | IBKR Client Portal Gateway container |
| Node.js 18+ | tradingview-mcp sidecar |
| TradingView Desktop (macOS) | Live chart integration (optional) |

---

## Quick Start

```bash
# 1. Clone
git clone <this-repo> && cd claudia_ui

# 2. Python env
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e "../ibkr_core_mcp"

# 3. Environment
cp .env.example .env
# Edit .env — minimum: ANTHROPIC_API_KEY

# 4. Personal documents
cp docs/context.example.md docs/context.md
cp docs/principles.example.md docs/principles.md
# Edit both to configure ClaudIA's persona and your trading rules
chmod 600 docs/context.md docs/principles.md

# 5. TradingView sidecar (optional — skip if using screenshot mode only)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version

# 6. Launch
./start-claudia.sh             # recommended: starts IBKR gateway + ClaudIA
# or:
chainlit run claudia/app.py    # ClaudIA only — use the in-chat "Start IBKR Gateway" button
```

Open **http://localhost:8000**

---

## TradingView Desktop

For live chart integration, TradingView Desktop must be open with remote debugging enabled.
ClaudIA can launch it for you via the **"Launch TradingView"** button in the welcome message, or manually:

```bash
open -a "Trading View" --args --remote-debugging-port=9222
```

If the sidecar breaks after a TradingView or npm update, see
[`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) for the break pattern catalog
and recovery steps, including a direct CDP from Python fallback.

---

## Architecture

```
Chainlit UI (localhost:8000)
    ↓
claudia/app.py              — session lifecycle, action callbacks, startup buttons
claudia/agent.py            — Anthropic SDK streaming loop, tool routing
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, relationships
claudia/order_flow.py       — cl.Action order staging → biometric gates
claudia/alert_manager.py    — background price alert monitor
claudia/status.py           — ConnectivityChecker: polls IBKR/GDrive/TV every 60s
claudia/tradingview.py      — tradingview-mcp sidecar, CDP health, PineScript display
claudia/gdrive_sync.py      — claudia.db + context/principles sync to Google Drive
claudia/session_reporter.py — auto-generate session report at session end
    ↓                               ↓
ibkr_core_mcp               tradingview-mcp (Node.js, localhost stdio)
(local editable install)            ↓
    ↓                       TradingView Desktop (CDP, localhost:9222)
IBKR Client Portal Gateway
(Docker, localhost:5055)
```

---

## Documentation

| File | Contents |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Developer guide: setup, env vars, architecture, hard rules |
| [`SECURITY.md`](SECURITY.md) | Security model: order barriers, threat model, audit checklist |
| [`docs/flex-query-setup.md`](docs/flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) | TradingView break patterns, recovery steps, CDP fallback |
| [`docs/connectivity.md`](docs/connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows, live test results |
| [`docs/project-status.md`](docs/project-status.md) | Feature timeline, test coverage, live test plan and log |

---

## External API Reference

Any contribution touching API behavior, error codes, endpoint paths, or field names **must reference the official documentation first** — never assume from memory.

| API | Used in | Official reference |
|---|---|---|
| IBKR Client Portal API | `ibkr_core_mcp` | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/ |
| IBKR Flex Web Service | `ibkr_core_mcp/flex_query.py` | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3.htm |
| IBKR Flex error codes | `ibkr_core_mcp/flex_query.py` | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm |
| Anthropic Messages API | `claudia/agent.py` | https://docs.anthropic.com/en/api/messages |
| Anthropic tool use | `claudia/agent.py` | https://docs.anthropic.com/en/docs/build-with-claude/tool-use |
| Google Drive API v3 | `claudia/gdrive_sync.py` | https://developers.google.com/drive/api/reference/rest/v3 |
| TradingView MCP | `claudia/tradingview.py` | https://github.com/tradesdontlie/tradingview-mcp |
| Chrome DevTools Protocol | `claudia/tradingview.py` | https://chromedevtools.github.io/devtools-protocol/ |
| Chainlit | `claudia/app.py` | https://docs.chainlit.io |
| `requests` (web fetch) | `claudia/agent.py` | https://docs.python-requests.org/ |
| `html2text` (HTML → Markdown) | `claudia/agent.py` | https://github.com/Alir3z4/html2text |
| `watchdog` (file monitoring) | `claudia/context_loader.py` | https://watchdog.readthedocs.io/ |
| `mcp` Python client (stdio) | `claudia/tradingview.py` | https://github.com/modelcontextprotocol/python-sdk |

Full protocol and per-file ownership: [`CLAUDE.md → API Reference`](CLAUDE.md#api-reference--docs-first).

---

## Data Stores

| Store | Path | Contents |
|---|---|---|
| `claudia.db` | `data/claudia.db` | Sessions, messages, decisions, relationships, doc versions |
| `store.db` | `~/.ibkr_core/store.db` | Trade history (Flex), position snapshots, backtests, alerts |

`claudia.db` syncs to Google Drive at session end (when `GOOGLE_DRIVE_FOLDER_ID` is set).
`store.db` is managed by `ibkr_core_mcp` and is never synced — it is rebuilt from Flex archives on any machine.

Both databases are excluded from git. Run `PRAGMA integrity_check` to audit health.

---

## Testing

```bash
pytest -m "not integration"   # 136 unit tests (no IBKR gateway needed)
pytest                        # all tests (requires live IBKR gateway)
```
