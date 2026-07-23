"""Tests for order_flow — order summary formatting and execute_staged_order."""

# ── Imports ──────────────────────────────────────────────────────────────────
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.order_flow import (
    _format_cancel_summary,
    _format_modify_summary,
    _format_order_summary,
    _resolve_account_id,
    execute_cancel_order,
    execute_modify_order,
    execute_staged_order,
)

# ── _resolve_account_id ──────────────────────────────────────────────────────

def test_resolve_account_id_accountid_key():
    assert _resolve_account_id([{"accountId": "U12345"}]) == "U12345"


def test_resolve_account_id_acctid_fallback():
    assert _resolve_account_id([{"acctId": "U777"}]) == "U777"


def test_resolve_account_id_id_fallback():
    assert _resolve_account_id([{"id": "U999"}]) == "U999"


def test_resolve_account_id_empty_list():
    assert _resolve_account_id([]) == ""


# ── _format_order_summary ────────────────────────────────────────────────────

def test_format_market_order():
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 100,
        "order_type": "MKT",
        "limit_price": None,
        "stop_price": None,
        "reason": "Momentum breakout",
    }
    summary = _format_order_summary(proposal)
    assert "BUY" in summary
    assert "100" in summary
    assert "AAPL" in summary
    assert "MKT" in summary
    assert "Momentum breakout" in summary
    assert "Touch ID" in summary


def test_format_limit_order():
    proposal = {
        "symbol": "NVDA",
        "action": "BUY",
        "quantity": 20,
        "order_type": "LMT",
        "limit_price": 850.0,
        "stop_price": None,
        "reason": "Support bounce",
    }
    summary = _format_order_summary(proposal)
    assert "$850.00" in summary
    assert "limit" in summary.lower()
    assert "NVDA" in summary


def test_format_stop_order():
    proposal = {
        "symbol": "MSFT",
        "action": "SELL",
        "quantity": 50,
        "order_type": "STP",
        "limit_price": None,
        "stop_price": 395.0,
        "reason": "Stop loss",
    }
    summary = _format_order_summary(proposal)
    assert "$395.00" in summary
    assert "stop" in summary.lower()
    assert "SELL" in summary


def test_format_order_missing_reason():
    proposal = {
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 10,
        "order_type": "MKT",
    }
    summary = _format_order_summary(proposal)
    assert "SPY" in summary
    assert "BUY" in summary


def test_format_order_fut_shows_sec_label():
    """FUT sec_type adds [FUT] label to the summary line."""
    proposal = {
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 5500.0,
        "sec_type": "FUT",
        "tif": "DAY",
    }
    summary = _format_order_summary(proposal)
    assert "[FUT]" in summary
    assert "ES" in summary


def test_format_order_stk_no_sec_label():
    """STK sec_type shows no bracket label (it's the default)."""
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "MKT",
        "sec_type": "STK",
    }
    summary = _format_order_summary(proposal)
    assert "[STK]" not in summary


def test_format_order_default_sec_type_no_label():
    """Missing sec_type defaults to STK — no bracket label."""
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "MKT",
    }
    summary = _format_order_summary(proposal)
    # No [X] instrument label — only bracket in the text is in the disclaimer URL
    assert "[FUT]" not in summary
    assert "[STK]" not in summary


def test_format_order_tif_shown():
    """TIF value appears in the summary line."""
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 100.0,
        "tif": "GTC",
    }
    summary = _format_order_summary(proposal)
    assert "GTC" in summary


# ── _format_cancel_summary ───────────────────────────────────────────────────

def test_format_cancel_summary_basic():
    proposal = {
        "order_id": "242538143", "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 100.0, "tif": "GTC",
        "reason": "Closing test position",
    }
    summary = _format_cancel_summary(proposal)
    assert "242538143" in summary
    assert "AAPL" in summary
    assert "BUY" in summary
    assert "GTC" in summary
    assert "Closing test position" in summary
    assert "Touch ID" in summary


def test_format_cancel_summary_missing_reason():
    proposal = {"order_id": "1", "symbol": "SPY", "action": "SELL", "quantity": 5, "order_type": "MKT"}
    summary = _format_cancel_summary(proposal)
    assert "SPY" in summary
    assert "1" in summary


