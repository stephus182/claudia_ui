# Live P&L Streaming in ClaudIA — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ClaudIA a live account P&L data path — a background WebSocket subscriber that keeps `pnl_snapshots` populated, plus a `get_live_pnl` tool and an opening-status-block line to surface it.

**Architecture:** A new `PnLStreamer` class (`claudia/pnl_stream.py`) opens its own `ibkr_core_mcp.IBKRWebSocket`, subscribes to the `spl` P&L topic, and writes every tick to `SQLiteStore.record_pnl_snapshot()`. It runs as a process-level singleton background `asyncio.Task` (mirrors `ConnectivityChecker` in `claudia/status.py`), started from `on_chat_start`. `get_latest_pnl()` is read by a new `get_live_pnl` local tool (`claudia/agent.py`) and by the session-start welcome message (`claudia/app.py`).

**Tech Stack:** Python 3.11+, `ibkr_core_mcp` (editable install, already has `IBKRWebSocket`/`PnLUpdate`/`SQLiteStore.record_pnl_snapshot`/`get_latest_pnl` as of commit `3b83db0`), `asyncio`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), Chainlit.

**Spec:** `docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md`

---

## Before you start

This plan assumes a worktree already exists at
`/Users/steph/Claude_Projects/claudia_ui/.worktrees/live-pnl-streaming` (branch
`feature/live-pnl-streaming`), with its own `.venv` where both `claudia_ui` and
`ibkr_core_mcp` (from `/Users/steph/Claude_Projects/ibkr_core_mcp`, main branch,
commit `3b83db0` or later) are installed editable:

```bash
cd /Users/steph/Claude_Projects/claudia_ui/.worktrees/live-pnl-streaming
source .venv/bin/activate
```

All commands below assume this venv is active and this is the working directory.

Baseline (already verified): `pytest -q` → 207 passed.

---

### Task 1: `PnLStreamer` — failing tests first

**Files:**
- Create: `tests/test_pnl_stream.py`

`claudia/pnl_stream.py` does not exist yet — these tests will fail on import.
That is expected and confirms TDD is being followed correctly.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for claudia/pnl_stream.py — background P&L WebSocket subscriber."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
```

- [ ] **Step 2: Run tests to verify they fail on import**

Run: `pytest tests/test_pnl_stream.py -q`
Expected: `ModuleNotFoundError: No module named 'claudia.pnl_stream'` (or
`ImportError`) — confirms the module doesn't exist yet.

---

### Task 2: Implement `PnLStreamer`

**Files:**
- Create: `claudia/pnl_stream.py`

- [ ] **Step 1: Write the module**

```python
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
        BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")).apply(session)
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

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_pnl_stream.py -q -v`
Expected: 9 passed (all tests from Task 1).

- [ ] **Step 3: Commit**

```bash
git add claudia/pnl_stream.py tests/test_pnl_stream.py
git commit -m "feat: add PnLStreamer background WebSocket subscriber for live P&L

