"""Tests for order_flow — summary formatting and the framework-agnostic
_execute_{staged,cancel,modify}_order_core execution paths (Gate 1/Gate 2, place_order,
IBKR rejection handling, decision logging), driven through a send_status recorder."""

# ── Imports ──────────────────────────────────────────────────────────────────
import json
from unittest.mock import MagicMock, patch

import pytest

from claudia import order_flow
from claudia.order_flow import (
    _READBACK_DELAY_S,
    _compare_modify_readback,
    _execute_cancel_order_core,
    _execute_modify_order_core,
    _execute_staged_order_core,
    _extract_order_id,
    _format_cancel_summary,
    _format_modify_summary,
    _format_order_summary,
    _is_ibkr_rejection,
    _read_back,
    _resolve_account_id,
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
        "limit_price": 105.0,
        "changes": [{"field": "limit_price", "previous_value": 100.0}],
    }
    summary = _format_modify_summary(proposal)
    assert "242538143" in summary
    assert "limit_price" in summary
    assert "100.0" in summary
    assert "105.0" in summary
    assert "Touch ID" in summary


def test_format_modify_summary_shows_every_changed_field():
    """One line per entry — a multi-field modify must not show only the first."""
    proposal = {
        "order_id": "1", "conid": 1, "symbol": "AAPL", "limit_price": 105.0, "quantity": 3,
        "changes": [
            {"field": "limit_price", "previous_value": 100.0},
            {"field": "quantity", "previous_value": 1},
        ],
    }
    summary = _format_modify_summary(proposal)
    assert "limit_price: 100.0 → 105.0" in summary
    assert "quantity: 1 → 3" in summary


def test_format_modify_summary_no_changed_fields_noted():
    proposal = {"order_id": "1", "conid": 1, "symbol": "AAPL", "changes": []}
    summary = _format_modify_summary(proposal)
    assert "AAPL" in summary
    assert "no changed fields listed" in summary


def test_format_modify_summary_renders_a_malformed_entry_instead_of_raising():
    """The last step before the human sees the proposal must not be the thing that fails —
    a render that dies is how a button silently failed to appear
    (finding-llm-proposal-block-emission)."""
    proposal = {"order_id": "1", "conid": 1, "symbol": "AAPL", "changes": ["limit_price"]}
    summary = _format_modify_summary(proposal)
    assert "malformed" in summary
    assert "limit_price" in summary


def test_format_modify_summary_shows_reason():
    proposal = {
        "order_id": "1", "conid": 1, "symbol": "AAPL", "tif": "GTC",
        "changes": [{"field": "tif", "previous_value": "DAY"}],
        "reason": "Extending time in force",
    }
    summary = _format_modify_summary(proposal)
    assert "Extending time in force" in summary


# ── Helpers ───────────────────────────────────────────────────────────────────

def _no_readback_delay():
    """Collapse the L2 read-back wait so the suite never actually sleeps 2 s per test.

    Patches the constant, not asyncio.sleep — the ordering guarantee (sleep, *then*
    read) stays under test in test_read_back_waits_before_reading, which patches
    asyncio.sleep itself and asserts the real constant is the value awaited."""
    return patch.object(order_flow, "_READBACK_DELAY_S", 0)


def _make_action(order_payload=None):
    if order_payload is None:
        order_payload = {
            "symbol": "AAPL", "action": "BUY", "quantity": 50,
            "order_type": "MKT", "limit_price": None, "stop_price": None, "reason": "Test",
        }
    action = MagicMock()
    action.payload = {"order": json.dumps(order_payload)}
    return action


def _set_readback(client, **fields):
    """Give a mocked IBKRClient a `get_order_status` payload for the L2 read-back.

    Defaults describe a working order whose fields match the default modify proposal
    (BUY 1 AAPL LMT GTC), so the shared cancel/modify mock satisfies the modify field
    comparison out of the box; cancel tests that assert on output override
    `order_status="Cancelled"`. Synthetic order ids only — never live account data.
    Field names are IBKR's own, from
    https://ibkrcampus.com/docs/web-api/web-api-v-1-0-documentation/endpoints/order-monitoring/order-status.md
    """
    payload = {
        "order_status": "Submitted",
        "order_status_description": "Order is working",
        "side": "BUY",
        "order_type": "LMT",
        "tif": "GTC",
        "total_size": 1,
        "size": 1,
        "order_description": "BUY 1 AAPL LMT 105.00 GTC",
    }
    payload.update(fields)
    client.get_order_status.return_value = payload
    return client


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
    # Absent from the live book by default, so the existing place tests keep exercising
    # the get_order_status fall-through. Absence is never evidence either way — the tests
    # below that assert on presence set it explicitly (`_set_live_book`).
    client.get_live_orders.return_value = []
    _set_readback(client)
    return mod, client


def _set_live_book(client, *orders):
    """Give a mocked IBKRClient a `get_live_orders` payload for the placement check.

    Field names are IBKR's own Live Orders fields (`orderId`, `status`) as returned by
    `IBKRClient.get_live_orders`. Synthetic order ids only — never live account data.
    """
    client.get_live_orders.return_value = list(orders)
    return client


async def _run(action, ibkr_mod, store=None, session_id="test-session"):
    """Drive _execute_staged_order_core with a send_status recorder + mocked ibkr_core_mcp.

    Accepts the same `action` object the _make_* helpers build (its payload carries the
    proposal dict) so the existing call sites need no change: extract the proposal and call
    the framework-agnostic core directly — the Chainlit wrapper is gone, and the core is
    where every safety-critical Gate-1/Gate-2 / place_order / rejection path lives."""
    proposal = json.loads(action.payload["order"])
    send_status, calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
        await _execute_staged_order_core(proposal, send_status, session_id=session_id, store=store)
    return calls


