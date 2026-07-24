"""pn.serve probe: re-verify V1-V6 under Panel's native Tornado server (no FastAPI,
no bokeh-fastapi, no monkeypatch). Adapted from d4_probe.py + probe_d7_server_fixed.py.

Topology mirrors what claudia/panel_app.py will become:

    if __name__ == "__main__": main()  ->  pn.serve(create_session, port=8003, show=False)

Verifies:
  V1  per-session factory semantics (module counter + singleton vs per-session chat)
  V2  asyncio.create_task background chat.send renders
  V3  plain-thread -> loop.call_soon_threadsafe(partial(chat.send, ...)) renders (x2 rapid)
  V4  pn.state.on_session_destroyed UNPATCHED: fires? timing? thread? once-per-session?
      async cb body? blocking cb freezes other sessions? (?block=1 -> time.sleep(10))
  V5  SIGINT with a session open: destroy cbs? does pn.serve() RETURN (post-serve code
      as the Task 5.6b shutdown hook)? atexit?
  V6  kwargs (port from env, show=False, title), Ctrl-C cleanliness, __main__ entry,
      tornado importable.

Run:  .venv/bin/python pnserve_probe.py           (port from PROBE_PORT, default 8003)
Log:  pnserve_log.jsonl next to this file (JSONL, ms timestamps).
"""

import asyncio
import atexit
import datetime
import json
import os
import threading
import time
from functools import partial
from pathlib import Path

import panel as pn
import tornado  # V6d: importable without new installs (Panel dependency)

pn.extension()

LOG = Path(__file__).parent / "pnserve_log.jsonl"

# --- V1 module-level (process-wide) state ------------------------------------
FACTORY_CALLS = 0
SINGLETON = None
TASKS: list = []  # keep strong refs to created tasks (RUF006)


class Singleton:
    """Stand-in for process-wide objects (GDriveSync, ConversationStore, ...)."""

    def __init__(self) -> None:
        self.created_at = datetime.datetime.now().isoformat(timespec="milliseconds")
        self.created_in_factory_call = FACTORY_CALLS


def log_rec(rec: dict) -> None:
    rec["t"] = datetime.datetime.now().isoformat(timespec="milliseconds")
    with LOG.open("a") as f:
        f.write(json.dumps(rec, default=repr) + "\n")


def _loop_status() -> str:
    try:
        loop = asyncio.get_running_loop()
        return f"OK running={loop.is_running()} id={id(loop)}"
    except RuntimeError as e:
        return f"RuntimeError: {e}"


