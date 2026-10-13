"""Tests for `claudia.gateway_launch` — the CLI surface only.

This module used to hold the gateway orchestration and its ordering tests. Both moved to
`claudia.gateway_session` in stage 2 of
`docs/plans/archive/gateway/2026-08-06-gateway-session-lifecycle-owner.md`, and the sequence assertions
moved with them to `tests/test_gateway_session.py`, where the owner's call order is
observable.

What is left here is what the CLI is actually responsible for: turning a phase into an
exit code a shell script can branch on, printing without buffering, and reading the
gateway's own log rather than `docker logs`.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from claudia.gateway_launch import main, read_login_events
from claudia.gateway_preflight import LOGIN_PHRASES, GatewayState, LoginEvent
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
        patch("claudia.gateway_launch.read_state", return_value=GatewayState(reachable=True)),
        patch("claudia.gateway_launch.read_login_events", return_value=([], "")),
        patch("ibkr_core_mcp.gateway.GatewayManager") as manager,
    ):
        main(["--diagnose"])

    session.establish.assert_not_called()
    session.recover.assert_not_called()
    manager.assert_not_called()


_A_REFUSAL_AFTER_A_GOOD_LOGIN = [
    LoginEvent("2026-10-07 17:00:00", "login", ""),
    LoginEvent("2026-10-07 17:00:00", "authenticated", ""),
    LoginEvent("2026-10-07 18:13:51", "login", ""),
    LoginEvent("2026-10-07 18:13:51", "validate", "401"),
    LoginEvent("2026-10-07 18:13:51", "retry", "7"),
    LoginEvent("2026-10-07 18:13:51", "giving up", ""),
]


@pytest.mark.parametrize(
    ("events", "problem", "block"),
    [
        (
            _A_REFUSAL_AFTER_A_GOOD_LOGIN,
            "",
            [
                "Gateway log — the last login and what followed (4 events; 2 earlier not shown):",
                "  2026-10-07 18:13:51 UTC  login",
                "  2026-10-07 18:13:51 UTC  validate 401",
                "  2026-10-07 18:13:51 UTC  retry 7",
                "  2026-10-07 18:13:51 UTC  giving up",
            ],
        ),
        (
            [LoginEvent("2026-10-09 19:51:22", "giving up", "")],
            "",
            [
                "Gateway log — no login recorded; the last 1 events:",
                "  2026-10-09 19:51:22 UTC  giving up",
            ],
        ),
        (
            None,
            "Error response from daemon: No such container: x",
            [
                "The gateway's own log could not be read: Error response from daemon: No such container: x"
            ],
        ),
    ],
    ids=["from-the-newest-login", "no-login-recorded", "unreadable"],
)
def test_diagnose_prints_the_last_login_and_what_followed(capsys, events, problem, block):
    """Not the raw log: until gap #93, `--diagnose` printed its last 60 lines minus DEBUG,
    and on 2026-10-12 two of the 39 it would have printed carried the account number. The
    timeline is time, event and code, from the newest login — where the answer is."""
    with (
        patch("claudia.gateway_launch.read_state", return_value=GatewayState(reachable=True)),
        patch("claudia.gateway_launch.read_login_events", return_value=(events, problem)),
    ):
        main(["--diagnose"])
    out = capsys.readouterr().out
    assert "\n".join(f"  {line}" for line in block) in out


def test_diagnose_never_prints_the_user_id_and_its_flags_line_no_username(capsys):
    """`raw: {state}` printed the whole reading — the IBKR user id and the username with it,
    both set on the live gateway 2026-10-12 (gap #101 c) — and the FREE "SSO alive" text
    printed the user id too (#101 b). Operator, 2026-10-12: drop the user id; the username
    may show where it says whose session a borrowed one is. So: the id nowhere, and the flags
    line names neither (those two read only set or empty)."""
    state = GatewayState(
        reachable=True, user_id=12345678, sso_user="synthetic_user", sso_valid=True
    )
    with (
        patch("claudia.gateway_launch.read_state", return_value=state),
        patch("claudia.gateway_launch.read_login_events", return_value=([], "")),
    ):
        main(["--diagnose"])
    out = capsys.readouterr().out
    flags = next(line for line in out.splitlines() if line.lstrip().startswith("reachable="))
    assert "12345678" not in out
    assert "synthetic_user" not in flags


def test_diagnose_reads_a_free_gateway_through_its_log():
    """The refinement the preflight applies, applied here too: a FREE reading whose last
    login IBKR refused is REFUSED, and `--diagnose`'s rule maps it to 3 — something
    blocks a login."""
    with (
        patch("claudia.gateway_launch.read_state", return_value=GatewayState(reachable=True)),
        patch(
            "claudia.gateway_launch.read_login_events",
            return_value=(_A_REFUSAL_AFTER_A_GOOD_LOGIN, ""),
        ),
    ):
        assert main(["--diagnose"]) == 3


# ── Gap #93: the one read of the gateway's log — shell-free, an explicit environment ──


def test_the_log_read_is_one_shell_free_bounded_command():
    """CLA-SEC-010 — list form, never a shell: here not even inside the container, because
    grep's own `--include` picks the dated files (measured 2026-10-12: the same 93 lines as the
    shell glob) — and a command that ends on its own, the timeout only a backstop (stopping a
    `docker exec` client leaves its command running in the container, measured that day)."""
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        read_login_events()

    phrases = [arg for phrase in LOGIN_PHRASES for arg in ("-e", phrase)]
    assert run.call_args.args[0] == [
        "docker",
        "exec",
        "ibkr_core_gateway",
        "grep",
        "-rHF",
        "--include=gw.[0-9]*.log",
        *phrases,
        "/app/api_gateway/logs",
    ]
    assert run.call_args.kwargs["timeout"] > 0


def test_the_docker_cli_gets_path_home_and_docker_variables_only():
    """The § 10 recipe's explicit environment: nothing of ClaudIA's — the API key included —
    reaches the child. Measured 2026-10-12: PATH alone runs `docker exec` here; HOME holds the
    CLI's configuration, DOCKER_* choose a daemon."""
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "DOCKER_HOST": "unix:///probe.sock",
        "ANTHROPIC_API_KEY": "sk-not-a-real-key",
        "IBKR_GATEWAY_URL": "https://localhost:5055/v1/api",
    }
    with patch.dict(os.environ, environment, clear=True), patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        read_login_events()

    assert run.call_args.kwargs["env"] == {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "DOCKER_HOST": "unix:///probe.sock",
    }


