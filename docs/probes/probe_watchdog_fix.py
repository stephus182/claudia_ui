"""Probe: verify the candidate FIX pattern for ContextLoader teardown.

Pattern: keep the handler reference; per-session teardown uses
BaseObserver.remove_handler_for_watch(handler, watch) instead of unschedule(watch).
Checks:
  1. removing one handler leaves the sibling's handler firing
  2. removing the LAST handler leaves an empty-set entry + live emitter (documented)
  3. re-scheduling the same path afterwards works (no FSEvents 'already scheduled')
  4. double-remove raises KeyError (must be suppressed in stop_watching)
"""
import sys
import time
import shutil
import threading
from pathlib import Path

WORKTREE = "/Users/steph/Claude_Projects/claudia_ui/.worktrees/panel-migration"
sys.path.insert(0, WORKTREE)

from watchdog.events import FileSystemEvent, FileSystemEventHandler  # noqa: E402
from claudia.context_loader import _get_shared_observer  # noqa: E402

SCRATCH = Path(__file__).parent
DOCS = SCRATCH / "watchdog_probe_docs_fix"
WAIT = 3.0

if DOCS.exists():
    shutil.rmtree(DOCS)
DOCS.mkdir()
target = DOCS / "context.md"
target.write_text("initial\n")

counter = 0


def touch(label):
    global counter
    counter += 1
    target.write_text(f"rev {counter} {label}\n")
    time.sleep(WAIT)


class H(FileSystemEventHandler):
    def __init__(self, name, store):
        self.name = name
        self.store = store
        self.ev = threading.Event()

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self.store.append(self.name)
            self.ev.set()


obs = _get_shared_observer()
store = []
h1, h2 = H("h1", store), H("h2", store)

w1 = obs.schedule(h1, str(DOCS), recursive=False)
w2 = obs.schedule(h2, str(DOCS), recursive=False)

print("=== baseline: both handlers fire ===")
h1.ev.clear(); h2.ev.clear()
touch("baseline")
h1.ev.wait(2); h2.ev.wait(2)
print(f"h1={h1.ev.is_set()} h2={h2.ev.is_set()}")

print("\n=== remove h1 via remove_handler_for_watch -> h2 must still fire ===")
obs.remove_handler_for_watch(h1, w1)
h1.ev.clear(); h2.ev.clear()
touch("after-remove-h1")
h2.ev.wait(2)
print(f"h1={h1.ev.is_set()} (want False)  h2={h2.ev.is_set()} (want True)")
print(f"handlers for watch: {len(obs._handlers[w1])}, emitters: {len(obs._emitters)}")

print("\n=== double-remove h1 -> exception type? ===")
try:
    obs.remove_handler_for_watch(h1, w1)
    print("no exception (unexpected)")
except Exception as e:
    print(f"raised {type(e).__name__}: {e!r}")

print("\n=== remove LAST handler (h2) -> emitter state, then re-schedule works? ===")
obs.remove_handler_for_watch(h2, w2)
print(f"handlers for watch: {len(obs._handlers[w1])}, emitters: {len(obs._emitters)}")
h3 = H("h3", store)
try:
    w3 = obs.schedule(h3, str(DOCS), recursive=False)
    print("re-schedule after last-handler-removal: OK (no FSEvents error)")
except Exception as e:
    print(f"re-schedule FAILED: {type(e).__name__}: {e}")
    raise SystemExit(1)
h3.ev.clear()
touch("after-reschedule")
h3.ev.wait(2)
print(f"h3 fires after re-schedule: {h3.ev.is_set()}")

print("\n=== unschedule while another handler present (sanity of trap) ===")
# w3 currently has h3 only; add h4 then unschedule -> both die (already proven)
print("skipped (proven by probe_watchdog.py)")
print("\nfired sequence:", store)
