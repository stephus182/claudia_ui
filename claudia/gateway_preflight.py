"""Read-only pre-flight check: is the IBKR brokerage session free, held, or contested?

Run before touching the Client Portal login page::

    python -m claudia.gateway_preflight

## Why this exists

Only **one** brokerage session exists per username across all IBKR services — Client
Portal, TWS and IBKR Mobile all claim the same one
(https://ibkrcampus.com/docs/web-api/authentication/multiple-sessions.md). Repeated logins
are what escalate into the IB Key challenge/response, so the cheapest fix is to stop
logging in when you do not have to.

## What this diagnosed, 2026-08-05

A login that failed every time for days, with a correctly-formatted 8-digit response code
and the right username. `/tickle` alone could not explain it and actively misled:

    userId      populated        -> looks like a good session
    ssoExpires  renewing         -> looks alive
    competing   FALSE            -> looks uncontested
    authStatus  authenticated:false, connected:false

`/sso/validate` gave the answer in one field: ``CLIENT_APP: "IBKRMOBILE_000.a-000"``. The
gateway was holding a session **issued to the phone**, which it cannot authenticate as, so
every retry was doomed before it started. `competing` was false because the gateway never
got far enough to register a claim — the flag describes a fight, and there was no fight.

Two follow-ons, both measured rather than reasoned about:

* `POST /iserver/auth/ssodh/init` moved `connected` False->True and left `authenticated`
  False, twice. It raises the bridge; it cannot supply an authentication that never
  happened.
* `POST /logout` returned ``{"status": true}`` and the session came straight back with a
  full 10-minute window — because three separate ticklers (the gateway container's own
  `tickler.sh`, a host launchd keepalive, and `ConnectivityChecker`) each renew it every
  60 seconds. The keepalive built to protect a good session cannot tell a good one from a
  borrowed one, and had been preserving an unusable session all day.

## What this CANNOT see — measured 2026-08-06

**A session held by another IBKR app is invisible here until the gateway itself holds
one.** With a live IBKR Mobile login in progress on the same username, `/tickle` and
`/sso/validate` both answered **401** and this check reported **FREE**.

That is not the same condition as `EXIT_BORROWED`, and conflating them would be a
serious misreading:

| Condition | What holds the session | Visible before a login? |
|---|---|---|
| `EXIT_BORROWED` | the **gateway**, holding an SSO session stamped `CLIENT_APP: IBKRMOBILE…` | yes, in `/sso/validate` |
| Another app logged in | **that app**, independently; the gateway holds nothing | **no** |

So `EXIT_FREE` means "the gateway holds nothing", never "a login will succeed" — the
wording of its guidance says exactly that, and must keep saying it. Only one brokerage
session exists per username across Client Portal, TWS and IBKR Mobile
(https://ibkrcampus.com/docs/web-api/authentication/multiple-sessions.md), and IBKR's own
advice for the collision is to log out of the other app first — which is a step this
check can recommend but can never verify in advance.

## Read-only by default, and where it is not

`read_state` issues **two GETs and nothing else** — `/tickle` for whether *a* session is
authenticated, `/sso/validate` for **whose** it is. It never posts, which
`test_read_state_reads_both_endpoints_and_writes_to_neither` pins: a check that can
establish or destroy a session is not a check, and re-authenticating speculatively is what
breaks a fresh login (`feedback-ibkr-session-safety`).

`release_session` is the single exception and is reached only through `--release`. It is
`POST /logout`, which IBKR scopes to the gateway's **local** session, so releasing a
borrowed mobile session does not log the phone out.

The one side effect the GETs do have is benign: `/tickle` resets the keepalive timer,
exactly as `ConnectivityChecker.check_ibkr` does every 60 seconds anyway. Reading can
extend a session's life, never shorten it.
"""

from __future__ import annotations

import logging
import sys
import warnings
from dataclasses import dataclass

