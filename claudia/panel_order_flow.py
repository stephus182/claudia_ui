"""Panel rendering layer for order_flow.py's framework-agnostic order/cancel/modify cores.

Reuses order_flow.py's framework-agnostic pieces directly: _format_*_summary (pure
formatting, already tested) and _execute_*_order_core (the actual safety-critical
order-placement logic, extracted in a prior task specifically so this file never
re-derives it — see that task's rationale). Only the rendering (buttons embedded in a
chat message) and the send_status wiring are Panel-specific.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import panel as pn

from claudia.order_flow import (
    SendStatus,
    _execute_cancel_order_core,
    _execute_modify_order_core,
    _execute_staged_order_core,
    _format_cancel_summary,
    _format_modify_summary,
    _format_order_summary,
)
from claudia.panel_markdown import safe_markdown

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)


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
    proposal: dict,
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
    summary_pane = safe_markdown(_format_order_summary(proposal))
    stage_btn = pn.widgets.Button(label="Stage this order", color="success")
    cancel_btn = pn.widgets.Button(label="Cancel", color="light")
    send_status = _make_send_status(chat)

    async def _on_stage(event) -> None:
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
        stage_btn.disabled = True
        cancel_btn.disabled = True
        try:
            await _execute_staged_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order staging failed (session %s)", session_id)
            raise

    async def _on_cancel(event) -> None:
        """Dismiss the proposal without contacting IBKR. Disables both buttons first."""
        stage_btn.disabled = True
        cancel_btn.disabled = True
        try:
            chat.send("Order proposal cancelled.", user="ClaudIA", respond=False)
        except Exception:
            log.exception("Failed to send order-proposal cancellation notice (session %s)", session_id)
            raise

    stage_btn.on_click(_on_stage)
    cancel_btn.on_click(_on_cancel)

    chat.send(
        pn.Column(summary_pane, pn.Row(stage_btn, cancel_btn)),
        user="ClaudIA — Order Proposal",
        respond=False,
    )


async def render_cancel_proposal(
    chat: pn.chat.ChatInterface,
    proposal: dict,
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
    summary_pane = safe_markdown(_format_cancel_summary(proposal))
    cancel_btn = pn.widgets.Button(label="Cancel this order", color="danger")
    keep_btn = pn.widgets.Button(label="Keep order", color="light")
    send_status = _make_send_status(chat)

    async def _on_cancel_click(event) -> None:
        """Cancel the live order — same one-shot and Gate 1/Gate 2 contract as `_on_stage`."""
        cancel_btn.disabled = True
        keep_btn.disabled = True
        try:
            await _execute_cancel_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order cancellation failed (session %s)", session_id)
            raise

    async def _on_keep_click(event) -> None:
        """Dismiss the proposal, leaving the order untouched. No IBKR call."""
        cancel_btn.disabled = True
        keep_btn.disabled = True
        try:
            chat.send("Cancel proposal dismissed — order left unchanged.", user="ClaudIA", respond=False)
        except Exception:
            log.exception("Failed to send cancel-proposal dismissal notice (session %s)", session_id)
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
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a modify proposal as a chat message with a modify button.

    The summary shows a field-by-field diff built from the LLM-supplied `_changed_fields`
    and `_previous_values` keys — i.e. the "before" column is authored by the same party
    proposing the change. Gate 2 re-renders the real order for confirmation, so the
    authoritative view is the AppKit dialog, not this summary.

    Args:
        chat: The session's ChatInterface. Also the target for status messages.
        proposal: Validated modify-proposal dict; IBKR requires the *full* replacement
            order, not a diff.
        session_id: Session to attribute the decision to. Optional — see
            `render_order_proposal`.
        store: Conversation store for decision logging. Optional, as above.
    """
    summary_pane = safe_markdown(_format_modify_summary(proposal))
    modify_btn = pn.widgets.Button(label="Modify this order", color="success")
    discard_btn = pn.widgets.Button(label="Discard", color="light")
    send_status = _make_send_status(chat)

    async def _on_modify_click(event) -> None:
        """Modify the live order — same one-shot and Gate 1/Gate 2 contract as `_on_stage`."""
        modify_btn.disabled = True
        discard_btn.disabled = True
        try:
            await _execute_modify_order_core(proposal, send_status, session_id, store)
        except Exception:
            log.exception("Order modification failed (session %s)", session_id)
            raise

    async def _on_discard_click(event) -> None:
        """Discard the proposal, leaving the order untouched. No IBKR call."""
        modify_btn.disabled = True
        discard_btn.disabled = True
        try:
            chat.send("Modify proposal discarded — order left unchanged.", user="ClaudIA", respond=False)
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
