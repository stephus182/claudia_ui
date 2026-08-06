"""Tests for `claudia.gateway_session` — stages 1 and 2 of the lifecycle-owner plan.

The invariants in §3 of `docs/plans/2026-08-06-gateway-session-lifecycle-owner.md` are
asserted here **over every phase**, not over a representative one. That choice is the
lesson from the defect that started the plan: the broken code returned a correct-looking
value for the case anyone would have tested, and was wrong for the two cases that
mattered. Parametrising over the whole enum is what makes a future ninth phase fail
loudly instead of silently inheriting a default.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from claudia.gateway_preflight import GatewayState
from claudia.gateway_session import (
    GatewaySession,
    SessionPhase,
    SessionState,
    SuspendLock,
    classify,
    declare,
    describe,
    observe,
)

_NOW = datetime(2026, 8, 6, 13, 0, tzinfo=UTC)

# Every phase, so the invariant tests below cannot quietly skip one.
ALL_PHASES = list(SessionPhase)
SUSPENDED = {SessionPhase.AUTHENTICATING, SessionPhase.RECOVERING}


# ── Readings, matching what this gateway actually returns ────────────────────


def _down() -> GatewayState:
    """The gateway process is not answering at all."""
    return GatewayState(reachable=False, detail="ConnectionError")


def _free() -> GatewayState:
    """Reachable, holding nothing — HTTP 401 on tickle and sso/validate."""
    return GatewayState(reachable=True, detail="HTTP 401 — no session yet")


def _authenticated() -> GatewayState:
    """Brokerage session up. Whether it is LIVE depends on the data check."""
    return GatewayState(reachable=True, authenticated=True, connected=True)


def _borrowed() -> GatewayState:
    """The 2026-08-05 diagnosis: SSO valid, owned by IBKR Mobile, unauthenticated here."""
    return GatewayState(
        reachable=True, authenticated=False, connected=False, sso_valid=True,
        client_app="IBKRMOBILE_000.a-000", sso_user="ibkruser", user_id=10541387,
    )


def _contested() -> GatewayState:
    """Another IBKR client is competing for the single brokerage session."""
    return GatewayState(reachable=True, competing=True)


# ── classify: the single definition of what a reading means ──────────────────


@pytest.mark.parametrize(
    "reading,data_ok,expected",
    [
        (_down(), False, SessionPhase.DOWN),
        (_down(), True, SessionPhase.DOWN),  # unreachable wins over any data claim
        (_free(), False, SessionPhase.FREE),
        (_authenticated(), True, SessionPhase.LIVE),
        (_authenticated(), False, SessionPhase.DEGRADED),
        (_borrowed(), False, SessionPhase.BORROWED),
        (_contested(), False, SessionPhase.CONTESTED),
    ],
)
def test_classify_maps_each_reading_to_its_phase(reading, data_ok, expected):
    """Every reading this gateway produces has exactly one phase."""
    assert classify(reading, data_ok) is expected


def test_live_is_unreachable_without_a_data_confirmation():
    """The invariant: an authenticated flag alone can never produce LIVE.

    The brokerage half and the data half are documented as independent subsystems with
    different prerequisites, and were observed diverging on this account on 2026-08-04
    (ledger live while `/iserver/account/orders` returned `no bridge`). Reporting an
    unconfirmed session as live publishes figures nobody can vouch for.
    """
    assert classify(_authenticated(), data_ok=False) is SessionPhase.DEGRADED
    assert classify(_authenticated(), data_ok=True) is SessionPhase.LIVE


def test_classify_requires_data_ok_explicitly():
    """No default: forgetting to confirm must be a TypeError, never a silent LIVE."""
    with pytest.raises(TypeError):
        classify(_authenticated())  # type: ignore[call-arg]


def test_a_working_session_outranks_a_competing_claim():
    """Matches `gateway_preflight.verdict` step for step — one definition, not two.

    `verdict` checks `authenticated and connected` before `competing`, so this must too;
    a second, drifting definition of the same reading is the class of mistake the whole
    plan exists to remove.
    """
    both = GatewayState(reachable=True, authenticated=True, connected=True, competing=True)
    assert classify(both, data_ok=True) is SessionPhase.LIVE


def test_classify_never_returns_a_declared_phase():
    """AUTHENTICATING and RECOVERING describe an operation, not a reading."""
    readings = [_down(), _free(), _authenticated(), _borrowed(), _contested()]
    produced = {classify(r, ok) for r in readings for ok in (True, False)}
    assert produced.isdisjoint(SUSPENDED)


# ── The suspension invariant, over every phase ───────────────────────────────


@pytest.mark.parametrize("phase", ALL_PHASES)
def test_suspension_is_exactly_the_two_declared_phases(phase):
    """Total suspension, with no carve-outs (plan §8.1).

    Any request renews the session, so an exception for one actor reintroduces the
    traffic that made `POST /logout` unable to clear a borrowed session on 2026-08-05.
    """
    state = SessionState(phase=phase, as_of=_NOW)
    assert state.is_suspended is (phase in SUSPENDED)
    assert state.may_call_ibkr is (phase not in SUSPENDED)


@pytest.mark.parametrize("phase", ALL_PHASES)
def test_only_live_permits_account_data(phase):
    """DEGRADED must not read as usable — it is the state where figures look fine."""
    assert SessionState(phase=phase, as_of=_NOW).is_live is (phase is SessionPhase.LIVE)


@pytest.mark.parametrize("phase", ALL_PHASES)
def test_every_phase_has_a_reason(phase):
    """A phase with no wording would reach the UI as a blank explanation."""
    assert describe(phase).strip()


def test_borrowed_names_the_app_holding_the_session():
    """The field that actually explains a stuck login is the one a user needs to see."""
    assert "IBKRMOBILE_000.a-000" in describe(SessionPhase.BORROWED, _borrowed())


# ── declare: the hole that must stay closed ──────────────────────────────────


@pytest.mark.parametrize("phase", sorted(SUSPENDED, key=lambda p: p.value))
def test_the_two_operational_phases_may_be_declared(phase):
    """These describe something the owner is doing and cannot be read off the wire."""
    assert declare(phase, now=_NOW).phase is phase


@pytest.mark.parametrize("phase", [p for p in ALL_PHASES if p not in SUSPENDED])
def test_an_observed_phase_can_never_be_declared(phase):
    """Otherwise `declare(LIVE)` would assert a confirmed session without confirming."""
    with pytest.raises(ValueError, match="observed phase"):
        declare(phase)


# ── Staleness: same rule the dashboard already enforces ──────────────────────


def test_a_failed_read_does_not_refresh_as_of():
    """Age must keep growing, or a session frozen 20 minutes ago reads as one second old."""
    established = observe(_authenticated(), data_ok=True, now=_NOW)
    later = established.stale_copy("gateway stopped answering")

    assert later.as_of == _NOW
    assert later.phase is SessionPhase.LIVE
    assert later.detail == "gateway stopped answering"
    assert later.age_seconds(now=_NOW + timedelta(minutes=20)) == pytest.approx(1200)


def test_age_is_never_negative():
    """A clock that steps backwards must not produce a negative age."""
    state = observe(_free(), data_ok=False, now=_NOW)
    assert state.age_seconds(now=_NOW - timedelta(seconds=30)) == 0.0


def test_observe_keeps_the_evidence_the_phase_came_from():
    """A decision must be explainable from the reading it was made on, not a later one."""
    state = observe(_borrowed(), data_ok=False, now=_NOW)
    assert state.gateway is not None
    assert state.gateway.client_app == "IBKRMOBILE_000.a-000"
    assert state.data_ok is False


# ── The holder ───────────────────────────────────────────────────────────────


def test_starts_down_and_says_it_has_not_read_yet():
    """"Not read yet" and "the gateway is down" must not look identical to a consumer."""
    session = GatewaySession()
    assert session.phase is SessionPhase.DOWN
    assert "not been read" in session.state().detail


def test_subscribers_fire_only_on_a_phase_change():
    """A notification repeated every poll trains the reader to dismiss it."""
    session = GatewaySession()
    seen: list[SessionPhase] = []
    session.subscribe(lambda s: seen.append(s.phase))

    assert session.publish(observe(_free(), False, now=_NOW)) is True
    assert session.publish(observe(_free(), False, now=_NOW)) is False  # same phase
    assert session.publish(observe(_authenticated(), True, now=_NOW)) is True

    assert seen == [SessionPhase.FREE, SessionPhase.LIVE]


def test_a_republish_with_a_new_reason_is_not_a_transition():
    """Same situation, new wording — the staleness path must not toast on every poll."""
    session = GatewaySession()
    session.publish(observe(_authenticated(), True, now=_NOW))
    seen: list[SessionPhase] = []
    session.subscribe(lambda s: seen.append(s.phase))

    assert session.publish(session.state().stale_copy("gateway timed out")) is False
    assert seen == []


def test_one_failing_subscriber_does_not_starve_the_others():
    """The moment a subscriber raises is the moment the rest most need telling."""
    session = GatewaySession()
    reached: list[str] = []
    session.subscribe(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    session.subscribe(lambda s: reached.append("second"))

    session.publish(observe(_free(), False, now=_NOW))

    assert reached == ["second"]


def test_unsubscribe_stops_delivery_and_is_idempotent():
    """Per-session teardown must be safe to run twice — Panel destroys sessions abruptly."""
    session = GatewaySession()
    seen: list[SessionPhase] = []
    unsubscribe = session.subscribe(lambda s: seen.append(s.phase))

    unsubscribe()
    unsubscribe()  # must not raise
    session.publish(observe(_free(), False, now=_NOW))

    assert seen == []


@pytest.mark.parametrize("phase", ALL_PHASES)
def test_the_holder_predicates_agree_with_the_state(phase):
    """The holder must never form a second opinion about its own state."""
    session = GatewaySession()
    session.publish(SessionState(phase=phase, as_of=_NOW))

    assert session.is_live() is session.state().is_live
    assert session.is_suspended() is session.state().is_suspended
    assert session.may_call_ibkr() is session.state().may_call_ibkr


# ═══════════════════════════════════════════════════════════════════════════
# Stage 2 — the owner acts.
#
# The defect that started the plan was an ORDERING bug that returned a
# correct-looking value, so these assert call sequences and forbidden actions,
# not just outcomes.
# ═══════════════════════════════════════════════════════════════════════════

_MUTATORS = {"start", "restart"}


class FakeManager:
    """Records every call it receives, in order, so sequence can be asserted."""

    def __init__(self, running: bool = False, reachable: bool = True) -> None:
        """Configure whether the container is running and whether it answers."""
        self.calls: list[str] = []
        self._running = running
        self._reachable = reachable

    def ensure_docker_running(self, timeout: int = 60) -> None:
        """Record the call."""
        self.calls.append("ensure_docker_running")

    def is_running(self) -> bool:
        """Record the call and report the configured state."""
        self.calls.append("is_running")
        return self._running

    def start(self) -> None:
        """Record the call and mark the container running."""
        self.calls.append("start")
        self._running = True

    def restart(self) -> None:
        """Record the call and mark the container running."""
        self.calls.append("restart")
        self._running = True

    def wait_for_gateway(self, timeout: int = 120, poll_interval: int = 3) -> bool:
        """Record the call and report the configured reachability."""
        self.calls.append("wait_for_gateway")
        return self._reachable

    def open_login_page(self) -> None:
        """Record the call."""
        self.calls.append("open_login_page")


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Patch read_state/confirm_session, recording reads in the manager's call log."""

    def _install(manager, readings, confirmations=(True,)):
        """Queue readings and data-confirmations; the last of each repeats."""
        reads = list(readings)
        confs = list(confirmations)

        def fake_read(url, timeout=5.0):
            """Stand-in for read_state that appends to the manager's call log."""
            manager.calls.append("read_state")
            return reads.pop(0) if len(reads) > 1 else reads[0]

        def fake_confirm(url, timeout=10.0):
            """Stand-in for confirm_session."""
            ok = confs.pop(0) if len(confs) > 1 else confs[0]
            return ok, "account(s) <redacted>" if ok else "no bridge"

        monkeypatch.setattr("claudia.gateway_session.read_state", fake_read)
        monkeypatch.setattr("claudia.gateway_session.confirm_session", fake_confirm)
        return SuspendLock(path=tmp_path / "session.suspend")

    return _install