def _sent_contents(calls):
    """Return every status text string the send_status callback recorded."""
    return [text for text, _author in calls]


def _make_cancel_action(payload=None):
    if payload is None:
        payload = {
            "order_id": "242538143", "symbol": "AAPL", "action": "BUY",
            "quantity": 1, "order_type": "LMT", "limit_price": 100.0, "tif": "GTC", "reason": "Test",
        }
    action = MagicMock()
    action.payload = {"order": json.dumps(payload)}
    return action


def _make_modify_action(payload=None):
    if payload is None:
        payload = {
            "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
            "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
            "tif": "GTC", "sec_type": "STK",
            "changes": [{"field": "limit_price", "previous_value": 100.0}],
        }
    action = MagicMock()
    action.payload = {"order": json.dumps(payload)}
    return action


def _make_cancel_modify_ibkr_mock():
    mod = MagicMock()
    client = MagicMock()
    mod.IBKRClient.return_value = client
    mod.BrowserCookieAuth = MagicMock()
    mod.Config.from_env.return_value = MagicMock()
    client.get_accounts.return_value = [{"accountId": "U12345"}]
    # Documented successful-cancel body, verbatim shape (order_id is an int there):
    # https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md
    client.cancel_order.return_value = {
        "msg": "Request was submitted", "order_id": 242538143,
        "conid": 265598, "account": "U12345",
    }
    client.modify_order_and_confirm.return_value = {"order_id": "242538143", "order_status": "Submitted"}
    _set_readback(client)
    return mod, client


async def _run_cancel(action, ibkr_mod, store=None, session_id="test-session"):
    proposal = json.loads(action.payload["order"])
    send_status, calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
        await _execute_cancel_order_core(proposal, send_status, session_id=session_id, store=store)
    return calls


async def _run_modify(action, ibkr_mod, store=None, session_id="test-session"):
    proposal = json.loads(action.payload["order"])
    send_status, calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
        await _execute_modify_order_core(proposal, send_status, session_id=session_id, store=store)
    return calls


# ── execute_staged_order — basic paths ───────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_staged_order_contract_not_found():
    """STK: search_contract returns [] → error message, no place_order, button removed."""
    ibkr_mod, client = _make_ibkr_mock()
    client.search_contract.return_value = []
    action = _make_action()
    recorded = await _run(action, ibkr_mod)
    assert any("Could not find contract" in c for c in _sent_contents(recorded))
    client.place_order_and_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_execute_staged_order_success_reports_the_observed_state():
    """Happy path → dispatch reported as accepted, then the state actually read back.

    The old wording ('staged successfully') asserted an outcome from the POST response
    shape alone; success is now only ever claimed from a get_order_status observation."""
    ibkr_mod, _client = _make_ibkr_mock()
    action = _make_action()
    contents = _sent_contents(await _run(action, ibkr_mod))
    assert any("Dispatch accepted by IBKR" in c for c in contents)
    assert any("Verified via get_order_status" in c and "Submitted" in c for c in contents)
    assert not any("successfully" in c for c in contents)


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
    recorded = await _run(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_staged_order_dialog_cancel_error():
    """'cancelled by user' in error → dialog cancellation message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Order cancelled by user")
    action = _make_action()
    recorded = await _run(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_staged_order_reply_chain_decline_error():
    """'declined IBKR order reply' (place_order_and_confirm mid-chain decline) →
    a message distinct from the Touch ID failure text, since Gate 1 succeeded and
    the user consciously declined a follow-up IBKR prompt after the order was placed."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("User declined IBKR order reply")
    action = _make_action()
    recorded = await _run(action, ibkr_mod)
    contents = _sent_contents(recorded)
    assert any("follow-up IBKR confirmation" in c for c in contents)
    assert not any("authentication failed or was cancelled" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_staged_order_generic_error():
    """Generic exception → generic 'Order staging failed' message."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Connection reset")
    action = _make_action()
    recorded = await _run(action, ibkr_mod)
    assert any("Order staging failed" in c or "Order not placed" in c
               for c in _sent_contents(recorded))


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
    docs/plans/2026-07-23-futures-order-field-8089-bug.md)."""
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
    recorded = await _run(action, ibkr_mod)
    assert any("futures contracts" in c for c in _sent_contents(recorded))
    client.place_order_and_confirm.assert_not_called()


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
    recorded = await _run(action, ibkr_mod)
    contents = _sent_contents(recorded)
    assert any("FOP" in c or "Futures Options" in c or "conid" in c.lower()
               for c in contents)
    client.place_order_and_confirm.assert_not_called()


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
# Shape copied verbatim from docs/plans/2026-07-23-futures-order-field-8089-bug.md.

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


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # The real live rejection payload (verbatim shape) → rejected.
        pytest.param(_REJECTION_PAYLOAD, True, id="live-rejection-payload"),
        # Live-verified success list (order_id + order_status) → success.
        pytest.param(_SUCCESS_PAYLOAD, False, id="live-success-list"),
        # Success dict shape (modify/cancel return a single dict) → success.
        pytest.param({"order_id": "242538143", "order_status": "Submitted"}, False,
                     id="success-dict"),
        # Zero order id with no action/error and no order_status → rejected
        # (third marker's direct coverage, both string and int spellings).
        pytest.param([{"order_id": "0"}], True, id="zero-order-id-str"),
        pytest.param([{"order_id": 0}], True, id="zero-order-id-int"),
        # Non-zero order id alone is success — both key spellings occur across
        # IBKR responses.
        pytest.param([{"orderId": "123"}], False, id="nonzero-orderId-camel"),
        pytest.param([{"order_id": "123"}], False, id="nonzero-order_id-snake"),
        # Degenerate inputs fail safe (claim rejection, never false success).
        pytest.param([], True, id="empty-list"),
        pytest.param(["nonsense"], True, id="non-dict-entries"),
        # Multi-entry: order id resolution is last-write-wins — the reply-chain
        # terminal entry is last, so it is the authoritative one.
        pytest.param([{"order_id": "123"}, {"order_id": "0"}], True,
                     id="multi-entry-last-wins"),
    ],
)
def test_is_ibkr_rejection_contract(result, expected):
    """Pin _is_ibkr_rejection's classification contract directly — including the
    no-status/zero-id fallback marker and degenerate-input fail-safe polarity,
    which the end-to-end tests above never reach (their fixtures trip the
    action/error markers first)."""
    assert _is_ibkr_rejection(result) is expected