import requests
import urllib3

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayState:
    """One reading of the gateway's session, typed. Fields default to "unknown", not False.

    Merges the two endpoints because neither answers the question alone: `/tickle` supplies
    `authenticated`/`connected`/`competing`/`collision`, and `/sso/validate` supplies
    `client_app`/`sso_user`/`sso_valid`.

    `reachable` False means the HTTP call never produced a usable body, so every other
    field is meaningless — the caller must check it first. That distinction matters here
    for the same reason it does on the dashboard: "the gateway said no session" and "the
    gateway did not answer" are opposite claims and lead to opposite actions.

    An unreadable `/sso/validate` degrades to empty strings rather than failing the whole
    read: the `/tickle` half is still worth having, and a missing owner simply means the
    borrowed-session verdict cannot fire — never that it fires wrongly.
    """

    reachable: bool
    authenticated: bool = False
    connected: bool = False
    competing: bool = False
    collision: bool = False
    user_id: int | None = None
    sso_expires_ms: int | None = None
    detail: str = ""
    # From /sso/validate. `client_app` is the field that actually explains a stuck login:
    # measured 2026-08-05 it read "IBKRMOBILE_000.a-000" while the gateway sat at
    # authenticated=False — the gateway was holding the phone's SSO session, which it
    # cannot authenticate as. Nothing in /tickle exposes this, which is why every other
    # signal looked contradictory (userId set, ssoExpires renewing, competing=false).
    client_app: str = ""
    sso_user: str = ""
    sso_valid: bool = False


# Exit codes — distinct so this can gate a shell script, not just inform a human.
EXIT_READY = 0        # a working session already exists; do NOT log in again
EXIT_UNREACHABLE = 1  # gateway process is not answering at all
EXIT_FREE = 2         # nothing holds the session; a login now should succeed
EXIT_CONTESTED = 3    # another IBKR client holds it; logging in now starts a fight
EXIT_BORROWED = 4     # the gateway holds an SSO session issued to a different client app


