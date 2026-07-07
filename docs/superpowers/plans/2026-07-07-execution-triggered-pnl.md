# Execution-Triggered P&L Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the always-on `PnLStreamer` (continuous `spl`-topic WebSocket subscription, writing every tick) with `ExecutionListener` — a background subscriber that listens only for trade executions (`str` topic, any origin) and runs a bounded one-shot P&L check per settled batch of executions.

**Architecture:** One persistent WebSocket connection subscribed to `str` only. On each `TradeExecution`, transiently subscribe to `spl`, wait (bounded by a timeout) for exactly one `PnLUpdate`, record it, unsubscribe. If more executions arrive while waiting, run one more capture round after the current one settles — reconciling bursts without dropping any execution as a trigger, and without needing one snapshot per execution (P&L is cumulative, not per-trade).

**Tech Stack:** Python 3.11+, `ibkr_core_mcp` (`IBKRWebSocket.subscribe_executions`/`subscribe_pnl`, `TradeExecution`, `PnLUpdate`, `SQLiteStore.record_pnl_snapshot`/`get_latest_pnl` — all already exist, no `ibkr_core_mcp` changes needed), `asyncio`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-07-07-execution-triggered-pnl-design.md`

---

## Before you start

This plan assumes a worktree already exists at
`/Users/steph/Claude_Projects/claudia_ui/.worktrees/execution-triggered-pnl` (branch
`feature/execution-triggered-pnl`), with its own `.venv` where both `claudia_ui` and
`ibkr_core_mcp` (from `/Users/steph/Claude_Projects/ibkr_core_mcp`, main branch) are
installed editable:

```bash
cd /Users/steph/Claude_Projects/claudia_ui/.worktrees/execution-triggered-pnl
source .venv/bin/activate
```

All commands below assume this venv is active and this is the working directory.

Baseline (already verified): `pytest -q` → 223 passed.

---

### Task 1: `ExecutionListener` — failing tests first

**Files:**
- Create: `tests/test_execution_listener.py`

`claudia/execution_listener.py` does not exist yet — these tests will fail on import.
That is expected and confirms TDD is being followed correctly.

- [ ] **Step 1: Write the failing tests**

```python
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

    async def fake_iter():
        yield pnl

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, fake_iter())

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

    async def fake_iter():
        yield execution
        yield pnl

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, fake_iter())

    assert result is True
    store.record_pnl_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_capture_pnl_once_times_out_without_pnl_update():
    listener, store = _make_listener()

    async def never_arrives():
        await asyncio.sleep(1)
        yield  # pragma: no cover — unreachable given the short timeout below

    ws = MagicMock()
    ws.subscribe_pnl = AsyncMock()
    ws.unsubscribe_pnl = AsyncMock()

    result = await listener._capture_pnl_once(ws, never_arrives(), timeout=0.05)

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

    class _BrokenIter:
        async def __anext__(self):
            raise ConnectionError("dropped")

    with pytest.raises(ConnectionError, match="dropped"):
        await listener._capture_pnl_once(ws, _BrokenIter(), timeout=0.05)

    ws.unsubscribe_pnl.assert_awaited_once()


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
```

- [ ] **Step 2: Run tests to verify they fail on import**

Run: `pytest tests/test_execution_listener.py -q`
Expected: `ModuleNotFoundError: No module named 'claudia.execution_listener'` (or
`ImportError`) — confirms the module doesn't exist yet.

---

### Task 2: Implement `ExecutionListener`

**Files:**
- Create: `claudia/execution_listener.py`

- [ ] **Step 1: Write the module**

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_execution_listener.py -q -v`
Expected: 19 passed (all tests from Task 1).

- [ ] **Step 3: Commit**

```bash
git add claudia/execution_listener.py tests/test_execution_listener.py
git commit -m "feat: add ExecutionListener — execution-triggered P&L checks

Replaces the always-on PnLStreamer design: one persistent WebSocket
connection subscribed only to str (trade executions, any origin), which
triggers a bounded one-shot spl check per settled batch of executions,
instead of a continuous spl subscription writing every tick.

Reconciliation: since account P&L is cumulative (not per-trade), one
snapshot after the last known execution is sufficient — but no execution is
silently dropped as a trigger; a burst of executions during an in-progress
capture round causes one more round to run after the current one settles.

See docs/superpowers/specs/2026-07-07-execution-triggered-pnl-design.md."
```

---

### Task 3: Rewire `app.py` to `ExecutionListener`

**Files:**
- Modify: `claudia/app.py`

- [ ] **Step 1: Update the import**

Find (currently line 249):

```python
from claudia.pnl_stream import PnLStreamer, format_pnl_snapshot
```

Replace with:

```python
from claudia.execution_listener import ExecutionListener, format_pnl_snapshot
```

