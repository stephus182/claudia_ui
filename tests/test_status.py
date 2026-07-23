from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests as req

from claudia.status import ConnectivityChecker, ServiceStatus


def _ibkr_ok_response():
    """Mock response: gateway up, session authenticated and connected."""
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "iserver": {"authStatus": {"authenticated": True, "connected": True}}
    }
    return m


def _ibkr_unauthed_response():
    """Mock response: gateway up but session not authenticated (e.g. before login)."""
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": False}}
    }
    return m


@pytest.fixture
def checker(tmp_path):
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=tmp_path / "token.json",
    )


def test_check_ibkr_ok(checker):
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()) as mock_get:
        assert checker.check_ibkr() is True
        mock_get.assert_called_once_with(
            "https://localhost:5055/v1/api/tickle",
            timeout=3,
            verify=False,
        )


def test_check_ibkr_unauthenticated(checker):
    """Gateway up but session not logged in → False."""
    with patch("claudia.status.requests.get", return_value=_ibkr_unauthed_response()):
        assert checker.check_ibkr() is False


def test_check_ibkr_non_200(checker):
    m = MagicMock()
    m.status_code = 401
    with patch("claudia.status.requests.get", return_value=m):
        assert checker.check_ibkr() is False


def test_check_ibkr_connection_error(checker):
    with patch("claudia.status.requests.get", side_effect=ConnectionError("refused")):
        assert checker.check_ibkr() is False


def test_check_ibkr_timeout(checker):
    with patch("claudia.status.requests.get", side_effect=req.Timeout("timeout")):
        assert checker.check_ibkr() is False


def test_check_ibkr_ok_stashes_auth_status(checker):
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
        checker.check_ibkr()
    assert checker._last_ibkr_auth_status == {"authenticated": True, "connected": True}


def test_check_ibkr_soft_timeout_stashes_auth_status(checker):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    with patch("claudia.status.requests.get", return_value=m):
        assert checker.check_ibkr() is False
    assert checker._last_ibkr_auth_status == {"authenticated": False, "connected": True}


def test_check_ibkr_non_200_clears_auth_status(checker):
    checker._last_ibkr_auth_status = {"authenticated": True, "connected": True}
    m = MagicMock()
    m.status_code = 401
    with patch("claudia.status.requests.get", return_value=m):
        checker.check_ibkr()
    assert checker._last_ibkr_auth_status == {}


def test_check_ibkr_exception_clears_auth_status(checker):
    checker._last_ibkr_auth_status = {"authenticated": True, "connected": True}
    with patch("claudia.status.requests.get", side_effect=ConnectionError("refused")):
        checker.check_ibkr()
    assert checker._last_ibkr_auth_status == {}