def _clock():
    """A monotonic clock advancing 5s per call, so timeouts expire without sleeping."""
    state = {"t": 0.0}

    def tick() -> float:
        """Advance the clock 5s and return it."""
        state["t"] += 5.0
        return state["t"]

    return tick


def _establish(session, gm, lock, **kw):
    """Run establish with sleeping and clocks stubbed out."""
    return session.establish(
        gm, url="https://x/v1/api", emit=lambda _: None, lock=lock,
        sleep=lambda _: None, monotonic=_clock(), **kw,
    )


# ── The ordering guarantee ───────────────────────────────────────────────────


def test_a_running_container_is_never_mutated_before_the_session_is_read(wire):
    """The defect this plan replaced, pinned: no start/restart before `read_state`.

    Until 2026-08-06 `panel_app._on_start_gateway` called `GatewayManager.start()` —
    which removes any existing container — and only then read the session. Asserted on
    the sequence, because the old code's return value looked correct.
    """
    gm = FakeManager(running=True)
    lock = wire(gm, [_authenticated()])
    session = GatewaySession()

    _establish(session, gm, lock)

    read_at = gm.calls.index("read_state")
    assert [c for c in gm.calls[:read_at] if c in _MUTATORS] == [], gm.calls


def test_a_stopped_container_is_started_before_the_read(wire):
    """A stopped container cannot hold a session, so starting it destroys no evidence."""
    gm = FakeManager(running=False)
    lock = wire(gm, [_free()])
    session = GatewaySession()

    _establish(session, gm, lock)

    assert gm.calls.index("start") < gm.calls.index("read_state")


