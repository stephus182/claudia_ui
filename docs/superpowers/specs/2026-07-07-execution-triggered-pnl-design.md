# Design: Execution-Triggered P&L Checks (supersedes always-on streaming)

## Context

`docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md` (implemented in
`claudia_ui` commit `636f8c6`) added `PnLStreamer`: a background WebSocket subscriber
that stayed continuously subscribed to IBKR's `spl` (P&L) topic for the life of the
process, writing every tick into `SQLiteStore.pnl_snapshots`.

On reflection (this session), that design was judged overkill: it ran a persistent
background subscription and wrote every incoming tick to SQLite regardless of whether
anything meaningful had happened — unbounded table growth for data nobody reads between
trades, for a chat assistant where "live" never needed sub-second freshness. The owner's
stated goal: replace the always-on stream with an **execution-triggered check** — when a
trade happens (any origin), fetch P&L once, record it, done. No continuous polling, no
continuous WebSocket-driven writes.

This spec replaces `PnLStreamer`/`claudia/pnl_stream.py` with `ExecutionListener`/
`claudia/execution_listener.py`. `get_live_pnl` (the tool) and the opening status block
are **unchanged** — both only ever read `SQLiteStore.get_latest_pnl()`; this spec only
changes how that table gets populated.

## Decisions (already made, via reflection this session)

1. **Trigger scope: any execution, any origin** (mobile/TWS/web/API), not just trades
   ClaudIA itself places. Ruled out two alternatives first:
   - Polling `get_trades(source='live')` (REST) to diff for new executions — rejected
     because `ibkr_core_mcp`'s own docs (cited in this repo's CLAUDE.md) say that
     endpoint is "advised to call... once per session"; polling it every N seconds to
     watch for new fills would violate that documented guidance directly, and this
     codebase treats "docs first, never assume" as a hard rule after two past incidents
     from exactly that kind of assumption.
   - Triggering only off ClaudIA's own `place_order_and_confirm`/`modify_order_and_confirm`
     completions (`claudia/order_flow.py`) — rejected because it misses mobile/TWS/web-placed
     trades entirely, and the owner explicitly wants any-origin coverage.
   - Chosen: the WebSocket `str` (trade executions) topic — already implemented in
     `ibkr_core_mcp` (`IBKRWebSocket.subscribe_executions()`), documented to capture all
     origins, and (unlike continuous `spl`) a sparse, meaningful signal — a trade
     actually happening — rather than a constant tick stream.
2. **One connection, not two.** `ExecutionListener` keeps a single persistent WebSocket
   connection subscribed to `str` only, for the life of the process (same singleton
   shape as the old `PnLStreamer`/the existing `ConnectivityChecker`). It does **not**
   stay subscribed to `spl` — `spl` is subscribed/unsubscribed transiently, once per
   capture round, on the same connection.
3. **P&L capture is a one-shot check, not a filtered stream.** On each `TradeExecution`,
   `ExecutionListener` subscribes to `spl`, waits (bounded by a timeout) for exactly one
   `PnLUpdate`, records it, and unsubscribes — returning the connection to its
   executions-only steady state. This is a real request/response-shaped "check", not a
   continuously-open `spl` subscription with writes gated by a time window (an earlier,
   rejected alternative from this same discussion).
4. **Reconciliation, not drop-on-collision.** Since account P&L is cumulative (not
   per-trade), one snapshot taken after the *last* known execution is sufficient — no
   need for one snapshot per execution. But no execution may be silently ignored as a
   trigger: if additional executions arrive while a capture round is already waiting for
   its `PnLUpdate`, the listener runs one more capture round after the current one
   settles, repeating until a round completes with zero additional executions observed
   during it. Explicitly not over-engineered for high-frequency trading — the owner
   confirmed this is not a high-frequency tool, so the "run one more round" path will
   almost always execute zero or one extra time in practice, but is correct if a burst
   of near-simultaneous fills happens.
5. **Trade executions themselves are not persisted by this component.** `TradeExecution`
   messages are used purely as a trigger signal. Persisting executions into
   `SQLiteStore.trades` remains explicitly out of scope (same decision as the original
   2026-07-06 spec) — Flex sync + `get_trades(source='live')` already cover trade
   history; this component's only side effect is calling `record_pnl_snapshot()`.
6. **Rename, don't leave a stale name.** `claudia/pnl_stream.py` → `claudia/execution_listener.py`,
   `PnLStreamer` → `ExecutionListener`. `format_pnl_snapshot()` (already correct, no
   dependency on how the row was captured) moves to the renamed file unchanged.

## Architecture

