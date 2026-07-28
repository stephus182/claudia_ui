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

# `integer`, not `number`: order_flow.py:310 does int(qty) ("whole shares only", per its
# own field table at :288), so a fractional 1.7 would be SILENTLY truncated to 1 — a
# mutation of a user-specified order parameter, which order-parameter immutability
# forbids, and the same shape as crash vector 4 (wrong outcome, no exception). `integer`
# is a type keyword, not a numeric constraint, so it registers fine (conid already proves
# integers do). It also rejects inf/NaN/True for free, which `number` admits. If
# fractional quantities are ever supported, widen this deliberately — never incidentally.
#
# Positivity is still NOT enforced: `exclusiveMinimum` is a 400 (see the probe record), so
# the bound lives in the description — which mirrors what the SDK's transform_schema does
# on the parse path, so the model at least sees it. A description is not enforcement; see
# "Handler obligations" above.
_QUANTITY: dict[str, object] = {
    "type": "integer",
    "description": "Number of units/contracts. Whole units only, and must be greater than 0.",
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
    "enum": ["limit_price", "stop_price", "quantity", "order_type", "tif"],
}
_SCALAR: dict[str, object] = {
    "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}]
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
        "properties": {
            "symbol": {"type": "string"},
            "action": _ACTION,
            "quantity": _QUANTITY,
            "order_type": _ORDER_TYPE,
            "limit_price": _PRICE,
            "stop_price": _PRICE,
            "tif": _TIF,
            "sec_type": _SEC_TYPE,
            "conid": _CONID,
            "reason": {"type": "string"},
        },
        "required": [
            "symbol",
            "action",
            "quantity",
            "order_type",
            "limit_price",
            "stop_price",
            "tif",
            "sec_type",
            "conid",
            "reason",
        ],
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
        "properties": {
            "order_id": {"type": "string"},
            "symbol": {"type": "string"},
            "action": _ACTION,
            "quantity": _QUANTITY,
            "order_type": _ORDER_TYPE,
            "limit_price": _PRICE,
            "stop_price": _PRICE,
            "tif": _TIF,
            "reason": {"type": "string"},
        },
        "required": [
            "order_id",
            "symbol",
            "action",
            "quantity",
            "order_type",
            "limit_price",
            "stop_price",
            "tif",
            "reason",
        ],
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
        "Renders a button; modifies nothing by itself."
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
            "reason",
            "changes",
        ],
        "additionalProperties": False,
    },
}

PROPOSAL_TOOLS: list[dict] = [PROPOSE_ORDER, PROPOSE_CANCEL, PROPOSE_MODIFY]
PROPOSAL_TOOL_NAMES: frozenset[str] = frozenset(str(t["name"]) for t in PROPOSAL_TOOLS)
