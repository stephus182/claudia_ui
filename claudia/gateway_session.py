"""The authoritative state of the IBKR gateway session — one owner, one answer.

**Stages 1-3 of `docs/plans/2026-08-06-gateway-session-lifecycle-owner.md`.** The file
is layered, and the layering is deliberate:

* **Stage 1 (pure).** `SessionPhase`, `SessionState`, `classify`, `observe`, `declare`,
  `describe` and `GatewaySession`'s holder half — no I/O of any kind, so the rules can be
  exhaustively tested before anything depends on them.
* **Stage 2 (acts).** `SuspendLock`, `confirm_session`, `_establish`, `_recover` — the
  only code permitted to drive a login or mutate the container.
* **Stage 3 (polls).** `GatewaySession.start/stop`, `_poll_once` and
  `attempt_soft_recovery` — the background read every other component subscribes to, and
  the last session-affecting write, moved here out of `ConnectivityChecker`.

Everything from stage 2 down performs blocking I/O and must be called via
`asyncio.to_thread` from an event loop.

## Verified assumptions

Claims this module depends on that were **executed** rather than reasoned about, because
an unverified assumption is how the last round of defects arrived:

* Bash `echo` to a redirected (non-TTY) stdout is **unbuffered** — tested 2026-08-06 by
  writing a line, sleeping 3s and reading the file mid-run. That is what makes
  `scripts/ibkr-keepalive.sh`'s `SUSPENDED` line usable as live evidence instead of
  something that might surface minutes later.
* The launchd keepalive **does** have `$HOME`, so `${HOME}/.ibkr_core/session.suspend`
  resolves in that context — confirmed 2026-08-06 by the daemon logging
  `10:40:10 SUSPENDED` while a lock was held, then `10:41:05 OK` once released.

## The problem this exists to solve

Nine independent actors touch the gateway on their own timers, with no coordination:
two ticklers (one inside the container, one launchd), `ConnectivityChecker` at 60s,
`DashboardPoller` at 15s, `ExecutionListener`'s WebSocket, the agent's ~44 tools,
`gateway_preflight`, `GatewayManager`, and `scripts/gateway-reset.sh`. Between them they
issue session-affecting writes from three separate modules and mutate the container from
two.

That is why fixing one participant kept revealing the next gap: there was no place where
an invariant could be stated. Six such gaps were found in a single session on 2026-08-06,
each *after* the previous fix — including one where the pre-flight check ran after the
container it was inspecting had already been destroyed.

## The fact the whole design turns on

> "If the gateway has not received **any requests** for several minutes an open session
> will automatically timeout."
> — https://ibkrcampus.com/docs/web-api/v1/endpoints/session/ping-the-server.md

Renewal is not the ticklers' job. *Every* request renews the session — `DashboardPoller`
alone, at 15s, keeps one alive indefinitely without a single `/tickle`. So "run fewer
ticklers" was never the fix, and it is the direct explanation for why `POST /logout`
could not clear the borrowed session diagnosed on 2026-08-05: three ticklers, a 15-second
poller and a reconnecting WebSocket were all renewing the very session being cleared.

The consequence is `SUSPENDED`, and why it has to mean *everything*: a single actor left
running during recovery is enough to defeat it.

## The states

| Phase | Meaning | Who may talk to the gateway |
|---|---|---|
| `DOWN` | container not running, or the Java process is not answering | nobody |
| `FREE` | reachable, holds no session — the only state a login may start from | owner reads |
| `AUTHENTICATING` | login page open, user at 2FA | **owner reads only** |
| `LIVE` | authenticated, connected, **and confirmed against a data endpoint** | everyone |
| `DEGRADED` | authenticated but the data half is dark | owner reads |
| `BORROWED` | the gateway holds an SSO session issued to another IBKR app | owner reads |
| `CONTESTED` | another client is competing for the single brokerage session | owner reads |
| `RECOVERING` | a session is deliberately being cleared | **nobody, ticklers included** |

`DEGRADED` exists because the brokerage half and the data half are **documented as
independent** and have been **observed diverging on this account** — not because the
exact combination has been captured.

The observed divergence ran the other way: on 2026-08-04 `/portfolio/{id}/ledger` was
serving live figures while `/iserver/account/orders` returned HTTP 400
`{"error": "Bad Request: no bridge"}` (recorded in `dashboard_data.fetch_orders`).

⚠ **`hmds: {"error": "no bridge"}` in the `/tickle` body is NOT a market-data outage, and
must not be wired to this state.** Measured 2026-08-06 on a fully healthy session:
`authenticated: true`, `connected: true`, `hmds: {"error": "no bridge"}` — and at the same
moment `/iserver/marketdata/history` returned real AAPL bars and
`/iserver/marketdata/snapshot` returned live quotes (309.97 / 310.00). An earlier draft of
this docstring cited that field as evidence for `DEGRADED`; it is not evidence of anything
actionable, and gating on it would produce a blanked dashboard while data flowed normally.

(The same measurement settled a second thing worth knowing: the **first**
`/iserver/marketdata/snapshot` call after a subscription returns only `conid` with no
fields. Calls 2 and 3 carry the quote. An empty first snapshot is initialisation, not a
failure.)

What justifies the state is the documentation plus the direction of the observed split:
IBKR says *"Market Data and Trading is not possible if not authenticated"*
(https://ibkrcampus.com/docs/web-api/v1/endpoints/session/authentication-status.md) while
the portfolio endpoints declare a *different* prerequisite entirely (see below), so
"authenticated but the data half is dark" is reachable by construction. When it is
observed directly, record it here and delete this paragraph's hedge.

## Why `LIVE` requires a data call and not a flag

`authStatus.authenticated` describes the **brokerage** session. The portfolio endpoints
are a different subsystem with a different prerequisite — IBKR documents it as
*"/portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint"*
(https://ibkrcampus.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger.md) — and
the two have been observed disagreeing in both directions on this machine. So `LIVE` is
only reachable with positive confirmation from a real data call, which is also that
documented prerequisite: one call discharges both obligations.

`classify` therefore takes `data_ok` as a required argument. There is deliberately no
default: forgetting to confirm must be a `TypeError`, not a silent `LIVE`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import requests
import urllib3

from claudia.gateway_preflight import GatewayState, gateway_url, read_state

log = logging.getLogger(__name__)


class SessionPhase(StrEnum):
    """The eight states a gateway session can be in.

    A `StrEnum` so a phase renders readably in a log line and compares to a literal in a
    test without `.value` ceremony, matching how `ServiceStatus` is used in
    `claudia/status.py`.

    Six are *observed* — `classify` derives them from a reading. Two are *declared*:
    `AUTHENTICATING` and `RECOVERING` describe an operation the owner is performing and
    cannot be read off the wire, because the gateway looks identical during a login it is
    waiting on and a login nobody has started.
    """

    DOWN = "down"
    FREE = "free"
    AUTHENTICATING = "authenticating"
    LIVE = "live"
    DEGRADED = "degraded"
    BORROWED = "borrowed"
    CONTESTED = "contested"
    RECOVERING = "recovering"


# The phases in which *nothing* may touch the gateway — not the pollers, not the
# WebSocket, not the ticklers, not the agent's tools.
#
# Total, with no carve-outs, and that is the decision rather than an oversight (plan §8.1).
# Because any request renews the session, an exception for "just the agent" or "just the
# keepalive" reintroduces precisely the traffic that made `POST /logout` unable to clear a
# borrowed session on 2026-08-05. One rule with no exceptions is the mechanism.
_SUSPENDED_PHASES = frozenset({SessionPhase.AUTHENTICATING, SessionPhase.RECOVERING})

# The phases in which account data may be requested at all. Only one qualifies: `LIVE` is
# defined as "authenticated, connected and confirmed", which is exactly the condition
# under which a figure on screen can be vouched for.
_DATA_PHASES = frozenset({SessionPhase.LIVE})

POLL_INTERVAL = 60.0
"""Seconds between session reads.

