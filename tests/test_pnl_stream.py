"""Tests for claudia/pnl_stream.py — background P&L WebSocket subscriber."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from claudia.pnl_stream import PnLStreamer


def _make_streamer():
    store = MagicMock()
    return PnLStreamer("https://localhost:5055/v1/api", store), store


def _fake_ws(listen_items):
    """Build a MagicMock IBKRWebSocket whose listen() yields the given items."""
    async def fake_listen():
        for item in listen_items:
            yield item

    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.subscribe_pnl = AsyncMock()
    ws.listen = fake_listen
    return ws


# ── _run_once ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_records_pnl_update():
    from ibkr_core_mcp.streaming import PnLUpdate

    streamer, store = _make_streamer()
    pnl = PnLUpdate(
        account="DU1234567.Core", row_type=1, dpl=12.5, nl=10000.0,
        upl=3.0, uel=9000.0, mv=5000.0,
    )
    fake_ws = _fake_ws([pnl])

    with patch("claudia.pnl_stream.BrowserCookieAuth"), \
         patch("claudia.pnl_stream.IBKRWebSocket", return_value=fake_ws):
        await streamer._run_once()

    store.record_pnl_snapshot.assert_called_once_with(
        account="DU1234567.Core", row_type=1, dpl=12.5, nl=10000.0,
        upl=3.0, uel=9000.0, mv=5000.0,
    )
    fake_ws.subscribe_pnl.assert_awaited_once()
    fake_ws.connect.assert_awaited_once()
    fake_ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_ignores_non_pnl_items():
    """Defensive isinstance guard: a non-PnLUpdate item must not reach record_pnl_snapshot."""
    streamer, store = _make_streamer()
    fake_ws = _fake_ws([object()])

    with patch("claudia.pnl_stream.BrowserCookieAuth"), \
         patch("claudia.pnl_stream.IBKRWebSocket", return_value=fake_ws):
        await streamer._run_once()

    store.record_pnl_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_disconnects_even_on_listen_error():
    streamer, store = _make_streamer()

    async def broken_listen():
        raise ConnectionError("dropped")
        yield  # pragma: no cover — unreachable, makes this an async generator

    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.subscribe_pnl = AsyncMock()
    ws.listen = broken_listen

    with patch("claudia.pnl_stream.BrowserCookieAuth"), \
         patch("claudia.pnl_stream.IBKRWebSocket", return_value=ws):
        with pytest.raises(ConnectionError):
            await streamer._run_once()

    ws.disconnect.assert_awaited_once()


# ── _run_with_retry ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_with_retry_retries_on_error_then_cancels():
    """A transient error triggers a retry, not propagation; CancelledError exits the loop."""
    streamer, _ = _make_streamer()
    call_count = 0

    async def flaky_run_once():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(streamer, "_run_once", side_effect=flaky_run_once), \
         patch("claudia.pnl_stream.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await streamer._run_with_retry()

    assert call_count == 2


@pytest.mark.asyncio
async def test_run_with_retry_cancelled_propagates_immediately():
    """CancelledError from _run_once must propagate immediately — no retry."""
    streamer, _ = _make_streamer()

    async def always_cancel():
        raise asyncio.CancelledError

    with patch.object(streamer, "_run_once", side_effect=always_cancel):
        with pytest.raises(asyncio.CancelledError):
            await streamer._run_with_retry()


@pytest.mark.asyncio
async def test_run_with_retry_clean_return_reconnects_after_5s():
    """A clean (non-raising) return from _run_once (WS closed cleanly) retries after 5s,
    not treated as a fatal exit."""
    streamer, _ = _make_streamer()
    call_count = 0

    async def clean_then_cancel():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return  # clean close
        raise asyncio.CancelledError

    with patch.object(streamer, "_run_once", side_effect=clean_then_cancel), \
         patch("claudia.pnl_stream.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
            await streamer._run_with_retry()

    assert call_count == 2
    mock_sleep.assert_any_call(5)


@pytest.mark.asyncio
async def test_run_with_retry_escalates_backoff_then_caps():
    """Consecutive exception failures must escalate through _RETRY_DELAYS and cap at
    the last value, not retry at a flat interval."""
    streamer, _ = _make_streamer()
    call_count = 0

    async def always_fail_then_cancel():
        nonlocal call_count
        call_count += 1
        if call_count <= 5:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    with patch.object(streamer, "_run_once", side_effect=always_fail_then_cancel), \
         patch("claudia.pnl_stream.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
            await streamer._run_with_retry()

    assert mock_sleep.call_args_list == [call(5), call(10), call(30), call(60), call(60)]


# ── start() / stop() lifecycle ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_is_idempotent():
    """Calling start() twice in a row (no await in between) must not create two tasks."""
    streamer, _ = _make_streamer()
    with patch.object(streamer, "_run_with_retry", new=AsyncMock(return_value=None)):
        streamer.start()
        task1 = streamer._task
        streamer.start()
        task2 = streamer._task
    assert task1 is task2
    await streamer.stop()


@pytest.mark.asyncio
async def test_stop_cancels_cleanly():
    streamer, _ = _make_streamer()

    async def never_ending():
        await asyncio.sleep(100)

    with patch.object(streamer, "_run_with_retry", side_effect=never_ending):
        streamer.start()
        await streamer.stop()

    assert streamer._task is None


@pytest.mark.asyncio
async def test_stop_before_start_is_noop():
    streamer, _ = _make_streamer()
    await streamer.stop()  # must not raise
    assert streamer._task is None
