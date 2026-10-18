"""Tests for the strict proposal tool schemas.

These schemas are a registration-time contract: an unsupported JSON Schema keyword is a
hard 400 on *every* request, not a silent strip. Local `jsonschema` validation cannot
prove API acceptance — it accepted both keywords the live API rejected — so the checks
here cover shape and handler obligations, and `test_live_api_accepts_proposal_tools`
(opt-in, `live_api` marker) covers acceptance.
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from claudia.proposal_tools import PROPOSAL_TOOLS

_LIVE_CHECK_ENV = "CLAUDIA_LIVE_SCHEMA_CHECK"

# Numeric-constraint keywords the tools endpoint rejects. exclusiveMinimum is the one
# actually proven to 400 (2026-07-27 probe); the siblings are banned pre-emptively
# because they are the same documented family and the same trap.
_BANNED_NUMERIC_KEYWORDS = frozenset(
    {"exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum", "multipleOf"}
)


def _schema(name: str) -> Any:
    """The input schema of the named proposal tool."""
    return next(t["input_schema"] for t in PROPOSAL_TOOLS if t["name"] == name)


def _walk_items(node: Any) -> Iterator[tuple[str, Any]]:
    """Yield every (key, value) at every depth, so a keyword can't hide inside anyOf/items."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_items(item)


def _walk_keys(node: Any) -> Iterator[str]:
    """Every key appearing anywhere in a nested schema, for whole-tree assertions."""
    for key, _ in _walk_items(node):
        yield key


def _walk_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict node at every depth, including the root."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_nodes(item)


@pytest.mark.parametrize(
    "payload",
    [
        # vector 1 — sec_type=None reached _format_order_summary, whose
        # .get("sec_type", "STK") default only fires on an *absent* key.
        {
            "symbol": "ES",
            "action": "BUY",
            "quantity": 1,
            "order_type": "LMT",
            "limit_price": 6000,
            "tif": "GTC",
            "sec_type": None,
            "reason": "t",
        },
        # vector 2 — a string price blew up every f"{price:.2f}" formatter.
        {
            "symbol": "ES",
            "action": "BUY",
            "quantity": 1,
            "order_type": "LMT",
            "limit_price": "6000.00",
            "tif": "GTC",
            "sec_type": "FUT",
            "reason": "t",
        },
        # vector 3 — a non-dict payload raised AttributeError outside the caught
        # exception type. Only the top-level "type": "object" rejects this: on a
        # list instance both `required` and `additionalProperties` are inert.
        [
            {
                "symbol": "ES",
                "action": "BUY",
                "quantity": 1,
                "order_type": "LMT",
                "limit_price": 6000.0,
                "tif": "GTC",
                "sec_type": "FUT",
                "reason": "t",
            }
        ],
        # vector 4 — a falsy parsed value skipped the render with no exception.
        {},
    ],
)
def test_schema_rejects_every_known_crash_vector(payload: Any) -> None:
    """All four vectors passed the old hand validator and crashed the formatters."""
    errors = list(Draft202012Validator(_schema("propose_order")).iter_errors(payload))
    assert errors, f"schema accepted a known crash vector: {payload}"


# The API's cap on union-typed parameters (every anyOf / type array) across ALL strict schemas
# in one request — measured 2026-10-18 (the module's probe record): 17 is refused, and the
# internal grammar limit refused every four- or five-strict-tool layout even at 14.
_API_UNION_BUDGET = 16


def _union_count(node: Any) -> int:
    """Properties typed with anyOf or a type array, at every depth — what the API counts."""
    count = 0
    if isinstance(node, dict):
        for prop in (node.get("properties") or {}).values():
            if isinstance(prop, dict) and ("anyOf" in prop or isinstance(prop.get("type"), list)):
                count += 1
            count += _union_count(prop)
        count += _union_count(node.get("items"))
        for branch in node.get("anyOf") or []:
            count += _union_count(branch)
    return count