# ── Which phases earn a login page ───────────────────────────────────────────


def test_a_live_session_is_left_alone(wire):
    """Re-authenticating a working session escalates into the IB Key challenge."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_authenticated()], confirmations=[True])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.LIVE
    assert "open_login_page" not in gm.calls


def test_degraded_does_not_trigger_a_login(wire):
    """A login cannot fix a market-data outage and would discard a working session."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_authenticated()], confirmations=[False])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.DEGRADED
    assert "open_login_page" not in gm.calls


def test_contested_is_never_auto_resolved(wire):
    """Evicting the other client starts the tug-of-war this module exists to prevent."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_contested()])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.CONTESTED
    assert "open_login_page" not in gm.calls
    assert "restart" not in gm.calls


def test_borrowed_does_not_restart_unless_allowed(wire):
    """A restart destroys a live session, so it stays opt-in per call."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_borrowed()])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.BORROWED
    assert "restart" not in gm.calls
    assert "open_login_page" not in gm.calls


def test_free_opens_the_login_page_and_confirms_before_declaring_success(wire):
    """LIVE requires the data call, not just an authenticated flag."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free(), _authenticated()], confirmations=[True])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.LIVE
    assert gm.calls.index("read_state") < gm.calls.index("open_login_page")


def test_an_authenticated_flag_alone_never_ends_the_wait(wire):
    """Authenticated with the data half dark must time out, never report success."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free(), _authenticated()], confirmations=[False])
    session = GatewaySession()

    result = _establish(session, gm, lock, login_timeout=20)

    assert result.phase is not SessionPhase.LIVE


