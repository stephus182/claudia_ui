"""Tests for claudia/execution_listener.py — execution-triggered P&L checks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from claudia.execution_listener import ExecutionListener


def _make_listener():
    store = MagicMock()
    return ExecutionListener("https://localhost:5055/v1/api", store), store


def _fake_ws(listen_items):
    """Build a MagicMock IBKRWebSocket whose listen() yields the given items."""
    async def fake_listen():
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
        async def get(self):
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


# ── _capture_pnl_until_settled ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_pnl_until_settled_single_round():
    listener, _ = _make_listener()
    with patch.object(listener, "_capture_pnl_once", new=AsyncMock(return_value=False)) as mock_once:
        await listener._capture_pnl_until_settled(MagicMock(), MagicMock())
    mock_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_pnl_until_settled_reruns_on_burst():
    listener, _ = _make_listener()
    with patch.object(
        listener, "_capture_pnl_once", new=AsyncMock(side_effect=[True, False])
    ) as mock_once:
        await listener._capture_pnl_until_settled(MagicMock(), MagicMock())
    assert mock_once.await_count == 2


# ── _run_once ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_triggers_capture_per_top_level_execution():
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
    listener, _ = _make_listener()

    async def broken_listen():
        raise ConnectionError("dropped")
        yield  # pragma: no cover — unreachable, makes this an async generator

    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.subscribe_executions = AsyncMock()
    ws.listen = broken_listen

    with patch("claudia.execution_listener.BrowserCookieAuth"), \
         patch("claudia.execution_listener.IBKRWebSocket", return_value=ws):
        with pytest.raises(ConnectionError):
            await listener._run_once()

    ws.disconnect.assert_awaited_once()


# ── _run_with_retry ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_with_retry_retries_on_error_then_cancels():
    """A transient error triggers a retry, not propagation; CancelledError exits the loop."""
    listener, _ = _make_listener()
    call_count = 0

    async def flaky_run_once():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=flaky_run_once), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await listener._run_with_retry()

    assert call_count == 2


@pytest.mark.asyncio
async def test_run_with_retry_cancelled_propagates_immediately():
    """CancelledError from _run_once must propagate immediately — no retry."""
    listener, _ = _make_listener()

    async def always_cancel():
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=always_cancel):
        with pytest.raises(asyncio.CancelledError):
            await listener._run_with_retry()


@pytest.mark.asyncio
async def test_run_with_retry_clean_return_reconnects_after_5s():
    """A clean (non-raising) return from _run_once (WS closed cleanly) retries after 5s,
    not treated as a fatal exit."""
    listener, _ = _make_listener()
    call_count = 0

    async def clean_then_cancel():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return  # clean close
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=clean_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
            await listener._run_with_retry()

    assert call_count == 2
    mock_sleep.assert_any_call(5)


@pytest.mark.asyncio
async def test_run_with_retry_escalates_backoff_then_caps():
    """Consecutive exception failures must escalate through _RETRY_DELAYS and cap at
    the last value, not retry at a flat interval."""
    listener, _ = _make_listener()
    call_count = 0

    async def always_fail_then_cancel():
        nonlocal call_count
        call_count += 1
        if call_count <= 5:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=always_fail_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
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
    listener, _ = _make_listener()

    async def never_ending():
        await asyncio.sleep(100)

    with patch.object(listener, "_run_with_retry", side_effect=never_ending):
        listener.start()
        await listener.stop()

    assert listener._task is None


@pytest.mark.asyncio
async def test_stop_before_start_is_noop():
    listener, _ = _make_listener()
    await listener.stop()  # must not raise
    assert listener._task is None


# ── format_pnl_snapshot ────────────────────────────────────────────────────────

def test_format_pnl_snapshot_none():
    from claudia.execution_listener import format_pnl_snapshot
    result = format_pnl_snapshot(None)
    assert "not yet available" in result.lower()


def test_format_pnl_snapshot_full():
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
