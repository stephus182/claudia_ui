from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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
    assert s == {"ibkr": "unknown", "gdrive": "unknown", "tv": "unknown"}


def test_get_status_returns_copy(checker):
    s1 = checker.get_status()
    s1["ibkr"] = "tampered"
    assert checker.get_status()["ibkr"] == "unknown"  # original unchanged
