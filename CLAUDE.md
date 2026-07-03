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

**ibkr_core_mcp** is a direct Python import — not an MCP server. The `ClaudeToolkit`
exposes IBKR tools that drop straight into the Anthropic SDK `tools=` parameter.
TradingView tools are merged in from the `tradingview-mcp` Node.js sidecar (curated
15-tool subset by default — see `_CURATED_TOOLS` in `claudia/tradingview.py`).

---

## Market Calendar

`SQLiteStore.get_market_calendar_context()` injects trading-day awareness into ClaudIA's system prompt at every session start. No API calls — pure pre-built library data.

**20 exchanges covered — full G20 + Eurex (current year + next year, past and future holidays):**

Excludes Russia (XMOS — IBKR suspended most Russian securities since 2022 sanctions) and Argentina (XBUE — capital controls, very limited IBKR access). Saudi Arabia (XSAU) trades Sun–Thu; Fridays appear as "closed" from a Mon–Fri perspective — correct, not a data error.

| Code | Exchange | Region | Why it matters |
|---|---|---|---|
| `XNYS` | NYSE | US | Primary staleness reference, equity order timing |
| `CME` | CME Futures | US | ES, CL, GC — different hours and holiday set vs NYSE |
| `XLON` | LSE London | Europe | European open/close effects on US pre-market |
| `XETR` | Xetra Frankfurt | Europe | EU macro events, German/EU equity flows |
| `XEUR` | Eurex | Europe | DAX futures, EURO STOXX 50 — EU derivatives benchmark |
| `XPAR` | Euronext Paris | Europe | CAC 40, EU large-cap equities |
| `XMIL` | Borsa Italiana | Europe | FTSE MIB, EU peripheral spreads |
| `XTKS` | TSE Tokyo | Asia | Nikkei, yen carry — first major session after US close |
| `XHKG` | HKEX Hong Kong | Asia | China proxy, Hang Seng, dim sum flows |
| `XSHG` | SSE Shanghai | Asia | China A-shares, direct macro signal |
| `XBOM` | BSE Mumbai | Asia | India — fastest-growing G20 equity market |
| `XKRX` | KRX Seoul | Asia | Samsung, TSMC proxy, semiconductor bellwether |
| `XASX` | ASX Sydney | Asia-Pacific | Iron ore, copper — first market to open globally |
| `XTSE` | TSX Toronto | Americas | Oil sands, gold miners |
| `BVMF` | B3 São Paulo | Americas | Brazilian commodities, EM sentiment |
| `XMEX` | BMV Mexico City | Americas | Nearshoring flows, peso/USD dynamics |
| `XJSE` | JSE Johannesburg | Africa | Mining, platinum group metals |
| `XSAU` | Tadawul | Middle East | Oil policy signal, Aramco flows (Sun–Thu week) |
| `XIDX` | IDX Jakarta | SE Asia | Commodities, EM Asia |
| `XIST` | Borsa Istanbul | EMEA | Macro volatility signal, lira dynamics |

**What ClaudIA receives in the system prompt:**
- Today's date and whether it is a trading day (NYSE reference)
- Last and next trading day
- Full holiday list for all 20 exchanges — proactive context for "why is volume low today?"
- **Futures vs Securities distinction** — explicitly injected so ClaudIA never confuses CME and equity schedules:
  - Most CME Globex products trade ~23h/day (Sun 5 PM CT → Fri 4 PM CT), daily 1h maintenance break 4–5 PM CT
  - IBKR routes all CME products via Globex (electronic only — no pit sessions)
  - **CME open when NYSE is closed**: MLK Day, Presidents Day, Memorial Day, Juneteenth, Labor Day, etc. — dynamically computed from exchange_calendars each session
- **CME product group schedule** (`_FUTURES_SCHEDULE` in `store.py`):

