"""
Core Anthropic SDK streaming agent loop for ClaudIA.

Builds the system prompt, loads conversation history, streams Claude responses
with multi-turn tool use, and persists every interaction to ConversationStore.

Order proposals: ClaudIA embeds a fenced ```order-proposal block in its response
when it wants to suggest a staged trade. agent.py strips the block from the
displayed text and passes the parsed JSON to order_flow for button rendering.

Anthropic SDK: anthropic.AsyncAnthropic with client.messages.stream() for
server-sent event streaming. Tool use follows the multi-turn loop pattern:
stream → collect tool_use blocks → execute tools → append tool_result → stream again.

Source (Messages API streaming): https://docs.anthropic.com/en/api/messages-streaming
Source (Tool use): https://docs.anthropic.com/en/docs/build-with-claude/tool-use
Source (Models): https://docs.anthropic.com/en/docs/about-claude/models
  Current default: claude-opus-4-8 (1M token context, $5/$25 per MTok input/output)
  Latest most-capable: claude-fable-5 ($10/$50 per MTok)
  Balance of speed/intelligence: claude-sonnet-4-6 ($3/$15 per MTok)
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import chainlit as cl
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore
    from claudia.context_loader import ContextLoader
    from claudia.tradingview import TradingViewBridge
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

## DATA INTEGRITY (non-overridable)

Every specific data point you present — prices, balances, positions, P&L, account values,
watchlist names, trade history, order status, contract IDs, or any other numerical or named
fact — MUST originate from one of the following guaranteed sources:
  1. A tool call result returned in this conversation.
  2. Content explicitly provided by the user in this conversation.
  3. The market calendar injected into this system prompt (exchange schedules and holidays only).

You MUST NOT invent, guess, estimate, or carry over any data point that was not returned by
a tool call or stated by the user. This includes reformatting, "filling in" missing fields,
or presenting partial tool results as complete.

If a tool call returns no data or an error: say so explicitly and stop. Do not substitute
remembered or plausible-sounding values.

If you are uncertain whether a data point came from a tool call or from your training: treat
it as invented and do not state it. Call the relevant tool instead.

## ORDER PROPOSAL FORMAT

When suggesting a specific trade, include exactly one fenced block using this format:

```order-proposal
{
  "symbol": "TICKER",
  "action": "BUY" or "SELL",
  "quantity": <integer>,
  "order_type": "MKT" or "LMT" or "STP" or "STOP_LIMIT",
  "limit_price": <float or null>,
  "stop_price": <float or null>,
  "tif": "DAY" or "GTC" or "IOC" or "OPG",
  "sec_type": "STK" or "FUT" or "OPT" or "FOP" or "CASH",
  "conid": <integer or null>,
  "reason": "<one-line rationale>"
}
```

The block will be rendered as a confirmation button for the user to review and stage.
Do NOT include multiple order proposals in a single message.

## ORDER PARAMETER IMMUTABILITY — NON-OVERRIDABLE

If the user specifies any order parameter (symbol, action, quantity, price, order type, TIF),
you MUST use EXACTLY that value in the proposal block. No rounding, no substitution, no
"helpful" adjustments.

If you believe a parameter is risky or unusual (e.g. limit far from market), you may say so
in your explanation text — but the proposal block must still contain the user's exact value.
The user decides. You propose, they confirm.

You MUST NEVER change a user-specified order parameter without the user explicitly approving
the new value in a follow-up message. This includes price, quantity, symbol, order type, and TIF.

## ORDER CANCEL / MODIFY FORMAT

To cancel an existing order, include exactly one fenced block:

```order-cancel-proposal
{
  "order_id": "<string, from a real get_live_orders/get_order_status/diagnose_orders call>",
  "symbol": "TICKER",
  "action": "BUY" or "SELL",
  "quantity": <integer>,
  "order_type": "MKT" or "LMT" or "STP" or "STOP_LIMIT",
  "limit_price": <float or null>,
  "stop_price": <float or null>,
  "tif": "DAY" or "GTC" or "IOC" or "OPG",
  "reason": "<one-line rationale>"
}
```

To modify an existing order, include exactly one fenced block:

```order-modify-proposal
{
  "order_id": "<string, from a real get_order_status call>",
  "conid": <integer, from the same get_order_status call — required, no fallback resolution>,
  "symbol": "TICKER",
  "action": "BUY" or "SELL",
  "quantity": <integer>,
  "order_type": "MKT" or "LMT" or "STP" or "STOP_LIMIT",
  "limit_price": <float or null>,
  "stop_price": <float or null>,
  "tif": "DAY" or "GTC" or "IOC" or "OPG",
  "sec_type": "STK" or "FUT" or "OPT" or "FOP" or "CASH",
  "reason": "<one-line rationale>",
  "_changed_fields": ["<field name(s) actually being changed>"],
  "_previous_values": {"<field name>": "<previous value>"}
}
```

Include at most ONE proposal block total per message — order-proposal, order-cancel-proposal,
or order-modify-proposal. Never combine two in the same response.

## ORDER CANCEL / MODIFY RULES — NON-OVERRIDABLE

- `order_id` MUST come from a real `get_live_orders`, `get_order_status`, or `diagnose_orders`
  tool call made earlier in THIS conversation. Never invent, guess, or reuse an order_id from
  memory or a previous session.
- Before proposing a cancel or modify, check the order's origin and editability:
  - `get_live_orders` already documents that orders placed via IBKR mobile or TWS cannot be
    modified or cancelled through the API — if the order's origin is external, say so and
    stop; do not propose a cancel/modify for it.
  - `get_order_status` returns `order_not_editable` and `cannot_cancel_order` boolean fields.
    If the relevant flag is true, tell the user the order cannot be changed/cancelled and why
    — do not propose the action anyway.
- A modify proposal REQUIRES calling `get_order_status(order_id)` first — it returns the
  contract id (`conid`) and full current field set that `get_live_orders` does not expose.
  Never build an order-modify-proposal from `get_live_orders` data alone.

## MODIFY PARAMETER IMMUTABILITY — NON-OVERRIDABLE

Every field in an order-modify-proposal that the user did NOT ask to change must be copied
byte-for-byte (the exact value) from the latest `get_order_status` result for that order. Only
the specific field(s) the user asked to change may differ. List every changed field in
`_changed_fields` and its prior value in `_previous_values` so the confirmation dialog can show
a clear before/after diff.

You MUST NEVER change an unrequested order field when building a modify proposal. This mirrors
the ORDER PARAMETER IMMUTABILITY rule above — the user decides, you propose, they confirm.
"""


