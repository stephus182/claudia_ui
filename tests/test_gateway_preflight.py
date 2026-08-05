"""Tests for the read-only gateway pre-flight check.

The tool exists because only one brokerage session exists per username across all IBKR
services, so a needless re-login is what escalates into IB Key challenge/response. Its
whole value is telling those states apart, so each verdict is pinned here.
"""

import logging
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from claudia.gateway_preflight import (
    EXIT_BORROWED,
    EXIT_CONTESTED,
    EXIT_FREE,
    EXIT_READY,
    EXIT_UNREACHABLE,
    GatewayState,
    read_state,
    release_session,
    verdict,
    warn_if_session_borrowed,
)


def _tickle(**auth):
    """A /tickle response with the given authStatus fields."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "session": "abc", "ssoExpires": 600000, "collission": auth.pop("collission", False),
        "userId": auth.pop("userId", 99999999),
        "iserver": {"authStatus": {"authenticated": False, "connected": False,
                                   "competing": False, **auth}},
    }
    return resp


def test_a_live_session_says_do_not_log_in_again():
    """The one state whose correct action is to do nothing — and the one most likely
    to be overridden out of habit, which is why it is checked first."""
    code, headline, guidance = verdict(
        GatewayState(reachable=True, authenticated=True, connected=True)
    )
    assert code == EXIT_READY
    assert "do not log in again" in headline.lower()
    assert "escalat" in guidance.lower()


@pytest.mark.parametrize("field", ["competing", "collision"])
def test_a_contested_session_is_reported_before_any_login_advice(field):
    """Another IBKR client holding the session must never read as 'free to log in'."""
    code, headline, _ = verdict(GatewayState(reachable=True, **{field: True}))
    assert code == EXIT_CONTESTED
    assert "another ibkr client" in headline.lower()


def test_bridge_up_but_unauthenticated_is_its_own_state():
    """connected=True, authenticated=False is the soft-timeout signature, not "no session".

    Measured live 2026-08-05: `ssodh/init` moved connected False->True and left
    authenticated False. The first version of `verdict` had no branch for it and reported
    "No session — free to log in", which described the wrong state.
    """
    code, headline, guidance = verdict(
        GatewayState(reachable=True, connected=True, authenticated=False, user_id=1)
    )
    assert code == EXIT_FREE
    assert "not authenticated" in headline.lower()
    assert "cannot supply an authentication" in guidance.lower()


def test_sso_alive_without_a_brokerage_session_names_the_user():
    """The state this account was actually in — SSO valid, brokerage session absent."""
    code, headline, guidance = verdict(
        GatewayState(reachable=True, user_id=99999999, connected=False)
    )
    assert code == EXIT_FREE
    assert "sso alive" in headline.lower()
    assert "99999999" in guidance


def test_an_unreachable_gateway_is_not_reported_as_a_free_session():
    """"The gateway did not answer" and "no session exists" are opposite claims."""
    code, headline, _ = verdict(GatewayState(reachable=False, detail="ConnectionError"))
    assert code == EXIT_UNREACHABLE
    assert "not answering" in headline.lower()


def test_read_state_never_raises_on_a_dead_gateway():
    """This runs when things are already broken, so it has to survive that."""
    with patch("claudia.gateway_preflight.requests.get", side_effect=OSError("refused")):
        state = read_state("https://localhost:5055/v1/api")
    assert state.reachable is False
    assert "refused" in state.detail


def test_read_state_types_the_live_response_shape():
    """The wire spelling `collission` is corrected on our side, not propagated."""
    with patch("claudia.gateway_preflight.requests.get",
               return_value=_tickle(authenticated=True, connected=True, collission=True)):
        state = read_state("https://localhost:5055/v1/api")
    assert state.authenticated and state.connected
    assert state.collision is True
    assert state.user_id == 99999999


def test_read_state_reads_both_endpoints_and_writes_to_neither():
    """Read-only is the whole contract: a check that can establish or destroy a session
    is not a check.

    Two GETs, deliberately — `/tickle` says whether *a* session is authenticated and
    `/sso/validate` says **whose** it is, and only the second explains a stuck login.
    Asserted over every call rather than the last one: with two GETs, checking
    `call_args` alone would silently stop covering `/tickle`.
    """
    with patch("claudia.gateway_preflight.requests.get", return_value=_tickle()) as get, \
         patch("claudia.gateway_preflight.requests.post") as post:
        read_state("https://localhost:5055/v1/api")
    urls = [c.args[0] for c in get.call_args_list]
    assert any(u.endswith("/tickle") for u in urls)
    assert any(u.endswith("/sso/validate") for u in urls)
    assert not post.called


# ── A borrowed SSO session ────────────────────────────────────────────────────


def _borrowed():
    """The state measured live on 2026-08-05: the phone's session, held by the gateway."""
    return GatewayState(
        reachable=True, authenticated=False, connected=False, competing=False,
        user_id=99999999, sso_valid=True, client_app="IBKRMOBILE_000.a-000",
        sso_user="ibkruser",
    )