# ── The suspend lock ─────────────────────────────────────────────────────────


def test_the_lock_is_held_during_the_login_and_released_after(wire, tmp_path):
    """Any request renews the session, so everything must be silent during a login."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free(), _authenticated()], confirmations=[True])
    session = GatewaySession()
    seen: list[bool] = []

    original = gm.open_login_page

    def spy() -> None:
        """Record whether the lock was active at the moment the page opened."""
        seen.append(SuspendLock.is_active(lock.path))
        original()

    gm.open_login_page = spy  # type: ignore[method-assign]
    _establish(session, gm, lock)

    assert seen == [True], "the suspend lock was not held while the login page was open"
    assert not lock.path.exists(), "the lock leaked after the login completed"


def test_the_lock_is_released_even_when_the_login_raises(wire, tmp_path):
    """A leaked lock silences every keepalive with nothing on screen to explain it."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free()])
    session = GatewaySession()

    def boom() -> None:
        """Fail the way a browser launch can."""
        raise RuntimeError("no browser")

    gm.open_login_page = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        _establish(session, gm, lock)

    assert not lock.path.exists()


def test_a_lock_naming_a_dead_process_is_ignored(tmp_path):
    """A crash must degrade to 'no suspension', never wedge every tickler forever."""
    path = tmp_path / "session.suspend"
    path.write_text(json.dumps({"pid": 999999, "reason": "crashed", "since": "x"}))
    assert SuspendLock.is_active(path) is False


