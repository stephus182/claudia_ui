"""CLA-SEC-002 — execution begins at a human click and nowhere else.

Audit 2026-09-13, weakness B-2. ClaudIA's contract is stronger than the core's two gates,
and it has to be: Gate 1 and Gate 2 stop an *unattended* write, but by the time they fire,
model intent has already become an order body. ClaudIA's rule is that model intent must
first become a button a person looked at and pressed.

On the day of the audit that was true, and asserted nowhere. The three cores had exactly
three non-test callers — the three `on_click` closures — but nothing said `panel_app`,
`panel_sink`, `execution_listener`, `dashboard_poller` or a script could not add a fourth.
The fill subscriber is the realistic one: it already receives an IBKR execution, already has
the session, and "close the rest of the position automatically" is one import away.

The second half is the inverse: a *button* is not a human unless nothing can press it from
code. Panel registers `on_click` as a watcher on the `clicks` parameter, so writing
`button.clicks = 1` or calling `param.trigger("clicks")` fires the handler with no browser
involved — which is exactly how `tests/conftest.py` drives one on purpose.
"""

from __future__ import annotations

import ast

from tests.security.structural import (
    PACKAGE_DIR,
    assignment_targets,
    call_line_numbers,
    called_names,
    first_call_line,
    function_named,
    functions_calling,
    handlers_bound_to,
    keyword_arguments_used,
    package_sources,
    referenced_names,
)

EXECUTION_CORES = frozenset(
    {
        "_execute_staged_order_core",
        "_execute_modify_order_core",
        "_execute_cancel_order_core",
    }
)

# The one module allowed to call a core, because it is the one that draws the buttons.
CLICK_LAYER = "panel_order_flow.py"

# Panel's `Button.on_click(cb)` is `self.param.watch(cb, 'clicks', onlychanged=False)`
# (panel 1.9.3, recorded in tests/conftest.py). Anything that writes `clicks` or triggers
# the parameter fires every registered handler.
CLICK_PARAMETER = "clicks"
TRIGGER_NAMES = frozenset({"trigger"})


def _module(source: str) -> ast.Module:
    """Parse a source string so the subtree checkers can read a whole module."""
    return ast.parse(source)


def test_only_the_click_layer_calls_an_execution_core():
    """Across `claudia/**` and `scripts/**`, one module reaches the cores."""
    offenders = {
        path.name: sorted(EXECUTION_CORES & referenced_names(source))
        for path, source in package_sources()
        if path.name not in (CLICK_LAYER, "order_flow.py")
        and EXECUTION_CORES & referenced_names(source)
    }
    assert not offenders, f"execution cores reached outside {CLICK_LAYER}: {offenders}"


def test_every_core_call_sits_in_a_function_bound_to_a_click():
    """Inside the click layer, a core is called only from a handler `on_click` registers.

    This is what "a human started it" means structurally. A helper that called a core
    without being bound — a retry, a "stage all", a cleanup path — would satisfy the
    module-level rule above and fail here.
    """
    source = (PACKAGE_DIR / CLICK_LAYER).read_text(encoding="utf-8")
    callers = set(functions_calling(source, EXECUTION_CORES))
    bound = handlers_bound_to(source, "on_click")
    assert callers, "no function calls a core — has the click layer moved?"
    assert callers <= bound, f"cores called from unbound functions: {sorted(callers - bound)}"


def test_every_click_handler_claims_the_one_shot_before_anything_else():
    """The guard covers the class of handlers, not the one that has a test.

    Measured 2026-09-14: deleting `if not acted.claim(): return` from five of the six
    handlers — keeping only the staged-order one, which is the only one a behavioural test
    drives — left the whole suite green. Two of those five are live IBKR writes. The
    behavioural test stays; this one is what makes it a rule.

    "Before anything else" is the substance: the claim is what makes two events racing on a
    single-threaded loop resolve to one action, so it has to be the first call in the body,
    ahead of every await.
    """
    source = (PACKAGE_DIR / CLICK_LAYER).read_text(encoding="utf-8")
    handlers = handlers_bound_to(source, "on_click")
    assert len(handlers) == 6, f"expected six bound handlers, found {sorted(handlers)}"

    for name in sorted(handlers):
        fn = function_named(source, name)
        claims = call_line_numbers(fn, ("claim",))
        assert claims, f"{name} does not claim the one-shot"
        assert claims[0] == first_call_line(fn), f"{name} does something before claiming"