@pytest.mark.asyncio
async def test_execute_staged_order_rejection_payload_reports_failure():
    """IBKR 200-with-rejection payload → REJECTED message, never 'staged successfully'."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.return_value = _REJECTION_PAYLOAD
    action = _make_action({
        "symbol": "ES", "action": "BUY", "quantity": 1,
        "order_type": "LMT", "limit_price": 6000.0, "sec_type": "FUT", "tif": "GTC",
    })
    recorded = await _run(action, ibkr_mod)
    contents = _sent_contents(recorded)
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


@pytest.mark.asyncio
async def test_execute_staged_order_real_success_payload_still_reports_success():
    """Regression guard: the live-verified success shape (order_id + order_status:
    Submitted) still reaches the read-back and produces the decision log."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.return_value = _SUCCESS_PAYLOAD
    store = MagicMock()
    action = _make_action()
    recorded = await _run(action, ibkr_mod, store=store, session_id="s42")
    assert any("Verified via get_order_status" in c for c in _sent_contents(recorded))
    # The read-back is keyed off the id in the POST response, not a guess.
    client.get_order_status.assert_called_once_with("1986940574")
    store.add_decision.assert_called_once()
    kwargs = store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_staged"
    # snake_case order_id (the live-verified spelling) must reach the decision
    # metadata — was previously read via camelCase orderId only, landing as None.
    assert kwargs["metadata"]["ibkr_order_id"] == "1986940574"


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
    recorded = await _run_cancel(action, ibkr_mod, store=store, session_id="s42")
    contents = _sent_contents(recorded)
    assert any("Cancel FAILED" in c for c in contents)
    assert not any("confirmed cancelled" in c.lower() for c in contents)
    store.add_decision.assert_not_called()
    # Nothing was dispatched, so there is nothing to read back — and the rejection's
    # own error text is the only evidence available (no order exists to query).
    client.get_order_status.assert_not_called()


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
    recorded = await _run_modify(action, ibkr_mod, store=store, session_id="s42")
    contents = _sent_contents(recorded)
    assert any("Modify REJECTED" in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)
    store.add_decision.assert_not_called()
    client.get_order_status.assert_not_called()


# ── execute_cancel_order ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_cancel_order_missing_order_id_sends_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_cancel_action({"symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    recorded = await _run_cancel(action, ibkr_mod)
    assert any("order_id" in c.lower() for c in _sent_contents(recorded))
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_cancel_order_success_sends_success_message():
    """A cancel is only reported as done once get_order_status reads back Cancelled."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Cancelled",
                  order_status_description="Order cancelled")
    action = _make_cancel_action()
    contents = _sent_contents(await _run_cancel(action, ibkr_mod))
    assert any("Verified via get_order_status" in c and "Cancelled" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_cancel_order_calls_client_with_account_and_order_id():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    action = _make_cancel_action(proposal)
    await _run_cancel(action, ibkr_mod)
    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)


@pytest.mark.asyncio
async def test_execute_cancel_order_success_logs_decision():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Cancelled")
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
    recorded = await _run_cancel(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_cancel_order_dialog_cancel_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("Order cancelled by user")
    action = _make_cancel_action()
    recorded = await _run_cancel(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_cancel_order_generic_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("Connection reset")
    action = _make_cancel_action()
    recorded = await _run_cancel(action, ibkr_mod)
    assert any("failed" in c.lower() or "not cancelled" in c.lower() for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_cancel_order_403_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.cancel_order.side_effect = RuntimeError("403 Forbidden")
    action = _make_cancel_action()
    recorded = await _run_cancel(action, ibkr_mod)
    assert any("403" in c for c in _sent_contents(recorded))


# ── execute_modify_order ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_modify_order_missing_order_id_sends_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({"conid": 265598, "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    recorded = await _run_modify(action, ibkr_mod)
    assert any("order_id" in c.lower() for c in _sent_contents(recorded))
    client.modify_order_and_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_execute_modify_order_missing_conid_sends_error_directing_to_get_order_status():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action({"order_id": "1", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    recorded = await _run_modify(action, ibkr_mod)
    contents = _sent_contents(recorded)
    assert any("get_order_status" in c or "conid" in c.lower() for c in contents)
    client.modify_order_and_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_execute_modify_order_success_sends_success_message():
    """Modify reports the read-back state plus the field comparison, never a bare
    'Order modified' claimed from the POST response."""
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action()
    contents = _sent_contents(await _run_modify(action, ibkr_mod))
    assert any("Verified via get_order_status" in c and "Submitted" in c for c in contents)
    assert any("Read-back matches the request" in c for c in contents)


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
    """Display-only proposal fields (`changes`, `reason`) must never reach the IBKR
    request body — modify_order() does no stripping of its own."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_modify_action()
    await _run_modify(action, ibkr_mod)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "changes" not in order_body
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
    field-8089 rejection as the place path (docs/plans/2026-07-23-futures-order-field-8089-bug.md)."""
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
    recorded = await _run_modify(action, ibkr_mod)
    assert any("Touch ID" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_modify_order_dialog_cancel_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("Order cancelled by user")
    action = _make_modify_action()
    recorded = await _run_modify(action, ibkr_mod)
    assert any("cancelled at" in c for c in _sent_contents(recorded))


@pytest.mark.asyncio
async def test_execute_modify_order_reply_chain_decline_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("User declined IBKR order reply")
    action = _make_modify_action()
    recorded = await _run_modify(action, ibkr_mod)
    contents = _sent_contents(recorded)
    assert any("follow-up IBKR confirmation" in c for c in contents)


@pytest.mark.asyncio
async def test_execute_modify_order_generic_error():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    client.modify_order_and_confirm.side_effect = RuntimeError("Connection reset")
    action = _make_modify_action()
    recorded = await _run_modify(action, ibkr_mod)
    assert any("failed" in c.lower() or "not modified" in c.lower() for c in _sent_contents(recorded))


# ── Extracted core functions (Task 3.2) — framework-agnostic, dict + callback in ────

def _make_send_status_recorder():
    """A send_status callback that records every (text, author) call, for assertions —
    the framework-agnostic equivalent of this file's existing _sent_contents(recorded)
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
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
        await _execute_staged_order_core(proposal, send_status, session_id="s1", store=None)
    assert any("Verified via get_order_status" in text for text, _author in calls)


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
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
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
        "changes": [{"field": "limit_price", "previous_value": 100.0}],
    }
    send_status, _calls = _make_send_status_recorder()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), _no_readback_delay():
        await _execute_modify_order_core(proposal, send_status, session_id="s1", store=None)
    _, _, order_body = client.modify_order_and_confirm.call_args.args
    assert "changes" not in order_body


