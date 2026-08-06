"""Tests for ConnectivityChecker — the GDrive/TradingView poller and the IBKR dot.

**The IBKR half no longer polls.** On 2026-08-06 the `/tickle` read and the `ssodh/init`
soft-recovery write both moved to `claudia.gateway_session`, which owns the session
lifecycle; `check_ibkr` is now a cached lookup on that owner. The tests for the moved
behaviour moved with it, to `tests/test_gateway_session.py` — they describe the owner now,
not this module.

What is pinned here is the seam: the dot reports exactly what the owner says, and forms no
opinion of its own. That is what makes the dot and the dashboard incapable of disagreeing,
which they demonstrably could before (plan F8).
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.status import ConnectivityChecker, ServiceStatus


@contextmanager
def _ibkr_up(checker):
    """The owner reports a live session for the duration of the block."""
    previous = checker._session.is_live.return_value
    checker._session.is_live.return_value = True
    try:
        yield
    finally:
        checker._session.is_live.return_value = previous


@contextmanager
def _ibkr_down(checker):
    """The owner reports IBKR unusable for the duration of the block.

    Replaces the old `patch("claudia.status.requests.get", side_effect=ConnectionError)`.
    The distinction the checker used to draw — connection error vs 401 vs unauthenticated
    — is now the owner's to make and is tested there against the full phase set; from this
    side there is exactly one question, and `is_live()` answers it.
    """
    previous = checker._session.is_live.return_value
    checker._session.is_live.return_value = False
    try:
        yield
    finally:
        checker._session.is_live.return_value = previous




@pytest.fixture
def session():
    """A stand-in session owner. `is_live()` is the only thing the checker asks it."""
    owner = MagicMock()
    owner.is_live.return_value = True
    return owner


@pytest.fixture
def checker(tmp_path, session):
    """A ConnectivityChecker with a throwaway token path and a fake session owner."""
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=tmp_path / "token.json",
        session=session,
    )


def test_check_ibkr_reports_what_the_owner_says(checker, session):
    """The dot is a view of `SessionState`, never an independent probe."""
    session.is_live.return_value = True
    assert checker.check_ibkr() is True
    session.is_live.return_value = False
    assert checker.check_ibkr() is False


def test_check_ibkr_makes_no_network_call_of_its_own(checker, session):
    """The whole point of the seam.

    Before 2026-08-06 this method issued its own `GET /tickle` against
    `iserver.authStatus`, while the dashboard read `/portfolio/*` — two subsystems with
    different prerequisites, which is how the dot could go red while the KPI tiles kept
    printing figures (plan F6/F8).
    """
    with patch("socket.create_connection") as sock:
        checker.check_ibkr()
    sock.assert_not_called()
    session.is_live.assert_called()


def test_a_degraded_session_is_not_a_green_dot(checker, session):
    """`is_live()` means authenticated, connected AND confirmed against a data endpoint.

    An authenticated flag alone has been observed alongside a dark data half, so anything
    short of confirmed must not show as OK.
    """
    session.is_live.return_value = False
    assert checker.check_ibkr() is False


def test_check_gdrive_falls_back_to_token_file_when_no_sync(checker, tmp_path):
    """With no sync wired, the token file's presence is the best available signal."""
    token = tmp_path / "token.json"
    token.write_text("{}")
    checker._gdrive_token_file = token
    assert checker.check_gdrive() is True


def test_check_gdrive_token_file_missing_no_sync(checker, tmp_path):
    """No token file means not configured, reported as down."""
    checker._gdrive_token_file = tmp_path / "missing.json"
    assert checker.check_gdrive() is False


def test_check_gdrive_uses_ping_when_sync_provided(checker):
    """With a sync wired, a real ping outranks the token file."""
    from unittest.mock import MagicMock
    sync = MagicMock()
    sync.ping.return_value = True
    checker._gdrive_sync = sync
    assert checker.check_gdrive() is True
    sync.ping.assert_called_once()


def test_check_gdrive_ping_failure_returns_false(checker):
    """A failed ping is reported as down."""
    from unittest.mock import MagicMock
    sync = MagicMock()
    sync.ping.return_value = False
    checker._gdrive_sync = sync
    assert checker.check_gdrive() is False


def test_check_tradingview_cdp_port_open(checker):
    """CDP port accepting connections → True (requires a bridge to be configured)."""
    checker.set_tv_bridge(MagicMock())
    with patch("claudia.status.socket.create_connection"):
        assert checker.check_tradingview() is True


