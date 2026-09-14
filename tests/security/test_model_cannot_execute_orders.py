"""CLA-SEC-001 — the model's own layer cannot reach order execution, structurally.

Audit 2026-09-13, weakness B-1. The property held on the day of the audit, but only because
the dispatcher happened not to route anywhere else: a fifth branch in `_stream_turn`, or
`from claudia.order_flow import _execute_staged_order_core` at the top of `agent.py`, would
have kept every existing test green. The tests that *looked* like this guard did not do it —
`test_local_tool_names_excludes_order_write_tools` compares two constants the same author
writes, and `test_proposal_handlers_cannot_reach_execution` substring-searches
`proposal_tools.py`, the declaration module, not the handler in `agent.py`.

ibkr_core_mcp holds the same line on its own side (`tests/security/test_order_write_boundary.py`),
but its `MODEL_LAYER` is the two-file tuple `claude_tools.py`/`mcp_server.py` and its source
walk is `ibkr_core_mcp/**`. No test in either repo read ClaudIA's model layer until this one.
"""

from __future__ import annotations

import pytest

from tests.security.structural import (
    PACKAGE_DIR,
    function_named,
    imported_modules,
    keyword_arguments_used,
    loops_over,
    package_sources,
    referenced_names,
    self_collaborators,
)

# The modules the LLM's own turn runs through: the loop that reads its `tool_use` blocks,
# the declarations it chooses from, and the sink that renders what it produced.
MODEL_LAYER = ("agent.py", "proposal_tools.py", "panel_sink.py")

# Every way to write an order through ibkr_core_mcp, plus the two things that would let a
# caller build a path of its own. `_session` is here for the model layer only: elsewhere in
# claudia it is an ordinary attribute name (a session-state dict in `panel_app`, an MCP
# `ClientSession` in `tradingview`, the gateway-session owner in `execution_listener`),
# measured 2026-09-14 — so a package-wide rule on it would be five false positives, and a
# rule nobody can keep green gets deleted.
ORDER_WRITE_NAMES = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "reply_order",
        "place_order_and_confirm",
        "modify_order_and_confirm",
        "_resolve_one_reply",
    }
)
GATE_AND_TRANSPORT_NAMES = frozenset(
    {"IBKRClient", "_post", "_session", "OrderWriteAuthorization", "_authorize_order_write"}
)

# `order_flow` holds the three cores; `panel_order_flow` holds the buttons that call them.
# `panel_sink` may import the second — rendering a proposal is its job — and must never
# import the first, which is what "the sink renders, it does not execute" means in code.
EXECUTION_MODULES = ("claudia.order_flow", "claudia.panel_order_flow")