def test_execute_modify_order_core_never_touches_action_or_removes_anything():
    """Mirrors the staged-order core's contract test — same no-action-param guarantee."""
    import inspect

    from claudia.order_flow import _execute_modify_order_core
    sig = inspect.signature(_execute_modify_order_core)
    assert "action" not in sig.parameters
    assert "proposal" in sig.parameters
    assert "send_status" in sig.parameters


# ── L2 — post-dispatch read-back ─────────────────────────────────────────────
# "Evidence is the only source of truth for orders, no assumptions." The POST
# response only proves the request was received. IBKR is explicit about this for
# cancels: the {"msg": "Request was submitted"} body "indicates our request to
# cancel order 987654 was received, but not that the order ticket itself has been
# canceled" — https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md
# Status semantics below are quoted from IBKR's own order-status-value page:
# .../endpoints/order-monitoring/order-status-value.md

# "accepted and is working at the destination" / "accepted by the system ... yet
# to be elected" / "completely filled" — all evidence the order reached the market.
CONFIRMED_PLACE = ["Submitted", "PreSubmitted", "Filled"]
# PendingSubmit: "transmitted your order, but have not yet received confirmation
# that it has been accepted by the order destination" — explicitly NOT evidence.
# Inactive / WarnState are likewise not a working order.
NOT_CONFIRMED_PLACE = ["PendingSubmit", "Inactive", "WarnState"]


# ── the delay is a wait, and the read follows the dispatch ───────────────────

def test_readback_delay_sits_above_the_client_subscription_warmup():
    """client.py's order endpoints warm up their subscription with a 1 s sleep; the
    read-back must wait longer than that or it reads a not-yet-populated view."""
    assert _READBACK_DELAY_S >= 2.0


@pytest.mark.asyncio
async def test_read_back_waits_before_reading():
    """The wait is awaited *before* the read, for the full configured delay — this is
    the one test that does not collapse the delay, so the ordering stays pinned."""
    seen = []

    def _status(order_id):
        seen.append(("read", order_id))
        return {"order_status": "Submitted", "order_status_description": "working"}

    async def _fake_sleep(seconds):
        seen.append(("sleep", seconds))

    client = MagicMock()
    client.get_order_status.side_effect = _status
    with patch.object(order_flow.asyncio, "sleep", _fake_sleep):
        confirmed, line, status = await _read_back(client, "555", "place")

    assert seen == [("sleep", _READBACK_DELAY_S), ("read", "555")]
    assert confirmed is True
    assert "Submitted" in line
    assert status["order_status"] == "Submitted"


@pytest.mark.asyncio
async def test_read_back_happens_after_the_dispatch_never_before():
    """A read taken before the POST would describe the pre-dispatch world."""
    ibkr_mod, client = _make_ibkr_mock()
    seen = []
    client.place_order_and_confirm.side_effect = lambda *_a, **_kw: (
        seen.append("dispatch") or [{"order_id": "777"}]
    )
    client.get_order_status.side_effect = lambda oid: (
        seen.append("read") or {"order_status": "Submitted", "order_status_description": ""}
    )
    await _run(_make_action(), ibkr_mod)
    assert seen == ["dispatch", "read"]


