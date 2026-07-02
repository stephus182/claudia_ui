"""Tests for order_flow — order summary formatting and execute_staged_order."""

# ── Imports ──────────────────────────────────────────────────────────────────
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claudia.order_flow import _format_order_summary, execute_staged_order


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
    client.place_order.return_value = [{"orderId": "999"}]
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
    client.place_order.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_success_sends_success_message():
    """Happy path → 'staged successfully' in chat."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("staged successfully" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_success_logs_decision():
    """Happy path → store.add_decision called with decision_type='trade_staged'."""
    ibkr_mod, client = _make_ibkr_mock()
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
    client.place_order.side_effect = RuntimeError("Authentication challenge failed")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_dialog_cancel_error():
    """'cancelled by user' in error → dialog cancellation message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("Order cancelled by user")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_generic_error():
    """Generic exception → generic 'Order staging failed' message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("Connection reset")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    assert any("Order staging failed" in c or "Order not placed" in c
               for c in _sent_contents(mock_cl))


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_success():
    """action.remove() called after successful staging."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action()
    await _run(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_exception():
    """action.remove() called even when place_order raises."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("IBKR error")
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
    client.place_order.assert_called_once()
    _account_id, order_body = client.place_order.call_args.args
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
    _, order_body = client.place_order.call_args.args
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
    _, order_body = client.place_order.call_args.args
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
    """FUT order body includes manualIndicator=True and extOperator (CME Rule 536-B)."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "MKT", "sec_type": "FUT",
    })
    await _run(action, ibkr_mod)
    _, order_body = client.place_order.call_args.args
    assert order_body.get("manualIndicator") is True
    assert order_body.get("extOperator") == "ClaudIA"


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
    _, order_body = client.place_order.call_args.args
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
    _, order_body = client.place_order.call_args.args
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
    client.place_order.assert_not_called()
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
    _, order_body = client.place_order.call_args.args
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
    _, order_body = client.place_order.call_args.args
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
    _, order_body = client.place_order.call_args.args
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
    client.place_order.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_fop_with_conid_proceeds():
    """FOP with pre-resolved conid → order submitted with 536-B fields."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 50.0, "sec_type": "FOP", "conid": 999888,
    })
    await _run(action, ibkr_mod)
    client.place_order.assert_called_once()
    _, order_body = client.place_order.call_args.args
    assert order_body.get("conid") == 999888
    assert order_body.get("manualIndicator") is True
    assert order_body.get("extOperator") == "ClaudIA"