def test_a_borrowed_session_names_the_app_holding_it():
    """The verdict must name the owning app — that is the whole diagnosis.

    Every weaker signal misled: userId was populated, ssoExpires was renewing, and
    competing was *false*, because the gateway never got far enough to register a claim.
    Only CLIENT_APP explains why the login is rejected however often it is retried.
    """
    code, headline, guidance = verdict(_borrowed())
    assert code == EXIT_BORROWED
    assert "IBKRMOBILE_000.a-000" in headline
    assert "log out" in guidance.lower()
    assert "swiping it away" in guidance.lower()  # closing != logging out


def test_a_borrowed_session_outranks_the_generic_not_authenticated_verdicts():
    """Ordering matters: 'free to log in' would send the user back into the retry loop."""
    code, _, _ = verdict(replace(_borrowed(), connected=True))
    assert code == EXIT_BORROWED


def test_the_startup_warning_fires_only_on_positive_proof(caplog):
    """Named owner + valid SSO + unauthenticated gateway. Anything less stays silent."""
    with patch("claudia.gateway_preflight.read_state", return_value=_borrowed()), \
         caplog.at_level(logging.ERROR, logger="claudia.gateway_preflight"):
        owner = warn_if_session_borrowed("https://x/v1/api")
    assert owner == "IBKRMOBILE_000.a-000"
    assert "LOG OUT" in caplog.text
    assert "ibkruser" in caplog.text


@pytest.mark.parametrize(
    ("label", "state"),
    [
        ("authenticated", GatewayState(reachable=True, authenticated=True, connected=True,
                                       sso_valid=True, client_app="IBKRMOBILE_000.a-000")),
        ("unreachable", GatewayState(reachable=False)),
        ("no sso", GatewayState(reachable=True, sso_valid=False, client_app="")),
        ("no owner named", GatewayState(reachable=True, sso_valid=True, client_app="")),
    ],
)
def test_the_startup_warning_stays_silent_without_proof(label, state, caplog):
    """A warning that fires when it need not is one that gets ignored when it must not."""
    with patch("claudia.gateway_preflight.read_state", return_value=state), \
         caplog.at_level(logging.ERROR, logger="claudia.gateway_preflight"):
        assert warn_if_session_borrowed("https://x/v1/api") is None
    assert caplog.text == ""


def test_release_reports_ibkrs_own_status_field_not_merely_a_200():
    """`{"status": false}` with HTTP 200 is a failed release — the slot is not free."""
    ok_resp, bad_resp = MagicMock(), MagicMock()
    ok_resp.status_code = bad_resp.status_code = 200
    ok_resp.json.return_value = {"status": True}
    bad_resp.json.return_value = {"status": False}

    with patch("claudia.gateway_preflight.requests.post", return_value=ok_resp):
        assert release_session("https://x/v1/api")[0] is True
    with patch("claudia.gateway_preflight.requests.post", return_value=bad_resp):
        assert release_session("https://x/v1/api")[0] is False


