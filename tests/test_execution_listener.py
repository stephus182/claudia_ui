"""Tests for claudia/execution_listener.py — execution-triggered P&L checks.

Since 2026-08-06 the listener only connects against a session the owner reports as
`LIVE` (plan S4). `_make_listener` therefore injects a live session by default, and the
gate itself is tested explicitly at the bottom of this file rather than left implicit —
an injected default that nothing asserts would be a mock quietly weaker than the real
thing.
"""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from claudia.execution_listener import _IDLE_POLL, _RETRY_DELAYS, ExecutionListener


def _live_session(live: bool = True):
    """A stand-in session owner. `is_live()` is the only thing the listener asks it."""
    owner = MagicMock()
    owner.is_live.return_value = live
    return owner


def _make_listener(live: bool = True):
    """An ExecutionListener wired to a mock store, returned alongside that store.

    Defaults to a LIVE session: most tests here exercise what the listener does once
    connected, and without an injected owner they would spin in the idle loop waiting for
    a session that never comes up.
    """
    store = MagicMock()
    listener = ExecutionListener(
        "https://localhost:5055/v1/api", store, session=_live_session(live)
    )
    return listener, store


def _fake_ws(listen_items):
    """Build a MagicMock IBKRWebSocket whose listen() yields the given items."""
    async def fake_listen():
        """Replay the given feed items as an async generator, standing in for the socket."""
        for item in listen_items:
            yield item

    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.subscribe_executions = AsyncMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()
    ws.listen = fake_listen
    return ws


# ── _capture_pnl_once ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_pnl_once_records_and_returns_false_when_no_extra_execution():
    """One P&L tick is recorded, and the quiet case reports no further execution to wait for."""
    from ibkr_core_mcp.streaming import PnLUpdate

    listener, store = _make_listener()
    pnl = PnLUpdate(
        account="DU1234567.Core", row_type=1, dpl=12.5, nl=10000.0,
        upl=3.0, uel=9000.0, mv=5000.0,
    )
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(pnl)

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, queue)

    assert result is False
    store.record_pnl_snapshot.assert_called_once_with(
        account="DU1234567.Core", row_type=1, dpl=12.5, nl=10000.0,
        upl=3.0, uel=9000.0, mv=5000.0,
    )
    ws.subscribe_pnl.assert_awaited_once()
    ws.unsubscribe_pnl.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_pnl_once_returns_true_when_execution_seen_mid_wait():
    """An execution arriving during the wait reports that another capture round is owed."""
    from ibkr_core_mcp.streaming import PnLUpdate, TradeExecution

    listener, store = _make_listener()
    execution = TradeExecution(execution_id="E2")
    pnl = PnLUpdate(account="DU1234567.Core", dpl=1.0, nl=1.0, upl=1.0, uel=1.0, mv=1.0)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(execution)
    queue.put_nowait(pnl)

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, queue)

    assert result is True
    store.record_pnl_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_capture_pnl_once_times_out_without_pnl_update():
    """A silent window times out and records nothing rather than inventing a snapshot."""
    listener, store = _make_listener()
    queue: asyncio.Queue = asyncio.Queue()  # nothing ever put on it

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, queue, timeout=0.05)

    assert result is False
    store.record_pnl_snapshot.assert_not_called()
    ws.unsubscribe_pnl.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_pnl_once_unsubscribe_error_does_not_mask_original_exception():
    """If the connection dies mid-capture AND unsubscribe_pnl also raises while
    cleaning up, the original (more informative) exception must win."""
    listener, _ = _make_listener()
    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock(side_effect=RuntimeError("also broken"))

    class _BrokenQueue:
        """A queue whose reads always fail, standing in for a dropped connection."""
        async def get(self):
            """Fail the way a dropped connection does."""
            raise ConnectionError("dropped")

    with pytest.raises(ConnectionError, match="dropped"):
        await listener._capture_pnl_once(ws, _BrokenQueue(), timeout=0.05)

    ws.unsubscribe_pnl.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_timeout_does_not_poison_subsequent_reads():
    """Regression test for the queue-based fan-out: a capture round that times
    out (no PnLUpdate ever arrives) must not corrupt the shared queue — a later
    item put on the same queue must still be retrievable. A raw-async-generator
    implementation (asyncio.wait_for(gen.__anext__(), timeout)) permanently
    exhausts the generator on a cancelled-by-timeout __anext__() call; a
    queue.get() call has no such effect when cancelled."""
    from ibkr_core_mcp.streaming import TradeExecution

    listener, store = _make_listener()
    queue: asyncio.Queue = asyncio.Queue()

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    # Capture round times out — nothing is ever put on the queue during this call.
    result = await listener._capture_pnl_once(ws, queue, timeout=0.05)
    assert result is False
    store.record_pnl_snapshot.assert_not_called()

    # The SAME queue must still work correctly afterward.
    await queue.put(TradeExecution(execution_id="E2"))
    item = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(item, TradeExecution)
    assert item.execution_id == "E2"


