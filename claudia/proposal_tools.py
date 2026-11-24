"""Strict tool definitions for order proposals — declaration only, no execution.

Replaces the fenced ```order-proposal block. Anthropic's agent-design guidance:
promote an action to a dedicated tool when you need to gate, render, or audit it —
order proposals need all three. `strict: true` guarantees `tool_use.input` validates
exactly, which makes the four crash vectors in the 2026-07-27 design doc structurally
impossible rather than merely rejected.

These tools reach NOTHING. They record a proposal for the UI layer to render. Placing
an order still requires a physical button click plus Gate 1 (Touch ID) and Gate 2
(AppKit dialog) — see Hard Rule 1 in CLAUDE.md.

Live API probe results — the single record for this module
--------------------------------------------------------------------------------------
2026-07-27, `claude-opus-4-8`, `max_tokens=1`, all three tools registered together (the
combined payload matters: the `additionalProperties` rejection below does not appear when
tools are probed one at a time). Comments elsewhere in this file point here rather than
restating any of it::

    400       exclusiveMinimum on quantity
              -> tools.0.custom: For 'number' type, property 'exclusiveMinimum'
                 is not supported
    400       previous_values as a free-form map (additionalProperties: true)
              -> tools.2.custom: For 'object' type, 'additionalProperties: true' is
                 not supported. Please set 'additionalProperties' to false
    ACCEPTED  anyOf nullability          (used — documented form)
    ACCEPTED  ["number","null"] type-array (NOT used — accepted but unspecified)
    ACCEPTED  minLength on strings       (NOT used — see "Deliberate omissions")
    ACCEPTED  minItems: 1 on changes     (used)

2026-10-18, gap #36 — `propose_bracket` and `propose_attach` added. `claude-opus-4-8`
(`claude-opus-5`, `claude-fable-5`, `claude-fable-5-1`: identical), `max_tokens=1`, 14
requests; the full table is in the git-ignored probe folder
(`docs/plans/probes/2026-10-17-bracket-live/strict-union-limit-probe.md`)::

    400       the five tools strict — 22 union-typed parameters
              -> Schemas contains too many parameters with union types (22 parameters
                 with type arrays or anyOf) ... (limit: 16 parameters with unions)
              The cap is a TOTAL across every strict schema in one request (the page:
              "Parameters with union types | 16"); each nullable field is one union.
              The three original tools carry 10; the child's three nullables and the
              bracket parent's four add 12. Reproduced on ClaudIA's exact production
              list (44 core + 5 local + 5 proposal tools) — every request would fail.
    400       every four- or five-strict-tool layout with the children stripped to
              14–16 unions, `children` folded onto propose_order included
              -> The compiled grammar is too large ... reduce the number of strict tools
                 (the page's "additional internal limits on the compiled grammar size",
                 no published number)
    ACCEPTED  the three originals strict (10) + the two new tools NON-strict, shapes
              verbatim                                   (used — see below)
    ACCEPTED  children folded onto a strict propose_order (13) + attach non-strict
              (NOT used — one tool fewer than the design, under an unmeasured ceiling)

So the two bracket tools are declared `strict: False` — the page's own first remedy
("Mark only critical tools as strict … rely on Claude's natural adherence for simpler
tools") — and what strict enforced at the API for them is enforced in-process instead:
`agent._schema_defect` validates the input against the tool's OWN `input_schema` (this
module stays the single declaration) before any field is read, rejecting whole exactly as
`_proposal_defect` does. The loss is constrained decoding: the model CAN emit an invalid
call for those two tools and is refused with a tool result, the path the seven defect
terms already use. Nothing new reaches the store, a card or a button. Non-strict tools do
not count toward the budget, so the three originals keep their known-good 10.

Two findings contradict the published reference. **Unsupported keywords are not stripped
— they are a hard 400**: the SDK's `transform_schema` runs only for
`output_config.format` / `output_format`, never for `tools=`, so hand-written tool dicts
reach the wire verbatim, and a bad keyword fails *every* request rather than a malformed
one. And **the published "not supported" list is unreliable in both directions** —
`minLength` is listed as unsupported yet is accepted, while `exclusiveMinimum` is rejected
though the page names only `minimum`/`maximum`/`multipleOf`. Probe before adding any
keyword; do not infer from the docs.

**Local `jsonschema` validation cannot prove API acceptance.** It accepted both rejected
schemas above, which is how two registration-time defects reached a fully green suite.
That is what `test_live_api_accepts_proposal_tools` exists to catch.

Deliberate omissions
--------------------------------------------------------------------------------------
`minLength: 1` is NOT used on `symbol` / `order_id`, though it was probed and accepted.
The same reference affirmatively lists "String constraints (`minLength`, `maxLength`)" as
not supported, so relying on it is the same class of risk as the `["number","null"]`
type-array — and a stronger one, since here the docs actively contradict observed
behaviour. If enforcement is ever tightened to match the published list it becomes a
registration-time 400 on every request, which is the exact failure this module exists to
prevent. It also bought nothing: `" "` satisfies `minLength: 1`, so blank-symbol
rejection was always a handler obligation (below).

Handler obligations — what this schema does NOT enforce
--------------------------------------------------------------------------------------
The hand validator `claudia/order_proposal_schema.py` has been retired and this schema is
now the single declaration of the contract. Four checks it guaranteed are NOT expressible
here, so `agent.py`'s `_proposal_defect()` carries them explicitly — without it they would
have been silently lost:

1. `quantity > 0` — `0` and `-5` both validate (`exclusiveMinimum` is a 400).
2. `symbol` non-blank — `""` and `"   "` both validate.
3. `order_id` non-blank — `""` and `"   "` both validate (cancel and modify).
4. No duplicate `changes` entries for the same field — `uniqueItems` is an array
   constraint beyond `minItems` 0/1 and is unsupported.

For `propose_bracket` and `propose_attach` (non-strict, see the 2026-10-18 record) the
schema's own guarantees — types, enums, required keys, closed objects — are
`agent._schema_defect`'s to enforce, in-process, before `_proposal_defect` runs; the
obligations above then apply to them as to the strict three.

Conversely, these WERE gaps in the old validator and are now closed structurally — do not
re-add hand checks for them: non-numeric or fractional `quantity`, `inf`/`NaN`/`True`
`quantity` (all rejected by `type: integer`), string prices (crash vector 2 — the old
validator never type-checked `limit_price`/`stop_price` at all), absent or `None` enum
fields, unknown extra keys, and a non-object payload. Note the enums are also
case-sensitive here, where the old validator accepted `"buy"`.

Docs source (correct on `strict` itself, unreliable on the keyword list):
https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""

from __future__ import annotations

from typing import Any

_ACTION: dict[str, object] = {"type": "string", "enum": ["BUY", "SELL"]}
_ORDER_TYPE: dict[str, object] = {"type": "string", "enum": ["MKT", "LMT", "STP", "STOP_LIMIT"]}
_TIF: dict[str, object] = {"type": "string", "enum": ["DAY", "GTC", "IOC", "OPG"]}
_SEC_TYPE: dict[str, object] = {"type": "string", "enum": ["STK", "FUT", "OPT", "FOP", "CASH"]}
# anyOf, not the ["number", "null"] type-array — see the probe record above. A string here
# crashed _format_order_summary's :.2f formatter (crash vector 2); neither branch admits
# "6000.00".
_PRICE: dict[str, object] = {"anyOf": [{"type": "number"}, {"type": "null"}]}
# Nullable here, but REQUIRED NON-NULL on propose_modify — the file's one deliberate
# asymmetry. CLAUDE.md: FUT/FOP modify/cancel must carry a pre-resolved conid, with no
# fallback symbol-based resolution, so modify cannot accept null.
_CONID: dict[str, object] = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

# `number`, since gap #18 (operator 2026-09-28: fractional quantities "as long as IB allows
# it"; built 2026-10-06). IBKR's order body types `quantity` as a double, and whether a
# fraction is permitted is the CONTRACT's own rule — `/iserver/contract/rules` returns
# `fraqTypes` (the order types that may be fractional) and `fraqInt` (the decimals allowed),
# per contract: two EUR pairs permit two decimals on every order type, CHF.USD permits none
# (measured 2026-09-28). `order_flow` reads that rule before Gate 1 and refuses what the
# contract refuses, quoting it; a whole quantity reads nothing. It was `integer` from
# 2026-07-27 to 2026-10-06 because `order_flow` did `int(qty)` and a fractional 1.7 would
# have been SILENTLY truncated to 1 — a mutation of a user-specified parameter; that
# truncation is gone with this change, the body carries the proposed value exactly.
# `number` admits inf, NaN and True, which `integer` refused for free: `_proposal_defect`
# refuses them now, as it already did for every price. No new keyword: `number` is what
# every price field in these schemas already uses, so the strict grammar needs no probe.
#
# Positivity is still NOT enforced: `exclusiveMinimum` is a 400 (see the probe record), so
# the bound lives in the description — which mirrors what the SDK's transform_schema does
# on the parse path, so the model at least sees it. A description is not enforcement; see
# "Handler obligations" above.
_QUANTITY: dict[str, object] = {
    "type": "number",
    "description": (
        "Number of units/contracts, greater than 0. A fractional quantity (0.5 shares, 1000.5 EUR) "
        "is accepted only where IBKR's rules for that contract permit it, checked before staging."
    ),
}

# The plan specified two parallel properties on propose_modify — `changed_fields` (array)
# and `previous_values` (free-form map). The map is a hard 400 (see the probe record
# above): strict mode's mandatory additionalProperties:false makes a free-form map
# inexpressible, since a closed object with no declared properties can hold nothing. So
# the pair collapses into ONE array — two structures describing one fact can disagree,
# which is the very failure class this change exists to remove.
#
# `enum` IS enforced under strict (unlike the numeric constraints), so the model cannot
# invent a field name. New values are deliberately NOT repeated here: they live in the
# top-level fields, which are what actually reach IBKR, so there is nothing to drift.
_MODIFIABLE_FIELD: dict[str, object] = {
    "type": "string",
    "enum": ["limit_price", "stop_price", "quantity", "order_type", "tif", "outside_rth"],
}

# IBKR's `outsideRTH` order attribute (2026-09-04, gap #33). Nullable like the prices: null
# means "the user did not say", and the handler sends nothing for it. The description is the
# ONLY text that reaches the model (a docstring never does), so it carries both the fact that
# makes the field matter and the immutability rule. Sources for the fact:
# docs/order-api-reference.md § Stop orders on US futures.
_OUTSIDE_RTH: dict[str, object] = {
    "anyOf": [{"type": "boolean"}, {"type": "null"}],
    "description": (
        "Allow the order to work outside regular trading hours (IBKR's outsideRTH). It "
        "matters for STP and STOP_LIMIT orders on futures: IBKR simulates those stops and "
        "triggers them only during regular trading hours unless this is true — a GTC stop "
        "on ES without it is not active overnight. Set true only when the user asks for it "
        "(e.g. 'active through the whole electronic session'); set null when the user did "
        "not say — never assume, and never change it on a modify unless asked."
    ),
}
# boolean added 2026-09-04 with `outside_rth` (review #3): a boolean prior value was
# inexpressible, and strict mode would have steered the model into "false" / 0 / null.
_SCALAR: dict[str, object] = {
    "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"}]
}
_CHANGES: dict[str, object] = {
    "type": "array",
    # minItems: 1 probed and accepted. Without it `changes: []` validates, and a modify
    # reaches Gate 2 showing an empty before/after diff. Duplicate entries for one field
    # remain possible — uniqueItems is unsupported (see "Handler obligations" above).
    "minItems": 1,
    "description": "One entry per field being changed, carrying that field's prior value.",
    "items": {
        "type": "object",
        "properties": {
            "field": _MODIFIABLE_FIELD,
            "previous_value": _SCALAR,
        },
        "required": ["field", "previous_value"],
        "additionalProperties": False,
    },
}

# Brackets and attached orders (gap #36, 2026-10-18). A child is what IBKR defines it to be
# — the parent's size, the opposite side, the same contract — so none of that is typed here:
# the model gives a kind, a price, optionally a limit (a stop-limit), its own tif and
# outside_rth. ONE object serves both `propose_bracket.children` and `propose_attach.child`
# (identity asserted by the tests), so a rule added to one cannot be missed by the other.
# Everything the child shares with the parent is derived on the order path from the parent,
# never typed twice: two structures describing one fact can disagree (the `_CHANGES` lesson).
_CHILD_KIND: dict[str, object] = {"type": "string", "enum": ["profit_taker", "stop_loss"]}
# anyOf, the documented nullable form, around the existing enum. Each nullable field here is
# one union-typed parameter — the budget the 2026-10-18 probe record is about.
_TIF_OR_NULL: dict[str, object] = {"anyOf": [_TIF, {"type": "null"}]}
_CHILD: dict[str, object] = {
    "type": "object",
    "description": (
        "One attached order, as IBKR defines it: the parent's size, the opposite side, the same "
        "contract — none of which is typed here. price is the limit for a profit_taker and the "
        "trigger for a stop_loss; limit_price (stop_loss only) turns the stop into a stop-limit; "
        "tif null = the parent's, a stated tif equal to or shorter than the parent's, never longer; "
        "outside_rth null = send nothing."
    ),
    "properties": {
        "kind": _CHILD_KIND,
        "price": {"type": "number"},
        "limit_price": _PRICE,
        "tif": _TIF_OR_NULL,
        "outside_rth": _OUTSIDE_RTH,
    },
    "required": ["kind", "price", "limit_price", "tif", "outside_rth"],
    "additionalProperties": False,
}
_CHILDREN: dict[str, object] = {
    "type": "array",
    "minItems": 1,  # the same probed keyword `_CHANGES` carries
    "description": "The attached orders — a profit taker and/or a stop loss. At least one.",
    "items": _CHILD,
}

# The parent of a bracket IS a propose_order, and the parent of an attach IS the working
# order propose_cancel names: the two tables below are those tools' own, built once and
# reused, so the four schemas cannot drift apart field by field.
_ORDER_PROPERTIES: dict[str, object] = {
    "symbol": {"type": "string"},
    "action": _ACTION,
    "quantity": _QUANTITY,
    "order_type": _ORDER_TYPE,
    "limit_price": _PRICE,
    "stop_price": _PRICE,
    "tif": _TIF,
    "sec_type": _SEC_TYPE,
    "conid": _CONID,
    "outside_rth": _OUTSIDE_RTH,
    "reason": {"type": "string"},
}
_ORDER_REQUIRED: list[str] = [
    "symbol",
    "action",
    "quantity",
    "order_type",
    "limit_price",
    "stop_price",
    "tif",
    "sec_type",
    "conid",
    "outside_rth",
    "reason",
]
_CANCEL_PROPERTIES: dict[str, object] = {
    "order_id": {"type": "string"},
    "symbol": {"type": "string"},
    "action": _ACTION,
    "quantity": _QUANTITY,
    "order_type": _ORDER_TYPE,
    "limit_price": _PRICE,
    "stop_price": _PRICE,
    "tif": _TIF,
    "reason": {"type": "string"},
}
_CANCEL_REQUIRED: list[str] = [
    "order_id",
    "symbol",
    "action",
    "quantity",
    "order_type",
    "limit_price",
    "stop_price",
    "tif",
    "reason",
]

PROPOSE_ORDER: dict[str, object] = {
    "name": "propose_order",
    "description": (
        "Surface a NEW order to the user as a staging button. Call this whenever the user "
        "asks you to place, stage, or propose an order. This tool does not place anything: "
        "it renders a button the user must click, which then requires Touch ID and a "
        "confirmation dialog. Use the user's exact values — never round or adjust."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": _ORDER_PROPERTIES,
        "required": _ORDER_REQUIRED,
        "additionalProperties": False,
    },
}

PROPOSE_CANCEL: dict[str, object] = {
    "name": "propose_cancel",
    "description": (
        "Surface a CANCEL of an existing order as a staging button. Call this whenever the "
        "user asks you to cancel, kill, or pull an order that is already working. order_id "
        "MUST come from a get_live_orders / get_order_status / diagnose_orders call in this "
        "conversation — never from memory. Renders a button; cancels nothing by itself."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": _CANCEL_PROPERTIES,
        "required": _CANCEL_REQUIRED,
        "additionalProperties": False,
    },
}

PROPOSE_MODIFY: dict[str, object] = {
    "name": "propose_modify",
    "description": (
        "Surface a MODIFY of an existing order as a staging button. Call this whenever the "
        "user asks you to change any parameter of an order that is already working — price, "
        "quantity, order type, or time in force. Requires a get_order_status(order_id) call "
        "first in this conversation — it returns the conid and the full current field set. "
        "Copy every field the user did NOT ask to change byte-for-byte from that result. "
        "outside_rth is the exception: IBKR's order status reports it for stocks "
        "(outside_rth) but NOT for futures, so for a futures order take it from what the "
        "user asked when the order was placed or modified in this conversation; if you do "
        "not know, send null — the approval text will warn that the modify resends the "
        "order without the attribute. Renders a button; modifies nothing by itself."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "conid": {"type": "integer"},
            "symbol": {"type": "string"},
            "action": _ACTION,
            "quantity": _QUANTITY,
            "order_type": _ORDER_TYPE,
            "limit_price": _PRICE,
            "stop_price": _PRICE,
            "tif": _TIF,
            "sec_type": _SEC_TYPE,
            "outside_rth": _OUTSIDE_RTH,
            "reason": {"type": "string"},
            # Task 3 resolved the adapter question by deleting the adapter:
            # order_flow._format_modify_summary now reads `changes` directly, so the dict
            # that reaches the render path, the execution core and the decisions table is
            # byte-identical to what the model emitted. Reshaping it in the handler would
            # have put a mutation of an order proposal on the path to Gate 2.
            "changes": _CHANGES,
        },
        "required": [
            "order_id",
            "conid",
            "symbol",
            "action",
            "quantity",
            "order_type",
            "limit_price",
            "stop_price",
            "tif",
            "sec_type",
            "outside_rth",
            "reason",
            "changes",
        ],
        "additionalProperties": False,
    },
}

PROPOSE_BRACKET: dict[str, object] = {
    "name": "propose_bracket",
    "description": (
        "Surface a NEW order WITH attached orders (a bracket) as a staging button: a parent plus "
        "a profit taker and/or a stop loss that IBKR holds and releases only when the parent "
        "fills. Each child is the parent's size, the opposite side and the same contract — give "
        "only its kind, its price(s), and optionally its own tif and outside_rth. Use this "
        "whenever the user asks for a profit taker, a stop loss or a bracket on a new order. "
        "Never propose the exit as a separate standalone order. Renders a button; places nothing."
    ),
    # NOT strict: the API's union budget (the 2026-10-18 probe record). The same closed shape
    # is enforced in-process by `agent._schema_defect`.
    "strict": False,
    "input_schema": {
        "type": "object",
        "properties": {**_ORDER_PROPERTIES, "children": _CHILDREN},
        "required": [*_ORDER_REQUIRED, "children"],
        "additionalProperties": False,
    },
}

PROPOSE_ATTACH: dict[str, object] = {
    "name": "propose_attach",
    "description": (
        "Surface ONE attached order (a profit taker or a stop loss) on an order ALREADY WORKING "
        "at IBKR as a staging button. order_id MUST come from get_live_orders / get_order_status / "
        "diagnose_orders in this conversation — never from memory; copy the parent's fields from "
        "that result so the card shows what the child attaches to. The child is the parent's "
        "size, the opposite side, the same contract — give only its kind, its price(s), and "
        "optionally tif and outside_rth. One child per call: a profit taker AND a stop loss are "
        "two calls. Renders a button; attaches nothing by itself."
    ),
    # NOT strict — the same budget and the same in-process enforcement as propose_bracket.
    "strict": False,
    "input_schema": {
        "type": "object",
        "properties": {**_CANCEL_PROPERTIES, "child": _CHILD},
        "required": [*_CANCEL_REQUIRED, "child"],
        "additionalProperties": False,
    },
}

# Appended last in the agent's tool list, so the cache breakpoint sits on the final entry:
# it moved from propose_modify to propose_attach once (2026-10-18); do not reorder again.
PROPOSAL_TOOLS: list[dict[str, Any]] = [
    PROPOSE_ORDER,
    PROPOSE_CANCEL,
    PROPOSE_MODIFY,
    PROPOSE_BRACKET,
    PROPOSE_ATTACH,
]
PROPOSAL_TOOL_NAMES: frozenset[str] = frozenset(str(t["name"]) for t in PROPOSAL_TOOLS)
