"""Probe: ContextLoader.stop_watching multi-session behavior (Task 5.6 Verification 2).

Two ContextLoader instances watch the SAME temp docs dir on the shared module-level
Observer. Verifies whether one loader's stop_watching() / start_watching() restart
kills the other loader's hot-reload callback.

Run: .venv/bin/python probe_watchdog.py   (from worktree root or anywhere)
"""
import sys
import time
import shutil
import threading
from pathlib import Path

WORKTREE = "/Users/steph/Claude_Projects/claudia_ui/.worktrees/panel-migration"
sys.path.insert(0, WORKTREE)

from claudia.context_loader import ContextLoader, _get_shared_observer  # noqa: E402

SCRATCH = Path(__file__).parent
DOCS = SCRATCH / "watchdog_probe_docs"

WAIT = 3.0  # FSEvents + 0.3s debounce margin


def setup_docs():
    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir()
    (DOCS / "context.md").write_text("initial context\n")
    (DOCS / "principles.md").write_text("initial principles\n")


counter = 0


def touch(label):
    """Modify context.md with unique content, wait for events to propagate."""
    global counter
    counter += 1
    (DOCS / "context.md").write_text(f"content revision {counter} ({label})\n")
    time.sleep(WAIT)


def main():
    setup_docs()

    fired = {"cb1": [], "cb2": []}
    ev1, ev2 = threading.Event(), threading.Event()

    def cb1(changed_file, new_prompt):
        fired["cb1"].append(changed_file)
        ev1.set()

    def cb2(changed_file, new_prompt):
        fired["cb2"].append(changed_file)
        ev2.set()

    l1 = ContextLoader(docs_path=DOCS)
    l2 = ContextLoader(docs_path=DOCS)

    print("=== STEP 0: internals after both loaders start_watching ===")
    l1.start_watching(cb1)
    l2.start_watching(cb2)
    obs = _get_shared_observer()
    print(f"l1._watch == l2._watch : {l1._watch == l2._watch}")
    print(f"l1._watch is l2._watch : {l1._watch is l2._watch}")
    print(f"observer._handlers keys: {len(obs._handlers)}")
    for w, hs in obs._handlers.items():
        print(f"  watch={w!r} -> {len(hs)} handler(s)")
    print(f"observer._emitters     : {len(obs._emitters)}")

    print("\n=== STEP 1: touch with both watching -> expect cb1 AND cb2 ===")
    ev1.clear(); ev2.clear()
    touch("both-watching")
    ev1.wait(2); ev2.wait(2)
    print(f"cb1 fired: {ev1.is_set()}   cb2 fired: {ev2.is_set()}")

    print("\n=== STEP 2: l1.stop_watching() -> touch -> does cb2 still fire? ===")
    l1.stop_watching()
    print(f"after l1.stop: observer._handlers keys: {len(obs._handlers)}, "
          f"emitters: {len(obs._emitters)}")
    for w, hs in obs._handlers.items():
        print(f"  watch={w!r} -> {len(hs)} handler(s)")
    ev1.clear(); ev2.clear()
    touch("after-l1-stop")
    ev2.wait(2)
    print(f"cb1 fired: {ev1.is_set()} (expected False)   "
          f"cb2 fired: {ev2.is_set()}  <-- THE TRAP QUESTION")
    step2_cb2 = ev2.is_set()

    print("\n=== STEP 3: fresh setup, then l1 RESTARTS (start_watching again) "
          "while l2 active -> touch -> cb2? ===")
    l1b = ContextLoader(docs_path=DOCS)
    l2b = ContextLoader(docs_path=DOCS)
    fired3 = {"cb1b": 0, "cb2b": 0}
    ev1b, ev2b = threading.Event(), threading.Event()

    def cb1b(f, p):
        fired3["cb1b"] += 1
        ev1b.set()

    def cb2b(f, p):
        fired3["cb2b"] += 1
        ev2b.set()

    l1b.start_watching(cb1b)
    l2b.start_watching(cb2b)
    # sanity: both fire
    ev1b.clear(); ev2b.clear()
    touch("step3-baseline")
    ev1b.wait(2); ev2b.wait(2)
    print(f"baseline: cb1b={ev1b.is_set()} cb2b={ev2b.is_set()}")
    # restart l1b only
    l1b.start_watching(cb1b)
    print(f"after l1b restart: handlers keys={len(obs._handlers)}")
    for w, hs in obs._handlers.items():
        print(f"  watch={w!r} -> {len(hs)} handler(s)")
    ev1b.clear(); ev2b.clear()
    touch("after-l1b-restart")
    ev1b.wait(2); ev2b.wait(2)
    print(f"after restart touch: cb1b fired={ev1b.is_set()}   "
          f"cb2b fired={ev2b.is_set()}  <-- RESTART QUESTION")
    step3_cb2 = ev2b.is_set()
    step3_cb1 = ev1b.is_set()

    print("\n=== VERDICT ===")
    print(f"one session's stop_watching kills sibling's callback: {not step2_cb2}")
    print(f"one session's start_watching restart kills sibling:   {not step3_cb2}")
    print(f"restarting session itself still works after restart:  {step3_cb1}")


if __name__ == "__main__":
    main()
