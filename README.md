# ClaudIA — AI Trading Assistant

ClaudIA is a Panel-based trading assistant that gives you a persistent, principle-guided AI for market analysis, strategy work, and human-confirmed order staging. It connects to Interactive Brokers via `ibkr_core_mcp` and to TradingView Desktop via the `tradingview-mcp` Node.js sidecar. 

---

## Features

- **Conversational IBKR access** — positions, P&L, live orders, account summary, market data, backtests, price alerts — all via natural language
- **Execution-triggered P&L** — a background listener watches for trade executions (any origin — mobile, TWS, web, API) and refreshes account P&L automatically each time a trade fills; no continuous polling
- **Full trade history** — 7-year backfill via IBKR Flex Queries; `sync_flex_trades` keeps it current; `get_trades source='store'` queries with no date limit
- **Human-confirmed order staging** — ClaudIA proposes trades (equities and futures); you click a button → Touch ID → AppKit colored dialog (green/BUY, red/SELL). The LLM has no order-execution tools. CME Rule 536-B fields auto-added for futures
- **TradingView live integration** — reads your active chart, sets symbols/timeframes; every ```pine block ClaudIA emits gets a **Copy** button (real client-side clipboard) and an **Inject into TradingView** button that sets the Pine Editor source directly
- **Live account dashboard** — KPI strip · positions · working orders · realised P&L, polled every 15s, read-only by construction. Note the "Realised today" tile follows IBKR's accounting day, which **rolls in the late ET evening, not at midnight** — see [Live Dashboard](#live-dashboard--and-the-two-realised-figures)
- **External candlestick chart pane** — a HoloViews/hvplot chart beside the chat (symbol/period/bar controls, SMA overlay, volume subplot), OHLCV from the Drive cache with fetch-on-miss from IBKR; fully independent of the conversation
- **Screenshot analysis** — upload any TradingView chart for vision-based analysis (no Desktop required)
- **Principle-guided responses** — your personal `docs/principles.md` is loaded as a system prompt; ClaudIA refuses proposals that violate your rules
- **Persistent memory** — all sessions, decisions, and symbol observations stored in SQLite with FTS5 search ("what did I decide about NVDA last month?")
- **GDrive sync** — `claudia.db` and context/principles docs auto-sync to Google Drive; pick up any session from any machine
- **Hot-reload documents** — edit `context.md` or `principles.md` while a session is open; changes apply from the next message
- **In-chat startup buttons** — "Start IBKR Gateway" and "Launch TradingView" action buttons appear when services are offline at session start
- **Connectivity status dots** — live IBKR / GDrive / TradingView indicators above the chat; the dots re-read status every 5s over Panel's websocket, and the underlying services are polled every 60s (IBKR's `/tickle` keepalive interval)
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
pip install -e "../ibkr_core_mcp" --config-settings editable_mode=strict
# strict mode required for mypy to resolve ibkr_core_mcp — see CLAUDE.md Dev Setup

# 3. Environment
cp .env.example .env
# Edit .env — minimum: ANTHROPIC_API_KEY

# 4. Personal documents (git-ignored persona + trading rules)
# With GOOGLE_DRIVE_FOLDER_ID set they download from Drive automatically — skip this.
# Otherwise create them by hand:
touch docs/context.md docs/principles.md   # then write persona / trading rules
chmod 600 docs/context.md docs/principles.md

# 5. TradingView sidecar (optional — skip if using screenshot mode only)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version

# 6. Launch
./start-claudia.sh             # recommended: starts IBKR gateway + ClaudIA
# or:
python -m claudia.panel_app    # ClaudIA only — use the in-chat "Start IBKR Gateway" button
```

Open **http://localhost:8001**

---

## TradingView Desktop

For live chart integration, TradingView Desktop must be open with remote debugging enabled.
ClaudIA can launch it for you via the **"Launch TradingView"** button in the welcome message, or manually:

```bash
open -a "Trading View" --args --remote-debugging-port=9222
```

If TradingView is already running **without** the debug port, use the one-command fix —
it quits TV gracefully, relaunches with the debug flag, and waits for CDP to come up:

