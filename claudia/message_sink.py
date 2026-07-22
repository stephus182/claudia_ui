"""Message-sink abstraction decoupling ClaudIAAgent's core loop from any specific UI
framework.

ClaudIAAgent depends only on the MessageSink protocol below, not on chainlit or panel
directly — migrating the UI framework changes which concrete sink is constructed at
session start, not the safety-critical loop itself (streaming, tool routing, the
hardcoded safety block, order-proposal parsing). ChainlitMessageSink here preserves
today's exact Chainlit behavior; see claudia/panel_sink.py (built in a later task) for
the Panel counterpart.
"""

from __future__ import annotations

from typing import Protocol

import chainlit as cl


class ToolStepHandle(Protocol):
    """Mutable handle for one in-flight tool call's displayed input/output."""

    input: str
    output: str

    async def __aenter__(self) -> ToolStepHandle: ...
    async def __aexit__(self, exc_type, exc, tb) -> bool | None: ...


class MessageSink(Protocol):
    """Everything ClaudIAAgent needs from a UI to render one turn's output."""

    async def send_message(self, text: str) -> None:
        """Send a plain assistant-authored text message."""
        ...

    def tool_step(self, name: str) -> ToolStepHandle:
        """Return an async-context-manager tool-call indicator for tool `name`."""
        ...

    async def send_max_tokens_warning(self) -> None:
        """Notify the user a response was truncated at the token limit."""
        ...

    async def send_order_proposal(self, proposal: dict) -> None: ...
    async def send_cancel_proposal(self, proposal: dict) -> None: ...
    async def send_modify_proposal(self, proposal: dict) -> None: ...


class ChainlitMessageSink:
    """MessageSink backed by Chainlit — preserves today's exact UI behavior unchanged."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def send_message(self, text: str) -> None:
        await cl.Message(content=text).send()

    def tool_step(self, name: str):
        return cl.Step(name=name, type="tool")

    async def send_max_tokens_warning(self) -> None:
        await cl.Message(
            content="_⚠ Response truncated — token limit reached. "
                    "Ask me to continue if the answer is incomplete._",
            author="System",
        ).send()

    async def send_order_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_order_proposal
        await render_order_proposal(proposal, session_id=self._session_id)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_cancel_proposal
        await render_cancel_proposal(proposal, session_id=self._session_id)

    async def send_modify_proposal(self, proposal: dict) -> None:
        from claudia.order_flow import render_modify_proposal
        await render_modify_proposal(proposal, session_id=self._session_id)
