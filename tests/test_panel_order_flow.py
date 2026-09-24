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
    client.modify_order_and_confirm.return_value = {
        "order_id": "242538143",
        "order_status": "Submitted",
    }
    return mod, client


# ─── The order buttons: naming, colour and behaviour (gap #67) ───────────────────────────
#
# Agreed with the operator one button at a time on 2026-09-24 (memory
# `feedback-order-button-naming-and-colour`): UPPERCASE, no articles; blue (`primary`) =
# validate, red (`danger`) = throw away or remove, neutral (`default`) = leave in place;
# never "CANCEL" on a button that does not cancel an order. The expected values below are
# written out literally ON PURPOSE — they are the spec, so they must not be read from the
# module under test.
#
# Every behavioural test finds its button BY LABEL and clicks it. The earlier tests found
# buttons by position and checked labels and clicks separately, so nothing held "the button
# that reads X" to "the action it performs", and two of the three dismiss buttons were never
# clicked at all: wired to an order core, they would have left the suite green.

_PLACE = {
    "symbol": "AAPL",
    "action": "BUY",
    "quantity": 10,
    "conid": 265598,
    "order_type": "MKT",
    "limit_price": None,
    "stop_price": None,
}
_CANCEL = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
_MODIFY = {
    "order_id": "555",
    "conid": 265598,
    "symbol": "AAPL",
    "action": "BUY",
    "quantity": 1,
    "order_type": "LMT",
    "limit_price": 105.0,
    "changes": [{"field": "limit_price", "previous_value": 100.0}],
}

# Every client method that writes an order. A click reaches exactly one of them or none.
_WRITES = (
    "place_order",
    "place_order_and_confirm",
    "modify_order",
    "modify_order_and_confirm",
    "cancel_order",
    "reply_order",
)

# (card, label) -> the one write that label's click must reach; None = no IBKR write at all.
_EFFECT = {
    ("place", "STAGE ORDER"): "place_order_and_confirm",
    ("place", "DISCARD"): None,
    ("cancel", "CANCEL ORDER"): "cancel_order",
    ("cancel", "KEEP ORDER"): None,
    ("modify", "MODIFY ORDER"): "modify_order_and_confirm",
    ("modify", "DISCARD"): None,
}

# (card, dismiss label) -> the exact chat line the dismissal posts.
_DISMISS_MESSAGE = {
    ("place", "DISCARD"): "Order proposal discarded — nothing was sent to IBKR.",
    ("cancel", "KEEP ORDER"): "Proposal dismissed — order left unchanged at IBKR.",
    ("modify", "DISCARD"): "Modify proposal discarded — order left unchanged at IBKR.",
}


async def _render(kind: str, chat):
    """Render one card of `kind` and return (summary pane, button row)."""
    if kind == "place":
        await render_order_proposal(chat, dict(_PLACE), session_id="s1", store=None)
    elif kind == "cancel":
        with patch("claudia.panel_order_flow.cancel_proposal_contract_label", return_value=None):
            await render_cancel_proposal(chat, dict(_CANCEL), session_id="s1", store=None)
    else:
        await render_modify_proposal(chat, dict(_MODIFY), session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    return column[0], column[1]


def _by_label(row, label):
    """The one button in `row` whose label is `label` — never found by position."""
    hits = [b for b in row if b.label == label]
    assert len(hits) == 1, f"expected one {label!r} button, found {[b.label for b in row]}"
    return hits[0]


def _writes_reached(client) -> list[str]:
    """The order-writing client methods a click actually called, in `_WRITES` order."""
    return [w for w in _WRITES if getattr(client, w).called]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("place", [("STAGE ORDER", "primary"), ("DISCARD", "danger")]),
        ("cancel", [("CANCEL ORDER", "danger"), ("KEEP ORDER", "default")]),
        ("modify", [("MODIFY ORDER", "primary"), ("DISCARD", "danger")]),
    ],
)
async def test_each_card_shows_exactly_the_agreed_buttons_and_colours(kind, expected):
    """Labels, colours and their order on each card, as agreed on 2026-09-24."""
    chat = _make_chat()
    _, row = await _render(kind, chat)
    assert [(b.label, b.color) for b in row] == expected


@pytest.mark.asyncio
async def test_the_place_card_is_sent_as_one_order_proposal_message():
    """The place card is one message, attributed to the proposal author, with two buttons."""
    chat = _make_chat()
    await render_order_proposal(chat, dict(_PLACE), session_id="s1", store=None)
    chat.send.assert_called_once()
    assert chat.send.call_args.kwargs["user"] == "ClaudIA — Order Proposal"
    assert len(chat.send.call_args.args[0][1]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "label"), list(_EFFECT))