| Group | Exchange | Globex Hours (CT) | Key products |
|---|---|---|---|
| Equity Index | CME | Sun 5 PM – Fri 4 PM (~23h) | ES, NQ, RTY, YM |
| Energy | NYMEX | Sun 5 PM – Fri 4 PM (~23h) | CL, NG, RB, HO |
| Metals | COMEX | Sun 5 PM – Fri 4 PM (~23h) | GC, SI, HG |
| Foreign Currency | CME | Sun 5 PM – Fri 4 PM (~23h) | 6E, 6J, 6B, 6A |
| Interest Rates | CBOT | Sun 5 PM – Fri 4 PM (~23h) | ZN, ZB, ZF, ZT |
| Agriculture/Grains | CBOT | Sun 7 PM – Fri 1:20 PM (~17h) | ZC, ZS, ZW — closes at 1:20 PM CT, **not 4 PM** |
| Softs/Livestock | CME/CBOT | Varies — shorter than financials | LE, GF, HE, CC |

**Performance (designed for zero marginal cost):**

| Call | Time |
|---|---|
| First call per process (cold) | ~3.4s — exchange_calendars loads numpy arrays for 20 exchanges once |
| Subsequent calls same day | 0.01ms — process-level date-keyed cache hit |
| Next day / process restart | Recomputes fresh — cache key includes today's date |

Cache lives in `_market_calendar_cache` (module-level dict in `ibkr_core_mcp/store.py`). Key: `(date_str, tuple(exchange_codes))` — auto-invalidates at midnight, no manual expiry needed.

**Staleness check** also uses the NYSE calendar: `stale = newest < last_trading_day`. This correctly handles weekends and holidays — no false stale on Saturdays, no missed sync after a holiday Monday.

---

## Trade Data Architecture

Two complementary sources — each covers what the other cannot:

| Source | Tool | Coverage | Latency |
|---|---|---|---|
| IBKR Flex Web Service | `sync_flex_trades` / `get_trades source='store'` | Full history (years), settled trades | T+1 — yesterday at best |
| IBKR Client Portal REST API | `get_trades source='live'` | Last 6 days, today's intraday | Real-time |

Flex never has today's trades. The live API fills that gap.

**Startup sync decision** (in `app.py → _background_flex_sync`):
1. `stale == False` → skip (newest == last NYSE trading day — calendar-aware, not a fixed day count)
2. Last `flex_sync` log entry < 4h ago → skip (recent attempt, avoid API lockout)
3. Otherwise → sync, log result, back up `store.db` to Drive `account_data/`

**Data stores:**
- `~/.ibkr_core/store.db` — SQLite, all Flex-synced trades (full history from account open); includes `flex_import_log` manifest
- `data/claudia.db` — SQLite, conversation history, sessions, decisions
- Drive `market_data/` — Parquet OHLCV cache
- Drive `account_data/` — Flex XML archives (`ClaudIA_Full_Activity_*.xml` manual, `flex_U*.xml` auto-synced), `store.db` backup

**Flex import integrity** — `verify_flex_import` cross-checks every tradeID in the Drive XML archives against `store.db`. The `flex_import_log` manifest tracks SHA-256, trade count, and `verified_at` per file. Manual archives are pre-validated and never re-verified; auto-synced files are verified by hash on re-check (full tradeID scan only if hash changed). `check_flex_coverage` is an activity distribution report only — gaps reflect genuine inactivity (30-day min hold periods produce 50–68 day gaps), not missing imports.

See [`docs/flex-query-setup.md`](docs/flex-query-setup.md) for full setup and troubleshooting.

### Live Orders Two-Call Pattern

`get_live_orders` (and `diagnose_orders`) use a documented IBKR two-call pattern. Per IBKR Campus documentation, `/iserver/account/orders` behaves like `/iserver/marketdata/snapshot`: the first call instantiates the subscription and returns empty/snapshot data; the second call returns the actual live order list.

**Implementation in `client.py`:**
```python
self._get("/iserver/account/orders?force=true")  # instantiate subscription
time.sleep(1)
data = self._get("/iserver/account/orders")       # retrieve actual data
```

