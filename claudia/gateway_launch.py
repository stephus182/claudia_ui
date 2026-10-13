"""Command line over `claudia.gateway_session` — the launcher and the diagnostic.

This module is **presentation only**. Every decision about the gateway — whether to start
a container, whether to open a login page, whether a session is usable — belongs to
`claudia.gateway_session`, which `start-claudia.sh`, the in-chat **Start IBKR Gateway**
button and this CLI all share. One authority, so the three cannot drift.

    python -m claudia.gateway_launch            # bring the session up and confirm it
    python -m claudia.gateway_launch --diagnose # read-only: state + the login timeline

Two things live here that are not presentation, by necessity (gap #93): the one read of the
gateway's own log, `read_login_events` — a process spawn, and processes are spawned only here
and in `tradingview.py` (CLA-SEC-010) — and the startup warning built on it. Neither decides
anything about the session: the rule is `gateway_preflight`'s, every decision still
`gateway_session`'s, which receives the read from its callers.

Until 2026-08-06 this file held the orchestration itself, and before that
`start-claudia.sh` called `GatewayManager.startup()` with no pre-flight at all. Both are
gone: see `docs/plans/archive/gateway/2026-08-06-gateway-session-lifecycle-owner.md` for why owning the
sequence in one place was the only fix that converged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ibkr_core_mcp.gateway import GatewayManager

from claudia.gateway_preflight import (
    EXIT_FREE,
    EXIT_REFUSED,
    LOGIN_PHRASES,
    GatewayState,
    LoginEvent,
    gateway_url,
    last_login,
    login_events,
    newest_login_index,
    read_state,
    refine_with_log,
    verdict,
)
from claudia.gateway_session import SessionPhase, get_session

log = logging.getLogger(__name__)

# The gateway\'s real log. `docker logs` shows only container start-up chatter and the
# tickler\'s status codes — verified 2026-08-06 by probing `GET /` (HTTP 302) and finding
# no corresponding line. Every request the Java process serves is written here instead,
# which is where a failing login is actually visible.
GATEWAY_LOG_DIR = "/app/api_gateway/logs"

GATEWAY_CONTAINER = GatewayManager.CONTAINER_NAME
"""The container's name, **derived** rather than restated.

ibkr_core_mcp owns the container's identity; it creates it and it is the only thing that
can rename it. Until 2026-08-06 this was the literal `"ibkr_core_gateway"`, which meant a
rename in that repo would have left this module running `docker exec` against a container
that no longer existed — and the failure would have surfaced as "could not read the
gateway log" during a diagnosis, which is the worst moment for a tool to be quietly wrong
about what it is inspecting.