async def test_each_button_does_what_its_label_says(kind, label):
    """Click the button FOUND BY ITS LABEL; it reaches exactly its own write, or none."""
    write = _EFFECT[(kind, label)]
    chat = _make_chat()
    ibkr_mod, client = _make_ibkr_mock()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        _, row = await _render(kind, chat)
        clients_built = ibkr_mod.IBKRClient.call_count
        await _get_click_callback(_by_label(row, label))(None)

    assert _writes_reached(client) == ([write] if write else [])
    if write is None:
        # Not merely "no write": a dismissal must not even build a client.
        assert ibkr_mod.IBKRClient.call_count == clients_built
        assert chat.send.call_args.args[0] == _DISMISS_MESSAGE[(kind, label)]
    assert all(b.disabled for b in row), "every button on a card is one-shot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "dismiss", "confirm"),
    [
        ("place", "DISCARD", "STAGE ORDER"),
        ("cancel", "KEEP ORDER", "CANCEL ORDER"),
        ("modify", "DISCARD", "MODIFY ORDER"),
    ],
)
async def test_after_a_dismiss_the_confirm_button_dispatches_nothing(kind, dismiss, confirm):
    """Dismiss, then click the confirm button anyway: no write reaches IBKR.

    The one-shot was only ever tested as "confirm twice". This is the other order, and the
    one a human can produce: a disabled button still has its handler on the server.
    """
    chat = _make_chat()
    ibkr_mod, client = _make_ibkr_mock()
    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        _, row = await _render(kind, chat)
        await _get_click_callback(_by_label(row, dismiss))(None)
        await _get_click_callback(_by_label(row, confirm))(None)

    assert _writes_reached(client) == []


@pytest.mark.asyncio
async def test_order_buttons_obey_the_naming_and_colour_rules_as_a_class():
    """The rules over EVERY order button, derived from what each click actually does.

    Not a list of today's labels: a new card, or a label or handler moved between buttons,
    is judged by its behaviour. One label has one colour; blue exactly on the buttons that
    reach a place or modify write; red on the one that reaches a cancel, and "CANCEL" on no
    other button; UPPERCASE throughout.
    """
    seen: dict[str, set[str]] = {}
    for kind in ("place", "cancel", "modify"):
        _, row = await _render(kind, _make_chat())
        for button in row:
            chat = _make_chat()
            ibkr_mod, client = _make_ibkr_mock()
            with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
                _, fresh = await _render(kind, chat)
                await _get_click_callback(_by_label(fresh, button.label))(None)
            reached = _writes_reached(client)
            validates = any(w.startswith(("place", "modify")) for w in reached)
            cancels = "cancel_order" in reached

            assert button.label == button.label.upper(), button.label
            assert (button.color == "primary") == validates, (button.label, button.color)
            if cancels:
                assert button.color == "danger", button.label
            assert ("CANCEL" in button.label) == cancels, button.label
            seen.setdefault(button.label, set()).add(button.color)

    assert all(len(colours) == 1 for colours in seen.values()), seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "confirm"),
    [("place", "STAGE ORDER"), ("cancel", "CANCEL ORDER"), ("modify", "MODIFY ORDER")],
)
async def test_the_card_text_names_the_button_that_exists(kind, confirm):
    """The instruction on each card names its real confirm button, and no retired label."""
    summary, row = await _render(kind, _make_chat())
    assert _by_label(row, confirm)  # the button exists
    assert f"'{confirm}'" in summary.object
    for retired in ("Stage this order", "Cancel this order", "Modify this order"):
        assert retired not in summary.object


@pytest.mark.asyncio
async def test_render_order_proposal_stage_click_executes_and_disables_buttons():
    """Clicking stage runs the execution core and disables both buttons — one-shot."""
    chat = _make_chat()
    # Carries a conid because real proposals do, and because a placement without one is
    # now refused before it reaches IBKR (order_flow._needs_conid_text).
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 10,
        "conid": 265598,
        "order_type": "MKT",
        "limit_price": None,
        "stop_price": None,
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
async def test_a_second_click_does_not_dispatch_a_second_order():
    """One proposal, at most one write — enforced in the handler, not by the browser.

    `disabled = True` is set but never read, so the one-shot lived entirely in the client:
    two `clicks` events arriving before the disabled patch reaches the browser ran the core
    twice, each with a fresh cOID (audit 2026-09-13, finding A-6). Both gates still fire on
    the second pass, so this was never a bypass — but "at most once" was a docstring, and a
    server-side re-entry (a replayed event, a future programmatic trigger) had nothing to
    stop it.
    """
    chat = _make_chat()
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 10,
        "conid": 265598,
        "order_type": "MKT",
        "limit_price": None,
        "stop_price": None,
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    stage_btn = chat.send.call_args.args[0][1][0]
    click = _get_click_callback(stage_btn)

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await click(None)
        await click(None)

    client.place_order_and_confirm.assert_called_once()