Symptoms and diagnosis:
- `orders: [], snapshot: true` on only one call → missing second call, not a data issue
- Still empty after two calls → possible IBKR session not fully initialized; check `ping()` first
- Mobile/TWS-placed orders: expected to be visible after proper initialization (under test)
- GTC orders from previous days: should appear in the live list regardless of when placed

Source: [IBKR Campus — Request & Modify Orders](https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/)

---

### Market Data Fetch Behavior

`fetch_market_data` uses `get_market_history_paginated()` in `ibkr_core_mcp/client.py`, which calls `GET /iserver/marketdata/history` with automatic pagination for requests exceeding the **1000 data point limit** (verified from official docs). Pagination uses the `startTime` parameter to walk backwards in 1000-calendar-day chunks.

**`_fetch_market_data` in `claude_tools.py` retries up to 3 times (2s delay) on `IBKRAPIError` or empty response** — handles first-call warmup where IBKR returns 404/500 while initializing the subscription for a new symbol. The `with_retry` wrapper in `rate_limiter.py` covers 429/503; warmup errors (404/500) are handled separately at the tool level.

Symptoms and diagnosis:
- First call for a new symbol fails → warmup, auto-retried, transparent
- All retries fail → check account/positions endpoints; if those work, may be a subscription or period/bar validity issue
- Period too long → paginator splits into chunks automatically; if one chunk fails, the merged result will be incomplete

---

## GDrive Sync

`claudia/gdrive_sync.py` — `GDriveSync` class, auto-enabled when `GOOGLE_DRIVE_FOLDER_ID` is set. No new env vars required.

### What syncs

| File | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (first session per process, before DB opens). **Freshness guard:** skipped when the local DB (incl. `-wal` mtime) is newer than the Drive copy's `modifiedTime` — an older Drive copy never overwrites a newer local DB. Stale `-wal`/`-shm` sidecars are removed before the downloaded file lands. |
| `claudia.db` | local → Drive | Session stop (after `close_session`). Uploads a **WAL-consistent snapshot** made with `sqlite3.Connection.backup()` — never the live file, so commits still in `claudia.db-wal` are included and concurrent checkpoints can't tear the upload. |
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
| `download_db` sees older Drive copy | Log warning; keep newer local DB (freshness guard); it syncs to Drive at session end |
| `upload_db` at stop | Log warning; local copy preserved; syncs next session (freshness guard protects it across a process restart) |
| `read_text` for context/principles | Log warning; fall back to local `docs/` files |
| `ping()` (connectivity poll) | Returns `False`; status light turns red; no exception raised |

**Threading note:** `upload_db` uses `threading.RLock` (reentrant) because `_find_file`
calls `_get_service()`, which also acquires the same lock. A plain `Lock` would deadlock
when `upload_db` is called while a session is active.

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

# 6. TradingView sidecar (optional — one-time install)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version to vendor/

# 7. Run ClaudIA
./start-claudia.sh            # recommended: IBKR gateway + ClaudIA
# or:
chainlit run claudia/app.py   # ClaudIA only (in-chat "Start IBKR Gateway" button available)
# → Open http://localhost:8000
#
# TradingView startup (no manual terminal commands needed):
#   - If TradingView Desktop is not running, the welcome message shows
#     a "Launch TradingView" button.
#   - Click it — ClaudIA launches TradingView with --remote-debugging-port=9222,
#     waits up to 30s for CDP port 9222, then reconnects the MCP sidecar.
#   - TV tools become available in the active session without a page reload.
#   - If TV is already running WITHOUT the debug port, the button shows an
#     error with instructions to quit and relaunch.
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
| `CLAUDIA_VOICE_ENABLED` | optional | Reserved — TTS output not yet implemented |
| `FIRECRAWL_API_KEY` | optional | Firecrawl API key — enables `firecrawl_search` and `firecrawl_crawl` tools; keyless free tier works without it (rate-limited) |
| `GDRIVE_WEB_DOCS_FOLDER_ID` | optional | Drive folder for `firecrawl_crawl` saved web docs (`web_docs/` subfolder of root if unset) |
| `CRAWL4AI_PROFILES_DIR` | optional | Directory for Crawl4AI browser login profiles (default: `~/.ibkr_core/crawl4ai_profiles`); used by the Playwright-based fallback scraper in `ibkr_core_mcp/scrape_fallback.py` when Firecrawl returns low-quality content |
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