@pytest.mark.asyncio
async def test_capture_pnl_once_propagates_stop_async_iteration_on_closed_mid_wait():
    """A _CLOSED sentinel arriving while waiting for a PnLUpdate (WS closed mid-
    capture) must propagate as StopAsyncIteration, not be silently swallowed."""
    from claudia.execution_listener import _CLOSED

    listener, store = _make_listener()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(_CLOSED)

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    with pytest.raises(StopAsyncIteration):
        await listener._capture_pnl_once(ws, queue)

    store.record_pnl_snapshot.assert_not_called()
    ws.unsubscribe_pnl.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_pnl_once_propagates_forwarded_exception_mid_wait():
    """An exception forwarded onto the queue (as _pump does on a real WS error)
    while waiting for a PnLUpdate must propagate, not be silently swallowed."""
    listener, store = _make_listener()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(ConnectionError("dropped"))

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    with pytest.raises(ConnectionError, match="dropped"):
        await listener._capture_pnl_once(ws, queue)

    store.record_pnl_snapshot.assert_not_called()
    ws.unsubscribe_pnl.assert_awaited_once()


# ── _capture_pnl_until_settled ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_pnl_until_settled_single_round():
    """A quiet capture settles in one round."""
    listener, _ = _make_listener()
    with patch.object(listener, "_capture_pnl_once", new=AsyncMock(return_value=False)) as mock_once:
        await listener._capture_pnl_until_settled(MagicMock(), MagicMock())
    mock_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_pnl_until_settled_reruns_on_burst():
    """An execution burst re-runs the capture until the account stops moving."""
    listener, _ = _make_listener()
    with patch.object(
        listener, "_capture_pnl_once", new=AsyncMock(side_effect=[True, False])
    ) as mock_once:
        await listener._capture_pnl_until_settled(MagicMock(), MagicMock())
    assert mock_once.await_count == 2


# ── _run_once ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_triggers_capture_per_top_level_execution():
    """The execution feed is subscribed once and each execution triggers a capture."""
    from ibkr_core_mcp.streaming import TradeExecution

    listener, _ = _make_listener()
    fake_ws = _fake_ws([TradeExecution(execution_id="E1"), TradeExecution(execution_id="E2")])

    with patch("claudia.execution_listener.BrowserCookieAuth"), \
         patch("claudia.execution_listener.IBKRWebSocket", return_value=fake_ws), \
         patch.object(listener, "_capture_pnl_until_settled", new=AsyncMock()) as mock_capture:
        await listener._run_once()

    fake_ws.subscribe_executions.assert_awaited_once()
    assert mock_capture.await_count == 2
    fake_ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_returns_cleanly_when_websocket_closes():
    """An empty listen() (WebSocket closed cleanly, no items ever sent) must make
    _run_once return without raising, so _run_with_retry treats it as a clean
    close (reconnect after a fixed 5s) rather than an error (escalating backoff)."""
    listener, _ = _make_listener()
    fake_ws = _fake_ws([])  # empty — listen() yields nothing, then ends

    with patch("claudia.execution_listener.BrowserCookieAuth"), \
         patch("claudia.execution_listener.IBKRWebSocket", return_value=fake_ws):
        await listener._run_once()  # must not raise

    fake_ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_disconnects_even_on_listen_error():
    """The socket is disconnected on the error path, so a drop cannot leak a connection."""
    listener, _ = _make_listener()

    async def broken_listen():
        """Fail on first read, as a dropped socket does."""
        raise ConnectionError("dropped")
        yield  # type: ignore[unreachable]  # pragma: no cover — makes this an async generator

    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.subscribe_executions = AsyncMock()
    ws.listen = broken_listen

    with patch("claudia.execution_listener.BrowserCookieAuth"), \
         patch("claudia.execution_listener.IBKRWebSocket", return_value=ws), \
         pytest.raises(ConnectionError):
        await listener._run_once()

    ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_end_to_end_reconciles_burst_through_real_pump_and_capture():
    """True integration test: drives the real (unmocked) pump -> queue ->
    _capture_pnl_until_settled -> _capture_pnl_once chain through a burst of two
    executions followed by two PnLUpdate ticks, confirming the whole pipeline
    reconciles correctly end-to-end (not just each piece in isolation, which is
    what every other test in this file does)."""
    from ibkr_core_mcp.streaming import PnLUpdate, TradeExecution

    listener, store = _make_listener()
    fake_ws = _fake_ws([
        TradeExecution(execution_id="E1"),
        TradeExecution(execution_id="E2"),
        PnLUpdate(account="DU1234567.Core", dpl=1.0, nl=1.0, upl=1.0, uel=1.0, mv=1.0),
        PnLUpdate(account="DU1234567.Core", dpl=2.0, nl=2.0, upl=2.0, uel=2.0, mv=2.0),
    ])

    with patch("claudia.execution_listener.BrowserCookieAuth"), \
         patch("claudia.execution_listener.IBKRWebSocket", return_value=fake_ws):
        await listener._run_once()

    fake_ws.subscribe_executions.assert_awaited_once()
    assert store.record_pnl_snapshot.call_count == 2
    fake_ws.disconnect.assert_awaited_once()