def test_release_is_never_reached_by_the_read_only_path():
    """`read_state` must not be able to release anything — its contract is read-only."""
    with patch("claudia.gateway_preflight.requests.get", return_value=_tickle()), \
         patch("claudia.gateway_preflight.requests.post") as post:
        read_state("https://localhost:5055/v1/api")
    assert not post.called


@pytest.mark.parametrize(
    "body", [{}, {"CLIENT_APP": None}, {"CLIENT_APP": 12345}, "a bare string", [1, 2, 3], None]
)
def test_a_malformed_sso_body_degrades_instead_of_raising(body):
    """`read_state` promises never to raise, and an odd shape is what a sick gateway emits.

    A bare string or list is valid JSON, so `resp.json()` can return one; calling `.get`
    on it raised AttributeError straight out of `read_state` until 2026-08-05. Degrading
    means the borrowed-session verdict simply cannot fire — never that it fires wrongly.
    """
    tick = MagicMock()
    tick.status_code = 200
    tick.json.return_value = {"iserver": {"authStatus": {}}}
    sso = MagicMock()
    sso.status_code = 200
    sso.json.return_value = body

    with patch("claudia.gateway_preflight.requests.get", side_effect=[tick, sso]):
        state = read_state("https://x/v1/api")

    assert state.reachable is True
    assert isinstance(state.client_app, str)
    assert verdict(state)[0] in {EXIT_READY, EXIT_UNREACHABLE, EXIT_FREE,
                                 EXIT_CONTESTED, EXIT_BORROWED}


def test_verdict_is_total_over_every_field_combination():
    """No state may fall through without a code, a headline and guidance.

    The verdict chain grew a branch at a time; an unreachable combination would surface as
    a blank message at exactly the moment someone needs to be told what to do.
    """
    import itertools

    for r, a, c, comp, coll, sso, app in itertools.product([True, False], repeat=7):
        state = GatewayState(reachable=r, authenticated=a, connected=c, competing=comp,
                             collision=coll, sso_valid=sso, client_app="X" if app else "")
        code, headline, guidance = verdict(state)
        assert code in {EXIT_READY, EXIT_UNREACHABLE, EXIT_FREE, EXIT_CONTESTED, EXIT_BORROWED}
        assert headline and guidance, f"empty verdict for {state}"


def test_a_401_means_alive_and_ready_not_down():
    """HTTP 401 is the gateway answering with "no session", not failing to answer.

    Measured 2026-08-05 straight after a container restart: both `/tickle` and
    `/sso/validate` returned 401 with empty bodies on a gateway that was running perfectly
    and waiting to be logged into. Folding that into `reachable=False` told the user
    "Gateway is NOT answering. Start it first" — wrong advice at the exact moment the
    session slot was finally clean.
    """
    resp = MagicMock()
    resp.status_code = 401
    with patch("claudia.gateway_preflight.requests.get", return_value=resp):
        state = read_state("https://localhost:5055/v1/api")

    assert state.reachable is True
    assert state.authenticated is False
    assert state.client_app == ""
    code, headline, _ = verdict(state)
    assert code == EXIT_FREE, "a clean gateway must read as free to log in"
    assert "not answering" not in headline.lower()


def test_a_genuine_transport_failure_is_still_down():
    """The 401 carve-out must not swallow a real outage."""
    with patch("claudia.gateway_preflight.requests.get", side_effect=OSError("refused")):
        assert verdict(read_state("https://x/v1/api"))[0] == EXIT_UNREACHABLE

    resp = MagicMock()
    resp.status_code = 502
    with patch("claudia.gateway_preflight.requests.get", return_value=resp):
        assert verdict(read_state("https://x/v1/api"))[0] == EXIT_UNREACHABLE
