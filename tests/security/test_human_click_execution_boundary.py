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

from tests.security.structural import (
    PACKAGE_DIR,
    assignment_targets,
    functions_calling,
    handlers_bound_to,
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
        if TRIGGER_NAMES & referenced_names(source):
            offenders.append(f"{path.name}: references param trigger")
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


def test_the_click_probe_sees_a_programmatic_press():
    assert "clicks" in assignment_targets("def go(btn):\n    btn.clicks = 1\n")
    assert "trigger" in referenced_names('def go(btn):\n    btn.param.trigger("clicks")\n')
