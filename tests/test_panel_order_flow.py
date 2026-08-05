"""Tests for panel_order_flow.py — Panel-side order-staging button rendering.

Mirrors tests/test_order_flow.py's mocking conventions (_make_ibkr_mock-style patch.dict
on sys.modules) since render_*_proposal here calls straight through to order_flow.py's
already-tested _execute_*_core functions — these tests verify the Panel-specific wiring
(buttons constructed, on_click bound, message sent, buttons disabled after click), not the
order-placement logic itself (that's test_order_flow.py's job, already covered).
"""

from unittest.mock import MagicMock, patch

import pytest

from claudia.panel_order_flow import (
    render_cancel_proposal,
    render_modify_proposal,
    render_order_proposal,
)
from tests.conftest import _get_click_callback


def _make_chat():
    """A chat interface stub that records what was sent."""
    chat = MagicMock()
    chat.send = MagicMock()
    return chat


def _make_ibkr_mock():
    """Same shape as test_order_flow.py's helper of the same name — a successful,
    minimal STK order path, since these tests only need the *call* to succeed, not
    every branch (that's already covered in test_order_flow.py)."""
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    mod.BrowserCookieAuth = MagicMock()
    mod.Config.from_env.return_value = MagicMock()
    client.search_contract.return_value = [{"conid": 265598, "companyName": "APPLE INC"}]
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    client.place_order_and_confirm.return_value = [{"orderId": "999"}]
    client.cancel_order.return_value = {"order_id": "242538143", "msg": "Cancelled"}
    client.modify_order_and_confirm.return_value = {"order_id": "242538143", "order_status": "Submitted"}
    return mod, client


@pytest.mark.asyncio
async def test_render_order_proposal_sends_message_with_two_buttons():
    """An order proposal renders one message carrying a stage and a cancel button."""
    chat = _make_chat()
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "MKT"}
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    chat.send.assert_called_once()
    args, kwargs = chat.send.call_args
    assert kwargs["user"] == "ClaudIA — Order Proposal"
    # sent content is a pn.Column containing a pn.Row of 2 buttons — inspect structurally
    column = args[0]
    button_row = column[1]
    assert len(button_row) == 2
    assert button_row[0].name == "Stage this order"
    assert button_row[1].name == "Cancel"


@pytest.mark.asyncio
async def test_render_order_proposal_stage_click_executes_and_disables_buttons():
    """Clicking stage runs the execution core and disables both buttons — one-shot."""
    chat = _make_chat()
    # Carries a conid because real proposals do, and because a placement without one is
    # now refused before it reaches IBKR (order_flow._needs_conid_text).
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 10, "conid": 265598,
        "order_type": "MKT", "limit_price": None, "stop_price": None,
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    stage_btn, cancel_btn = column[1][0], column[1][1]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(stage_btn)(None)  # simulate a real click

    client.place_order_and_confirm.assert_called_once()
    assert stage_btn.disabled is True
    assert cancel_btn.disabled is True


@pytest.mark.asyncio
async def test_render_order_proposal_cancel_click_disables_without_executing():
    """Dismissing disables the buttons and reaches no IBKR path."""
    chat = _make_chat()
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "MKT"}
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    stage_btn, cancel_btn = column[1][0], column[1][1]

    await _get_click_callback(cancel_btn)(None)

    assert stage_btn.disabled is True
    assert cancel_btn.disabled is True
    # 2 chat.send calls total: the original proposal render + the cancellation notice
    assert chat.send.call_count == 2


@pytest.mark.asyncio
async def test_render_cancel_proposal_sends_message_with_two_buttons():
    """A cancel proposal renders one message carrying a cancel and a keep button."""
    chat = _make_chat()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Cancel this order"
    assert button_row[1].name == "Keep order"


@pytest.mark.asyncio
async def test_render_cancel_proposal_confirm_click_calls_cancel_core():
    """Confirming routes to the cancel core, not to any other path."""
    chat = _make_chat()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    ibkr_mod, client = _make_ibkr_mock()
    await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    cancel_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(cancel_btn)(None)

    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


@pytest.mark.asyncio
async def test_render_modify_proposal_sends_message_with_two_buttons():
    """A modify proposal renders one message carrying a modify and a discard button."""
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "changes": [{"field": "limit_price", "previous_value": 100.0}],
    }
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Modify this order"
    assert button_row[1].name == "Discard"


@pytest.mark.asyncio
async def test_render_modify_proposal_confirm_click_calls_modify_core():
    """Confirming routes to the modify core, not to any other path."""
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "changes": [{"field": "limit_price", "previous_value": 100.0}],
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    modify_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(modify_btn)(None)

    client.modify_order_and_confirm.assert_called_once()
