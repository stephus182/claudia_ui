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

Source (Messages API streaming): https://platform.claude.com/docs/en/api/messages-streaming
Source (Tool use): https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview

Models — read 2026-08-05 from
https://platform.claude.com/docs/en/about-claude/models/overview

  | model              | ctx | out  | $/MTok in-out | notes                            |
  |--------------------|-----|------|---------------|----------------------------------|
  | claude-fable-5     | 1M  | 128k | 10 / 50       | most capable widely released     |
  | claude-opus-5      | 1M  | 128k |  5 / 25       | the docs' starting recommendation|
  | claude-opus-4-8    | 1M  | 128k |  5 / 25       | **this app's default — legacy**  |
  | claude-sonnet-5    | 1M  | 128k |  3 / 15       | speed/intelligence balance       |

**`claude-opus-4-8` is listed under "Legacy models" as of this reading**, and Opus 5 is
priced identically with the same context and output limits. The default in
`panel_app._MODEL` has deliberately NOT been moved: switching the model a live trading
assistant runs on is a behaviour change, not a docs correction, and it belongs to whoever
owns the account rather than to a docstring refresh.

Whatever it moves to must satisfy **both** constraints in `docs/env-vars-reference.md`
§ `CLAUDIA_MODEL` — adaptive thinking *and* mid-conversation system messages. Opus 5 and
Fable 5 satisfy both; the Sonnet line satisfies only the first, which is what
`warn_if_model_lacks_operator_channel` exists to catch. Sonnet 5 also carries introductory
pricing of $2/$10 through 2026-08-31, so its steady-state cost is the $3/$15 above.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

### DERIVED FIGURES MUST NAME THEIR BASE

Every percentage, ratio, multiple or "X% of Y" statement MUST name the quantity it is
computed against, and that quantity must itself come from a guaranteed source above.

This is not stylistic. A 3,009.91 loss on a position now worth 9,245 is a 32.6% decline
measured against current market value, and a 24.6% drawdown measured against the 12,254.91
cost basis. Both are defensible; they differ by 8 percentage points; and a percentage
quoted without its base cannot be checked by the person acting on it. Stating one number
while naming the other base is worse than stating neither, because it reads as verifiable
and fails verification.

Where the base you want is not present in a tool result — cost basis, for instance, when
you were only given market value and unrealized P&L — either derive it explicitly and show
that arithmetic, or say which base you are using instead. Do not switch bases silently.

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

## NARRATED ACTIONS REQUIRE A TOOL CALL — NON-OVERRIDABLE

An action you report performing must have been performed by a tool call in this turn.
Announcing an action is not performing it, and describing a result is not observing one.

- If you write that you will switch the chart, capture a screenshot, read the Pine editor,
  compile a script, pull a quote, load data, or check anything at all, the matching tool call
  MUST appear in this same turn before you report any outcome of it. There is no path from
  intent to result that does not pass through the tool.
- If you announce an action and then do not make the call, say exactly that and stop. "I said
  I would capture it and did not" is a complete answer. A described chart, editor, connection
  or result that no tool returned is a fabrication, and it is worse than saying nothing,
  because it reads as an observation and fails verification.
- Never describe what a chart, screen, editor, feed or account shows unless a tool result in
  this turn put it in front of you, or the user did. What you remember of it from an earlier
  turn is not what it shows now, and a value you can picture is not a value you read.
- A button the user clicks is not a tool call you made, and you have no result from it. Say
  what you know and what you do not, then read the state with a tool if you need it.

## ORDER EXISTENCE REQUIRES EVIDENCE — NON-OVERRIDABLE

An order exists, or does not exist, only as the current turn's evidence shows. Three rules,
none of which may be softened:

1. If a tool call you need in order to establish an order's state FAILS or returns an error,
   you have no evidence about that order. Say plainly that the check failed and that you
   could not verify it, then stop. You MUST NOT fall back on a result from an earlier turn
   to state what an order's status is now — an earlier result describes the world before
   whatever has happened since, including any order the user staged in between.
2. You MUST NEVER conclude from a failed, empty, or missing lookup that an order does not
   exist, was never placed, or that "there is nothing to cancel". A failed call is not
   evidence of absence. "The check failed, so I cannot verify" is the whole answer; offer
   to try again.
3. Absence from `get_live_orders` is not proof that an order does not exist. That feed
   excludes Filled, Cancelled, ApiCancelled and Expired orders, so a fully filled order is
   absent from it too. To state that an order is gone you need a positive observation —
   `get_order_status` reporting it — not the absence of a row.

Stating that an order does not exist, when it does, is as serious a failure as inventing
one, and more dangerous: it hides live exposure from the user.
"""


_PROPOSAL_KINDS: dict[str, str] = {
    "propose_order": "order",
    "propose_cancel": "cancel",
    "propose_modify": "modify",
}
"""Tool name -> the proposal kind recorded in `_pending_proposal` and dispatched on."""


_GUARDRAIL_NOTICE = (
    "⚠️ **No staging button was created for that message.** ClaudIA accepted the "
    "proposal but it could not be rendered, so **nothing has been staged and no order "
    "exists**. Ask again and it will be re-proposed."
)
"""Shown, persisted, and mirrored to the model whenever a proposal fails to render.

Deliberately unhedged. It exists to contradict a claim the transcript already carries —
on 2026-07-17 and 2026-07-24 ClaudIA told the user a staging button had been created when
none had, and in a later turn defended the claim ("it's sitting in front of you") because
nothing had ever told it otherwise. Wording like "may not have been created" would leave
that claim standing.
"""


_OPERATOR_NOTE = (
    "The {kind} proposal in the preceding turn was accepted but failed to render. "
    "No staging button exists and nothing was staged. Do not tell the user it was staged."
)
"""Operator-channel body for a failed render — see `_append_operator_message` for the why."""


_OPERATOR_CHANNEL_MODELS = frozenset({
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
})
"""Models that accept a mid-conversation `role: "system"` message.

Documented on the official prompt-caching page (read 2026-08-05), which names exactly
these four and excludes the Sonnet line outright: *"This feature is not available on
Claude Sonnet 5; use the top-level `system` field instead."* Sonnet 4.6 is likewise absent
from the supported set.

**Measured, not merely read** — the page states the exclusion but gives no error, and a doc
is a claim where execution is evidence. Probed 2026-08-05 with the production message
shape (`test_live_api_rejects_the_operator_channel_on_an_excluded_model`)::

    claude-sonnet-4-6  ->  400 invalid_request_error
                           "role 'system' is not supported on this model"

`claude-opus-4-8` accepts the same shape, in all three placements the agent really
produces — probed 2026-07-27 and re-run 2026-08-05
(`test_live_api_accepts_mid_conversation_system_message`).

`CLAUDIA_MODEL` is a documented user-facing knob, and until 2026-08-05
`docs/env-vars-reference.md` named `claude-sonnet-4-6` as "the supported alternative" — it
had been checked against the adaptive-thinking requirement only, which Sonnet does meet.
The operator channel arrived later (2026-07-27) and nothing re-checked the recommendation
against it.

**The failure is delayed, which is what makes it worth a startup warning.**
`_append_operator_message` only appends when there is something to deliver, so a session on
an unsupported model runs perfectly until the first proposal renders — and from then on
*every* turn carries an emission record and 400s. The channel that breaks is the one
carrying the evidence that ClaudIA staged an order, i.e. the thing that stops it telling
the user a live order does not exist.

Deliberately NOT solved by falling back to a `<system-reminder>` block in the user turn,
which is what the docs suggest for unsupported models: `_append_operator_message` exists
*because* a user-turn block is forgeable by the model, and silently downgrading a
non-spoofable channel to a spoofable one is a worse outcome than refusing to run on that
model. This is an allowlist rather than a denylist for the same reason the store's decision
allowlists are: a model absent from it gets a warning, never silent acceptance.
"""


def warn_if_model_lacks_operator_channel(model: str) -> str | None:
    """Log a loud, actionable error when `model` cannot carry the operator channel.

    Called once at startup, mirroring `install_check.warn_if_stale`. Warns and returns
    rather than raising, for the same reason that one does: the list above can only go
    stale in the direction of a *new* supported model, and refusing to start would turn a
    lagging constant into an outage. The log line says what will break and when, because
    the symptom — a 400 that begins partway through a session rather than at startup — is
    not something anyone would trace back to a model id unaided.

    **When it starts moved on 2026-08-11.** It used to be "after the first rendered order
    proposal", which made the failure rare and late. The called-tool ledger puts the channel
    in use from the turn after *any* non-proposal tool call, so on an unsupported model the
    400 now arrives far earlier and in far more sessions. Measured over this repo's own
    history: 53 of the 63 sessions that ever received a user message made a ledger-eligible
    call (**84%**), against 10 (16%) that recorded a `trade_proposed` decision. Bigger blast
    radius and an easier symptom to spot — but only if this message says so.

    Args:
        model: The resolved `CLAUDIA_MODEL` value.

    Returns:
        The model id when it is not known to support the channel, else None.
    """
    if model in _OPERATOR_CHANNEL_MODELS:
        return None
    log.error(
        "MODEL %r IS NOT KNOWN TO SUPPORT MID-CONVERSATION SYSTEM MESSAGES. ClaudIA will "
        "start and answer normally, then fail with an API 400 on EVERY turn once the "
        "operator channel has anything to carry — which is the turn after ANY tool call, "
        "because the called-tool ledger rides this channel, and again for rendered "
        "proposals, completed orders and guardrail notices. Those payloads are what stop "
        "ClaudIA answering from memory about a tool result it no longer has, or denying "
        "that a staged order exists. Known-good: %s. If this model is newly supported, add it to "
        "agent._OPERATOR_CHANNEL_MODELS; check "
        "https://platform.claude.com/docs/en/build-with-claude/prompt-caching",
        model,
        ", ".join(sorted(_OPERATOR_CHANNEL_MODELS)),
    )
    return model


_TOOL_LEDGER_HEADER = (
    "TOOLS ALREADY RUN IN THIS SESSION (before this turn — called by you, or run by a\n"
    "user button click)\n"
    "Their results are NOT in your context — only your own earlier messages about them.\n"
    "Anything you state about what one returned is recollection, not evidence. To report a\n"
    "current value or state, read it again in this turn rather than recalling it."
)
"""Header of the replayed called-tool ledger. See `ClaudIAAgent._called_tool_records`.

