"""Action bar — three reconnect buttons whose colour is the service's live state.

Replaces the status dots and the in-chat button message (2026-09-03). The rule the user
set: a click **reconnects**, it never merely checks — the light is already accurate, that is
the whole point. Colour is the light: green connected, red down, white with a border for
not configured (or not yet checked). The bar is repainted from `ConnectivityChecker.get_status()` on the same
5-second timer as the dashboard, and again immediately after a reconnect finishes.

Panel-only: no IBKR, no SQL, no `panel_app` import. The reconnect coroutines are injected —
they live in `panel_app`, next to the singletons they touch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

import panel as pn

from claudia.status import ServiceStatus

# Keyed like ConnectivityChecker.get_status(); the order is the order on screen.
SERVICE_LABELS: dict[str, str] = {"ibkr": "IBKR", "tv": "TradingView", "gdrive": "Drive"}

_DRIVE_UNCONFIGURED_HINT = "Drive not configured — set GOOGLE_DRIVE_FOLDER_ID"
_RECONNECT_HINT = "Click to reconnect"

Reconnect = Callable[[], Awaitable[None]]

# `default`, not `light`, for the neutral state and for End Session: Bokeh's `light` style
# sets `border-color: transparent` (bokeh.js, `.bk-btn-light`), so a light button on the
# white page reads as loose text with no edge. `default` keeps a real border and a white
# fill — a button that looks like a button (user 2026-09-04).
_COLOR: dict[ServiceStatus, Literal["success", "danger", "default"]] = {
    ServiceStatus.OK: "success",
    ServiceStatus.ERROR: "danger",
    ServiceStatus.UNKNOWN: "default",
}


class ActionBar:
    """One `pn.Row`: a button per monitored service, then End Session.

    `repaint(status)` is the only way a colour changes; `status` (a getter) is called once
    more after each reconnect so the light does not wait for the next timer tick.
    """

    def __init__(
        self,
        reconnect: Mapping[str, Reconnect],
        end_session: Callable[[], Awaitable[None]],
        on_error: Callable[[str], None],
        status: Callable[[], Mapping[str, ServiceStatus]],
    ) -> None:
        """Wire the buttons.

        Args:
            reconnect: One coroutine factory per key in `SERVICE_LABELS`.
            end_session: Awaited by End Session; the bar disables itself first.
            on_error: Receives one line when a reconnect raises. The caller decides where
                it goes (the System log, in ClaudIA).
            status: Returns the checker's current status map; called after a reconnect.
        """
        self._reconnect = dict(reconnect)
        self._end_session = end_session
        self._on_error = on_error
        self._status = status
        self._busy: set[str] = set()
        self.buttons: dict[str, pn.widgets.Button] = {
            key: pn.widgets.Button(label=label, color="default", description=_RECONNECT_HINT)
            for key, label in SERVICE_LABELS.items()
        }
        for key, button in self.buttons.items():
            button.on_click(self._make_click(key))
        self.end_button = pn.widgets.Button(label="End Session", color="default")
        self.end_button.on_click(self._on_end)
        self.row = pn.Row(*self.buttons.values(), self.end_button)

    def repaint(self, status: Mapping[str, ServiceStatus]) -> None:
        """Recolour from a status map. Unknown keys are ignored; missing ones are left as is.

        Drive alone is disabled on UNKNOWN: for Drive that state means "no credentials",
        and a click cannot reconnect what was never configured. For IBKR and TradingView
        UNKNOWN is "not checked yet", and a click may well be what brings them up.
        A button whose reconnect is in flight keeps its disable-first state.
        """
        for key, button in self.buttons.items():
            if key not in status:
                continue
            button.color = _COLOR[status[key]]
            if key in self._busy:
                continue
            unconfigured = key == "gdrive" and status[key] is ServiceStatus.UNKNOWN
            button.disabled = unconfigured
            button.description = _DRIVE_UNCONFIGURED_HINT if unconfigured else _RECONNECT_HINT

    def disable_all(self) -> None:
        """Leave the whole bar inert (after End Session)."""
        for button in self.row.objects:
            button.disabled = True

    def _make_click(self, key: str) -> Callable[[Any], Awaitable[None]]:
        """Build the click handler for one service: disable-first, await, always re-enable."""
        button = self.buttons[key]
        label = SERVICE_LABELS[key]

        async def _on_click(event: Any) -> None:
            """Run this service's reconnect once; report a failure; repaint from `status`."""
            self._busy.add(key)
            button.disabled = True
            button.loading = True
            try:
                await self._reconnect[key]()
            except Exception as exc:
                self._on_error(f"✕ {label} reconnect failed: {exc}")
            finally:
                self._busy.discard(key)
                button.loading = False
                button.disabled = False
                self.repaint(self._status())

        return _on_click

    async def _on_end(self, event: Any) -> None:
        """End Session: disable everything first, then run the injected cleanup."""
        self.disable_all()
        await self._end_session()