def test_format_cancel_summary_shows_limit_price():
    proposal = {
        "order_id": "5", "symbol": "NVDA", "action": "BUY", "quantity": 10,
        "order_type": "LMT", "limit_price": 850.0,
    }
    summary = _format_cancel_summary(proposal)
    assert "$850.00" in summary


def test_format_cancel_summary_shows_stop_price():
    """STP orders show the stop price too, mirroring _format_order_summary."""
    proposal = {
        "order_id": "6", "symbol": "MSFT", "action": "SELL", "quantity": 50,
        "order_type": "STP", "stop_price": 395.0,
    }
    summary = _format_cancel_summary(proposal)
    assert "$395.00" in summary


# ── _format_modify_summary ───────────────────────────────────────────────────

def test_format_modify_summary_shows_changed_fields():
    proposal = {
        "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
        "limit_price": 105.0, "_changed_fields": ["limit_price"],
        "_previous_values": {"limit_price": 100.0},
    }
    summary = _format_modify_summary(proposal)
    assert "242538143" in summary
    assert "limit_price" in summary
    assert "100.0" in summary
    assert "105.0" in summary
    assert "Touch ID" in summary


def test_format_modify_summary_no_changed_fields_noted():
    proposal = {"order_id": "1", "conid": 1, "symbol": "AAPL", "_changed_fields": [], "_previous_values": {}}
    summary = _format_modify_summary(proposal)
    assert "AAPL" in summary


def test_format_modify_summary_shows_reason():
    proposal = {
        "order_id": "1", "conid": 1, "symbol": "AAPL",
        "_changed_fields": ["tif"], "_previous_values": {"tif": "DAY"}, "tif": "GTC",
        "reason": "Extending time in force",
    }
    summary = _format_modify_summary(proposal)
    assert "Extending time in force" in summary


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_action(order_payload=None):
    if order_payload is None:
        order_payload = {
            "symbol": "AAPL", "action": "BUY", "quantity": 50,
            "order_type": "MKT", "limit_price": None, "stop_price": None, "reason": "Test",
        }
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": json.dumps(order_payload)}
    return action


def _make_ibkr_mock():
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    mod.BrowserCookieAuth = MagicMock()
    mod.Config.from_env.return_value = MagicMock()
    client.search_contract.return_value = [{"conid": 265598, "companyName": "APPLE INC"}]
    client.get_futures.return_value = [
        {"conid": 495512557, "expirationDate": 20260918, "multiplier": "50",
         "contractDesc": "ES SEP 26"},
    ]
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    client.place_order_and_confirm.return_value = [{"orderId": "999"}]
    return mod, client


async def _run(action, ibkr_mod, store=None, session_id="test-session"):
    """Run execute_staged_order with mocked cl + ibkr_core_mcp."""
    import claudia.order_flow as _of
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
            await execute_staged_order(action, session_id=session_id, store=store)
    finally:
        _of.cl = original_cl
    return mock_cl


def _sent_contents(mock_cl):
    """Return list of all content strings sent via cl.Message."""
    return [c.kwargs["content"] for c in mock_cl.Message.call_args_list]


def _make_cancel_action(payload=None):
    if payload is None:
        payload = {
            "order_id": "242538143", "symbol": "AAPL", "action": "BUY",
            "quantity": 1, "order_type": "LMT", "limit_price": 100.0, "tif": "GTC", "reason": "Test",
        }
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": json.dumps(payload)}
    return action


def _make_modify_action(payload=None):
    if payload is None:
        payload = {
            "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
            "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
            "tif": "GTC", "sec_type": "STK",
            "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
        }
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": json.dumps(payload)}
    return action


def _make_cancel_modify_ibkr_mock():
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    mod.BrowserCookieAuth = MagicMock()
    mod.Config.from_env.return_value = MagicMock()
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    client.cancel_order.return_value = {"order_id": "242538143", "msg": "Cancelled"}
    client.modify_order_and_confirm.return_value = {"order_id": "242538143", "order_status": "Submitted"}
    return mod, client


async def _run_cancel(action, ibkr_mod, store=None, session_id="test-session"):
    import claudia.order_flow as _of
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
            await execute_cancel_order(action, session_id=session_id, store=store)
    finally:
        _of.cl = original_cl
    return mock_cl


