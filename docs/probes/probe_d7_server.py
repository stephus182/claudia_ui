"""D7 probe: pn.state.on_session_destroyed under FastAPI add_application + uvicorn.

Mirrors the claudia panel_app.py topology (panel_app.py:386-389).
Each session (tab) is labeled via ?label=X&block=1 query args.
All observations go to d7_probe_log.jsonl as JSON lines with wallclock timestamps.

Run: .venv/bin/uvicorn probe_d7_server:app --port 8123
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

LOG = Path(__file__).parent / "d7_probe_log.jsonl"


def log(rec: dict) -> None:
    rec["t"] = datetime.datetime.now().isoformat(timespec="milliseconds")
    with LOG.open("a") as f:
        f.write(json.dumps(rec, default=repr) + "\n")


app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@add_application("/", app=app, title="D7 Probe")
def page():
    doc = pn.state.curdoc
    sid = doc.session_context.id if doc and doc.session_context else None
    args = {k: v[0].decode() for k, v in (pn.state.session_args or {}).items()}
    label = args.get("label", "unlabeled")
    blocking = args.get("block", "0") == "1"
    log({"event": "session_created", "label": label, "sid": sid,
         "blocking_cb": blocking, "thread": threading.current_thread().name})

    def echo(contents, user, instance):
        # server-side receipt timestamp is the responsiveness measurement
        log({"event": "chat_message_received", "label": label, "sid": sid,
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
            # closure access obviously works; test whether UI ops still function
            chat.send("goodbye from destroy hook", user="System", respond=False)
            rec["chat_send_in_callback"] = "no exception"
        except Exception as e:
            rec["chat_send_in_callback"] = f"{type(e).__name__}: {e}"
        log(rec)
        if blocking:
            time.sleep(15)  # stand-in for blocking Drive upload
            log({"event": "destroyed_after_block", "label": label,
                 "blocked_secs": 15})
        else:
            log({"event": "destroyed_end", "label": label})

    pn.state.on_session_destroyed(on_destroyed)

    # Does registering an ASYNC callback work / fire / get awaited?
    async def on_destroyed_async(session_context):
        log({"event": "async_destroyed_cb_body_ran", "label": label})

    try:
        pn.state.on_session_destroyed(on_destroyed_async)
        log({"event": "async_cb_registered", "label": label, "error": None})
    except Exception as e:
        log({"event": "async_cb_registered", "label": label,
             "error": f"{type(e).__name__}: {e}"})

    return pn.Column(f"# D7 probe — session {label}", chat)