def create_session():
    """Per-session factory passed as a callable to pn.serve."""
    global FACTORY_CALLS, SINGLETON
    FACTORY_CALLS += 1
    n = FACTORY_CALLS
    if SINGLETON is None:
        SINGLETON = Singleton()

    args = {k: v[0].decode() for k, v in (pn.state.session_args or {}).items()}
    label = args.get("label", f"sess{n}")
    blocking = args.get("block", "0") == "1"

    doc = pn.state.curdoc
    sid = doc.session_context.id if doc and doc.session_context else None

    # V1: factory context — thread + live running loop
    loop = asyncio.get_running_loop()  # raises if not on a live loop -> would 500
    log_rec({
        "event": "factory_call", "n": n, "label": label, "sid": sid,
        "thread": threading.current_thread().name,
        "running_loop": _loop_status(),
        "singleton_id": id(SINGLETON),
        "singleton_created_at": SINGLETON.created_at,
        "blocking_cb": blocking,
    })

    def echo(contents, user, instance):
        log_rec({"event": "chat_message_received", "label": label, "sid": sid,
                 "contents": str(contents),
                 "thread": threading.current_thread().name})
        return f"echo: {contents}"

    chat = pn.chat.ChatInterface(callback=echo, sizing_mode="stretch_width")
    log_rec({"event": "chat_created", "label": label, "chat_id": id(chat)})
    chat.send(f"ready [{label}] factory#{n}", user="System", respond=False)

    # --- V2: background asyncio task sends into the chat ---------------------
    async def late_send() -> None:
        await asyncio.sleep(2.0)
        try:
            chat.send(f"V2 late message [{label}]", user="System", respond=False)
            log_rec({"event": "v2_late_send_ok", "label": label,
                     "thread": threading.current_thread().name})
        except Exception as e:  # noqa: BLE001
            log_rec({"event": "v2_late_send_error", "label": label,
                     "error": f"{type(e).__name__}: {e}"})

    TASKS.append(asyncio.create_task(late_send()))

    # --- V3: plain OS thread -> loop.call_soon_threadsafe bridge -------------
    def thread_worker() -> None:
        time.sleep(4.0)
        try:
            loop.call_soon_threadsafe(
                partial(chat.send, f"V3 thread message 1 [{label}]",
                        user="System", respond=False))
            loop.call_soon_threadsafe(
                partial(chat.send, f"V3 thread message 2 [{label}]",
                        user="System", respond=False))
            log_rec({"event": "v3_thread_delivered", "label": label,
                     "thread": threading.current_thread().name,
                     "curdoc_on_thread": repr(pn.state.curdoc)})
        except Exception as e:  # noqa: BLE001
            log_rec({"event": "v3_thread_error", "label": label,
                     "error": f"{type(e).__name__}: {e}"})

    threading.Thread(target=thread_worker, name=f"v3-{label}", daemon=True).start()

    # --- V4: destroy callbacks (sync + async), optional blocking -------------
    def on_destroyed(session_context) -> None:
        rec = {
            "event": "destroyed_start", "label": label, "sid_at_create": sid,
            "cb_arg_type": type(session_context).__name__,
            "cb_arg_id": getattr(session_context, "id", None),
            "cb_arg_destroyed_flag": getattr(session_context, "destroyed", None),
            "thread": threading.current_thread().name,
            "running_loop_in_cb": _loop_status(),
            "pn_state_curdoc": repr(pn.state.curdoc),
        }
        try:
            chat.send("goodbye from destroy hook", user="System", respond=False)
            rec["chat_send_in_callback"] = "no exception"
        except Exception as e:  # noqa: BLE001
            rec["chat_send_in_callback"] = f"{type(e).__name__}: {e}"
        log_rec(rec)
        if blocking:
            time.sleep(10)  # stand-in for a blocking Drive upload
            log_rec({"event": "destroyed_after_block", "label": label,
                     "blocked_secs": 10})
        else:
            log_rec({"event": "destroyed_end", "label": label})

    pn.state.on_session_destroyed(on_destroyed)

    async def on_destroyed_async(session_context) -> None:
        log_rec({"event": "async_destroyed_cb_body_ran", "label": label})

    try:
        pn.state.on_session_destroyed(on_destroyed_async)
        log_rec({"event": "async_cb_registered", "label": label, "error": None})
    except Exception as e:  # noqa: BLE001
        log_rec({"event": "async_cb_registered", "label": label,
                 "error": f"{type(e).__name__}: {e}"})

    return pn.Column(
        f"# pnserve probe — {label} | factory#{n} | chat_id={id(chat)} | "
        f"singleton_id={id(SINGLETON)}",
        chat,
    )


def main() -> None:
    port = int(os.environ.get("PROBE_PORT", "8003"))
    # V6a: optional comma-separated origins, e.g. "localhost:8003,127.0.0.1:8003"
    ws_origin_env = os.environ.get("PROBE_WS_ORIGIN", "")
    ws_origin = [o for o in ws_origin_env.split(",") if o] or None
    log_rec({"event": "main_start", "pid": os.getpid(), "port": port,
             "panel": pn.__version__, "tornado": tornado.version,
             "websocket_origin": ws_origin,
             "main_thread": threading.current_thread().name})
    atexit.register(lambda: log_rec({"event": "atexit_ran"}))
    server = None
    try:
        # V6a: port from env, no browser auto-open, title set
        server = pn.serve(
            create_session, port=port, show=False, title="pnserve probe",
            start=True, websocket_origin=ws_origin,
        )
        # V5: reached only if/when the server loop stops (SIGINT path)
        sessions = None
        try:
            sessions = [s.id for s in server.get_sessions()]
        except Exception as e:  # noqa: BLE001
            sessions = f"get_sessions failed: {type(e).__name__}: {e}"
        log_rec({"event": "pn_serve_returned", "server": repr(server),
                 "live_sessions_at_return": sessions})
    finally:
        # Task 5.6b candidate hook: code here runs after Ctrl-C stops the loop
        log_rec({"event": "post_serve_finally",
                 "note": "Task 5.6b Drive upload would run here"})


if __name__ == "__main__":
    main()
