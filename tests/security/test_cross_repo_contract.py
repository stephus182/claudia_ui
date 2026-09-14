"""CLA-SEC-012 — the boundary with ibkr_core_mcp is a security boundary, so it is pinned.

Audit 2026-09-13, findings C. The two packages divide one safety property between them:
ibkr_core_mcp owns the gates, the write endpoints and the capability registry; claudia_ui
owns the human click, the proposal lifecycle and every surface a person reads before
authorising. Neither repository's test suite reads the other's source, so the seam between
them is the one place where both can be green while the property is broken.

Three ways that happens, each with a test here:

1. **A symbol moves.** ClaudIA imports `price_text_safe` and `change_value_text` from
   `ibkr_core_mcp.order_confirm`, neither of which is in the core's `__all__`. A rename
   there leaves core CI green and breaks ClaudIA at import time, in a session, not in CI —
   because CI checks out the core at floating `main` and this repository pins no revision
   (Stage 3.8 of the plan; until that lands, this test is the detector).

2. **An entry point changes shape.** ClaudIA calls exactly three gated methods, by name,
   with two keyword arguments. A signature change is a silent behaviour change at the one
   call site that reaches a live brokerage account.

3. **A documented count drifts.** ClaudIA's prose says how many tools the model can reach
   and what they are. The registry is the core's; every number here is computed from it, so
   a tool added there cannot leave a stale sentence here.

These assertions run against the *installed* core, so they check what this machine and CI
actually resolve rather than what a document claims.
"""

from __future__ import annotations

import inspect

import pytest

from tests.security.structural import package_sources, referenced_names

# What ClaudIA imports from the core, and from where. Every entry is a name this repository
# would fail to start without. Kept explicit rather than derived from the imports, so that
# adding one is a decision someone makes here and not a diff nobody reads.
IMPORTED_API: dict[str, tuple[str, ...]] = {
    "ibkr_core_mcp": (
        "ClaudeToolkit",
        "Config",
        "IBKRClient",
        "BrowserCookieAuth",
        "GDriveCache",
        "SQLiteStore",
    ),
    "ibkr_core_mcp.order_confirm": ("change_value_text", "price_text_safe"),
    "ibkr_core_mcp.streaming": ("IBKRWebSocket", "PnLUpdate", "TradeExecution"),
    "ibkr_core_mcp.gateway": ("GatewayManager",),
    "ibkr_core_mcp.auth": ("BrowserCookieAuth",),
}

# The three gated entry points, and the keyword arguments ClaudIA passes to each. The gates
# live inside these methods in the core; calling a different one, or the same one with a
# changed signature, is how an order write stops being gated the way this repository thinks.
GATED_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    "place_order_and_confirm": ("reply_log",),
    "modify_order_and_confirm": ("reply_log",),
    "cancel_order": ("order_details",),
}


@pytest.mark.parametrize("module_name", sorted(IMPORTED_API))
def test_every_imported_core_symbol_still_exists(module_name):
    """Import each one the way ClaudIA imports it, from the installed package."""
    import importlib

    module = importlib.import_module(module_name)
    missing = [name for name in IMPORTED_API[module_name] if not hasattr(module, name)]
    assert not missing, f"{module_name} no longer provides {missing}"


def test_the_three_gated_entry_points_keep_their_shape():
    """Name, presence, and the keyword arguments ClaudIA actually passes."""
    from ibkr_core_mcp import IBKRClient

    for method_name, keywords in GATED_ENTRY_POINTS.items():
        method = getattr(IBKRClient, method_name, None)
        assert method is not None, f"IBKRClient no longer has {method_name}"
        parameters = inspect.signature(method).parameters
        missing = [kw for kw in keywords if kw not in parameters]
        assert not missing, f"{method_name} no longer accepts {missing}"


def test_claudia_calls_no_gated_method_it_has_not_pinned():
    """The reverse direction: a fourth entry point added here must be pinned here too.

    Without this, `GATED_ENTRY_POINTS` documents the seam as of the day it was written and
    silently stops covering it — the failure mode the audit found in three other tests.
    """
    from ibkr_core_mcp import IBKRClient

    gated_names = {
        name
        for name in dir(IBKRClient)
        if name in {"place_order", "modify_order", "cancel_order", "reply_order"}
        or name.endswith("_and_confirm")
    }
    called = set()
    for _path, source in package_sources():
        called |= gated_names & referenced_names(source)
    assert called == set(GATED_ENTRY_POINTS), (
        f"ClaudIA calls {sorted(called)}; this test pins {sorted(GATED_ENTRY_POINTS)}"
    )


def test_the_capability_registry_is_the_source_of_the_tool_numbers():
    """Whatever ClaudIA says about the toolkit must be computed from the registry.

    The audit found `SECURITY.md` claiming "44 read-only ClaudeToolkit tools" while 20 of
    the 44 carried a capability other than READ_ONLY — four of them ungated IBKR account
    writes. The count was right and the adjective was false, and nothing could tell.
    """
    from ibkr_core_mcp.claude_tools import CAPABILITIES, TOOL_DEFINITIONS

    assert TOOL_DEFINITIONS, "the registry is empty"
    assert all(t.get("capabilities") for t in TOOL_DEFINITIONS), (
        "a tool declares no capability, so no honest sentence can be written about the set"
    )
    assert "ORDER_EXECUTION" not in CAPABILITIES, (
        "the forbidden capability gained a spelling — a tool could now declare it"
    )
    read_only = [str(t["name"]) for t in TOOL_DEFINITIONS if "READ_ONLY" in t["capabilities"]]
    assert len(read_only) < len(TOOL_DEFINITIONS), (
        "every tool now reads as read-only; re-check before writing that down anywhere"
    )


def test_claudia_exposes_the_whole_registry_to_the_model_and_knows_it():
    """ClaudIA hands the model every tool the registry defines, writers included.

    This is a deliberate position, not an oversight — the alert writers are useful and the
    order writers are not in the registry at all. It is recorded as a test so that the day
    it stops being deliberate, something says so.
    """
    from ibkr_core_mcp.claude_tools import TOOL_DEFINITIONS

    # `frozenset(...)`: the registry types `capabilities` as a bare Collection, so set
    # algebra on it is not statically valid even though every value is a frozenset.
    mutating = {
        str(t["name"])
        for t in TOOL_DEFINITIONS
        if frozenset(t["capabilities"])
        & {"ACCOUNT_STATE", "GOOGLE_DRIVE", "DATABASE", "SANDBOX_EXECUTION"}
    }
    assert mutating, "no tool mutates anything — the registry's meaning has changed"
    # `str(...)` for the same reason as the frozenset above: the registry's value type is a
    # union, so a name read out of it is not statically a `str`.
    account_writers = {
        str(t["name"]) for t in TOOL_DEFINITIONS if "ACCOUNT_STATE" in t["capabilities"]
    }
    assert account_writers == {
        "create_price_alert",
        "modify_price_alert",
        "delete_alert",
        "activate_alert",
    }, (
        "the set of ungated IBKR account writes the model can reach has changed: "
        f"{sorted(account_writers)}"
    )
