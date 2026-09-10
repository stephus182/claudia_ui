"""`claudia.contract_identity` — one futures contract named in IBKR's own terms (2026-09-10)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claudia import contract_identity as ci

_ES_INFO = {
    "symbol": "ES",
    "local_symbol": "ESU6",
    "contract_month": "202609",
    "text": "ES SEP26 (50)",
    "company_name": "E-mini S&P 500",
    "maturity_date": "20260918",
    "multiplier": "50",
    "currency": "USD",
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The module cache is process-wide; every test starts empty."""
    ci.clear_cache()
    yield
    ci.clear_cache()


def test_parse_contract_info_reads_the_measured_payload():
    """The fields measured live 2026-09-10 on ES conid 649180671 map one to one."""
    identity = ci.parse_contract_info(649180671, _ES_INFO)
    assert identity is not None
    assert identity.local_symbol == "ESU6"
    assert identity.month == "SEP26"
    assert identity.expires == "2026-09-18"
    assert identity.name == "E-mini S&P 500"
    assert identity.multiplier == 50.0
    assert identity.currency == "USD"
    assert identity.label == "ESU6 · SEP26 · expires 2026-09-18"


def test_parse_contract_info_declines_without_a_local_symbol():
    """No local symbol → None: the caller keeps showing the ticker, never a guessed name."""
    assert ci.parse_contract_info(1, {**_ES_INFO, "local_symbol": ""}) is None
    assert ci.parse_contract_info(1, None) is None
    assert ci.parse_contract_info(1, "not a dict") is None


def test_parse_contract_info_tolerates_missing_month_and_expiry():
    """A contract with a local symbol but odd date fields keeps what it has; the label
    shrinks rather than inventing a date."""
    identity = ci.parse_contract_info(
        1, {"local_symbol": "ESU6", "contract_month": "2026", "maturity_date": "x"}
    )
    assert identity is not None
    assert identity.month is None and identity.expires is None and identity.multiplier is None
    assert identity.label == "ESU6"


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        ("202609", "SEP26"),
        ("202701", "JAN27"),
        ("202613", None),
        ("2026", None),
        (None, None),
        (202612, "DEC26"),
    ],
)
def test_month_token_is_ibkrs_own_mmmyy(raw, token):
    """`YYYYMM` → `MMMYY`, the token `/iserver/secdef/info` takes; junk → None."""
    assert ci.month_token(raw) == token


def test_contract_identity_reads_once_and_caches():
    """One contract-info GET per conid for the process; the second call is the cache."""
    client = MagicMock()
    client.get_contract_info.return_value = _ES_INFO
    first = ci.contract_identity(client, 649180671)
    second = ci.contract_identity(client, 649180671)
    assert first is second and first is not None and first.local_symbol == "ESU6"
    client.get_contract_info.assert_called_once_with(649180671)


def test_contract_identity_never_raises_and_does_not_cache_a_failure():
    """A failed read returns None and is retried next time — the gateway may be back."""
    client = MagicMock()
    client.get_contract_info.side_effect = RuntimeError("HTTP 500")
    assert ci.contract_identity(client, 1) is None
    client.get_contract_info.side_effect = None
    client.get_contract_info.return_value = _ES_INFO
    assert ci.contract_identity(client, 1) is not None
    assert client.get_contract_info.call_count == 2
