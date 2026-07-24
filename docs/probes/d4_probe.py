"""D4 probe: deliver chat.send() into a live Panel session from a plain OS thread.

Mount idiom copied from claudia/panel_app.py (FastAPI + panel.io.fastapi.add_application,
served by uvicorn). One candidate active per run, selected via env var D4_CANDIDATE:

  A       loop bridge: factory captures asyncio.get_running_loop();
          thread calls loop.call_soon_threadsafe(partial(chat.send, ...))
  B       pn.state.execute(partial(chat.send, ...)) called from the plain thread,
          nothing captured (note: state.curdoc is a ContextVar -> None on a plain
          thread, so per panel/io/state.py:726-743 this degenerates to a direct call)
  BPRIME  factory captures doc = pn.state.curdoc;
          thread calls doc.add_next_tick_callback(partial(chat.send, ...))
  C       naive: thread calls chat.send(...) directly

  A2 / BPRIME2 / C2: pressure variants — same idiom, TWO messages back-to-back.

Run:  D4_CANDIDATE=A .venv/bin/uvicorn d4_probe:app --port 8002
"""

import asyncio
import os
import threading
import time
from functools import partial

import panel as pn
from fastapi import FastAPI
from panel.io.fastapi import add_application

CANDIDATE = os.environ.get("D4_CANDIDATE", "A").upper()


def _build_chat_app() -> pn.chat.ChatInterface:
    chat = pn.chat.ChatInterface()
    chat.send(
        f"probe ready — candidate {CANDIDATE}", user="System", respond=False
    )

    # Captures made synchronously in the factory (session event-loop context)
    loop = asyncio.get_running_loop()
    doc = pn.state.curdoc
    print(f"[probe] factory: loop={loop!r} doc={doc!r} "
          f"thread={threading.current_thread().name}", flush=True)

    def make_msg(label: str, n: int = 1) -> str:
        return f"CANDIDATE-{label} message {n} delivered from thread {threading.current_thread().name}"

    def worker() -> None:
        time.sleep(2.0)
        print(f"[probe] worker firing candidate {CANDIDATE} "
              f"(thread {threading.current_thread().name}, "
              f"pn.state.curdoc={pn.state.curdoc!r})", flush=True)
        try:
            if CANDIDATE == "A":
                loop.call_soon_threadsafe(
                    partial(chat.send, make_msg("A"), user="Alert", respond=False)
                )
            elif CANDIDATE == "A2":
                loop.call_soon_threadsafe(
                    partial(chat.send, make_msg("A2", 1), user="Alert", respond=False)
                )
                loop.call_soon_threadsafe(
                    partial(chat.send, make_msg("A2", 2), user="Alert", respond=False)
                )
            elif CANDIDATE == "B":
                pn.state.execute(
                    partial(chat.send, make_msg("B"), user="Alert", respond=False)
                )
            elif CANDIDATE == "BPRIME":
                doc.add_next_tick_callback(
                    partial(chat.send, make_msg("BPRIME"), user="Alert", respond=False)
                )
            elif CANDIDATE == "BPRIME2":
                doc.add_next_tick_callback(
                    partial(chat.send, make_msg("BPRIME2", 1), user="Alert", respond=False)
                )
                doc.add_next_tick_callback(
                    partial(chat.send, make_msg("BPRIME2", 2), user="Alert", respond=False)
                )
            elif CANDIDATE == "C":
                chat.send(make_msg("C"), user="Alert", respond=False)
            elif CANDIDATE == "C2":
                chat.send(make_msg("C2", 1), user="Alert", respond=False)
                chat.send(make_msg("C2", 2), user="Alert", respond=False)
            else:
                raise ValueError(f"unknown candidate {CANDIDATE}")
        except Exception:
            import traceback
            traceback.print_exc()
        # Report server-side object count a moment later, for the
        # "in chat.objects but not rendered" failure-mode check.
        time.sleep(2.0)
        print(f"[probe] post-delivery: len(chat.objects)={len(chat.objects)} "
              f"texts={[getattr(o, 'object', o) for o in chat.objects]}", flush=True)

    threading.Thread(target=worker, name="d4-watchdog-sim", daemon=True).start()
    return chat


app = FastAPI()


@add_application("/", app=app, title="D4 probe")
def _serve() -> pn.chat.ChatInterface:
    return _build_chat_app()
