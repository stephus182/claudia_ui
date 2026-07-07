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
docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md for why that
was judged overkill), this module stays subscribed only to str (trade
executions) — a sparse, meaningful signal — and transiently subscribes to spl
only long enough to capture one P&L tick after a trade happens. See
docs/superpowers/specs/2026-07-07-execution-triggered-pnl-design.md.

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


def format_pnl_snapshot(latest: dict[str, Any] | None) -> str:
    """Format a SQLiteStore.get_latest_pnl() row into a human-readable P&L line.

    Shared by the get_live_pnl tool (agent.py) and the opening status block
    (app.py) so both surfaces render identically. Any individually-missing
    numeric field formats as 'n/a' rather than discarding the whole snapshot.
    """
    if latest is None:
        return (
            "Live P&L not yet available — the P&L stream may still be "
            "connecting, or no snapshot has been recorded yet."
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
        try:
            await ws.connect()
            log.info("ExecutionListener: WebSocket connected")
            await ws.subscribe_executions()
            listen_iter = ws.listen()
            try:
                while True:
                    item = await listen_iter.__anext__()
                    if isinstance(item, TradeExecution):
                        await self._capture_pnl_until_settled(ws, listen_iter)
            except StopAsyncIteration:
                return  # WebSocket closed cleanly — _run_with_retry treats a
                        # clean return as a reconnect-after-5s, not an error
        finally:
            await ws.disconnect()

    async def _capture_pnl_until_settled(self, ws: IBKRWebSocket, listen_iter) -> None:
        """Run one-shot P&L capture rounds until a round completes with no
        additional executions observed during it. Account P&L is cumulative,
        so one snapshot after the last known execution is sufficient — no need
        for one snapshot per execution — but no execution may be silently
        dropped as a trigger."""
        while await self._capture_pnl_once(ws, listen_iter):
            pass  # another execution landed mid-round — run one more, fresh round

    async def _capture_pnl_once(
        self, ws: IBKRWebSocket, listen_iter, timeout: float = _PNL_CAPTURE_TIMEOUT
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
                    item = await asyncio.wait_for(listen_iter.__anext__(), remaining)
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