**Load-time resolution (not per-message):** `ClaudIAAgent._get_system_blocks()` builds the
system prompt **once per session** and caches it — document reads and the `doc_versions`
version-note query happen at that point, never on every prompt. The watchdog in
`ContextLoader` increments `reload_count` on every file-change event; the agent compares its
cached counter against the loader's on each message and rebuilds only when they differ.
Steady-state per-message cost is one integer comparison. This also guarantees the system
prompt is byte-identical across a session's tool-loop turns, which prompt caching (below)
depends on.

**Versioning:** On every session start, a SHA-256 hash of both documents is computed. If
the hash is new, it is automatically registered as the next version (`v1`, `v2`, …) in
`claudia.db → doc_versions`, and a human-readable snapshot is written to
`docs/versions/{label}/`. ClaudIA's system prompt always includes the active version label
so it knows which rules are in effect. If the version changed since the last session, a
`WARNING: v1 → v2` alert appears in chat.

ClaudIA has two tools to reason about version history:
- `list_doc_versions` — enumerate registered versions with dates
- `get_doc_version("v1")` — retrieve the full content of any past version to check for contradictions with current rules

Past conversation history retrieved from memory always includes which document version was active,
so ClaudIA can flag if something discussed under old rules conflicts with the current principles.

---

## Prompt Caching

`claudia/agent.py` marks three `cache_control: {"type": "ephemeral"}` breakpoints on every
`client.messages.stream()` call, following the prefix hierarchy `tools → system → messages`:

| Breakpoint | Helper | Caches |
|---|---|---|
| Last tool definition | `_with_cache_marker` | All tool schemas (`ClaudeToolkit` + TradingView `extra_tools` + `_LOCAL_TOOLS`) |
| System prompt (block form) | `_system_blocks` | Version note, context.md, principles.md, market calendar, `_SAFETY_BLOCK` |
| Last message content block | `_with_history_cache_marker` | Conversation history — rebuilt on a **copy** every call, never mutating the loop's working `messages` list |

**Why 3 breakpoints, not the note's original 2:** caching only tools + system left the
growing conversation re-processed at full price on every tool-loop turn. The messages
breakpoint closes that gap — each call reads the prior prefix at 0.1× and writes only the
newly appended blocks at 1.25×.