def read_state(gateway_url: str, timeout: float = 5.0) -> GatewayState:
    """Read the session state. Never raises, never writes.

    Args:
        gateway_url: Base URL including the `/v1/api` suffix, e.g. the value of
            `Config.gateway_url`.
        timeout: Seconds to wait. Short — an unreachable gateway is a normal answer here,
            not an exceptional one.

    Returns:
        A `GatewayState`. On any failure `reachable` is False and `detail` carries the
        reason, rather than an exception reaching the caller: this runs when things are
        already broken, so it has to survive that.
    """
    try:
        # The gateway serves a self-signed cert on localhost; the warning it raises is
        # noise on every single call and would bury this tool's actual output.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(f"{gateway_url}/tickle", timeout=timeout, verify=False)
    except Exception as exc:
        return GatewayState(reachable=False, detail=f"{type(exc).__name__}: {exc}")

    if resp.status_code == 401:
        # 401 is the gateway ANSWERING, not failing: it is alive and holds no authenticated
        # session — exactly the state a freshly started or freshly logged-out gateway is in,
        # and the best possible moment to log in. Folding it into `reachable=False` told the
        # user "Gateway is NOT answering. Start it first" about a gateway that was running
        # perfectly and waiting for them (measured 2026-08-05, straight after a container
        # restart). "Did not answer" and "answered, saying no session" are opposite claims
        # and lead to opposite actions.
        return GatewayState(reachable=True, detail="HTTP 401 — no session yet")
    if resp.status_code != 200:
        return GatewayState(reachable=False, detail=f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception as exc:
        return GatewayState(reachable=False, detail=f"unparseable body: {exc}")

    auth = (body.get("iserver") or {}).get("authStatus") or {}
    sso = _read_sso(gateway_url, timeout)
    return GatewayState(
        reachable=True,
        authenticated=bool(auth.get("authenticated")),
        connected=bool(auth.get("connected")),
        competing=bool(auth.get("competing")),
        # IBKR spells it "collission" on the wire. Kept verbatim on the read side and
        # corrected on ours, rather than propagating a typo through the codebase.
        collision=bool(body.get("collission")),
        user_id=body.get("userId"),
        sso_expires_ms=body.get("ssoExpires"),
        client_app=str(sso.get("CLIENT_APP") or ""),
        sso_user=str(sso.get("USER_NAME") or ""),
        sso_valid=bool(sso.get("RESULT")),
    )


def _read_sso(gateway_url: str, timeout: float) -> dict:
    """`GET /sso/validate`, or `{}` if it cannot be read. Read-only.

    Separate from `/tickle` because it answers a different question: `/tickle` says whether
    *a* session is authenticated, this says **whose** it is. That distinction is the whole
    reason a stuck login is diagnosable at all.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(f"{gateway_url}/sso/validate", timeout=timeout, verify=False)
        if resp.status_code != 200:
            return {}
        body = resp.json()
        # `resp.json()` returns whatever parsed — a bare string or list is valid JSON, and
        # calling `.get` on one raised AttributeError straight out of `read_state`, whose
        # contract is that it never raises. An unexpected shape is exactly what a gateway
        # in a strange state emits, which is when this runs.
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def verdict(state: GatewayState) -> tuple[int, str, str]:
    """Turn a state into (exit code, headline, what to do next).

    Ordered most-actionable first. The `authenticated and connected` case is checked
    before everything else on purpose: it is the one where the correct action is to do
    *nothing*, and it is the one a person is most likely to override out of habit.
    """
    if not state.reachable:
        return (
            EXIT_UNREACHABLE,
            "Gateway is NOT answering",
            f"{state.detail}. Start it first — the login page will not load either.",
        )

    if state.authenticated and state.connected:
        return (
            EXIT_READY,
            "Session is LIVE — do not log in again",
            "Everything is already authenticated. Opening the login page now would "
            "re-authenticate for nothing, and a needless re-login is what escalates into "
            "the IB Key challenge/response.",
        )

    if state.competing or state.collision:
        return (
            EXIT_CONTESTED,
            "Another IBKR client holds the session",
            "IBKR Mobile, TWS or a Client Portal tab is using the single brokerage "
            "session this username gets. Fully close it, re-run this check, and log in "
            "only once it reads FREE — logging in now evicts it and starts the tug-of-war.",
        )

    # Checked before the generic not-authenticated cases: when a valid SSO session exists
    # but the gateway is not authenticated, *whose* session it is explains the whole
    # failure, and no amount of retrying the login page fixes it.
    if state.sso_valid and not state.authenticated and state.client_app:
        return (
            EXIT_BORROWED,
            f"SSO session belongs to {state.client_app}",
            f"The gateway is holding a session that was issued to {state.client_app} "
            f"(user {state.sso_user or 'unknown'}), so it cannot authenticate as itself — "
            "which is why the login is rejected however many times it is retried. Log out "
            "of that app properly (the Log Out menu item; closing or swiping it away "
            "leaves the server-side session alive), then re-run this. If it persists, "
            "`--release` makes the gateway drop the borrowed session.",
        )

    if state.connected and not state.authenticated:
        return (
            EXIT_FREE,
            "Bridge up, but NOT authenticated",
            "IBKR's documented soft-timeout signature. The gateway is talking to IBKR but "
            "the login has not completed. `ssodh/init` raises the bridge and nothing more "
            "— it cannot supply an authentication that never happened, so this state still "
            "needs the login page. Measured 2026-08-05: two inits moved connected "
            "False->True and left authenticated False both times.",
        )

    if state.user_id and not state.connected:
        return (
            EXIT_FREE,
            "SSO alive, brokerage session NOT established",
            f"IBKR knows you (userId {state.user_id}) but no brokerage session exists, and "
            "nothing is competing for it. This is the good moment to log in: complete the "
            "page through to 'Client login succeeds'.",
        )

    return (
        EXIT_FREE,
        "The gateway holds no session",
        "Nothing here holds a session, so a login can proceed. Note that a session held "
        "by another IBKR app is NOT visible from here — measured 2026-08-06, a live IBKR "
        "Mobile login left /tickle and /sso/validate both answering 401 and this check "
        "reading FREE. Only one brokerage session exists per username, so if the login "
        "is rejected, close TWS / IBKR Mobile / any Client Portal tab and try once more.",
    )


DEFAULT_GATEWAY_URL = "https://localhost:5055/v1/api"
"""Matches `Config`'s own default for `IBKR_GATEWAY_URL`."""


def gateway_url() -> str:
    """The gateway URL, read directly from the environment rather than via `Config`.

    `Config.from_env()` raises unless `ANTHROPIC_API_KEY` is set, which has nothing to do
    with reaching the IBKR gateway. A diagnostic that refuses to run because an unrelated
    key is missing is useless precisely when it is needed — a half-configured environment
    is one of the states this is for. `.env` is still loaded so the normal setup works.
    """
    import os

    from dotenv import load_dotenv

    load_dotenv(override=False)
    return os.environ.get("IBKR_GATEWAY_URL", DEFAULT_GATEWAY_URL)


def release_session(gateway_url_: str, timeout: float = 10.0) -> tuple[bool, str]:
    """`POST /logout` — make the gateway drop the session it is holding. Opt-in only.

    Deliberately **not** part of `read_state`: that function's contract is that it cannot
    change anything, and a check which might destroy a session is not a check. This is the
    one call in the module that writes, it is reached only via `--release`, and it is never
    run automatically.

    ⚠ **Scope is NOT documented, and an earlier version of this docstring said it was.**
    IBKR's page says exactly one thing about it — *"Logs the user out of the gateway
    session. Any further activity requires re-authentication."*
    (https://ibkrcampus.com/docs/web-api/v1/endpoints/session/logout-of-the-current-session.md).
    It says nothing about whether that cascades to a session held by TWS or IBKR Mobile.
    This docstring used to assert "it ends the gateway's local session, not the SSO
    session globally — so releasing a borrowed IBKR Mobile session does not log the phone
    out", attributed to IBKR. **IBKR does not say that**; it was our inference wearing the
    documentation's authority, and it is the reassuring half of the claim, which is the
    worst half to get wrong.

    Only one brokerage session exists per username across Client Portal, TWS and IBKR
    Mobile (https://ibkrcampus.com/docs/web-api/authentication/multiple-sessions.md), so
    the *a priori* case for a cascade is real rather than paranoid. Until someone
    deliberately tests it — with nothing at risk on the phone — treat `--release` as
    **potentially ending an IBKR Mobile session too**, and never run it while another app
    is being used to manage a live position.

    This is not `reauthenticate`, which `feedback-ibkr-session-safety` forbids calling
    speculatively — that one re-establishes a session and kills fresh logins. This one only
    drops what is already held, and is worth doing precisely when what is held is unusable.

    Returns:
        (released, detail). `released` reflects IBKR's own `status` field, not merely a
        200 — the endpoint returns `{"status": true}` on success and the distinction
        matters when the point is to know whether the slot is actually free.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.post(f"{gateway_url_}/logout", timeout=timeout, verify=False)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        return bool(resp.json().get("status")), resp.text.strip()
    except Exception:
        return False, resp.text.strip()[:120]


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
        url: Gateway base URL. Defaults to the environment's.

    Returns:
        The owning `CLIENT_APP` when the warning fired, else None.
    """
    state = read_state(url or gateway_url())
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


_MARK = {EXIT_READY: "OK", EXIT_UNREACHABLE: "DOWN", EXIT_FREE: "FREE",
         EXIT_CONTESTED: "BUSY", EXIT_BORROWED: "BORROWED"}


def _report(state: GatewayState) -> int:
    """Print one verdict block and return its exit code."""
    code, headline, guidance = verdict(state)
    print(f"[{_MARK[code]}] {headline}\n")
    print(f"  {guidance}\n")
    if state.reachable:
        print(f"  authenticated={state.authenticated}  connected={state.connected}  "
              f"competing={state.competing}  collision={state.collision}")
        if state.client_app:
            print(f"  sso owner={state.client_app}  user={state.sso_user}")
        if state.sso_expires_ms is not None:
            print(f"  sso expires in ~{state.sso_expires_ms / 60000:.1f} min")
    return code


def main(argv: list[str] | None = None) -> int:
    """Print the verdict; with `--release`, drop a held session first and re-check."""
    import sys as _sys

    args = _sys.argv[1:] if argv is None else argv
    url = gateway_url()

    if "--release" in args:
        print("Releasing the gateway's held session (POST /logout)…\n")
        released, detail = release_session(url)
        print(f"  released={released}  {detail}\n")
        print("Re-checking:\n")

    return _report(read_state(url))


if __name__ == "__main__":
    sys.exit(main())
