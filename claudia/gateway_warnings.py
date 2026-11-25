"""The two start-up warnings about the gateway's session, on the core's readings.

ClaudIA's words over the core's facts (core F45, design § 3.5): what is read, and what a reading
means, is `ibkr_core_mcp.gateway`'s; that ClaudIA says it at its start, before the banner, is
ClaudIA's. Both are provable-only and silent otherwise, and neither can stop a start.
"""

from __future__ import annotations

import logging

from ibkr_core_mcp.exceptions import ConfigError
from ibkr_core_mcp.gateway.manager import read_login_events
from ibkr_core_mcp.gateway.preflight import (
    EXIT_FREE,
    EXIT_REFUSED,
    GatewayState,
    last_login,
    read_state,
    refine_with_log,
    verdict,
)

from claudia.env_file import gateway_url

log = logging.getLogger(__name__)


def _read(url: str | None) -> GatewayState:
    """The gateway's state; a URL the core refuses (not loopback) is said once and reads as down.

    The core raises before any request, because it will not disable certificate checking for
    another host. A start must not end in a traceback for it: the same URL is refused again,
    with the same sentence, where the toolkit is built.
    """
    try:
        return read_state(url or gateway_url())
    except ConfigError as exc:
        log.error("IBKR_GATEWAY_URL is not usable: %s", exc)
        return GatewayState(reachable=False)


def warn_if_session_borrowed(url: str | None = None) -> str | None:
    """Log a loud error when another IBKR app **provably** holds the gateway's session.

    Called at startup beside `install_check.warn_if_stale` and
    `agent.warn_if_model_lacks_operator_channel` — all three catch a condition that lets
    ClaudIA start cleanly and fail confusingly later.

    ## "Provably", not "probably"

    It fires only on positive evidence: `/sso/validate` returned `RESULT: true` **and**
    named a `CLIENT_APP`, **and** `/tickle` reports the gateway unauthenticated. That is a
    session which exists, belongs to something, and is not usable here — not an inference
    from absence. Every weaker signal was actively misleading when this was diagnosed on
    2026-08-05: `userId` was populated, `ssoExpires` was renewing, and `competing` was
    **false**, because the gateway never got far enough to register a claim.

    Silent when the gateway is unreachable or already authenticated — there is nothing to
    warn about in either, and a warning that fires when it need not is one that gets
    ignored when it must not.

    Args:
        url: Gateway base URL. Defaults to ClaudIA's own.

    Returns:
        The owning `CLIENT_APP` when the warning fired, else None.
    """
    state = _read(url)
    if not (state.reachable and state.sso_valid and state.client_app and not state.authenticated):
        return None
    log.error(
        "LOG OUT OF YOUR IBKR APP FIRST — the gateway is holding an SSO session issued to "
        "%s (user %s), not to itself, so its login will be rejected no matter how many "
        "times you retry the two-factor code. Only one brokerage session exists per "
        "username across Client Portal, TWS and IBKR Mobile. Use the app's **Log Out** "
        "menu item: closing or swiping it away leaves the server-side session alive. Then "
        "run `python -m claudia.gateway_preflight` and log in once it no longer names "
        "another app.",
        state.client_app,
        state.sso_user or "unknown",
    )
    return state.client_app


def warn_if_login_refused(url: str | None = None) -> str | None:
    """Log a loud error at ClaudIA's start when IBKR refused the gateway's last login (gap #93).

    Beside `warn_if_session_borrowed` and in its spirit: provable only — a
    FREE reading whose log shows the last login refused (`refine_with_log`, the preflight's
    rule) — and silent otherwise. The operator's 2026-10-07 sequence ended with ClaudIA
    starting 6.5 minutes after a login IBKR had refused, with nothing on screen saying so.
    An unreadable log stays silent here: the preflight and `--diagnose` say why. The log is
    read only after a FREE reading — a live session needs no `docker exec`.

    Returns:
        The refused login's "YYYY-MM-DD HH:MM:SS" (UTC) when the warning fired, else None.
    """
    found = verdict(_read(url))
    if found[0] != EXIT_FREE:
        return None
    events, problem = read_login_events()
    code, headline, guidance = refine_with_log(found, events, problem)
    last = last_login(events) if events is not None else None
    if code != EXIT_REFUSED or last is None:
        return None
    log.error("%s — %s", headline.upper(), guidance)
    return last.at