- [ ] **Step 2: Update the singleton global**

Find (currently line 269):

```python
_pnl_streamer: PnLStreamer | None = None
```

Replace with:

```python
_execution_listener: ExecutionListener | None = None
```

- [ ] **Step 3: Update the docstring lifecycle list**

Find (currently line 411, inside `on_chat_start`'s docstring):

```python
    8. PnLStreamer start (singleton — survives across sessions)
```

Replace with:

```python
    8. ExecutionListener start (singleton — survives across sessions)
```

- [ ] **Step 4: Update the singleton-start block in `on_chat_start`**

Find (currently lines 540-549):

```python
    # Start P&L streamer (singleton — persists across sessions, account-wide not
    # session-scoped). Construction is synchronous (no await between the None-check
    # and assignment), so — like _connectivity_checker above — no lock is needed;
    # this differs from _get_tv_bridge()'s lock, which guards an async subprocess spawn.
    global _pnl_streamer
    if _pnl_streamer is None:
        cfg = _config or Config.from_env()
        _config = cfg
        _pnl_streamer = PnLStreamer(cfg.gateway_url, toolkit._store)
    _pnl_streamer.start()
```

Replace with:

```python
    # Start execution listener (singleton — persists across sessions, account-wide
    # not session-scoped). Listens for trade executions (any origin) and triggers a
    # one-shot P&L check per execution — see claudia/execution_listener.py.
    # Construction is synchronous (no await between the None-check and assignment),
    # so — like _connectivity_checker above — no lock is needed; this differs from
    # _get_tv_bridge()'s lock, which guards an async subprocess spawn.
    global _execution_listener
    if _execution_listener is None:
        cfg = _config or Config.from_env()
        _config = cfg
        _execution_listener = ExecutionListener(cfg.gateway_url, toolkit._store)
    _execution_listener.start()
```

- [ ] **Step 5: Drop the stale "(streaming)" qualifier in the welcome message**

Find (currently line 588):

```python
            f"**Account P&L** (streaming)\n{pnl_text}\n\n"
```

Replace with:

```python
            f"**Account P&L**\n{pnl_text}\n\n"
```

- [ ] **Step 6: Verify the module still imports cleanly and the full suite is unaffected**

Run: `python -c "import claudia.app"`
Expected: no output, exit code 0.

Run: `pytest -q`
Expected: 242 passed (223 baseline + 19 new from Task 1/2 — this task adds no new
tests and must not break any existing ones).

- [ ] **Step 7: Commit**

```bash
git add claudia/app.py
git commit -m "feat: rewire app.py from PnLStreamer to ExecutionListener"
```

---

### Task 4: Rewire `agent.py` to `ExecutionListener`

**Files:**
- Modify: `claudia/agent.py`

- [ ] **Step 1: Update `_get_live_pnl`**

Find (currently lines 611-617):

```python
    def _get_live_pnl(self) -> str:
        """Format the latest live P&L snapshot from PnLStreamer's background WebSocket
        subscription (claudia/pnl_stream.py). Returns a friendly message if no
        snapshot has been recorded yet — never raises."""
        from claudia.pnl_stream import format_pnl_snapshot
        latest = self._toolkit._store.get_latest_pnl()
        return format_pnl_snapshot(latest)
```

Replace with:

```python
    def _get_live_pnl(self) -> str:
        """Format the latest live P&L snapshot recorded by ExecutionListener's
        execution-triggered background WebSocket subscription
        (claudia/execution_listener.py). Returns a friendly message if no
        snapshot has been recorded yet — never raises."""
        from claudia.execution_listener import format_pnl_snapshot
        latest = self._toolkit._store.get_latest_pnl()
        return format_pnl_snapshot(latest)
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `pytest tests/test_agent.py -k get_live_pnl -v`
Expected: 3 passed (these tests only exercise `_get_live_pnl`'s output shape via a
mocked `self._toolkit._store.get_latest_pnl`, unaffected by which module
`format_pnl_snapshot` is imported from).

Run: `pytest tests/test_agent.py -q`
Expected: all tests passed, same count as before this task.

- [ ] **Step 3: Commit**

```bash
git add claudia/agent.py
git commit -m "feat: rewire agent.py's get_live_pnl tool to ExecutionListener's module"
```

---

### Task 5: Delete the old `PnLStreamer`

**Files:**
- Delete: `claudia/pnl_stream.py`
- Delete: `tests/test_pnl_stream.py`

Nothing references these files anymore after Tasks 3-4 — `app.py` and `agent.py` both
import from `claudia.execution_listener` now.

- [ ] **Step 1: Confirm nothing in the source or test tree still references the old module**

Run: `grep -rn "pnl_stream\|PnLStreamer" claudia/ tests/`
Expected: no matches. (This intentionally excludes `CLAUDE.md`, which still references
`pnl_stream`/`PnLStreamer` at this point — that's updated in Task 6, next. Any match
inside `claudia/` or `tests/` here means Task 3 or 4 was missed and must be fixed
before proceeding.)

- [ ] **Step 2: Delete the files**

```bash
git rm claudia/pnl_stream.py tests/test_pnl_stream.py
```

- [ ] **Step 3: Verify the full suite**

Run: `pytest -q`
Expected: 229 passed (242 from Task 3 minus the 13 tests in the deleted
`tests/test_pnl_stream.py`: 3 `_run_once` tests + 4 `_run_with_retry` tests + 3
lifecycle tests + 3 `format_pnl_snapshot` tests = 13).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: remove superseded PnLStreamer (replaced by ExecutionListener)"
```

---

### Task 6: CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the "Live P&L Streaming" section**

Find (currently lines 147-172):

```markdown
### Live P&L Streaming

`claudia/pnl_stream.py`'s `PnLStreamer` runs a background WebSocket subscription to
`ibkr_core_mcp`'s `spl` (P&L) topic for the life of the process — one subscription
shared across all concurrent Chainlit sessions, started from `on_chat_start` (singleton,
same pattern as `ConnectivityChecker`). Every tick is written to
`SQLiteStore.pnl_snapshots` via `record_pnl_snapshot()`.

ClaudIA does **not** run `ibkr_core_mcp.mcp_server` — `PnLStreamer` is a self-contained
subscriber, consistent with ClaudIA's direct-import architecture (see top of this file).
Retry/backoff on disconnect mirrors `ibkr_core_mcp.mcp_server._stream_loop_with_retry`'s
shape (delays: 5s, 10s, 30s, 60s).

Both surfaces render via the same `format_pnl_snapshot()` helper (`claudia/pnl_stream.py`)
so they can't drift out of sync: IBKR's `spl` topic sends incremental ticks, so any single
numeric field can be `None` on the latest row even while the stream is healthy —
`format_pnl_snapshot()` renders each field independently ("n/a" for that one field only)
rather than discarding the whole snapshot. The "stream connecting…" fallback only appears
before the very first snapshot has ever been recorded.

Surfaced two ways:
- **`get_live_pnl` tool** (`claudia/agent.py`, local tool) — on-demand, reads
  `SQLiteStore.get_latest_pnl()` directly.
- **Opening status block** (`claudia/app.py::on_chat_start`) — an "Account P&L
  (streaming)" section in the session-start welcome message.

Design spec: `docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md`
```

Replace with:

```markdown
### Execution-Triggered P&L Checks

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

ClaudIA does **not** run `ibkr_core_mcp.mcp_server` — `ExecutionListener` is a
self-contained subscriber, consistent with ClaudIA's direct-import architecture (see top
of this file). Retry/backoff on disconnect mirrors
`ibkr_core_mcp.mcp_server._stream_loop_with_retry`'s shape (delays: 5s, 10s, 30s, 60s).

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
```

- [ ] **Step 2: Confirm no stray references remain**

Run: `grep -rn "pnl_stream\|PnLStreamer" claudia/ tests/ CLAUDE.md`
Expected: no matches anywhere in the repo now.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document ExecutionListener, replacing PnLStreamer docs"
```

---

### Task 7: Full-suite verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full unit test suite**

Run: `pytest -q`
Expected: 229 passed, 0 failed (223 baseline + 19 new in `tests/test_execution_listener.py`
− 13 removed with `tests/test_pnl_stream.py`).

- [ ] **Step 2: Lint the touched files only**

This repo has pre-existing, unrelated `ruff` findings elsewhere (8 pre-existing `E402`
errors in `tests/test_agent.py`, confirmed present on `main` before this work started)
— so the gate here is scoped to files this plan touches:

Run: `ruff check claudia/execution_listener.py claudia/app.py claudia/agent.py tests/test_execution_listener.py`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm every new test asserts real behavior**

Re-read `tests/test_execution_listener.py`. Confirm each assertion checks a specific
value (exact `record_pnl_snapshot` call args, exact `True`/`False` return values, exact
await counts, exact backoff delay sequences) — none should be a vacuous
`assert result is not None` or `assert True`.

- [ ] **Step 4: Manual smoke check (best-effort, requires a running IBKR gateway)**

Not required to consider this plan complete (no live gateway available at plan-writing
time), but if one is available before merging:

```bash
chainlit run claudia/app.py
```

Open the chat, confirm the welcome message includes an "**Account P&L**" section
(either a formatted snapshot or the "not yet available" message). Place a small test
order (or wait for a natural fill from any source — mobile, TWS, web), and confirm
`get_live_pnl` returns a fresh, non-"not yet available" snapshot shortly after.