"Called by you, or run by a user button click" since 2026-08-12: the Pine Inject button
persists its `pine_set_source` call as a tool row (`panel_pinescript._on_inject`), so the
ledger can now name a tool the model never called. The old header's "TOOLS YOU CALLED"
would have asserted the model's own agency over a click — false in the same way the
failures this channel corrects are false, just in the other direction.
"""


_PROPOSAL_DECISION_TOOLS: dict[str, str] = {
    "trade_proposed": "propose_order",
    "trade_cancel_proposed": "propose_cancel",
    "trade_modify_proposed": "propose_modify",
}
"""Rendered-proposal decision type -> the tool call that produced it.

Keys mirror `conversation_store.RENDERED_PROPOSAL_TYPES` (the store owns the decision
vocabulary; naming the *tool* is a message-construction concern and belongs here). Pinned
to each other by test_emission_record_tools_cover_exactly_the_store_allowlist, so a fourth
rendered type cannot be added on one side and silently dropped on the other.
"""


_EMISSION_RECORD_HEADER = (
    "Proposals you have already emitted in this session. Each one rendered as a staging "
    "button for the user. This list is complete: if a proposal is not named here, you did "
    "not emit it and no button for it exists. Identities only — the parameters you "
    "proposed are not repeated here, so never restate any of them from this list."
)
"""Header of the replayed emission-record block. See `_emission_records` for the why."""


_COMPLETED_ACTION_VERBS: dict[str, str] = {
    "trade_staged": "PLACED",
    "trade_cancelled": "CANCEL SENT for",
    "trade_modified": "MODIFY SENT for",
}
"""Completed-action decision type -> how the record names what reached IBKR.

Keys mirror `conversation_store.COMPLETED_ORDER_ACTION_TYPES` (the store owns the decision
vocabulary; wording is a message-construction concern and belongs here). Pinned by
test_completed_action_verbs_cover_exactly_the_store_allowlist, so a fourth post-click type
cannot be added on one side and silently dropped on the other.

The verbs name the *dispatch*, not the outcome — "PLACED" is followed on every line by what
the read-back actually observed. A verb that implied the outcome ("CANCELLED") would assert
more than the row knows.
"""


_COMPLETED_ORDER_HEADER = (
    "Order actions completed in this session. Each one is a button click by the user that "
    "passed both confirmation gates and was DISPATCHED TO IBKR. A button click produces no "
    "tool call and no message, so nothing else in this conversation records that it "
    "happened — this list is your only evidence of it.\n"
    "Every action below reached IBKR. What each line states is what was observed "
    "immediately afterwards, and it was true at that moment only: never present it as the "
    "order's current state, and never conclude from this list — or from a lookup that "
    "failed, errored, or returned nothing — that an order does not exist or was never "
    "placed. To state an order's current state you must call a tool now. Identities and "
    "observed states only, so never restate an order parameter from this list."
)
"""Header of the replayed completed-action block. See `_completed_order_records`."""


_UNBACKED_CLAIM_NOTICE = (
    "⚠️ **That message described an order action that never happened.** No proposal tool "
    "was called, so **no staging button was created, nothing has been staged and no order "
    "exists**. Ask again and it will be proposed for real."
)
"""Shown, persisted and mirrored to the model when a staging claim has no tool call behind it.

Unhedged for the same reason as `_GUARDRAIL_NOTICE`: it exists to contradict a claim the
transcript already carries, and "may not have been created" would leave that claim standing.
Its own wording must never trip `_claims_completed_proposal` — pinned by
tests/test_agent.py::test_the_guardrails_own_texts_never_trip_the_detector.
"""


_UNBACKED_CLAIM_OPERATOR_NOTE = (
    "Your preceding message told the user an order action was completed, but you called no "
    "proposal tool in that turn: no staging button was created and nothing was staged. The "
    "user has been told. Do not repeat or defend that claim. Writing about a proposal is "
    "not proposing — call `propose_order`, `propose_cancel` or `propose_modify` if you "
    "intend one."
)
"""Operator-channel body for a narrated action nothing backs. See `_append_operator_message`."""


_STALE_BOOK_CLAIM_NOTICE = (
    "⚠️ **That message described a check of your live orders that never ran.** No "
    "order-book tool was called in that turn, so **any order state it stated came from "
    "memory, not from IBKR**. Ask again and the book will be read for real."
)
"""Shown, persisted and mirrored to the model when a book-check claim has no tool call.

Unhedged for the same reason as its two siblings, and worded to trip neither detector —
pinned by tests/test_agent.py::test_the_guardrails_own_texts_never_trip_the_detector.
"""


_STALE_BOOK_CLAIM_OPERATOR_NOTE = (
    "Your preceding message told the user you had checked their live orders, but no "
    "order-book tool ran in that turn. The user has been told. Do not repeat or defend "
    "that claim, and do not restate any order's status from it. Call `get_live_orders`, "
    "`get_order_status` or `diagnose_orders` before describing what any order is doing now."
)
"""Operator-channel body for an unverified book check. See `_append_operator_message`."""


_UNBACKED_ACTION_NOTICE = (
    "⚠️ **That message reported an action that never ran.** No tool was called in that "
    "turn, so nothing was switched, captured, compiled, fetched or checked, and anything "
    "it described came from memory rather than from a live read. Ask again and it will "
    "be done for real."
)
"""Shown, persisted and mirrored to the model when an action report has no tool call.

Unhedged for the same reason as its three siblings: it exists to contradict a claim the
transcript already carries, and "may not have run" would leave that claim standing. Its
own wording must trip none of the four detectors — pinned by
tests/test_agent.py::test_the_guardrails_own_texts_never_trip_the_detector.
"""


_UNBACKED_ACTION_OPERATOR_NOTE = (
    "Your preceding message announced an action and then reported it as done, but no "
    "tool ran in that turn: the action did not happen and nothing it described was "
    "observed. The user has been told. Do not repeat or defend that claim, and do not "
    "restate any value from it. Announcing an action is not performing it — call the "
    "tool in this turn, or state plainly that you did not act."
)
"""Operator-channel body for a narrated action nothing ran. See `_append_operator_message`."""


_UNBACKED_RESULT_NOTICE = (
    "⚠️ **That message displayed a block presented as a raw tool result, but no tool "
    "ran in that turn.** The block was constructed, not returned by any tool, and none "
    "of its values were observed. Ask again and the tool will be called for real."
)
"""Shown, persisted and mirrored to the model when a vouched-for payload has no tool call.

The most serious member of the family: the measured instance was an audit response — the
user asked for the raw result specifically to verify an earlier number, and the model
manufactured one. The wording names the block as constructed rather than wrong, because
its values may even coincide with reality; what is false is the provenance.
"""


_UNBACKED_RESULT_OPERATOR_NOTE = (
    "Your preceding message showed the user a fenced block presented as a raw tool "
    "result, but no tool ran in that turn: you constructed that payload. The user has "
    "been told. Never present constructed content as a tool return — call the tool and "
    "show what it actually returned, or state plainly that you have no result to show."
)
"""Operator-channel body for a constructed payload. See `_append_operator_message`."""


_BOOK_READING_TOOLS = frozenset({"get_live_orders", "get_order_status", "diagnose_orders"})
"""The tools whose results are evidence about the current order book.

Enumerated from ibkr_core_mcp's declarations, not from memory: those three plus
`preview_order` are the toolkit's entire order surface, and `preview_order` is deliberately
absent — it prices a hypothetical order and reads no existing one, so treating it as
evidence would clear exactly the claim this check exists to catch. Drift is pinned by
tests/test_agent.py::test_book_reading_tools_match_the_toolkits_order_surface.
"""


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])(?=\s|[A-Z])|\n+")
"""Sentence boundary: terminal punctuation followed by whitespace or a capital, or a newline.

The capital-letter branch is not cosmetic — streamed replies routinely arrive with the space
eaten ("…stays untouched.Cancel staged — …"), and without it the claim and its surrounding
prose stay in one span. Requiring whitespace-or-capital is also what keeps "$6,500.00" and
"1.5" intact, since a digit follows those periods.
"""

_MARKUP = re.compile(r"[*_`>#|]+")
"""Markdown that can sit anywhere inside a claim ("the **button**'s above") and must not
break the match. Replaced with a space, never deleted, so table cells do not fuse into words.
"""

# Curly apostrophes and dashes are spelled as `\uXXXX` escapes throughout these patterns:
# in a character class an en dash and a hyphen are indistinguishable on screen, and getting
# that wrong silently changes what the guardrail matches. `re` resolves the escapes itself.
_BUTTON_IS_HERE = re.compile(
    r"\bbuttons?\b['\u2019]?s?"
    r"(?:\s+(?:is|are|was|were))?"
    r"(?:\s+(?:now|live|up|right|just|sitting|waiting|there|ready))*"
    r"\s+(?:above|below|up top)\b",
    re.I,
)
"""Claim shape 1: a staging button is asserted to exist *here*, in this message.

