"""Panel rendering layer for order_flow.py's framework-agnostic order/cancel/modify cores.

Reuses order_flow.py's framework-agnostic pieces directly: _format_*_summary (pure
formatting, already tested) and _execute_*_order_core (the actual safety-critical
order-placement logic, extracted in a prior task specifically so this file never
re-derives it — see that task's rationale). Only the rendering (buttons embedded in a
chat message) and the send_status wiring are Panel-specific.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any

import panel as pn
from param.parameterized import Event

from claudia.order_flow import (
    CANCEL_ORDER_LABEL,
    DISCARD_LABEL,
    KEEP_ORDER_LABEL,
    MODIFY_ORDER_LABEL,
    STAGE_ORDER_LABEL,
    SendStatus,
    _execute_cancel_order_core,
    _execute_modify_order_core,
    _execute_staged_order_core,
    _format_cancel_summary,
    _format_modify_summary,
    _format_order_summary,
    cancel_proposal_contract_label,
    proposal_contract_label,
)
from claudia.panel_markdown import safe_markdown

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)


_ORDER_BUTTON_COLORS: dict[str, str] = {
    STAGE_ORDER_LABEL: "primary",
    MODIFY_ORDER_LABEL: "primary",
    CANCEL_ORDER_LABEL: "danger",
    DISCARD_LABEL: "danger",
    KEEP_ORDER_LABEL: "default",
}
"""The one colour of each order-card button, keyed by its label (gap #67).

Agreed with the operator on 2026-09-24, one button at a time: **blue (`primary`) validates**
(the two buttons that lead to a new or changed order), **red (`danger`) throws away or
removes** (cancelling a live order, discarding a proposal), **neutral (`default`) leaves in
place**. Keyed by label so one word can never carry two colours: the operator caught exactly
that when `DISCARD` was red on one card and neutral on the other.

Why not green for the confirm buttons, as before: green is a *side* colour on this path (the
Gate 2 banner is green for BUY, red for SELL), so a green button on a SELL card said "buy" at
a glance. Why `CANCEL ORDER` stays red although the user asked for it (Apple drops the
destructive style for a deliberately chosen action): a model chose *which* order, and trading
tools colour cancel/flatten red. Why `default` and not `light` for neutral: `.bk-btn-light`
has a transparent border in Bokeh's CSS, so it reads as text (`docs/panel/ui-design-reference.md`
§6, rejected 2026-09-04). Panel's `primary` is `#0d6efd`, close to Apple's accessible system
blue (R30 G110 B244; `.firecrawl/apple-buttons/color.md`).
"""


def _order_button(label: str) -> pn.widgets.Button:
    """Build an order-card button: its label, and the colour that label always carries.

    Every button on the three cards is built here, never with an inline `color=`, so the
    table above is the only place a colour is chosen. A label missing from the table raises
    `KeyError` at render rather than falling back to a default colour.
    """
    return pn.widgets.Button(label=label, color=_ORDER_BUTTON_COLORS[label])


def _snapshot(proposal: dict[str, Any]) -> dict[str, Any]:
    """The card's own copy of the proposal, taken before anything is rendered from it.

    What the human reads and what the click sends must be the same values. Until 2026-09-14
    the click closure held the model's own `tool_use.input` object by reference, so any
    writer reaching that dict between render and click would have changed the order without
    changing the card (audit 2026-09-13, finding B-4). No such writer exists — the
    invariant rested on that absence, which is exactly the shape `ibkr_core_mcp.client`
    closed one layer down on 2026-09-13 by copying the body at method entry.

    `deepcopy`, not `dict()`: `changes` is a list of dicts and a shallow copy would still
    share it.

    Args:
        proposal: The validated proposal dict, as handed to the sink.

    Returns:
        An independent copy. The caller rebinds its own name to it, so every later read —
        summary, contract label, click closure — sees the same frozen values.
    """
    return copy.deepcopy(proposal)


class _OneShot:
    """A card is acted on at most once, enforced here rather than by the browser.

    `disabled = True` was set on every handler and read by none, so the one-shot lived
    entirely client-side: two `clicks` events arriving before the disabled patch reached the
    browser ran the core twice, each with a fresh `cOID` (audit 2026-09-13, finding A-6).
    Both gates fire on the second pass, so this was never a bypass — but "at most once" was
    a docstring, and a server-side re-entry had nothing to stop it.

    Claimed synchronously, before the first `await` in any handler, so two coroutines
    scheduled from the same event cannot both win it on a single-threaded loop. Covers the
    dismiss buttons too: once a card has been acted on, neither half of it acts again.
    """

    def __init__(self) -> None:
        """Unclaimed."""
        self._claimed = False

    def claim(self) -> bool:
        """Take the single action this card allows.

        Returns:
            True for the first caller, False for every later one.
        """
        if self._claimed:
            return False
        self._claimed = True
        return True


def _make_send_status(chat: pn.chat.ChatInterface) -> SendStatus:
    """Bind a send_status callback to one specific chat session. order_flow's
    _execute_*_order_core functions call this SendStatus `(text, author) -> None` to
    surface progress and results; binding it to `chat` routes those messages to the
    right Panel session (each session gets its own ChatInterface)."""

    async def _send_status(text: str, author: str) -> None:
        """Route one status line to this session's chat feed."""
        chat.send(text, user=author, respond=False)

    return _send_status


