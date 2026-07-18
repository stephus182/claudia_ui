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
    """UNKNOWN → OK at startup: _send_alert is called but no Chainlit message sent."""
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
         patch("chainlit.Message") as mock_msg:
        mock_msg.return_value.send = AsyncMock()
        await checker_with_token._run_checks()

    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    assert checker_with_token.get_status()["gdrive"] == ServiceStatus.OK
    # UNKNOWN→OK: no Chainlit message instantiated
    mock_msg.assert_not_called()


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
    with patch("claudia.status.requests.post", return_value=m) as mock_post:
        assert checker._attempt_soft_recovery() is True
    mock_post.assert_called_once_with(
        "https://localhost:5055/v1/api/iserver/auth/ssodh/init",
        json={"publish": True, "compete": False},
        timeout=5,
        verify=False,
    )


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
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()), \
         patch("chainlit.Message.send", AsyncMock()):
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