The deictic locator is the whole point. "button" alone is the single most common word in
ClaudIA's honest staging talk — explanations, warnings, recaps and the self-correction all
use it — and a detector keyed on the noun is the one that measured 81% false positives. What
no innocent sentence does is point at a button in the message being written. The permitted
gap between the noun and the locator is a fixed handful of copulas and adverbs, which is what
keeps "produced no button — the only real cancel proposal is item 3 above" out: 40-odd
characters of unrelated clause sit between its two words.
"""

_ACTION_DONE = re.compile(
    r"^(?:(?:done|ok|okay|alright)\s*[\u2014\u2013:,-]\s*)?"
    r"(?:(?:the\s+)?(?:cancel|modify|order|trade)\s+)?"
    r"(?:re-?)?(?:staged|proposed)\b"
    r"(?=\s*(?:[\u2014\u2013,.;:!-]|$)"
    r"|\s+(?:the|a|an|your|this|that|it|one|for|on|at|with)\b)",
    re.I,
)
"""Claim shape 2: the sentence *opens* with a completed proposal act ("Cancel staged — …").

Sentence-initial only, deliberately. A participle that opens the sentence is the predicate;
the same word later in the sentence is almost always attributive or historical — "your test
order, staged through ClaudIA", "both were staged through me", "| … | ClaudIA-staged |".

The lookahead separates the two readings that share the opening slot. Punctuation or a
determiner/preposition after the participle makes it a verb ("Staged — the button's above",
"Done — re-staged with your new value"); a bare noun after it makes it a compound modifier
("Staged button ≠ live order"). That single distinction is what keeps the honest sentence out
while keeping the failure in.

First person is deliberately absent. "I proposed a cancel earlier" and "I staged that
yesterday" are recaps of previous turns, which this check has no evidence about — L3's
emission records are the channel for those. Only claims about *this* turn belong here.
"""

_GOVERNING_OPERATOR = re.compile(
    r"(?:\b(?:no|not|never|without|cannot|if|whether|would|will|shall|"
    r"described|describing|claimed|claiming|pretend|pretending|wrote|writing|"
    r"said|presented|showed|reported|told|"
    r"want me to|say the word|once you|when you)\b"
    r"|n['\u2019]t\b)",
    re.I,
)
"""Negation, condition and meta-report — the three ways a staging phrase is not a claim.

Two corrections from the 2026-08-12 review, both verified by executed reproductions:
the original `n't` sat inside the `\\b(?:...)\\b` group, and `\\b` before the `n` cannot
match after a word character — so "can't"/"didn't" never vetoed anything, ever. It now
matches as its own suffix alternation. And `said|presented|showed|reported|told` join the
meta-report family: "The warning said I presented a constructed block as a raw tool
result" is a model honestly explaining its own correction, and the guardrail firing on
that would teach the user to ignore the one signal that matters.

