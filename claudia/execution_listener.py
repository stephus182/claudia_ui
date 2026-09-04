"""Background WebSocket subscriber that listens for IBKR trade executions (any
origin — mobile, TWS, web, API), reports each one to every subscribed session as an
`ExecutionReport` (2026-09-04 — the automatic execution report: chat message authored
IBKR, System-log toast, operator note, decision row; see `panel_app._make_fill_subscriber`),
and triggers a one-shot account P&L check per settled batch of executions, recording the
result into SQLiteStore.pnl_snapshots via record_pnl_snapshot().

Runs for the life of the process — one subscription shared across all
concurrent Panel sessions. Mirrors the background-task shape of
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

Source: https://ibkrcampus.com/docs/web-api/v1/ws/order-position-operations/request-trades-data.md
Source: https://ibkrcampus.com/docs/web-api/v1/ws/order-position-operations/request-profit-loss.md
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import requests
from ibkr_core_mcp.auth import BrowserCookieAuth
from ibkr_core_mcp.streaming import IBKRWebSocket, PnLUpdate, TradeExecution

if TYPE_CHECKING:
    from ibkr_core_mcp import ClaudeToolkit, SQLiteStore

log = logging.getLogger(__name__)

_RETRY_DELAYS = [5, 10, 30, 60]  # seconds between reconnect attempts

# How long to wait before re-checking whether the session has come up. Deliberately NOT
# part of _RETRY_DELAYS: "the session is not up yet" is the ordinary state at startup and
# throughout every login, not an error, so it must not consume the backoff budget and push
# a genuine reconnect out to a minute.
_IDLE_POLL = 5
_PNL_CAPTURE_TIMEOUT = 10.0  # seconds to wait for a P&L tick after an execution

_CLOSED = object()  # sentinel: the pump task signals a clean WebSocket close

_ET = ZoneInfo("America/New_York")

# Execution ids already reported, bounded. IBKR's `str` documentation says only that
# `realtimeUpdatesOnly` decides whether historical executions are *displayed*; it does not
# promise a resubscribe after a reconnect never re-sends one, so a replayed id must not
# become a second FILLED message and a second decision row (review 2026-09-04, #7).
_SEEN_EXECUTIONS_MAX = 500


def _plain_number(value: float | int, min_decimals: int = 0) -> str:
    """Render a broker figure without altering it: IBKR's digits, thousands grouped.

    `f"{x:g}"` drops digits past six significant figures (1234567 → 1.23457e+06) and
    `:.2f` rounds a 4-dp FX price — a known broker figure must not be altered any more than an
    unknown one may be guessed (review 2026-09-04, #3). `Decimal(str(x))` keeps the digits
    the float carried; `min_decimals` pads a price to the conventional two.
    """
    try:
        q = Decimal(str(value)).normalize()
    except InvalidOperation:
        return str(value)
    exponent = -q.as_tuple().exponent if q.as_tuple().exponent < 0 else 0  # type: ignore[operator]
    decimals = max(int(exponent), min_decimals)
    return f"{q:,.{decimals}f}"


@dataclass(frozen=True)
class ExecutionReport:
    """One fill, as IBKR reported it — the fields a trader reads, pre-formatted.

    Built ONLY from the `TradeExecution` event (2026-09-04): the assistant writes none of it,
    which is why the chat message that carries it is authored "IBKR", not "ClaudIA". Unknown
    fields render as `?` or are omitted — never guessed. No currency: the event carries none,
    and a bare number is the honest render (`order_flow._price_suffix` makes the same choice).
    """

    execution_id: str
    symbol: str        # IBKR's `symbol`, e.g. "ES" — "" when absent
    verb: str          # BOUGHT / SOLD / the raw side if IBKR sends something else
    size: str          # "1", "1,234,567", "0.5", or "?" — IBKR's digits, never rounded
    contract: str      # "ES Sep18 '26" (FUT, measured live); "AMD" (STK, IBKR's doc shape)
    price: str         # "7,732.00", "1.08345" — IBKR's digits, two decimals at least, or "?"
    time_et: str       # "12:47:05 ET" from the UTC trade_time, or ""
    exchange: str
    origin: str        # "via ClaudIA (CLAUDIA-…)" or "external (TWS / mobile / web portal)"

    @classmethod
    def from_event(cls, event: TradeExecution) -> ExecutionReport:
        """Map IBKR's event verbatim. Today's real fill: `B 1 ES Sep18 '26 @ 7732.0`,
        `20260904-16:47:05`, ref `CLAUDIA-1788538622110`, CME → BOUGHT 1 ES Sep18 '26 @
        7,732.00 · 12:47:05 ET · CME · via ClaudIA."""
        side = (event.side or "").strip().upper()
        verb = {"B": "BOUGHT", "BUY": "BOUGHT", "S": "SOLD", "SELL": "SOLD"}.get(side, side or "?")
        size = _plain_number(event.size) if isinstance(event.size, (int, float)) else "?"
        price = _plain_number(event.price, 2) if isinstance(event.price, (int, float)) else "?"
        symbol = (event.symbol or "").strip()
        desc = (event.contract_description_1 or "").strip()
        # IBKR's documented STK shape has contract_description_1 == symbol ("AMD" / "AMD");
        # the measured FUT shape has symbol "ES" + description "Sep18 '26". Join only when
        # the description does not already start with the ticker (review 2026-09-04, #4).
        if desc and (not symbol or desc.startswith(symbol)):
            contract = desc
        else:
            contract = " ".join(p for p in (symbol, desc) if p) or "?"
        time_et = ""
        raw = (event.trade_time or "").strip()
        # IBKR's documented format is YYYYMMDD-HH:mm:ss UTC; anything else is left blank.
        with contextlib.suppress(ValueError):
            time_et = (
                datetime.strptime(raw, "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC).astimezone(_ET)
                .strftime("%H:%M:%S ET")
            )
        ref = (event.order_ref or "").strip()
        origin = f"via ClaudIA ({ref})" if ref.upper().startswith("CLAUDIA-") else "external (TWS / mobile / web portal)"
        return cls(
            execution_id=event.execution_id, symbol=symbol, verb=verb, size=size,
            contract=contract, price=price, time_et=time_et,
            exchange=(event.exchange or "").strip(), origin=origin,
        )

    def as_dict(self) -> dict[str, str]:
        """The report's fields as a plain, JSON-serialisable dict (for the decision row)."""
        return asdict(self)


def format_execution_report(report: ExecutionReport) -> str:
    """The chat text for a fill: one bold line a trader reads in a glance, then provenance.

    Rendered through the chat's `safe_markdown` renderer like every plain string; nothing here
    is model output.
    """
    head = f"**FILLED: {report.verb} {report.size} {report.contract} @ {report.price}**"
    tail = " · ".join(p for p in (report.time_et, report.exchange) if p)
    first = f"{head} · {tail}" if tail else head
    return f"{first}\n{report.origin} · execution {report.execution_id}"


FillSubscriber = Callable[[ExecutionReport], Awaitable[None]]


def format_pnl_snapshot(latest: dict[str, Any] | None) -> str:
    """Format a SQLiteStore.get_latest_pnl() row into a human-readable P&L line.

    Shared by the get_live_pnl tool (agent.py) and the opening status block
    (opening_status.py) so both surfaces render identically. Any individually-missing
    numeric field formats as 'n/a' rather than discarding the whole snapshot.
    """
    if latest is None:
        return (
            "Live P&L not yet available — no trade execution has been recorded "
            "yet, or the execution listener may still be connecting."
        )

    def _fmt_signed(v: float | None) -> str:
        """Format a P&L figure with an explicit sign; "n/a" when absent."""
        return f"{v:+.2f}" if isinstance(v, (int, float)) else "n/a"

    def _fmt(v: float | None) -> str:
        """Format a plain 2-decimal figure; "n/a" when absent."""
        return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"

    return (
        f"Live P&L ({latest['account']}):\n"
        f"Daily P&L: {_fmt_signed(latest['dpl'])} | "
        f"Unrealized: {_fmt_signed(latest['upl'])} | "
        f"Net Liquidity: {_fmt(latest['nl'])} | "
        f"Excess Liquidity: {_fmt(latest['uel'])} | "
        f"Market Value: {_fmt(latest['mv'])}"
    )


def get_live_pnl_text(toolkit: ClaudeToolkit) -> str:
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

    def __init__(
        self, gateway_url: str, store: SQLiteStore, session: Any = None
    ) -> None:
        """Configure the listener. No connection is opened until `start()`.

        Args:
            gateway_url: Base URL of the IBKR Client Portal gateway.
            store: Store that executions and P&L snapshots are written to.
            session: The `GatewaySession` that owns IBKR connectivity. Defaults to the
                process-wide one; injected in tests.
        """
        self._gateway_url = gateway_url
        self._store = store
        self._task: asyncio.Task | None = None
        self._session_override = session
        self._subscribers: list[FillSubscriber] = []
        self._seen_executions: deque[str] = deque(maxlen=_SEEN_EXECUTIONS_MAX)

    def subscribe(self, callback: FillSubscriber) -> Callable[[], None]:
        """Register an async callback for every fill (any origin). Returns an unsubscribe.

        Same shape as `ConnectivityChecker.subscribe`: one process-wide listener, one
        callback per browser session, detached on End Session and on the destroy hook.
        """
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            """Detach this callback. Safe to call twice — a missing entry is ignored."""
            with contextlib.suppress(ValueError):
                self._subscribers.remove(callback)

        return _unsubscribe

    async def _notify_fill(self, event: TradeExecution) -> None:
        """Deliver one fill to every subscriber; a raising subscriber is logged, not fatal.

        Called from BOTH places an execution can surface — the outer loop and the P&L
        capture round, which drains the same queue (review 2026-09-04, #1: a bracket's second
        leg within 10 s of the first was consumed there and never reported). Each execution
        id is reported once (see `_SEEN_EXECUTIONS_MAX`). The list is copied so a callback
        that unsubscribes itself mid-notify cannot corrupt the walk; an exception in one
        session — or in building the report — is logged and cannot cost another session its
        report or the listener its loop.
        """
        if event.execution_id in self._seen_executions:
            log.info("ExecutionListener: execution %s already reported — skipped", event.execution_id)
            return
        self._seen_executions.append(event.execution_id)
        try:
            report = ExecutionReport.from_event(event)
        except Exception as exc:
            log.warning("Could not build an execution report for %s: %s", event.execution_id, exc, exc_info=True)
            return
        for subscriber in list(self._subscribers):
            try:
                await subscriber(report)
            except Exception as exc:
                log.warning(
                    "Could not deliver an execution report to a subscriber: %s", exc, exc_info=True
                )

    @property
    def _session(self) -> Any:
        """The session owner. Resolved lazily so import order stays unconstrained."""
        from claudia.gateway_session import get_session

        return self._session_override or get_session()

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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _run_with_retry(self) -> None:
        """Retry forever with backoff on error. CancelledError propagates
        immediately — no retry, clean shutdown.

        **Connects only against a session the owner reports as LIVE.** The WebSocket is
        not exempt from the rule that any request renews the session: measured in the
        gateway's own log during a real login on 2026-08-06, this loop was reconnecting
        every few seconds and getting `{"message": "waiting for session"}` back while the
        user was still at the 2FA prompt. Waiting here costs one poll interval and removes
        that traffic entirely.
        """
        attempt = 0
        while True:
            try:
                if not self._session.is_live():
                    # Not an error, so it must not consume the backoff budget — a session
                    # that is simply not up yet is the ordinary state at startup and
                    # during every login.
                    await asyncio.sleep(_IDLE_POLL)
                    continue
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
        """Run one full WebSocket lifecycle: authenticate, subscribe, pump until close.

        Extracts the browser session cookie, subscribes with
        `realtime_updates_only=True` (historical replay would re-record executions already
        in the store), and pumps messages until the stream ends.

        A clean close surfaces as `StopAsyncIteration` and **returns normally** — the
        caller treats that as "reconnect in 5s", not as an error. Genuine failures
        propagate so the retry loop can log them.
        """
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
                            # Report FIRST: the capture below can block up to 10 s per
                            # round, and a fill must not wait on a P&L tick (2026-09-04).
                            await self._notify_fill(item)
                            await self._capture_pnl_until_settled(ws, queue)
                except StopAsyncIteration:
                    return  # WebSocket closed cleanly — _run_with_retry treats
                            # a clean return as a reconnect-after-5s, not an error
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
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
                except TimeoutError:
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
                    await self._notify_fill(item)  # every fill is reported, mid-round too
                    saw_extra_execution = True
        finally:
            # suppress() is equivalent to try/except/pass here — does not mask an
            # exception already propagating from the try block above.
            with contextlib.suppress(Exception):
                await ws.unsubscribe_pnl()