def test_every_tool_is_closed_and_strict_where_the_api_budget_allows() -> None:
    """Closed objects on all five; `strict: true` on exactly the three the API can compile.

    The two bracket tools are declared non-strict — explicitly, never by omission — because
    the five together exceed the API's union budget (the module's 2026-10-18 record); the
    same closed shape is enforced in-process by `agent._schema_defect`, which keys on this
    very flag. A tool whose `strict` key is absent would be enforced by neither.
    """
    for tool in PROPOSAL_TOOLS:
        assert "strict" in tool, tool["name"]
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["input_schema"]["required"]
    strict = {str(t["name"]) for t in PROPOSAL_TOOLS if t["strict"] is True}
    assert strict == {"propose_order", "propose_cancel", "propose_modify"}
    assert {str(t["name"]) for t in PROPOSAL_TOOLS if t["strict"] is False} == {
        "propose_bracket",
        "propose_attach",
    }


def test_the_strict_tools_stay_inside_the_api_union_budget() -> None:
    """A 17th union-typed parameter across the strict tools is a 400 on EVERY request.

    The live probe is the authority; this is the local guard that fails before a billed
    request does. It cannot see the internal grammar limit, which is why a change here still
    needs the probe (`CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest -m live_api`).
    """
    strict_unions = sum(_union_count(t["input_schema"]) for t in PROPOSAL_TOOLS if t["strict"])
    assert strict_unions <= _API_UNION_BUDGET, strict_unions
    assert strict_unions == 10  # the three originals, unchanged since 2026-09-04


def test_no_numeric_constraint_keyword_at_any_depth() -> None:
    """A numeric-constraint keyword makes the API reject the whole request with a 400.

    Proven by live probe on 2026-07-27 (claude-opus-4-8, max_tokens=1). Sending a schema
    carrying exclusiveMinimum returns:

        tools.0.custom: For 'number' type, property 'exclusiveMinimum' is not supported

    These keywords are NOT silently stripped: the SDK's transform_schema only runs for
    output_config.format / output_format, never for tools=, so hand-written tool dicts
    reach the wire verbatim. Re-adding one takes every request down at runtime, not at
    import — hence this guard. The quantity>0 bound lives in the description instead and
    must be enforced by the render-path handler.
    """
    offenders = [
        f"{tool['name']}: {key}"
        for tool in PROPOSAL_TOOLS
        for key in _walk_keys(tool["input_schema"])
        if key in _BANNED_NUMERIC_KEYWORDS
    ]
    assert not offenders, "Keywords the tools endpoint 400s on:\n" + "\n".join(offenders)


def test_no_string_constraint_keywords_anywhere() -> None:
    """minLength/maxLength are deliberately absent, though minLength probes as ACCEPTED.

    The cited reference affirmatively lists "String constraints (minLength, maxLength)"
    as not supported. Depending on it is the same class of risk as the unspecified
    ["number","null"] type-array, and a stronger one: here the docs actively contradict
    observed behaviour, so if enforcement is ever tightened to match the published list,
    every request 400s at registration — the exact failure this module prevents.

    It also bought nothing: "   " satisfies minLength: 1. Blank/whitespace rejection for
    symbol and order_id is a handler obligation, recorded in the module docstring.
    """
    offenders = [
        f"{tool['name']}: {key}"
        for tool in PROPOSAL_TOOLS
        for key in _walk_keys(tool["input_schema"])
        if key in {"minLength", "maxLength"}
    ]
    assert not offenders, "Deliberately-omitted string constraints found:\n" + "\n".join(offenders)


