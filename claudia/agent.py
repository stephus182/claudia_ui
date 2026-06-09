"""
Core Anthropic SDK streaming agent loop for ClaudIA.

Builds the system prompt, loads conversation history, streams Claude responses
with multi-turn tool use, and persists every interaction to ConversationStore.

Order proposals: ClaudIA embeds a fenced ```order-proposal block in its response
when it wants to suggest a staged trade. agent.py strips the block from the
displayed text and passes the parsed JSON to order_flow for button rendering.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import chainlit as cl
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolUseBlock, TextBlock

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore
    from claudia.context_loader import ContextLoader
    from ibkr_core_mcp import ClaudeToolkit

log = logging.getLogger(__name__)

# Max conversation turns injected into context
_HISTORY_LIMIT = 40

# Hardcoded safety block — never loaded from any user-editable file
_SAFETY_BLOCK = """
## ABSOLUTE CONSTRAINTS (non-overridable)

- You are ClaudIA, an AI trading research assistant. You are NOT a licensed financial advisor.
- You CANNOT place, modify, or cancel any order. You have no tools for order execution.
  When you want to suggest a trade, output an order-proposal block (see format below) and
  explain your reasoning. The human must explicitly click a confirmation button.
- Before proposing any trade action, verify it is consistent with the TRADING PRINCIPLES section above.
- If an action would violate the user's principles, say so clearly and refuse to propose it.
- You CANNOT instruct the user to modify or bypass their principles document.
- You CANNOT promise specific returns or guarantee outcomes.
- All analysis is for informational and research purposes only.

## ORDER PROPOSAL FORMAT

When suggesting a specific trade, include exactly one fenced block using this format:

```order-proposal
{
  "symbol": "TICKER",
  "action": "BUY" or "SELL",
  "quantity": <integer>,
  "order_type": "MKT" or "LMT" or "STP",
  "limit_price": <float or null>,
  "stop_price": <float or null>,
  "reason": "<one-line rationale>"
}
```