# ── place ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", CONFIRMED_PLACE)
async def test_place_reports_confirmed_only_on_working_states(status):
    ibkr_mod, client = _make_ibkr_mock()
    _set_readback(client, order_status=status, order_status_description="d")
    store = MagicMock()
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))
    assert any("Verified via get_order_status" in c and status in c for c in contents)
    assert not any("not confirmed" in c for c in contents)
    meta = store.add_decision.call_args.kwargs["metadata"]
    assert meta["readback_confirmed"] is True
    assert meta["readback_order_status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NOT_CONFIRMED_PLACE)
async def test_place_does_not_claim_success_on_pending_states(status):
    """PendingSubmit/Inactive/WarnState are not a working order — say so, and never
    dress the dispatch up as a placement."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_readback(client, order_status=status, order_status_description="d")
    store = MagicMock()
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))
    assert any("not confirmed" in c and status in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)
    assert not any("successfully" in c for c in contents)
    # The dispatch still happened, so the decision row is still written — recording
    # the observed state, and recording that it was not a confirmation.
    meta = store.add_decision.call_args.kwargs["metadata"]
    assert meta["readback_confirmed"] is False
    assert meta["readback_order_status"] == status
    assert "UNVERIFIED" in store.add_decision.call_args.kwargs["summary_text"]


@pytest.mark.asyncio
async def test_place_without_an_order_id_says_it_could_not_verify():
    """A dispatch that yields no id is unverifiable — the honest output is to say so,
    not silence and never a claim of success."""
    ibkr_mod, client = _make_ibkr_mock()
    # order_status present (so it is not classified a rejection), no id anywhere.
    client.place_order_and_confirm.return_value = [{"order_status": "Submitted"}]
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    assert any("could not be verified" in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)
    client.get_order_status.assert_not_called()
    client.get_live_orders.assert_not_called()


# ── place: presence in the live book ─────────────────────────────────────────
#
# User rule, 2026-07-27: "each action must be validated by evidence: when placing an
# order, check live orders to validate its presence."
#
# The asymmetry that governs every test below — get_live_orders filters _TERMINAL_STATUSES
# (Filled, Cancelled, ApiCancelled, Expired) out of the feed, so:
#   resting order   -> present  -> the strongest positive evidence there is
#   filled order    -> absent   -> absence is EXPECTED, not a failure
#   cancelled order -> absent   -> indistinguishable from never-existed
# Presence is proof; absence never is.

LIVE_ORDER = {"orderId": "999", "status": "Submitted", "ticker": "AAPL"}


@pytest.mark.asyncio
async def test_presence_in_the_live_book_confirms_the_placement():
    """The positive case the user's rule is about: the order is in the live book, so it
    exists and is working — and no second call is needed to say so."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client, LIVE_ORDER)
    store = MagicMock()
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))

    assert any("Verified via get_live_orders" in c and "Submitted" in c for c in contents)
    assert any("present in the live order book" in c for c in contents)
    client.get_order_status.assert_not_called()
    meta = store.add_decision.call_args.kwargs["metadata"]
    assert meta["readback_confirmed"] is True
    assert meta["readback_order_status"] == "Submitted"


@pytest.mark.asyncio
async def test_presence_is_matched_on_the_order_id_not_on_any_order_being_there():
    """Someone else's resting order is not evidence about this one."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client, {"orderId": "111222333", "status": "Submitted"})
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))

    assert not any("Verified via get_live_orders" in c for c in contents)
    client.get_order_status.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    {"order_id": "999", "status": "Submitted"},   # snake_case spelling
    {"orderId": 999, "status": "Submitted"},      # int, as IBKR sometimes returns
])
async def test_presence_accepts_both_id_spellings_and_numeric_ids(row):
    """Both spellings occur across IBKR order responses (see `_extract_order_id`), and a
    missed match here would be reported as an absence — the dangerous direction."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client, row)
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))

    assert any("Verified via get_live_orders" in c for c in contents)
    client.get_order_status.assert_not_called()


@pytest.mark.asyncio
async def test_present_but_not_working_says_the_order_exists_without_confirming_it():
    """Presence proves existence; the row's own status decides whether it is working.
    PendingSubmit is present in the feed but is not a working order — both facts must be
    stated, because either one alone misleads."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client, {"orderId": "999", "status": "PendingSubmit"})
    store = MagicMock()
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))

    assert any("present in the live order book" in c and "PendingSubmit" in c for c in contents)
    assert any("not confirmed" in c for c in contents)
    assert not any("Verified via get_live_orders" in c for c in contents)
    meta = store.add_decision.call_args.kwargs["metadata"]
    assert meta["readback_confirmed"] is False
    assert meta["readback_order_status"] == "PendingSubmit"


@pytest.mark.asyncio
async def test_absence_falls_through_to_the_status_endpoint_and_a_fill_still_confirms():
    """A filled order is absent from the live book by design. Treating that absence as a
    failure would report a real fill as a non-placement."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client)  # empty book
    _set_readback(client, order_status="Filled", order_status_description="completely filled")
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))

    assert any("Verified via get_order_status" in c and "Filled" in c for c in contents)
    client.get_live_orders.assert_called_once()
    client.get_order_status.assert_called_once()


@pytest.mark.asyncio
async def test_absence_is_never_reported_as_success_or_as_failure():
    """The whole rule in one test: absent from the book AND the status read failed. The
    only honest output is "could not verify" — never "the order is not there"."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_live_book(client)
    client.get_order_status.side_effect = RuntimeError("500 Internal Server Error")
    store = MagicMock()
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))
    joined = " ".join(contents)

    assert "could not be verified" in joined
    assert "NOT evidence" in joined                 # absence is named as non-evidence
    assert "Verified via get_live_orders" not in joined
    assert "does not exist" not in joined
    assert "was not placed" not in joined
    assert "Order not placed" not in joined
    assert store.add_decision.call_args.kwargs["metadata"]["readback_confirmed"] is False


@pytest.mark.asyncio
async def test_a_failed_live_book_call_is_not_an_absence():
    """get_live_orders returning 500 says nothing about the order — which is exactly the
    call that failed on 2026-07-27. It must fall through to the status endpoint, and the
    failure must be named, never silently rendered as "not in the book"."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_live_orders.side_effect = RuntimeError("500 Internal Server Error")
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    joined = " ".join(contents)

    assert "live order book could not be read" in joined
    assert "500" in joined
    assert "not in the live order book" not in joined
    client.get_order_status.assert_called_once()


