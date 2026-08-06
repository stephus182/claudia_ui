"""Connectivity monitor for ClaudIA.

Polls IBKR gateway, GDrive token file, and TradingView sidecar every 60s.
Caches status in memory (instant reads for the panel_app status dots).
Notifies registered subscribers on state transitions; alert delivery (e.g. Panel
chat messages) is wired externally via subscribe().
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from claudia.tradingview import _TV_DEBUG_PORT

if TYPE_CHECKING:
    from claudia.gateway_session import GatewaySession
    from claudia.gdrive_sync import GDriveSync
    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds — matches IBKR /tickle keepalive requirement


class ServiceStatus(StrEnum):
    """Connectivity state of one monitored service.

    Three states, and the distinction matters for alerting:

    - `UNKNOWN`: **not configured** — e.g. no Drive credentials on disk. Renders as a grey
      dot and is deliberately *not* an error; a UNKNOWN→OK transition at startup raises no
      alert.
    - `OK`: reachable and, for IBKR, authenticated.
    - `ERROR`: configured but unreachable. This is the only state that alerts.
    """

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


_DISCONNECT_MESSAGES = {
    "ibkr":   "⚠️ **IBKR Gateway disconnected** — check the Client Portal and log in.",
    "gdrive": "⚠️ **Google Drive disconnected** — check credentials or network.",
    "tv":     "⚠️ **TradingView sidecar stopped** — TradingView tools unavailable.",
}
_RECONNECT_MESSAGES = {
    "ibkr":   "✅ **IBKR Gateway reconnected.**",
    "gdrive": "✅ **Google Drive reconnected.**",
    "tv":     "✅ **TradingView reconnected.**",
}


class ConnectivityChecker:
    """Background poller that monitors IBKR gateway, GDrive, and TradingView every 60s.

    Notifies registered subscribers on state transitions (UNKNOWN/OK → ERROR, ERROR → OK)
    — see subscribe().
    The cached status dict is read synchronously via get_status() by panel_app's status dots.
    """

    def __init__(
        self,
        gateway_url: str,
        gdrive_token_file: Path,
        tv_bridge: TradingViewBridge | None = None,
        gdrive_sync: GDriveSync | None = None,
        session: GatewaySession | None = None,
    ) -> None:
        """Initialise the checker. Call start() to begin polling.

        gdrive_sync is optional — if None, check_gdrive() falls back to a token-file
        existence check (no live API round-trip).

        `session` is the `GatewaySession` that owns IBKR connectivity, defaulting to the
        process-wide one. This checker no longer polls IBKR itself — see `check_ibkr`.
        """
        self._gateway_url = gateway_url.rstrip("/")
        self._gdrive_token_file = Path(gdrive_token_file)
        self._tv_bridge = tv_bridge
        self._gdrive_sync = gdrive_sync
        # The session owner. Defaulted rather than required so existing call sites and
        # tests keep working; `panel_app` passes the process-wide one explicitly.
        if session is None:
            from claudia.gateway_session import get_session

            session = get_session()
        self._session = session
        self._status: dict[str, ServiceStatus] = {
            "ibkr":   ServiceStatus.UNKNOWN,
            "gdrive": ServiceStatus.UNKNOWN,
            "tv":     ServiceStatus.UNKNOWN,
        }
        self._last_ibkr_auth_status: dict = {}
        self._task: asyncio.Task | None = None
        self._subscribers: list[Callable[[str], Awaitable[None]]] = []

    def get_status(self) -> dict[str, ServiceStatus]:
        """Return a shallow copy of the current status dict (thread-safe for callers)."""
        return dict(self._status)

    # ── Individual checks (synchronous, cheap) ──────────────────────────────

    def check_ibkr(self) -> bool:
        """Whether IBKR is usable, according to the session owner.

        **This performs no HTTP of its own since 2026-08-06.** It used to `GET /tickle` on
        its own 60-second timer and, on a soft timeout, `POST /iserver/auth/ssodh/init`.
        Both moved to `claudia.gateway_session`, for two distinct reasons:

        1. *One owner writes* (plan invariant §3.1). A monitor that can silently
           re-establish a session is a second authority over that session's lifecycle,
           and competing authorities are what made every earlier fix uncover another gap.
        2. **The dot and the dashboard were reading different subsystems.** This checked
           `iserver.authStatus`; the KPI tiles read `/portfolio/*`, which IBKR documents
           with no brokerage-session prerequisite at all. So the dot could go red while
           the tiles kept printing figures from a subsystem that was still answering —
           plan F6/F8, and the defect that started this whole track. Both now read one
           `SessionState` and cannot disagree.

        `is_live()` rather than "reachable": it means authenticated, connected **and**
        confirmed against a real data endpoint, which is the only condition under which a
        green dot is honest.
        """
        return bool(self._session.is_live())

    def check_gdrive(self) -> bool:
        """Return True if GDrive is reachable.

        When _gdrive_sync is wired, calls GDriveSync.ping() which does a live
        files().list round-trip — reflects real API reachability, not just token presence.
        When _gdrive_sync is None (GOOGLE_DRIVE_FOLDER_ID unset), falls back to token-file
        existence; the green light then means "credentials present" not "API reachable".
        """
        if self._gdrive_sync is not None:
            return self._gdrive_sync.ping()
        return self._gdrive_token_file.exists()

    def check_tradingview(self) -> bool:
        """TCP connect to TradingView Desktop's CDP port — more reliable than proc.poll().

        Returns False immediately when no bridge is configured: the sidecar is not available
        regardless of whether TradingView Desktop is running on port 9222.
        """
        if self._tv_bridge is None:
            return False
        try:
            with socket.create_connection(("localhost", _TV_DEBUG_PORT), timeout=1.0):
                return True
        except OSError:
            return False

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def set_tv_bridge(self, bridge: TradingViewBridge) -> None:
        """Update the TradingView bridge reference after checker construction."""
        self._tv_bridge = bridge

    def start(self) -> None:
        """Start the background polling loop as an asyncio Task.

        Idempotent in the sense that it does nothing if a task is already running.
        If the previous task finished or was cancelled it creates a new one — it
        does not silently no-op like a lock guard would.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            log.info("ConnectivityChecker started (interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        """Cancel the background polling task. Safe to call if polling was never started."""
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("ConnectivityChecker stopped")

    def subscribe(self, callback: Callable[[str], Awaitable[None]]) -> Callable[[], None]:
        """Register a callback to receive future alert text (e.g. the same strings
        _DISCONNECT_MESSAGES/_RECONNECT_MESSAGES already produce today). Returns an
        unsubscribe function."""
        self._subscribers.append(callback)
        def _unsubscribe() -> None:
            """Detach this callback. Safe to call twice — a missing entry is ignored."""
            with suppress(ValueError):
                self._subscribers.remove(callback)
        return _unsubscribe

    # ── Internal ────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Check once immediately, then every POLL_INTERVAL seconds until cancelled.

        `CancelledError` breaks the loop cleanly; any other exception is logged and the
        loop continues, so one transient failure cannot take monitoring down for the rest
        of the session.
        """
        try:
            await self._run_checks()      # run once immediately on start
        except Exception as exc:
            log.warning("ConnectivityChecker initial poll error: %s", exc)
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self._run_checks()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ConnectivityChecker poll error: %s", exc)

    async def _run_checks(self) -> None:
        """Poll all three services, map results to ServiceStatus, and alert on transitions.

        IBKR needs no extra step here any more: the soft-timeout recovery it used to run
        (`ssodh/init` when a previously-OK session shows connected-but-unauthenticated)
        moved to `gateway_session.GatewaySession._poll_once` on 2026-08-06, along with the
        read itself. `check_ibkr` is now a cached lookup, so this poll makes two network
        calls rather than three. TradingView maps a closed CDP port to `UNKNOWN` (not
        launched) rather than `ERROR`. Alerts fire only on a state change.
        """
        ibkr_ok = await asyncio.to_thread(self.check_ibkr)
        gdrive_ok = await asyncio.to_thread(self.check_gdrive)
        tv_ok = await asyncio.to_thread(self.check_tradingview)
        new = {
            "ibkr":   ServiceStatus.OK if ibkr_ok else ServiceStatus.ERROR,
            "gdrive": ServiceStatus.OK if gdrive_ok else ServiceStatus.ERROR,
            # Not configured → UNKNOWN (gray dot), not ERROR (red dot)
            "tv": (
                ServiceStatus.OK if tv_ok
                else ServiceStatus.UNKNOWN if self._tv_bridge is None
                else ServiceStatus.ERROR
            ),
        }
        for service, new_state in new.items():
            prev_state = self._status[service]
            if prev_state != new_state:
                self._status[service] = new_state
                await self._send_alert(service, prev_state, new_state)

    async def _send_alert(self, service: str, prev: ServiceStatus, new: ServiceStatus) -> None:
        """Notify subscribers of a state change worth surfacing.

        Two deliberate silences: `UNKNOWN`→`OK` (a service coming online at startup is not
        news) and any transition into `UNKNOWN`. The subscriber list is copied before
        iterating so a callback that unsubscribes itself mid-notify cannot corrupt the walk.
        """
        if new == ServiceStatus.ERROR:
            msg = _DISCONNECT_MESSAGES.get(service, f"⚠️ {service} disconnected.")
        elif new == ServiceStatus.OK and prev == ServiceStatus.ERROR:
            msg = _RECONNECT_MESSAGES.get(service, f"✅ {service} reconnected.")
        else:
            return  # UNKNOWN → OK at startup: silent
        for subscriber in list(self._subscribers):  # copy: a subscriber unsubscribing
                                                       # itself mid-notify must not skip
                                                       # or corrupt the remaining iteration
            try:
                await subscriber(msg)
            except Exception as exc:
                log.warning("Could not push connectivity alert to a subscriber: %s", exc, exc_info=True)