def test_nullable_fields_use_documented_anyOf_form() -> None:
    """Nullability uses anyOf, not the accepted-but-unspecified ["number", "null"] type-array.

    Both forms were accepted by the probe (variants B and C), so this is not a bug fix —
    it keeps a safety-critical schema off undocumented behaviour.
    """
    props = _schema("propose_order")["properties"]
    assert props["limit_price"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    assert props["conid"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    # Gap #18: a quantity is a number — IBKR's order body types it `double` — and whether a
    # fraction is permitted is the contract's rule, checked on the order path.
    assert props["quantity"]["type"] == "number"
    # 2026-09-04: the outside-RTH attribute, same nullable form (null = "the user did not say").
    assert props["outside_rth"]["anyOf"] == [{"type": "boolean"}, {"type": "null"}]


def test_outside_rth_is_carried_and_required_on_order_and_modify() -> None:
    """`outside_rth` (2026-09-04, gap #33) is on propose_order AND propose_modify — a modify
    resends the whole order, so a replacement without it would silently drop the attribute
    and a futures stop would stop working overnight. Required under strict like every key;
    its description is the only text that reaches the model, so it must say when to set it
    and must not invite assumptions (order-parameter immutability)."""
    for name in ("propose_order", "propose_modify"):
        schema = _schema(name)
        assert "outside_rth" in schema["properties"], name
        assert "outside_rth" in schema["required"], name
        desc = schema["properties"]["outside_rth"]["description"]
        assert "futures" in desc.lower() and "regular trading hours" in desc.lower()
        assert "never assume" in desc.lower() or "only when the user" in desc.lower()
    changes_items = _schema("propose_modify")["properties"]["changes"]["items"]
    assert "outside_rth" in changes_items["properties"]["field"]["enum"]


def test_every_object_node_is_explicitly_closed() -> None:
    """Every `type: object` node must SET additionalProperties to false — absent is not enough.

    `additionalProperties: true` is a hard 400 (probe, 2026-07-27, all three registered):

        tools.2.custom: For 'object' type, 'additionalProperties: true' is not
        supported. Please set 'additionalProperties' to false

    The docs say it "must be set to false for objects", so an object that merely omits the
    key is equally non-conforming — hence this asserts presence-and-false rather than
    filtering for `is not False`, which a nested object with the key absent slips past.
    Local jsonschema accepts every one of these forms; see test_live_api_accepts_proposal_tools.
    """
    offenders = [
        f"{tool['name']}: object node with additionalProperties={node.get('additionalProperties', '<absent>')!r}"
        for tool in PROPOSAL_TOOLS
        for node in _walk_nodes(tool["input_schema"])
        if node.get("type") == "object" and node.get("additionalProperties") is not False
    ]
    assert not offenders, "Object nodes not explicitly closed:\n" + "\n".join(offenders)


def test_modify_carries_one_change_structure_not_two() -> None:
    """Two parallel structures describing one fact can disagree — the failure class this removes.

    `changes` replaces the planned `changed_fields` + `previous_values` pair. The `enum` on
    `field` IS enforced under strict, so the model cannot invent a field name.
    """
    props = _schema("propose_modify")["properties"]
    assert "changed_fields" not in props
    assert "previous_values" not in props
    item = props["changes"]["items"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == ["field", "previous_value"]
    assert item["properties"]["field"]["enum"] == [
        "limit_price",
        "stop_price",
        "quantity",
        "order_type",
        "tif",
        "outside_rth",  # 2026-09-04
    ]
    # New values are NOT duplicated here; the top-level fields are authoritative.
    assert "new_value" not in item["properties"]


@pytest.mark.live_api
@pytest.mark.skipif(
    os.environ.get(_LIVE_CHECK_ENV) != "1",
    reason=f"live API schema check is opt-in: set {_LIVE_CHECK_ENV}=1",
)
def test_live_api_accepts_proposal_tools() -> None:
    """Register the real PROPOSAL_TOOLS against the live API and assert it accepts them.

    Local jsonschema validation CANNOT prove API acceptance: it accepted both schemas that
    the tools endpoint rejected outright, so two defects reached a fully green suite —

        tools.0.custom: For 'number' type, property 'exclusiveMinimum' is not supported
        tools.2.custom: For 'object' type, 'additionalProperties: true' is not supported.
                        Please set 'additionalProperties' to false

    Both are registration-time 400s: they fail every request, not just malformed ones, so
    without this check the failure surfaces in production rather than in CI. All three
    tools are sent together because the second 400 only appears in the combined payload.

    Opt-in — it costs a real API call, so it is gated on an explicit env var rather than on
    the presence of a key (which is set in this dev environment). Run it with:

        CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest tests/test_proposal_tools.py -m live_api -v

    Marked `live_api`, not `integration`: the latter is defined in pyproject.toml as
    "requires live IBKR gateway and credentials", which this does not, and CLAUDE.md
    records that no test carries it.

    This is a schema-acceptance probe only, not a behavioural test: max_tokens=1, a
    throwaway prompt, and no assertion about what the model says.

    Sent through `_with_cache_marker`, which is how `agent.py` actually ships them: the
    last tool in the array — `propose_attach` since 2026-10-18 (`propose_modify` before), as
    the agent appends PROPOSAL_TOOLS last —
    carries the prompt-cache breakpoint. `strict: true` alongside `cache_control` on one
    tool is its own untested combination, and a rejection there would be the same
    registration-time 400 that fails every request. Probed together, 2026-07-27: accepted.

    Since 2026-10-18 the five are three strict and two non-strict: the five strict would
    carry 22 union-typed parameters against the API's cap of 16 across all strict schemas
    in a request (the module's probe record). This test is what found it.
    """
    import anthropic
    from dotenv import load_dotenv

    from claudia.agent import _with_cache_marker

    # Same credential resolution the app uses (order_flow.py): the key lives in .env, not
    # in the ambient environment. Never logged or interpolated anywhere.
    load_dotenv(override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail(f"{_LIVE_CHECK_ENV}=1 but no ANTHROPIC_API_KEY resolved (checked .env)")

    model = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
    try:
        anthropic.Anthropic().messages.create(
            model=model,
            max_tokens=1,
            tools=_with_cache_marker(PROPOSAL_TOOLS),  # type: ignore[arg-type]
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.BadRequestError as exc:  # pragma: no cover - only on schema breakage
        pytest.fail(f"live API rejected PROPOSAL_TOOLS on {model}: {exc}")
    # The evidence line for the module docstring's probe record (visible with `-s`).
    print(f"live API accepted {len(PROPOSAL_TOOLS)} PROPOSAL_TOOLS on {model}")


def test_valid_proposal_is_accepted() -> None:
    """A well-formed order proposal validates."""
    ok = {
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 6000.0,
        "stop_price": None,
        "tif": "GTC",
        "sec_type": "FUT",
        "conid": None,
        "outside_rth": None,
        "reason": "test order",
    }
    assert not list(Draft202012Validator(_schema("propose_order")).iter_errors(ok))


def test_valid_cancel_proposal_is_accepted() -> None:
    """A well-formed cancel proposal validates."""
    ok = {
        "order_id": "716373691",
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 6100.0,
        "stop_price": None,
        "tif": "GTC",
        "reason": "user asked to pull the resting bid",
    }
    assert not list(Draft202012Validator(_schema("propose_cancel")).iter_errors(ok))


def test_valid_modify_proposal_is_accepted() -> None:
    """`changes` is the one bespoke structure here — a required array of required closed
    objects, invented mid-task and absent from the plan. The live probe proves it
    registers; this proves a realistic instance actually validates.
    """
    ok = {
        "order_id": "716373691",
        "conid": 495512563,
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 6050.0,
        "stop_price": None,
        "tif": "GTC",
        "sec_type": "FUT",
        "outside_rth": None,
        "reason": "user moved the bid down 50 points",
        "changes": [{"field": "limit_price", "previous_value": 6100.0}],
    }
    assert not list(Draft202012Validator(_schema("propose_modify")).iter_errors(ok))


@pytest.mark.parametrize(
    "changes",
    [
        [],  # minItems: 1 — an empty diff would reach Gate 2 showing no before/after
        [{"field": "not_a_field", "previous_value": 1}],  # enum IS enforced under strict
        [{"field": "limit_price"}],  # previous_value is required
        [{"field": "limit_price", "previous_value": 1, "extra": "x"}],  # closed object
    ],
)
def test_modify_rejects_malformed_changes(changes: Any) -> None:
    """A malformed `changes` entry is rejected rather than rendered as a broken diff."""
    proposal = {
        "order_id": "1",
        "conid": 1,
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 1.0,
        "stop_price": None,
        "tif": "GTC",
        "sec_type": "FUT",
        "reason": "r",
        "changes": changes,
    }
    errors = list(Draft202012Validator(_schema("propose_modify")).iter_errors(proposal))
    assert errors, f"schema accepted malformed changes: {changes}"


def test_previous_value_accepts_a_boolean_for_an_outside_rth_change() -> None:
    """Review 2026-09-04 #3: `outside_rth` became modifiable but `previous_value` admitted
    only string | number | null, so a boolean prior was inexpressible and strict mode would
    have steered the model into "false" / 0 / null. Booleans are now in the scalar union."""
    ok = {
        "order_id": "555",
        "conid": 649180671,
        "symbol": "ES",
        "action": "BUY",
        "quantity": 1,
        "order_type": "STP",
        "limit_price": None,
        "stop_price": 7720.0,
        "tif": "GTC",
        "sec_type": "FUT",
        "outside_rth": True,
        "reason": "keep it live overnight",
        "changes": [{"field": "outside_rth", "previous_value": False}],
    }
    assert not list(Draft202012Validator(_schema("propose_modify")).iter_errors(ok))


def test_modify_description_does_not_point_outside_rth_at_a_source_that_lacks_it() -> None:
    """Measured 2026-09-04: `get_order_status` returns `outside_rth` for a stock order and
    NO such key for a futures order (`.firecrawl/ibkr/order-status.md` documents neither).
    The modify description must say where the value comes from for futures — the user —
    rather than tell the model to copy a field that is not there."""
    desc = next(t for t in PROPOSAL_TOOLS if t["name"] == "propose_modify")["description"]
    assert "outside_rth" in desc
    assert "futures" in desc.lower()


# --- Brackets and attached orders (gap #36, 2026-10-18) -----------------------------------
# The parent of a bracket is a propose_order; the parent of an attach is the propose_cancel
# identity (the working order's fields); the child is one shape shared by both tools.

_CHILD_OK = {
    "kind": "profit_taker",
    "price": 170.0,
    "limit_price": None,
    "tif": None,
    "outside_rth": None,
}


def _valid_bracket() -> dict[str, Any]:
    """A well-formed bracket proposal: propose_order's eleven fields plus one child."""
    return {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 150.0,
        "stop_price": None,
        "tif": "GTC",
        "sec_type": "STK",
        "conid": 265598,
        "outside_rth": None,
        "reason": "test",
        "children": [_CHILD_OK],
    }


def test_valid_bracket_proposal_is_accepted() -> None:
    """A parent with a profit taker and a stop-limit stop loss validates."""
    ok = {
        **_valid_bracket(),
        "children": [
            _CHILD_OK,
            {
                "kind": "stop_loss",
                "price": 140.0,
                "limit_price": 139.5,
                "tif": "DAY",
                "outside_rth": False,
            },
        ],
    }
    assert not list(Draft202012Validator(_schema("propose_bracket")).iter_errors(ok))


def test_bracket_schema_requires_at_least_one_child() -> None:
    """`minItems: 1` on `children` — a bracket without a child is a plain order, propose_order's job."""
    bad = {**_valid_bracket(), "children": []}
    assert list(Draft202012Validator(_schema("propose_bracket")).iter_errors(bad))


def test_valid_attach_proposal_is_accepted() -> None:
    """A working order's identity plus one child validates."""
    ok = {
        "order_id": "123",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 150.0,
        "stop_price": None,
        "tif": "GTC",
        "reason": "test",
        "child": _CHILD_OK,
    }
    assert not list(Draft202012Validator(_schema("propose_attach")).iter_errors(ok))


def test_bracket_children_and_attach_child_are_one_shape() -> None:
    """One child object, defined once: a rule added to one tool cannot be missed by the other."""
    bracket = _schema("propose_bracket")["properties"]["children"]["items"]
    attach = _schema("propose_attach")["properties"]["child"]
    assert bracket is attach


def test_bracket_parent_fields_are_propose_orders_and_attach_fields_are_propose_cancels() -> None:
    """The parents are the existing tools' own tables: the four schemas cannot drift field by field."""
    order = _schema("propose_order")["properties"]
    bracket = _schema("propose_bracket")["properties"]
    assert {k: v for k, v in bracket.items() if k != "children"} == order
    cancel = _schema("propose_cancel")["properties"]
    attach = _schema("propose_attach")["properties"]
    assert {k: v for k, v in attach.items() if k != "child"} == cancel