async def render_order_proposal(
    chat: pn.chat.ChatInterface,
    proposal: dict[str, Any],
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render an order proposal as a chat message with a staging button.

    The rendered summary is the human's only pre-Touch-ID view of what they are approving,
    so it goes through `safe_markdown` — `reason` is free-form LLM prose (SECURITY.md §9).
    The proposal has already been schema-checked in `agent.py` before reaching here.

    Args:
        chat: The session's ChatInterface. Also the target for status messages.
        proposal: Validated order-proposal dict from the LLM.
        session_id: Session to attribute the decision to. Optional — omitting it (with
            `store`) means the click is executed but not recorded in the decision log.
        store: Conversation store for decision logging. Optional, as above.
    """
    proposal = _snapshot(proposal)
    contract_label = await asyncio.to_thread(proposal_contract_label, proposal)
    summary_pane = safe_markdown(_format_order_summary(proposal, contract_label=contract_label))
    acted = _OneShot()
    stage_btn = _order_button(STAGE_ORDER_LABEL)
    discard_btn = _order_button(DISCARD_LABEL)
    send_status = _make_send_status(chat)

    async def _on_stage(event: Event) -> None:
        """Stage the order — the click that initiates a live IBKR order.

        One-shot: both buttons are disabled before the core runs and are never re-enabled,
        so a proposal can be acted on at most once. Contrast `panel_pinescript`, which
        re-enables on failure because injection is idempotent; order placement is not.

        Gate 1 (Touch ID) and Gate 2 (the AppKit dialog) run inside the core — this handler
        does not itself confirm anything. Exceptions are logged and re-raised so Panel's own
        error surfacing still fires rather than the failure being swallowed.
        """
        # Disabled before the call starts, not only in finally: _execute_staged_order_core's
        # Gate 1/Gate 2 chain is fully synchronous (blocking threading/subprocess calls, no
        # await suspension point) — the server-side state is stale from the first moment a
        # double-click could happen either way, but there is no reason to leave the earlier
        # window open when closing it costs nothing.
        if not acted.claim():
            return
        stage_btn.disabled = True
        discard_btn.disabled = True
        try:
            await _execute_staged_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order staging failed (session %s)", session_id)
            raise

    async def _on_discard(event: Event) -> None:
        """Dismiss the proposal without contacting IBKR. Disables both buttons first."""
        if not acted.claim():
            return
        stage_btn.disabled = True
        discard_btn.disabled = True
        try:
            chat.send(
                "Order proposal discarded — nothing was sent to IBKR.",
                user="ClaudIA",
                respond=False,
            )
        except Exception:
            log.exception("Failed to send order-proposal discard notice (session %s)", session_id)
            raise

    stage_btn.on_click(_on_stage)
    discard_btn.on_click(_on_discard)

    chat.send(
        pn.Column(summary_pane, pn.Row(stage_btn, discard_btn)),
        user="ClaudIA — Order Proposal",
        respond=False,
    )


async def render_cancel_proposal(
    chat: pn.chat.ChatInterface,
    proposal: dict[str, Any],
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a cancel proposal as a chat message with a cancel button.

    Args:
        chat: The session's ChatInterface. Also the target for status messages.
        proposal: Validated cancel-proposal dict; `order_id` identifies the live order.
        session_id: Session to attribute the decision to. Optional — see
            `render_order_proposal`.
        store: Conversation store for decision logging. Optional, as above.
    """
    proposal = _snapshot(proposal)
    contract_label = await asyncio.to_thread(cancel_proposal_contract_label, proposal)
    summary_pane = safe_markdown(_format_cancel_summary(proposal, contract_label=contract_label))
    acted = _OneShot()
    cancel_btn = _order_button(CANCEL_ORDER_LABEL)
    keep_btn = _order_button(KEEP_ORDER_LABEL)
    send_status = _make_send_status(chat)

    async def _on_cancel_click(event: Event) -> None:
        """Cancel the live order — same one-shot and Gate 1/Gate 2 contract as `_on_stage`."""
        if not acted.claim():
            return
        cancel_btn.disabled = True
        keep_btn.disabled = True
        try:
            await _execute_cancel_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order cancellation failed (session %s)", session_id)
            raise

    async def _on_keep_click(event: Event) -> None:
        """Dismiss the proposal, leaving the order untouched. No IBKR call."""
        if not acted.claim():
            return
        cancel_btn.disabled = True
        keep_btn.disabled = True
        try:
            chat.send(
                "Proposal dismissed — order left unchanged at IBKR.", user="ClaudIA", respond=False
            )
        except Exception:
            log.exception(
                "Failed to send cancel-proposal dismissal notice (session %s)", session_id
            )
            raise

    cancel_btn.on_click(_on_cancel_click)
    keep_btn.on_click(_on_keep_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(cancel_btn, keep_btn)),
        user="ClaudIA — Cancel Proposal",
        respond=False,
    )