_PHRASE_LINE = "/app/api_gateway/logs/gw.2026-10-07.log:17:53:14.123 filler Client login succeeds\n"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            MagicMock(returncode=0, stdout=_PHRASE_LINE, stderr=""),
            ([LoginEvent("2026-10-07 17:53:14", "login", "")], ""),
        ),
        (MagicMock(returncode=1, stdout="", stderr=""), ([], "")),
        (
            MagicMock(
                returncode=1,
                stdout="",
                stderr="Error response from daemon: No such container: ibkr_core_gateway\n",
            ),
            (None, "Error response from daemon: No such container: ibkr_core_gateway"),
        ),
        (
            MagicMock(
                returncode=2,
                stdout=_PHRASE_LINE,
                stderr="grep: /app/api_gateway/logs/x.log: No such file or directory\n",
            ),
            (None, "grep: /app/api_gateway/logs/x.log: No such file or directory"),
        ),
        (
            MagicMock(
                returncode=0,
                stdout="/app/api_gateway/logs/gw.2026-10-07.log:no time giving up\n",
                stderr="",
            ),
            (None, "gateway log format not recognised: a 'giving up' line has no leading time"),
        ),
        (
            OSError(2, "No such file or directory", "docker"),
            (None, "docker could not be run: [Errno 2] No such file or directory: 'docker'"),
        ),
        (subprocess.TimeoutExpired(["docker"], 15), (None, "docker did not answer within 15 s")),
    ],
    ids=[
        "read",
        "no-line-matched-is-no-login",
        "no-such-container-also-exits-1",
        "a-partial-read-is-not-parsed",
        "a-line-without-a-time",
        "no-docker-program",
        "timed-out",
    ],
)
def test_the_log_read_fails_open_with_its_reason(outcome, expected):
    """The exit rule, measured 2026-10-12 against the real container: 0 → parse; 1 with an
    empty stderr → nothing matched; anything else → unreadable, stderr's first line the reason.
    Exit 1 alone cannot tell "no line matched" from "no such container" — only stderr does —
    and a partial read prints lines AND exits 2: those lines are never used, since what the
    missing file held is exactly what would make the verdict answer about an older login."""
    effect = (
        {"side_effect": outcome}
        if isinstance(outcome, BaseException)
        else {"return_value": outcome}
    )
    with patch("subprocess.run", **effect):
        assert read_login_events() == expected


def test_the_launcher_hands_the_owner_the_log_read():
    """Gap #93: the owner names a refused last login before the page and stops the wait early
    on a newer refusal only when its caller hands it the log read — the spawn lives here
    (CLA-SEC-010). The chat button hands it the same function (`tests/test_panel_app.py`)."""
    session = MagicMock()
    session.establish.return_value = _state(SessionPhase.LIVE)
    with (
        patch("claudia.gateway_launch.get_session", return_value=session),
        patch("ibkr_core_mcp.gateway.GatewayManager"),
    ):
        main([])

    assert session.establish.call_args.kwargs["read_login"] is read_login_events


# ── Gap #93, piece 6: the startup warning beside the borrowed-session check ───────

_REFUSED_LAST = [
    LoginEvent("2026-10-07 18:13:51", "login", ""),
    LoginEvent("2026-10-07 18:13:51", "giving up", ""),
]
_VALIDATED_LAST = [
    LoginEvent("2026-10-07 18:13:51", "login", ""),
    LoginEvent("2026-10-07 18:13:51", "authenticated", ""),
]


@pytest.mark.parametrize(
    ("reading", "log", "fires"),
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
def test_the_startup_warning_fires_only_on_a_refused_last_login(caplog, reading, log, fires):
    """The operator's 10-07 sequence ended with ClaudIA starting 6.5 minutes after a login IBKR
    had refused, with nothing on screen saying so. Provable only, like the borrowed check:
    a FREE reading whose last login was refused. Silent otherwise — an unreadable log is the
    preflight's and `--diagnose`'s to explain — and on any other reading the log is not read."""
    import logging

    from claudia.gateway_launch import warn_if_login_refused

    with (
        patch("claudia.gateway_launch.read_state", return_value=reading),
        patch("claudia.gateway_launch.read_login_events", return_value=log) as read,
        caplog.at_level(logging.ERROR, logger="claudia.gateway_launch"),
    ):
        fired = warn_if_login_refused()

    assert (fired, "IBKR REFUSED" in caplog.text) == (
        ("2026-10-07 18:13:51" if fires else None),
        fires,
    )
    if reading.authenticated:
        read.assert_not_called()