```
on_chat_start
    ↓
_get_execution_listener()  (process-level singleton, same shape as the old _pnl_streamer)
    ↓
ExecutionListener.start()  → asyncio.Task running _run_with_retry() forever
    ↓ (retry/backoff on error: 5s, 10s, 30s, 60s — unchanged from PnLStreamer)
_run_once():
    connect, subscribe_executions()  [str topic only — steady state]
    loop: pull next item from the connection
        TradeExecution → _capture_pnl_until_settled(ws, listen_iter)
    (any other exception → propagates to _run_with_retry for reconnect)

_capture_pnl_until_settled(ws, listen_iter):
    while _capture_pnl_once(ws, listen_iter) is True:
        pass   # another execution landed mid-round — run one more, fresh round

_capture_pnl_once(ws, listen_iter, timeout=10s) -> saw_extra_execution: bool:
    subscribe_pnl()
    try:
        loop, bounded by timeout:
            PnLUpdate      → record_pnl_snapshot(); return saw_extra_execution
            TradeExecution → saw_extra_execution = True; keep waiting
            timeout elapses → log warning; return saw_extra_execution
    finally:
        unsubscribe_pnl()   # best-effort; errors here must not mask an
                             # exception already propagating from the try block
```

`get_live_pnl` (agent.py) and the opening status block (app.py) are unchanged consumers
of `SQLiteStore.get_latest_pnl()` / `format_pnl_snapshot()` — this spec only changes
`ExecutionListener`'s internals and its two call sites' imports/singleton naming.

## Components

### 1. `claudia/execution_listener.py` (renamed from `claudia/pnl_stream.py`)

- `format_pnl_snapshot()` — moves here unchanged (already agnostic to how a row was
  captured).
- `ExecutionListener` class — same public shape as `PnLStreamer` (`__init__(gateway_url,
  store)`, `start()`, `async stop()`), same `_RETRY_DELAYS = [5, 10, 30, 60]` and
  `_run_with_retry()` logic (verbatim — CancelledError propagates immediately, clean
  WebSocket-closed-cleanly case retries after a fixed 5s, other exceptions escalate
  through the delay list and cap at 60s).
- `_run_once()` — rewritten: subscribes to `str` only (not `spl`), holds one
  `listen_iter = ws.listen()` for the connection's lifetime, and on each
  `TradeExecution` calls `_capture_pnl_until_settled`.
- `_capture_pnl_until_settled(ws, listen_iter)` — loops `_capture_pnl_once` while it
  reports an extra execution was seen during its round.
