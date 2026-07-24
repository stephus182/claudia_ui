"""Message-sink abstraction decoupling ClaudIAAgent's core loop from any specific UI
framework.

ClaudIAAgent depends only on the MessageSink protocol below, not on the UI framework
directly — the concrete sink constructed at session start is the only thing that knows
the framework, not the safety-critical loop itself (streaming, tool routing, the
hardcoded safety block, order-proposal parsing). The live implementation is
claudia/panel_sink.py's PanelMessageSink, which duck-types this protocol.
"""

from __future__ import annotations

from typing import Protocol


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