`scripts/gateway-reset.sh` already derived it this way and was the model for this."""


def login_timeline(events: Sequence[LoginEvent] | None, problem: str) -> list[str]:
    """`--diagnose`'s view of the gateway's log: the newest login and what followed it.

    Time, event and code per line — never a line of the log. Until gap #93 this printed the
    log's last 60 lines minus DEBUG, and on 2026-10-12 two of the 39 it would have printed
    carried the account number; request lines and their status codes are no longer shown
    (operator, 2026-10-12: "timeline only … nothing can leak"). With no login recorded, the
    last 20 events are shown — a refused sequence is 12 at most, measured.
    """
    if events is None:
        return [f"The gateway's own log could not be read: {problem}"]
    newest = newest_login_index(events)
    if newest is None:
        shown = list(events[-20:])
        head = f"Gateway log — no login recorded; the last {len(shown)} events:"
    else:
        shown = list(events[newest:])
        head = (
            f"Gateway log — the last login and what followed ({len(shown)} events; "
            f"{len(events) - len(shown)} earlier not shown):"
        )
    return [head] + [f"  {e.at} UTC  {e.event}{' ' + e.code if e.code else ''}" for e in shown]


def warn_if_login_refused(url: str | None = None) -> str | None:
    """Log a loud error at ClaudIA's start when IBKR refused the gateway's last login (gap #93).

    Beside `gateway_preflight.warn_if_session_borrowed` and in its spirit: provable only — a
    FREE reading whose log shows the last login refused (`refine_with_log`, the preflight's
    rule) — and silent otherwise. The operator's 2026-10-07 sequence ended with ClaudIA
    starting 6.5 minutes after a login IBKR had refused, with nothing on screen saying so.
    An unreadable log stays silent here: the preflight and `--diagnose` say why. The log is
    read only after a FREE reading — a live session needs no `docker exec`.

    Returns:
        The refused login's "YYYY-MM-DD HH:MM:SS" (UTC) when the warning fired, else None.
    """
    found = verdict(read_state(url or gateway_url()))
    if found[0] != EXIT_FREE:
        return None
    events, problem = read_login_events()
    code, headline, guidance = refine_with_log(found, events, problem)
    last = last_login(events) if events is not None else None
    if code != EXIT_REFUSED or last is None:
        return None
    log.error("%s — %s", headline.upper(), guidance)
    return last.at


def _flags(state: GatewayState) -> str:
    """A reading's flags for `--diagnose` — not `repr(state)`, which carried the IBKR user id
    and the username (both set on the live gateway, 2026-10-12; gap #101). Those two read
    only "set" or "empty"; a field added to `GatewayState` later is not printed until named here."""
    return (
        f"reachable={state.reachable} authenticated={state.authenticated} "
        f"connected={state.connected} competing={state.competing} collision={state.collision} "
        f"sso_valid={state.sso_valid} client_app={state.client_app or '—'} "
        f"sso_expires_ms={state.sso_expires_ms} "
        f"user_id={'set' if state.user_id is not None else 'empty'} "
        f"sso_user={'set' if state.sso_user else 'empty'} detail={state.detail!r}"
    )


def _docker_env() -> dict[str, str]:
    """The docker CLI's environment: PATH, HOME and any DOCKER_* variable — nothing else.

    The explicit environment of the spawn recipe (`docs/security-architecture.md` § 10): nothing
    of ClaudIA's, the API key included, reaches the child. Measured 2026-10-12 on the operator's
    machine: PATH alone runs `docker exec`; HOME holds the CLI's own configuration (contexts),
    and DOCKER_* select a daemon where one is configured that way.
    """
    import os

    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}


def read_login_events(
    container: str = GATEWAY_CONTAINER, timeout: float = 15.0
) -> tuple[list[LoginEvent] | None, str]:
    """The login events of the gateway's own log, or None and why they could not be read.

    One command and no shell anywhere: `docker exec <container> grep -rHF
    --include=gw.[0-9]*.log -e <phrase>… /app/api_gateway/logs`. grep's own `--include` picks the
    dated files (the `gw.message.*` files are not read), and only the lines carrying one of
    `LOGIN_PHRASES` leave the container, to be cut to time, event and code by `login_events`
    before anything else sees them. The command ends on its own over finite files; the timeout
    is a backstop, because stopping a `docker exec` client leaves its command running inside
    the container (measured 2026-10-12).

    The exit rule, measured the same day against the real container: 0 → parse; 1 with an
    empty stderr → no line matched, so no login is recorded; anything else → unreadable, the
    first line of stderr the reason. Exit 1 alone cannot tell "no line matched" from "no such
    container" — only stderr does — and a partial read (a file gone) prints lines AND exits 2;
    those lines are never used, since what the missing file held is exactly what would make
    the verdict answer about an older login. Never raises for any of it: the caller keeps the
    HTTP verdict and says why the log could not help.

    Returns:
        `(events, "")` when read — an empty list when no login line exists — else
        `(None, reason)`.
    """
    import subprocess

    command = ["docker", "exec", container, "grep", "-rHF", "--include=gw.[0-9]*.log"]
    for phrase in LOGIN_PHRASES:
        command += ["-e", phrase]
    command.append(GATEWAY_LOG_DIR)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=_docker_env()
        )
    except subprocess.TimeoutExpired:
        return None, f"docker did not answer within {timeout:g} s"
    except OSError as exc:
        return None, f"docker could not be run: {exc}"
    problem = result.stderr.strip()
    if result.returncode == 0 or (result.returncode == 1 and not problem):
        try:
            return login_events(result.stdout), ""
        except ValueError as exc:
            return None, str(exc)
    return None, (
        problem.splitlines()[0][:200] if problem else f"docker exited {result.returncode}"
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the requested operation, and map the phase to an exit code.

    Exit codes let `start-claudia.sh` branch: 0 the session is usable or a login is under
    way, 1 the gateway is not answering, 3 something blocks a login (contested, borrowed,
    or a login that did not complete).
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for a login to complete (default 300). There is no "
        "--no-wait: opening the login page suspends every tickler in the system, "
        "and a suspension has to be bounded by whoever declared it. Returning early "
        "would leave the session in AUTHENTICATING with nothing left to end it.",
    )
    parser.add_argument(
        "--allow-restart",
        action="store_true",
        help="Recreate the container if it holds a session borrowed from another IBKR "
        "app. Off by default: a restart destroys a live session.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Print the session state and the gateway's INTERNAL log, then exit without "
        "touching anything.",
    )
    args = parser.parse_args(argv)

    def out(line: str = "") -> None:
        """Print and flush.

        `print` alone block-buffers whenever stdout is not a TTY — a pipe, a log file,
        `start-claudia.sh` redirected anywhere. This command can then sit silently for the
        whole `--wait-timeout` while working normally, which is indistinguishable from a
        hang at exactly the moment the user is waiting on it. Measured 2026-08-06: a
        backgrounded `--wait` run produced a zero-byte log while polling correctly.
        """
        print(line, flush=True)

    url = gateway_url()

    if args.diagnose:
        state = read_state(url)
        events, problem = read_login_events()
        code, headline, guidance = refine_with_log(verdict(state), events, problem)
        out(f"  {headline}")
        for paragraph in guidance.split("\n\n"):
            out(f"  {paragraph}")
        out()
        out(f"  {_flags(state)}")
        out()
        out("─" * 72)
        for line in login_timeline(events, problem):
            out(f"  {line}")
        return 0 if code in (0, 2) else 3

    session = get_session()
    result = session.establish(
        GatewayManager(),
        emit=out,
        allow_restart=args.allow_restart,
        login_timeout=args.wait_timeout,
        read_login=read_login_events,
    )

    out()
    out(f"  {result.phase.value.upper()} — {result.detail}")
    out()

    if result.phase is SessionPhase.DOWN:
        return 1
    if result.phase is SessionPhase.LIVE:
        return 0
    return 3


def _cli() -> None:  # pragma: no cover - thin entry point
    """Console entry point: configure minimal logging and exit with `main`\'s code."""
    import sys

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    _cli()
