"""Panel-side MessageSink implementation.

Phase 2 scope: send_message and tool_step are real; order/cancel/modify proposal
rendering is a plain, honest placeholder until Phase 3 ports order_flow.py's
message-with-buttons pattern to Panel (claudia/panel_order_flow.py).
"""

from __future__ import annotations

import json
from typing import Any


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

    def __init__(self, chat, session_id: str) -> None:
        self._chat = chat
        self._session_id = session_id

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
        self._chat.send(
            f"Order staging is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )

    async def send_cancel_proposal(self, proposal: dict) -> None:
        self._chat.send(
            f"Order cancellation is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )

    async def send_modify_proposal(self, proposal: dict) -> None:
        self._chat.send(
            f"Order modification is not yet available in this preview build.\n\n"
            f"Proposed: `{json.dumps(proposal)}`",
            user="System",
            respond=False,
        )
