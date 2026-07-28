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
_BANNED_NUMERIC_KEYWORDS = frozenset({"exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum", "multipleOf"})


def _schema(name: str) -> Any:
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
    for key, _ in _walk_items(node):
        yield key


def _walk_nodes(node: Any) -> Iterator[dict]:
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


def test_every_tool_is_strict_and_closed() -> None:
    for tool in PROPOSAL_TOOLS:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["input_schema"]["required"]


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
    last tool in the array — `propose_modify`, since Task 3 appends PROPOSAL_TOOLS last —
    carries the prompt-cache breakpoint. `strict: true` alongside `cache_control` on one
    tool is its own untested combination, and a rejection there would be the same
    registration-time 400 that fails every request. Probed together, 2026-07-27: accepted.
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


def test_valid_proposal_is_accepted() -> None:
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
        "reason": "test order",
    }
    assert not list(Draft202012Validator(_schema("propose_order")).iter_errors(ok))


def test_valid_cancel_proposal_is_accepted() -> None:
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
