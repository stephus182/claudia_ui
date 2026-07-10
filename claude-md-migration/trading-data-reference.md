# Trading Data & Connectivity Reference

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

**Flex import integrity** — `verify_flex_import` cross-checks every tradeID in the Drive XML
archives against `store.db`. The `flex_import_log` manifest tracks SHA-256, trade count, and
`verified_at` per file. Manual archives are pre-validated and never re-verified; auto-synced
files are verified by hash on re-check (full tradeID scan only if hash changed).
`check_flex_coverage` is an activity distribution report only — gaps reflect genuine inactivity
(30-day min hold periods produce 50–68 day gaps), not missing imports.

See `docs/flex-query-setup.md` for full setup and troubleshooting.

## Live Orders Two-Call Pattern

`get_live_orders` (and `diagnose_orders`) use a documented IBKR two-call pattern. Per IBKR
Campus documentation, `/iserver/account/orders` behaves like `/iserver/marketdata/snapshot`:
the first call instantiates the subscription and returns empty/snapshot data; the second call
returns the actual live order list.

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

Source: https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/

## Execution-Triggered P&L Checks

`claudia/execution_listener.py`'s `ExecutionListener` runs one persistent WebSocket
connection subscribed only to IBKR's `str` (trade executions) topic — capturing fills
from any origin (mobile, TWS, web, API), not just trades ClaudIA itself places. On each
execution, it transiently subscribes to `spl` (P&L), waits (bounded by a 10s timeout)
for exactly one `PnLUpdate`, records it via `SQLiteStore.record_pnl_snapshot()`, and
unsubscribes — returning to its executions-only steady state.

This replaced an earlier design (`PnLStreamer`, see git history and
`docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md`) that stayed
continuously subscribed to `spl` and wrote every tick — judged overkill for a chat
assistant where "live" never needed sub-second freshness, and it grew `pnl_snapshots`
unboundedly for data nobody read between trades.

Reconciliation: account P&L is cumulative (not per-trade), so one snapshot after the
*last* known execution is sufficient — but no execution is silently dropped as a
trigger. If more executions arrive while a capture round is already waiting for its
`PnLUpdate`, `ExecutionListener` runs one more round after the current one settles,
repeating until a round completes with zero additional executions observed during it.
Internally, a background "pump" task drains the WebSocket into an `asyncio.Queue` that
both the outer execution loop and the P&L capture read from — this avoids a subtle bug
where cancelling a shared async generator's `__anext__()` (e.g. on a capture timeout)
permanently exhausts it; cancelling a `queue.get()` waiter has no such effect.

ClaudIA does **not** run `ibkr_core_mcp.mcp_server` — `ExecutionListener` is a
self-contained subscriber, consistent with ClaudIA's direct-import architecture. Retry/backoff
on disconnect mirrors `ibkr_core_mcp.mcp_server._stream_loop_with_retry`'s shape (delays: 5s,
10s, 30s, 60s).

Both surfaces render via the same `format_pnl_snapshot()` helper
(`claudia/execution_listener.py`) so they can't drift out of sync — any individually
`None` numeric field renders as "n/a" rather than discarding the whole snapshot.

Surfaced two ways:
- **`get_live_pnl` tool** (`claudia/agent.py`, local tool) — on-demand, reads
  `SQLiteStore.get_latest_pnl()` directly.
- **Opening status block** (`claudia/app.py::on_chat_start`) — an "Account P&L" section
  in the session-start welcome message, reflecting P&L as of the last recorded
  execution (not literally "live" — refreshed only when a trade happens).

Design spec: `docs/superpowers/specs/2026-07-07-execution-triggered-pnl-design.md`

## Market Data Fetch Behavior

`fetch_market_data` uses `get_market_history_paginated()` in `ibkr_core_mcp/client.py`, which
calls `GET /iserver/marketdata/history` with automatic pagination for requests exceeding the
**1000 data point limit** (verified from official docs). Pagination uses the `startTime`
parameter to walk backwards in 1000-calendar-day chunks.

**`_fetch_market_data` in `claude_tools.py` retries up to 3 times (2s delay) on
`IBKRAPIError` or empty response** — handles first-call warmup where IBKR returns 404/500
while initializing the subscription for a new symbol. The `with_retry` wrapper in
`rate_limiter.py` covers 429/503; warmup errors (404/500) are handled separately at the tool
level.

Symptoms and diagnosis:
- First call for a new symbol fails → warmup, auto-retried, transparent
- All retries fail → check account/positions endpoints; if those work, may be a subscription or period/bar validity issue
- Period too long → paginator splits into chunks automatically; if one chunk fails, the merged result will be incomplete

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

## Price Alerts

Alerts are managed exclusively through IBKR's native server-side alert system — they fire
even when ClaudIA is not running, and appear on the IBKR mobile app.

ClaudIA has five alert tools (via `ibkr_core_mcp.ClaudeToolkit`):

| Tool | What it does |
|---|---|
| `create_price_alert` | Resolves symbol → conid, posts alert to IBKR server |
| `get_alerts` | List all configured alerts with status |
| `modify_price_alert` | Update threshold or direction on an existing alert |
| `delete_alert` | Remove an alert by ID |
| `activate_alert` | Toggle an alert on/off without deleting it |

There is no background polling loop in claudia_ui — IBKR delivers the notification directly
to the mobile app and desktop.
