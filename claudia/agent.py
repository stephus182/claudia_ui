"""Core Anthropic SDK streaming agent loop for ClaudIA.

Builds the system prompt, loads conversation history, streams Claude responses
with multi-turn tool use, and persists every interaction to ConversationStore.

Order proposals: ClaudIA calls one of the three `propose_*` tools (see
claudia/proposal_tools.py) when it wants to suggest a staged trade. Their handlers
here record the tool input — which the API has already validated against a strict
schema — and the recorded dict is handed to the MessageSink for button rendering
after the tool loop finishes. The handlers reach nothing: no IBKR call, no execution.

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

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

# Declaration-only tool schemas. Importing them does not couple agent.py to the
# order-execution layer — that module reaches nothing (CLAUDE.md Hard Rule 1).
from claudia.proposal_tools import PROPOSAL_TOOL_NAMES, PROPOSAL_TOOLS

if TYPE_CHECKING:
    from ibkr_core_mcp import ClaudeToolkit

    from claudia.context_loader import ContextLoader
    from claudia.conversation_store import ConversationStore
    from claudia.message_sink import MessageSink
    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

# Max conversation turns injected into context
_HISTORY_LIMIT = 40

# max_tokens caps thinking AND response text together (official adaptive-thinking docs).
# 4096 was sized for a no-thinking response and truncates once reasoning engages.
# Source: https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
_MAX_TOKENS = 16000

# Hardcoded safety block — never loaded from any user-editable file
_SAFETY_BLOCK = """
## ABSOLUTE CONSTRAINTS (non-overridable)

- You are ClaudIA, an AI trading research assistant. You are NOT a licensed financial advisor.
- You CANNOT place, modify, or cancel any order. You have no tools for order execution.
  When you want to suggest a trade, call the matching proposal tool (`propose_order`,
  `propose_cancel`, `propose_modify`) and explain your reasoning. Those tools only render
  a button — they reach nothing. The human must explicitly click it to stage anything.
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

## ORDER PROPOSAL — USE THE TOOLS, NEVER PROSE

To propose a trade action, call the matching tool: `propose_order` (new),
`propose_cancel`, or `propose_modify`. There is no text format for proposals — writing
about a proposal without calling the tool means no button is created and nothing is staged.
Call at most one proposal tool per response.

## ORDER PARAMETER IMMUTABILITY — NON-OVERRIDABLE

If the user specifies any order parameter (symbol, action, quantity, price, order type, TIF),
you MUST use EXACTLY that value in the proposal block. No rounding, no substitution, no
"helpful" adjustments.

If you believe a parameter is risky or unusual (e.g. limit far from market), you may say so
in your explanation text — but the proposal block must still contain the user's exact value.
The user decides. You propose, they confirm.

You MUST NEVER change a user-specified order parameter without the user explicitly approving
the new value in a follow-up message. This includes price, quantity, symbol, order type, and TIF.

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
  Never build a `propose_modify` call from `get_live_orders` data alone.

## MODIFY PARAMETER IMMUTABILITY — NON-OVERRIDABLE

Every field in a `propose_modify` call that the user did NOT ask to change must be copied
byte-for-byte (the exact value) from the latest `get_order_status` result for that order. Only
the specific field(s) the user asked to change may differ. Give `changes` one entry per changed
field, carrying that field's prior value, so the confirmation dialog can show a clear
before/after diff.

You MUST NEVER change an unrequested order field when building a modify proposal. This mirrors
the ORDER PARAMETER IMMUTABILITY rule above — the user decides, you propose, they confirm.

## TOOL RESULT FRESHNESS — NON-OVERRIDABLE

Every tool result is valid only for the turn in which it was returned. When the user asks you
to "retry", "try again", "check again", "verify", "confirm that", or otherwise re-attempt
something you already did earlier in this conversation, you MUST make a fresh tool call in
the current turn before responding — never restate, reuse, paraphrase, or reconstruct a
previous tool result as if it were newly fetched. This applies even when you are confident you
already know the answer, and even when the previous attempt failed and nothing about the
situation has visibly changed — a failed call must be genuinely retried, not assumed to still
be failing.