@pytest.mark.asyncio
async def test_a_degenerate_live_book_shape_is_not_an_absence():
    """Fail safe on a shape that is not a list — no invented absence."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_live_orders.return_value = {"orders": "nonsense"}
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))

    assert any("live order book could not be read" in c for c in contents)
    client.get_order_status.assert_called_once()


@pytest.mark.asyncio
async def test_the_live_book_is_read_after_the_wait_and_after_the_dispatch():
    """The wait comes first (a book read taken too early sees a not-yet-populated view),
    and the whole check follows the POST."""
    ibkr_mod, client = _make_ibkr_mock()
    seen = []
    client.place_order_and_confirm.side_effect = lambda *_a, **_kw: (
        seen.append("dispatch") or [{"order_id": "999"}]
    )
    client.get_live_orders.side_effect = lambda: seen.append("live_orders") or [LIVE_ORDER]
    send_status, _calls = _make_send_status_recorder()

    async def _fake_sleep(seconds):
        seen.append(("sleep", seconds))

    proposal = json.loads(_make_action().payload["order"])
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}), \
            patch.object(order_flow.asyncio, "sleep", _fake_sleep):
        await _execute_staged_order_core(proposal, send_status)

    assert seen == ["dispatch", ("sleep", _READBACK_DELAY_S), "live_orders"]


@pytest.mark.asyncio
async def test_the_cancel_path_does_not_use_the_live_book_as_evidence():
    """Disappearance from the live book cannot prove a cancel: Cancelled is one of the
    statuses that feed filters out, so a cancelled order and one that never existed look
    identical there. A cancellation still requires get_order_status == Cancelled."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Cancelled", order_status_description="d")
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod))

    client.get_live_orders.assert_not_called()
    assert any("Verified via get_order_status" in c and "Cancelled" in c for c in contents)


@pytest.mark.asyncio
async def test_the_modify_path_does_not_use_the_live_book_as_evidence():
    """get_live_orders exposes neither the conid nor the full field set the modify
    comparison needs — the per-order endpoint stays the only evidence there."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    await _run_modify(_make_modify_action(), ibkr_mod)

    client.get_live_orders.assert_not_called()
    client.get_order_status.assert_called_once()


# ── cancel ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PendingCancel", "PreCancelled"])
async def test_cancel_pending_is_not_a_cancellation(status):
    """IBKR: "your order is not confirmed canceled. You may still receive an execution
    while your cancellation request is pending." Reporting these as a cancellation is
    exactly the assertion-without-evidence this guardrail exists to stop."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status=status, order_status_description="d")
    store = MagicMock()
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod,
                                                store=store, session_id="s1"))
    assert any("not confirmed" in c for c in contents)
    assert any("may still receive an execution" in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)
    meta = store.add_decision.call_args.kwargs["metadata"]
    assert meta["readback_confirmed"] is False
    assert meta["readback_order_status"] == status


@pytest.mark.asyncio
async def test_cancel_confirmed_only_on_cancelled():
    """"Cancelled": "the balance of your order has been confirmed canceled by the
    system" — the only documented value that is evidence of a cancellation."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Cancelled", order_status_description="d")
    store = MagicMock()
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod,
                                                store=store, session_id="s1"))
    assert any("Verified via get_order_status" in c and "Cancelled" in c for c in contents)
    assert store.add_decision.call_args.kwargs["metadata"]["readback_confirmed"] is True


@pytest.mark.asyncio
async def test_cancel_still_working_is_not_a_cancellation():
    """The order reading Submitted after a cancel request means the cancel has not
    taken effect — never report it as cancelled."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Submitted")
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod))
    assert any("not confirmed" in c and "Submitted" in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)


@pytest.mark.asyncio
async def test_cancel_provisional_line_does_not_claim_the_ticket_is_gone():
    """IBKR's own words about the cancel response: it confirms the request was
    received, not that the order ticket has been canceled."""
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod))
    provisional = next(c for c in contents if "accepted by IBKR" in c)
    assert "not that the order ticket has been cancelled" in provisional
    assert "Verifying live state" in provisional


# ── 503 / read failure ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["place", "cancel"])
async def test_503_reads_as_no_evidence_never_as_confirmation(runner):
    """get_order_status returns 503 by design for orders cancelled or filled before
    the active session, and for FA/linked accounts without an account switch. A read
    that fails is an absence of evidence — it can never be upgraded to confirmation."""
    if runner == "place":
        ibkr_mod, client = _make_ibkr_mock()
        client.get_order_status.side_effect = RuntimeError("503 Service Unavailable")
        contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    else:
        ibkr_mod, client = _make_cancel_modify_ibkr_mock()
        client.get_order_status.side_effect = RuntimeError("503 Service Unavailable")
        contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod))
    assert any("could not be verified" in c and "503" in c for c in contents)
    assert any("Do not assume" in c for c in contents)
    assert not any("Verified via get_order_status" in c for c in contents)
    assert not any("successfully" in c for c in contents)


@pytest.mark.asyncio
async def test_a_failed_read_back_is_not_reported_as_a_failed_dispatch():
    """The order WAS dispatched. Reporting 'Order not placed' because the *read*
    failed would be a second fabrication, in the opposite direction."""
    ibkr_mod, client = _make_ibkr_mock()
    client.get_order_status.side_effect = RuntimeError("boom")
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    assert any("Dispatch accepted by IBKR" in c for c in contents)
    assert not any("Order not placed" in c for c in contents)


@pytest.mark.asyncio
async def test_a_non_dict_read_back_is_not_confirmation():
    """Degenerate shapes fail safe — no invented state."""
    client = MagicMock()
    client.get_order_status.return_value = "nonsense"
    with _no_readback_delay():
        confirmed, line, status = await _read_back(client, "555", "place")
    assert confirmed is False
    assert status is None
    assert "not confirmed" in line