def test_a_lock_naming_a_live_process_suspends(tmp_path):
    """The live case, so the dead-PID test above cannot pass for the wrong reason."""
    path = tmp_path / "session.suspend"
    path.write_text(json.dumps({"pid": os.getpid(), "reason": "login", "since": "x"}))
    assert SuspendLock.is_active(path) is True


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"pid": "abc"}'])
def test_a_malformed_lock_fails_open(tmp_path, body):
    """Failing closed would silence every tickler because a file got truncated."""
    path = tmp_path / "session.suspend"
    path.write_text(body)
    assert SuspendLock.is_active(path) is False


def test_an_absent_lock_is_not_active(tmp_path):
    """The ordinary case: no file, no suspension."""
    assert SuspendLock.is_active(tmp_path / "nope") is False


# ── Recovery ─────────────────────────────────────────────────────────────────


def test_recovery_restarts_the_container_and_never_calls_logout(wire, monkeypatch):
    """`POST /logout` did not work (three ticklers renewed through it) and its scope
    relative to IBKR Mobile is undocumented. Recovery must not depend on it."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free()])
    session = GatewaySession()
    posts: list[str] = []
    monkeypatch.setattr(
        "claudia.gateway_session.requests.post",
        lambda *a, **k: posts.append(str(a)),
        raising=False,
    )

    session.recover(gm, url="https://x/v1/api", emit=lambda _: None, lock=lock)

    assert "restart" in gm.calls
    assert posts == [], "recovery must not POST to the gateway"


def test_recovery_holds_the_lock_across_the_restart(wire):
    """No tickler may re-establish the session being cleared."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free()])
    session = GatewaySession()
    seen: list[bool] = []

    original = gm.restart

    def spy() -> None:
        """Record whether the lock was active at the moment of the restart."""
        seen.append(SuspendLock.is_active(lock.path))
        original()

    gm.restart = spy  # type: ignore[method-assign]
    session.recover(gm, url="https://x/v1/api", emit=lambda _: None, lock=lock)

    assert seen == [True]
    assert not lock.path.exists()


# ── Unreachable ──────────────────────────────────────────────────────────────


def test_an_unreachable_gateway_is_never_sent_a_login_page(wire):
    """Nothing is read from, and no page opened for, a gateway that does not answer."""
    gm = FakeManager(running=True, reachable=False)
    lock = wire(gm, [_free()])
    session = GatewaySession()

    result = _establish(session, gm, lock)

    assert result.phase is SessionPhase.DOWN
    assert "open_login_page" not in gm.calls
    assert "read_state" not in gm.calls


# ── The invariant, over every starting phase ─────────────────────────────────


@pytest.mark.parametrize(
    "reading,confirm",
    [(_authenticated(), True), (_authenticated(), False), (_free(), True),
     (_contested(), False), (_borrowed(), False)],
)
def test_the_login_page_is_never_opened_without_a_read_first(wire, reading, confirm):
    """The CLAUDE.md rule, asserted over every branch rather than one of them."""
    gm = FakeManager(running=True)
    lock = wire(gm, [reading], confirmations=[confirm])
    session = GatewaySession()

    _establish(session, gm, lock, login_timeout=20)

    if "open_login_page" in gm.calls:
        assert gm.calls.index("read_state") < gm.calls.index("open_login_page")


def test_a_timed_out_login_does_not_leave_the_session_suspended(wire):
    """AUTHENTICATING left in place would suspend every tickler indefinitely."""
    gm = FakeManager(running=True)
    lock = wire(gm, [_free()])
    session = GatewaySession()

    result = _establish(session, gm, lock, login_timeout=20)

    assert result.phase is not SessionPhase.AUTHENTICATING
    assert result.is_suspended is False
    assert "IBKR Mobile" in result.detail or "Mobile" in result.detail


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 — polling, and the last session-affecting write.
#
# These migrated from tests/test_status.py on 2026-08-06 with the code they
# describe: `ssodh/init` was ConnectivityChecker's, and a monitor able to
# re-establish a session is a second authority over it (plan invariant §3.1).
# ═══════════════════════════════════════════════════════════════════════════

from claudia.gateway_session import attempt_soft_recovery  # noqa: E402


