"""Panel-side MessageSink implementation.

send_message and tool_step are real; order/cancel/modify proposal rendering delegates
to claudia/panel_order_flow.py, which ports order_flow.py's message-with-buttons
pattern to Panel on top of the framework-agnostic _execute_*_core functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import panel as pn

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore


class _PanelToolStepHandle:
    """Wraps a real pn.chat.ChatStep — Panel's built-in equivalent of Chainlit's
    cl.Step, shipped in panel==1.9.3 (confirmed live, 2026-07-22 — see Phase 4's
    header note for the verification). Translates the ToolStepHandle protocol's
    plain .input/.output attribute-setting into ChatStep's own .stream() calls, and
    delegates to ChatStep's own (synchronous) __enter__/__exit__ for status
    transitions and exception formatting.

    Deliberately does NOT set a custom failed_title on the underlying ChatStep —
    verified live that doing so suppresses ChatStep's own automatic
    exception-message streaming (the self.stream(exc_msg) call in its __exit__ is
    gated on failed_title being None). Leaving it unset gets a correct
    auto-generated title *and* the real error text in the body, for free.
    """

    def __init__(self, chat_step: pn.chat.ChatStep) -> None:
        self._chat_step = chat_step
        self._input = ""
        self._output = ""
        self._input_set = False

    @property
    def input(self) -> str:
        return self._input

    @input.setter
    def input(self, value: str) -> None:
        self._input = value
        self._chat_step.stream(f"Input: `{value}`")
        self._input_set = True

    @property
    def output(self) -> str:
        return self._output

    @output.setter
    def output(self, value: str) -> None:
        self._output = value
        # Consecutive string .stream() calls concatenate into one Markdown pane with
        # no separator (verified live) — supply our own blank-line break.
        sep = "\n\n" if self._input_set else ""
        self._chat_step.stream(f"{sep}Output: {value}")

    async def __aenter__(self) -> _PanelToolStepHandle:
        self._chat_step.__enter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # ChatStep.__exit__ is unannotated upstream (panel/chat/step.py) so mypy sees
        # its return as Any regardless of how chat_step above is typed — confirmed by
        # isolated probe, 2026-07-22. Its source always returns an actual bool on every
        # path (explicit `return False`, or falls through to `return True`), so this
        # cast is a correctness statement, not a suppression.
        return bool(self._chat_step.__exit__(exc_type, exc, tb))


class PanelMessageSink:
    """MessageSink backed by a live pn.chat.ChatInterface instance for one session."""

    def __init__(self, chat, session_id: str, store: ConversationStore | None = None) -> None:
        self._chat = chat
        self._session_id = session_id
        self._store = store

    async def send_message(self, text: str) -> None:
        self._chat.send(text, user="ClaudIA", respond=False)

    def tool_step(self, name: str) -> _PanelToolStepHandle:
        chat_step = pn.chat.ChatStep(
            default_title=f"`{name}`",
            running_title=f"Running `{name}`…",
            success_title=f"`{name}`",
            # failed_title deliberately left unset — see _PanelToolStepHandle's docstring.
        )
        self._chat.send(chat_step, user="System", respond=False)
        return _PanelToolStepHandle(chat_step)

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