def test_check_tradingview_cdp_port_closed(checker):
    """CDP port refused → False."""
    checker.set_tv_bridge(MagicMock())
    with patch("claudia.status.socket.create_connection", side_effect=OSError("refused")):
        assert checker.check_tradingview() is False


def test_check_tradingview_cdp_timeout(checker):
    """CDP port timeout → False."""
    checker.set_tv_bridge(MagicMock())
    with patch("claudia.status.socket.create_connection", side_effect=OSError("timed out")):
        assert checker.check_tradingview() is False


def test_get_status_initial(checker):
    """Before the first poll every service reads UNKNOWN, not an error."""
    s = checker.get_status()
    assert s == {
        "ibkr":   ServiceStatus.UNKNOWN,
        "gdrive": ServiceStatus.UNKNOWN,
        "tv":     ServiceStatus.UNKNOWN,
    }


def test_get_status_returns_copy(checker):
    """Callers get a copy, so a UI mutation cannot corrupt the checker's state."""
    s1 = checker.get_status()
    s1["ibkr"] = "tampered"
    assert checker.get_status()["ibkr"] == ServiceStatus.UNKNOWN  # original unchanged


# ── TradingView UNKNOWN when not configured ────────────────────────────────

def test_check_tradingview_no_bridge_returns_false(checker):
    """No bridge → False; _run_checks maps this to UNKNOWN, not ERROR."""
    assert checker.check_tradingview() is False
    assert checker._tv_bridge is None


# ── Subscriber registry ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_returns_unsubscribe_callable(checker):
    """Subscribing hands back the callable that undoes it."""
    async def _subscriber(msg: str) -> None:
        """Record the alert text this subscriber received."""
        pass
    unsubscribe = checker.subscribe(_subscriber)
    assert callable(unsubscribe)
    assert _subscriber in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_notifies_all_subscribers_with_formatted_message(checker):
    """Every subscriber receives the same pre-formatted alert text."""
    received_a, received_b = [], []
    async def _sub_a(msg: str) -> None:
        """Record the alert text this subscriber received."""
        received_a.append(msg)
    async def _sub_b(msg: str) -> None:
        """Record the alert text this subscriber received."""
        received_b.append(msg)
    checker.subscribe(_sub_a)
    checker.subscribe(_sub_b)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert received_a == received_b
    assert "disconnected" in received_a[0].lower()


@pytest.mark.asyncio
async def test_send_alert_unknown_to_ok_notifies_no_subscribers(checker):
    """Mirrors the pre-existing test_run_checks_unknown_to_ok_no_alert's intent —
    startup settling into a good state is silent, not an alert-worthy transition."""
    received = []
    async def _subscriber(msg: str) -> None:
        """Record the alert text this subscriber received."""
        received.append(msg)
    checker.subscribe(_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.OK)

    assert received == []


@pytest.mark.asyncio
async def test_send_alert_unsubscribed_callback_stops_receiving(checker):
    """An unsubscribed callback receives nothing further."""
    received = []
    async def _subscriber(msg: str) -> None:
        """Record the alert text this subscriber received."""
        received.append(msg)
    unsubscribe = checker.subscribe(_subscriber)
    unsubscribe()

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert received == []
    assert _subscriber not in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_subscriber_unsubscribing_itself_midloop_does_not_skip_others(checker):
    """A subscriber that unsubscribes itself *during* its own notify callback must not
    corrupt the in-progress iteration — the copy in `for subscriber in list(...)` is what
    guarantees a second subscriber, registered after it, still gets notified in the same
    _send_alert call. (Fails if _send_alert iterates the live list instead of a copy.)"""
    received = []
    async def _self_unsubscribing(msg: str) -> None:
        """Unsubscribe from inside the callback, exercising mutation during iteration."""
        received.append(("first", msg))
        unsubscribe_first()  # mutate the subscriber list mid-notify
    unsubscribe_first = checker.subscribe(_self_unsubscribing)

    async def _second(msg: str) -> None:
        """Record that this subscriber still ran after the first unsubscribed itself."""
        received.append(("second", msg))
    checker.subscribe(_second)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    # Both notified this call; the mid-loop removal didn't skip the second subscriber.
    assert [tag for tag, _ in received] == ["first", "second"]
    # And the self-unsubscribe did take effect for future calls.
    assert _self_unsubscribing not in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_one_subscriber_exception_does_not_block_others(checker):
    """Mirrors the existing try/except-per-send pattern _send_alert already has for its
    single external call site today — a failing subscriber must not prevent other
    subscribers (or the status update itself) from proceeding."""
    received = []
    async def _broken_subscriber(msg: str) -> None:
        """Raise, so a failing subscriber cannot silence the others."""
        raise RuntimeError("subscriber blew up")
    async def _good_subscriber(msg: str) -> None:
        """Record delivery, proving the broken subscriber did not stop the fan-out."""
        received.append(msg)
    checker.subscribe(_broken_subscriber)
    checker.subscribe(_good_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert len(received) == 1


# ── State transition tests (async) ────────────────────────────────────────

@pytest.fixture
def checker_with_token(tmp_path, session):
    """A ConnectivityChecker whose Drive token file exists."""
    token = tmp_path / "token.json"
    token.write_text("{}")
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=token,
        session=session,
    )


