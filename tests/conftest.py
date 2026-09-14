"""Shared test helpers, and the session-wide live-I/O block (CLA-SEC-008).

Two jobs. The Panel helpers were moved here from tests/test_panel_order_flow.py
(_get_click_callback) and tests/test_panel_app.py (_find_buttons) during the Task 5.6b
quality review (2026-07-24) so both modules drive live pn.widgets.Button objects through
one verified idiom instead of drifting copies. Plain functions imported explicitly
(from tests.conftest import ...) — not fixtures — since they take the object under
inspection as an argument.

The second job, added 2026-09-14 (audit 2026-09-13, finding A-5): no unit test opens a
socket, resolves a name, or sees a real secret. What the audit measured beforehand —
four tests resolving `example.com`, four connecting to the IBKR gateway on 127.0.0.1:5055
(one of which is the session keepalive), and the operator's real `ANTHROPIC_API_KEY` in
the pytest process — is asserted against by tests/security/test_no_live_io.py.
"""

import os

import panel as pn
import pytest

# Every entry here resolves a real name through `agent._validate_public_url`'s
# `gethostbyname`, which is the SSRF guard's second layer. Blocking DNS does not make these
# tests fail loudly — `SocketBlockedError` is not `socket.gaierror`, so it escapes the
# guard's narrow except and returns "Invalid URL: ...", and an assertion on "Blocked" would
# then pass for entirely the wrong reason. The matching tests for a *private* address are
# deliberately NOT listed: localhost and a literal 127.x need no DNS to be recognised, so
# they keep their meaning under the block.
_REAL_DNS_EXEMPT_TESTS = {
    "test_fetch_web_page_blocks_redirect_to_private_address",
    "test_fetch_web_page_follows_public_redirect",
    "test_fetch_web_page_blocks_redirect_loop",
    "test_fetch_web_page_ssrf_guard_allows_public",
}

# Tests that bind a real loopback socket because the thing under test IS socket behaviour.
# Kept apart from the DNS list on purpose: this one reaches no name and no remote host, it
# asks the kernel for a free port on 127.0.0.1 and hands it back. Mocking it would leave
# `_port_is_free` asserted against a mock of the only thing it does.
_REAL_LOOPBACK_BIND_TESTS = {
    "test_port_is_free_detects_a_bound_port",
}

# Scrubbed by prefix, never by a list of names: in ibkr_core_mcp the first hand-kept list
# had 14 names, the test that checked it had 7, and neither carried the one that mattered.
# `CLAUDIA_` is deliberately absent — it carries configuration, and `CLAUDIA_LIVE_SCHEMA_CHECK`
# is how the operator opts into the live_api tests.
_SECRET_ENV_PREFIXES = (
    "ANTHROPIC_",
    "IBKR_",
    "GDRIVE_",
    "GOOGLE_",
    "FIRECRAWL_",
    "CRAWL4AI_",
    "TRADINGVIEW_",
)


def _neutralise_dotenv() -> None:
    """Make `load_dotenv` a no-op at every import site, before any claudia module loads.

    This cannot be a fixture. `claudia/panel_app.py` calls `load_dotenv(override=False)` at
    module scope (line 86), and collection imports that module long before the first fixture
    runs — measured 2026-09-13: a probe inside a running test found a real `sk-ant-` key of
    length 108 and `IBKR_FLEX_TOKEN` in `os.environ`. `pytest_configure` runs before
    collection, so patching the `dotenv` module here is what the module-level call picks up.
    """
    noop = lambda *args, **kwargs: False  # noqa: E731
    import dotenv
    import dotenv.main

    dotenv.load_dotenv = noop
    dotenv.main.load_dotenv = noop


def pytest_configure(config):
    """Arm the block for the whole session, import and collection included.

    From the first test onward pytest-socket's own per-test hooks take over (see
    `pytest_collection_modifyitems`); its teardown re-enables sockets after every test,
    which is why this call alone is not the per-test guarantee.
    """
    from pytest_socket import disable_socket

    _neutralise_dotenv()
    for name in [k for k in os.environ if k.startswith(_SECRET_ENV_PREFIXES)]:
        del os.environ[name]
    disable_socket(allow_unix_socket=True)


def pytest_unconfigure(config):
    """Hand the process back as it was found."""
    from pytest_socket import enable_socket

    enable_socket()


def pytest_collection_modifyitems(config, items):
    """Give every test one of pytest-socket's own markers.

    The plugin applies them in its `pytest_runtest_setup`, which runs before any fixture,
    module-scoped ones included — the ordering ibkr_core_mcp's review of 2026-09-13 found
    a session block plus a function-scoped fixture could not give. `live_api` and
    `integration` tests are opt-in and genuinely need the network.
    """
    for item in items:
        needs_network = (
            item.get_closest_marker("integration")
            or item.get_closest_marker("live_api")
            or item.name in _REAL_DNS_EXEMPT_TESTS
            or item.name in _REAL_LOOPBACK_BIND_TESTS
        )
        item.add_marker(pytest.mark.enable_socket if needs_network else pytest.mark.disable_socket)


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
