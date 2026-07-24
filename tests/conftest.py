"""Shared test helpers for the Panel test modules.

Moved here from tests/test_panel_order_flow.py (_get_click_callback) and
tests/test_panel_app.py (_find_buttons) during the Task 5.6b quality review
(2026-07-24) so both modules drive live pn.widgets.Button objects through one
verified idiom instead of drifting copies. Plain functions imported explicitly
(from tests.conftest import ...) — not fixtures — since they take the object
under inspection as an argument.
"""

import panel as pn


def _find_buttons(chat):
    """All pn.widgets.Button objects across chat messages (Phase 3 pattern:
    buttons live in a pn.Column/Row inside a message)."""
    found = []
    for m in chat.objects:
        obj = getattr(m, "object", None)
        if obj is None:
            continue
        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, pn.widgets.Button):
                found.append(node)
            stack.extend(getattr(node, "objects", []))
    return found


def _get_click_callback(button):
    """Extract the real on_click callback from a live pn.widgets.Button, for direct
    invocation in a unit test (no browser, no running Panel server).

    Verified live, 2026-07-22, against the installed panel==1.9.3: Button.on_click(cb)
    is implemented as `self.param.watch(cb, 'clicks', onlychanged=False)` (confirmed via
    `inspect.getsource(pn.widgets.Button.on_click)`) — there is no `_on_click` attribute
    on the button itself. The registered callback lives in
    `button.param.watchers['clicks']['value']`, a list of param Watcher namedtuples;
    Panel's own internal sync watchers (name/label/value mirroring etc.) are always
    registered with `onlychanged=True`, while on_click's own watcher is always
    `onlychanged=False` — confirmed by direct inspection of that list — so filtering on
    that flag reliably isolates the one watcher the production render/send functions
    registered, regardless of how many internal watchers Panel itself adds. Calling
    `.fn` directly and awaiting it (async callbacks are supported natively, confirmed via
    `param.parameterized`'s `iscoroutinefunction(watcher.fn)` branch) exercises the exact
    function a real click would invoke, without needing Panel's async_executor/event-loop
    plumbing that a bare pytest run doesn't have.
    """
    watchers = button.param.watchers["clicks"]["value"]
    matches = [w.fn for w in watchers if not w.onlychanged]
    assert len(matches) == 1, f"expected exactly 1 on_click watcher, found {len(matches)}"
    return matches[0]