- `_capture_pnl_once(ws, listen_iter, timeout=10.0) -> bool` — the one-shot check
  described above. Uses `asyncio.wait_for(listen_iter.__anext__(), remaining)` per
  pulled item, with `remaining` recomputed against a wall-clock deadline each iteration
  (so a `TradeExecution` arriving mid-wait doesn't extend the original timeout window).
  The `finally` block's `unsubscribe_pnl()` call is wrapped in its own `try/except
  Exception: pass` — matching the existing `TradingViewBridge.stop()` convention in this
  codebase ("errors here must not raise") — so a connection that died during the `try`
  block surfaces its real exception to `_run_with_retry`, not a secondary error from a
  doomed `unsubscribe_pnl()` call.

### 2. `claudia/app.py`

Every current reference to the old name, verified by line number against the current
file:

- Line 249: `from claudia.pnl_stream import PnLStreamer, format_pnl_snapshot` →
  `from claudia.execution_listener import ExecutionListener, format_pnl_snapshot`
- Line 269: `_pnl_streamer: PnLStreamer | None = None` →
  `_execution_listener: ExecutionListener | None = None`
- Line 411 (docstring numbered list): `8. PnLStreamer start (singleton — survives
  across sessions)` → `8. ExecutionListener start (singleton — survives across
  sessions)`
- Line 540 (comment): `# Start P&L streamer (singleton — persists across sessions,
  account-wide not session-scoped). ...` → reworded to describe the execution-listener
  model, e.g. `# Start execution listener (singleton — persists across sessions,
  account-wide not session-scoped). Listens for trade executions (any origin) and
  triggers a one-shot P&L check per execution — see claudia/execution_listener.py.`
- Lines 544-549 (the singleton block itself): `global _pnl_streamer` →
  `global _execution_listener`; `if _pnl_streamer is None: ... _pnl_streamer =
  PnLStreamer(cfg.gateway_url, toolkit._store)` → same shape with
  `_execution_listener`/`ExecutionListener`; `_pnl_streamer.start()` →
  `_execution_listener.start()`.
- Line 588: `f"**Account P&L** (streaming)\n{pnl_text}\n\n"` →
  `f"**Account P&L**\n{pnl_text}\n\n"` — drop the `(streaming)` qualifier since nothing
  streams anymore; the data is "P&L as of the last recorded execution."

No logic change beyond the rename — the opening status block still just calls
`format_pnl_snapshot(latest_pnl)`; `format_pnl_snapshot()`'s own text already says "no
snapshot has been recorded yet" for the `None` case, which remains accurate.

### 3. `claudia/agent.py`

Verified by line number against the current file — both references inside
`_get_live_pnl()` need updating:

- Line 613 (docstring): `"""Format the latest live P&L snapshot from PnLStreamer's
  background WebSocket subscription (claudia/pnl_stream.py). ..."""` → reworded to
  reference `ExecutionListener` and `claudia/execution_listener.py`.
- Line 615: `from claudia.pnl_stream import format_pnl_snapshot` →
  `from claudia.execution_listener import format_pnl_snapshot`.

No other change — the method's behavior and its existing 3 tests
(`test_handle_local_tool_get_live_pnl_populated`/`_none`/
`_partial_fields_format_as_na`) are unaffected by *how* the snapshot was captured, since
`format_pnl_snapshot()`'s signature and behavior don't change.

### 4. `CLAUDE.md`

- Rewrite the "Live P&L Streaming" section (added in the previous pass) to describe the
  new mechanism: `ExecutionListener` subscribes to `str` only, triggers a one-shot `spl`
  check per execution (any origin), reconciles bursts by re-running until settled, and
  does not persist executions themselves. Rename the section heading to something
  accurate, e.g. "Execution-Triggered P&L Checks".

## Migration

- Delete `claudia/pnl_stream.py` and `tests/test_pnl_stream.py` entirely (not deprecated
  in place — this is a full replacement, not an addition).
- New `claudia/execution_listener.py` and `tests/test_execution_listener.py`.
- No SQLite schema change — `pnl_snapshots` (from `ibkr_core_mcp`, untouched) and
  `get_latest_pnl()`/`record_pnl_snapshot()` are reused exactly as before. The only
  behavioral change to the data itself: rows are now written *per settled execution
  batch* instead of on every raw `spl` tick, so the table grows far more slowly and every
  row corresponds to "P&L shortly after some real trade," not an arbitrary point in a
  continuous stream.

## Testing

Mirrors the retry/lifecycle test style already established for `PnLStreamer` (reused
almost verbatim for `_run_with_retry`/`start`/`stop`), plus new tests for the
execution-triggered capture logic:

- `_capture_pnl_once`:
  - a `PnLUpdate` arriving before any other `TradeExecution` → records exactly one
    snapshot, returns `False` (no extra execution seen), `unsubscribe_pnl` called.
  - a `TradeExecution` arriving mid-wait, then a `PnLUpdate` → records the snapshot,
    returns `True` (caller should run another round).
  - no `PnLUpdate` arrives before the timeout → logs a warning, returns whatever
    `saw_extra_execution` accumulated to, `unsubscribe_pnl` still called (verify via a
    short timeout in the test, e.g. `timeout=0.05`, not the real 10s default).
  - `unsubscribe_pnl` raising inside the `finally` block must not mask an exception
    raised earlier in the `try` block (the specific hardening this spec calls for) —
    test by making `listen_iter.__anext__()` raise `ConnectionError` and
    `ws.unsubscribe_pnl` also raise, asserting the original `ConnectionError` propagates,
    not the `unsubscribe_pnl` error.
- `_capture_pnl_until_settled`:
  - a single settled round (one `_capture_pnl_once` call returning `False`) — called once.
  - a burst: first round returns `True`, second returns `False` — called exactly twice.
- `_run_once`:
  - a `TradeExecution` item triggers `_capture_pnl_until_settled` (verify via a mocked/
    patched method, not by re-testing the whole capture logic here).
  - `subscribe_executions()` called exactly once per connection (not per-execution).
- `_run_with_retry`/`start`/`stop`: reuse the exact tests from
  `tests/test_pnl_stream.py` (retry-on-error-then-cancel, cancelled-propagates-immediately,
  clean-return-reconnects-after-5s, escalating-backoff-then-caps, start-idempotent,
  stop-cancels-cleanly, stop-before-start-is-noop) — same logic, same tests, new file.
- `format_pnl_snapshot()`: reuse the exact 3 tests from `tests/test_pnl_stream.py`
  (none/full/partial-fields) — the function itself is unchanged.
- No changes needed to `tests/test_agent.py`'s `get_live_pnl` tests or any status-block
  tests (there are none, per the original spec's decision, still valid) — neither
  consumer's behavior changed.

## Out of scope

- Persisting `TradeExecution` data itself (still Flex + REST's job).
- Any change to `pnl_snapshots`' schema or `record_pnl_snapshot()`/`get_latest_pnl()` in
  `ibkr_core_mcp` — untouched.
- Surfacing the triggering execution's own details (symbol, side, size) in chat — only
  P&L is refreshed; a "trade executed" notification is a plausible future feature but
  not requested here.
- Configurable capture timeout (hardcoded `10.0` seconds) — no requirement surfaced for
  making this tunable via env var or config.