# ── modify — fields, not just status ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_compares_fields_not_just_status():
    """A modify that silently did not apply still reads Submitted — the status alone
    proves the order exists, not that the change landed."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Submitted", total_size=1)  # requested 3
    store = MagicMock()
    action = _make_modify_action({
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 3, "order_type": "LMT", "limit_price": 105.0, "tif": "GTC",
        "sec_type": "STK",
    })
    contents = _sent_contents(await _run_modify(action, ibkr_mod, store=store, session_id="s1"))
    assert any("does NOT match the request" in c for c in contents)
    assert any("quantity" in c and "not confirmed" in c for c in contents)
    assert store.add_decision.call_args.kwargs["metadata"]["readback_confirmed"] is False


@pytest.mark.asyncio
async def test_modify_field_comparison_tolerates_ibkr_number_formatting():
    """IBKR may return "3.0" where the request had 3. A comparison that cried
    mismatch on that would train the user to ignore the warning."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, total_size="3.0", side="buy", order_type="lmt", tif="gtc")
    action = _make_modify_action({
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 3, "order_type": "LMT", "limit_price": 105.0, "tif": "GTC",
        "sec_type": "STK",
    })
    contents = _sent_contents(await _run_modify(action, ibkr_mod))
    assert any("Read-back matches the request" in c for c in contents)
    assert not any("does NOT match" in c for c in contents)


