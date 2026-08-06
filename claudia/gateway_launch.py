"""Command line over `claudia.gateway_session` — the launcher and the diagnostic.

This module is **presentation only**. Every decision about the gateway — whether to start
a container, whether to open a login page, whether a session is usable — belongs to
`claudia.gateway_session`, which `start-claudia.sh`, the in-chat **Start IBKR Gateway**
button and this CLI all share. One authority, so the three cannot drift.

    python -m claudia.gateway_launch            # bring the session up and confirm it
    python -m claudia.gateway_launch --diagnose # read-only: state + the gateway\'s own log

Until 2026-08-06 this file held the orchestration itself, and before that
`start-claudia.sh` called `GatewayManager.startup()` with no pre-flight at all. Both are
gone: see `docs/plans/2026-08-06-gateway-session-lifecycle-owner.md` for why owning the
sequence in one place was the only fix that converged.
"""

from __future__ import annotations

import logging

from ibkr_core_mcp.gateway import GatewayManager

from claudia.gateway_preflight import gateway_url, read_state, verdict
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


def gateway_log_tail(
    lines: int = 60, container: str = GATEWAY_CONTAINER, include_debug: bool = False
) -> str:
    """The last `lines` of the gateway\'s internal log, or a reason it could not be read.

    Diagnostics only — never on a success path. This is the log that shows `/sso/*`
    traffic, the WebSocket\'s `{"message": "waiting for session"}` and every endpoint\'s
    real status code, none of which reach `docker logs`.

    `include_debug` is False by default because the DEBUG lines are almost entirely
    `Remapping Set-cookies […]` restating the cookie the INFO line above already logged.
    Dropping them filters a **duplicate**, not evidence — every request line, status code
    and WebSocket message is INFO and always kept.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             f'tail -{int(lines)} {GATEWAY_LOG_DIR}/gw.$(date +%Y-%m-%d).log'],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return f"(could not read the gateway log: {type(exc).__name__}: {exc})"
    if result.returncode != 0:
        return f"(could not read the gateway log: {result.stderr.strip()[:200]})"
    if include_debug:
        return result.stdout
    return "\n".join(line for line in result.stdout.splitlines() if " DEBUG " not in line)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the requested operation, and map the phase to an exit code.

    Exit codes let `start-claudia.sh` branch: 0 the session is usable or a login is under
    way, 1 the gateway is not answering, 3 something blocks a login (contested, borrowed,
    or a login that did not complete).
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--wait-timeout", type=float, default=300.0,
        help="Seconds to wait for a login to complete (default 300). There is no "
             "--no-wait: opening the login page suspends every tickler in the system, "
             "and a suspension has to be bounded by whoever declared it. Returning early "
             "would leave the session in AUTHENTICATING with nothing left to end it.",
    )
    parser.add_argument(
        "--allow-restart", action="store_true",
        help="Recreate the container if it holds a session borrowed from another IBKR "
             "app. Off by default: a restart destroys a live session.",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Print the session state and the gateway\'s INTERNAL log, then exit without "
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
        code, headline, guidance = verdict(state)
        out(f"  {headline}")
        out(f"  {guidance}")
        out()
        out(f"  raw: {state}")
        out()
        out("─" * 72)
        out(gateway_log_tail())
        return 0 if code in (0, 2) else 3

    session = get_session()
    result = session.establish(
        GatewayManager(),
        emit=out,
        allow_restart=args.allow_restart,
        login_timeout=args.wait_timeout,
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
