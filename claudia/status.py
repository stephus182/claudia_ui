"""
Connectivity monitor for ClaudIA.

Polls IBKR gateway, GDrive token file, and TradingView sidecar every 60s.
Caches status in memory (instant reads for /api/status endpoint).
Pushes cl.Message alerts to chat on state transitions.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds


class ServiceStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


_DISCONNECT_MESSAGES = {
    "ibkr":   "⚠️ **IBKR Gateway disconnected** — check the Client Portal and log in.",
    "gdrive": "⚠️ **Google Drive disconnected** — credentials file not found.",
    "tv":     "⚠️ **TradingView sidecar stopped** — TradingView tools unavailable.",
}
_RECONNECT_MESSAGES = {
    "ibkr":   "✅ **IBKR Gateway reconnected.**",
    "gdrive": "✅ **Google Drive reconnected.**",
    "tv":     "✅ **TradingView reconnected.**",
}


class ConnectivityChecker:
    def __init__(
        self,
        gateway_url: str,
        gdrive_token_file: Path,
        tv_bridge: Optional["TradingViewBridge"] = None,
    ) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._gdrive_token_file = Path(gdrive_token_file)
        self._tv_bridge = tv_bridge
        self._status: dict[str, ServiceStatus] = {
            "ibkr":   ServiceStatus.UNKNOWN,
            "gdrive": ServiceStatus.UNKNOWN,
            "tv":     ServiceStatus.UNKNOWN,
        }
        self._task: asyncio.Task | None = None

    def get_status(self) -> dict[str, ServiceStatus]:
        return dict(self._status)

    # ── Individual checks (synchronous, cheap) ──────────────────────────────

    def check_ibkr(self) -> bool:
        try:
            resp = requests.get(
                f"{self._gateway_url}/tickle",
                timeout=3,
                verify=False,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def check_gdrive(self) -> bool:
        return self._gdrive_token_file.exists()

    def check_tradingview(self) -> bool:
        bridge = self._tv_bridge
        if bridge is None:
            return False
        proc = getattr(bridge, "_process", None)
        if proc is None:
            return False
        return proc.poll() is None  # None = process still alive

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            log.info("ConnectivityChecker started (interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("ConnectivityChecker stopped")

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
        new = {
            "ibkr":   ServiceStatus.OK if await asyncio.to_thread(self.check_ibkr) else ServiceStatus.ERROR,
            "gdrive": ServiceStatus.OK if self.check_gdrive() else ServiceStatus.ERROR,
            "tv":     ServiceStatus.OK if self.check_tradingview() else ServiceStatus.ERROR,
        }
        for service, new_state in new.items():
            prev_state = self._status[service]
            if prev_state != new_state:
                self._status[service] = new_state
                await self._send_alert(service, prev_state, new_state)

    async def _send_alert(self, service: str, prev: str, new: str) -> None:
        import chainlit as cl
        if new == ServiceStatus.ERROR:
            msg = _DISCONNECT_MESSAGES.get(service, f"⚠️ {service} disconnected.")
        elif new == ServiceStatus.OK and prev == ServiceStatus.ERROR:
            msg = _RECONNECT_MESSAGES.get(service, f"✅ {service} reconnected.")
        else:
            return  # UNKNOWN → OK at startup: silent
        try:
            await cl.Message(content=msg, author="System").send()
        except Exception as exc:
            log.warning("Could not push connectivity alert: %s", exc)