@pytest.mark.asyncio
async def test_modify_says_plainly_that_price_is_not_verifiable():
    """IBKR's order-status response carries no discrete limit/stop price field, so a
    price change cannot be machine-verified. Say that, and surface IBKR's own
    order_description so the human can read the resting price themselves."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_description="BUY 1 AAPL LMT 105.00 GTC")
    contents = _sent_contents(await _run_modify(_make_modify_action(), ibkr_mod))
    assert any("price could not be verified" in c for c in contents)
    assert any("BUY 1 AAPL LMT 105.00 GTC" in c for c in contents)


@pytest.mark.asyncio
async def test_modify_price_caveat_absent_for_a_non_price_modify():
    """No price in the request → no price caveat. The warning must mean something."""
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_type="MKT", total_size=2)
    action = _make_modify_action({
        "order_id": "555", "conid": 265598, "symbol": "AAPL", "action": "BUY",
        "quantity": 2, "order_type": "MKT", "tif": "GTC", "sec_type": "STK",
    })
    contents = _sent_contents(await _run_modify(action, ibkr_mod))
    assert not any("price could not be verified" in c for c in contents)
    assert any("Read-back matches the request" in c for c in contents)


@pytest.mark.asyncio
async def test_modify_non_working_status_is_not_confirmed_even_if_fields_match():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    _set_readback(client, order_status="Inactive")
    contents = _sent_contents(await _run_modify(_make_modify_action(), ibkr_mod))
    assert any("not confirmed" in c and "Inactive" in c for c in contents)


def test_compare_modify_readback_reports_no_comparable_fields_as_unconfirmed():
    """Nothing observed to compare is not a match — never invent agreement."""
    ok, line = _compare_modify_readback({"quantity": 1, "side": "BUY"}, {"order_status": "Submitted"})
    assert ok is False
    assert "no comparable" in line.lower()


# ── _extract_order_id ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param([{"order_id": "1986940574"}], "1986940574", id="snake"),
        pytest.param([{"orderId": "999"}], "999", id="camel"),
        pytest.param([{"order_id": 12345}], "12345", id="int-coerced-to-str"),
        pytest.param({"order_id": "242538143"}, "242538143", id="bare-dict"),
        pytest.param([{"order_id": "0"}], None, id="zero-is-not-an-id"),
        pytest.param([{"order_status": "Submitted"}], None, id="no-id-field"),
        pytest.param([], None, id="empty"),
        pytest.param(["nonsense"], None, id="non-dict"),
        # Last-write-wins across entries, matching _is_ibkr_rejection's reasoning:
        # the reply-chain terminal entry is last, so it is the authoritative one.
        pytest.param([{"order_id": "111"}, {"order_id": "222"}], "222", id="last-wins"),
    ],
)
def test_extract_order_id_contract(result, expected):
    assert _extract_order_id(result) == expected


# ── provisional line ordering ────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", CONFIRMED_PLACE + NOT_CONFIRMED_PLACE)
async def test_provisional_line_precedes_the_verified_one(status):
    """What is known (the dispatch was accepted) is said first and separately from
    what is observed (the live state). Neither is ever called a success."""
    ibkr_mod, client = _make_ibkr_mock()
    _set_readback(client, order_status=status)
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    provisional = next(i for i, c in enumerate(contents) if "accepted by ibkr" in c.lower())
    verified = next(i for i, c in enumerate(contents) if "verifying" not in c.lower()
                    and ("get_order_status" in c or "not confirmed" in c))
    assert provisional < verified
    assert any("verifying" in c.lower() for c in contents)
    assert not any("successfully" in c for c in contents)


# ── a post-dispatch failure must not be reported as a failed dispatch ────────
# The dispatch returning means the write reached IBKR. Anything that fails after
# that — surfacing the result, writing the decision row — is a reporting failure,
# not a placement failure. Saying "Order not placed" there is the same defect this
# task exists to close, pointed the other way.

@pytest.mark.asyncio
async def test_place_post_dispatch_failure_does_not_claim_the_order_was_not_placed():
    ibkr_mod, _client = _make_ibkr_mock()
    store = MagicMock()
    store.add_decision.side_effect = RuntimeError("database is locked")
    contents = _sent_contents(await _run(_make_action(), ibkr_mod, store=store, session_id="s1"))
    assert not any("**Order not placed:**" in c for c in contents)
    assert any("WAS dispatched to IBKR" in c for c in contents)
    assert any("database is locked" in c for c in contents)


@pytest.mark.asyncio
async def test_cancel_post_dispatch_failure_does_not_claim_the_order_was_not_cancelled():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    store = MagicMock()
    store.add_decision.side_effect = RuntimeError("database is locked")
    contents = _sent_contents(await _run_cancel(_make_cancel_action(), ibkr_mod,
                                                store=store, session_id="s1"))
    assert not any("**Order not cancelled:**" in c for c in contents)
    assert any("WAS dispatched to IBKR" in c for c in contents)


@pytest.mark.asyncio
async def test_modify_post_dispatch_failure_does_not_claim_the_order_was_not_modified():
    ibkr_mod, _client = _make_cancel_modify_ibkr_mock()
    store = MagicMock()
    store.add_decision.side_effect = RuntimeError("database is locked")
    contents = _sent_contents(await _run_modify(_make_modify_action(), ibkr_mod,
                                                store=store, session_id="s1"))
    assert not any("**Order not modified:**" in c for c in contents)
    assert any("WAS dispatched to IBKR" in c for c in contents)


@pytest.mark.asyncio
async def test_a_failure_before_the_dispatch_still_says_the_order_was_not_placed():
    """Regression guard on the other branch: the pre-dispatch taxonomy is unchanged."""
    ibkr_mod, client = _make_ibkr_mock()
    client.place_order_and_confirm.side_effect = RuntimeError("Connection reset")
    contents = _sent_contents(await _run(_make_action(), ibkr_mod))
    assert any("**Order not placed:**" in c for c in contents)
    assert not any("WAS dispatched" in c for c in contents)


# ── blank read-back fields are absence, not disagreement ─────────────────────

def test_blank_readback_field_is_uncomparable_not_a_mismatch():
    """An empty value carries no information — treating it as disagreement would be a
    false alarm, and false alarms are what teach a user to ignore the real ones."""
    ok, line = _compare_modify_readback(
        {"quantity": 3, "side": "BUY"},
        {"order_status": "Submitted", "total_size": "   ", "side": "BUY"},
    )
    assert ok is True
    assert "does NOT match" not in line
    assert "quantity" not in line


def test_documented_cancel_success_body_is_not_classified_a_rejection():
    """The documented successful-cancel body carries no order_status — it must not trip
    _is_ibkr_rejection's no-status/zero-id marker, or a real cancel would be reported as
    FAILED and never reach the read-back at all. Shape verbatim from
    https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md"""
    body = {"msg": "Request was submitted", "order_id": 987654,
            "conid": 265598, "account": "U12345"}
    assert _is_ibkr_rejection(body) is False


@pytest.mark.asyncio
async def test_an_unknown_readback_action_can_never_confirm():
    """Fail-safe polarity: a programming error must not become a false confirmation."""
    client = MagicMock()
    client.get_order_status.return_value = {"order_status": "Submitted"}
    with _no_readback_delay():
        confirmed, line, _status = await _read_back(client, "555", "not-an-action")
    assert confirmed is False
    assert "not confirmed" in line


# ── read-back vocabulary (live-measured 2026-08-05) ───────────────────────────
#
# A modify that applied perfectly was reported to the user as "the modification is NOT
# confirmed", because IBKR answers in a different vocabulary than it accepts. Order
# 314390101, limit 50 -> 100: the change landed, and the read-back objected that it had
# requested 'LMT' where IBKR reported 'LIMIT', and 'BUY' where IBKR reported 'B'.
#
# For the SAME order that day, `get_live_orders` reported "BUY" and "Limit", and IBKR's
# own OpenAPI spec documents orderStatus.side as enum ['BUY','SELL'] — which the live
# response did not honour. Three spellings, one field.


def test_ibkr_vocabulary_does_not_read_as_a_mismatch():
    """The exact pair that fired on a successful modify."""
    from claudia.order_flow import _values_match

    assert _values_match("LMT", "LIMIT")
    assert _values_match("BUY", "B")
    assert _values_match("SELL", "S")


def test_vocabulary_folding_does_not_hide_a_real_difference():
    """The other direction of failure. Silencing a genuine mismatch would defeat the
    read-back entirely, which is worse than the false alarm this fixes."""
    from claudia.order_flow import _values_match

    assert not _values_match("BUY", "SELL")
    assert not _values_match("BUY", "S")
    assert not _values_match("LMT", "MKT")
    assert not _values_match("LMT", "STP")


def test_an_unmeasured_vocabulary_still_fails_loud():
    """Stop and market synonyms are deliberately NOT guessed at. Until a pair has been
    observed it must mismatch: a false alarm costs a second look, a silently-accepted
    difference costs a wrong order state."""
    from claudia.order_flow import _values_match

    assert not _values_match("STP", "STOP")
    assert not _values_match("MKT", "MARKET")


def test_a_successful_modify_now_reads_as_confirmed():
    """End to end through the comparison, in the shape IBKR actually returned."""
    from claudia.order_flow import _compare_modify_readback

    agree, line = _compare_modify_readback(
        {"orderType": "LMT", "side": "BUY", "tif": "GTC", "quantity": 1},
        {"order_type": "LIMIT", "side": "B", "tif": "GTC", "total_size": "1.0"},
    )
    assert agree is True
    assert "does NOT match" not in line


def test_a_genuinely_changed_side_is_still_caught():
    from claudia.order_flow import _compare_modify_readback

    agree, line = _compare_modify_readback(
        {"orderType": "LMT", "side": "BUY", "quantity": 1},
        {"order_type": "LIMIT", "side": "S", "total_size": "1.0"},
    )
    assert agree is False
    assert "side" in line