def test_check_gdrive_falls_back_to_token_file_when_no_sync(checker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    checker._gdrive_token_file = token
    assert checker.check_gdrive() is True


def test_check_gdrive_token_file_missing_no_sync(checker, tmp_path):
    checker._gdrive_token_file = tmp_path / "missing.json"
    assert checker.check_gdrive() is False


def test_check_gdrive_uses_ping_when_sync_provided(checker):
    from unittest.mock import MagicMock
    sync = MagicMock()
    sync.ping.return_value = True
    checker._gdrive_sync = sync
    assert checker.check_gdrive() is True
    sync.ping.assert_called_once()


def test_check_gdrive_ping_failure_returns_false(checker):
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
    s = checker.get_status()
    assert s == {
        "ibkr":   ServiceStatus.UNKNOWN,
        "gdrive": ServiceStatus.UNKNOWN,
        "tv":     ServiceStatus.UNKNOWN,
    }


def test_get_status_returns_copy(checker):
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
    async def _subscriber(msg: str) -> None:
        pass
    unsubscribe = checker.subscribe(_subscriber)
    assert callable(unsubscribe)
    assert _subscriber in checker._subscribers


@pytest.mark.asyncio
async def test_send_alert_notifies_all_subscribers_with_formatted_message(checker):
    received_a, received_b = [], []
    async def _sub_a(msg: str) -> None:
        received_a.append(msg)
    async def _sub_b(msg: str) -> None:
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
        received.append(msg)
    checker.subscribe(_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.OK)

    assert received == []


@pytest.mark.asyncio
async def test_send_alert_unsubscribed_callback_stops_receiving(checker):
    received = []
    async def _subscriber(msg: str) -> None:
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
        received.append(("first", msg))
        unsubscribe_first()  # mutate the subscriber list mid-notify
    unsubscribe_first = checker.subscribe(_self_unsubscribing)

    async def _second(msg: str) -> None:
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
        raise RuntimeError("subscriber blew up")
    async def _good_subscriber(msg: str) -> None:
        received.append(msg)
    checker.subscribe(_broken_subscriber)
    checker.subscribe(_good_subscriber)

    await checker._send_alert("ibkr", ServiceStatus.UNKNOWN, ServiceStatus.ERROR)

    assert len(received) == 1


# ── State transition tests (async) ────────────────────────────────────────

@pytest.fixture
def checker_with_token(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=token,
    )


@pytest.mark.asyncio
async def test_run_checks_unknown_to_ok_no_alert(checker_with_token):
    """UNKNOWN → OK at startup: _send_alert runs but notifies no subscribers."""
    received = []
    async def _subscriber(msg: str) -> None:
        received.append(msg)
    checker_with_token.subscribe(_subscriber)

    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    assert checker_with_token.get_status()["gdrive"] == ServiceStatus.OK
    # UNKNOWN→OK: no alert dispatched to subscribers
    assert received == []


@pytest.mark.asyncio
async def test_run_checks_unknown_to_error_emits_alert(checker):
    """UNKNOWN → ERROR at startup: _send_alert called for each failing service."""
    with patch("claudia.status.requests.get", side_effect=ConnectionError()), \
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

    with patch("claudia.status.requests.get", side_effect=ConnectionError()), \
         patch.object(checker_with_token, "_send_alert", new_callable=AsyncMock) as mock_alert:
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.OK
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_recovers_silently_from_soft_timeout(checker_with_token):
    """OK -> soft-timeout -> ssodh/init succeeds -> stays OK, no alert."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    recovery_resp = MagicMock()
    recovery_resp.status_code = 200
    recovery_resp.json.return_value = {"authenticated": True, "connected": True}

    with patch(
        "claudia.status.requests.get",
        side_effect=[soft_timeout_resp, _ibkr_ok_response()],
    ), patch(
        "claudia.status.requests.post", return_value=recovery_resp
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ) as mock_alert:
        await checker_with_token._run_checks()

    mock_post.assert_called_once()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert ibkr_calls == []  # never visibly disconnected


@pytest.mark.asyncio
async def test_run_checks_recovery_succeeds_but_recheck_still_fails(checker_with_token):
    """Recovery POST returns 200, but the immediate re-check still fails (possibly
    with a different signature than the original) — must still produce exactly one
    normal disconnect alert, not a masked or duplicated one."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    hard_disconnect_resp = MagicMock()
    hard_disconnect_resp.status_code = 200
    hard_disconnect_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": False}}
    }
    recovery_resp = MagicMock()
    recovery_resp.status_code = 200
    recovery_resp.json.return_value = {"authenticated": True, "connected": True}

    with patch(
        "claudia.status.requests.get",
        side_effect=[soft_timeout_resp, hard_disconnect_resp],
    ), patch(
        "claudia.status.requests.post", return_value=recovery_resp
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ) as mock_alert:
        await checker_with_token._run_checks()

    mock_post.assert_called_once()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.OK
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_soft_recovery_failure_falls_back_to_disconnect_alert(checker_with_token):
    """OK -> soft-timeout -> ssodh/init fails -> normal ERROR alert, same as today."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    failed_recovery = MagicMock()
    failed_recovery.status_code = 500

    with patch(
        "claudia.status.requests.get", return_value=soft_timeout_resp
    ), patch(
        "claudia.status.requests.post", return_value=failed_recovery
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ) as mock_alert:
        await checker_with_token._run_checks()

    mock_post.assert_called_once()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.OK
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_no_recovery_attempt_from_unknown_state(checker_with_token):
    """UNKNOWN -> soft-timeout-shaped response: never attempt recovery — this is the
    fresh/settling-login window, exactly what the existing no-proactive-reauth rule
    protects. Must go straight to a normal ERROR, untouched."""
    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }

    with patch(
        "claudia.status.requests.get", return_value=soft_timeout_resp
    ), patch(
        "claudia.status.requests.post"
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ):
        await checker_with_token._run_checks()

    mock_post.assert_not_called()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_no_recovery_attempt_from_error_state(checker_with_token):
    """ERROR -> soft-timeout-shaped response: never attempt recovery — only a
    transition FROM a previously-confirmed OK state may trigger it, per the same
    rule that excludes UNKNOWN. A prior real disconnect that happens to look
    soft-timeout-shaped on the next poll must not silently paper over it."""
    checker_with_token._status["ibkr"] = ServiceStatus.ERROR

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }

    with patch(
        "claudia.status.requests.get", return_value=soft_timeout_resp
    ), patch(
        "claudia.status.requests.post"
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ):
        await checker_with_token._run_checks()

    mock_post.assert_not_called()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_no_recovery_attempt_on_hard_disconnect(checker_with_token):
    """OK -> connected:false (hard disconnect, e.g. competing session or container
    down): never attempt recovery — ssodh/init cannot fix this, only a real
    browser+2FA login can."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    hard_disconnect_resp = MagicMock()
    hard_disconnect_resp.status_code = 200
    hard_disconnect_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": False}}
    }

    with patch(
        "claudia.status.requests.get", return_value=hard_disconnect_resp
    ), patch(
        "claudia.status.requests.post"
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ):
        await checker_with_token._run_checks()

    mock_post.assert_not_called()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_error_to_ok_emits_reconnect(checker):
    """ERROR → OK: _send_alert called with (service, ERROR, OK)."""
    checker._status["ibkr"] = ServiceStatus.ERROR
    checker._status["gdrive"] = ServiceStatus.ERROR

    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
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

    with patch("claudia.status.requests.get", side_effect=ConnectionError()), \
         patch.object(checker_with_token, "_send_alert", new_callable=AsyncMock) as mock_alert:
        await checker_with_token._run_checks()

    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert ibkr_calls == []


