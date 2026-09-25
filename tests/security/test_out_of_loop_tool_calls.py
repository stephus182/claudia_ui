"""Every tool run outside the agent loop goes through the recording seam (gap #21, closed
2026-09-25 — the class, not the instance).

**The failure this pins.** A real tool can run without leaving a `tool` row: a chart-pane
Load click fetched market data through `toolkit.execute` directly, so the bar cache it
filled could later be narrated by the model ("cache miss … fetched … it worked") with
neither the ledger nor the store able to say what actually ran. The two startup Flex
calls had the same shape until 2026-09-23. `claudia/tool_record.py` is the seam that
records such a call before its caller can interpret the outcome; this file makes using it
the only way to call the toolkit from anywhere but the agent loop.

**What counts.** Any `<…toolkit>.execute` attribute reference — a call, or the bound method
handed to a thread — in any module of the package or `scripts/`, except:

* `agent.py`, the loop itself: every call it makes is a `tool_use` / `tool_result` pair in
  the store already;
* `tool_record.py`, the seam;
* the one documented exemption below, each of whose reasons is itself asserted.

Adding a name to either list is a decision, and the diff should say why.
"""

from __future__ import annotations

import ast

from tests.security.structural import function_named, package_sources

DIRECT_CALLERS: frozenset[str] = frozenset({"agent.py", "tool_record.py"})

# module -> (function holding the call, why it needs no row of its own)
EXEMPT: dict[str, tuple[str, str]] = {
    "execution_listener.py": (
        "get_live_pnl_text",
        "reached only through agent.py's `get_live_pnl` local tool, whose own call is the "
        "recorded one; a second row would double-count one action",
    ),
}


def _toolkit_execute_sites(source: str) -> list[int]:
    """Line numbers of every `<…toolkit>.execute` attribute in `source`."""
    sites: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Attribute) and node.attr == "execute"):
            continue
        base = node.value
        owner = (
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else ""
        )
        if owner.lower().endswith("toolkit"):
            sites.append(node.lineno)
    return sites


def test_no_tool_run_outside_the_agent_loop_escapes_the_recording_seam():
    """The class: every out-of-loop toolkit call is inside `record_and_execute`."""
    offenders: dict[str, list[int]] = {}
    for path, source in package_sources():
        if path.name in DIRECT_CALLERS:
            continue
        sites = _toolkit_execute_sites(source)
        if path.name in EXEMPT:
            fn = function_named(source, EXEMPT[path.name][0])
            sites = [ln for ln in sites if not (fn.lineno <= ln <= (fn.end_lineno or fn.lineno))]
        if sites:
            offenders[path.name] = sites
    assert offenders == {}, f"toolkit called outside the seam: {offenders}"


def test_the_guard_sees_the_calls_it_permits():
    """Not vacuous: the loop and the seam each hold at least one site the walker recognises."""
    seen = {path.name: _toolkit_execute_sites(source) for path, source in package_sources()}
    assert seen["agent.py"], "the walker no longer recognises the agent loop's own calls"
    assert seen["tool_record.py"], "the walker no longer recognises the seam's call"


def test_each_exemption_reason_is_true():
    """`get_live_pnl_text` is referenced by agent.py alone, and only inside `_get_live_pnl`."""
    for path, source in package_sources():
        if path.name == "execution_listener.py":
            continue
        hits = [i + 1 for i, line in enumerate(source.splitlines()) if "get_live_pnl_text" in line]
        if path.name != "agent.py":
            assert not hits, f"{path.name} reaches the ledger helper outside the agent loop: {hits}"
            continue
        fn = function_named(source, "_get_live_pnl")
        outside = [ln for ln in hits if not (fn.lineno <= ln <= (fn.end_lineno or fn.lineno))]
        assert hits and not outside, (
            f"agent.py references the helper outside _get_live_pnl: {outside}"
        )