Matches IBKR's *"It is expected to call this endpoint approximately every 60 seconds"*
(https://ibkrcampus.com/docs/web-api/v1/endpoints/session/ping-the-server.md). The read
is a `/tickle`, so polling and renewing are the same act — which is the whole reason the
suspend lock has to cover this loop too.

Declared **here**, above `GatewaySession`, rather than in the stage-3 section where it
used to live. Defined after the class, it could not be used as a default, so the real
interval was two hardcoded `60.0` literals and this constant — the one carrying the IBKR
citation — was read by nothing: editing it changed no behaviour. Found 2026-08-06 by
grepping for its own readers.
"""


@dataclass(frozen=True)
class SessionState:
    """One reading of the session, with the evidence it was derived from.

    `as_of` is **when the evidence was obtained**, never when the loop last woke up. A
    failed read must carry the previous `as_of` forward (see `stale_copy`) so age keeps
    growing instead of being masked by a fresh timestamp — the same rule
    `DashboardSnapshot` already enforces for account figures, and for the same reason: on
    a trading surface, stale data that looks current is the worst available failure.

    `gateway` is the raw `GatewayState` the phase was derived from, kept so a caller can
    explain a decision using the evidence it was actually made on. Re-reading to explain a
    decision would describe a different instant, which is how contradictory messages get
    written. It is None only for a declared phase, where there is no reading.
    """

    phase: SessionPhase
    as_of: datetime
    detail: str = ""
    gateway: GatewayState | None = None
    data_ok: bool | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since this state's evidence was obtained. Never negative."""
        return max(0.0, ((now or datetime.now(UTC)) - self.as_of).total_seconds())

    @property
    def is_live(self) -> bool:
        """Whether account data may be requested and trusted right now."""
        return self.phase in _DATA_PHASES

    @property
    def is_suspended(self) -> bool:
        """Whether **all** gateway traffic is currently forbidden.

        The predicate every actor checks before issuing any request, including the
        ticklers whose entire purpose is to issue requests.
        """
        return self.phase in _SUSPENDED_PHASES

    @property
    def may_call_ibkr(self) -> bool:
        """Whether an actor is permitted to make any gateway call at all.

        Distinct from `is_live`, and the distinction is load-bearing. `is_live` gates
        actors that need *data* — the dashboard poller, the execution listener. This gates
        actors that merely need *reachability*, including the agent's tools: a tool call
        against a `FREE` gateway simply 401s and the error is informative, whereas the
        same call during `AUTHENTICATING` is renewal traffic aimed at the session being
        established.
        """
        return not self.is_suspended

    def stale_copy(self, detail: str) -> SessionState:
        """Copy of this state with a new reason attached and **`as_of` unchanged**.

        What the owner publishes when a read fails: the phase and evidence are the last
        ones actually established, and the age keeps growing. Stamping the current time
        here would report a session frozen twenty minutes ago as one second old.

        There is deliberately **no `now` parameter**. Every other constructor here takes
        one for testability, and an ignored one on this method would be a trap: a caller
        passing `now=` would reasonably expect it to land in `as_of`, which is the single
        thing this method exists to leave alone.
        """
        return replace(self, detail=detail)


def classify(state: GatewayState, data_ok: bool) -> SessionPhase:
    """Map one gateway reading plus a data-endpoint result to an observed phase.

    Pure, total, and the single definition of what a reading *means*. The ordering below
    deliberately matches `gateway_preflight.verdict` step for step, so the two can never
    disagree about the same reading — a second, drifting definition of "the session is
    borrowed" sitting beside the authoritative one is the class of mistake this whole plan
    exists to remove.

    In particular `authenticated and connected` is tested **before** `competing`, exactly
    as `verdict` does: a session that works is working, whatever else is claiming it.

    Args:
        state: The reading, from `gateway_preflight.read_state`.
        data_ok: Whether a real data endpoint answered. **Required, with no default** —
            `LIVE` is unreachable without positive confirmation, and a defaulted argument
            would let a caller who forgot to confirm silently obtain it. Pass False
            whenever the check has not been made; the result is `DEGRADED`, which is
            honest, and which no consumer treats as usable.

    Returns:
        One of the six *observed* phases. `AUTHENTICATING` and `RECOVERING` are declared
        by the owner and are never returned here.
    """
    if not state.reachable:
        return SessionPhase.DOWN
    if state.authenticated and state.connected:
        return SessionPhase.LIVE if data_ok else SessionPhase.DEGRADED
    if state.competing or state.collision:
        return SessionPhase.CONTESTED
    if state.sso_valid and state.client_app and not state.authenticated:
        return SessionPhase.BORROWED
    return SessionPhase.FREE


# Human-readable reasons, one per observed phase. Held here rather than built at the call
# site so the wording cannot drift between the status dot, the chat and the CLI.
_PHASE_DETAIL = {
    SessionPhase.DOWN: "The gateway process is not answering.",
    SessionPhase.FREE: "No session — a login now should succeed.",
    SessionPhase.LIVE: "Authenticated and confirmed against a data endpoint.",
    SessionPhase.DEGRADED: "Authenticated, but the data half is not answering.",
    SessionPhase.BORROWED: "The gateway holds a session issued to another IBKR app.",
    SessionPhase.CONTESTED: "Another IBKR client is competing for the session.",
    SessionPhase.AUTHENTICATING: "A login is in progress — all gateway traffic suspended.",
    SessionPhase.RECOVERING: "Clearing the session — all gateway traffic suspended.",
}


def describe(phase: SessionPhase, state: GatewayState | None = None) -> str:
    """The one-line reason for a phase, naming the other app when one is known."""
    detail = _PHASE_DETAIL[phase]
    if phase is SessionPhase.BORROWED and state is not None and state.client_app:
        return f"The gateway holds a session issued to {state.client_app}."
    return detail


def observe(
    state: GatewayState, data_ok: bool, now: datetime | None = None
) -> SessionState:
    """Build a complete `SessionState` from a reading. The normal way to make one."""
    phase = classify(state, data_ok)
    return SessionState(
        phase=phase,
        as_of=now or datetime.now(UTC),
        detail=describe(phase, state),
        gateway=state,
        data_ok=data_ok,
    )


def declare(phase: SessionPhase, now: datetime | None = None) -> SessionState:
    """Build a state for a *declared* phase — one the owner is causing, not reading.

    Only `AUTHENTICATING` and `RECOVERING` may be declared. Every other phase is a claim
    about the gateway and must come from evidence, so declaring one would be a way to
    assert `LIVE` without having confirmed it — the exact hole `classify`'s mandatory
    `data_ok` closes.
    """
    if phase not in _SUSPENDED_PHASES:
        raise ValueError(
            f"{phase} is an observed phase and must come from `observe()`. Only "
            f"{sorted(p.value for p in _SUSPENDED_PHASES)} may be declared."
        )
    return SessionState(phase=phase, as_of=now or datetime.now(UTC), detail=describe(phase))


@dataclass
class GatewaySession:
    """Holds the current `SessionState` and notifies subscribers when the phase changes.

    Stage 1 is the holder alone: no polling, no writes, no I/O. `establish()` and
    `recover()` arrive in stage 2 and will be the **only** methods in the codebase that
    write to a session endpoint or mutate the container.

    Subscribers fire on **phase transitions only**, never on every republish. The same
    reasoning as `DashboardView._notify_staleness`: a notification repeated every poll
    trains the reader to dismiss it, which costs more than it buys. A republish that
    carries a new `detail` but the same phase is still the same situation.
    """

    _state: SessionState = field(
        default_factory=lambda: SessionState(
            phase=SessionPhase.DOWN,
            as_of=datetime.now(UTC),
            detail="The session has not been read yet.",
        )
    )
    _subscribers: list[Callable[[SessionState], None]] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _poll_url: str | None = None
    _interval: float = POLL_INTERVAL

    def state(self) -> SessionState:
        """The current state. Synchronous, no I/O — safe from a Panel callback.

        The state is a frozen dataclass replaced wholesale, so a reader can never observe
        a half-updated one: the attribute either still points at the old object or already
        points at the complete new one.
        """
        return self._state

    @property
    def phase(self) -> SessionPhase:
        """Shorthand for `state().phase`."""
        return self._state.phase

    def is_live(self) -> bool:
        """Whether account data may be requested and trusted. What pollers gate on."""
        return self._state.is_live

    def is_suspended(self) -> bool:
        """Whether all gateway traffic is forbidden. What *everything* gates on."""
        return self._state.is_suspended

    def may_call_ibkr(self) -> bool:
        """Whether any gateway call is permitted. What the agent's tools gate on."""
        return self._state.may_call_ibkr

    def publish(self, state: SessionState) -> bool:
        """Replace the state, notifying subscribers if the phase changed.

        Returns whether the phase changed, so a caller can log a transition without
        re-deriving it.

        A subscriber that raises is logged and skipped: one bad callback must not stop the
        others from being told the session went down, which is the moment they most need
        to know.
        """
        previous, self._state = self._state, state
        if previous.phase is state.phase:
            return False
        log.info(
            "Gateway session %s -> %s (%s)", previous.phase.value, state.phase.value,
            state.detail,
        )
        for callback in list(self._subscribers):
            try:
                callback(state)
            except Exception:
                log.exception("Gateway session subscriber failed; continuing")
        return True

    def read_now(self, url: str | None = None) -> SessionState:
        """Read the gateway, confirm the data half, publish and return the new state.

        Blocking HTTP — call via `asyncio.to_thread` from an event loop. Two GETs plus,
        when the brokerage session is up, the `/portfolio/accounts` confirmation that
        `LIVE` requires.

        A read that cannot reach the gateway publishes `DOWN` with a fresh `as_of`,
        because "unreachable" is itself an observation. A read that *succeeds* but finds
        the data half dark publishes `DEGRADED` — never `LIVE`.
        """
        api_url = url or gateway_url()
        reading = read_state(api_url)
        data_ok = False
        if reading.authenticated and reading.connected:
            data_ok, detail = confirm_session(api_url)
            state = observe(reading, data_ok)
            if not data_ok:
                state = replace(state, detail=f"{state.detail} ({detail})")
            self.publish(state)
            return self._state
        self.publish(observe(reading, data_ok))
        return self._state

    def establish(
        self, manager: GatewayManagerLike, **kwargs: object
    ) -> SessionState:
        """Bring the session to `LIVE`, or explain why it cannot get there.

        The **only** entry point in the codebase that opens a login page or starts a
        container. See `_establish` for the ordering contract and the evidence behind it.
        """
        return _establish(self, manager, **kwargs)  # type: ignore[arg-type]

    def recover(
        self, manager: GatewayManagerLike, **kwargs: object
    ) -> SessionState:
        """Clear an unusable session by recreating the container.

        The **only** entry point that mutates a container holding a session. Deliberately
        does not call `POST /logout` — see `_recover` for both reasons.
        """
        return _recover(self, manager, **kwargs)  # type: ignore[arg-type]

    # ── Background polling (stage 3) ────────────────────────────────────────

    def start(self, url: str | None = None, interval: float = POLL_INTERVAL) -> None:
        """Begin polling the session. Idempotent; restarts a finished task.

        Same lifecycle shape as `ConnectivityChecker` and `DashboardPoller`: one task for
        the whole process, blocking work on a worker thread, and a synchronous cached read
        for every consumer.
        """
        if self._task is None or self._task.done():
            self._poll_url = url
            self._interval = interval
            self._task = asyncio.create_task(self._poll_loop())
            log.info("GatewaySession polling started (interval=%.0fs)", interval)

    def stop(self) -> None:
        """Cancel the polling task. Safe when polling was never started."""
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("GatewaySession polling stopped")

    async def _poll_loop(self) -> None:
        """Poll immediately, then every `interval` seconds until cancelled.

        Every exception except `CancelledError` is logged and the loop continues: this
        loop is what every other component now reads its connectivity from, so one bad
        response must not leave the whole app believing the gateway is unreachable
        forever.
        """
        try:
            await self._poll_once()
        except Exception as exc:
            log.warning("GatewaySession initial poll error: %s", exc)
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("GatewaySession poll error: %s", exc)

    async def _poll_once(self) -> None:
        """One read, plus the one narrowly-scoped write the owner is allowed to make.

        **Skipped entirely while suspended.** A poll is a `/tickle`, and a `/tickle`
        renews — so polling through a login or a recovery would be the owner defeating its
        own suspension, which is the failure mode the whole plan exists to remove.

        The soft-recovery step fires only on IBKR's documented signature *and* only from a
        previously-`LIVE` session: `connected` with `authenticated` false, after the
        session was working. Never on a settling login (that starts from `DOWN`/`FREE`)
        and never on a hard disconnect (`connected` false), because `ssodh/init` raises the
        bridge and cannot supply an authentication that never happened.
        """
        if self._state.is_suspended:
            return
        was_live = self._state.phase is SessionPhase.LIVE
        state = await asyncio.to_thread(self.read_now, self._poll_url)
        if (
            was_live
            and state.phase is not SessionPhase.LIVE
            and state.gateway is not None
            and state.gateway.connected
            and not state.gateway.authenticated
            and await asyncio.to_thread(
                attempt_soft_recovery, self._poll_url or gateway_url()
            )
        ):
            log.info("GatewaySession: soft timeout recovered via ssodh/init")
            await asyncio.to_thread(self.read_now, self._poll_url)

    def subscribe(self, callback: Callable[[SessionState], None]) -> Callable[[], None]:
        """Register `callback` for phase transitions. Returns an unsubscribe callable.

        Unsubscribing is idempotent and safe from a destroyed Panel session — the same
        contract `ConnectivityChecker.subscribe` already uses, so per-session teardown
        works identically for both.
        """
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            """Remove `callback`. Safe to call more than once."""
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — the owner acts. Everything below performs I/O.
#
# These are the ONLY functions in the codebase permitted to mutate the gateway
# container or to drive a login. Stage 1 above stays pure and is what they are
# correct against.
# ══════════════════════════════════════════════════════════════════════════════


class GatewayManagerLike(Protocol):
    """The slice of `ibkr_core_mcp.gateway.GatewayManager` the owner uses.

    A Protocol rather than the concrete class so the orchestration is testable without
    Docker. That matters more than usual here: the defect that started this plan was an
    *ordering* bug that returned a correct-looking value, so the tests have to observe the
    call sequence, which means the manager has to be substitutable.
    """

    def ensure_docker_running(self, timeout: int = ...) -> None:
        """Start the Docker daemon if it is not already up."""
        ...

    def is_running(self) -> bool:
        """Whether the gateway container is currently running."""
        ...

    def start(self) -> None:
        """Create and start the container, **removing any existing one first**."""
        ...

    def restart(self) -> None:
        """Stop, remove and recreate the container — clears whatever session it held."""
        ...

    def wait_for_gateway(self, timeout: int = ..., poll_interval: int = ...) -> bool:
        """Block until the Java process answers HTTP. False on timeout."""
        ...

    def open_login_page(self) -> None:
        """Open the Client Portal login page in the system browser."""
        ...


DEFAULT_SUSPEND_LOCK = Path.home() / ".ibkr_core" / "session.suspend"
"""Where the cross-process suspend flag lives, beside `store.db` in the same directory."""


@dataclass
class SuspendLock:
    """A cross-process "do not touch the gateway" flag.

    Three renewers live in three different runtimes — a bash loop **inside the container**,
    a launchd bash loop **on the host**, and a Python task inside ClaudIA. No in-process
    mechanism can suspend the first two, so the flag has to be something all of them can
    read cheaply. A file is the only such thing.

    ## Why a PID is written into it

    A lock that outlives the process holding it is worse than no lock: it would silently
    stop every tickler forever, and the symptom — sessions quietly timing out — looks
    nothing like its cause. So the owning PID is written in, and `is_active` treats a lock
    whose PID is gone as absent. A crash therefore degrades to "no suspension", which is
    exactly today's behaviour and cannot be worse than it.

    ## Why it is not an OS file lock

    `flock` releases on process death, which sounds ideal, but bash readers cannot test it
    without holding it, and one of the two readers is a `curl` loop. A plain file plus a
    liveness check is readable by `sh` in one line, which is the actual requirement.
    """

    path: Path = DEFAULT_SUSPEND_LOCK
    _held: bool = False

    def acquire(self, reason: str) -> None:
        """Write the flag. Idempotent — re-acquiring only refreshes the reason."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"pid": os.getpid(), "reason": reason, "since": datetime.now(UTC).isoformat()}
            )
        )
        self._held = True

    def release(self) -> None:
        """Remove the flag. Safe when it was never acquired or already gone."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - unexpected filesystem state
            log.warning("Could not release the gateway suspend lock: %s", exc)
        self._held = False

    def __enter__(self) -> SuspendLock:
        """Support `with lock.for_reason(...)`-style use; acquire happens separately."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Always release, including when the body raised.

        The lock suppresses every keepalive in the system, so leaking one on an exception
        would leave the session slowly dying with nothing on screen to explain it.
        """
        self.release()

    @classmethod
    def is_active(cls, path: Path = DEFAULT_SUSPEND_LOCK) -> bool:
        """Whether a **live** process is currently suspending gateway traffic.

        A lock naming a dead PID is stale and reported as inactive, so a crash cannot
        wedge the system. An unreadable or malformed lock is also treated as inactive:
        failing open matches today's behaviour, whereas failing closed would silence every
        tickler because a file got truncated.
        """
        try:
            data = json.loads(path.read_text())
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        try:
            os.kill(pid, 0)  # signal 0 tests existence without touching the process
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # alive, owned by another user
        return True


def confirm_session(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Prove the session serves data, and satisfy the portfolio prerequisite in one call.

    `authStatus.authenticated` describes the brokerage session. The portfolio endpoints
    are a different subsystem with their own documented precondition:

        "/portfolio/accounts or /portfolio/subaccounts must be called prior to this
        endpoint."
        — https://ibkrcampus.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger.md

    So this one GET does double duty: it is the evidence that a login produced something
    usable rather than merely an authenticated flag, and it is that prerequisite call,
    made deliberately at login time instead of implicitly by whichever poll happened to
    run first.

    Returns `(ok, detail)` and never raises — a half-established session is an ordinary
    outcome here, and the caller has to report it rather than crash on it.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(f"{url}/portfolio/accounts", timeout=timeout, verify=False)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"/portfolio/accounts returned HTTP {resp.status_code}"
    try:
        body = resp.json()
    except Exception as exc:
        return False, f"/portfolio/accounts body was not JSON: {exc}"
    if not isinstance(body, list) or not body:
        return False, "/portfolio/accounts returned no accounts"
    ids = [str(a.get("accountId") or "") for a in body if isinstance(a, dict)]
    return True, f"account(s) {', '.join(i for i in ids if i) or 'unnamed'}"


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


# Guidance attached to a login that never completed. It leads with the other-app
# collision because that is both the likeliest cause and the one the pre-flight is
# structurally unable to see: measured 2026-08-06, a live IBKR Mobile login left
# /tickle and /sso/validate answering 401 and the check reading FREE.
_LOGIN_INCOMPLETE_GUIDANCE = (
    "No confirmed session. Only one brokerage session exists per username, and a session "
    "held by TWS, IBKR Mobile or a Client Portal tab is NOT visible from here until the "
    "gateway holds one — so if the login was rejected rather than abandoned, that is the "
    "first thing to check. Nothing was retried and no second login page was opened. "
    "`python -m claudia.gateway_launch --diagnose` prints the gateway's own log."
)


def _establish(
    session: GatewaySession,
    manager: GatewayManagerLike,
    *,
    url: str | None = None,
    emit: Callable[[str], None] | None = None,
    open_login: bool = True,
    allow_restart: bool = False,
    reach_timeout: int = 120,
    login_timeout: float = 300.0,
    poll_interval: float = 5.0,
    lock: SuspendLock | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SessionState:
    """Take the gateway from wherever it is to `LIVE`, or explain why it cannot get there.

    Implemented as a module function taking the session, rather than a method, purely so
    the whole sequence is visible in one place; `GatewaySession.establish` is the public
    entry point and simply calls this.

    ## The order is the contract

    1. Ensure Docker is running.
    2. Start the container **only if it is not already running**. A running container may
       hold a session and only a read is entitled to say whether it does — so it is never
       removed, restarted or otherwise touched before step 4. This is the exact defect the
       plan was written for: until 2026-08-06 the chat button called
       `GatewayManager.start()`, which removes any existing container, *before* the
       pre-flight that was supposed to inspect it.
    3. Wait for the Java process to answer.
    4. Read and classify. A stopped or absent container cannot hold a session, so steps 2
       and 3 cannot have destroyed evidence.
    5. Open the login page **only** from `FREE`, and only with the suspend lock held.

    ## Why the lock wraps the login and not just the recovery

    Any request renews the session, so every actor must be silent while one is being
    established — pollers, the WebSocket, both ticklers and the agent's tools. Measured in
    the gateway's own log during a real login attempt on 2026-08-06, with ClaudIA running:

        13:12:25  ws /v1/api/ws -> {"message":"waiting for session"}
        13:12:26  GET /v1/api/portfolio/{accountId}/ledger,401
        13:12:30  GET /v1/api/portfolio/{accountId}/positions/0,401

    Returns the final published `SessionState`. Never raises for an ordinary gateway
    problem: unreachable, contested, borrowed and "the user did not finish" are all
    normal answers that a caller mid-startup has to branch on rather than catch.
    """
    say = emit or (lambda line: log.info("%s", line))
    api_url = url or gateway_url()
    _sleep = sleep or time.sleep
    _now = monotonic or time.monotonic
    suspend = lock if lock is not None else SuspendLock()

    say("▶ Ensuring Docker is running…")
    manager.ensure_docker_running()

    if manager.is_running():
        say("▶ Gateway container already running — leaving it alone until the session is read.")
    else:
        say("▶ No gateway container running — starting one…")
        manager.start()

    say(f"▶ Waiting for the gateway to answer (up to {reach_timeout}s)…")
    if not manager.wait_for_gateway(timeout=reach_timeout):
        session.publish(
            SessionState(
                phase=SessionPhase.DOWN,
                as_of=datetime.now(UTC),
                detail=(
                    f"The gateway did not answer within {reach_timeout}s. The login page "
                    "will not load either — run `python -m claudia.gateway_launch "
                    "--diagnose` for the gateway's own log."
                ),
            )
        )
        return session.state()

    say("▶ Reading the session before touching anything…")
    state = session.read_now(api_url)
    say(f"   {state.phase.value}: {state.detail}")

    if state.phase is SessionPhase.LIVE:
        say("✔ Already authenticated — not opening the login page.")
        return state

    if state.phase is SessionPhase.CONTESTED:
        # Never auto-resolved. Evicting the other client starts the tug-of-war this whole
        # module exists to prevent, and closing that app is something the user can do
        # deliberately in a way this process cannot do for them.
        return state

    if state.phase is SessionPhase.BORROWED:
        if not allow_restart:
            return state
        state = _recover(
            session, manager, url=api_url, emit=say, reach_timeout=reach_timeout, lock=suspend
        )
        if state.phase is not SessionPhase.FREE:
            return state

    if state.phase is SessionPhase.DEGRADED:
        # Authenticated but the data half is dark. A login cannot fix that and would throw
        # away a working brokerage session to chase a market-data outage.
        say("✕ Authenticated, but the data half is not answering — a login would not help.")
        return state

    if state.phase is not SessionPhase.FREE or not open_login:
        return state

    # ── FREE: the one phase that earns a login page ──────────────────────────
    suspend.acquire("login in progress")
    try:
        session.publish(declare(SessionPhase.AUTHENTICATING))
        manager.open_login_page()
        say("▶ Login page opened. Complete it through to 'Client login succeeds'.")
        say(f"  Nothing else will touch the gateway for up to {int(login_timeout)}s.")

        deadline = _now() + login_timeout
        announced = False
        while _now() < deadline:
            reading = read_state(api_url)
            if reading.authenticated and reading.connected:
                ok, detail = confirm_session(api_url)
                if ok:
                    say(f"✔ Session established and serving data — {detail}")
                    session.publish(observe(reading, True))
                    return session.state()
                if not announced:
                    say(f"   authenticated, waiting for the data half — {detail}")
                    announced = True
            elif reading.sso_valid and reading.client_app and not reading.authenticated:
                say(f"✕ The gateway is now holding {reading.client_app}'s session.")
                session.publish(observe(reading, False))
                return session.state()
            _sleep(poll_interval)

        # Timed out. Republished as a fresh reading rather than left in AUTHENTICATING:
        # leaving the declared phase in place would suspend every tickler indefinitely.
        final = session.read_now(api_url)
        session.publish(final.stale_copy(_LOGIN_INCOMPLETE_GUIDANCE))
        return session.state()
    finally:
        suspend.release()


def _recover(
    session: GatewaySession,
    manager: GatewayManagerLike,
    *,
    url: str | None = None,
    emit: Callable[[str], None] | None = None,
    reach_timeout: int = 120,
    lock: SuspendLock | None = None,
) -> SessionState:
    """Clear a session the gateway cannot use, by recreating the container.

    ## Why this does not call `POST /logout`

    Two independent reasons, both evidence:

    1. **It does not work.** On 2026-08-05 `/logout` returned `{"status": true}` and the
       session came straight back with a full 10-minute window, because three separate
       ticklers renewed it every 60 seconds. Recreating the container did clear it.
    2. **Its scope is undocumented.** IBKR's page says only *"Logs the user out of the
       gateway session. Any further activity requires re-authentication."* — nothing about
       whether that cascades to TWS or IBKR Mobile
       (https://ibkrcampus.com/docs/web-api/v1/endpoints/session/logout-of-the-current-session.md).
       Since only one brokerage session exists per username, a cascade is plausible, and
       logging a user out of the phone they are managing a live position on is not a risk
       worth taking for a call that did not work anyway.

    `release_session` (above, moved into this module on 2026-08-06 so the owner holds
    every session write) still exists behind `gateway_preflight --release` for deliberate
    manual use. Nothing automatic calls it.

    The suspend lock is held across the whole operation so no tickler can re-establish
    what is being cleared, and released in `finally` — a leaked lock would silence every
    keepalive with nothing on screen to explain the session slowly dying.
    """
    say = emit or (lambda line: log.info("%s", line))
    api_url = url or gateway_url()
    suspend = lock if lock is not None else SuspendLock()

    suspend.acquire("recovering the session")
    try:
        session.publish(declare(SessionPhase.RECOVERING))
        say("▶ Recreating the gateway container to clear the session…")
        manager.restart()
        if not manager.wait_for_gateway(timeout=reach_timeout):
            session.publish(
                SessionState(
                    phase=SessionPhase.DOWN,
                    as_of=datetime.now(UTC),
                    detail="The gateway did not come back after the restart.",
                )
            )
            return session.state()
        state = session.read_now(api_url)
        say(f"   after restart: {state.phase.value}")
        return state
    finally:
        suspend.release()


_SESSION: GatewaySession | None = None


def get_session() -> GatewaySession:
    """The process-wide session owner.

    A single instance per process, for the same reason `ConnectivityChecker` and
    `DashboardPoller` are: two owners would be two authorities, which is the condition
    this module exists to remove. The chat button, the CLI and every subscriber must be
    talking about the same session or the suspend guarantee means nothing.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = GatewaySession()
    return _SESSION


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3 — the owner polls, and owns the last session-affecting write.
# ══════════════════════════════════════════════════════════════════════════════

def attempt_soft_recovery(url: str, timeout: float = 5.0) -> bool:
    """`POST /iserver/auth/ssodh/init` — raise the brokerage bridge after a soft timeout.

    Moved here from `ConnectivityChecker` on 2026-08-06. It is a session-affecting write,
    and the plan's first invariant is that the owner performs those; a monitor that can
    silently re-establish a session is a second authority over it.

    ## What it can and cannot do — measured, not assumed

    It raises the bridge and nothing more. Measured 2026-08-05 during the borrowed-session
    diagnosis, two inits moved `connected` False→True and left `authenticated` False both
    times: **it cannot supply an authentication that never happened**. So it is worth
    trying for IBKR's documented soft-timeout signature (previously working, now
    `connected` with `authenticated` false) and worthless for anything else — which is
    exactly how `GatewaySession._poll_once` gates it.

    `compete` is hardcoded False and must stay that way: it must never force-evict a
    concurrent IBKR Mobile or TWS session. IBKR signals a real competing session with
    HTTP 200 and `authenticated: false` in the body rather than an error status, so the
    body is checked, not merely the status code.

    Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/session/initialize-brokerage-session.md
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.post(
                f"{url}/iserver/auth/ssodh/init",
                json={"publish": True, "compete": False},
                timeout=timeout,
                verify=False,
            )
        if resp.status_code != 200:
            return False
        body = resp.json()
    except Exception as exc:
        log.debug("ssodh/init failed: %s", exc)
        return False
    auth = (body.get("iserver") or {}).get("authStatus") or body
    return bool(auth.get("authenticated"))
