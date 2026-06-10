from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import requests as req

from claudia.status import ConnectivityChecker, ServiceStatus


@pytest.fixture
def checker(tmp_path):
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=tmp_path / "token.json",
    )


def test_check_ibkr_ok(checker):
    with patch("claudia.status.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        assert checker.check_ibkr() is True
        mock_get.assert_called_once_with(
            "https://localhost:5055/v1/api/tickle",
            timeout=3,
            verify=False,
        )


def test_check_ibkr_non_200(checker):
    with patch("claudia.status.requests.get") as mock_get:
        mock_get.return_value.status_code = 401
        assert checker.check_ibkr() is False


def test_check_ibkr_connection_error(checker):
    with patch("claudia.status.requests.get", side_effect=ConnectionError("refused")):
        assert checker.check_ibkr() is False


def test_check_ibkr_timeout(checker):
    with patch("claudia.status.requests.get", side_effect=req.Timeout("timeout")):
        assert checker.check_ibkr() is False


def test_check_gdrive_file_exists(checker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    checker._gdrive_token_file = token
    assert checker.check_gdrive() is True


def test_check_gdrive_file_missing(checker, tmp_path):
    checker._gdrive_token_file = tmp_path / "missing.json"
    assert checker.check_gdrive() is False


def test_check_tradingview_no_bridge(checker):
    assert checker.check_tradingview() is False


def test_check_tradingview_process_running(checker):
    bridge = MagicMock()
    bridge._process = MagicMock()
    bridge._process.poll.return_value = None   # None = still running
    checker._tv_bridge = bridge
    assert checker.check_tradingview() is True


def test_check_tradingview_process_exited(checker):
    bridge = MagicMock()
    bridge._process = MagicMock()
    bridge._process.poll.return_value = 1      # non-None = exited
    checker._tv_bridge = bridge
    assert checker.check_tradingview() is False


def test_check_tradingview_no_process_attr(checker):
    bridge = MagicMock(spec=[])                # no _process attribute
    checker._tv_bridge = bridge
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
    with patch("claudia.status.requests.get") as mock_get, \
         patch("chainlit.Message") as mock_msg:
        mock_msg.return_value.send = AsyncMock()
        mock_get.return_value.status_code = 200
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
async def test_run_checks_error_to_ok_emits_reconnect(checker):
    """ERROR → OK: _send_alert called with (service, ERROR, OK)."""
    checker._status["ibkr"] = ServiceStatus.ERROR
    checker._status["gdrive"] = ServiceStatus.ERROR

    with patch("claudia.status.requests.get") as mock_get, \
         patch.object(checker, "_send_alert", new_callable=AsyncMock) as mock_alert:
        mock_get.return_value.status_code = 200
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
    with patch("claudia.status.requests.get") as mock_get, \
         patch.object(checker, "_send_alert", new_callable=AsyncMock):
        mock_get.return_value.status_code = 200
        await checker._run_checks()

    assert checker.get_status()["tv"] == ServiceStatus.UNKNOWN


@pytest.mark.asyncio
async def test_stop_cancels_task(checker):
    """stop() cancels the poll loop; start() can restart it."""
    with patch("claudia.status.requests.get") as mock_get, \
         patch("chainlit.Message.send", AsyncMock()):
        mock_get.return_value.status_code = 200
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