async def _run_modify(action, ibkr_mod, store=None, session_id="test-session"):
    import claudia.order_flow as _of
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
            await execute_modify_order(action, session_id=session_id, store=store)
    finally:
        _of.cl = original_cl
    return mock_cl


# ── execute_staged_order — basic paths ───────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_staged_order_invalid_payload_sends_error():
    """Invalid JSON payload → error message + action removed."""
    import claudia.order_flow as _of
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": "not json {{{"}
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        await execute_staged_order(action, session_id="s1")
    finally:
        _of.cl = original_cl
    assert "Invalid order proposal" in mock_cl.Message.call_args_list[0].kwargs["content"]
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_contract_not_found():
    """STK: search_contract returns [] → error message, no place_order, button removed."""
    ibkr_mod, client = _make_ibkr_mock()
    client.search_contract.return_value = []
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("Could not find contract" in c for c in _sent_contents(mock_cl))
    client.place_order_and_confirm.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_success_sends_success_message():
    """Happy path → 'staged successfully' in chat."""
    ibkr_mod, _client = _make_ibkr_mock()
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("staged successfully" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_success_logs_decision():
    """Happy path → store.add_decision called with decision_type='trade_staged'."""
    ibkr_mod, _client = _make_ibkr_mock()
    store = MagicMock()
    action = _make_action()
    await _run(action, ibkr_mod, store=store, session_id="s42")
    store.add_decision.assert_called_once()
    kwargs = store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_staged"
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["session_id"] == "s42"


@pytest.mark.asyncio
async def test_execute_staged_order_touch_id_error():
    """'authentication' in error → Touch ID failure message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Authentication challenge failed")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_dialog_cancel_error():
    """'cancelled by user' in error → dialog cancellation message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Order cancelled by user")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_reply_chain_decline_error():
    """'declined IBKR order reply' (place_order_and_confirm mid-chain decline) →
    a message distinct from the Touch ID failure text, since Gate 1 succeeded and
    the user consciously declined a follow-up IBKR prompt after the order was placed."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("User declined IBKR order reply")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = _sent_contents(mock_cl)
    assert any("follow-up IBKR confirmation" in c for c in contents)
    assert not any("authentication failed or was cancelled" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_generic_error():
    """Generic exception → generic 'Order staging failed' message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Connection reset")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("Order staging failed" in c or "Order not placed" in c
               for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_success():
    """action.remove() called after successful staging."""
    ibkr_mod, _client = _make_ibkr_mock()
    action = _make_action()
    await _run(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_exception():
    """action.remove() called even when place_order raises."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("IBKR error")
    action = _make_action()
    await _run(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_limit_price_in_order_body():
    """LMT order with limit_price → 'price' field in order body sent to place_order."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "TSLA", "action": "BUY", "quantity": 10,
        "order_type": "LMT", "limit_price": 245.0, "stop_price": None, "reason": "Dip buy",
    })
    await _run(action, ibkr_mod)
    client.place_order_and_confirm.assert_called_once()
    _account_id, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("price") == 245.0
    assert order_body.get("orderType") == "LMT"


@pytest.mark.asyncio
async def test_execute_staged_order_quantity_is_int():
    """quantity sent to place_order is int, not float."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "AAPL", "action": "BUY", "quantity": 5,
        "order_type": "MKT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert isinstance(order_body.get("quantity"), int)


@pytest.mark.asyncio
async def test_execute_staged_order_stk_no_cme_fields():
    """STK order body must NOT contain manualIndicator or extOperator."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "AAPL", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "STK",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert "manualIndicator" not in order_body
    assert "extOperator" not in order_body


# ── execute_staged_order — futures (FUT) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_staged_order_fut_uses_get_futures_not_search():
    """FUT: conid resolved via get_futures(), search_contract never called."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    client.get_futures.assert_called_once_with(["ES"])
    client.search_contract.assert_not_called()


@pytest.mark.asyncio
async def test_execute_staged_order_fut_cme_536b_fields():
    """FUT order body includes manualIndicator=True but NOT extOperator — IBKR rejects
    extOperator with any non-empty value as undocumented field 8089 on this account
    class (proven via whatif isolation 2026-07-23; see
    docs/2026-07-23-futures-order-field-8089-bug.md)."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("manualIndicator") is True
    assert "extOperator" not in order_body


@pytest.mark.asyncio
async def test_execute_staged_order_fut_multiplier_in_order_body():
    """FUT: _multiplier from get_futures response passed as display field."""
    ibkr_mod, client = _make_ibkr_mock()
    # Multiplier "50" as a string (matches IBKR response format)
    client.get_futures.return_value = [
        {"conid": 495512557, "expirationDate": 20260918, "multiplier": "50",
         "contractDesc": "ES SEP 26"},
    ]
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 5500.0, "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("_multiplier") == 50.0


@pytest.mark.asyncio
async def test_execute_staged_order_fut_no_multiplier_field_when_absent():
    """FUT: _multiplier not added to order body when get_futures returns no multiplier."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_futures.return_value = [
        {"conid": 495512557, "expirationDate": 20260918, "contractDesc": "ES SEP 26"},
    ]
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert "_multiplier" not in order_body


@pytest.mark.asyncio
async def test_execute_staged_order_fut_not_found():
    """FUT: get_futures returns [] → error message, no place_order."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_futures.return_value = []
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    mock_cl = await _run(action, ibkr_mod)
    assert any("futures contracts" in c for c in _sent_contents(mock_cl))
    client.place_order_and_confirm.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_fut_front_month_selected():
    """FUT: lowest expirationDate is selected as front month."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_futures.return_value = [
        {"conid": 700000, "expirationDate": 20261218, "multiplier": "50", "contractDesc": "ES DEC 26"},
        {"conid": 495512557, "expirationDate": 20260918, "multiplier": "50", "contractDesc": "ES SEP 26"},
        {"conid": 800000, "expirationDate": 20270318, "multiplier": "50", "contractDesc": "ES MAR 27"},
    ]
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("conid") == 495512557  # lowest expirationDate = front month


# ── execute_staged_order — conid override ────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_staged_order_conid_override_skips_resolution():
    """Proposal with conid set → uses it directly, no search_contract or get_futures."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "AAPL", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "STK", "conid": 265598,
    })
    await _run(action, ibkr_mod)
    client.search_contract.assert_not_called()
    client.get_futures.assert_not_called()
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("conid") == 265598


@pytest.mark.asyncio
async def test_execute_staged_order_conid_override_works_for_fut():
    """Pre-resolved conid also works for FUT (bypasses get_futures)."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT", "conid": 495512557,
    })
    await _run(action, ibkr_mod)
    client.get_futures.assert_not_called()
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("conid") == 495512557
    # FUT 536-B fields still added when sec_type is FUT
    assert order_body.get("manualIndicator") is True


# ── execute_staged_order — FOP guard ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_staged_order_fop_without_conid_sends_error():
    """FOP without conid → clear error message, no place_order, button removed."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 50.0, "sec_type": "FOP",
    })
    mock_cl = await _run(action, ibkr_mod)
    contents = _sent_contents(mock_cl)
    assert any("FOP" in c or "Futures Options" in c or "conid" in c.lower()
               for c in contents)
    client.place_order_and_confirm.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_fop_with_conid_proceeds():
    """FOP with pre-resolved conid → order submitted with manualIndicator but NOT
    extOperator (rejected by IBKR as field 8089 — see FUT test above)."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 50.0, "sec_type": "FOP", "conid": 999888,
    })
    await _run(action, ibkr_mod)
    client.place_order_and_confirm.assert_called_once()
    _, order_body = client.place_order_and_confirm.call_args.args
    assert order_body.get("conid") == 999888
    assert order_body.get("manualIndicator") is True
    assert "extOperator" not in order_body


# ── IBKR 200-with-rejection payloads (2026-07-23 live FUT test) ──────────────
# IBKR returns order rejections as an HTTP 200 payload — no exception raised —
# so the result must be classified, not assumed successful.
# Shape copied verbatim from docs/2026-07-23-futures-order-field-8089-bug.md.

_REJECTION_PAYLOAD = [{
    "error": "\"BUY 1 ES SEP'26 @ 6000.00\"\nCan not contain field # 8089",
    "cqe": {"post_payload": {"rejections": ["Can not contain field # 8089"],
                             "sec_type": "FUT", "conid": "649180671", "exchange": "CME",
                             "order_id": "0"}},
    "action": "order_submit_issue",
}]

# Live-verified success shape (AAPL order, earlier live test).
_SUCCESS_PAYLOAD = [{"order_id": "1986940574", "order_status": "Submitted",
                     "encrypt_message": "1"}]


@pytest.mark.asyncio
async def test_execute_staged_order_rejection_payload_reports_failure():
    """IBKR 200-with-rejection payload → REJECTED message, never 'staged successfully'."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.return_value = _REJECTION_PAYLOAD
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 6000.0, "sec_type": "FUT", "tif": "GTC",
    })
    mock_cl = await _run(action, ibkr_mod)
    contents = _sent_contents(mock_cl)
    assert any("REJECTED" in c for c in contents)
    assert not any("staged successfully" in c for c in contents)
    # Raw IBKR response stays visible (broker-response transparency convention).
    assert any("Can not contain field # 8089" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_rejection_payload_logs_no_success_decision():
    """A rejected order must not be recorded as a 'trade_staged' decision — matches
    the other failure paths in this module, which log no decision at all."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.return_value = _REJECTION_PAYLOAD
    store = MagicMock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 6000.0, "sec_type": "FUT", "tif": "GTC",
    })
    await _run(action, ibkr_mod, store=store, session_id="s42")
    store.add_decision.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_real_success_payload_still_reports_success():
    """Regression guard: the live-verified success shape (order_id + order_status:
    Submitted) still produces the success message and decision log."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.return_value = _SUCCESS_PAYLOAD
    store = MagicMock()
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod, store=store, session_id="s42")
    assert any("staged successfully" in c for c in _sent_contents(mock_cl))
    store.add_decision.assert_called_once()
    assert store.add_decision.call_args.kwargs["decision_type"] == "trade_staged"


