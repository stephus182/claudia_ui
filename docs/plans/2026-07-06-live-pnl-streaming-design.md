# Design: Live P&L Streaming in ClaudIA

## Context

`ibkr_core_mcp` (commit `3b83db0`) added two new IBKR WebSocket topics to
`IBKRWebSocket`: `str` (live trade executions) and `spl` (live account P&L).
The P&L side persists ticks into a new `pnl_snapshots` SQLite table via
`SQLiteStore.record_pnl_snapshot()`/`get_latest_pnl()`, and is exposed through
a new `ibkr://pnl/live` MCP resource — but only when something runs
`ibkr_core_mcp.mcp_server --stream`.

ClaudIA (`claudia_ui`) does not run the MCP server today. Per its own
architecture (`CLAUDE.md`): "`ibkr_core_mcp` is a direct Python import — not
an MCP server." ClaudIA imports `ClaudeToolkit`/`IBKRClient`/`SQLiteStore`
directly and calls tools synchronously; its only MCP-client code is the
unrelated `tradingview-mcp` Node sidecar. Nothing today keeps `pnl_snapshots`
populated for ClaudIA's `store.db`.

This spec adds a self-contained P&L streaming path inside ClaudIA: a
background WebSocket subscriber that keeps `pnl_snapshots` populated, plus a
tool and an opening-status-block line that surface the latest snapshot to the
user/agent. No `ibkr_core_mcp` changes are required — `IBKRWebSocket`,
`PnLUpdate`, `record_pnl_snapshot()`, and `get_latest_pnl()` already exist.

## Decisions (already made, via brainstorming)

1. **Data source**: ClaudIA runs its own dedicated P&L WebSocket subscription
   — it does not spawn or talk to `ibkr_core_mcp.mcp_server` as an MCP
   sidecar. This matches ClaudIA's direct-import architecture and avoids
   introducing a second architectural pattern (MCP client) for IBKR data when
   one doesn't otherwise exist in this codebase.
2. **Surfacing**: both a new agent tool (`get_live_pnl`, for on-demand
   questions) and a line in the existing session-start opening status block
   (for passive visibility) — not a live-refreshing Chainlit UI element (out
   of scope; biggest engineering lift for the least differentiated value
   given ClaudIA's existing "point-in-time snapshot at session start" pattern
   for account summary/positions/orders).
3. **Scope**: P&L only. Live trade executions (`str` topic) are explicitly
   out of scope for this pass — Flex + the existing `get_trades(source='live')`
   REST path already cover ClaudIA's trade-history needs, and mixing WS
   executions into that flow is a separate design decision or later work.

## Architecture

```
on_chat_start
    ↓
_get_pnl_streamer()  (process-level singleton, lock-guarded — same shape as _get_tv_bridge())
    ↓
PnLStreamer.start()  (claudia/pnl_stream.py)
    ↓ owns an asyncio.Task running _run() forever, retry-with-backoff on error
IBKRWebSocket (ibkr_core_mcp.streaming)
    ↓ subscribe_pnl() → async for item in ws.listen()
PnLUpdate → toolkit._store.record_pnl_snapshot(...)   (ibkr_core_mcp.SQLiteStore, store.db)
    ↓
get_latest_pnl()  ←──────────────┬── get_live_pnl tool (agent.py _LOCAL_TOOLS)
                                  └── opening status block (app.py on_chat_start)
```

## Components

### 1. `claudia/pnl_stream.py` — `PnLStreamer`

New module. Not a rewrite of `mcp_server._stream_loop` — that function also
dispatches `LiveQuote` (price alerts) and `TradeExecution`, neither of which
ClaudIA needs here. `PnLStreamer` is P&L-only, following the existing
`ConnectivityChecker` background-task shape (`status.py`):

```python
class PnLStreamer:
    """Background WebSocket subscriber that keeps SQLiteStore.pnl_snapshots
    populated with live account P&L ticks (ibkr_core_mcp spl topic).

    Runs for the life of the process — one subscription shared across all
    concurrent Chainlit sessions, since IBKR account P&L is account-wide,
    not session-scoped.
    """

    def __init__(self, gateway_url: str, store: "SQLiteStore") -> None:
        self._gateway_url = gateway_url
        self._store = store
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_with_retry())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_with_retry(self) -> None:
        """Retry forever with capped exponential backoff on error.
        CancelledError propagates immediately (clean shutdown, no retry)."""
        attempt = 0
        while True:
            try:
                await self._run_once()
                attempt = 0  # clean exit from _run_once (shouldn't normally happen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = min(2 ** attempt, 60)
                log.warning("PnLStreamer error (attempt %d), retrying in %ds: %s",
                            attempt + 1, delay, type(exc).__name__)
                await asyncio.sleep(delay)
                attempt += 1

    async def _run_once(self) -> None:
        import requests
        from ibkr_core_mcp.auth import BrowserCookieAuth
        from ibkr_core_mcp.streaming import IBKRWebSocket, PnLUpdate

        session = requests.Session()
        BrowserCookieAuth().apply(session)
        cookie = session.headers.get("Cookie", "")

        ws = IBKRWebSocket(self._gateway_url, cookie)
        try:
            await ws.connect()
            log.info("PnLStreamer: WebSocket connected")
            await ws.subscribe_pnl()
            async for item in ws.listen():
                if isinstance(item, PnLUpdate):
                    self._store.record_pnl_snapshot(
                        account=item.account, row_type=item.row_type,
                        dpl=item.dpl, nl=item.nl, upl=item.upl,
                        uel=item.uel, mv=item.mv,
                    )
        finally:
            await ws.disconnect()
```

