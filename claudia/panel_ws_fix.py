"""Runtime fix for bokeh-fastapi 0.1.8's dead websocket-disconnect detection.

Starlette's raw WebSocket.receive() RETURNS a {"type": "websocket.disconnect"}
message dict — it never raises WebSocketDisconnect (only receive_text/bytes/json
do; starlette/websockets.py:35-57). bokeh_fastapi's WSHandler._receive_loop only
catches the exception (bokeh_fastapi/handler.py:271-315), so the disconnect
message falls through, a second receive() raises RuntimeError (swallowed
upstream), client_lost()/detach_session() never run, the session's connection
count never reaches zero, and Bokeh's _cleanup_sessions never destroys the
session — pn.state.on_session_destroyed callbacks NEVER fire.

Probe-verified 2026-07-23 (see the Panel migration plan's "D7 RESOLVED" note):
with this fixed loop, disconnect is detected the second the tab closes and the
full destroy chain (15-32s unused-session cleanup) works normally. The original
loop also fails to break after its exception path — falling through to a stale/
unbound ws_msg — which this version corrects.

Version-guarded: patched only on known-broken releases; on any other version we
log a WARNING and leave the (possibly fixed) upstream code alone — without a
working disconnect path, session cleanup silently never runs, so the warning is
the honest signal to re-verify against the new release.
"""

import logging
from importlib.metadata import version as _pkg_version

# bokeh-fastapi 0.1.8 ships no py.typed marker, so mypy can't analyze it.
from bokeh_fastapi.handler import WSHandler  # type: ignore[import-untyped]
from starlette.websockets import WebSocketDisconnect

log = logging.getLogger(__name__)

_KNOWN_BROKEN = frozenset({"0.1.8"})


def _installed_version() -> str:
    return _pkg_version("bokeh-fastapi")


async def _receive_loop_fixed(self) -> None:
    """Faithful copy of WSHandler._receive_loop (bokeh-fastapi 0.1.8,
    handler.py:271-315) with two corrections: handle the returned
    websocket.disconnect message, and break (not fall through) on the
    WebSocketDisconnect exception path.

    Re-verify after any bokeh-fastapi upgrade by diffing
    inspect.getsource(WSHandler._receive_loop) against this function — expect
    exactly the two corrections (see docs/probes/README.md)."""
    while True:
        try:
            ws_msg = await self._socket.receive()
        except WebSocketDisconnect as e:
            log.info(
                "WebSocket connection closed: code=%s, reason=%r", e.code, e.reason
            )
            self.application.client_lost(self.connection)
            break
        if ws_msg.get("type") == "websocket.disconnect":
            log.info("WebSocket disconnect message: code=%s", ws_msg.get("code"))
            self.application.client_lost(self.connection)
            break

        if "text" in ws_msg:
            fragment = ws_msg["text"]
        elif "bytes" in ws_msg:
            fragment = ws_msg["bytes"]
        else:
            continue

        try:
            message = await self._receive(fragment)
        except Exception as e:
            # If you go look at self._receive, it's catching the
            # expected error types... here we have something weird.
            log.error(
                "Unhandled exception receiving a message: %r: %r",
                e,
                fragment,
                exc_info=True,
            )
            await self._internal_error("server failed to parse a message")
            message = None

        if not message:
            continue
        try:
            work = await self._handle(message)
            if work:
                await self.send_message(work)
        except Exception as e:
            log.error(
                "Handler or its work threw an exception: %r: %r",
                e,
                message,
                exc_info=True,
            )
            await self._internal_error("server failed to handle a message")


def apply_ws_disconnect_fix() -> bool:
    """Patch WSHandler._receive_loop on known-broken bokeh-fastapi versions.
    Returns True if patched. Call once at import time, before add_application."""
    ver = _installed_version()
    if ver not in _KNOWN_BROKEN:
        log.warning(
            "bokeh-fastapi %s is not a known-broken version — disconnect fix NOT "
            "applied; re-verify session-destroy behavior against this release "
            "(migration plan D7 notes) and update _KNOWN_BROKEN accordingly; "
            "without a working disconnect path, Panel session cleanup silently "
            "never runs.",
            ver,
        )
        return False
    WSHandler._receive_loop = _receive_loop_fixed
    log.info("Applied bokeh-fastapi %s websocket-disconnect fix", ver)
    return True