@pytest.mark.asyncio
async def test_execute_cancel_order_rejection_payload_reports_failure():
    """cancel_order returning an error payload (200, no exception) → FAILED message,
    no 'cancelled' success message, no decision logged."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.return_value = {
        "error": "Order not found", "action": "order_submit_issue", "order_id": "0",
    }
    store = MagicMock()
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod, store=store, session_id="s42")
    contents = _sent_contents(mock_cl)
    assert any("Cancel FAILED" in c for c in contents)
    assert not any("**Order cancelled:**" in c for c in contents)
    store.add_decision.assert_not_called()


@pytest.mark.asyncio
async def test_execute_modify_order_rejection_payload_reports_failure():
    """modify_order_and_confirm returning a rejection payload (200, no exception) →
    REJECTED message, no 'modified' success message, no decision logged."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.return_value = {
        "error": "\"BUY 1 ES SEP'26 @ 6000.00\"\nCan not contain field # 8089",
        "cqe": {"post_payload": {"rejections": ["Can not contain field # 8089"],
                                 "order_id": "0"}},
        "action": "order_submit_issue",
    }
    store = MagicMock()
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod, store=store, session_id="s42")
    contents = _sent_contents(mock_cl)
    assert any("Modify REJECTED" in c for c in contents)
    assert not any("**Order modified:**" in c for c in contents)
    store.add_decision.assert_not_called()


