"""Message-sink abstraction decoupling ClaudIAAgent's core loop from any specific UI
framework.

ClaudIAAgent depends only on the MessageSink protocol below, not on the UI framework
directly — the concrete sink constructed at session start is the only thing that knows
the framework, not the safety-critical loop itself (streaming, tool routing, the
hardcoded safety block, the `propose_*` tool handlers). The live implementation is
claudia/panel_sink.py's PanelMessageSink, which duck-types this protocol.
"""

from __future__ import annotations

from typing import Protocol


class ToolStepHandle(Protocol):
    """Mutable handle for one in-flight tool call's displayed input/output."""

    input: str
    output: str

    async def __aenter__(self) -> ToolStepHandle:
        """Open the step indicator and return the handle to write input/output on."""
        ...

    async def __aexit__(self, exc_type, exc, tb) -> bool | None:
        """Close the step, marking it success or failure from `exc_type`.

        The return value governs **exception suppression**: a truthy value swallows an
        exception raised inside the tool call. Implementations must return falsy unless they
        genuinely intend to hide the error from the agent loop.
        """
        ...


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

    # ── Order proposals ──────────────────────────────────────────────────────────────
    #
    # All three render a human-approval surface and MUST NOT execute anything. The agent
    # loop has no order-execution tools (CLAUDE.md Hard Rule 1); execution begins only when
    # a human clicks the rendered button, and even then only after Gate 1 (Touch ID) and
    # Gate 2 (the AppKit confirmation dialog). A sink that acted on a proposal directly
    # would defeat the single most important safety property in the system.
    #
    # `proposal` is a `propose_*` tool's input (claudia/proposal_tools.py): validated by
    # the API against a strict schema, then checked by agent.py for the few guarantees that
    # schema cannot express. A sink must still never repair or normalise its values, since
    # order parameters are immutable.

    async def send_order_proposal(self, proposal: dict) -> None:
        """Render a new-order proposal for human approval. Never places the order.

        Args:
            proposal: Keys per _SAFETY_BLOCK's "ORDER PROPOSAL FORMAT" — `symbol`, `action`,
                `quantity`, `order_type`, `limit_price`, `stop_price`, `tif`, `sec_type`,
                `conid`, `reason`.
        """
        ...

    async def send_cancel_proposal(self, proposal: dict) -> None:
        """Render an order-cancellation proposal for human approval. Never cancels.

        Args:
            proposal: `order_id` identifies the live order; the remaining fields
                (`symbol`, `action`, `quantity`, `order_type`, prices, `tif`) are
                display-only context.
        """
        ...

    async def send_modify_proposal(self, proposal: dict) -> None:
        """Render an order-modification proposal for human approval. Never modifies.

        Args:
            proposal: The **full replacement order** (IBKR requires the complete order, not
                a diff), keyed by `order_id`. `changes` drives the displayed diff — one
                `{"field", "previous_value"}` entry per field being changed. It is
                LLM-authored, so the diff is presentation, not a verified before/after.
        """
        ...