@pytest.mark.asyncio
async def test_the_card_is_a_snapshot_of_what_the_click_will_send():
    """What the human read must be what the click sends, whatever happens to the dict after.

    The closure held the model's own proposal object by reference, so anything that wrote
    to it between render and click would change the order without changing the card
    (audit 2026-09-13, finding B-4). No writer exists today; the invariant rested on that
    absence, which is the same shape ibkr_core_mcp closed one layer down on 2026-09-13 by
    copying the body at method entry.
    """
    chat = _make_chat()
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 10,
        "conid": 265598,
        "order_type": "LMT",
        "limit_price": 100.0,
        "stop_price": None,
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_order_proposal(chat, proposal, session_id="s1", store=None)
    stage_btn = chat.send.call_args.args[0][1][0]

    # Whatever this is — a sink normalisation, a later turn, a future recall path.
    proposal["quantity"] = 999
    proposal["limit_price"] = 1.0

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(stage_btn)(None)

    body = client.place_order_and_confirm.call_args.args[1]
    assert body["quantity"] == 10, f"the click sent a quantity the card never showed: {body}"
    assert body["price"] == 100.0, f"the click sent a price the card never showed: {body}"


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
async def test_render_cancel_proposal_confirm_click_calls_cancel_core():
    """Confirming routes to the cancel core, not to any other path."""
    chat = _make_chat()
    proposal = {
        "order_id": "555",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "MKT",
    }
    ibkr_mod, client = _make_ibkr_mock()
    with patch("claudia.panel_order_flow.cancel_proposal_contract_label", return_value=None):
        await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    cancel_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(cancel_btn)(None)

    args, kwargs = client.cancel_order.call_args
    assert args == ("U12345", "555")
    # The core hands the dialog IBKR-shaped detail (gap #40), never the proposal dict.
    assert kwargs["order_details"]["side"] == "BUY" and "order_id" not in kwargs["order_details"]


@pytest.mark.asyncio
async def test_render_modify_proposal_confirm_click_calls_modify_core():
    """Confirming routes to the modify core, not to any other path."""
    chat = _make_chat()
    proposal = {
        "order_id": "555",
        "conid": 265598,
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 105.0,
        "changes": [{"field": "limit_price", "previous_value": 100.0}],
    }
    ibkr_mod, client = _make_ibkr_mock()
    await render_modify_proposal(chat, proposal, session_id="s1", store=None)
    column = chat.send.call_args.args[0]
    modify_btn = column[1][0]

    with patch.dict("sys.modules", {"ibkr_core_mcp": ibkr_mod, "dotenv": MagicMock()}):
        await _get_click_callback(modify_btn)(None)

    client.modify_order_and_confirm.assert_called_once()


@pytest.mark.asyncio
async def test_render_order_proposal_shows_the_contract_line_for_a_future():
    """The render site fetches the label off the loop and the summary carries it."""
    chat = _make_chat()
    proposal = {
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "STP",
        "stop_price": 7900.0,
        "tif": "GTC",
        "sec_type": "FUT",
        "conid": 649180671,
        "reason": "test",
    }
    with patch(
        "claudia.panel_order_flow.proposal_contract_label",
        return_value="ESU6 · SEP26 · expires 2026-09-18",
    ) as label:
        await render_order_proposal(chat, proposal, session_id="s1", store=None)
    label.assert_called_once_with(proposal)
    column = chat.send.call_args.args[0]
    assert "ESU6 · SEP26 · expires 2026-09-18" in column[0].object


@pytest.mark.asyncio
async def test_render_cancel_proposal_names_the_resolved_contract():
    """The cancel card carries the contract it is about to remove (gap #37).

    The resolver is read off the live order rather than the proposal, because
    `propose_cancel` carries no conid — so this pins the wiring, not just the formatter.
    """
    chat = _make_chat()
    proposal = {
        "order_id": "1793215923",
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "STP",
        "stop_price": 7900.0,
        "tif": "GTC",
    }
    with patch(
        "claudia.panel_order_flow.cancel_proposal_contract_label",
        return_value="ESU6 · SEP26 · expires 2026-09-18",
    ) as label:
        await render_cancel_proposal(chat, proposal, session_id="s1", store=None)
    label.assert_called_once_with(proposal)
    column = chat.send.call_args.args[0]
    assert "ESU6 · SEP26 · expires 2026-09-18" in column[0].object