Mirrors ConnectivityChecker's background-task shape and
ibkr_core_mcp.mcp_server._stream_loop_with_retry's retry/backoff pattern.
Keeps SQLiteStore.pnl_snapshots populated so get_latest_pnl() has data
without ClaudIA needing to run ibkr_core_mcp.mcp_server."
```

---

### Task 3: Wire `PnLStreamer` into `app.py` (singleton + opening status block)

**Files:**
- Modify: `claudia/app.py`

No new tests for this task — `on_chat_start` has no existing unit test
coverage today (`trade_status`, `status_block`, `trade_context`, and
`_background_flex_sync` are all inline and untested; see spec's Testing
section for why this task intentionally follows that existing convention
rather than introducing a one-off testable-helper extraction). `PnLStreamer`
itself (Task 1–2) and `get_live_pnl` (Task 4–5) already cover the underlying
data path with real tests.

- [ ] **Step 1: Add the import**

In `claudia/app.py`, find this line (currently at line 248):

```python
from claudia.status import ConnectivityChecker
```

Add directly after it:

```python
from claudia.status import ConnectivityChecker
from claudia.pnl_stream import PnLStreamer
```

- [ ] **Step 2: Add the module-level singleton**

Find this line (currently at line 267):

```python
_connectivity_checker: ConnectivityChecker | None = None
```

Add directly after it:

```python
_connectivity_checker: ConnectivityChecker | None = None
_pnl_streamer: PnLStreamer | None = None
```

- [ ] **Step 3: Start the singleton in `on_chat_start`**

Find this block (currently around lines 520–535):

```python
    # Start connectivity monitor (singleton — persists across sessions)
    global _connectivity_checker
    if _connectivity_checker is None:
        cfg = _config or Config.from_env()
        _config = cfg  # cache for future sessions; may already be set by GDriveSync block above
        _connectivity_checker = ConnectivityChecker(
            gateway_url=cfg.gateway_url,
            gdrive_token_file=cfg.gdrive_token_file,
            tv_bridge=_tv_bridge,
            gdrive_sync=_gdrive_sync,
        )
    # Call unconditionally — start() is idempotent and restarts a cancelled task
    _connectivity_checker.start()
    # Update bridge if TradingView became available after the checker was constructed
    if _tv_bridge is not None:
        _connectivity_checker.set_tv_bridge(_tv_bridge)
```

Add directly after it (still inside `on_chat_start`, before the `# Build agent
for this session` comment that follows):

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

- [ ] **Step 4: Extend the opening status block with a Live P&L section**

Find this block (currently lines 559–577):

```python
    ibkr_offline = False
    try:
        gateway_up = await cl.make_async(toolkit.client.ping)()
        if not gateway_up:
            raise ConnectionError("IBKR gateway not reachable")
        (opening_text, _), (orders_text, _), (positions_text, _) = await _asyncio.gather(
            cl.make_async(toolkit.execute)("get_account_summary", {}),
            cl.make_async(toolkit.execute)("get_live_orders", {}),
            cl.make_async(toolkit.execute)("get_positions", {}),
        )
        status_block = (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        )
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        status_block = "*IBKR gateway not connected — data will load when gateway is online.*"
        ibkr_offline = True