def _make_block_stripper(tag: str):
    """Build a function that extracts and removes a fenced ```{tag} block from text.

    Shared by order-proposal, order-cancel-proposal, and order-modify-proposal — each is
    a single JSON block the LLM emits in place of calling an order-execution tool directly
    (see Hard Rule 1 in CLAUDE.md); a human later approves it via a physical button click.
    """
    pattern = re.compile(rf"```{re.escape(tag)}\s*\n(.*?)\n```", re.DOTALL)

    def _strip(text: str) -> tuple[str, dict | None]:
        m = pattern.search(text)
        if not m:
            return text, None
        try:
            proposal = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning("Malformed %s JSON in response", tag)
            return text, None
        clean = pattern.sub("", text).strip()
        return clean, proposal

    return _strip


_strip_order_proposal = _make_block_stripper("order-proposal")
_strip_order_cancel_proposal = _make_block_stripper("order-cancel-proposal")
_strip_order_modify_proposal = _make_block_stripper("order-modify-proposal")


_LOCAL_TOOLS: list[dict] = [
    {
        "name": "list_doc_versions",
        "description": (
            "List all registered context/principles document versions with their dates. "
            "Use this before calling get_doc_version to see which versions exist."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_doc_version",
        "description": (
            "Retrieve the full context.md and principles.md content for a specific document version. "
            "Use to check whether a past discussion happened under different rules than today's."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "version": {
                    "type": "string",
                    "description": "Version label, e.g. 'v1'. Use list_doc_versions first.",
                }
            },
            "required": ["version"],
        },
    },
    {
        "name": "search_past_conversations",
        "description": (
            "Full-text search across all past conversation history (all sessions). "
            "Use when the user asks what was discussed, analyzed, or considered in previous sessions. "
            "Returns relevant message excerpts with session context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or phrase to search for, e.g. 'AAPL support level' or 'security controls'.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_web_page",
        "description": (
            "Fetch and read any public web page — documentation, financial news, research, broker pages. "
            "Returns the page content as readable text. Use when the user asks you to look at a URL, "
            "read documentation, or research something online. "
            "Does not work on pages that require JavaScript rendering or login."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to fetch, e.g. 'https://example.com/page'.",
                },
                "extract": {
                    "type": "string",
                    "description": "Optional: specific section or information to focus on.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_live_pnl",
        "description": (
            "Get the latest account P&L snapshot (daily P&L, unrealized P&L, "
            "net liquidity, excess liquidity, market value), automatically refreshed "
            "each time a trade executes (any origin — mobile, TWS, web, API). "
            "Use when the user asks for current/live/real-time P&L. "
            "For historical performance analysis use get_analytics instead."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _LOCAL_TOOLS)


def _with_cache_marker(tools: list[dict]) -> list[dict]:
    """Return tools with a prompt-cache breakpoint on the last entry.

    The last dict is copied, never mutated — the inputs are shared module-level
    constants (_LOCAL_TOOLS, ibkr_core_mcp TOOL_DEFINITIONS).
    Marking the last tool caches the entire tools array (prefix hierarchy:
    tools -> system -> messages).
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    if not tools:
        return tools
    marked = list(tools)
    marked[-1] = {**marked[-1], "cache_control": {"type": "ephemeral"}}
    return marked


def _build_version_note(doc_version: str | None, store: "ConversationStore | None") -> str:
    """Return the active-version header line for the system prompt, or "" if no version."""
    if not doc_version:
        return ""
    versions = store.list_doc_versions() if store else []
    current_idx = next((i for i, v in enumerate(versions) if v["version"] == doc_version), -1)
    if current_idx > 0:
        prev = versions[current_idx - 1]
        prev_note = f", previous: {prev['version']} (until {prev['created_at'][:10]})"
    else:
        prev_note = ""
    return f"**Active document version: {doc_version}{prev_note}**\n\n"


def _build_system_prompt(
    context_prompt: str,
    doc_version: str | None = None,
    store: "ConversationStore | None" = None,
    trade_context: str | None = None,
) -> str:
    """Assemble the full system prompt: version note + context + trade context + safety block.

    _SAFETY_BLOCK is always appended last and unconditionally — it cannot be
    suppressed or overridden by content in the earlier sections.
    """
    trade_block = f"\n\n{trade_context}" if trade_context else ""
    return _build_version_note(doc_version, store) + context_prompt + trade_block + _SAFETY_BLOCK


def _system_blocks(system_prompt: str) -> list[dict]:
    """Wrap the system prompt in block form with a prompt-cache breakpoint.

    The marker on the last (only) system block caches tools + system together.
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _log_cache_usage(usage) -> None:
    """Log prompt-cache health from a message_start usage object.

    created > 0  -> prefix written this call (1.25x input price)
    read > 0     -> prefix served from cache (0.1x input price)
    both zero    -> caching silently failed (below-minimum prefix, misplaced
                    marker, or a >20-block turn outside the lookback window)
                    -- warn so it is caught as tools evolve.
    """
    created = getattr(usage, "cache_creation_input_tokens", None) or 0
    read = getattr(usage, "cache_read_input_tokens", None) or 0
    uncached = getattr(usage, "input_tokens", None) or 0
    log.info("prompt cache: created=%d read=%d uncached=%d", created, read, uncached)
    if created == 0 and read == 0:
        log.warning(
            "prompt cache inactive (created=0, read=0) — check cache_control placement"
        )


def _history_to_messages(history: list[dict]) -> list[MessageParam]:
    """Convert ConversationStore rows to Anthropic message dicts."""
    messages: list[MessageParam] = []
    for row in history:
        role = row["role"]
        if role == "user":
            messages.append({"role": "user", "content": row["content"] or ""})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": row["content"] or ""})
        # tool rows are intentionally skipped: the DB does not store the
        # tool_use_id UUIDs assigned by Anthropic, and the intermediate
        # assistant messages containing the matching tool_use blocks are
        # not persisted either. Injecting orphaned tool_result blocks causes
        # Anthropic API 400 errors. The assistant's text response already
        # captures what each tool returned.
    return messages


def _with_history_cache_marker(messages: list) -> list:
    """Return a copy of messages with a prompt-cache breakpoint on the final content block.

    Third breakpoint (after tools and system): caches the conversation prefix so
    each tool-loop call reads the prior prefix at 0.1x and writes only the newly
    added blocks at 1.25x. Copies the last message and its block list — the
    caller's list is the loop's working state and must never carry markers
    between iterations.

    Caveat (documented in docs/prompt-caching-upgrade.md): a single turn adding
    more than 20 content blocks (10+ parallel tool calls) falls outside the
    20-block lookback window and re-writes instead of reading — visible as
    created>0/read=0 in the _log_cache_usage line.
    Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last["content"]
    if isinstance(content, str):
        if not content:
            return messages  # "empty text blocks cannot be cached" (official docs)
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = list(content)
        if not blocks:
            return messages
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    return messages[:-1] + [last]


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
        tv_bridge: "TradingViewBridge | None" = None,
        doc_version: str | None = None,
        trade_context: str | None = None,
    ) -> None:
        """Initialise the agent for one Chainlit session.

        extra_tools: TradingView tool definitions from TradingViewBridge.get_tools();
            merged into the Anthropic tools= list alongside toolkit's 42 IBKR tools.
        trade_context: optional market-calendar string injected into the system prompt
            at session start (built by ibkr_core_mcp.SQLiteStore.get_market_calendar_context).
        """
        self._toolkit = toolkit
        self._store = store
        self._loader = context_loader
        self._session_id = session_id
        self._model = model
        self._extra_tools = extra_tools or []
        self._tv_bridge = tv_bridge
        self._doc_version = doc_version
        self._trade_context = trade_context
        self._tv_tool_names: set[str] = {t["name"] for t in self._extra_tools}
        self._client = AsyncAnthropic()
        self._system_blocks_cache: list[dict] | None = None
        self._system_reload_seen: int = -1

    def set_tv_bridge(self, bridge: "TradingViewBridge", tools: list[dict]) -> None:
        """Update TradingView connection mid-session (called by on_launch_tradingview)."""
        self._tv_bridge = bridge
        self._extra_tools = tools
        self._tv_tool_names = {t["name"] for t in tools}

    def _get_system_blocks(self) -> list[dict]:
        """Return the cached system-prompt blocks, built at most once per session.

        Version note, documents, and market calendar are resolved when ClaudIA
        loads — not on each prompt. The only rebuild trigger is the loader's
        reload_count (event-driven hot-reload); steady-state per-message cost is
        one int comparison. Byte-identical blocks across calls also guarantee
        prompt-cache stability for the system segment.
        """
        count = self._loader.reload_count
        if self._system_blocks_cache is None or count != self._system_reload_seen:
            prompt = _build_system_prompt(
                self._loader.load_system_prompt(), self._doc_version, self._store,
                self._trade_context,
            )
            self._system_blocks_cache = _system_blocks(prompt)
            self._system_reload_seen = count
        return self._system_blocks_cache

    @property
    def _all_tools(self) -> list[dict]:
        return _with_cache_marker(self._toolkit.tools + self._extra_tools + _LOCAL_TOOLS)

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

        system_blocks = self._get_system_blocks()

        # Multi-turn tool loop
        full_response_text = ""
        order_proposal: dict | None = None

        while True:
            response_text = ""
            tool_calls: list[dict] = []
            stop_reason: str | None = None

            async with self._client.messages.stream(
                model=self._model,
                max_tokens=4096,
                system=system_blocks,
                messages=_with_history_cache_marker(messages),
                tools=self._all_tools,
            ) as stream:
                async for event in stream:
                    etype = event.type

                    if etype == "message_start":
                        _log_cache_usage(event.message.usage)

                    elif etype == "content_block_start":
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
                        elif delta.type == "input_json_delta" and tool_calls:
                            tool_calls[-1]["input_json"] += delta.partial_json

                    elif etype == "message_delta":
                        stop_reason = event.delta.stop_reason

            # --- Stream complete ---

            if stop_reason == "max_tokens":
                await cl.Message(
                    content="_⚠ Response truncated — token limit reached. "
                            "Ask me to continue if the answer is incomplete._",
                    author="System",
                ).send()

            # Append assistant turn to the running message list
            assistant_content: list = []
            if response_text:
                assistant_content.append({"type": "text", "text": response_text})
            for tc in tool_calls:
                try:
                    inp = json.loads(tc["input_json"]) if tc["input_json"] else {}
                except json.JSONDecodeError as exc:
                    log.warning("Tool %r: could not parse input JSON (%s) — sending empty input", tc["name"], exc)
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
                    if tc["name"] in _LOCAL_TOOL_NAMES:
                        result_text = self._handle_local_tool(tc["name"], tc["input"])
                    elif tc["name"] in self._tv_tool_names and self._tv_bridge is not None:
                        result_text = await self._tv_bridge.execute(tc["name"], tc["input"])
                    else:
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
        display_text, cancel_proposal = _strip_order_cancel_proposal(display_text)
        display_text, modify_proposal = _strip_order_modify_proposal(display_text)

        # Persist final assistant message
        msg_id = self._store.add_message(
            self._session_id, "assistant", display_text
        )

        # Render text response
        if display_text:
            await cl.Message(content=display_text).send()

        # Render a proposal button if present — at most one type per message.
        # The system prompt instructs at most one proposal block per response; if the
        # LLM ever violates that, only order_proposal renders — log so it's not silent.
        proposal_count = sum(1 for p in (order_proposal, cancel_proposal, modify_proposal) if p)
        if proposal_count > 1:
            log.warning(
                "Multiple proposal blocks in one response (order=%s cancel=%s modify=%s) — "
                "only the highest-priority one is rendered/logged",
                bool(order_proposal), bool(cancel_proposal), bool(modify_proposal),
            )
        if order_proposal:
            from claudia.order_flow import render_order_proposal
            await render_order_proposal(order_proposal, session_id=self._session_id)
        elif cancel_proposal:
            from claudia.order_flow import render_cancel_proposal
            await render_cancel_proposal(cancel_proposal, session_id=self._session_id)
        elif modify_proposal:
            from claudia.order_flow import render_modify_proposal
            await render_modify_proposal(modify_proposal, session_id=self._session_id)

        # Log any user-directed trade proposal for future recall
        self._log_proposal(
            display_text, order_proposal, msg_id,
            cancel_proposal=cancel_proposal, modify_proposal=modify_proposal,
        )

    def _handle_local_tool(self, name: str, inputs: dict) -> str:
        """Dispatch the five locally-implemented tools and return a string result.

        Local tools (list_doc_versions, get_doc_version, search_past_conversations,
        fetch_web_page, get_live_pnl) are defined in TOOL_DEFINITIONS but executed here
        rather than via toolkit.execute(). They always return a string — never raise.
        """
        if name == "list_doc_versions":
            versions = self._store.list_doc_versions()
            if not versions:
                return "No document versions registered yet."
            lines = [f"- {v['version']}: registered {v['created_at'][:10]}" for v in versions]
            return "Document versions:\n" + "\n".join(lines)
        if name == "get_doc_version":
            version = inputs.get("version", "")
            data = self._store.get_doc_version(version)
            if data is None:
                available = [v["version"] for v in self._store.list_doc_versions()]
                return (
                    f"Version '{version}' not found. "
                    f"Available: {', '.join(available) or 'none'}."
                )
            return (
                f"## context.md ({data['version']}, as of {data['created_at'][:10]})\n\n"
                f"{data['context_text']}\n\n"
                f"## principles.md ({data['version']}, as of {data['created_at'][:10]})\n\n"
                f"{data['principles_text']}"
            )
        if name == "search_past_conversations":
            query = inputs.get("query", "").strip()
            if not query:
                return "No query provided."
            results = self._store.search_messages(query, max_results=5)
            if not results:
                return f"No past conversations found matching '{query}'."
            parts = []
            for r in results:
                role = r.get("role", "")
                snippet = r.get("snippet") or r.get("content") or ""
                created = (r.get("created_at") or "")[:10]
                parts.append(f"[{created}] {role}: {snippet[:300]}")
            return "\n\n---\n\n".join(parts)
        if name == "fetch_web_page":
            return self._fetch_web_page(inputs)
        if name == "get_live_pnl":
            return self._get_live_pnl()
        return f"Unknown local tool: {name}"

    def _get_live_pnl(self) -> str:
        """Format the latest live P&L snapshot recorded by ExecutionListener's
        execution-triggered background WebSocket subscription
        (claudia/execution_listener.py). Returns a friendly message if no
        snapshot has been recorded yet — never raises."""
        from claudia.execution_listener import format_pnl_snapshot
        latest = self._toolkit._store.get_latest_pnl()
        return format_pnl_snapshot(latest)

    @staticmethod
    def _validate_public_url(url: str) -> str | None:
        """SSRF guard: return an error string unless url is a public http/https URL.

        Prevents prompt-injection attacks from fetching localhost:5055 (IBKR gateway)
        or other internal services and leaking their responses to the LLM.
        Called on the initial URL AND on every redirect hop (finding S1, audit
        2026-06-25 H-1: a public URL that 302s to a private address is the same
        attack one hop removed).
        """
        import ipaddress
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"Blocked: only http/https URLs are supported (got {parsed.scheme!r})."
            host = (parsed.hostname or "").lower()
            if not host:
                return "Blocked: URL has no hostname."
            if host in ("localhost", "0.0.0.0") or host.startswith("127.") or host.startswith("169.254."):
                return "Blocked: cannot fetch from localhost or link-local addresses."
            try:
                addr = ipaddress.ip_address(host)
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return "Blocked: cannot fetch from private or reserved IP addresses."
            except ValueError:
                # Not a literal IP — resolve via DNS and re-check.
                # Catches decimal (2130706433) and hex (0x7f000001) encoded IPs that
                # bypass string-prefix checks but resolve to private addresses on Linux.
                import socket as _socket
                try:
                    resolved_ip = _socket.gethostbyname(host)
                    addr = ipaddress.ip_address(resolved_ip)
                    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                        return "Blocked: URL resolves to a private or reserved IP address."
                except _socket.gaierror:
                    pass  # unresolvable hostname — let requests handle the error
        except Exception as exc:
            return f"Invalid URL: {exc}"
        return None

    _MAX_REDIRECTS = 5

    def _fetch_web_page(self, inputs: dict) -> str:
        import html2text
        import urllib.parse
        import requests as _req
        url = inputs.get("url", "").strip()
        if not url:
            return "No URL provided."
        # Follow redirects manually so every hop passes the SSRF guard —
        # allow_redirects=True would let a public URL 302 to a private address
        # without re-validation (finding S1).
        resp = None
        for hop in range(self._MAX_REDIRECTS + 1):
            err = self._validate_public_url(url)
            if err:
                return err if hop == 0 else f"{err} (via redirect)"
            try:
                resp = _req.get(
                    url,
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; ClaudIA/1.0)"},
                    allow_redirects=False,
                )
            except Exception as exc:
                return f"Could not fetch {url}: {exc}"
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    break  # malformed redirect — treat as final response
                url = urllib.parse.urljoin(url, location)
                continue
            break
        else:
            return f"Blocked: too many redirects (>{self._MAX_REDIRECTS})."
        try:
            resp.raise_for_status()
        except Exception as exc:
            return f"Could not fetch {url}: {exc}"
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        text = h.handle(resp.text)
        # Trim to a reasonable size to avoid flooding context
        if len(text) > 12000:
            text = text[:12000] + "\n\n[… content truncated at 12,000 chars]"
        extract = inputs.get("extract", "").strip()
        if extract:
            return f"[Fetched: {url}]\nFocus: {extract}\n\n{text}"
        return f"[Fetched: {url}]\n\n{text}"

    def _log_proposal(
        self,
        text: str,
        order_proposal: dict | None,
        msg_id: int,
        cancel_proposal: dict | None = None,
        modify_proposal: dict | None = None,
    ) -> None:
        """Log a user-directed trade proposal to the proposals table for future recall.

        ClaudIA does not decide to trade — it surfaces a proposal when directed by the user.
        The user decides at the button → Touch ID → confirmation dialog. This records that
        a proposal was *surfaced*, not that a decision was made — so an unclicked cancel or
        modify proposal is still recorded here, same as an unclicked order-proposal.
        Priority mirrors handle_message's render order: order_proposal wins if more than
        one type is somehow present in the same response.
        """
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
        elif cancel_proposal:
            symbol = cancel_proposal.get("symbol", "")
            order_id = cancel_proposal.get("order_id", "")
            reason = cancel_proposal.get("reason", "")
            self._store.add_decision(
                session_id=self._session_id,
                decision_type="trade_cancel_proposed",
                summary_text=f"CANCEL order {order_id} ({symbol}): {reason}",
                symbol=symbol,
                message_id=msg_id,
                metadata={"order": cancel_proposal},
            )
        elif modify_proposal:
            symbol = modify_proposal.get("symbol", "")
            order_id = modify_proposal.get("order_id", "")
            reason = modify_proposal.get("reason", "")
            self._store.add_decision(
                session_id=self._session_id,
                decision_type="trade_modify_proposed",
                summary_text=f"MODIFY order {order_id} ({symbol}): {reason}",
                symbol=symbol,
                message_id=msg_id,
                metadata={"order": modify_proposal},
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