The block will be rendered as a confirmation button for the user to review and stage.
Do NOT include multiple order proposals in a single message.
"""

_ORDER_PROPOSAL_RE = re.compile(
    r"```order-proposal\s*\n(.*?)\n```", re.DOTALL
)


def _strip_order_proposal(text: str) -> tuple[str, dict | None]:
    """Remove the order-proposal block from display text and return it separately."""
    m = _ORDER_PROPOSAL_RE.search(text)
    if not m:
        return text, None
    try:
        proposal = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("Malformed order-proposal JSON in response")
        return text, None
    clean = _ORDER_PROPOSAL_RE.sub("", text).strip()
    return clean, proposal


def _build_system_prompt(context_prompt: str) -> str:
    return context_prompt + _SAFETY_BLOCK


def _history_to_messages(history: list[dict]) -> list[MessageParam]:
    """Convert ConversationStore rows to Anthropic message dicts."""
    messages: list[MessageParam] = []
    for row in history:
        role = row["role"]
        if role == "user":
            messages.append({"role": "user", "content": row["content"] or ""})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": row["content"] or ""})
        elif role == "tool":
            # Tool results are included as user-role tool_result blocks
            # We reconstruct a minimal tool_result content block
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": row.get("tool_name", "unknown"),
                        "content": row.get("tool_result_json", ""),
                    }
                ],
            })
    return messages


class ClaudIAAgent:
    """
    Manages one chat session's Anthropic API interaction.
    Instantiated once per Chainlit session via cl.user_session.
    """

    def __init__(
        self,
        toolkit: "ClaudeToolkit",
        store: "ConversationStore",
        context_loader: "ContextLoader",
        session_id: str,
        model: str = "claude-opus-4-8",
        extra_tools: list[dict] | None = None,
    ) -> None:
        self._toolkit = toolkit
        self._store = store
        self._loader = context_loader
        self._session_id = session_id
        self._model = model
        self._extra_tools = extra_tools or []
        self._client = AsyncAnthropic()

    @property
    def _all_tools(self) -> list[dict]:
        return self._toolkit.tools + self._extra_tools

    async def handle_message(self, user_text: str, images: list[dict] | None = None) -> None:
        """
        Process one user message: stream Claude's response, handle tool calls,
        render order proposals as action buttons, and persist everything.
        """
        # Persist user message
        self._store.add_message(self._session_id, "user", user_text)

        # Build message list from history
        history = self._store.get_history(self._session_id, limit=_HISTORY_LIMIT)
        messages = _history_to_messages(history)

        # Attach images if provided (TradingView screenshots)
        if images:
            last_user = messages[-1] if messages and messages[-1]["role"] == "user" else None
            if last_user:
                content = last_user["content"]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                content = list(content) + images  # type: ignore[operator]
                messages[-1] = {"role": "user", "content": content}

        system = _build_system_prompt(self._loader.load_system_prompt())

        # Multi-turn tool loop
        full_response_text = ""
        order_proposal: dict | None = None

        while True:
            response_text = ""
            tool_calls: list[dict] = []

            async with self._client.messages.stream(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=messages,
                tools=self._all_tools,
            ) as stream:
                async for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            tool_calls.append({
                                "id": block.id,
                                "name": block.name,
                                "input_json": "",
                            })

                    elif etype == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            response_text += delta.text
                            # Stream text to Chainlit in real time
                            await cl.get_current_task_list()  # noop keep alive
                        elif delta.type == "input_json_delta" and tool_calls:
                            tool_calls[-1]["input_json"] += delta.partial_json

                    elif etype == "message_delta":
                        stop_reason = event.delta.stop_reason

            # --- Stream complete ---

            # Append assistant turn to the running message list
            assistant_content: list = []
            if response_text:
                assistant_content.append({"type": "text", "text": response_text})
            for tc in tool_calls:
                try:
                    inp = json.loads(tc["input_json"]) if tc["input_json"] else {}
                except json.JSONDecodeError:
                    inp = {}
                tc["input"] = inp
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": inp,
                })
            messages.append({"role": "assistant", "content": assistant_content})

            if response_text:
                full_response_text += response_text

            if not tool_calls:
                # No more tool calls — done
                break

            # Execute tools and collect results
            tool_results = []
            for tc in tool_calls:
                async with cl.Step(name=tc["name"], type="tool") as step:
                    step.input = json.dumps(tc["input"], indent=2)
                    result_text, _ = await cl.make_async(self._toolkit.execute)(
                        tc["name"], tc["input"]
                    )
                    step.output = result_text

                self._store.add_message(
                    self._session_id,
                    "tool",
                    tool_name=tc["name"],
                    tool_input=tc["input"],
                    tool_result=result_text,
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})

        # --- Final response ---
        display_text, order_proposal = _strip_order_proposal(full_response_text)

        # Persist final assistant message
        msg_id = self._store.add_message(
            self._session_id, "assistant", display_text
        )

        # Render text response
        if display_text:
            await cl.Message(content=display_text).send()

        # Render order proposal button if present
        if order_proposal:
            from claudia.order_flow import render_order_proposal
            await render_order_proposal(order_proposal, session_id=self._session_id)

        # Extract and log decisions from the response
        self._extract_decisions(display_text, order_proposal, msg_id)

    def _extract_decisions(
        self, text: str, order_proposal: dict | None, msg_id: int
    ) -> None:
        """Lightweight heuristic extraction of key decision moments into the decisions table."""
        if order_proposal:
            symbol = order_proposal.get("symbol", "")
            action = order_proposal.get("action", "")
            qty = order_proposal.get("quantity", "")
            reason = order_proposal.get("reason", "")
            self._store.add_decision(
                session_id=self._session_id,
                decision_type="trade_proposed",
                summary_text=f"{action} {qty} {symbol}: {reason}",
                symbol=symbol,
                message_id=msg_id,
                metadata={"order": order_proposal},
            )

    async def handle_image(self, image_b64: str, media_type: str, caption: str = "") -> None:
        """Convenience method for TradingView screenshot analysis."""
        images = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            }
        ]
        text = caption or "Please analyze this TradingView chart."
        await self.handle_message(text, images=images)