```

Replace it with:

```python
    ibkr_offline = False
    try:
        gateway_up = await cl.make_async(toolkit.client.ping)()
        if not gateway_up:
            raise ConnectionError("IBKR gateway not reachable")
        (opening_text, _), (orders_text, _), (positions_text, _), latest_pnl = await _asyncio.gather(
            cl.make_async(toolkit.execute)("get_account_summary", {}),
            cl.make_async(toolkit.execute)("get_live_orders", {}),
            cl.make_async(toolkit.execute)("get_positions", {}),
            cl.make_async(toolkit._store.get_latest_pnl)(),
        )
        _pnl_fields = (
            [latest_pnl.get(k) for k in ("dpl", "upl", "nl", "uel", "mv")]
            if latest_pnl is not None else []
        )
        if latest_pnl is not None and all(v is not None for v in _pnl_fields):
            dpl, upl, nl, uel, mv = _pnl_fields
            pnl_text = (
                f"Daily: {dpl:+.2f} | Unrealized: {upl:+.2f} | Net Liq: {nl:.2f} | "
                f"Excess Liq: {uel:.2f} | Mkt Value: {mv:.2f}"
            )
        else:
            pnl_text = "_Live P&L stream connecting…_"
        status_block = (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Live P&L** (streaming)\n{pnl_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        )
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        status_block = "*IBKR gateway not connected — data will load when gateway is online.*"
        ibkr_offline = True
```

- [ ] **Step 5: Verify the module still imports cleanly and the full suite is unaffected**

Run: `python -c "import claudia.app"`
Expected: no output, exit code 0 (import succeeds — confirms no syntax errors
and no circular import between `claudia.app` and the new `claudia.pnl_stream`).

Run: `pytest -q`
Expected: 207 passed (unchanged — this task adds no new tests and must not
break any existing ones).

- [ ] **Step 6: Commit**

```bash
git add claudia/app.py
git commit -m "feat: start PnLStreamer at session start, show live P&L in welcome message

Singleton pattern matches _connectivity_checker (sync construction, no lock
needed). Opening status block gets a 4th section: formatted snapshot when
available, a 'stream connecting' note when the streamer hasn't recorded one
yet, and no mention at all when IBKR itself is offline (existing pattern)."
```

---

### Task 4: `get_live_pnl` tool — failing tests first

**Files:**
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_agent.py`, directly after
`test_handle_local_tool_unknown_name` (currently ending at line 222, right
before the `# ── ClaudIAAgent._extract_decisions` section header):

```python
def test_handle_local_tool_get_live_pnl_populated():
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = {
        "account": "DU1234567.Core", "dpl": 12.5, "nl": 10000.0,
        "upl": 3.0, "uel": 9000.0, "mv": 5000.0,
    }
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "DU1234567.Core" in result
    assert "+12.50" in result
    assert "10000.00" in result


def test_handle_local_tool_get_live_pnl_none():
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = None
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "not yet available" in result.lower()


def test_handle_local_tool_get_live_pnl_partial_fields_format_as_na():
    """A snapshot with some None numeric fields (early/partial tick) must format
    those fields as 'n/a' rather than raising a format-spec TypeError."""
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = {
        "account": "DU1234567.Core", "dpl": None, "nl": 10000.0,
        "upl": None, "uel": None, "mv": None,
    }
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "n/a" in result
    assert "10000.00" in result  # the one populated field still formats normally
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k get_live_pnl -v`
Expected: 3 failed — `_handle_local_tool` returns `"Unknown local tool:
get_live_pnl"` for all three (assertions fail because that string doesn't
contain "DU1234567.Core"/"not yet available"/"n/a").

---

### Task 5: Implement `get_live_pnl` tool

**Files:**
- Modify: `claudia/agent.py`

- [ ] **Step 1: Add the tool definition to `_LOCAL_TOOLS`**

Find the end of the `_LOCAL_TOOLS` list (currently lines 177–200, the
`fetch_web_page` entry immediately followed by `]`):

```python
    {
        "name": "fetch_web_page",
        "description": (
            "Fetch and read any public web page — documentation, financial news, research, broker pages. "
            "Returns the page content as readable text. Use when the user asks you to look at a URL, "
            "read documentation, or research something online. "
            "Does not work on pages that require JavaScript rendering or login."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to fetch, e.g. 'https://example.com/page'.",
                },
                "extract": {
                    "type": "string",
                    "description": "Optional: specific section or information to focus on.",
                },
            },
            "required": ["url"],
        },
    },
]
```

Replace the closing `]` with a new entry followed by `]`:

```python
    {
        "name": "fetch_web_page",
        "description": (
            "Fetch and read any public web page — documentation, financial news, research, broker pages. "
            "Returns the page content as readable text. Use when the user asks you to look at a URL, "
            "read documentation, or research something online. "
            "Does not work on pages that require JavaScript rendering or login."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to fetch, e.g. 'https://example.com/page'.",
                },
                "extract": {
                    "type": "string",
                    "description": "Optional: specific section or information to focus on.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_live_pnl",
        "description": (
            "Get the latest streamed account P&L snapshot (daily P&L, unrealized P&L, "
            "net liquidity, excess liquidity, market value) from ClaudIA's live WebSocket "
            "P&L subscription. Use when the user asks for current/live/real-time P&L. "
            "For historical performance analysis use get_analytics instead."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
```

- [ ] **Step 2: Add the dispatch handler**

Find this line in `_handle_local_tool` (currently line 595–596):

```python
        if name == "fetch_web_page":
            return self._fetch_web_page(inputs)
        return f"Unknown local tool: {name}"
```

Replace with:

```python
        if name == "fetch_web_page":
            return self._fetch_web_page(inputs)
        if name == "get_live_pnl":
            return self._get_live_pnl()
        return f"Unknown local tool: {name}"
```

- [ ] **Step 3: Add the `_get_live_pnl` method**

Add this method directly after `_handle_local_tool` (which currently ends
right before the `@staticmethod` / `_validate_public_url` method):

```python
    def _get_live_pnl(self) -> str:
        """Format the latest live P&L snapshot from PnLStreamer's background WebSocket
        subscription (claudia/pnl_stream.py). Returns a friendly message if no
        snapshot has been recorded yet — never raises."""
        latest = self._toolkit._store.get_latest_pnl()
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -k get_live_pnl -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full agent test file to confirm no regressions**

Run: `pytest tests/test_agent.py -q`
Expected: all tests passed (previous count + 3 new).

- [ ] **Step 6: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "feat: add get_live_pnl tool reading PnLStreamer's latest snapshot

Local tool (like list_doc_versions/fetch_web_page) — reads
toolkit._store.get_latest_pnl() directly rather than via toolkit.execute(),
since this surfaces ClaudIA's own streaming state, not an IBKR REST call."
```

---

### Task 6: CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a short section documenting the feature**

Find the `### Live Orders Two-Call Pattern` section (search for
`Source: [IBKR Campus — Request & Modify Orders]` — the line immediately
before the `---` that ends that section). Add a new section directly after
that `---`:

```markdown
### Live P&L Streaming

`claudia/pnl_stream.py`'s `PnLStreamer` runs a background WebSocket
subscription to `ibkr_core_mcp`'s `spl` (P&L) topic for the life of the
process — one subscription shared across all concurrent Chainlit sessions,
started from `on_chat_start` (singleton, same pattern as
`ConnectivityChecker`). Every tick is written to `SQLiteStore.pnl_snapshots`
via `record_pnl_snapshot()`.

ClaudIA does **not** run `ibkr_core_mcp.mcp_server` — `PnLStreamer` is a
self-contained subscriber, consistent with ClaudIA's direct-import
architecture (see top of this file). Retry/backoff on disconnect mirrors
`ibkr_core_mcp.mcp_server._stream_loop_with_retry`'s shape (delays: 5s, 10s,
30s, 60s).

Surfaced two ways:
- **`get_live_pnl` tool** (`claudia/agent.py`, local tool) — on-demand, reads
  `SQLiteStore.get_latest_pnl()` directly.
- **Opening status block** (`claudia/app.py::on_chat_start`) — a "Live P&L"
  line in the session-start welcome message; shows a "stream connecting" note
  until the first tick arrives.

Design spec: `docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md`

---
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document PnLStreamer / get_live_pnl live P&L streaming"
```

---

### Task 7: Full-suite verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full unit test suite**

Run: `pytest -m "not integration" -q`
Expected: all tests pass — baseline was 207 passed; this plan adds 9
(`test_pnl_stream.py`) + 3 (`test_agent.py`) = 12 new tests, so expect 219
passed, 0 failed.

- [ ] **Step 2: Lint the touched files only**

The repo has pre-existing, unrelated `ruff` findings elsewhere (verified
baseline: 14 errors before this plan started) — so the gate here is scoped to
files this plan touches, not the whole repo:

Run: `ruff check claudia/pnl_stream.py claudia/app.py claudia/agent.py tests/test_pnl_stream.py tests/test_agent.py`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm every new test asserts real behavior**

Re-read `tests/test_pnl_stream.py` and the 3 new tests in `tests/test_agent.py`
added in this plan. Confirm each assertion checks a specific value (exact
`record_pnl_snapshot` call args, exact formatted strings, exact retry/attempt
counts) — none should be a vacuous `assert result is not None` or
`assert True`.

- [ ] **Step 4: Manual smoke check (best-effort, requires a running IBKR gateway)**

Not required to consider this plan complete (no live gateway available at
plan-writing time), but if one is available before merging:

```bash
chainlit run claudia/app.py
```

Open the chat, confirm the welcome message includes a "**Live P&L**
(streaming)" section (either a formatted snapshot or "stream connecting…"),
then ask "what's my live P&L?" and confirm the agent calls `get_live_pnl` and
returns a sensible answer.