```bash
./scripts/launch-tradingview-debug.sh
```

If the sidecar breaks after a TradingView or npm update, see
[`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) for the break pattern catalog
and recovery steps, including a direct CDP from Python fallback.

---

## Architecture

```
Panel UI (localhost:8001 — native pn.serve Tornado)
    ↓
claudia/panel_app.py        — pn.serve entry: session lifecycle, status dots, startup buttons
claudia/panel_sink.py       — PanelMessageSink: agent output → pn.chat.ChatInterface
claudia/panel_order_flow.py — order/cancel/modify proposal buttons → order_flow cores
claudia/panel_pinescript.py — ```pine copy (real clipboard) / inject buttons
claudia/panel_chart.py      — external HoloViews candlestick chart pane (STK, cache-backed)
claudia/agent.py            — Anthropic SDK streaming loop, tool routing, prompt caching
claudia/proposal_tools.py   — strict-schema propose_order/cancel/modify declarations (no execution)
claudia/message_sink.py     — MessageSink protocol (the UI-decoupling seam)
claudia/order_flow.py       — framework-agnostic order-execution cores → biometric gates
claudia/opening_status.py   — UI-free opening-status builders
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, doc_versions
claudia/status.py           — ConnectivityChecker: polls IBKR/GDrive/TV every 60s
claudia/execution_listener.py — ExecutionListener: WS trade-execution listener, triggers P&L checks
claudia/tradingview.py      — tradingview-mcp sidecar, CDP health, TradingViewBridge
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

## Order Staging

ClaudIA proposes trades; you approve them through two physical gates. Proposing is a tool
call that records a proposal and returns a result — it reaches no IBKR API. The LLM has
**no** order-execution tools.

```
ClaudIA calls propose_order (strict-schema tool — records, executes nothing)
    ↓ agent.py hands the validated input → MessageSink.send_order_proposal()
    ↓ panel_order_flow.render_order_proposal() → button: "Stage this order"
    ↓ User clicks → order_flow._execute_staged_order_core()
    ↓ Gate 1 — Touch ID (macOS LocalAuthentication)
    ↓ Gate 2 — AppKit dialog: green=BUY / red=SELL, 60s auto-cancel, Enter disabled
    ↓ IBKRClient.place_order() → IBKR gateway
```

**Supported instruments:**

| `sec_type` | Conid resolution | Extra fields |
|---|---|---|
| `STK` (default) | **none — a `conid` must already be in the proposal** (from `get_market_snapshot` / `preview_order`, which route through the authoritative resolver) | — |
| `FUT` | `/trsrv/futures` → front month | `manualIndicator: True` (CME Rule 536-B). `extOperator` is deliberately **not** sent — IBKR rejects it with error 8089 despite the docs marking it "Required*"; live-proven 2026-07-24 |
| `FOP` | pre-resolved `conid` required in the proposal (via `get_option_chain`) | same 536-B fields as FUT |

**Placement resolves no symbols.** The order path used to fall back to
`/iserver/secdef/search` → `contracts[0]`, which carries no country and no currency and
has an undocumented result order — so `contracts[0]` for `IGV` was the **Mexican**
listing, priced in MXN, in a USD account. FUT is the one exception, resolved by front
month and unambiguous by construction. Do not restore symbol resolution here: it would
put a second, drifting definition beside the authoritative one.

The Gate 2 dialog shows correct futures notional: `price × qty × multiplier` (multiplier fetched from `/trsrv/futures`).

Full field spec and immutability rule: [`docs/order-api-reference.md`](docs/order-api-reference.md),
summarized in [`CLAUDE.md`](CLAUDE.md) § Order Staging.

---

## Live Dashboard — and the two realised figures

A polling dashboard sits beside the chat: a KPI strip over tabs for Chart, Positions,
working Orders and P&L, refreshed every 15s. Every table is read-only with no click
handler bound — cancelling an order stays behind `propose_cancel` and both gates.

The P&L tab shows **two realised numbers that will not reconcile, and are not meant to.**
They are different quantities; never add them or "fix" one to match the other. The
dashboard states all three reasons on the surface itself rather than leaving you to
discover them by subtraction:

| | **Realised today** (tile) | **Week / month / YTD** |
|---|---|---|
| Source | IBKR ledger `realizedpnl` — live | Flex statements — **T+1, never includes today** |
| Cost basis | IBKR's real-time `avgCost` | the statement basis |
| Day boundary | IBKR's **accounting** roll — see below | IBKR's **session** date (18:00 ET futures, 20:00 ET stock, 17:00 ET FX) |

### The "today" on that tile ends in the late evening, not at midnight

**Measured 2026-08-05: the ledger accumulator rolls late in the ET evening, at an hour
that varies.** Not midnight ET, and not midnight UTC. So for the last hours of a calendar
day the tile labelled "Realised today" can **already be showing tomorrow** — typically
0.00, right after a day that realised something. That reset is correct behaviour, not a
data fault, and it is worth knowing before you read the number at 22:30 and conclude the
feed broke. This project documented it as a calendar day until that measurement.

Three readings killed three candidates in turn — midnight ET, midnight UTC, then a fixed
clock hour (a 37-read watch ending exactly on the surviving bracket's upper bound found
the field unmoved). What actually triggers it is a broker-side accounting run, and this
project does not know its schedule. **No specific time is quoted here on purpose:** a
disclosure you can check against the clock is worth nothing if it is wrong.

Two further conventions of the same field, both measured to the cent the same evening:

- **It is futures-realised at the traded prices — entry → exit, not settlement-relative.**
  A lot opened at 80.84 and closed the next trade date *across* a 75.77 settlement still
  reports against 80.84. Daily variation margin moves cash; it does not re-base this
  number.
- **Realisation is booked at the closing fill, carrying both legs' commissions.**

Mechanics, evidence and the negative controls: read the module docstring of
[`claudia/dashboard_data.py`](claudia/dashboard_data.py) first — it carries the source
table for every figure on the screen — then `REALISED_LEDGER_WINDOW` in the same file.

---

## Agent — Prompt & Context Handling

`claudia/agent.py` assembles four kinds of information into every API call: the
system prompt (context.md + principles.md + market calendar + hardcoded safety
block), tool schemas (`ClaudeToolkit` + TradingView + local tools), conversation
history (`ConversationStore`), and tool results returned mid-loop.

**System prompt — built once per session, not per message.** Doc-version and
document checks run when ClaudIA loads; a watchdog-driven reload counter
(`ContextLoader.reload_count`) triggers a rebuild only when `context.md` or
`principles.md` actually changes. Steady-state per-message cost is one integer
comparison — no file reads, no DB query.

**Prompt caching — 3 breakpoints** (`cache_control: ephemeral`, prefix hierarchy
`tools → system → messages`):

| Breakpoint | Caches |
|---|---|
| Last tool definition | All tool schemas (42+ IBKR/TV/local tools) |
| System prompt (block form) | Context, principles, calendar, safety block |
| Last message content block | Conversation history, refreshed per API call |

Live-verified: a ~22K-token static prefix drops to 0.1× cost on every warm call
(vs. full price uncached) — ~90% input-token cost reduction on cached calls.
Cache health is logged on every call (`prompt cache: created=… read=… uncached=…`).

**No dead memory tables.** `sessions`, `messages`, `decisions`, `doc_versions` are
the only tables — a `relationships` table and a `decisions` FTS index were
removed 2026-07-03 (never wired to any tool or caller).

Full information-flow map (prompts, session archive, scrape access, and the
design constraints a future RAG layer must respect) —
[`docs/audits/2026-07-03-agent-info-architecture-review.md`](docs/audits/2026-07-03-agent-info-architecture-review.md).
Implementation plan and live-verified numbers —
`docs/plans/2026-07-03-prompt-caching-upgrade.md`.

---

## Documentation

| File | Contents |
|---|---|
| [`docs/README.md`](docs/README.md) | Full documentation catalog — every doc in `docs/`, categorized |
| [`CLAUDE.md`](CLAUDE.md) | Developer guide: setup, env vars, architecture, hard rules |
| [`SECURITY.md`](SECURITY.md) | Security model: order barriers, threat model, audit checklist |
| [`docs/flex-query-setup.md`](docs/flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) | TradingView break patterns, recovery steps, CDP fallback |
| [`docs/connectivity.md`](docs/connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows, live test results |
| [`docs/project-status.md`](docs/project-status.md) | Milestone history, test coverage, live testing index and log, known gaps |

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
| Panel | `claudia/panel_*.py` | https://panel.holoviz.org |
| hvPlot / HoloViews | `claudia/panel_chart.py` | https://hvplot.holoviz.org / https://holoviews.org |
| Bokeh (HoloViews' rendering backend) | `claudia/panel_chart.py` | https://docs.bokeh.org |
| `requests` (web fetch) | `claudia/agent.py` | https://docs.python-requests.org/ |
| `html2text` (HTML → Markdown) | `claudia/agent.py` | https://github.com/Alir3z4/html2text |
| `watchdog` (file monitoring) | `claudia/context_loader.py` | https://watchdog.readthedocs.io/ |
| `mcp` Python client (stdio) | `claudia/tradingview.py` | https://github.com/modelcontextprotocol/python-sdk |

Full protocol and per-file ownership: [`CLAUDE.md → API Reference`](CLAUDE.md#api-reference--docs-first).

---

## Data Stores

| Store | Path | Contents |
|---|---|---|
| `claudia.db` | `data/claudia.db` | Sessions, messages, decisions, doc versions |
| `store.db` | `~/.ibkr_core/store.db` | Trade history (Flex), position snapshots, backtests, alerts |

Both databases are excluded from git. Run `PRAGMA integrity_check` to audit health.

---

## Google Drive Architecture (multi-machine portability)

ClaudIA is designed to run on any machine — all persistent state lives in a single Google Drive root folder. Set `GOOGLE_DRIVE_FOLDER_ID` and ClaudIA restores itself automatically.

```
<GOOGLE_DRIVE_FOLDER_ID>/          ← one root folder, one env var
  context.md                       ← ClaudIA persona (cloud-authoritative)
  principles.md                    ← trading rules (cloud-authoritative)
  db/
    claudia.db                     ← conversation history (download at start, upload at end)
  market_data/
    manifest.json
    QQQ_1D_6M_2026-06-26.parquet   ← OHLCV cache (shared across machines)
  account_data/
    flex_U123_2026-06-26_REF.xml   ← Flex XML archives (re-importable to SQLite)
    store.db                       ← ibkr_core_mcp trade store backup
```

**What syncs automatically:**

| Data | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (first session per process, before DB opens) |
| `claudia.db` | local → Drive | Session end (WAL-consistent backup snapshot — never the live file) |
| `context.md` + `principles.md` | Drive → memory | Every session start |
| Flex XML | local → `account_data/` | After every successful Flex sync |
| OHLCV parquet | local → `market_data/` | After every `fetch_market_data` call |

**What a new machine needs** (nothing else):
- `GOOGLE_DRIVE_FOLDER_ID` — root folder ID
- `GDRIVE_TOKEN_FILE` + `GDRIVE_CREDENTIALS_FILE` — OAuth2 credentials
- `ANTHROPIC_API_KEY` — Claude API key
- `IBKR_FLEX_TOKEN` + `IBKR_FLEX_QUERY_ID` — to re-sync trade history from IBKR

`store.db` is rebuilt from Flex XML archives in `account_data/` via `sync_flex_archive` — no manual export needed.

---

## Testing

```bash
pytest                                        # full suite — 757 unit tests, no IBKR gateway needed
ruff check claudia/ tests/ && mypy claudia/   # lint + type gates

CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest -m live_api   # opt-in; bills real Anthropic API calls
```

The `live_api` tests are skipped by default. They exist because a local schema validator
cannot prove the API accepts a request — three defects that would have returned a 400 on
every call once passed both a documentation review and a green suite. Probe the live API
before adding a JSON Schema keyword or changing a message-role placement.

Live IBKR verification (order staging, gateway flows) is done manually and recorded in
[`docs/project-status.md`](docs/project-status.md) § Live Test Log.
