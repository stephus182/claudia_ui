"""D7 probe variant 2: same topology, but with bokeh_fastapi's broken disconnect
detection monkeypatched (bokeh-fastapi 0.1.8 handler.py:271-296 expects
WebSocketDisconnect from starlette's raw WebSocket.receive(), which never raises it;
the 'websocket.disconnect' message dict falls through and client_lost() is never
called, so sessions are never marked unused and on_session_destroyed never fires).

The patch handles the disconnect message dict, calls client_lost(), and breaks —
this restores the normal bokeh cleanup chain so the rest of the destroy contract
(timing, thread, blocking behavior, async cb, curdoc) can be measured.

Run: .venv/bin/uvicorn probe_d7_server_fixed:app --port 8124
"""
import asyncio
import datetime
import json
import threading
import time
from pathlib import Path

import panel as pn
from fastapi import FastAPI
from panel.io.fastapi import add_application

# --- monkeypatch bokeh_fastapi WSHandler._receive_loop -----------------------
from starlette.websockets import WebSocketDisconnect  # noqa: E402
from bokeh_fastapi.handler import WSHandler  # noqa: E402
import logging

patch_log = logging.getLogger("d7.patch")


async def _receive_loop_fixed(self) -> None:
    while True:
        try:
            ws_msg = await self._socket.receive()
        except WebSocketDisconnect as e:
            log_rec({"event": "ws_disconnect_exception", "code": e.code})
            self.application.client_lost(self.connection)
            break
        if ws_msg.get("type") == "websocket.disconnect":
            # starlette's raw receive() returns the disconnect message instead of
            # raising (starlette/websockets.py:35-57) — handle it here.
            log_rec({"event": "ws_disconnect_message", "code": ws_msg.get("code")})
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
        except Exception:
            await self._internal_error("server failed to parse a message")
            message = None

        if not message:
            continue
        try:
            work = await self._handle(message)
            if work:
                await self.send_message(work)
        except Exception:
            await self._internal_error("server failed to handle a message")


WSHandler._receive_loop = _receive_loop_fixed
# -----------------------------------------------------------------------------

LOG = Path(__file__).parent / "d7_probe_log_fixed.jsonl"


def log_rec(rec: dict) -> None:
    rec["t"] = datetime.datetime.now().isoformat(timespec="milliseconds")
    with LOG.open("a") as f:
        f.write(json.dumps(rec, default=repr) + "\n")


app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@add_application("/", app=app, title="D7 Probe Fixed")
def page():
    doc = pn.state.curdoc
    sid = doc.session_context.id if doc and doc.session_context else None
    args = {k: v[0].decode() for k, v in (pn.state.session_args or {}).items()}
    label = args.get("label", "unlabeled")
    blocking = args.get("block", "0") == "1"
    log_rec({"event": "session_created", "label": label, "sid": sid,
             "blocking_cb": blocking, "thread": threading.current_thread().name})

    def echo(contents, user, instance):
        log_rec({"event": "chat_message_received", "label": label, "sid": sid,
                 "contents": str(contents),
                 "thread": threading.current_thread().name})
        return f"echo: {contents}"

    chat = pn.chat.ChatInterface(callback=echo, sizing_mode="stretch_width")

    def on_destroyed(session_context):
        rec = {
            "event": "destroyed_start",
            "label": label,
            "sid_at_create": sid,
            "cb_arg_type": type(session_context).__name__,
            "cb_arg_id": getattr(session_context, "id", None),
            "cb_arg_destroyed_flag": getattr(session_context, "destroyed", None),
            "thread": threading.current_thread().name,
        }
        try:
            loop = asyncio.get_running_loop()
            rec["asyncio_get_running_loop"] = f"OK running={loop.is_running()}"
        except RuntimeError as e:
            rec["asyncio_get_running_loop"] = f"RuntimeError: {e}"
        rec["pn_state_curdoc"] = repr(pn.state.curdoc)
        try:
            chat.send("goodbye from destroy hook", user="System", respond=False)
            rec["chat_send_in_callback"] = "no exception"
        except Exception as e:
            rec["chat_send_in_callback"] = f"{type(e).__name__}: {e}"
        log_rec(rec)
        if blocking:
            time.sleep(15)  # stand-in for blocking Drive upload
            log_rec({"event": "destroyed_after_block", "label": label,
                     "blocked_secs": 15})
        else:
            log_rec({"event": "destroyed_end", "label": label})

    pn.state.on_session_destroyed(on_destroyed)

    async def on_destroyed_async(session_context):
        log_rec({"event": "async_destroyed_cb_body_ran", "label": label})

    try:
        pn.state.on_session_destroyed(on_destroyed_async)
        log_rec({"event": "async_cb_registered", "label": label, "error": None})
    except Exception as e:
        log_rec({"event": "async_cb_registered", "label": label,
                 "error": f"{type(e).__name__}: {e}"})

    return pn.Column(f"# D7 fixed probe — session {label}", chat)
