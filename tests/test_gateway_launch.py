"""Tests for `claudia.gateway_launch` — the CLI surface only.

This module used to hold the gateway orchestration and its ordering tests. Both moved to
`claudia.gateway_session` in stage 2 of
`docs/plans/2026-08-06-gateway-session-lifecycle-owner.md`, and the sequence assertions
moved with them to `tests/test_gateway_session.py`, where the owner's call order is
observable.

What is left here is what the CLI is actually responsible for: turning a phase into an
exit code a shell script can branch on, printing without buffering, and reading the
gateway's own log rather than `docker logs`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from claudia.gateway_launch import gateway_log_tail, main
from claudia.gateway_session import SessionPhase, SessionState


def _state(phase: SessionPhase) -> SessionState:
    """A minimal state carrying just the phase the CLI branches on."""
    return SessionState(phase=phase, as_of=datetime.now(UTC), detail="because")


# ── Exit codes: the shell's only view of the verdict ─────────────────────────


@pytest.mark.parametrize(
    "phase,expected",
    [
        (SessionPhase.LIVE, 0),
        (SessionPhase.DOWN, 1),
        (SessionPhase.FREE, 3),
        (SessionPhase.DEGRADED, 3),
        (SessionPhase.BORROWED, 3),
        (SessionPhase.CONTESTED, 3),
    ],
)
def test_exit_code_per_phase(monkeypatch, phase, expected):
    """`start-claudia.sh` branches on these, so every phase must map deliberately.

    0 only for a confirmed session, 1 only for a gateway that is not answering, and 3 for
    everything that blocks a login — a set that is deliberately not empty, because
    "opened the login page and it was never completed" is a real outcome and must not
    look like success.
    """
    session = MagicMock()
    session.establish.return_value = _state(phase)
    with (
        patch("claudia.gateway_launch.get_session", return_value=session),
        patch("ibkr_core_mcp.gateway.GatewayManager"),
    ):
        assert main([]) == expected


def test_the_cli_delegates_rather_than_deciding(monkeypatch):
    """Presentation only: the CLI must not re-implement any part of the sequence.

    It passes a progress sink and the restart flag through and renders what comes back.
    If this ever grows a branch of its own, the launcher and the chat button can drift
    again — which is the failure the plan was written to end.
    """
    session = MagicMock()
    session.establish.return_value = _state(SessionPhase.LIVE)
    with (
        patch("claudia.gateway_launch.get_session", return_value=session),
        patch("ibkr_core_mcp.gateway.GatewayManager"),
    ):
        main(["--allow-restart", "--wait-timeout", "45"])

    kwargs = session.establish.call_args.kwargs
    assert kwargs["allow_restart"] is True
    assert kwargs["login_timeout"] == 45.0
    assert callable(kwargs["emit"])


def test_there_is_no_way_to_skip_the_wait():
    """Returning early would strand the session in AUTHENTICATING.

    Opening the login page suspends every tickler in the system, and a suspension has to
    be bounded by whoever declared it. A `--no-wait` flag would hand the user a login page
    and leave nothing left to end the suspension, so the option deliberately does not
    exist.
    """
    with pytest.raises(SystemExit):
        main(["--no-wait"])


# ── Diagnose: read-only, and against the RIGHT log ───────────────────────────


def test_diagnose_touches_nothing(monkeypatch):
    """A diagnostic that can change the thing it inspects is not a diagnostic."""
    session = MagicMock()
    with (
        patch("claudia.gateway_launch.get_session", return_value=session),
        patch("claudia.gateway_launch.read_state") as read,
        patch("claudia.gateway_launch.gateway_log_tail", return_value="log"),
        patch("ibkr_core_mcp.gateway.GatewayManager") as manager,
    ):
        read.return_value = MagicMock(reachable=True)
        main(["--diagnose"])

    session.establish.assert_not_called()
    session.recover.assert_not_called()
    manager.assert_not_called()


def test_log_tail_reads_the_gateways_own_log_not_docker_logs():
    """`docker logs` shows almost nothing — measured 2026-08-06.

    A probe `GET /` returning HTTP 302 produced no `docker logs` line at all, which very
    nearly led to the false conclusion that a browser was not reaching the gateway. Every
    request the Java process serves goes to a file inside the container instead.
    """
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="line\n")
        gateway_log_tail(lines=5)

    command = run.call_args.args[0]
    assert command[:3] == ["docker", "exec", "ibkr_core_gateway"]
    assert "/app/api_gateway/logs" in command[-1]


def test_log_tail_drops_debug_duplicates_but_keeps_every_request_line():
    """The filter removes a restatement, never evidence.

    DEBUG lines are almost entirely `Remapping Set-cookies […]` repeating the cookie the
    INFO line above already carries. Request lines, status codes and WebSocket messages
    are all INFO and must survive.
    """
    raw = (
        "13:12:26 INFO  BaseServiceProxy : -> GET /v1/api/portfolio/x/ledger,401|91ms\n"
        "13:12:26 DEBUG BaseServiceProxy : Remapping Set-cookies [x-sess-uuid=…] -> \n"
        '13:12:25 INFO  GwWebsocketHandler : response body: {"message":"waiting for session"}\n'
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=raw)
        filtered = gateway_log_tail()
        unfiltered = gateway_log_tail(include_debug=True)

    assert "Remapping" not in filtered
    assert "ledger,401" in filtered
    assert "waiting for session" in filtered
    assert "Remapping" in unfiltered


def test_log_tail_reports_why_it_could_not_read_rather_than_raising():
    """A diagnostic that crashes when things are broken is useless when it is needed."""
    with patch("subprocess.run", side_effect=OSError("no docker")):
        assert "could not read the gateway log" in gateway_log_tail()
