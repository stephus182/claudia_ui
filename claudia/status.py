"""
Connectivity monitor for ClaudIA.

Polls IBKR gateway, GDrive token file, and TradingView sidecar every 60s.
Caches status in memory (instant reads for /api/status endpoint).
Notifies registered subscribers on state transitions; alert delivery (e.g. Chainlit
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

import requests

from claudia.tradingview import _TV_DEBUG_PORT

if TYPE_CHECKING:
    from claudia.gdrive_sync import GDriveSync
    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds — matches IBKR /tickle keepalive requirement


class ServiceStatus(StrEnum):
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
    The cached status dict is served synchronously by GET /api/status for the UI lights.
    """

    def __init__(
        self,
        gateway_url: str,
        gdrive_token_file: Path,
        tv_bridge: TradingViewBridge | None = None,
        gdrive_sync: GDriveSync | None = None,
    ) -> None:
        """Initialise the checker. Call start() to begin polling.

        gdrive_sync is optional — if None, check_gdrive() falls back to a token-file
        existence check (no live API round-trip).
        """
        self._gateway_url = gateway_url.rstrip("/")
        self._gdrive_token_file = Path(gdrive_token_file)
        self._tv_bridge = tv_bridge
        self._gdrive_sync = gdrive_sync
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
        """Return True if the IBKR gateway is reachable and the session is authenticated.

        Side effect: calling /tickle resets the IBKR session keepalive timer, so
        the 60-second poll interval also prevents auto-logout during idle sessions.
        """
        try:
            resp = requests.get(
                f"{self._gateway_url}/tickle",
                timeout=3,
                verify=False,  # IBKR gateway uses a self-signed cert on localhost
            )
            if resp.status_code != 200:
                self._last_ibkr_auth_status = {}
                return False
            auth = resp.json().get("iserver", {}).get("authStatus", {})
            self._last_ibkr_auth_status = auth
            if auth.get("competing"):
                log.warning("IBKR: competing session detected — another TWS/gateway session is active")
            return bool(auth.get("authenticated") and auth.get("connected"))
        except Exception:
            self._last_ibkr_auth_status = {}
            return False

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

    def _attempt_soft_recovery(self) -> bool:
        """Silently re-establish a soft-timed-out brokerage session.

        Only ever called from _run_checks() when the previous poll was OK and the
        current poll shows IBKR's documented soft-timeout signature
        (connected=true, authenticated=false) — never on a fresh/settling login
        (that transition starts from UNKNOWN) and never on a hard disconnect
        (connected=false). `compete` is hardcoded False: it must never force-evict
        a concurrent IBKR Mobile/TWS session — if a real competing session is the
        actual cause, IBKR returns HTTP 200 with authenticated:false in the body
        (same response shape as /tickle) rather than an error status, so this
        method checks the body, not just the status code, before reporting success.

        Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ssodh-init
        Endpoint: POST /iserver/auth/ssodh/init
        """
        try:
            resp = requests.post(
                f"{self._gateway_url}/iserver/auth/ssodh/init",
                json={"publish": True, "compete": False},
                timeout=5,
                verify=False,
            )
            if resp.status_code != 200:
                return False
            return bool(resp.json().get("authenticated"))
        except Exception:
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
            with suppress(ValueError):
                self._subscribers.remove(callback)
        return _unsubscribe

    # ── Internal ────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
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
        ibkr_ok = await asyncio.to_thread(self.check_ibkr)
        if not ibkr_ok and self._status["ibkr"] == ServiceStatus.OK:
            auth = self._last_ibkr_auth_status
            if (
                auth.get("connected")
                and not auth.get("authenticated")
                and await asyncio.to_thread(self._attempt_soft_recovery)
            ):
                log.info("IBKR: soft-timeout recovered silently via ssodh/init")
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
                log.warning("Could not push connectivity alert to a subscriber: %s", exc)
