"""
Background WebSocket subscriber that keeps SQLiteStore.pnl_snapshots populated
with live account P&L ticks (ibkr_core_mcp spl topic).

Runs for the life of the process — one subscription shared across all
concurrent Chainlit sessions, since IBKR account P&L is account-wide, not
session-scoped. Mirrors the background-task shape of ConnectivityChecker
(status.py) rather than reusing ibkr_core_mcp.mcp_server._stream_loop, which
also dispatches trade executions and market-data alerts ClaudIA doesn't need
here. Retry/backoff shape mirrors ibkr_core_mcp.mcp_server._stream_loop_with_retry.

Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ws-pnl-sub
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import requests

from ibkr_core_mcp.auth import BrowserCookieAuth
from ibkr_core_mcp.streaming import IBKRWebSocket, PnLUpdate

if TYPE_CHECKING:
    from ibkr_core_mcp import SQLiteStore

log = logging.getLogger(__name__)

_RETRY_DELAYS = [5, 10, 30, 60]  # seconds between reconnect attempts


class PnLStreamer:
    """Background WebSocket subscriber for live account P&L (spl topic).

    Lifecycle:
      streamer.start()       — fire off the background task (idempotent; matches
                                ConnectivityChecker.start()'s restart-if-done semantics)
      await streamer.stop()  — cancel the task cleanly
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
            log.info("PnLStreamer started")

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
                # _run_once only returns without exception if the WebSocket closed
                # cleanly (e.g. gateway shutdown). Retry after a short delay.
                log.info("PnLStreamer: WebSocket closed cleanly; reconnecting in 5s")
                await asyncio.sleep(5)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                log.warning(
                    "PnLStreamer error (attempt %d), retrying in %ds: %s",
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
