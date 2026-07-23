"""Panel-side MessageSink implementation.

send_message and tool_step are real; order/cancel/modify proposal rendering delegates
to claudia/panel_order_flow.py, which ports order_flow.py's message-with-buttons
pattern to Panel on top of the framework-agnostic _execute_*_core functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore


class _PanelToolStepHandle:
    """Posts a message when a tool call starts, updates it in place when it ends —
    the same message.object-reassignment technique Panel's own docs use for the
    order-staging button pattern (research doc, point 4), applied here to a status
    message instead of a button. Phase 4 replaces this with the dedicated Status
    component once issue #6291's chrome-level gap is resolved or hand-built.
    """

    def __init__(self, chat, name: str) -> None:
        self._chat = chat
        self._name = name
        self.input: str = ""
        self.output: str = ""
        self._message: Any = None

    async def __aenter__(self) -> _PanelToolStepHandle:
        self._message = self._chat.send(
            f"**Running:** `{self._name}`…", user="System", respond=False
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._message.object = (
            f"**Tool:** `{self._name}`\n\n"
            f"Input: `{self.input}`\n\n"
            f"Output: {self.output}"
        )
        return False


class PanelMessageSink:
    """MessageSink backed by a live pn.chat.ChatInterface instance for one session."""

    def __init__(self, chat, session_id: str, store: ConversationStore | None = None) -> None:
        self._chat = chat
        self._session_id = session_id
        self._store = store

    async def send_message(self, text: str) -> None:
        self._chat.send(text, user="ClaudIA", respond=False)

    def tool_step(self, name: str) -> _PanelToolStepHandle:
        return _PanelToolStepHandle(self._chat, name)

    async def send_max_tokens_warning(self) -> None:
        self._chat.send(
            "⚠ Response truncated — token limit reached. "
            "Ask me to continue if the answer is incomplete.",
            user="System",
            respond=False,
        )

    async def send_order_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_order_proposal
        await render_order_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_cancel_proposal
        await render_cancel_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_modify_proposal(self, proposal: dict) -> None:
        from claudia.panel_order_flow import render_modify_proposal
        await render_modify_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)