**Live-verified** (2026-07-03, exact production request shape): a ~22K-token static prefix
(42+ tool schemas + system prompt) written once (`cache_creation_input_tokens=22047`), then
read at 0.1× on every subsequent call (`cache_read_input_tokens=22047`); an appended
assistant+user turn wrote only its 17-token delta. Full numbers:
[`docs/live-test-log.md`](docs/live-test-log.md#run-2026-07-03-1).

**Health telemetry:** every call logs `prompt cache: created=… read=… uncached=…` at INFO
(`_log_cache_usage`), with a WARNING if both `created` and `read` are zero — the silent-failure
signal for a below-minimum prefix (1,024 tokens on `claude-opus-4-8`) or a misplaced marker.

**What invalidates the cache** (tools cache survives; system+messages caches do not):
- Editing `context.md`/`principles.md` (hot-reload) or a doc-version change — expected, rare
- TradingView sidecar connect/disconnect (`set_tv_bridge` swaps `_extra_tools`)
- A single tool-loop turn adding more than 20 content blocks (10+ parallel tool calls) exceeds
  the 20-block lookback window and silently misses instead of reading — visible as
  `created>0 read=0` in the log line
- Once a session exceeds `_HISTORY_LIMIT=40` messages, the sliding history window shifts the
  messages prefix and that cache misses once per user turn (tools+system unaffected)

Full design rationale, source-verified claims, and the three-round consistency review:
[`docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md`](docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md) ·
[`docs/2026-07-03-llm-best-practices-sources.md`](docs/2026-07-03-llm-best-practices-sources.md).

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

---

## Conversation Memory

All interactions are stored in `data/claudia.db` (separate from ibkr_core_mcp's `~/.ibkr_core/store.db`).

| Table | Contents |
|---|---|
| `sessions` | One row per Chainlit session, with start/end time, document hash, and `doc_version` |
| `messages` | Full message history (user, assistant, tool calls and results) — primary memory store |
| `decisions` | User-directed trade proposals surfaced by ClaudIA — each tagged with `doc_version`. ClaudIA does not decide to trade; it surfaces a proposal when directed by the user. The user decides at the button → Touch ID → confirmation dialog. |
| `doc_versions` | Versioned snapshots of `context.md` + `principles.md` — full text, hash, date |

(A `relationships` table and a decisions FTS index were removed 2026-07-03 — never wired to any caller; symbol-level knowledge belongs to the planned knowledge layer. Existing DBs are migrated safely: derived index dropped, `relationships` dropped only if empty.)

**Search:** ClaudIA uses SQLite FTS5 to search full conversation history. Ask: *"What did we discuss about NVDA last month?"* The `search_past_conversations` tool searches all messages across all sessions. Results include the doc version active at the time.

**Version snapshots** are also written to `docs/versions/{label}/` as human-readable files for reference.

---

## Order Staging Flow

ClaudIA **cannot** place orders autonomously. When ClaudIA suggests a trade:

1. ClaudIA embeds an `order-proposal` JSON block in its response.
2. `agent.py` strips the block and calls `order_flow.render_order_proposal()`.
3. A Chainlit message appears with full order details + **"Stage this order"** button.
4. User clicks — `execute_staged_order()` fires in `order_flow.py`.
5. **Gate 1:** Apple Touch ID / biometric authentication (`ibkr_core_mcp.human_auth`).
6. **Gate 2:** AppKit NSAlert dialog (green banner for BUY, red for SELL) with full order details, **SEND TO IBKR** button, and 60-second auto-cancel. Return key is disabled to prevent accidental confirm.
7. `IBKRClient.place_order()` sends the order to IBKR only after both gates pass.

### Order proposal format

```json
{
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 100.00,
  "stop_price": null,
  "tif": "GTC",
  "sec_type": "STK",
  "reason": "one-line rationale"
}
```

`sec_type` values: `STK` (default), `FUT`, `OPT`, `CASH`.  
`order_type` values: `MKT`, `LMT`, `STP`, `STOP_LIMIT`, `MIDPRICE`, `TRAIL`, `TRAILLMT`.  
`tif` values: `DAY`, `GTC`, `OPG`, `IOC`.

### ORDER PARAMETER IMMUTABILITY (non-overridable)

ClaudIA must use the **exact values** provided by the user for every order parameter (symbol, action, quantity, price, order type, TIF). No rounding, substitution, or "helpful" adjustments. If a parameter seems risky, ClaudIA explains in text — the proposal block still uses the user's value. A parameter change requires explicit user approval in a follow-up message.

Enforced in `claudia/agent.py` system prompt and in memory (`feedback-order-parameter-immutability.md`).

### Order body field spec (from IBKR CP API docs, verified 2026-07-02)

Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#place-order

| Field | Type | Required? | Notes |
|---|---|---|---|
| `conid` | int | yes* | *or `conidex`; SMART-routes when set |
| `orderType` | str | yes | `LMT` \| `MKT` \| `STP` \| `STOP_LIMIT` \| `MIDPRICE` \| `TRAIL` \| `TRAILLMT` |
| `side` | str | yes | `"BUY"` \| `"SELL"` |
| `tif` | str | yes | `DAY` \| `GTC` \| `OPG` \| `IOC` \| `PAX` (crypto) |
| `quantity` | int | yes | whole shares/contracts only |
| `price` | float | LMT / STOP_LIMIT | limit price |
| `auxPrice` | float | STOP_LIMIT / TRAILLMT | stop price |
| `acctId` | str | no | defaults to first account |
| `ticker` | str | no | underlying symbol — valid IBKR field, not stripped |
| `cOID` | str | no | customer order ID; max 64 chars; unique per 24h |
| `listingExchange` | str | no | default: SMART routing |
| `outsideRTH` | bool | no | allow execution outside regular trading hours |
| `manualIndicator` | bool | **FUT/FOP** | CME Rule 536-B — required since May 1, 2025 |
| `extOperator` | str | **FUT/FOP** | CME Rule 536-B — identifies submitting system |

Display-only fields use `_` prefix (`_companyName`, `_multiplier`) — stripped by `client.py` before the API call. `ticker` is **not** stripped (valid IBKR field).

### Instrument-specific paths

**Equities (STK):**
- Conid resolved via `IBKRClient.search_contract()` → `/iserver/secdef/search`
- `manualIndicator` / `extOperator` omitted (equity orders; would cause 400 if included)

**Futures (FUT):**
- Conid resolved via `IBKRClient.get_futures()` → `/trsrv/futures`, front month picked by lowest `expirationDate`
- `/iserver/secdef/search` does **not** support FUT — do not use it for futures conid resolution
- `manualIndicator: True` and `extOperator: "ClaudIA"` added automatically (CME Rule 536-B, mandatory since May 1, 2025)
- Contract multiplier fetched from `/trsrv/futures` response and passed as `_multiplier` display field
- Gate 2 dialog shows correct notional: `price × qty × multiplier`

**Futures Options (FOP):**
- Same `manualIndicator` + `extOperator` requirement as FUT
- Conid resolution via options chain flow (not yet implemented in `order_flow.py`)

Source (536-B requirement): https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/

---

## Price Alerts

Alerts are managed exclusively through IBKR's native server-side alert system — they fire even when ClaudIA is not running, and appear on the IBKR mobile app.

ClaudIA has five alert tools (via `ibkr_core_mcp.ClaudeToolkit`):

| Tool | What it does |
|---|---|
| `create_price_alert` | Resolves symbol → conid, posts alert to IBKR server |
| `get_alerts` | List all configured alerts with status |
| `modify_price_alert` | Update threshold or direction on an existing alert |
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
(78 MCP tools + `tv` CLI, 4.1k stars, last updated April 2026). ClaudIA exposes a curated
16-tool subset by default to control token cost; the full set is available via `bridge.get_all_tools()`.

**Normal startup — no manual terminal commands needed:**
1. Run `./start-claudia.sh` (or `chainlit run claudia/app.py`).
2. If TradingView Desktop is not running, the welcome message shows a **"Launch TradingView"** button.
3. Click it — ClaudIA calls `launch_tradingview()` which runs
   `open -a "TradingView" --args --remote-debugging-port=9222`, polls for CDP port 9222
   up to 30s, then reconnects the MCP sidecar. TV tools become available without a page reload.
4. If TV is already running **without** the debug port, the button shows an error with
   instructions to quit TV and relaunch — ClaudIA cannot inject the debug flag into a running process.

**Python 3.14 compatibility note:** The sidecar now starts successfully even when TV Desktop
is not running (fixed 2026-06-30: `AsyncIOTaskInfo.__init__` patched in `app.py` to handle
`current_task()` returning `None` — the 5th Python 3.14/anyio compat patch). When TV Desktop
is not running: sidecar starts, tools are listed, but tool calls fail at the CDP layer.
ClaudIA falls back to screenshot mode (drag/paste a chart screenshot into chat).
The anyio upstream bug (`_MemoryObjectItemReceiver` + `get_current_task`) is unfixed in
anyio 4.14.1 and MCP 1.28.1 as of 2026-06-30.

Binary discovery order (`_find_tv_mcp_bin()`):
1. `TRADINGVIEW_MCP_PATH` env var (validated: file must exist and end in `.js`)
2. `tradingview-mcp` on PATH
3. `~/.tradingview-mcp/src/server.js` (pure JS layout — current)
4. `~/.tradingview-mcp/build/index.js` (TypeScript build output — legacy)
5. `vendor/tradingview-mcp/src/server.js` (archived fallback, needs `node_modules/`)
6. `vendor/tradingview-mcp/index.js` (legacy single-bundle archive)

**PineScript:** ClaudIA generates PineScript v5 directly. Use the **"Inject into TradingView"**
button to paste it into the Pine Editor via the `pine_set_source` MCP tool.

**Curated 16-tool subset** (`_CURATED_TOOLS` in `claudia/tradingview.py`):

Verified against live sidecar 2026-06-30. Tool descriptions are provided by the sidecar
at runtime via MCP `list_tools()` — they appear in the Anthropic `tools=` parameter and
are the only documentation ClaudIA receives about what each tool does.

| Category | Tools |
|---|---|
| Chart reading | `chart_get_state`, `quote_get`, `data_get_ohlcv`, `data_get_study_values` |
| Chart control | `chart_set_symbol`, `chart_set_timeframe`, `indicator_set_inputs` |
| Pine Script IDE | `pine_set_source`, `pine_smart_compile`, `pine_get_errors`, `pine_get_source` |
| Strategy results | `data_get_strategy_results`, `data_get_equity` (equity curve), `data_get_trades` |
| Utility | `tv_health_check`, `capture_screenshot` |

**Upgrading the sidecar:**
```bash
git -C ~/.tradingview-mcp pull
npm -C ~/.tradingview-mcp install
# Restart ClaudIA — startup log will show commit and warn of any renamed tools:
#   INFO  tradingview-mcp sidecar: .../server.js (commit abc1234)
#   INFO  tradingview-mcp connected: 78 total tools, 16 curated
#   WARNING  curated tools not found in sidecar: {data_get_equity_curve}  ← rename detected
# If a WARNING appears, update _CURATED_TOOLS in claudia/tradingview.py, then:
./scripts/archive-tv-mcp.sh    # snapshot the new working version to vendor/
```

**Version detection at startup (`claudia/tradingview.py → TradingViewBridge.start()`):**
- Logs sidecar binary path + git commit (best-effort; `unknown` if running from vendor/)
- Logs total tool count and curated count
- Emits a `WARNING` if any name in `_CURATED_TOOLS` is absent from the sidecar — detects
  silent tool renames between sidecar versions (e.g. `data_get_equity_curve` → `data_get_equity`)
- Tool descriptions and input schemas come from the sidecar's `list_tools()` — ClaudIA has
  no hardcoded schema; what the sidecar reports is what Claude receives in `tools=`
- Schema drift (a tool exists but its parameters changed) is not auto-detected — check the
  [sidecar changelog](https://github.com/tradesdontlie/tradingview-mcp) after any `git pull`

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
| IBKR | `GET /tickle` → parse `iserver.authStatus.authenticated && connected` | Sends "reconnected" / "disconnected" chat alert |
| GDrive | `GDriveSync.ping()` — live `files().list` round-trip | Sends alert on state change |
| TradingView | TCP connect to port 9222 | Sends alert on state change |

**IBKR:** `/tickle` returns HTTP 200 even when the session is not authenticated (e.g. before
login or after timeout). The check parses `iserver.authStatus` from the JSON body and
requires both `authenticated: true` and `connected: true`. Side effect: the `/tickle` call
also resets the IBKR session keepalive timer, so polling every 60s prevents auto-logout.

**GDrive:** `GDriveSync.ping()` is called every poll cycle when Drive sync is enabled. Falls
back to token-file existence check if `GDriveSync` was not wired (e.g.
`GOOGLE_DRIVE_FOLDER_ID` not set). The green light reflects real API reachability, not
just credential presence.

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

## API Reference — Docs First

**Never assume API behavior, error codes, endpoint paths, or field names from memory. Always verify against official documentation first. This applies to every external API claudia_ui touches.**

**Protocol:** Use `WebFetch` to load the relevant doc page before writing any error message, fix, or diagnosis. Cite the source URL in the error string and in the commit message.

This rule exists because two bugs in one session were caught instantly by checking the official docs — and had gone undetected for months because nobody checked:
- Flex error 1001 mislabeled twice (rate limit → auth failure → actually transient generation failure)
- Flex endpoint URL wrong from day one (`gdcdyn` vs `ndcdyn`) — Flex API never worked until the doc was read

**IBKR Client Portal API** (`ibkr_core_mcp/client.py`, `claude_tools.py`)

| Topic | Official source |
|---|---|
| Client Portal API reference | https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/ |
| Web API reference | https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-ref/ |
| Orders / modify (two-call pattern) | https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/ |
| IBKR Campus (general) | https://www.interactivebrokers.com/campus/ibkr-api-page/ |

**IBKR Flex Web Service** (`ibkr_core_mcp/flex_query.py`)

| Topic | Official source |
|---|---|
| Flex Web Service setup (endpoints, headers) | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3.htm |
| Flex error codes (all 21 codes) | https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm |

**Anthropic API** (`claudia/agent.py`)

Note: `docs.anthropic.com` 301-redirects to `platform.claude.com` (verified 2026-07-02). New references should use the canonical `platform.claude.com` host.

| Topic | Official source |
|---|---|
| Messages API (streaming, tool use) | https://docs.anthropic.com/en/api/messages |
| Tool use reference | https://docs.anthropic.com/en/docs/build-with-claude/tool-use |
| Model names and capabilities | https://docs.anthropic.com/en/docs/about-claude/models |
| Prompt caching (breakpoints, pricing, lookback, invalidation) | https://platform.claude.com/docs/en/build-with-claude/prompt-caching |
| Streaming events (`message_start` usage shape) | https://platform.claude.com/docs/en/build-with-claude/streaming |
| Context engineering for agents (just-in-time retrieval, memory, compaction) | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents |

Scraped-evidence convention: design docs and plans that assert API behavior carry a claim→source table with verbatim quotes and scrape dates — see `docs/2026-07-03-llm-best-practices-sources.md` for the reference example.

**Google Drive API v3** (`claudia/gdrive_sync.py`)

| Topic | Official source |
|---|---|
| Drive API v3 reference | https://developers.google.com/drive/api/reference/rest/v3 |
| Files: upload / download | https://developers.google.com/drive/api/guides/manage-uploads |

**TradingView MCP** (`claudia/tradingview.py`)

| Topic | Official source |
|---|---|
| tradingview-mcp tool list and usage | https://github.com/tradesdontlie/tradingview-mcp |
| Chrome DevTools Protocol | https://chromedevtools.github.io/devtools-protocol/ |

**Chainlit** (`claudia/app.py`)

| Topic | Official source |
|---|---|
| Chainlit API reference (Message, Action, Step, Audio) | https://docs.chainlit.io/api-reference/message |
| Chainlit configuration (chainlit.yaml) | https://docs.chainlit.io/backend/config |
| Chainlit custom CSS / JS | https://docs.chainlit.io/customisation/custom-js |

**Standard libraries used in claudia_ui**

| Library | Used in | Official reference |
|---|---|---|
| `requests` | `claudia/agent.py` (`fetch_web_page` tool) | https://docs.python-requests.org/ |
| `html2text` | `claudia/agent.py` (HTML → Markdown for web fetch) | https://github.com/Alir3z4/html2text |
| `watchdog` | `claudia/context_loader.py` (file system event monitoring) | https://watchdog.readthedocs.io/ |
| `mcp` | `claudia/tradingview.py` (MCP stdio client for tradingview-mcp sidecar) | https://github.com/modelcontextprotocol/python-sdk |

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

Tools in ibkr_core_mcp (shipped):
- `preview_order` — read-only whatif order preview (`ibkr_core_mcp/claude_tools.py`)
- `get_pnl` — real-time partitioned P&L (`ibkr_core_mcp/claude_tools.py`)