def _source(name: str) -> str:
    return (PACKAGE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("module", MODEL_LAYER)
def test_the_model_layer_names_no_order_write(module):
    """Not one of these modules may so much as name a write method or a client.

    AST, not grep: a docstring may describe `place_order` (several do, and should), but no
    expression may reference it.
    """
    forbidden = (ORDER_WRITE_NAMES | GATE_AND_TRANSPORT_NAMES) & referenced_names(_source(module))
    assert not forbidden, f"{module} references {sorted(forbidden)}"


@pytest.mark.parametrize("module", MODEL_LAYER)
def test_the_model_layer_does_not_import_the_execution_path(module):
    """`panel_sink` renders through `panel_order_flow`; nothing here reaches the cores."""
    allowed = {"claudia.panel_order_flow"} if module == "panel_sink.py" else set()
    imported = imported_modules(_source(module)) & set(EXECUTION_MODULES)
    assert not (imported - allowed), f"{module} imports {sorted(imported - allowed)}"


def test_the_tool_dispatcher_has_exactly_four_sinks():
    """A model-chosen tool name reaches these collaborators and no others.

    The dispatcher is a closed `if/elif/else` over `_LOCALLY_HANDLED`, the curated
    TradingView names and the toolkit, with an availability guard between. Its safety is
    that a name it does not know falls through to `ClaudeToolkit.execute`'s literal dict and
    comes back as a string. This pins the collaborator set rather than the branch text, so a
    reordering is fine and a *new sink* is not.
    """
    turn = function_named(_source("agent.py"), "_stream_turn")
    reached: set[str] = set()
    for loop in loops_over(turn, "tool_calls"):
        reached |= self_collaborators(loop)
    assert reached == {
        "_handle_local_tool",  # the local if-chain: propose_* and the five utility tools
        "_toolkit",  # ClaudeToolkit.execute — a literal 44-entry dict, no order write
        "_tv_bridge",  # the curated TradingView names, over MCP stdio
        "_tv_tool_names",  # which names that branch owns
        "_sink",  # the tool-step indicator
        "_store",  # the raw tool_use/tool_result record
        "_session_id",  # which session that record belongs to
    }


def test_claudia_never_mints_or_forwards_an_order_authorization():
    """One Touch ID per write is ibkr_core_mcp's to grant; ClaudIA's job is to ask.

    `place_order(authorization=…)` trusts a value only `_authorize_order_write` should mint.
    The core's AST test holds the minting site; this is the other half — that its caller
    never constructs one, forwards one, or patches the gate away outside tests.
    """
    offenders = []
    for path, source in package_sources():
        names = referenced_names(source) & {
            "OrderWriteAuthorization",
            "_authorize_order_write",
            "require_touch_id",
        }
        if names:
            offenders.append(f"{path.name}: {sorted(names)}")
        if "authorization" in keyword_arguments_used(source):
            offenders.append(f"{path.name}: passes authorization=")
    assert not offenders, "ClaudIA reached into the gate: " + "; ".join(offenders)


def test_only_order_flow_calls_a_gated_write():
    """The three calls that reach IBKR live in one file, so there is one place to review.

    Scope is `claudia/**` plus `scripts/**`: a dev harness runs the same agent against the
    same store, and "it is only a script" is precisely the argument that would put one there.
    """
    offenders = {
        path.name: sorted(ORDER_WRITE_NAMES & referenced_names(source))
        for path, source in package_sources()
        if path.name != "order_flow.py" and ORDER_WRITE_NAMES & referenced_names(source)
    }
    assert not offenders, f"order writes outside order_flow.py: {offenders}"


# ── Guards on the guards ─────────────────────────────────────────────────────────────────
#
# Each probe above is only worth its runtime if it fires on the violation it describes. The
# snippets below are the violations a future change would most plausibly look like.


def test_the_name_probe_sees_a_write_reached_through_a_toolkit():
    snippet = (
        "class A:\n"
        "    def _handle_local_tool(self, name, inputs):\n"
        "        return self._toolkit.client.place_order(inputs['acct'], inputs)\n"
    )
    assert ORDER_WRITE_NAMES & referenced_names(snippet) == {"place_order"}


def test_the_name_probe_ignores_a_docstring_mention():
    snippet = 'def f():\n    """Never call place_order from here."""\n    return 1\n'
    assert not ORDER_WRITE_NAMES & referenced_names(snippet)


def test_the_import_probe_sees_a_function_local_import():
    snippet = (
        "async def send_order_proposal(self, proposal):\n"
        "    from claudia.order_flow import _execute_staged_order_core\n"
        "    await _execute_staged_order_core(proposal)\n"
    )
    assert "claudia.order_flow" in imported_modules(snippet)


def test_the_sink_probe_sees_a_new_collaborator():
    snippet = (
        "class A:\n"
        "    def _stream_turn(self):\n"
        "        for tc in tool_calls:\n"
        "            self._executor.run(tc)\n"
    )
    loop = loops_over(function_named(snippet, "_stream_turn"), "tool_calls")[0]
    assert "_executor" in self_collaborators(loop)


def test_the_authorization_probe_sees_a_forwarded_grant():
    snippet = (
        "def go(client, body, auth):\n    return client.place_order(body, authorization=auth)\n"
    )
    assert "authorization" in keyword_arguments_used(snippet)
