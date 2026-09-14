"""CLA-SEC-008 — a unit test reaches no live system and sees no real secret.

Audit 2026-09-13, finding A-5. Measured before this suite existed: four tests resolved
`example.com` through the SSRF guard's `gethostbyname`, four more opened a TCP connection to
the IBKR gateway on 127.0.0.1:5055 through an unpatched `warn_if_session_borrowed` (they
passed offline only because `read_state` swallows the exception — with the gateway running
they issue a real `/tickle`, which is the session keepalive), and importing
`claudia.panel_app` ran `load_dotenv` at module scope, so the operator's real
`ANTHROPIC_API_KEY` and `IBKR_FLEX_TOKEN` sat in the pytest process.

The mechanism is in `tests/conftest.py`; this file is what fails when it stops working.
"""

from __future__ import annotations

import os
import socket

import pytest

from tests.conftest import (
    _REAL_DNS_EXEMPT_TESTS,
    _REAL_LOOPBACK_BIND_TESTS,
    _SECRET_ENV_PREFIXES,
)


def test_a_tcp_connection_is_refused():
    """The IBKR gateway, the Anthropic API and the TradingView CDP port are all TCP."""
    from pytest_socket import SocketBlockedError

    with pytest.raises(SocketBlockedError):
        socket.create_connection(("127.0.0.1", 5055), 0.1)


def test_name_resolution_is_refused():
    """DNS is the half a mocked HTTP client does not cover."""
    from pytest_socket import SocketBlockedError

    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.com", 443)


@pytest.mark.parametrize("prefix", _SECRET_ENV_PREFIXES)
def test_no_secret_bearing_variable_is_visible(prefix):
    """Scrubbed by prefix, not by a hand-kept list of names — a list goes stale silently."""
    leaked = sorted(k for k in os.environ if k.startswith(prefix))
    assert not leaked, f"secret-bearing variables reached a unit test: {leaked}"


def test_importing_the_app_does_not_load_the_operators_env():
    """`claudia.panel_app` calls `load_dotenv` at module scope, which a fixture cannot undo.

    Collection imports the module long before any fixture runs, so the neutralisation has
    to happen in `pytest_configure`. This asserts the result rather than the mechanism: if
    the call ever moves into `main()`, this still passes and the guard can be reconsidered.
    """
    import claudia.panel_app  # noqa: F401

    assert not [k for k in os.environ if k.startswith(_SECRET_ENV_PREFIXES)]


def test_every_exemption_still_names_a_real_test():
    """An exemption that outlives its test is a hole nobody can see.

    The same guard ibkr_core_mcp keeps on its own list (`test_conftest_hygiene.py`): three
    stale names survived there for a week after the tools they covered were deleted.
    """
    import subprocess
    import sys

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    exemptions = _REAL_DNS_EXEMPT_TESTS | _REAL_LOOPBACK_BIND_TESTS
    stale = sorted(name for name in exemptions if name not in collected)
    assert not stale, f"socket exemptions naming no test: {stale}"