async def render_modify_proposal(
    chat: pn.chat.ChatInterface,
    proposal: dict[str, Any],
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a modify proposal as a chat message with a modify button.

    The summary shows a field-by-field diff built from the LLM-supplied `changes` array —
    i.e. the "before" column is authored by the same party proposing the change. Gate 2
    re-renders the real order for confirmation, so the authoritative view is the AppKit
    dialog, not this summary.

    Args:
        chat: The session's ChatInterface. Also the target for status messages.
        proposal: Validated modify-proposal dict; IBKR requires the *full* replacement
            order, not a diff.
        session_id: Session to attribute the decision to. Optional — see
            `render_order_proposal`.
        store: Conversation store for decision logging. Optional, as above.
    """
    proposal = _snapshot(proposal)
    contract_label = await asyncio.to_thread(proposal_contract_label, proposal)
    summary_pane = safe_markdown(_format_modify_summary(proposal, contract_label=contract_label))
    acted = _OneShot()
    modify_btn = _order_button(MODIFY_ORDER_LABEL)
    discard_btn = _order_button(DISCARD_LABEL)
    send_status = _make_send_status(chat)

    async def _on_modify_click(event: Event) -> None:
        """Modify the live order — same one-shot and Gate 1/Gate 2 contract as `_on_stage`."""
        if not acted.claim():
            return
        modify_btn.disabled = True
        discard_btn.disabled = True
        try:
            await _execute_modify_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order modification failed (session %s)", session_id)
            raise

    async def _on_discard_click(event: Event) -> None:
        """Discard the proposal, leaving the order untouched. No IBKR call."""
        if not acted.claim():
            return
        modify_btn.disabled = True
        discard_btn.disabled = True
        try:
            chat.send(
                "Modify proposal discarded — order left unchanged at IBKR.",
                user="ClaudIA",
                respond=False,
            )
        except Exception:
            log.exception("Failed to send modify-proposal discard notice (session %s)", session_id)
            raise

    modify_btn.on_click(_on_modify_click)
    discard_btn.on_click(_on_discard_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(modify_btn, discard_btn)),
        user="ClaudIA — Modify Proposal",
        respond=False,
    )