If the user directly asks you to prove a result came from a real tool call (e.g. "show me the
raw tool result"), you MUST either show the actual output of a tool call you just made, or
say plainly that you have not made that call — never construct a plausible-looking result and
present it as real. Fabricating a tool result, or fabricating "evidence" that a result is real,
is a more serious violation than simply not knowing the answer.

If you are about to respond to a retry/re-check/verify request without having made a tool call
in the current turn, stop and make the tool call first.
"""


_PROPOSAL_KINDS: dict[str, str] = {
    "propose_order": "order",
    "propose_cancel": "cancel",
    "propose_modify": "modify",
}
"""Tool name -> the proposal kind recorded in `_pending_proposal` and dispatched on."""


def _proposal_defect(kind: str, inputs: dict) -> str | None:
    """Return why a proposal must be rejected, or None when it carries no defect.

    `strict: true` already guarantees the types, enums, required keys and closed objects of
    `proposal_tools.py`'s schemas, so nothing here re-checks those. Four things it cannot
    express are checked here instead, because retiring `order_proposal_schema.py` would
    otherwise drop each guarantee silently. All four apply to every proposal kind that
    declares the field — the schemas share `_QUANTITY` across all three tools:

    1. `quantity > 0` — `exclusiveMinimum` is a hard 400 on the tools endpoint (probed
       2026-07-27), so the bound lives only in the field's description: guidance, not
       enforcement.
    2. `symbol` non-blank — `minLength` is deliberately unused (see that module's
       "Deliberate omissions"), and `"   "` would satisfy it anyway.
    3. `order_id` non-blank on cancel/modify — same, and acting on the wrong or no order is
       the failure mode for both.
    4. No duplicate `changes` entries — two entries for one field would render a
       contradictory before/after diff. Unlike 1-3 this rests on the published unsupported
       list rather than a probe: `uniqueItems` was never sent, because a rejected keyword
       is a registration-time 400 on every request and adding it to find out is the risk
       this handler exists to avoid.

    The type checks below are belt-and-braces against a caller that is not the strict tool
    loop (tests, a future non-API path); they are not a claim that the schema fails.

    Args:
        kind: "order", "cancel", or "modify".
        inputs: The tool_use.input dict, **never mutated** — order parameters are immutable,
            so a defective proposal is rejected whole, never repaired.

    Returns:
        A one-line reason suitable for a tool_result, or None if the proposal is acceptable.
    """
    quantity = inputs.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return f"quantity={quantity!r} must be a whole number greater than 0"

    symbol = inputs.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return f"symbol={symbol!r} is blank"

    if kind in ("cancel", "modify"):
        order_id = inputs.get("order_id")
        if not isinstance(order_id, str) or not order_id.strip():
            return f"order_id={order_id!r} is blank"

    if kind == "modify":
        changes = inputs.get("changes")
        if not isinstance(changes, list) or not changes:
            return f"changes={changes!r} must list at least one changed field"
        fields: list[str] = []
        for entry in changes:
            if not isinstance(entry, dict) or not isinstance(entry.get("field"), str):
                return f"changes entry {entry!r} is not a {{field, previous_value}} object"
            fields.append(entry["field"])
        duplicated = sorted({f for f in fields if fields.count(f) > 1})
        if duplicated:
            return f"changes names {', '.join(duplicated)} more than once"

    return None


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

_LOCALLY_HANDLED: frozenset[str] = _LOCAL_TOOL_NAMES | PROPOSAL_TOOL_NAMES
"""Every tool the agent executes itself rather than routing to the toolkit or TradingView.

Hard Rule 1 (CLAUDE.md) applies to this whole set, not just `_LOCAL_TOOL_NAMES`: none of
these may place, modify, cancel, or reply to an order. The `propose_*` handlers record a
proposal for the render path and reach nothing.
"""


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


def _build_version_note(doc_version: str | None, store: ConversationStore | None) -> str:
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
    store: ConversationStore | None = None,
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


def _log_thinking_usage(usage) -> None:
    """Log the thinking share of output tokens from a message_delta usage object.

    `output_tokens_details.thinking_tokens` is the only signal that proves reasoning
    actually engaged, so it is what the adaptive-thinking rollout is measured on. "When
    streaming, this breakdown appears only on the final `message_delta` event" — so when
    it is absent nothing is logged, rather than a misleading zero.
    Source: https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
    """
    details = getattr(usage, "output_tokens_details", None)
    thinking_tokens = getattr(details, "thinking_tokens", None) if details else None
    if thinking_tokens is not None:
        log.info(
            "thinking tokens: %d of %d output",
            thinking_tokens,
            getattr(usage, "output_tokens", None) or 0,
        )


def _history_to_messages(history: list[dict]) -> list[MessageParam]:
    """Convert ConversationStore rows to Anthropic message dicts.

    Args:
        history: Rows from `get_history`, each with `role` and `content`.

    Returns:
        Only `user` and `assistant` messages, in order. **Tool rows are deliberately
        dropped** — the DB stores neither the Anthropic-assigned `tool_use_id`s nor the
        intermediate assistant messages carrying the matching `tool_use` blocks, so
        replaying them would send orphaned `tool_result` blocks and the API would 400. The
        assistant's text already summarises what each tool returned.
    """
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
    # cache_control's nested dict alongside str fields is correct at runtime (same
    # plain-dict-request-body pattern as the SDK call in _run_turn) — not a str-only dict.
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}  # type: ignore[dict-item]
    last["content"] = blocks
    return [*messages[:-1], last]


class ClaudIAAgent:
    """Manages one chat session's Anthropic API interaction.
    Instantiated once per chat session by whichever UI entry point owns that session
    (claudia/panel_app.py — the sole entry point since the Phase 11 cutover removed the
    Chainlit app).
    """

    def __init__(
        self,
        toolkit: ClaudeToolkit,
        store: ConversationStore,
        context_loader: ContextLoader,
        session_id: str,
        sink: MessageSink,
        model: str = "claude-opus-4-8",
        extra_tools: list[dict] | None = None,
        tv_bridge: TradingViewBridge | None = None,
        doc_version: str | None = None,
        trade_context: str | None = None,
    ) -> None:
        """Initialise the agent for one chat session.

        sink: the MessageSink this session renders output through — decouples this
            class from any specific UI framework (see claudia/message_sink.py).
        extra_tools: TradingView tool definitions from TradingViewBridge.get_tools();
            merged into the Anthropic tools= list alongside toolkit's 42 IBKR tools.
        trade_context: optional market-calendar string injected into the system prompt
            at session start (built by ibkr_core_mcp.SQLiteStore.get_market_calendar_context).
        """
        self._toolkit = toolkit
        self._store = store
        self._loader = context_loader
        self._session_id = session_id
        self._sink = sink
        self._model = model
        self._extra_tools = extra_tools or []
        self._tv_bridge = tv_bridge
        self._doc_version = doc_version
        self._trade_context = trade_context
        self._tv_tool_names: set[str] = {t["name"] for t in self._extra_tools}
        self._client = AsyncAnthropic()
        self._system_blocks_cache: list[dict] | None = None
        self._system_reload_seen: int = -1
        # (kind, tool_use.input) for the one proposal this turn may make. Written only by
        # _record_proposal, cleared at the top of every handle_message.
        self._pending_proposal: tuple[str, dict] | None = None

    def set_tv_bridge(self, bridge: TradingViewBridge, tools: list[dict]) -> None:
        """Update the TradingView connection mid-session, after a successful launch.

        Called by panel_app's "Launch TradingView" button handler
        (`_send_action_buttons._on_launch_tv`) once the sidecar is up, so a session that
        started without TradingView gains its tools without a restart.

        Args:
            bridge: The connected TradingViewBridge.
            tools: Curated TradingView tool definitions to merge into the Anthropic
                `tools=` list; their names are recorded so the dispatcher can route calls
                to the bridge rather than the IBKR toolkit.
        """
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
        """The full tool list for the Anthropic `tools=` parameter.

        Order is toolkit (42 IBKR tools) + TradingView extras + `_LOCAL_TOOLS` +
        `PROPOSAL_TOOLS`, and is stable across turns because the cache marker is applied to
        the final entry — reordering would invalidate the tools cache breakpoint on every
        request. The proposal tools go last for that reason: appending is the only way to
        add tools without moving the ones already cached ahead of them.
        """
        return _with_cache_marker(
            self._toolkit.tools + self._extra_tools + _LOCAL_TOOLS + PROPOSAL_TOOLS
        )

    async def handle_message(self, user_text: str, images: list[dict] | None = None) -> None:
        """Process one user message end to end.

        Streams Claude's response, runs the multi-turn tool loop (stream → collect
        `tool_use` → execute → append `tool_result` → stream again), renders whichever
        proposal tool the model called, and persists every message and decision.

        Args:
            user_text: The user's message, persisted before the first API call so it
                survives a mid-turn failure.
            images: Optional Anthropic image content blocks (screenshot uploads,
                TradingView captures) appended to this turn's user message.

        Persists: the user message, the final assistant text, and any proposal as a
        decision row.

        Exceptions from the API or a tool are **not** caught here — they propagate to the
        Panel callback, which surfaces them in the chat feed
        (`ChatInterface.callback_exception="summary"`). Tool-level errors that the toolkit
        turns into strings are fed back to the model instead, so the loop continues.
        """
        # Cleared FIRST, before anything can raise: a turn that dies mid-loop leaves its
        # recorded proposal behind, and a stale one would render a staging button the user
        # never asked for in the next turn.
        self._clear_pending_proposal()

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

        while True:
            response_text = ""
            tool_calls: list[dict] = []
            thinking_blocks: list[dict] = []
            stop_reason: str | None = None

            # system/tools/messages are built as plain dicts throughout this file rather than
            # the SDK's precise TypedDict unions (far simpler to construct/mutate JSON-shaped
            # request bodies this way) — structurally correct at runtime, not statically
            # provable against the SDK's param types. Covered by test_agent.py's 79 tests.
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                # Opus 4.8 does NOT think unless asked: omitting this parameter means no
                # reasoning at all (unlike Opus 5, where adaptive is the default). Both
                # sub-settings stay at their defaults, fixed for the process:
                #   effort (high) — changing it between requests invalidates the messages
                #     cache breakpoint, "and can invalidate tool and system-prompt
                #     breakpoints too, depending on where the model renders the
                #     configuration". Steer per-turn depth by prompting, never by effort.
                #     https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
                #   display (omitted on this model) — we never surface reasoning text, so
                #     blocks arrive with an empty `thinking` field and only `signature`
                #     populated. They are echoed back unchanged either way.
                #     https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows
                thinking={"type": "adaptive"},
                system=system_blocks,  # type: ignore[arg-type]
                messages=_with_history_cache_marker(messages),
                tools=self._all_tools,  # type: ignore[arg-type]
            ) as stream:
                async for event in stream:
                    # Narrow on `event.type`/`delta.type` directly (not a copied variable) —
                    # mypy's discriminated-union narrowing only tracks the checked expression
                    # itself, so branching on a pre-assigned copy defeats it. Same runtime
                    # behavior, but lets mypy verify each branch's attribute access.
                    if event.type == "message_start":
                        _log_cache_usage(event.message.usage)

                    elif event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            tool_calls.append({
                                "id": block.id,
                                "name": block.name,
                                "input_json": "",
                            })
                        elif block.type == "thinking":
                            thinking_blocks.append(
                                {"type": "thinking", "thinking": "", "signature": ""}
                            )
                        elif block.type == "redacted_thinking":
                            # Carried through for the same reason as thinking blocks, and
                            # in the same list so their relative order is preserved (the
                            # turn as a whole is normalized — see the reconstruction
                            # below). Dropping it while echoing its siblings is a
                            # documented 400 ("...blocks in the latest assistant message
                            # cannot be modified"), raised exactly when a rebuilt
                            # assistant turn filters content blocks by type.
                            # https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
                            thinking_blocks.append(
                                {"type": "redacted_thinking", "data": block.data}
                            )

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            response_text += delta.text
                        elif delta.type == "input_json_delta" and tool_calls:
                            tool_calls[-1]["input_json"] += delta.partial_json
                        # A delta always belongs to the most recently opened block and
                        # redacted_thinking blocks carry none, so `[-1]` is necessarily a
                        # `thinking` dict here and both keys exist. Deliberately not
                        # guarded by block type: if that invariant ever broke, dropping a
                        # signature fragment would echo back a silently truncated
                        # signature, which the API rejects as modified — or worse, does
                        # not. A KeyError surfacing in the chat feed is the safer failure.
                        elif delta.type == "thinking_delta" and thinking_blocks:
                            thinking_blocks[-1]["thinking"] += delta.thinking
                        elif delta.type == "signature_delta" and thinking_blocks:
                            thinking_blocks[-1]["signature"] += delta.signature

                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason
                        _log_thinking_usage(event.usage)

            # --- Stream complete ---

            if stop_reason == "max_tokens":
                await self._sink.send_max_tokens_warning()

            # Append assistant turn to the running message list. Thinking blocks come
            # first and are echoed back unmodified: the docs require it during tool use,
            # where they carry the reasoning behind each tool call.
            #
            # This rebuild normalizes block order to thinking → text → tool_use, which is
            # a no-op against the documented shape of a single response rather than a
            # reordering: the worked tool-use round trip returns exactly that order, and
            # interleaved thinking opens a NEW thinking block at the start of the *next*
            # response — one more pass of this loop — instead of splitting the tool_use
            # blocks inside one response. Order among the thinking/redacted_thinking
            # blocks themselves is preserved as streamed.
            # https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows
            assistant_content: list = [*thinking_blocks]
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
                async with self._sink.tool_step(tc["name"]) as step:
                    step.input = json.dumps(tc["input"], indent=2)
                    if tc["name"] in _LOCALLY_HANDLED:
                        result_text = self._handle_local_tool(tc["name"], tc["input"])
                    elif tc["name"] in self._tv_tool_names and self._tv_bridge is not None:
                        result_text = await self._tv_bridge.execute(tc["name"], tc["input"])
                    else:
                        result_text, _ = await asyncio.to_thread(
                            self._toolkit.execute, tc["name"], tc["input"]
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

            messages.append({"role": "user", "content": tool_results})  # type: ignore[typeddict-item]

        # --- Final response ---
        # Nothing is parsed out of the text any more: a proposal is a tool call the API
        # already validated, recorded by _record_proposal during the loop above. At most
        # one exists per turn — the second call is refused there, not silently dropped.
        display_text = full_response_text.strip()
        kind, proposal = self._pending_proposal or (None, None)

        # Persist final assistant message
        msg_id = self._store.add_message(
            self._session_id, "assistant", display_text
        )

        # Render text response
        if display_text:
            await self._sink.send_message(display_text)

        # Render the staging button for whichever proposal was recorded.
        if proposal is not None:
            if kind == "order":
                await self._sink.send_order_proposal(proposal)
            elif kind == "cancel":
                await self._sink.send_cancel_proposal(proposal)
            elif kind == "modify":
                await self._sink.send_modify_proposal(proposal)

        # Log any user-directed trade proposal for future recall
        self._log_proposal(
            display_text,
            proposal if kind == "order" else None,
            msg_id,
            cancel_proposal=proposal if kind == "cancel" else None,
            modify_proposal=proposal if kind == "modify" else None,
        )

    def _clear_pending_proposal(self) -> None:
        """Discard any proposal left over from an earlier turn.

        A method rather than an inline `self._pending_proposal = None`, deliberately: mypy
        narrows an attribute to `None` at the point of assignment and does not widen it
        again across the `_handle_local_tool` call that reassigns it, so inlining this makes
        the whole render dispatch below statically unreachable. Assigning inside a method
        keeps the declared type in force at the read site, where the value really can be a
        recorded proposal.
        """
        self._pending_proposal = None

    def _record_proposal(self, name: str, inputs: dict) -> str:
        """Record one `propose_*` call for the render path. Executes nothing.

        CLAUDE.md Hard Rule 1: this reaches no IBKR API, directly or indirectly. It stores
        the tool input and returns a string; the staging button, Gate 1 (Touch ID) and
        Gate 2 (the AppKit dialog) all still stand between it and a live order.

        The returned `tool_result` is the point of the whole change — before it, the model
        had no feedback on whether its proposal had landed, and defended claims that a
        button existed when none did (finding-llm-proposal-block-emission). So it reports
        exactly what this method knows and nothing more: that the proposal was recorded, or
        precisely why it was refused. It cannot know the render later succeeded, and must
        never imply it did.

        Args:
            name: One of `_PROPOSAL_KINDS`' keys.
            inputs: `tool_use.input`, already schema-valid. Stored by reference and never
                mutated — order parameters are immutable, so a defective proposal is
                rejected whole and re-proposed, never silently corrected.

        Returns:
            An acceptance or refusal string for the model. On refusal `_pending_proposal`
            is left untouched, so nothing is rendered and nothing is staged.
        """
        kind = _PROPOSAL_KINDS[name]

        if self._pending_proposal is not None:
            log.warning("Second proposal in one turn (%s); keeping the first", name)
            return (
                "REJECTED — a proposal is already pending for this turn, and only one may "
                "be proposed. No staging button was created for this call; the first "
                "proposal stands."
            )

        defect = _proposal_defect(kind, inputs)
        if defect is not None:
            log.warning("Rejected %s proposal: %s", name, defect)
            return (
                f"REJECTED — {defect}. No staging button was created and nothing was "
                "staged. Tell the user plainly what was wrong; do not substitute a "
                "corrected value for any order parameter they specified."
            )

        self._pending_proposal = (kind, inputs)
        return (
            "Proposal accepted. A staging button will be rendered for the user; "
            "nothing is staged or sent to IBKR until they click it and pass Touch ID "
            "and the confirmation dialog."
        )

    def _handle_local_tool(self, name: str, inputs: dict) -> str:
        """Dispatch every locally-executed tool and return a string result.

        Two groups, both listed in `_LOCALLY_HANDLED`: the three `propose_*` tools, which
        only record a proposal (`_record_proposal`), and the five utility tools
        (list_doc_versions, get_doc_version, search_past_conversations, fetch_web_page,
        get_live_pnl) that are declared alongside the toolkit's but executed here rather
        than via toolkit.execute(). They always return a string — never raise.
        """
        if name in PROPOSAL_TOOL_NAMES:
            return self._record_proposal(name, inputs)
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
        """Best-available live P&L text — see execution_listener.get_live_pnl_text
        for the cache-then-ledger-fallback logic. Never raises."""
        from claudia.execution_listener import get_live_pnl_text
        return get_live_pnl_text(self._toolkit)

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
        """Fetch a public web page as Markdown for the LLM — the SSRF boundary.

        This is the only place the agent loop makes an LLM-directed outbound HTTP request,
        so the guard here is load-bearing (SECURITY.md §8, Layer 1).

        **Redirects are followed manually** — `allow_redirects=False`, up to
        `_MAX_REDIRECTS` (5) hops — and `_validate_public_url` re-runs on *every* hop.
        Letting `requests` follow them would allow a public URL to 302 into
        `localhost:5055` (the IBKR gateway) without re-validation; that was finding S1,
        2026-07-03. A hop that fails validation reports "(via redirect)" so the log
        distinguishes it from a directly-blocked URL.

        Residual, accepted: a TOCTOU gap between the DNS check and `requests.get()` — a
        hostname could flip to a private address in between. The Crawl4AI path closes this
        with a browser-level route handler (Layer 2); this path does not.

        Args:
            inputs: `url` (required) and optional `extract`, a focus hint echoed back to
                the model above the content.

        Returns:
            `[Fetched: <url>]` followed by the page as Markdown, truncated at 12,000
            characters to bound context growth. **Never raises** — every failure path
            (missing URL, blocked host, too many redirects, connection error, non-2xx)
            returns an explanatory string, because the caller feeds this straight back to
            the model as a tool result.
        """
        import urllib.parse

        import html2text
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
        """Send a single image with an optional caption as one user turn.

        Convenience wrapper over `handle_message` for TradingView screenshot analysis.

        Args:
            image_b64: Base64-encoded image bytes (no data-URI prefix).
            media_type: MIME type, e.g. "image/png".
            caption: Optional accompanying text; a default prompt is used when empty.
        """
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