def _resp(status: int, body: object) -> MagicMock:
    """A stand-in requests.Response with the given status and JSON body."""
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_soft_recovery_reports_success_only_when_the_body_says_authenticated(monkeypatch):
    """HTTP 200 is not the answer — the body is.

    IBKR signals a real competing session with HTTP 200 and `authenticated: false` rather
    than an error status, so a status-code-only check would report a recovery that did
    not happen.
    """
    body = {"iserver": {"authStatus": {"authenticated": True, "connected": True}}}
    monkeypatch.setattr("claudia.gateway_session.requests.post", lambda *a, **k: _resp(200, body))
    assert attempt_soft_recovery("https://x/v1/api") is True


def test_soft_recovery_200_but_not_authenticated_is_a_failure(monkeypatch):
    """The competing-session shape: 200, with the body saying it did not work."""
    body = {"iserver": {"authStatus": {"authenticated": False, "connected": True}}}
    monkeypatch.setattr("claudia.gateway_session.requests.post", lambda *a, **k: _resp(200, body))
    assert attempt_soft_recovery("https://x/v1/api") is False


def test_soft_recovery_non_200_is_a_failure(monkeypatch):
    """Anything but 200 means the bridge was not raised."""
    monkeypatch.setattr("claudia.gateway_session.requests.post", lambda *a, **k: _resp(500, {}))
    assert attempt_soft_recovery("https://x/v1/api") is False


def test_soft_recovery_never_raises(monkeypatch):
    """It runs when things are already broken, so it must survive that."""
    def boom(*a, **k):
        """Fail the way an unreachable gateway does."""
        raise OSError("refused")

    monkeypatch.setattr("claudia.gateway_session.requests.post", boom)
    assert attempt_soft_recovery("https://x/v1/api") is False


def test_soft_recovery_never_sets_compete_true(monkeypatch):
    """`compete: true` would force-evict a live IBKR Mobile or TWS session.

    The single brokerage session per username means eviction is a real consequence, not a
    theoretical one — this must never be flipped to win a race.
    """
    captured: dict = {}

    def capture(*args, **kwargs):
        """Record the JSON body the call would send."""
        captured.update(kwargs.get("json") or {})
        return _resp(200, {"iserver": {"authStatus": {"authenticated": True}}})

    monkeypatch.setattr("claudia.gateway_session.requests.post", capture)
    attempt_soft_recovery("https://x/v1/api")
    assert captured.get("compete") is False


@pytest.mark.asyncio
async def test_polling_is_skipped_entirely_while_suspended():
    """A poll is a `/tickle`, and a `/tickle` renews.

    Polling through a login or a recovery would be the owner defeating its own
    suspension — the precise failure the plan exists to remove.
    """
    session = GatewaySession()
    session.publish(declare(SessionPhase.AUTHENTICATING))
    with patch.object(session, "read_now") as read:
        await session._poll_once()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_soft_recovery_fires_only_from_a_previously_live_session(monkeypatch):
    """IBKR's documented soft-timeout signature, and only after the session worked.

    `ssodh/init` raises the bridge; it cannot supply an authentication that never
    happened (measured 2026-08-05 — two inits moved `connected` False→True and left
    `authenticated` False both times). Firing it on a settling login would be a write
    against a session nobody has established yet.
    """
    soft = GatewayState(reachable=True, authenticated=False, connected=True)
    calls: list[str] = []
    monkeypatch.setattr("claudia.gateway_session.attempt_soft_recovery",
                        lambda *a, **k: calls.append("tried") or False)

    # From FREE: no recovery — nothing was ever established.
    session = GatewaySession()
    session.publish(observe(_free(), False))
    with patch.object(session, "read_now", return_value=observe(soft, False)):
        await session._poll_once()
    assert calls == []

    # From LIVE: the documented soft timeout, so it is attempted.
    session.publish(observe(_authenticated(), True))
    with patch.object(session, "read_now", return_value=observe(soft, False)):
        await session._poll_once()
    assert calls == ["tried"]


@pytest.mark.asyncio
async def test_no_soft_recovery_on_a_hard_disconnect(monkeypatch):
    """`connected` false is not a soft timeout; init cannot help and must not be sent."""
    calls: list[str] = []
    monkeypatch.setattr("claudia.gateway_session.attempt_soft_recovery",
                        lambda *a, **k: calls.append("tried") or False)
    session = GatewaySession()
    session.publish(observe(_authenticated(), True))

    with patch.object(session, "read_now", return_value=observe(_down(), False)):
        await session._poll_once()

    assert calls == []
