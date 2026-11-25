"""The two start-up warnings: provable only, silent otherwise, and neither can stop a start."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from ibkr_core_mcp.gateway.preflight import GatewayState, LoginEvent

from claudia.gateway_warnings import warn_if_login_refused, warn_if_session_borrowed

_LOGGER = "claudia.gateway_warnings"

_REFUSED_LAST = [
    LoginEvent("2026-10-07 18:13:51", "login", ""),
    LoginEvent("2026-10-07 18:13:51", "giving up", ""),
]
_VALIDATED_LAST = [
    LoginEvent("2026-10-07 18:13:51", "login", ""),
    LoginEvent("2026-10-07 18:13:51", "authenticated", ""),
]


def test_the_startup_warning_fires_only_on_positive_proof(caplog):
    """Named owner + valid SSO + unauthenticated gateway: the state measured live on 2026-08-05.
    Break: the `authenticated` condition dropped — the silent cases red."""
    borrowed = GatewayState(
        reachable=True, sso_valid=True, client_app="IBKRMOBILE_000.a-000", sso_user="ibkruser"
    )
    with (
        patch("claudia.gateway_warnings.read_state", return_value=borrowed),
        caplog.at_level(logging.ERROR, logger=_LOGGER),
    ):
        owner = warn_if_session_borrowed()

    assert owner == "IBKRMOBILE_000.a-000"
    assert "LOG OUT" in caplog.text
    assert "ibkruser" in caplog.text


@pytest.mark.parametrize(
    "state",
    [
        GatewayState(
            reachable=True,
            authenticated=True,
            connected=True,
            sso_valid=True,
            client_app="IBKRMOBILE_000.a-000",
        ),
        GatewayState(reachable=False),
        GatewayState(reachable=True, sso_valid=False, client_app=""),
        GatewayState(reachable=True, sso_valid=True, client_app=""),
    ],
    ids=["authenticated", "unreachable", "no sso", "no owner named"],
)
def test_the_startup_warning_stays_silent_without_proof(state, caplog):
    """A warning that fires when it need not is one that gets ignored when it must not."""
    with (
        patch("claudia.gateway_warnings.read_state", return_value=state),
        caplog.at_level(logging.ERROR, logger=_LOGGER),
    ):
        assert warn_if_session_borrowed() is None
    assert caplog.text == ""


@pytest.mark.parametrize(
    ("reading", "events", "fires"),
    [
        (GatewayState(reachable=True), (_REFUSED_LAST, ""), True),
        (GatewayState(reachable=True), (_VALIDATED_LAST, ""), False),
        (GatewayState(reachable=True), (None, "no such container"), False),
        (
            GatewayState(reachable=True, authenticated=True, connected=True),
            (_REFUSED_LAST, ""),
            False,
        ),
    ],
    ids=["free-and-refused", "free-and-validated", "log-unreadable", "live"],
)
def test_the_startup_warning_fires_only_on_a_refused_last_login(caplog, reading, events, fires):
    """The operator's 2026-10-07 sequence ended with ClaudIA starting 6.5 minutes after a login
    IBKR had refused, with nothing on screen saying so. Silent on an unreadable log, and on any
    reading that is not FREE the log is not read. Break: the FREE check dropped — live red."""
    with (
        patch("claudia.gateway_warnings.read_state", return_value=reading),
        patch("claudia.gateway_warnings.read_login_events", return_value=events) as read,
        caplog.at_level(logging.ERROR, logger=_LOGGER),
    ):
        fired = warn_if_login_refused()

    assert (fired, "IBKR REFUSED" in caplog.text) == (
        ("2026-10-07 18:13:51" if fires else None),
        fires,
    )
    if reading.authenticated:
        read.assert_not_called()


@pytest.mark.parametrize("warn", [warn_if_session_borrowed, warn_if_login_refused])
def test_a_url_the_core_refuses_is_said_and_does_not_stop_the_start(warn, monkeypatch, caplog):
    """The core raises before any request for a host that is not loopback, where ClaudIA's own
    reader returned "unreachable"; `main()` runs these before the banner. The real `read_state`
    is what runs here. Break: the `except ConfigError` removed — red on the traceback."""
    monkeypatch.setenv("IBKR_GATEWAY_URL", "https://gateway.invalid/v1/api")
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        assert warn() is None
    assert "IBKR_GATEWAY_URL is not usable" in caplog.text