# ── _run_with_retry ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_with_retry_retries_on_error_then_cancels():
    """A transient error triggers a retry, not propagation; CancelledError exits the loop."""
    listener, _ = _make_listener()
    call_count = 0

    async def flaky_run_once():
        """Fail once with a transient error, then cancel to end the retry loop."""
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=flaky_run_once), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()), \
         pytest.raises(asyncio.CancelledError):
        await listener._run_with_retry()

    assert call_count == 2


@pytest.mark.asyncio
async def test_run_with_retry_cancelled_propagates_immediately():
    """CancelledError from _run_once must propagate immediately — no retry."""
    listener, _ = _make_listener()

    async def always_cancel():
        """Cancel immediately, so the loop exits on its first pass."""
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=always_cancel), \
         pytest.raises(asyncio.CancelledError):
        await listener._run_with_retry()


@pytest.mark.asyncio
async def test_run_with_retry_clean_return_reconnects_after_5s():
    """A clean (non-raising) return from _run_once (WS closed cleanly) retries after 5s,
    not treated as a fatal exit."""
    listener, _ = _make_listener()
    call_count = 0

    async def clean_then_cancel():
        """Return cleanly once, then cancel to end the loop."""
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return  # clean close
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=clean_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()) as mock_sleep, \
         pytest.raises(asyncio.CancelledError):
        await listener._run_with_retry()

    assert call_count == 2
    mock_sleep.assert_any_call(5)


