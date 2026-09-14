"""CLA-SEC-012 — the boundary with ibkr_core_mcp is a security boundary, so it is pinned.

Audit 2026-09-13, findings C. The two packages divide one safety property between them:
ibkr_core_mcp owns the gates, the write endpoints and the capability registry; claudia_ui
owns the human click, the proposal lifecycle and every surface a person reads before
authorising. Neither repository's test suite reads the other's source, so the seam between
them is the one place where both can be green while the property is broken.

Three ways that happens, each with a test here:

1. **A symbol moves.** ClaudIA imports `price_text_safe` and `change_value_text` from
   `ibkr_core_mcp.order_confirm`, neither of which is in the core's `__all__`. A rename
   there leaves core CI green and breaks ClaudIA at import time, in a session. Since
   2026-09-14 the blocking lane resolves the core at the SHA in `core-ref.txt`, so this is
   caught by the informational forward-compatibility lane days before it is caught by an
   upgrade — which is the point of having two lanes rather than one floating `main`.

2. **An entry point changes shape.** ClaudIA calls exactly three gated methods, by name,
   with two keyword arguments. A signature change is a silent behaviour change at the one
   call site that reaches a live brokerage account.

3. **A documented count drifts.** ClaudIA's prose says how many tools the model can reach
   and what they are. The registry is the core's; every number here is computed from it, so
   a tool added there cannot leave a stale sentence here.

4. **A behaviour ClaudIA's own invariant rests on changes shape.** Two of them, added
   2026-09-14 after the core's OWASP recalibration, each pinned because a ClaudIA invariant
   would silently stop holding: the price formatter CLA-SEC-005 renders every human surface
   with, and the arity of the toolkit call the turn loop unpacks. Deliberately *not* pinned:
   the core's MCP transport bearer token (ClaudIA does not use that transport) and
   `redact_error` (ClaudIA has no call site for it — § 9 records that as a known limit, and
   pinning an API this repository does not use would be coupling for its own sake).

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


# ── The supported core revision ──────────────────────────────────────────────────────────


def _supported_core_ref() -> str:
    """The one ref this repository is supported against, read from `core-ref.txt`."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "core-ref.txt"
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    values = [ln for ln in lines if ln and not ln.startswith("#")]
    assert len(values) == 1, f"core-ref.txt must name exactly one ref, found {values}"
    return values[0]


def test_the_supported_core_revision_is_immutable_and_named_once():
    """CI resolves the core from this file; a branch name there is not a pinned build.

    Until 2026-09-14 both CI jobs checked out `ibkr_core_mcp` at floating `main`, so a green
    commit here was not reproducible and a push in the other repository could turn this one
    red with no commit in it. The fix is one file, and this test is what stops a later
    "just point it at main for now" from being invisible.
    """
    import re

    ref = _supported_core_ref()
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"core-ref.txt must hold a full 40-character commit SHA, not {ref!r} — a branch or "
        "a short SHA is not an immutable reference"
    )


def test_nothing_else_in_the_repository_names_a_core_revision():
    """One variable owns the SHA. A second copy is the thing that goes stale.

    Checked over the files a reader would expect to carry one: the workflows, the packaging
    metadata and the setup instructions.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    ref = _supported_core_ref()
    candidates = [
        *(root / ".github" / "workflows").glob("*.yml"),
        root / "pyproject.toml",
        root / "CLAUDE.md",
        root / "README.md",
    ]
    offenders = [
        str(p.relative_to(root))
        for p in candidates
        if p.exists() and re.search(r"\b[0-9a-f]{40}\b", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"a 40-character SHA is written outside core-ref.txt, in {offenders} — "
        f"core-ref.txt ({ref[:12]}…) is the only place that may name one"
    )


# ── Core behaviours a ClaudIA invariant rests on ─────────────────────────────────────────


def test_the_shared_price_formatter_still_renders_a_price_exactly():
    """CLA-SEC-005 is "what the human reads is what the click sends", and this is the "reads".

    `price_text_safe` lives in the core and renders every price on the proposal card, in the
    Gate 2 dialog and on the fill line. The defect it was written for was rounding: a 6E
    limit of 1.08455 read `1.08`, and two prices a full tick apart were indistinguishable on
    the surface a person authorises from. A core-side change back to two decimals would be
    green in that repository and would break this repository's strongest display claim with
    no commit here.
    """
    from ibkr_core_mcp.order_confirm import price_text_safe

    assert price_text_safe(1.08455) == "1.08455", "the formatter rounds again — re-read CLA-SEC-005"
    assert price_text_safe(0.5) == "0.50", "two decimals are the floor, not the ceiling"
    # Total by contract: the cancel card is built from `get_order_status`, so a string IBKR
    # sends that parses as no number must render as itself rather than take the card down.
    assert price_text_safe("Market") == "Market"


def test_the_toolkit_call_the_turn_loop_unpacks_keeps_its_arity():
    """`result_text, _ = self._toolkit.execute(name, inputs)` — a third return value is a 500.

    The dispatcher's one line into the core. It is not covered by the gated-entry-point test
    above, because `execute` is not an order write; it is covered here because unpacking is
    a contract and the turn loop has no other way to reach the toolkit.
    """
    from ibkr_core_mcp import ClaudeToolkit

    parameters = inspect.signature(ClaudeToolkit.execute).parameters
    assert [p for p in parameters if p != "self"][:2] == ["name", "inputs"], (
        f"ClaudeToolkit.execute's first two parameters changed: {list(parameters)}"
    )
    annotation = inspect.signature(ClaudeToolkit.execute).return_annotation
    assert "tuple" in str(annotation).lower(), (
        f"ClaudeToolkit.execute no longer returns a tuple ({annotation!r}); "
        "`result_text, _ = …` in agent._stream_turn unpacks one"
    )