Checked **only to the left of the match**, because in English an operator governs what
follows it. That asymmetry is load-bearing in both directions: it vetoes "No button was
produced above" and "I described staging a cancel…", and it does *not* veto the trailing
reassurance ClaudIA appends to every genuine staging line ("…— nothing is live until you
click it", "…and nothing will reach IBKR until you confirm"), which a sentence-wide negation
or future-tense veto would silently swallow.
"""


def _claims_completed_proposal(text: str) -> str | None:
    """Return the sentence claiming a completed order action, or None if there is none.

    This implements **Verify with citations**, the third of the three *basic* strategies in
    Anthropic's "Reduce hallucinations" guidance — *"have Claude verify each claim by finding
    a supporting quote after it generates a response — if it can't find a quote, it must
    retract the claim"*
    (https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations).
    One deliberate divergence: the doc has the model audit itself, which is worthless here,
    because the model that narrated a phantom cancel is exactly the one that would be asked
    whether it made one. **The trigger is textual; the verdict is evidence.** This function
    only decides that a claim was made — whether it is true is settled by `_pending_proposal`
    at the call site, and by nothing else.

    (Counted 2026-08-12: the page carries **three** basic strategies and four advanced ones.
    This docstring said "four basic techniques … the fourth is" from 2026-07-27 until then —
    an inherited miscount, corrected rather than repeated. Full technique-by-technique map:
    `docs/agent-behavior-reference.md`.)

    Precision is the entire design constraint. A detector that fires on ClaudIA's own honest
    self-correction is worse than no detector: it teaches the user to ignore the one signal
    that would have exposed the original defect. Measured against the whole assistant corpus
    of the live store (166 messages, 47 carrying staging vocabulary, 34 of those with no
    proposal recorded): the shapes matched 6 messages, the evidence check cleared the 5 that
    really had proposed, and the single remaining fire is the 2026-07-28 failure itself.
    **Zero false positives; precision 1/1.** The dropped 2026-07-27 detector measured 81%
    false positives on the same corpus by keying on vocabulary rather than on claims.

    What it gives up for that, knowingly:
      - claims about *earlier* turns ("it's already staged in my earlier message") — no
        evidence about those exists here; that is L3's channel.
      - claims about non-proposal tools ("Confirmed against the live book"), which were part
        of the same live failure. A general "I checked X" detector has no reliable shape.
      - a bare "Cancel staged." with no locator and no sentence-initial position.

    Args:
        text: The assistant text about to be, or already, shown to the user.

    Returns:
        The offending sentence — for the log line only, never for storage: it is live
        conversation text and the decision row must stay free of it.
    """
    for sentence in _SENTENCE_SPLIT.split(_MARKUP.sub(" ", text)):
        sentence = sentence.strip()
        # A question proposes nothing. "Want me to stage the cancel now?" is the single most
        # common staging sentence ClaudIA writes.
        if not sentence or sentence.endswith("?"):
            continue
        for shape in (_ACTION_DONE, _BUTTON_IS_HERE):
            hit = shape.search(sentence)
            if hit is not None and not _GOVERNING_OPERATOR.search(sentence[: hit.start()]):
                return sentence
    return None


_BOOK = (
    r"(?:live\s+book|order\s+book|the\s+book|live\s+orders?|open\s+orders?|"
    r"working\s+orders?|order\s+status|your\s+orders?)"
)
"""The order book, by every name ClaudIA calls it.

Deliberately excludes "IBKR" on its own. "Confirmed against IBKR" can honestly follow
`get_positions` or `get_account_summary`, and a noun that broad would make the evidence
gate below answer a different question than the sentence asked.
"""

_BOOK_VERB = r"(?:re-?)?(?:confirmed|checked|verified|pulled)"
"""Past tense only. That single restriction is what carries every offer and intention —
"let me check the book", "I'll pull the live orders", "should I check?" — without a veto
term, because none of them claims anything happened."""

_BOOK_LINK = (
    r"(?:\s+(?:against|with|in|on|via|at|from|the|your|our|both|all|that|it|current|full))*"
)
"""The only words permitted between the verb and the noun. Adjacency is what separates this
from the 81%-false-positive shape: in this corpus a verification verb and a book noun are
merely co-present in 76 of 169 messages, and adjacent in 2."""

# Verbal use only. An adjective takes a determiner ("a confirmed live order", "the checked
# open orders"), so neither alternative below can reach one — the false-positive class this
# corpus happens not to contain, which is exactly why it is excluded by construction.
_BOOK_CLAIM = re.compile(
    r"(?:^(?:just\s+)?" + _BOOK_VERB
    # `\u2019` (curly apostrophe) per this file's convention: a curly and a straight
    # apostrophe are indistinguishable on screen inside a character class.
    + r"|\bI(?:['\u2019]ve|\s+have|\s+just|\s+already|\s+also)*\s+" + _BOOK_VERB
    + r")\b" + _BOOK_LINK + r"\s+" + _BOOK,
    re.I,
)
"""Claim shape: "I looked at the order book", asserted about *this* turn."""

_PAST_TURN = re.compile(
    r"\b(?:earlier|before|previously|ago|last\s+time|last\s+turn|yesterday|"
    r"above|already|at\s+the\s+start|when\s+I\s+(?:first|last))\b",
    re.I,
)
"""A check attributed to an earlier turn, which this function has no evidence about.

Sentence-wide, unlike `_GOVERNING_OPERATOR`: a time adverb binds from either side ("Earlier
I pulled the book", "I pulled the book earlier"). Note "above" means the opposite here than
it does in `_BUTTON_IS_HERE` — there it points at a button in *this* message, here it points
at a lookup in a previous one.
"""


def _claims_fresh_book_check(text: str) -> str | None:
    """Return the sentence claiming a just-performed order-book check, or None.

    The freshness half of the 2026-07-28 failure, and the sibling of
    `_claims_completed_proposal`: that one asserts *claimed a proposal ⇒ called a proposal
    tool*, this one asserts *claimed a lookup ⇒ called a lookup tool*. The same message
    committed both, and neither check can see the other's half.

    Architecture is identical and deliberate — **the trigger is textual, the verdict is
    evidence**. This function decides only that a check was claimed; whether one happened is
    settled at the call site by `_BOOK_READING_TOOLS`, and by nothing else.

    Measured the same way, against the same live store (169 assistant messages). A verb from
    this family and a book noun are both present somewhere in 76 of them — the shape the
    dropped 2026-07-27 detector would have keyed on, and a 45% fire rate. Requiring them
    adjacent and the verb verbal leaves 2 matches: one cleared by evidence, and one fire,
    which is the 2026-07-28 failure itself. **Zero false positives.**

    What it gives up for that, knowingly:
      - claims of having checked *something else* ("confirmed against IBKR"), where the
        evidence set to check against is genuinely ambiguous.
      - a check that ran and failed. `get_live_orders` returning HTTP 500 still counts as
        evidence here, because sniffing a tool result for error-ness is a heuristic dressed
        as a fact. That case is the `_SAFETY_BLOCK`'s ORDER EXISTENCE section, which forbids
        concluding anything from a failed call, and it held live on 2026-07-28.
      - claims with no book noun at all ("Confirmed — two orders working").

    Args:
        text: The assistant text about to be, or already, shown to the user.

    Returns:
        The offending sentence — for the log line only, never for storage: it is live
        conversation text and the decision row must stay free of it.
    """
    for sentence in _SENTENCE_SPLIT.split(_MARKUP.sub(" ", text)):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        hit = _BOOK_CLAIM.search(sentence)
        if hit is None or _PAST_TURN.search(sentence):
            continue
        if not _GOVERNING_OPERATOR.search(sentence[: hit.start()]):
            return sentence
    return None


_SEGMENT_SPLIT = re.compile(r"(?<=[.!?…:;])(?=\s|[A-Z])|\n+")
"""`_SENTENCE_SPLIT` plus `:` and `;` boundaries.

The action detector needs the finer split because the streamed fabrications join intent
and report with a colon and no space — "the non-disruptive way:Here's TSLA", "Now
compiling:Done — injected". The book and proposal detectors keep the coarser split their
corpus measurements were made against.
"""

_TOOL_ACTION_VERB = (
    "(?:check|pull|fetch|read|load|retry|verify|run|compile|capture|switch|grab|"
    "query|inject|sync|scan)"
)
"""Verbs that commit to a tool action, and nothing else — a closed allowlist.

The allowlist is the primary veto. "Let me be precise", "let me know", "I'll hold that
as our reference point", "I'll accept the empirical result", "I'll take it at face
value" are the corpus's most common innocent lead-ins, and none of them survives a verb
gate — `be`, `know`, `hold`, `accept` and `take` are not tool actions. `get` and `look`
are excluded the same way ("I'll get back to you", "let me look at this differently");
no measured fabrication needed either. Deliberately absent: `stage`, `propose`, `place`,
`cancel`, `modify` — order vocabulary belongs to `_claims_completed_proposal`, and a
second detector matching it would double-correct a single lie.

Plain literals only (no regex metacharacters): the drift-guard test derives each verb's
participle from this string and asserts `_REPORTED_COMPLETE` carries it.
"""

_TOOL_ACTION_GERUND = (
    "(?:check|fetch|pull|compil|captur|inject|verify|retry|"
    "sync|query|grabb|scann)ing"
)
"""The same verbs as segment-opening gerunds — "Checking cache first, then fetching."

Measured shape (three instances, two sessions). Narrower than the verb list on purpose,
and narrowed further by the 2026-08-12 review: `running`/`getting` read as discourse, and
`switching`/`reading`/`loading` open English idioms that are not commitments — "Switching
gears:", "Reading between the lines,", "Loading up on semis" — each verified to arm the
detector on an innocent turn. Every measured gerund fabrication used checking/fetching/
compiling; the dropped stems keep their base-verb coverage in `_INTENT_PREAMBLE`'s
first-person branch, where "I'll switch"/"Let me read" carry no idiomatic reading.
"""

_INTENT_PREAMBLE = re.compile(
    r"(?:^(?:Now\s+|Then\s+)?" + _TOOL_ACTION_GERUND + r"\b"
    r"|\b(?:I(?:['\u2019]ll|\s+will|\s*['\u2019]?m\s+going\s+to)|Let\s+me)\s+"
    r"(?:\w+\s+){0,2}?" + _TOOL_ACTION_VERB + r"\b)",
    re.I,
)
"""The lead-in to act: first-person future, "let me", or a segment-opening gerund.

Up to two filler words between the auxiliary and the verb ("I'll just quickly check"),
bounded so "I'll ask you to check" stays out. The gerund branch is anchored to the
segment start, which is what keeps mid-sentence participial phrases ("while checking the
cache, I noticed…") from counting as commitments.
"""

_RESULT_NOUN = (
    "(?:charts?|screenshots?|editor|source|stud(?:y|ies)|indicators?|values?|"
    "bars?|quotes?|feed|status|results?|errors?|snapshots?)"
)
"""What a tool returns, by the names ClaudIA gives it — the gate on the "Here's" report.

Every noun is earned by a measured fabrication (the chart, the Pine editor, the studies,
the status call, the quote feed, the compile result). Deliberately excluded: `proposal`,
`strategy`, `script`, `plan`, `picture`, `read`, `data`, `table`, `summary` — a message
presenting the model's own composition ("Here's the proposal exactly as specified:",
"Here's a clean 20/50 SMA crossover strategy") is the false-positive class the corpus
actually contains, and the noun is what separates it from a tool result.
"""

_RESULT_IS_HERE = re.compile(
    r"\bHere(?:['\u2019]s|\s+is|\s+are)\b[^.!?\n]{0,60}?\b" + _RESULT_NOUN + r"\b",
    re.I,
)
"""Report shape 1: a tool result is presented *here*, in this message.

The deictic "Here's" plus a result noun within short range — "Here's the AMD chart",
"Here's exactly what's in the Pine editor right now", "Here's what the status call
returns". The 60-char bound keeps the noun attached to the presentation rather than
matching across an unrelated clause.
"""

_REPORTED_COMPLETE = re.compile(
    r"(?:^(?:Done|Checked|Pulled|Fetched|Read|Loaded|Retried|Verified|Ran|Compiled|"
    r"Captured|Switched|Grabbed|Queried|Injected|Synced|Scanned|Confirmed|"
    r"Cache\s+miss|Still\s+failing|Same\s+result|It\s+worked)\b"
    r"(?=\s*(?:[\u2014\u2013,.;:!-]|$)"
    r"|\s+(?:and|against|on|in|with|cleanly|successfully|clean)\b)"
    r"|\b(?:connection|gateway|feed|tools?|sidecar)(?:['\u2019]s|\s+is|\s+are)\s+live\b"
    r"|\bnow\s+cached\b"
    r"|\bNow\s+I\s+have\b)",
    re.I,
)
"""Report shape 2: the segment opens with a completed act, or asserts fetched state.

Segment-initial participles only, with `_ACTION_DONE`'s lookahead trick: punctuation or a
connective after the participle makes it a predicate ("Compiled.", "Loaded and compiled —
no errors", "Confirmed against the live book"); anything else makes it attributive or
imperative, which is why determiners are deliberately absent from the connective list —
"Read the docs before trading" and "Checked boxes don't mean settled orders" must stay
out. The unanchored tail shapes — "connection is live", "now cached", "Now I have" — are
each earned by a measured fabrication and carry no innocent reading in the corpus.
"""

_USER_SOURCE = re.compile(
    r"\byou(?:r)?\s+(?:sent|pasted|attached|uploaded|shared|screenshot|image|file)\b"
    r"|\bthe\s+(?:image|screenshot|file|chart)\s+you\b",
    re.I,
)
"""The intent names user-supplied content — "let me read the chart you sent".

DATA INTEGRITY's second guaranteed source: describing what the user handed over needs no
tool, so an intent aimed at it commits to nothing this detector should police. The call
site also clears the whole turn when image blocks are attached; this veto is for the turn
where the text names the source and the image arrived in an earlier message.
"""

_NOT_PERFORMED = re.compile(
    r"\b(?:did\s*n[o\u2019']t|didn['\u2019]t|could\s*n[o\u2019']t|couldn['\u2019]t|"
    r"could\s+not|was\s*n[o\u2019']t|wasn['\u2019]t|failed\s+to|unable\s+to|never|"
    r"without|would\s+(?:have|be|look)|if\s+I\s+had|from\s+memory|hypothetical)\b",
    re.I,
)
"""Non-performance readings of a report segment, checked sentence-wide.

Deliberately excludes bare `no`/`not`: "Loaded and compiled — no errors" is T6's exact
wording, and an honest-sounding negation inside a fabricated report must not clear it.
Only phrases that negate the *performance* qualify ("I couldn't capture the screenshot"),
plus the counterfactuals ("it would be here within a second").
"""

_REPORT_UNREALIZED = re.compile(
    r"\b(?:if|unless|once|when|would|will|shall|expect(?:ed|ing)?|afterwards|"
    r"next\s+time)\b",
    re.I,
)
"""A report segment describing an unrealized outcome, checked sentence-wide.

Added by the 2026-08-12 review after two executed false positives: "If the connection is
live, the inject will go through" and "Here's the status I'd expect to see once the
gateway is up" are honest announce-then-explain turns, and the conditional/future words
are what mark them. Sentence-wide because the marker can sit on either side of the report
match ("I'd expect" follows "Here's the status"). None of the measured fabrication
reports carries any of these words — re-measured after adding, same fires.
"""

_OWN_COMPOSITION = re.compile(
    r"\b(?:updated|revised|rewritten|modified|adjusted|tweaked|drafted|proposed|new)\s+"
    + _RESULT_NOUN,
    re.I,
)
"""The result noun names the model's own edit, not a tool's return.

"Here's the updated indicator:" after "I'll switch the RSI length to 9 for you" is the
model editing a script it wrote — an ordinary zero-tool flow the Copy/Inject buttons
exist to serve, verified to fire before this veto existed. A modifier of authorship in
front of the noun is what separates it from "Here's the ZZZ chart" (a claimed
observation); no measured fabrication carries one.
"""


def _claims_completed_action(text: str) -> str | None:
    """Return the segment reporting a completed tool action, or None if there is none.

    T7's shape, and the third member of the detector family: `_claims_completed_proposal`
    asserts *claimed a proposal ⇒ called a proposal tool*, `_claims_fresh_book_check`
    asserts *claimed a lookup ⇒ called a lookup tool*, and this one asserts *reported an
    action ⇒ some tool actually ran this turn*. Same architecture, deliberately — **the
    trigger is textual, the verdict is evidence**: this function only decides that an
    action was reported; whether anything ran is settled at the call site by
    `called_tools`, and by nothing else.

    The claim needs two halves in order: a lead-in to act (`_INTENT_PREAMBLE`) and a
    completion report (`_RESULT_IS_HERE` / `_REPORTED_COMPLETE`) at or after it. The
    report requirement is the largest veto there is: measured against the live store
    (225 assistant messages), intent alone appears in dozens of honest turns — "say the
    word and I'll pull your positions" — and every one stops before reporting an outcome.
    Measured the same way as the siblings, before shipping: **20 fires on zero-tool
    turns, every one an individually verified fabrication (2026-06-24 → 2026-08-12, nine
    sessions, both TV instances and the 2026-07-28 book failure among them); 22 turns
    matched textually and were cleared by their real tool calls; zero fires outside the
    verified set.** See `docs/plans/2026-08-12-t7-fabrication-guardrail.md`.

    What it gives up for that, knowingly:
      - a turn where *some* tool ran and a *different* claimed action did not ("called
        `get_positions`, also said it captured a screenshot"). The verdict is the whole
        turn's tool set, so one real call clears every claim in the message. Closing it
        needs a verb→tool map, and the TradingView surface has no closed declaration to
        pin one against the way `_BOOK_READING_TOOLS` pins against the toolkit.
      - a report with no announced intent before it, and a "Here's the ..." whose noun is
        outside `_RESULT_NOUN` — the two measured misses ("Here's the picture:" into a
        fabricated account table; "ZZZ close = 504.17" with no report verb at all). The
        intent half and the noun gate are what keep the model's own compositions out;
        widening either trades a measured 0-false-positive detector for a speculative one.
      - order-domain vocabulary, deliberately: staging, proposing and book-checking
        belong to the two siblings, and this detector standing down there is what keeps
        one lie from earning three corrections.

    Args:
        text: The assistant text about to be, or already, shown to the user.

    Returns:
        The report segment — for the log line only, never for storage: it is live
        conversation text and the decision row must stay free of it.
    """
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(_MARKUP.sub(" ", text))]
    start_index: int | None = None
    tail_offset = 0
    for i, segment in enumerate(segments):
        if not segment or segment.endswith("?"):
            continue
        hit = _INTENT_PREAMBLE.search(segment)
        if hit is None or _USER_SOURCE.search(segment):
            continue
        if _GOVERNING_OPERATOR.search(segment[: hit.start()]):
            continue
        start_index, tail_offset = i, hit.end()
        break
    if start_index is None:
        return None
    for i in range(start_index, len(segments)):
        # Within the intent's own segment, only text after the preamble can report.
        segment = segments[i][tail_offset:].strip() if i == start_index else segments[i]
        if not segment or segment.endswith("?"):
            continue
        # Sentence-wide vetoes: non-performance, unrealized outcomes, user-supplied
        # content, and the model's own compositions can sit on either side of the match.
        if _NOT_PERFORMED.search(segment) or _REPORT_UNREALIZED.search(segment):
            continue
        if _USER_SOURCE.search(segment) or _OWN_COMPOSITION.search(segment):
            continue
        match = _RESULT_IS_HERE.search(segment) or _REPORTED_COMPLETE.search(segment)
        if match is None:
            continue
        # Left-of-match only, deliberately (2026-08-12 review): sentence-wide, `already`
        # cleared "Done — the 6-month daily data is already cached", a one-adverb variant
        # of a measured fabrication — the adverb modifies the state, not the turn. Left
        # of the match it still reads as a recap ("Earlier I pulled the chart state —
        # here's the chart as it stood then").
        if _PAST_TURN.search(segment[: match.start()]):
            continue
        return segment
    return None


_VERBATIM_RESULT_CLAIM = re.compile(
    r"\b(?:raw|verbatim|actual|exact|explicit)\s+"
    r"(?:tool\s+|API\s+)?(?:call|result|output|response|payload|return)\b",
    re.I,
)
"""A block is being vouched for as a tool's own words — "the raw tool result, verbatim".

`raw`/`explicit` are the measured instance's own words; `verbatim`/`actual`/`exact` are
their nearest synonyms and each carries a must-fire fixture. `unmodified`/`untouched`
were trimmed by the 2026-08-12 review: no measured instance, no fixture, and every
alternate here widens the surface of an unhedged fabrication accusation — the same
every-alternate-earned rule `_RESULT_NOUN` states.
"""

_RESULT_CLAIM_EXCUSE = re.compile(
    r"\b(?:example|sample|illustrat\w*|hypothetical|mock|template|format|schema|"
    r"would\s+look|looks\s+like|for\s+instance)\b",
    re.I,
)
"""The honest readings: explaining a format or showing a hypothetical is not vouching."""


def _claims_verbatim_tool_result(text: str) -> str | None:
    """Return the sentence vouching for a fenced block as a raw tool result, or None.

    The fourth detector, for the shape the other three cannot see because it has no
    intent preamble and no order vocabulary: a fenced payload presented as a tool's own
    return. The measured instance (2026-07-10) is the worst message in the store — asked
    to call `quote_get` explicitly and show the raw result *as an audit*, the model
    emitted a fenced JSON block with an invented `_source` field and asserted it
    confirmed the numbers, in a turn with zero tool rows. A fabricated audit trail,
    produced on demand, defeating exactly the verification move the user made.

    Same split as the siblings: this function decides only that a block was vouched for;
    whether any tool ran is settled at the call site. The fence requirement is structural
    — the vouching phrase without a fence presents nothing ("Here's the raw tool result,
    verbatim:" followed by silence claims no content), and no honest zero-tool message in
    the corpus fences a payload while vouching for its provenance.

    Args:
        text: The assistant text about to be, or already, shown to the user.

    Returns:
        The vouching sentence — for the log line only, never for storage.
    """
    if "```" not in text:
        return None
    for sentence in _SENTENCE_SPLIT.split(_MARKUP.sub(" ", text)):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        hit = _VERBATIM_RESULT_CLAIM.search(sentence)
        if hit is None or _RESULT_CLAIM_EXCUSE.search(sentence):
            continue
        # Same vetoes as the action sibling (2026-08-12 review): a vouching phrase about
        # an earlier turn ("Earlier I showed you the raw tool result above") or inside a
        # non-performance reading ("I can't show the actual output — no tool ran") is a
        # recap or an honest refusal, not a claim about this turn's block.
        if _PAST_TURN.search(sentence) or _NOT_PERFORMED.search(sentence):
            continue
        if not _GOVERNING_OPERATOR.search(sentence[: hit.start()]):
            return sentence
    return None


def _proposal_defect(kind: str, inputs: dict) -> str | None:
    """Return why a proposal must be rejected, or None when it carries no defect.

    `strict: true` already guarantees the types, enums, required keys and closed objects of
    `proposal_tools.py`'s schemas, so nothing here re-checks those. Five things it cannot
    express are checked here instead, because retiring `order_proposal_schema.py` would
    otherwise drop each guarantee silently. Each applies to every proposal kind that
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
    5. `outside_rth` is True, False or None (2026-09-04). Strict mode types it on the API
       path; this is the guard for every other caller, because `bool("false")` is True and
       a coerced order attribute is a fabricated one.

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

    if "outside_rth" in inputs:
        outside_rth = inputs["outside_rth"]
        if outside_rth is not None and not isinstance(outside_rth, bool):
            return f"outside_rth={outside_rth!r} must be true, false or null"

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


def _ibkr_unavailable() -> str | None:
    """A `tool_result` explaining why IBKR is off limits, or None when it is not.

    Stage 5 of `docs/plans/2026-08-06-gateway-session-lifecycle-owner.md`. Suspension is
    **total** by decision: while a login or a recovery is in progress, nothing touches the
    gateway — the pollers, the WebSocket, both ticklers, and the model's tools.

    The reason is not tidiness. IBKR documents that *"if the gateway has not received any
    requests for several minutes an open session will automatically timeout"*
    (https://ibkrcampus.com/docs/web-api/v1/endpoints/session/ping-the-server.md), so every
    request renews the session — which means an exception carved out for "just the agent"
    reintroduces exactly the traffic that made `POST /logout` unable to clear a borrowed
    session on 2026-08-05. One rule with no exceptions is the mechanism.

    Returning an honest `tool_result` rather than raising is deliberate, and matches how
    `_proposal_defect` already handles a rejected proposal: the model is told plainly what
    did not happen and why, so it can say so instead of inventing a plausible answer or
    retrying into a session that is still being established.

    Only `AUTHENTICATING` and `RECOVERING` block. A `FREE` or `DOWN` gateway is *not*
    blocked here — the call simply fails with a 401 or a connection error, and that error
    is more informative to the model than a refusal would be.
    """
    from claudia.gateway_session import get_session

    state = get_session().state()
    if state.may_call_ibkr:
        return None
    return (
        "IBKR is temporarily unavailable: "
        f"{state.detail} No request was sent to the gateway, so this tool returned no "
        "data — nothing was read and nothing was changed. Tell the user the IBKR session "
        "is being established and that they should retry once it is live. Do not guess "
        "the answer and do not retry this tool in the same turn."
    )



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
        replaying them would send orphaned `tool_result` blocks and the API would 400.

        This used to add "the assistant's text already summarises what each tool returned",
        which was the justification for dropping them. It is false — measured 2026-08-11,
        see `ClaudIAAgent._called_tool_records`. The drop itself stands (the 400 is real);
        what compensates for it is the called-tool ledger on the operator channel, which
        tells the model the payloads are gone rather than pretending its prose replaced them.
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
        # Anthropic API 400 errors. What the model loses by this is restored as
        # the called-tool ledger, not as a summary — see the docstring.
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
        # Operator-channel notes awaiting delivery as `role: "system"` messages. Written
        # only by _emit_guardrail_notice, drained by _append_operator_message on the next
        # turn — deliberately NOT cleared per turn like _pending_proposal, since a note
        # queued by a turn that then raised must still reach the model.
        self._pending_operator_notes: list[str] = []

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

        # After the images, so the current user turn is still `messages[-1]` above.
        self._append_operator_message(messages)

        system_blocks = self._get_system_blocks()

        # Multi-turn tool loop
        full_response_text = ""
        # Every tool that actually ran this turn. `tool_calls` below is per-iteration and is
        # cleared on each pass, so it cannot answer "did a lookup happen anywhere in this
        # turn?" — which is the whole question `_claims_fresh_book_check` is checked against.
        called_tools: set[str] = set()

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
                    elif (blocked := _ibkr_unavailable()) is not None:
                        # Only this branch reaches IBKR. Local tools and TradingView tools
                        # are deliberately NOT gated — they touch nothing the gateway owns,
                        # and refusing them during a login would be a restriction with no
                        # safety argument behind it.
                        result_text = blocked
                    else:
                        result_text, _ = await asyncio.to_thread(
                            self._toolkit.execute, tc["name"], tc["input"]
                        )
                    step.output = result_text

                called_tools.add(tc["name"])
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
        # The sibling detectors' claimed sentences, kept for the general action detector
        # below: it stands down only when its own report overlaps one of them — the same
        # sentence corrected twice is noise, a second distinct lie is not (2026-08-12
        # review; the earlier turn-wide boolean let the second lie stand).
        claim: str | None = None
        stale: str | None = None

        # Persist final assistant message
        msg_id = self._store.add_message(
            self._session_id, "assistant", display_text
        )

        # Render text response
        if display_text:
            await self._sink.send_message(display_text)

        # Render the staging button for whichever proposal was recorded — then assert it
        # happened. `rendered` is set on the line *after* a specific await returns, never
        # after the dispatch as a whole, because the failure this closes was a silent skip:
        # a falsy parsed value matched no branch, raised nothing, and left the model's
        # "here's your button" standing with no button and no decision row. A try/except is
        # necessary but not sufficient — the guarantee has to be a positive assertion.
        if proposal is not None:
            render = {
                "order": self._sink.send_order_proposal,
                "cancel": self._sink.send_cancel_proposal,
                "modify": self._sink.send_modify_proposal,
            }.get(kind or "")
            rendered = False
            if render is None:
                log.error(
                    "Proposal kind %r routes to no renderer — nothing was rendered", kind
                )
            else:
                try:
                    await render(proposal)
                    rendered = True
                except Exception:
                    log.exception(
                        "Proposal render failed (kind=%s, session=%s)", kind, self._session_id
                    )

            if not rendered:
                await self._emit_guardrail_notice(kind or "unknown", msg_id)
            else:
                # Log the user-directed trade proposal for future recall. Only on a real
                # render: `trade_proposed` and its siblings must keep meaning "a button was
                # shown", or every historical row and regression baseline silently changes
                # meaning. A failure gets `proposal_render_failed` instead.
                self._log_proposal(
                    display_text,
                    proposal if kind == "order" else None,
                    msg_id,
                    cancel_proposal=proposal if kind == "cancel" else None,
                    modify_proposal=proposal if kind == "modify" else None,
                )
        elif display_text:
            # Nothing was recorded this turn, so the invariant above had nothing to assert —
            # which is precisely how the 2026-07-28 failure passed it. That turn called no
            # tool at all and still told the user "Cancel staged — button's above". The
            # claim is textual and the verdict is `proposal is None`; see
            # `_claims_completed_proposal` for the shapes and the corpus measurement.
            claim = _claims_completed_proposal(display_text)
            if claim is not None:
                await self._emit_unbacked_claim_notice(msg_id, claim)

        # Outside the proposal branches on purpose: claiming a lookup and claiming a
        # proposal are independent acts, and the 2026-07-28 turn committed both in one
        # message. A turn can also propose perfectly and still describe a book check it
        # never made, which neither branch above would ever look at.
        if display_text and not (called_tools & _BOOK_READING_TOOLS):
            stale = _claims_fresh_book_check(display_text)
            if stale is not None:
                await self._emit_stale_book_claim_notice(msg_id, stale)

        # The two general detectors — T7's shape — run last, gated on `not called_tools`:
        # with any real call the turn's report may be grounded, and that give-up is
        # documented on the detector. The payload check runs first and is deliberately
        # NOT gated on images or on the siblings (2026-08-12 review): no uploaded image
        # can ground the provenance of a fenced "raw tool result", and a book notice
        # about sentence A does not correct a constructed payload in sentence B.
        if display_text and not called_tools:
            shown = _claims_verbatim_tool_result(display_text)
            if shown is not None:
                await self._emit_unbacked_result_notice(msg_id, shown)
            elif not images:
                # `not images` clears the ACTION detector only: a screenshot the user
                # dragged in is DATA INTEGRITY's second guaranteed source, and describing
                # it needs no tool. The overlap test replaces a turn-wide stand-down
                # (2026-08-12 review): the 2026-07-28 message narrates a book check and a
                # staging in ONE sentence — correcting that sentence twice devalues both
                # notices — but a second, distinct lie elsewhere in the message still
                # earns its own correction.
                narrated = _claims_completed_action(display_text)
                if narrated is not None and not any(
                    s is not None and (narrated in s or s in narrated)
                    for s in (claim, stale)
                ):
                    await self._emit_unbacked_action_notice(msg_id, narrated)

    async def _emit_correction(
        self,
        msg_id: int,
        claim: str,
        *,
        log_message: str,
        notice: str,
        decision_type: str,
        summary_text: str,
        operator_note: str,
        metadata: dict | None = None,
    ) -> None:
        """Contradict one unbacked claim on all four surfaces, in the one safe order.

        The four claim detectors each need their own words, their own decision type and
        their own operator note — that separateness is deliberate and is preserved in the
        constants each caller passes. What is NOT worth repeating is the sequence, and
        before 2026-08-12 it existed in four copies with no test pinning it.

        **The order is the safety property: persist, record, queue, then display.** The
        store has shown no fault; the sink is the surface that can fail. Display last means
        a failing sink raises *after* the record exists, so a correction can never be lost
        because the chat feed broke. A transposition here would silently reintroduce
        exactly that, in exactly one shape — which is why the order now lives once and is
        asserted by `test_every_correction_persists_before_it_displays`.

        Args:
            msg_id: The assistant message whose text carries the claim.
            claim: The offending sentence or segment. **Logged only** — live conversation
                text, kept out of the decision row and every surface that leaves this
                machine.
            log_message: Printf-style prefix taking `claim` as its single `%r` argument.
            notice: The user-facing correction, persisted and displayed.
            decision_type: The row type, distinct per shape so the log stays separable.
            summary_text: The decision row's static summary. Never interpolates `claim`.
            operator_note: The non-forgeable `role:"system"` body for the next turn.
            metadata: Optional structured fields for the decision row. Only the
                render-failure shape uses it (`{"kind": ...}`); `add_decision` already
                defaults it to None, so the four claim shapes write exactly the row they
                always did.
        """
        log.warning(log_message, claim)
        self._store.add_message(self._session_id, "assistant", notice)
        self._store.add_decision(
            session_id=self._session_id,
            decision_type=decision_type,
            summary_text=summary_text,
            message_id=msg_id,
            metadata=metadata,
        )
        self._pending_operator_notes.append(operator_note)
        await self._sink.send_message(notice)

    async def _emit_stale_book_claim_notice(self, msg_id: int, claim: str) -> None:
        """Contradict a claimed order-book check that no tool call backs.

        The third member of the family, kept separate from the other two for the same
        reason they are separate from each other: this one means "the lookup never ran",
        which needs its own words to the user, its own decision type and its own operator
        note. Unlike those two it is not mutually exclusive with them — a single message can
        both narrate a staging and narrate the lookup that supposedly justified it, and
        each false claim gets its own correction rather than one covering for the other.

        Surface order and its safety argument live once, in `_emit_correction`.

        Args:
            msg_id: The assistant message whose text carries the unverified claim.
            claim: The offending sentence. **Logged only** — live conversation text, kept
                out of the decision row and every surface that leaves this machine.
        """
        await self._emit_correction(
            msg_id, claim,
            log_message="Book-check claim with no order-book tool called: %r",
            notice=_STALE_BOOK_CLAIM_NOTICE,
            decision_type="book_claim_unverified",
            summary_text=(
                "assistant text claimed a live-order check but no order-book tool was "
                "called — the stated order state is unverified"
            ),
            operator_note=_STALE_BOOK_CLAIM_OPERATOR_NOTE,
        )

    async def _emit_unbacked_action_notice(self, msg_id: int, claim: str) -> None:
        """Contradict a reported action that no tool call backs — T7's shape.

        The fourth member of the family, and the general case: it means "the action never
        ran", where its siblings mean "the proposal was never made" and "the lookup never
        ran". It stands down when either sibling has already corrected the turn (the call
        site's `corrected` gate), because the measured overlap is a single sentence
        committing two lies — two corrections for two claims, never three.

        Surface order and its safety argument live once, in `_emit_correction`.

        Args:
            msg_id: The assistant message whose text carries the unbacked report.
            claim: The offending segment. **Logged only** — live conversation text, kept
                out of the decision row and every surface that leaves this machine.
        """
        await self._emit_correction(
            msg_id, claim,
            log_message="Action reported with no tool call this turn: %r",
            notice=_UNBACKED_ACTION_NOTICE,
            decision_type="action_claim_unbacked",
            summary_text=(
                "assistant text reported a completed action but no tool was called in "
                "that turn — nothing it described was observed"
            ),
            operator_note=_UNBACKED_ACTION_OPERATOR_NOTE,
        )

    async def _emit_unbacked_result_notice(self, msg_id: int, claim: str) -> None:
        """Contradict a fenced block vouched for as a tool result that no tool produced.

        Kept separate from `_emit_unbacked_action_notice` for the same reason the
        siblings are separate from each other: "the payload is constructed" needs its own
        words to the user, its own decision type and its own operator note. This is the
        audit-defeating shape — the measured instance answered a user's explicit
        verification request with a manufactured payload — so the specific correction
        outranks the generic one at the call site.

        Args:
            msg_id: The assistant message whose text vouches for the block.
            claim: The vouching sentence. **Logged only**, as with its siblings.
        """
        await self._emit_correction(
            msg_id, claim,
            log_message="Constructed payload presented as a tool result: %r",
            notice=_UNBACKED_RESULT_NOTICE,
            decision_type="result_claim_unbacked",
            summary_text=(
                "assistant text presented a constructed block as a raw tool result — "
                "no tool was called in that turn"
            ),
            operator_note=_UNBACKED_RESULT_OPERATOR_NOTE,
        )

    async def _emit_unbacked_claim_notice(self, msg_id: int, claim: str) -> None:
        """Contradict a staging claim that no tool call backs, on all three surfaces.

        The sibling of `_emit_guardrail_notice`, and deliberately not merged with it: that
        one means "accepted but not rendered" and this one means "never proposed at all".
        They need different words to the user, a different decision type, and a different
        operator note, and a turn can only ever be in one of the two states — a recorded
        proposal takes the render path, so this branch runs only when none exists.

        Surface order and its safety argument live once, in `_emit_correction`.

        Args:
            msg_id: The assistant message whose text carries the unbacked claim.
            claim: The offending sentence. **Logged only.** It is live conversation text, so
                it stays out of the decision row, out of the session report and out of any
                surface that leaves this machine.
        """
        await self._emit_correction(
            msg_id, claim,
            log_message="Unbacked staging claim, no proposal tool called: %r",
            notice=_UNBACKED_CLAIM_NOTICE,
            decision_type="proposal_claim_unbacked",
            summary_text=(
                "assistant text claimed a completed order action but no proposal tool was "
                "called — nothing staged"
            ),
            operator_note=_UNBACKED_CLAIM_OPERATOR_NOTE,
        )

    async def _emit_guardrail_notice(self, kind: str, msg_id: int) -> None:
        """Contradict a claim the transcript already carries, on every surface that matters.

        Three surfaces, because the 2026-07-17 and 2026-07-24 failures were invisible on
        all three: the user saw prose describing a button and no button, the DB recorded
        nothing at all, and the model went into the next turn still believing it had
        staged something.

        Persistence comes first and the sink call last, deliberately. The sink is the
        component just demonstrated broken in this very turn; the store has shown no fault.
        If `send_message` also raises, the exception surfaces in the chat feed
        (`ChatInterface.callback_exception="summary"`) and the record survives — the reverse
        order would lose the record to protect a surface that fails loudly anyway.

        Args:
            kind: "order", "cancel", or "modify" — the proposal that did not render.
            msg_id: The assistant message whose text carries the unbacked claim.
        """
        # Persisted, not just displayed. session_reporter's anomaly scan reads tool rows
        # only, so an assistant-side failure can never surface there — and with no decision
        # row either, three consecutive 2026-07-17 failures produced a clean session report.
        # The decision row is what puts this in the report's Decisions section; the message
        # row puts it in the FTS index and in the next turn's replayed history.
        #
        # Routed through `_emit_correction` since 2026-08-12: this was the fifth hand-rolled
        # copy of the persist→record→queue→display sequence, and the only one the ordering
        # test did not cover. `claim` is the proposal kind rather than a sentence — this
        # shape has no offending text, the failure is a missing button — so nothing live
        # reaches the log that was not already there.
        await self._emit_correction(
            msg_id,
            kind,
            log_message="Proposal accepted but not rendered (kind=%r)",
            notice=_GUARDRAIL_NOTICE,
            decision_type="proposal_render_failed",
            summary_text=f"{kind} proposal accepted but not rendered — nothing staged",
            operator_note=_OPERATOR_NOTE.format(kind=kind),
            metadata={"kind": kind},
        )

    def _called_tool_records(self) -> str:
        """Return this session's called-tool ledger, or "" when no tool has been called.

        `_history_to_messages` drops tool rows, and its docstring rests on "the assistant's
        text already summarises what each tool returned". That assumption fails. From turn
        N+1 the model holds no payload at all — only its own prose — so it answers
        follow-ups from that prose and confabulates exactly where the prose is thinner than
        the question. Measured 2026-08-11: its own message had recorded study *names* only;
        asked for the settings it produced colours, line widths, precision and "30/70
        bands", terms with zero occurrences in the entire database, all time. Three such
        turns in one session, each with no tool row and a single API round-trip. The
        identical question in a fresh session (empty history) produced 5 tool calls and 10
        of 10 fields exact against the live chart — prose versus no prose was the only
        variable.

        This block does not restore the payloads; it states that they are gone. It covers
        every tool the session called except `PROPOSAL_TOOL_NAMES` — reads and writers
        alike, not just the TradingView ones from that incident, because the blindness is
        generic and an IBKR read is exactly as invisible as a chart read.

        Writers are listed but must never be *re-run* on the strength of it, which is why
        the header's remedy is phrased as a read ("read it again … rather than recalling
        it") and not as "call the tool again". The ledger names `create_price_alert`,
        `delete_alert`, `pine_set_source` and `chart_set_symbol` among others; re-calling
        the first of those creates a second alert nobody asked for. That is the same
        argument the proposal exclusion below rests on, and the first wording of this header
        had not generalised it.

        **Identity only — names, nothing else, and that is a safety boundary.** Never a
        `tool_input_json`, never a `tool_result_json`, never counts or timestamps. Same rule
        `_emission_records` states as "the record answers 'what did you do', never 'what
        were the values'": a tool input can carry an account number, an order id or a
        position, and this text goes into the model's context and the outgoing request body.
        A tool name is safe; anything else added here would be a new exposure.

        `PROPOSAL_TOOL_NAMES` are excluded. Two independent reasons, either sufficient:

        - Even phrased as a read, naming a proposal tool here is wrong: `propose_order` has
          no read to offer, so the only thing a re-call could mean is emitting a second
          staging button the user never asked for — an order-flow action, not a lookup.
        - A proposal call already has its own, more careful section on this same channel
          (`_emission_records`), whose value rests on excluding a render that *failed*. A
          second section naming `propose_order` with no such filter would put that name back
          on the non-forgeable channel for exactly the turns the exclusion exists to cover.

        Byte-stable across calls: `get_called_tool_names` returns distinct names sorted
        alphabetically, so calling a tool again changes nothing. An unstable block would
        rewrite the request body every turn for no reason.

        Returns:
            The header plus one line per tool, or "" when the session has called none that
            qualify — never an empty header, for the reason `_emission_records` gives.
        """
        lines = [
            f"  - {name}"
            for name in self._store.get_called_tool_names(self._session_id)
            if name not in PROPOSAL_TOOL_NAMES
        ]
        if not lines:
            return ""
        return "\n".join([_TOOL_LEDGER_HEADER, *lines])

    def _emission_records(self) -> str:
        """Return this session's proposal-emission record block, or "" when there is none.

        `_history_to_messages` drops tool rows, so from turn N+1 the model has no evidence
        it ever called `propose_order` on turn N — nothing but its own prose to reason from.
        That blindness is what let it insist "the order-modify-proposal is already staged in
        my earlier message — it's sitting in front of you", and eventually quote an order id
        it had invented. This block is the missing evidence, rebuilt from persisted rows on
        every turn.

        Only rendered proposals qualify: the rows come from
        `ConversationStore.get_rendered_proposals`, whose allowlist excludes
        `proposal_render_failed`. Replaying a failed render as an emission record would
        assert, on the channel the model cannot forge, the exact falsehood this guardrail
        removes.

        Identity only — tool, order id, symbol. No prices, quantities, order types, TIFs or
        the model's own free-text `reason`. The record answers "what did you do", never
        "what were the values": anything copyable into a later proposal is a fabrication
        surface, and order parameters are immutable, so a remembered-looking value is worse
        than no value. The order id is the one number allowed through, because returning a
        real, verified id is precisely what removes the pressure to invent one.

        Byte-stable across calls: `get_rendered_proposals` orders by `id`, and every field
        used here is copied verbatim from the row. An unstable block would rewrite the
        request body every turn for no reason.

        Returns:
            The header plus one line per emission, or "" when the session has emitted none —
            never an empty header. A message asserting nothing devalues a channel whose
            worth is that everything on it is load-bearing.
        """
        lines = []
        for row in self._store.get_rendered_proposals(self._session_id):
            tool = _PROPOSAL_DECISION_TOOLS.get(row.get("decision_type") or "")
            if tool is None:
                # Defence in depth: the store already allowlists. If a type ever reaches
                # here unmapped, dropping it under-reports; guessing a tool name would put
                # a fabricated call in front of the model, which is the failure class itself.
                log.warning("Unmapped rendered-proposal type %r", row.get("decision_type"))
                continue
            symbol = (row.get("symbol") or "").strip()
            order = (row.get("metadata") or {}).get("order") or {}
            order_id = str(order.get("order_id") or "").strip()
            if order_id:
                # cancel/modify: the id is the identity; the symbol only disambiguates.
                lines.append(f"  - {tool} for order {order_id}" + (f" ({symbol})" if symbol else ""))
            else:
                # A new-order proposal has no order id — the order does not exist until the
                # user clicks through both gates. The symbol is all the identity there is.
                lines.append(f"  - {tool} for {symbol}" if symbol else f"  - {tool}")
        if not lines:
            return ""
        return "\n".join([_EMISSION_RECORD_HEADER, *lines])

    def _completed_order_records(self) -> str:
        """Return this session's completed-order-action block, or "" when there is none.

        The staging path leaves *nothing* in the transcript: the user clicks a button, the
        gates fire, `order_flow` writes a decision row — no tool call, no assistant message,
        so `_history_to_messages` has nothing to replay. On 2026-07-27 an ES order was
        staged and read back as `Submitted`; minutes later `get_live_orders` failed twice
        with HTTP 500 and ClaudIA, seeing no evidence of the staging anywhere, told the user
        "there is nothing to cancel — the ES order was only ever a staged button". The order
        was live. This block is that missing evidence.

        A record here is a strictly stronger fact than an emission record: a proposal says
        "you offered a button", a completed action says "this write reached IBKR". Both live
        in the same operator message, in separate labelled sections, because they must never
        be read as the same claim.

        Each line carries the read-back verdict verbatim, and the two verdicts are worded to
        fail in opposite directions:

        - `readback_confirmed: true` — CONFIRMED, with the state observed. The line that
          contradicts a denial.
        - `readback_confirmed: false` — the dispatch still reached IBKR (the row exists only
          because the dispatch call returned), so the line says so, states what was observed
          or that nothing was, and says the order may be live and must be verified. Calling
          it confirmed would invent a working order; leaving it out would re-create the
          denial this whole change exists to stop. It is neither.

        Identity and observed state only — no prices, quantities, order types or TIFs, even
        though `metadata["proposal"]` holds all of them. Same reasoning as `_emission_records`:
        anything copyable into a later proposal is a fabrication surface.

        Byte-stable across calls: rows come back ordered by `id` and every field is copied
        verbatim from the row.

        Returns:
            The header plus one line per completed action, or "" when there are none.
        """
        lines = []
        for row in self._store.get_completed_order_actions(self._session_id):
            verb = _COMPLETED_ACTION_VERBS.get(row.get("decision_type") or "")
            if verb is None:
                # Defence in depth: the store already allowlists. Dropping an unmapped type
                # under-reports; guessing a verb would put a fabricated action in front of
                # the model, which is the failure class itself.
                log.warning("Unmapped completed order action %r", row.get("decision_type"))
                continue
            meta = row.get("metadata") or {}
            order_id = str(meta.get("ibkr_order_id") or "").strip()
            symbol = (row.get("symbol") or "").strip()
            # No id is the least verifiable outcome there is, and the one where silence
            # would be most dangerous — it is named, not skipped.
            identity = f"order {order_id}" if order_id else "an order for which IBKR returned no order id"
            if symbol:
                identity += f" ({symbol})"
            state = str(meta.get("readback_order_status") or "").strip()
            if meta.get("readback_confirmed") is True:
                lines.append(
                    f"  - {verb} {identity} — reached IBKR and the read-back CONFIRMED it; "
                    f"observed status {state or 'unrecorded'}."
                )
            else:
                lines.append(
                    f"  - {verb} {identity} — reached IBKR, but the read-back did NOT "
                    f"confirm it; "
                    f"{f'observed status {state}' if state else 'nothing was observed'}. "
                    f"This order may be live: verify it with a fresh tool call, and never "
                    f"tell the user it does not exist."
                )
        if not lines:
            return ""
        return "\n".join([_COMPLETED_ORDER_HEADER, *lines])

    def note_execution(self, report_text: str) -> None:
        """Queue a fill reported by IBKR's WebSocket for the next turn's operator message.

        The execution listener delivers the report to every session as IBKR sends it
        (2026-09-04); this puts the same text — the broker's record, not model output — into
        the non-spoofable channel so the model knows about the fill on the user's next
        message without being asked. Delivered once, then cleared, like every other note.
        """
        self._pending_operator_notes.append(
            "IBKR reported an execution (broker record via the execution WebSocket; not your "
            f"action, not something you placed or verified this turn): {report_text}"
        )

    def _append_operator_message(self, messages: list) -> None:
        """Deliver the operator channel as one `role: "system"` message after the user turn.

        Four payloads share it: the called-tool ledger (`_called_tool_records`), the
        proposal-emission records (`_emission_records`), the completed-order-action records
        (`_completed_order_records`) — all three derived from persisted rows and rebuilt
        every turn — and any queued render-failure notes (`_pending_operator_notes`,
        transient: queued once, delivered once, cleared). They are separate *sources* on
        purpose, since re-deriving records is what makes them cross-turn evidence while a
        note must not re-announce a failure already handled, and separate *sections* because
        "you called this tool and its result is gone", "a button was drawn" and "this write
        reached IBKR" are different facts. They share one *message* because the API's
        placement rule allows only one here.

        Why the system role: anything the model can write, it can forge, so this content
        placed in a user or assistant turn would be indistinguishable from a model-emitted
        look-alike — and the failure being corrected is precisely a model asserting
        something untrue about its own output. A mid-conversation system message cannot be
        spoofed from model output. Supported on claude-opus-4-8 with no beta header.
        Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

        Placement: appended after the current user turn, so the message is last in
        `messages` on the turn's first request and is followed by an assistant turn on every
        later tool-loop pass — both accepted (live-probed 2026-07-27, together with the
        cache-marked form). A system message may not be `messages[0]`; the API rejects that
        with "use the top-level 'system' parameter for the initial system prompt" (same
        probe), which the user-turn guard below makes unreachable. The L3 plan called for a
        record after *each* assistant turn that produced a proposal; that shape is a 400
        ("role 'system' must follow a 'user' message or an 'assistant' message ending in a
        server tool result", probed 2026-07-27) and consolidating here is what replaces it.
        Covered by tests/test_agent.py::test_live_api_accepts_mid_conversation_system_message
        and ::test_live_api_accepts_the_emission_record_channel.

        Prompt caching: everything here lands *after* the replayed history, never inside it,
        so the cached prefix is extended rather than invalidated — which is what makes it
        safe to rebuild the records on every turn even though their content grows.

        Notes are delivered once, best-effort: if the request carrying one fails outright it
        is not re-queued. Accepted, because it is the non-spoofable channel and not the only
        one — `_emit_guardrail_notice` also persists the notice as an assistant row, which
        `_history_to_messages` replays on every subsequent turn. Records need no such
        fallback: they are re-derived from the store next turn regardless.

        Args:
            messages: This turn's message list, ending in the current user turn. Mutated
                in place.
        """
        parts: list[str] = []
        # First, deliberately: the ledger is standing background context about the whole
        # session rather than a claim about any one turn, so it is the least urgent of the
        # four and sits furthest from where generation resumes.
        ledger = self._called_tool_records()
        if ledger:
            parts.append(ledger)
        records = self._emission_records()
        if records:
            parts.append(records)
        # After the proposals, deliberately: a completed action is the stronger fact of the
        # two ("this reached IBKR" vs "a button was drawn"), and the later a section sits,
        # the closer it is to where generation resumes.
        completed = self._completed_order_records()
        if completed:
            parts.append(completed)
        # Notes last: a note contradicts the turn immediately preceding this one and is the
        # most urgent of the four, so it sits closest to where generation resumes.
        parts.extend(self._pending_operator_notes)
        if not parts:
            return
        if messages and messages[-1]["role"] == "user":
            # One message however many payloads there are: two consecutive system messages
            # would put the second one after a system turn rather than a user turn, which
            # is outside the probed placement rule. Joining is never worse and keeps the
            # shape identical to the one the live check covers.
            #
            # `messages` is annotated bare (as in _with_history_cache_marker) because a
            # system message has no MessageParam literal — the SDK's param types model only
            # user/assistant. The role is correct on the wire (probed 2026-07-27); this
            # file builds every request body as plain dicts for that reason.
            messages.append({"role": "system", "content": "\n\n".join(parts)})
        else:
            # Unreachable: handle_message persists the user turn before reading history, so
            # it is always the last row. Dropped rather than misplaced anyway, because a
            # system message in the wrong position is a 400 that takes down the whole turn
            # — and the notice has already reached the user and the decisions table, while
            # the records are re-derived from the store on the next turn.
            log.error(
                "Operator note dropped: last message is %r, not a user turn",
                messages[-1]["role"] if messages else None,
            )
        self._pending_operator_notes.clear()

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

        A refusal here deliberately does **not** raise the render-completion guardrail
        notice (`_emit_guardrail_notice`), for three reasons. Its text — "ClaudIA accepted
        the proposal but it could not be rendered" — would be false, and shipping a false
        correction is the very defect class this guardrail exists to remove. Its decision
        type, `proposal_render_failed`, means "accepted but not rendered" and would stop
        meaning that. And the timing is structurally different: a refusal is returned
        *before* the model writes its user-facing text, so the model can and must say what
        went wrong (the string below tells it to), whereas a render failure happens *after*
        that text is already on screen and can only be contradicted. Pinned by
        tests/test_agent.py::test_rejected_proposal_does_not_raise_the_render_guardrail.
        Residual, accepted: a refusal reaches the user only through the tool-step pane.

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
            # Belt and braces behind `_fts_query`'s sanitisation. This method's contract is
            # that it never raises — the store escaping one used to take down the entire
            # turn, and a search is the least important thing in a session to fail hard on.
            try:
                results = self._store.search_messages(query, max_results=5)
            except Exception as exc:
                log.warning("Past-conversation search failed for %r: %s", query, exc)
                return (
                    f"The conversation search failed ({exc}). Nothing was searched — this "
                    f"is not evidence that the topic was never discussed. Say so plainly "
                    f"rather than concluding anything from it."
                )
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
