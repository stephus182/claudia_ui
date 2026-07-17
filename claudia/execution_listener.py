"""
Background WebSocket subscriber that listens for IBKR trade executions (any
origin — mobile, TWS, web, API) and triggers a one-shot account P&L check per
settled batch of executions, recording the result into
SQLiteStore.pnl_snapshots via record_pnl_snapshot().

Runs for the life of the process — one subscription shared across all
concurrent Chainlit sessions. Mirrors the background-task shape of
ConnectivityChecker (status.py). Retry/backoff shape mirrors
ibkr_core_mcp.mcp_server._stream_loop_with_retry.

Design: rather than staying continuously subscribed to IBKR's spl (P&L) topic
(the previous, since-removed PnLStreamer design — see
docs/plans/2026-07-06-live-pnl-streaming-design.md for why that
was judged overkill), this module stays subscribed only to str (trade
executions) — a sparse, meaningful signal — and transiently subscribes to spl
only long enough to capture one P&L tick after a trade happens. See
docs/plans/2026-07-07-execution-triggered-pnl-design.md.

A background "pump" task drains ws.listen() into an asyncio.Queue, and both
the outer execution loop and the transient P&L capture read from that queue
rather than driving ws.listen()'s async generator directly from two places.
This matters: asyncio.wait_for(listen_iter.__anext__(), timeout) cancelling
on timeout throws CancelledError into the generator at its suspension point,
which permanently exhausts it (subsequent __anext__() calls raise
StopAsyncIteration even though the underlying connection is still healthy) --
silently dropping any execution that arrives after a capture timeout.
Cancelling a queue.get() waiter has no such effect on the queue or the pump
task producing into it.

Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ws-trades-sub
Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ws-pnl-sub
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, TYPE_CHECKING

import requests

from ibkr_core_mcp.auth import BrowserCookieAuth
from ibkr_core_mcp.streaming import IBKRWebSocket, PnLUpdate, TradeExecution

if TYPE_CHECKING:
    from ibkr_core_mcp import SQLiteStore

log = logging.getLogger(__name__)

_RETRY_DELAYS = [5, 10, 30, 60]  # seconds between reconnect attempts
_PNL_CAPTURE_TIMEOUT = 10.0  # seconds to wait for a P&L tick after an execution

_CLOSED = object()  # sentinel: the pump task signals a clean WebSocket close


def format_pnl_snapshot(latest: dict[str, Any] | None) -> str:
    """Format a SQLiteStore.get_latest_pnl() row into a human-readable P&L line.

    Shared by the get_live_pnl tool (agent.py) and the opening status block
    (app.py) so both surfaces render identically. Any individually-missing
    numeric field formats as 'n/a' rather than discarding the whole snapshot.
    """
    if latest is None:
        return (
            "Live P&L not yet available — no trade execution has been recorded "
            "yet, or the execution listener may still be connecting."
        )

    def _fmt_signed(v: float | None) -> str:
        return f"{v:+.2f}" if isinstance(v, (int, float)) else "n/a"

    def _fmt(v: float | None) -> str:
        return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"

    return (
        f"Live P&L ({latest['account']}):\n"
        f"Daily P&L: {_fmt_signed(latest['dpl'])} | "
        f"Unrealized: {_fmt_signed(latest['upl'])} | "
        f"Net Liquidity: {_fmt(latest['nl'])} | "
        f"Excess Liquidity: {_fmt(latest['uel'])} | "
        f"Market Value: {_fmt(latest['mv'])}"
    )


def get_live_pnl_text(toolkit: Any) -> str:
    """Best-available live P&L text for display: the ExecutionListener's last
    captured snapshot if this process observed a trade execution, otherwise a
    live ledger pull.

    The reactive cache (SQLiteStore.pnl_snapshots) is empty whenever no
    execution has been observed during this process's lifetime — e.g. the
    user's last trade happened before ClaudIA started, or in an earlier
    session. get_account_ledger (/portfolio/{accountId}/ledger) has no such
    dependency: it returns correct realized/unrealized P&L on every call,
    live-verified 2026-07-17 (docs/plans/2026-07-17-account-pnl-display-fixes.md).
    """
    latest = toolkit._store.get_latest_pnl()
    if latest is not None:
        return format_pnl_snapshot(latest)
    text, _ = toolkit.execute("get_ledger", {})
    return text


async def _next_item(queue: asyncio.Queue[Any]) -> Any:
    """Pull the next item from the pump queue. Converts the _CLOSED sentinel
    into StopAsyncIteration and a forwarded exception into a real raise, so
    every caller (the outer execution loop and the P&L capture loop) shares
    one consistent signal contract regardless of which one is reading."""
    item = await queue.get()
    if item is _CLOSED:
        raise StopAsyncIteration
    if isinstance(item, BaseException):
        raise item
    return item


class ExecutionListener:
    """Background WebSocket subscriber: listens for trade executions (str topic,
    any origin) and triggers a one-shot account P&L check (spl topic) per
    settled batch of executions.

    Lifecycle:
      listener.start()       — fire off the background task (idempotent; matches
                                ConnectivityChecker.start()'s restart-if-done semantics)
      await listener.stop()  — cancel the task cleanly
    """

    def __init__(self, gateway_url: str, store: "SQLiteStore") -> None:
        self._gateway_url = gateway_url
        self._store = store
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background subscription loop as an asyncio Task.

        Idempotent: does nothing if a task is already running. If the previous
        task finished or was cancelled, creates a new one.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_with_retry())
            log.info("ExecutionListener started")

    async def stop(self) -> None:
        """Cancel the background task. Safe to call if never started."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_with_retry(self) -> None:
        """Retry forever with backoff on error. CancelledError propagates
        immediately — no retry, clean shutdown."""
        attempt = 0
        while True:
            try:
                await self._run_once()
                log.info("ExecutionListener: WebSocket closed cleanly; reconnecting in 5s")
                await asyncio.sleep(5)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                log.warning(
                    "ExecutionListener error (attempt %d), retrying in %ds: %s",
                    attempt + 1, delay, type(exc).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def _run_once(self) -> None:
        session = requests.Session()
        await asyncio.to_thread(
            BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")).apply, session
        )
        cookie = session.headers.get("Cookie", "")

        ws = IBKRWebSocket(self._gateway_url, cookie)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        try:
            await ws.connect()
            log.info("ExecutionListener: WebSocket connected")
            await ws.subscribe_executions(realtime_updates_only=True)
            pump_task = asyncio.create_task(self._pump(ws, queue))
            try:
                try:
                    while True:
                        item = await _next_item(queue)
                        if isinstance(item, TradeExecution):
                            await self._capture_pnl_until_settled(ws, queue)
                except StopAsyncIteration:
                    return  # WebSocket closed cleanly — _run_with_retry treats
                            # a clean return as a reconnect-after-5s, not an error
            finally:
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass
        finally:
            await ws.disconnect()

    async def _pump(self, ws: IBKRWebSocket, queue: asyncio.Queue[Any]) -> None:
        """Continuously drain ws.listen() into queue. Decouples the WebSocket's
        single underlying async generator from the multiple places that need
        to consume from it — see the module docstring for why this matters.

        Known limitation: IBKR can send multiple TradeExecution records in a
        single str-topic WebSocket frame, and ws.listen() yields them one at a
        time with no internal buffering guarantee. If this task is cancelled
        (only happens on ExecutionListener.stop()/process shutdown) between two
        yields of the same batch, any not-yet-yielded items in that batch are
        lost. Accepted: this only affects shutdown timing, not steady-state
        operation, and a lost trigger here is low-impact since P&L is
        cumulative — the next execution after restart still captures current
        P&L correctly.
        """
        try:
            async for item in ws.listen():
                await queue.put(item)
            await queue.put(_CLOSED)
        except Exception as exc:
            await queue.put(exc)

    async def _capture_pnl_until_settled(self, ws: IBKRWebSocket, queue: asyncio.Queue[Any]) -> None:
        """Run one-shot P&L capture rounds until a round completes with no
        additional executions observed during it. Account P&L is cumulative,
        so one snapshot after the last known execution is sufficient — no need
        for one snapshot per execution — but no execution may be silently
        dropped as a trigger."""
        while await self._capture_pnl_once(ws, queue):
            pass  # another execution landed mid-round — run one more, fresh round

    async def _capture_pnl_once(
        self, ws: IBKRWebSocket, queue: asyncio.Queue[Any], timeout: float = _PNL_CAPTURE_TIMEOUT
    ) -> bool:
        """Subscribe to spl, wait for exactly one PnLUpdate (bounded by timeout),
        record it, unsubscribe. Returns True if a TradeExecution arrived during
        the wait (caller should run another round to capture a fresher
        snapshot), False otherwise."""
        await ws.subscribe_pnl()
        saw_extra_execution = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("ExecutionListener: timed out waiting for P&L tick after execution")
                    return saw_extra_execution
                try:
                    item = await asyncio.wait_for(_next_item(queue), remaining)
                except asyncio.TimeoutError:
                    log.warning("ExecutionListener: timed out waiting for P&L tick after execution")
                    return saw_extra_execution
                if isinstance(item, PnLUpdate):
                    self._store.record_pnl_snapshot(
                        account=item.account, row_type=item.row_type,
                        dpl=item.dpl, nl=item.nl, upl=item.upl,
                        uel=item.uel, mv=item.mv,
                    )
                    return saw_extra_execution
                if isinstance(item, TradeExecution):
                    saw_extra_execution = True
        finally:
            try:
                await ws.unsubscribe_pnl()
            except Exception:
                pass  # must not mask an exception already propagating from the try block
