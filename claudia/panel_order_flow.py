"""Panel counterpart to order_flow.py's Chainlit-native render_*_proposal functions.

Reuses order_flow.py's framework-agnostic pieces directly: _format_*_summary (pure
formatting, already tested) and _execute_*_order_core (the actual safety-critical
order-placement logic, extracted in a prior task specifically so this file never
re-derives it — see that task's rationale). Only the rendering (buttons embedded in a
chat message) and the send_status wiring are Panel-specific.
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore


def _make_send_status(chat) -> SendStatus:
    """Bind a send_status callback to one specific chat session — the Panel
    counterpart to order_flow.py's module-level _cl_send_status, which doesn't need
    binding since Chainlit's cl.Message is already session-scoped via contextvars."""
    async def _send_status(text: str, author: str) -> None:
        chat.send(text, user=author, respond=False)
    return _send_status


async def render_order_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render an order proposal as a Panel chat message with staging/cancel buttons."""
    summary_pane = pn.pane.Markdown(_format_order_summary(proposal))
    stage_btn = pn.widgets.Button(name="Stage this order", button_type="success")
    cancel_btn = pn.widgets.Button(name="Cancel", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_stage(event) -> None:
        try:
            await _execute_staged_order_core(proposal, send_status, session_id, store)
        finally:
            stage_btn.disabled = True
            cancel_btn.disabled = True

    async def _on_cancel(event) -> None:
        chat.send("Order proposal cancelled.", user="ClaudIA", respond=False)
        stage_btn.disabled = True
        cancel_btn.disabled = True

    stage_btn.on_click(_on_stage)
    cancel_btn.on_click(_on_cancel)

    chat.send(
        pn.Column(summary_pane, pn.Row(stage_btn, cancel_btn)),
        user="ClaudIA — Order Proposal",
        respond=False,
    )


async def render_cancel_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a cancel proposal as a Panel chat message with cancel/keep buttons."""
    summary_pane = pn.pane.Markdown(_format_cancel_summary(proposal))
    cancel_btn = pn.widgets.Button(name="Cancel this order", button_type="danger")
    keep_btn = pn.widgets.Button(name="Keep order", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_cancel_click(event) -> None:
        try:
            await _execute_cancel_order_core(proposal, send_status, session_id, store)
        finally:
            cancel_btn.disabled = True
            keep_btn.disabled = True

    async def _on_keep_click(event) -> None:
        chat.send("Cancel proposal dismissed — order left unchanged.", user="ClaudIA", respond=False)
        cancel_btn.disabled = True
        keep_btn.disabled = True

    cancel_btn.on_click(_on_cancel_click)
    keep_btn.on_click(_on_keep_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(cancel_btn, keep_btn)),
        user="ClaudIA — Cancel Proposal",
        respond=False,
    )


async def render_modify_proposal(
    chat,
    proposal: dict,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Render a modify proposal as a Panel chat message with modify/discard buttons."""
    summary_pane = pn.pane.Markdown(_format_modify_summary(proposal))
    modify_btn = pn.widgets.Button(name="Modify this order", button_type="success")
    discard_btn = pn.widgets.Button(name="Discard", button_type="light")
    send_status = _make_send_status(chat)

    async def _on_modify_click(event) -> None:
        try:
            await _execute_modify_order_core(proposal, send_status, session_id, store)
        finally:
            modify_btn.disabled = True
            discard_btn.disabled = True

    async def _on_discard_click(event) -> None:
        chat.send("Modify proposal discarded — order left unchanged.", user="ClaudIA", respond=False)
        modify_btn.disabled = True
        discard_btn.disabled = True

    modify_btn.on_click(_on_modify_click)
    discard_btn.on_click(_on_discard_click)

    chat.send(
        pn.Column(summary_pane, pn.Row(modify_btn, discard_btn)),
        user="ClaudIA — Modify Proposal",
        respond=False,
    )