(Illustrative — final code adjusts to match this repo's logging setup and
type-checking conventions.)

### 2. Lifecycle wiring — `app.py`

Same pattern as `_get_tv_bridge()`/`_tv_bridge_lock`:

```python
_pnl_streamer: PnLStreamer | None = None
_pnl_streamer_lock = _asyncio.Lock()

async def _get_pnl_streamer() -> PnLStreamer:
    global _pnl_streamer
    async with _pnl_streamer_lock:
        if _pnl_streamer is None:
            toolkit = _get_toolkit()
            streamer = PnLStreamer(_config.gateway_url, toolkit._store)
            await streamer.start()
            _pnl_streamer = streamer
    return _pnl_streamer
```

Called from `on_chat_start` (best-effort — failure to start must not block
session start, same as the existing `ibkr_offline` handling for the opening
status block). Not stopped in `on_stop` — persists for process lifetime.

### 3. `get_live_pnl` tool — `agent.py`

Added to `_LOCAL_TOOLS` (alongside `list_doc_versions`, `fetch_web_page`,
etc.) and dispatched in `_handle_local_tool` — it reads
`self._toolkit._store.get_latest_pnl()` directly (the `ibkr_core_mcp.
SQLiteStore` instance), not via `toolkit.execute()`, since this reads
ClaudIA's own streaming state rather than calling an IBKR REST endpoint.

```python
{
    "name": "get_live_pnl",
    "description": (
        "Get the latest streamed account P&L snapshot (daily P&L, unrealized P&L, "
        "net liquidity, excess liquidity, market value) from ClaudIA's live WebSocket "
        "P&L subscription. Use when the user asks for current/live/real-time P&L. "
        "For historical performance analysis use get_analytics instead."
    ),
    "input_schema": {"type": "object", "properties": {}},
}
```

Handler:
```python
if name == "get_live_pnl":
    latest = self._toolkit._store.get_latest_pnl()
    if latest is None:
        return "Live P&L not yet available — the P&L stream may still be connecting, or no snapshot has been recorded yet."
    return (
        f"Live P&L ({latest['account']}):\n"
        f"Daily P&L: {latest['dpl']:+.2f} | Unrealized: {latest['upl']:+.2f} | "
        f"Net Liquidity: {latest['nl']:.2f} | Excess Liquidity: {latest['uel']:.2f} | "
        f"Market Value: {latest['mv']:.2f}"
    )
```

Any of the numeric fields can be `None` (per `PnLUpdate`'s tolerant parsing)
— the handler formats missing fields as `"n/a"` rather than raising on a
format-spec `TypeError`.

### 4. Opening status block — `app.py::on_chat_start`

Extends the existing `status_block` assembly (currently Account
Summary/Positions/Live Orders) with a fourth section, following the same
conditional pattern already used for `trade_status`/`ibkr_offline`:

- IBKR offline → omit the section (or one line noting it's unavailable,
  matching the existing offline message tone).
- IBKR online, no snapshot yet → `"*Live P&L stream connecting…*"`.
- IBKR online, snapshot present → same formatted line as the tool output.

## Testing

Mirrors `tests/test_status.py`'s style (fake/stub WS, `pytest-asyncio`):

- `PnLStreamer`:
  - a `PnLUpdate` from a fake `listen()` results in exactly one
    `record_pnl_snapshot` call with the right fields.
  - a raised `Exception` from `_run_once` triggers retry (not propagation);
    `asyncio.CancelledError` propagates immediately, no retry.
  - `stop()` cancels the task cleanly (no unhandled `CancelledError`).
- `get_live_pnl` tool handler: populated snapshot case; `None` (never
  recorded) case; a snapshot with some `None` numeric fields formats as
  `"n/a"` rather than raising.
- Opening status block: no new tests. `on_chat_start` has no existing unit
  test coverage today (`trade_status`, `status_block`, `trade_context`, and
  `_background_flex_sync` are all inline and untested), so the new P&L
  section follows the same existing convention — inline, alongside the other
  status sections — rather than introducing a one-off testable-helper
  extraction just for this addition. `PnLStreamer` and the `get_live_pnl`
  tool handler tests above cover the underlying data path
  (`record_pnl_snapshot` → `get_latest_pnl`); the status block is a thin,
  independently-uncovered display of the same already-tested data, matching
  how the other three sections in that block are handled today.

## Out of scope

- Live trade executions (`str` topic) — not wired into ClaudIA in this pass.
- A live-refreshing Chainlit UI element/ticker — the tool + opening-status-line
  combination is the chosen surface for now; a persistent UI widget is a
  possible future iteration if point-in-time reads prove insufficient.
- Per-account filtering in `get_live_pnl` — ClaudIA is single-account; no
  `account` parameter is exposed on the tool.