# ── execute_cancel_order ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_cancel_order_invalid_payload_sends_error():
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": "not json {{{"}
    import claudia.order_flow as _of
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        await execute_cancel_order(action, session_id="s1")
    finally:
        _of.cl = original_cl
    assert "Invalid cancel proposal" in mock_cl.Message.call_args_list[0].kwargs["content"]
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_cancel_order_missing_order_id_sends_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_cancel_action({"symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("order_id" in c.lower() for c in _sent_contents(mock_cl))
    client.cancel_order.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_cancel_order_success_sends_success_message():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("cancelled" in c.lower() for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_cancel_order_calls_client_with_account_and_order_id():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    action = _make_cancel_action(proposal)
    await _run_cancel(action, ibkr_mod)
    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


@pytest.mark.asyncio
async def test_execute_cancel_order_success_logs_decision():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    store = MagicMock()
    action = _make_cancel_action()
    await _run_cancel(action, ibkr_mod, store=store, session_id="s42")
    store.add_decision.assert_called_once()
    kwargs = store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_cancelled"
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["session_id"] == "s42"


@pytest.mark.asyncio
async def test_execute_cancel_order_touch_id_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("Authentication challenge failed")
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_cancel_order_dialog_cancel_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("Order cancelled by user")
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_cancel_order_generic_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("Connection reset")
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("failed" in c.lower() or "not cancelled" in c.lower() for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_cancel_order_remove_called_on_success():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    action = _make_cancel_action()
    await _run_cancel(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_cancel_order_remove_called_on_exception():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("IBKR error")
    action = _make_cancel_action()
    await _run_cancel(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_cancel_order_403_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("403 Forbidden")
    action = _make_cancel_action()
    mock_cl = await _run_cancel(action, ibkr_mod)
    assert any("403" in c for c in _sent_contents(mock_cl))


# ── execute_modify_order ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_modify_order_invalid_payload_sends_error():
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": "not json {{{"}
    import claudia.order_flow as _of
    mock_cl = MagicMock()
    mock_msg = MagicMock()
    mock_msg.send = AsyncMock()
    mock_cl.Message.return_value = mock_msg
    original_cl = _of.cl
    _of.cl = mock_cl
    try:
        await execute_modify_order(action, session_id="s1")
    finally:
        _of.cl = original_cl
    assert "Invalid modify proposal" in mock_cl.Message.call_args_list[0].kwargs["content"]
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_modify_order_missing_order_id_sends_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({"conid": 265598, "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    mock_cl = await _run_modify(action, ibkr_mod)
    assert any("order_id" in c.lower() for c in _sent_contents(mock_cl))
    client.modify_order_and_confirm.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_modify_order_missing_conid_sends_error_directing_to_get_order_status():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({"order_id": "1", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    mock_cl = await _run_modify(action, ibkr_mod)
    contents = _sent_contents(mock_cl)
    assert any("get_order_status" in c or "conid" in c.lower() for c in contents)
    client.modify_order_and_confirm.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_modify_order_success_sends_success_message():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod)
    assert any("modified" in c.lower() for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_modify_order_success_logs_decision():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    store = MagicMock()
    action = _make_modify_action()
    await _run_modify(action, ibkr_mod, store=store, session_id="s42")
    store.add_decision.assert_called_once()
    kwargs = store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_modified"
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["session_id"] == "s42"


@pytest.mark.asyncio
async def test_execute_modify_order_calls_client_with_account_order_id_and_body():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 3, "order_type": "LMT", "limit_price": 105.0, "tif": "GTC", "sec_type": "STK",
    })
    await _run_modify(action, ibkr_mod)
    client.modify_order_and_confirm.assert_called_once()
    account_id, order_id, order_body = client.modify_order_and_confirm.call_args.args
    assert account_id == "U12345"
    assert order_id == "555"
    assert order_body.get("conid") == 265598
    assert order_body.get("orderType") == "LMT"
    assert order_body.get("side") == "BUY"
    assert order_body.get("tif") == "GTC"
    assert order_body.get("quantity") == 3
    assert order_body.get("price") == 105.0


@pytest.mark.asyncio
async def test_execute_modify_order_builds_fresh_body_not_raw_proposal():
    """Display-only proposal fields (_changed_fields, _previous_values) must never
    reach the IBKR request body — modify_order() does no _-prefix stripping."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action()
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "_changed_fields" not in order_body
    assert "_previous_values" not in order_body
    assert "reason" not in order_body


@pytest.mark.asyncio
async def test_execute_modify_order_quantity_is_int():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({
        "order_id": "1", "conid": 1, "symbol": "AAPL", "action": "BUY",
        "quantity": 5, "order_type": "MKT",
    })
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert isinstance(order_body.get("quantity"), int)


@pytest.mark.asyncio
async def test_execute_modify_order_stk_no_cme_fields():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({
        "order_id": "1", "conid": 1, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "MKT", "sec_type": "STK",
    })
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "manualIndicator" not in order_body
    assert "extOperator" not in order_body


@pytest.mark.asyncio
async def test_execute_modify_order_fut_cme_536b_fields():
    """FUT modify body includes manualIndicator=True but NOT extOperator — same
    field-8089 rejection as the place path (docs/2026-07-23-futures-order-field-8089-bug.md)."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({
        "order_id": "1", "conid": 495512557, "symbol": "ES", "action": "BUY",
        "quantity": 1, "order_type": "MKT", "sec_type": "FUT",
    })
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert order_body.get("manualIndicator") is True
    assert "extOperator" not in order_body


@pytest.mark.asyncio
async def test_execute_modify_order_stop_limit_price_and_aux_price():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({
        "order_id": "1", "conid": 1, "symbol": "AAPL", "action": "SELL", "quantity": 1,
        "order_type": "STOP_LIMIT", "limit_price": 95.0, "stop_price": 96.0,
    })
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert order_body.get("price") == 95.0
    assert order_body.get("auxPrice") == 96.0


@pytest.mark.asyncio
async def test_execute_modify_order_touch_id_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("Authentication challenge failed")
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_modify_order_dialog_cancel_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("Order cancelled by user")
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_modify_order_reply_chain_decline_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("User declined IBKR order reply")
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod)
    contents = _sent_contents(mock_cl)
    assert any("follow-up IBKR confirmation" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_modify_order_generic_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("Connection reset")
    action = _make_modify_action()
    mock_cl = await _run_modify(action, ibkr_mod)
    assert any("failed" in c.lower() or "not modified" in c.lower() for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_modify_order_remove_called_on_success():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action()
    await _run_modify(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_modify_order_remove_called_on_exception():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("IBKR error")
    action = _make_modify_action()
    await _run_modify(action, ibkr_mod)
    action.remove.assert_called_once()


# ── Extracted core functions (Task 3.2) — framework-agnostic, dict + callback in ────

def _make_send_status_recorder():
    """A send_status callback that records every (text, author) call, for assertions —
    the framework-agnostic equivalent of this file's existing _sent_contents(mock_cl)
    helper, which only works against the cl.Message-based wrapper."""
    calls = []

    async def _send_status(text: str, author: str) -> None:
        calls.append((text, author))

    return _send_status, calls


@pytest.mark.asyncio
async def test_execute_staged_order_core_success_calls_send_status():
    """The extracted core, called directly with a plain dict (no cl.Action, no JSON
    parsing) and a plain callback (no chainlit), produces the same success behavior."""
    from claudia.order_flow import _execute_staged_order_core
    ibkr_mod, _client = _make_ibkr_mock()
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 50,
        "order_type": "MKT", "limit_price": None, "stop_price": None, "reason": "Test",
    }
    send_status, calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_staged_order_core(proposal, send_status, session_id="s1", store=None)
    assert any("staged successfully" in text for text, _author in calls)


@pytest.mark.asyncio
async def test_execute_staged_order_core_never_touches_action_or_removes_anything():
    """The core function has no cl.Action parameter at all and does not call .remove() —
    that guarantee now lives entirely in the wrapper (Step 3 below), verified separately."""
    import inspect

    from claudia.order_flow import _execute_staged_order_core
    sig = inspect.signature(_execute_staged_order_core)
    assert "action" not in sig.parameters
    assert "proposal" in sig.parameters
    assert "send_status" in sig.parameters


@pytest.mark.asyncio
async def test_execute_cancel_order_core_calls_client_with_account_and_order_id():
    from claudia.order_flow import _execute_cancel_order_core
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    send_status, _calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_cancel_order_core(proposal, send_status, session_id="s1", store=None)
    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


def test_execute_cancel_order_core_never_touches_action_or_removes_anything():
    """Mirrors the staged-order core's contract test — same no-action-param guarantee."""
    import inspect

    from claudia.order_flow import _execute_cancel_order_core
    sig = inspect.signature(_execute_cancel_order_core)
    assert "action" not in sig.parameters
    assert "proposal" in sig.parameters
    assert "send_status" in sig.parameters


@pytest.mark.asyncio
async def test_execute_modify_order_core_builds_fresh_body_not_raw_proposal():
    from claudia.order_flow import _execute_modify_order_core
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {
        "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
        "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "tif": "GTC", "sec_type": "STK",
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    send_status, _calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _execute_modify_order_core(proposal, send_status, session_id="s1", store=None)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "_changed_fields" not in order_body
    assert "_previous_values" not in order_body


def test_execute_modify_order_core_never_touches_action_or_removes_anything():
    """Mirrors the staged-order core's contract test — same no-action-param guarantee."""
    import inspect

    from claudia.order_flow import _execute_modify_order_core
    sig = inspect.signature(_execute_modify_order_core)
    assert "action" not in sig.parameters
    assert "proposal" in sig.parameters
    assert "send_status" in sig.parameters
