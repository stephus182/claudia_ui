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


def _make_chat():
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


def _get_click_callback(button):
    """Extract the real on_click callback from a live pn.widgets.Button, for direct
    invocation in a unit test (no browser, no running Panel server).

    Verified live, 2026-07-22, against the installed panel==1.9.3: Button.on_click(cb)
    is implemented as `self.param.watch(cb, 'clicks', onlychanged=False)` (confirmed via
    `inspect.getsource(pn.widgets.Button.on_click)`) — there is no `_on_click` attribute
    on the button itself. The registered callback lives in
    `button.param.watchers['clicks']['value']`, a list of param Watcher namedtuples;
    Panel's own internal sync watchers (name/label/value mirroring etc.) are always
    registered with `onlychanged=True`, while on_click's own watcher is always
    `onlychanged=False` — confirmed by direct inspection of that list — so filtering on
    that flag reliably isolates the one watcher this file's own render_* functions
    registered, regardless of how many internal watchers Panel itself adds. Calling
    `.fn` directly and awaiting it (async callbacks are supported natively, confirmed via
    `param.parameterized`'s `iscoroutinefunction(watcher.fn)` branch) exercises the exact
    function a real click would invoke, without needing Panel's async_executor/event-loop
    plumbing that a bare pytest run doesn't have.
    """
    watchers = button.param.watchers["clicks"]["value"]
    matches = [w.fn for w in watchers if not w.onlychanged]
    assert len(matches) == 1, f"expected exactly 1 on_click watcher, found {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_render_order_proposal_sends_message_with_two_buttons():
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
    chat = _make_chat()
    proposal = {
        "symbol": "AAPL", "action": "BUY", "quantity": 10,
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
    chat = _make_chat()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Cancel this order"
    assert button_row[1].name == "Keep order"


@pytest.mark.asyncio
async def test_render_cancel_proposal_confirm_click_calls_cancel_core():
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
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    button_row = column[1]
    assert button_row[0].name == "Modify this order"
    assert button_row[1].name == "Discard"


@pytest.mark.asyncio
async def test_render_modify_proposal_confirm_click_calls_modify_core():
    chat = _make_chat()
    proposal = {
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    modify_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(modify_btn)(None)

    client.modify_order_and_confirm.assert_called_once()
