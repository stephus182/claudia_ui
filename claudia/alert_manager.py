"""
Background price alert monitor for ClaudIA.

Polls ibkr_core_mcp's SQLiteStore for active price alerts and compares them
against live IBKR market snapshots. When an alert triggers, posts a Chainlit
notification to the active session.

The ibkr_core_mcp AlertManager handles alert persistence; this module provides
the Chainlit integration layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import chainlit as cl

if TYPE_CHECKING:
    from ibkr_core_mcp import IBKRClient, SQLiteStore

log = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds between alert checks


class AlertManager:
    """Starts a background task that watches for price alert triggers."""

    def __init__(self, ibkr: "IBKRClient", store: "SQLiteStore") -> None:
        self._ibkr = ibkr
        self._store = store
        self._task: asyncio.Task | None = None
        self._running = False
        self._triggered: set[int] = set()  # already-fired alert IDs this session

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            log.info("AlertManager started (poll interval: %ds)", _POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_alerts()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Alert poll error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _check_alerts(self) -> None:
        alerts = await cl.make_async(self._store.get_alerts)(active_only=True)
        if not alerts:
            return

        # Fetch live snapshots for unique conids
        conids = list({a["conid"] for a in alerts if a.get("conid") and a["id"] not in self._triggered})
        if not conids:
            return

        try:
            snapshots = await cl.make_async(self._ibkr.get_market_snapshot)(conids)
        except Exception as exc:
            log.debug("Could not fetch market snapshot for alerts: %s", exc)
            return

        price_map: dict[int, float] = {}
        for snap in (snapshots or []):
            conid = snap.get("conid") or snap.get("con_id")
            price = snap.get("31") or snap.get("last") or snap.get("84")  # field 31 = last price
            if conid and price:
                try:
                    price_map[int(conid)] = float(price)
                except (ValueError, TypeError):
                    pass

        for alert in alerts:
            alert_id = alert.get("id")
            if alert_id in self._triggered:
                continue
            conid = alert.get("conid")
            if not conid:
                continue
            current = price_map.get(int(conid))
            if current is None:
                continue

            condition = alert.get("condition", "")
            trigger_price = float(alert.get("trigger_price", 0))
            symbol = alert.get("symbol", str(conid))

            triggered = False
            if condition in (">=", "above") and current >= trigger_price:
                triggered = True
                direction = "above"
            elif condition in ("<=", "below") and current <= trigger_price:
                triggered = True
                direction = "below"

            if triggered:
                self._triggered.add(alert_id)
                msg = (
                    f"**Price Alert Triggered** — **{symbol}** is {direction} "
                    f"${trigger_price:.2f} (current: ${current:.2f})"
                )
                await cl.Message(content=msg, author="ClaudIA Alert").send()
                log.info("Alert fired: %s %s %s %.2f", symbol, condition, trigger_price, current)