@pytest.mark.asyncio
async def test_run_checks_tv_unknown_when_no_bridge(checker):
    """TV without a bridge stays UNKNOWN, not ERROR."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
         patch.object(checker, "_send_alert", new_callable=AsyncMock):
        await checker._run_checks()

    assert checker.get_status()["tv"] == ServiceStatus.UNKNOWN


# ── _attempt_soft_recovery() ────────────────────────────────────────────────

def test_attempt_soft_recovery_success(checker):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"authenticated": True, "connected": True, "competing": False}
    with patch("claudia.status.requests.post", return_value=m) as mock_post:
        assert checker._attempt_soft_recovery() is True
    mock_post.assert_called_once_with(
        "https://localhost:5055/v1/api/iserver/auth/ssodh/init",
        json={"publish": True, "compete": False},
        timeout=5,
        verify=False,
    )


def test_attempt_soft_recovery_200_but_body_says_not_authenticated_returns_false(checker):
    """HTTP 200 alone isn't success — IBKR returns 200 with authenticated:false in
    the body for e.g. a real competing session denying the reconnect. Same lesson
    check_ibkr() already learned about this gateway."""
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"authenticated": False, "connected": True, "competing": True}
    with patch("claudia.status.requests.post", return_value=m):
        assert checker._attempt_soft_recovery() is False


def test_attempt_soft_recovery_non_200_returns_false(checker):
    m = MagicMock()
    m.status_code = 500
    with patch("claudia.status.requests.post", return_value=m):
        assert checker._attempt_soft_recovery() is False


def test_attempt_soft_recovery_exception_returns_false(checker):
    with patch("claudia.status.requests.post", side_effect=req.ConnectionError()):
        assert checker._attempt_soft_recovery() is False


def test_attempt_soft_recovery_never_sets_compete_true(checker):
    """Regression guard: compete must never be true — it would force-evict a
    concurrent IBKR Mobile/TWS session."""
    m = MagicMock()
    m.status_code = 200
    with patch("claudia.status.requests.post", return_value=m) as mock_post:
        checker._attempt_soft_recovery()
    assert mock_post.call_args.kwargs["json"]["compete"] is False


@pytest.mark.asyncio
async def test_stop_cancels_task(checker):
    """stop() cancels the poll loop; start() can restart it."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
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