@pytest.mark.asyncio
async def test_run_checks_unknown_to_ok_no_alert(checker_with_token):
    """UNKNOWN → OK at startup: _send_alert runs but notifies no subscribers."""
    received = []
    async def _subscriber(msg: str) -> None:
        """Record the alert text this subscriber received."""
        received.append(msg)
    checker_with_token.subscribe(_subscriber)

    with _ibkr_up(checker_with_token):
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    assert checker_with_token.get_status()["gdrive"] == ServiceStatus.OK
    # UNKNOWN→OK: no alert dispatched to subscribers
    assert received == []


@pytest.mark.asyncio
async def test_run_checks_unknown_to_error_emits_alert(checker):
    """UNKNOWN → ERROR at startup: _send_alert called for each failing service."""
    with _ibkr_down(checker), \
         patch.object(checker, "_send_alert", new_callable=AsyncMock) as mock_alert:
        await checker._run_checks()

    assert checker.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.UNKNOWN
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_ok_to_error_emits_disconnect(checker_with_token):
    """OK → ERROR: _send_alert called with (service, OK, ERROR)."""
    # Seed IBKR as OK
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    with _ibkr_down(checker_with_token), \
         patch.object(checker_with_token, "_send_alert", new_callable=AsyncMock) as mock_alert:
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.OK
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_error_to_ok_emits_reconnect(checker):
    """ERROR → OK: _send_alert called with (service, ERROR, OK)."""
    checker._status["ibkr"] = ServiceStatus.ERROR
    checker._status["gdrive"] = ServiceStatus.ERROR

    with _ibkr_up(checker), \
         patch.object(checker, "_send_alert", new_callable=AsyncMock) as mock_alert:
        # gdrive token doesn't exist in base checker fixture → stays ERROR
        await checker._run_checks()

    assert checker.get_status()["ibkr"] == ServiceStatus.OK
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.ERROR
    assert ibkr_calls[0].args[2] == ServiceStatus.OK


@pytest.mark.asyncio
async def test_run_checks_repeated_error_no_extra_alert(checker_with_token):
    """ERROR → ERROR: no alert when state is already ERROR."""
    checker_with_token._status["ibkr"] = ServiceStatus.ERROR

    with _ibkr_down(checker_with_token), \
         patch.object(checker_with_token, "_send_alert", new_callable=AsyncMock) as mock_alert:
        await checker_with_token._run_checks()

    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert ibkr_calls == []


@pytest.mark.asyncio
async def test_run_checks_tv_unknown_when_no_bridge(checker):
    """TV without a bridge stays UNKNOWN, not ERROR."""
    with _ibkr_up(checker), \
         patch.object(checker, "_send_alert", new_callable=AsyncMock):
        await checker._run_checks()

    assert checker.get_status()["tv"] == ServiceStatus.UNKNOWN


# ── _attempt_soft_recovery() ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_cancels_task(checker):
    """stop() cancels the poll loop; start() can restart it."""
    with _ibkr_up(checker):
        checker.start()
        assert checker._task is not None
        assert not checker._task.done()
        checker.stop()
        import asyncio
        await asyncio.sleep(0)   # let cancellation propagate
        assert checker._task.done()
        # restart works after cancellation
        checker.start()
        assert not checker._task.done()
        checker.stop()
