"""Tests for order_flow — order summary formatting."""

# ── Imports for execute_staged_order tests ──────────────────────────────────
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claudia.order_flow import _format_order_summary, execute_staged_order


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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_action(order_payload=None):
    if order_payload is None:
        order_payload = {
            "symbol": "AAPL", "action": "BUY", "quantity": 50,
            "order_type": "MKT", "limit_price": None, "stop_price": None, "reason": "Test"
        }
    action = MagicMock()
    action.remove = AsyncMock()
    action.payload = {"order": json.dumps(order_payload)}
    return action


def _make_ibkr_mock():
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    client.search_contract.return_value = [{"conid": 265598}]
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    client.place_order.return_value = {"orderId": "999"}
    return mod, client


async def _run(action, ibkr_mod, store=None, session_id="test-session"):
    """Run execute_staged_order with mocked cl + ibkr_core_mcp.

    chainlit uses a lazy __getattr__ registry that raises KeyError on hasattr()
    introspection, so patch("claudia.order_flow.cl") fails. Instead we swap the
    module-level reference directly and restore it in a finally block.
    """
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


# ── execute_staged_order tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_staged_order_invalid_payload_sends_error():
    """Invalid JSON payload → sends error message and removes the action button."""
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
    sent_content = mock_cl.Message.call_args_list[0].kwargs["content"]
    assert "Invalid order proposal" in sent_content
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_contract_not_found():
    """search_contract returns [] → sends 'Could not find contract' message, no place_order call, button removed."""
    ibkr_mod, client = _make_ibkr_mock()
    client.search_contract.return_value = []
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = [c.kwargs["content"] for c in mock_cl.Message.call_args_list]
    assert any("Could not find contract" in c for c in contents)
    client.place_order.assert_not_called()
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_success_sends_success_message():
    """Happy path → success message contains 'staged successfully'."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = [c.kwargs["content"] for c in mock_cl.Message.call_args_list]
    assert any("staged successfully" in c for c in contents)


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
    """Exception with 'authentication' → display error mentions Touch ID."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("Authentication challenge failed")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = [c.kwargs["content"] for c in mock_cl.Message.call_args_list]
    assert any("Touch ID" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_dialog_cancel_error():
    """Exception with 'cancelled' → display error mentions confirmation dialog cancellation."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("User cancelled the order")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = [c.kwargs["content"] for c in mock_cl.Message.call_args_list]
    assert any("cancelled at" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_generic_error():
    """Generic exception → display error mentions gateway connection."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("Connection reset")
    action = _make_action()
    mock_cl = await _run(action, ibkr_mod)
    contents = [c.kwargs["content"] for c in mock_cl.Message.call_args_list]
    assert any("gateway connection" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_success():
    """action.remove() is called after a successful order staging."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action()
    await _run(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_remove_called_on_exception():
    """action.remove() is called even when place_order raises an exception."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order.side_effect = RuntimeError("IBKR error")
    action = _make_action()
    await _run(action, ibkr_mod)
    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_limit_price_in_order_body():
    """LMT order with limit_price → place_order receives order body with 'price' field."""
    ibkr_mod, client = _make_ibkr_mock()
    action = _make_action({
        "symbol": "TSLA", "action": "BUY", "quantity": 10,
        "order_type": "LMT", "limit_price": 245.0, "stop_price": None, "reason": "Dip buy"
    })
    await _run(action, ibkr_mod)
    client.place_order.assert_called_once()
    _account_id, order_list = client.place_order.call_args.args
    order_body = order_list[0]
    assert order_body.get("price") == 245.0
    assert order_body.get("orderType") == "LMT"