@pytest.mark.asyncio
async def test_run_with_retry_logs_traceback_on_error(caplog):
    """The previous type(exc).__name__-only logging hid the real cause of a repeating
    RuntimeError for a full live-test session (2026-07-10) before this fix — exc_info=True
    is required so the actual traceback is captured, not just the exception class name."""
    listener, _ = _make_listener()
    call_count = 0

    async def fail_then_cancel():
        """Raise the loop-misuse error once, then cancel to end the loop."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Timeout should be used inside a task")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=fail_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()), \
         caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await listener._run_with_retry()

    assert any(r.exc_info is not None for r in caplog.records)


@pytest.mark.asyncio
async def test_run_with_retry_escalates_backoff_then_caps():
    """Consecutive exception failures must escalate through _RETRY_DELAYS and cap at
    the last value, not retry at a flat interval."""
    listener, _ = _make_listener()
    call_count = 0

    async def always_fail_then_cancel():
        """Fail through every retry delay, then cancel to end the loop."""
        nonlocal call_count
        call_count += 1
        if call_count <= 5:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=always_fail_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()) as mock_sleep, \
         pytest.raises(asyncio.CancelledError):
        await listener._run_with_retry()

    assert mock_sleep.call_args_list == [call(5), call(10), call(30), call(60), call(60)]


# ── start() / stop() lifecycle ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_is_idempotent():
    """Calling start() twice in a row (no await in between) must not create two tasks."""
    listener, _ = _make_listener()
    with patch.object(listener, "_run_with_retry", new=AsyncMock(return_value=None)):
        listener.start()
        task1 = listener._task
        listener.start()
        task2 = listener._task
    assert task1 is task2
    await listener.stop()


@pytest.mark.asyncio
async def test_stop_cancels_cleanly():
    """Stopping cancels the task and clears the handle."""
    listener, _ = _make_listener()

    async def never_ending():
        """Block forever, so `stop()` has a live task to cancel."""
        await asyncio.sleep(100)

    with patch.object(listener, "_run_with_retry", side_effect=never_ending):
        listener.start()
        await listener.stop()

    assert listener._task is None


@pytest.mark.asyncio
async def test_stop_before_start_is_noop():
    """Stopping before starting is a no-op, not an error."""
    listener, _ = _make_listener()
    await listener.stop()  # must not raise
    assert listener._task is None


# ── format_pnl_snapshot ────────────────────────────────────────────────────────

def test_format_pnl_snapshot_none():
    """With no snapshot the line says so plainly rather than rendering zeros."""
    from claudia.execution_listener import format_pnl_snapshot
    result = format_pnl_snapshot(None)
    assert "not yet available" in result.lower()


def test_format_pnl_snapshot_full():
    """A full snapshot renders the account and every figure, signed where it is a P&L."""
    from claudia.execution_listener import format_pnl_snapshot
    result = format_pnl_snapshot({
        "account": "DU1234567.Core", "dpl": 12.5, "nl": 10000.0,
        "upl": 3.0, "uel": 9000.0, "mv": 5000.0,
    })
    assert "DU1234567.Core" in result
    assert "+12.50" in result
    assert "10000.00" in result


def test_format_pnl_snapshot_partial_fields_format_as_na():
    """A partial snapshot (e.g. a first, incomplete tick) must show 'n/a' per-field,
    not discard the whole snapshot as 'not yet available'."""
    from claudia.execution_listener import format_pnl_snapshot
    result = format_pnl_snapshot({
        "account": "DU1234567.Core", "dpl": None, "nl": 10000.0,
        "upl": None, "uel": None, "mv": None,
    })
    assert "n/a" in result
    assert "10000.00" in result
    assert "not yet available" not in result.lower()


# ── get_live_pnl_text ──────────────────────────────────────────────────────────

def test_get_live_pnl_text_uses_cache_when_populated():
    """A cached snapshot is used and no live call is made."""
    from unittest.mock import MagicMock

    from claudia.execution_listener import get_live_pnl_text
    toolkit = MagicMock()
    toolkit._store.get_latest_pnl.return_value = {
        "account": "U1675699.Core", "dpl": 12.5, "nl": 10000.0,
        "upl": 3.0, "uel": 9000.0, "mv": 5000.0,
    }
    result = get_live_pnl_text(toolkit)
    assert "U1675699.Core" in result
    toolkit.execute.assert_not_called()


def test_get_live_pnl_text_falls_back_to_ledger_when_cache_empty():
    """An empty cache falls back to a live ledger pull — the cache starts empty each process."""
    from unittest.mock import MagicMock

    from claudia.execution_listener import get_live_pnl_text
    toolkit = MagicMock()
    toolkit._store.get_latest_pnl.return_value = None
    toolkit.execute.return_value = ("Account Ledger (USD):\n  Realized P&L : +461.56", None)
    result = get_live_pnl_text(toolkit)
    assert "Realized P&L" in result
    assert "+461.56" in result
    toolkit.execute.assert_called_once_with("get_ledger", {})


# ── The S4 gate: no traffic against a session the owner has not confirmed ────


@pytest.mark.asyncio
async def test_does_not_connect_while_the_session_is_not_live():
    """The WebSocket is not exempt from the suspension rule.

    Any request renews the session, and this loop was measured reconnecting every few
    seconds during a real login on 2026-08-06 — the gateway's own log shows it receiving
    `{"message": "waiting for session"}` while the user was still at the 2FA prompt.
    """
    listener, _ = _make_listener(live=False)
    with patch.object(listener, "_run_once", new=AsyncMock()) as run_once:
        task = asyncio.create_task(listener._run_with_retry())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    run_once.assert_not_called()


@pytest.mark.asyncio
async def test_connects_once_the_session_is_live():
    """The gate must open again — a listener that never reconnects is worse than one
    that reconnects too eagerly."""
    listener, _ = _make_listener(live=True)
    with patch.object(listener, "_run_once", new=AsyncMock()) as run_once:
        task = asyncio.create_task(listener._run_with_retry())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    run_once.assert_awaited()


@pytest.mark.asyncio
async def test_waiting_for_a_session_does_not_consume_the_backoff_budget():
    """"Not up yet" is the ordinary startup state, not an error.

    Letting it advance the retry counter would push a genuine reconnect out to a minute on
    a gateway that had merely not finished logging in. The stub stops the loop on its first
    wait so exactly one value is observed — patching `asyncio.sleep` and then sleeping in
    the test itself records the test's own calls, which is how the first version of this
    test "failed" against correct code.
    """
    listener, _ = _make_listener(live=False)
    waits: list[float] = []

    async def record_then_stop(delay):
        """Record the wait the listener asked for, then end the loop."""
        waits.append(delay)
        raise asyncio.CancelledError

    with (
        patch("claudia.execution_listener.asyncio.sleep", new=record_then_stop),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await listener._run_with_retry()

    assert waits == [_IDLE_POLL], (
        f"idle waits must use _IDLE_POLL ({_IDLE_POLL}s), not the error backoff "
        f"{_RETRY_DELAYS}: got {waits}"
    )