def test_every_renderer_takes_its_own_snapshot_of_the_proposal():
    """Same class argument: the snapshot was tested on one renderer of three.

    Deleting `proposal = _snapshot(proposal)` from the cancel and modify renderers left the
    suite green (measured 2026-09-14). What the human reads has to be what the click sends
    on every card, not on the one that happens to be covered.
    """
    source = (PACKAGE_DIR / CLICK_LAYER).read_text(encoding="utf-8")
    renderers = [
        "render_order_proposal",
        "render_cancel_proposal",
        "render_modify_proposal",
    ]
    for name in renderers:
        fn = function_named(source, name)
        assert "_snapshot" in called_names(fn), f"{name} renders the caller's own dict"
        snap = call_line_numbers(fn, ("_snapshot",))
        assert snap[0] == first_call_line(fn), f"{name} reads the proposal before copying it"


def test_nothing_fires_a_button_from_code():
    """No module writes `clicks` or triggers the parameter `on_click` watches.

    The test suite does exactly this through `tests/conftest.py::_get_click_callback`, which
    is why the rule is scoped to the shipped package and the scripts rather than to `tests/`:
    driving a button is how a headless test simulates a person, and it must stay impossible
    anywhere a person is not.
    """
    offenders = []
    for path, source in package_sources():
        if CLICK_PARAMETER in assignment_targets(source):
            offenders.append(f"{path.name}: assigns .{CLICK_PARAMETER}")
        # `param.update(clicks=1)` fires every handler and writes no attribute, so the
        # assignment probe cannot see it (verified against a live Button, 2026-09-14).
        if CLICK_PARAMETER in keyword_arguments_used(source):
            offenders.append(f"{path.name}: passes {CLICK_PARAMETER}= to a call")
        # A *call* to `.trigger(...)`, not any name called `trigger`: a stop trigger is an
        # ordinary variable in this application, and a rule that breaks on one would be
        # deleted rather than obeyed.
        if TRIGGER_NAMES & called_names(_module(source)):
            offenders.append(f"{path.name}: calls param trigger")
        # `setattr(btn, "clicks", 1)` also fires the handler and is invisible to every
        # probe above. Nothing in the package needs dynamic attribute writing.
        if "setattr" in called_names(_module(source)):
            offenders.append(f"{path.name}: calls setattr")
    assert not offenders, "a button can be pressed from code: " + "; ".join(offenders)


def test_the_agent_loop_cannot_reach_a_rendered_button():
    """The sink hands a proposal to the renderer and keeps no reference to the widgets.

    If it kept one, "render then press it" would be two lines in a code path the model's
    turn already runs. The buttons exist only inside `render_*_proposal`'s local scope and
    inside the `pn.Column` sent to the chat.
    """
    sink = (PACKAGE_DIR / "panel_sink.py").read_text(encoding="utf-8")
    assert not {"Button", "on_click", CLICK_PARAMETER} & referenced_names(sink)


# ── Guards on the guards ─────────────────────────────────────────────────────────────────


def test_the_core_probe_sees_a_call_from_a_fill_subscriber():
    snippet = (
        "async def _on_fill(report):\n"
        "    from claudia.order_flow import _execute_cancel_order_core\n"
        "    await _execute_cancel_order_core(report.proposal, send, None, None)\n"
    )
    assert EXECUTION_CORES & referenced_names(snippet) == {"_execute_cancel_order_core"}


def test_the_binding_probe_sees_an_unbound_caller():
    snippet = (
        "async def render(chat, proposal):\n"
        "    async def _on_stage(event):\n"
        "        await _execute_staged_order_core(proposal)\n"
        "    async def _retry_later():\n"
        "        await _execute_staged_order_core(proposal)\n"
        "    btn.on_click(_on_stage)\n"
    )
    callers = set(functions_calling(snippet, EXECUTION_CORES))
    assert callers == {"_on_stage", "_retry_later"}
    assert handlers_bound_to(snippet, "on_click") == {"_on_stage"}
    assert callers - handlers_bound_to(snippet, "on_click") == {"_retry_later"}


def test_the_click_probe_sees_every_way_to_press_a_button():
    """All four were verified to fire a real `pn.widgets.Button`'s handler (2026-09-14)."""
    assert "clicks" in assignment_targets("def go(btn):\n    btn.clicks = 1\n")
    assert "trigger" in called_names(_module('def go(btn):\n    btn.param.trigger("clicks")\n'))
    assert "clicks" in keyword_arguments_used("def go(btn):\n    btn.param.update(clicks=1)\n")
    assert "setattr" in called_names(_module('def go(btn):\n    setattr(btn, "clicks", 1)\n'))
