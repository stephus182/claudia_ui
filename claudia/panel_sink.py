"""Panel-side MessageSink implementation.

send_message and tool_step are real; order/cancel/modify proposal rendering delegates
to claudia/panel_order_flow.py, which ports order_flow.py's message-with-buttons
pattern to Panel on top of the framework-agnostic _execute_*_core functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import panel as pn

from claudia.panel_markdown import escape_markup

if TYPE_CHECKING:
    from collections.abc import Callable

    from claudia.conversation_store import ConversationStore
    from claudia.tradingview import TradingViewBridge


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
        """Wrap a live ChatStep.

        Args:
            chat_step: The step already sent into the chat feed; this handle mutates it.

        `_input_set` tracks whether an input line was written, so `output` knows whether to
        emit a blank-line separator (consecutive `.stream()` calls concatenate with none).
        """
        self._chat_step = chat_step
        self._input = ""
        self._output = ""
        self._input_set = False

    @property
    def input(self) -> str:
        """The tool arguments as last written."""
        return self._input

    @input.setter
    def input(self, value: str) -> None:
        """Record the tool arguments and stream them into the live ChatStep.

        Side-effecting by design: assignment writes to the UI. `value` is LLM-authored, so
        it is escaped before streaming — ChatStep builds its own Markdown panes and has no
        `renderers` parameter, so the ChatInterface-level safe_markdown hook cannot reach
        here (security-audit-2026-07-25.md, H-1).
        """
        self._input = value
        self._chat_step.stream(f"Input: `{escape_markup(value)}`")
        self._input_set = True

    @property
    def output(self) -> str:
        """The tool result as last written."""
        return self._output

    @output.setter
    def output(self, value: str) -> None:
        """Record the tool result and stream it into the live ChatStep.

        Side-effecting by design: assignment writes to the UI.

        `value` is a **raw tool result** and the single most exposed sink in the UI — a page
        fetched by `fetch_web_page`/`firecrawl_*` reaches here verbatim, so an attacker who
        controls a web page ClaudIA visits needs no LLM cooperation to inject. It is escaped
        for the same reason as `input` above: ChatStep panes are outside the reach of the
        ChatInterface `renderers` hook (security-audit-2026-07-25.md, H-1).
        """
        self._output = value
        # Consecutive string .stream() calls concatenate into one Markdown pane with
        # no separator (verified live) — supply our own blank-line break.
        sep = "\n\n" if self._input_set else ""
        self._chat_step.stream(f"{sep}Output: {escape_markup(value)}")

    async def __aenter__(self) -> _PanelToolStepHandle:
        """Open the step. Async wrapper over ChatStep's synchronous `__enter__`."""
        self._chat_step.__enter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Close the step, letting ChatStep set success/failure status and format errors.

        Returns whatever ChatStep returns, so exception suppression stays ChatStep's
        decision rather than something this wrapper silently imposes.
        """
        # ChatStep.__exit__ is unannotated upstream (panel/chat/step.py) so mypy sees
        # its return as Any regardless of how chat_step above is typed — confirmed by
        # isolated probe, 2026-07-22. Its source always returns an actual bool on every
        # path (explicit `return False`, or falls through to `return True`), so this
        # cast is a correctness statement, not a suppression.
        return bool(self._chat_step.__exit__(exc_type, exc, tb))


class PanelMessageSink:
    """MessageSink backed by a live pn.chat.ChatInterface instance for one session.

    Four methods import from `claudia.panel_app` / `claudia.panel_pinescript` *inside* the
    function body rather than at module scope. That is deliberate: both of those modules
    import this one, so a top-level import would be a cycle. It also keeps `agent.py`
    untouched — PineScript detection lives here, in the sink, not in the agent loop.
    """

    def __init__(
        self,
        chat,
        session_id: str,
        store: ConversationStore | None = None,
        tv_bridge_getter: Callable[[], TradingViewBridge | None] | None = None,
    ) -> None:
        """Bind a sink to one browser session's chat feed.

        Args:
            chat: The session's `pn.chat.ChatInterface`.
            session_id: Session id, used to attribute logged order decisions.
            store: Conversation store for decision logging. Optional — without it, button
                clicks still execute but are not recorded.
            tv_bridge_getter: Callable resolving the TradingView bridge at *click* time
                rather than at construction, so a TradingView launch that happens after a
                ```pine message has already rendered is still picked up. Default None means
                inject reports "not connected".
        """
        self._chat = chat
        self._session_id = session_id
        self._store = store
        # Resolved lazily at click time (default None → inject shows not-connected)
        # so a TradingView launch after a ```pine message rendered is still picked up.
        self._tv_bridge_getter = tv_bridge_getter

    async def send_message(self, text: str) -> None:
        """Send assistant text, appending PineScript buttons when the text contains any.

        Non-obvious side effect worth knowing at the call site: if `text` contains one or
        more ```pine blocks, Copy/Inject button rows are rendered beneath the message.
        Callers do not opt in — detection happens here.
        """
        self._chat.send(text, user="ClaudIA", respond=False)
        # Auto-detect ```pine blocks and drop Copy/Inject buttons beneath them. Deferred
        # import (mirrors the order-proposal methods below) — no panel_app↔panel_sink cycle,
        # and agent.py stays untouched (detection lives in the sink path). See
        # claudia/panel_pinescript.py.
        from claudia.panel_pinescript import extract_pine_blocks, render_pinescript_blocks
        if extract_pine_blocks(text):
            await render_pinescript_blocks(
                self._chat, text, self._tv_bridge_getter,
                store=self._store, session_id=self._session_id,
            )

    def tool_step(self, name: str) -> _PanelToolStepHandle:
        """Send a tool-call step into the feed and return a handle for its input/output.

        Args:
            name: Tool name, shown in the step title.
        """
        chat_step = pn.chat.ChatStep(
            default_title=f"`{name}`",
            running_title=f"Running `{name}`…",
            success_title=f"`{name}`",
            # failed_title deliberately left unset — see _PanelToolStepHandle's docstring.
        )
        self._chat.send(chat_step, user="System", respond=False)
        return _PanelToolStepHandle(chat_step)

    async def send_max_tokens_warning(self) -> None:
        """Tell the user the response was cut off at the model's output-token limit."""
        self._chat.send(
            "⚠ Response truncated — token limit reached. "
            "Ask me to continue if the answer is incomplete.",
            user="System",
            respond=False,
        )

    async def send_order_proposal(self, proposal: dict) -> None:
        """Render the staging button for a new order. Places nothing — see MessageSink."""
        from claudia.panel_order_flow import render_order_proposal
        await render_order_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        """Render the cancel button for a live order. Cancels nothing — see MessageSink."""
        from claudia.panel_order_flow import render_cancel_proposal
        await render_cancel_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)

    async def send_modify_proposal(self, proposal: dict) -> None:
        """Render the modify button for a live order. Modifies nothing — see MessageSink."""
        from claudia.panel_order_flow import render_modify_proposal
        await render_modify_proposal(self._chat, proposal, session_id=self._session_id, store=self._store)
