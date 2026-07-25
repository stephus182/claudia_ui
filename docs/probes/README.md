# Verification probes (Panel migration — D4/D7/watchdog)

Standalone scripts behind the design decisions in `docs/plans/2026-07-22-panel-migration.md`
(D4/D7 notes) and Task 5.6a. Historical artifacts, copied verbatim from the runs that produced the
findings — not maintained code, not collected by pytest. Re-run them after upgrading Panel,
bokeh-fastapi, or watchdog.

| Probe | Verifies | Run |
|---|---|---|
| `d4_probe.py` | Thread→session chat delivery idiom: candidate A (`loop.call_soon_threadsafe` bridge) works; B/BPRIME/C variants characterized | `D4_CANDIDATE=A .venv/bin/uvicorn d4_probe:app --port 8002` (from this dir) |
| `probe_d7_server.py` | Stock bokeh-fastapi 0.1.8 topology: `pn.state.on_session_destroyed` hooks NEVER fire (disconnect message dropped) | `.venv/bin/uvicorn probe_d7_server:app --port 8123` |
| `probe_d7_server_fixed.py` | Fixed disconnect bridge: full destroy contract/timing (hook arg, thread, curdoc, blocking + async callbacks) | `.venv/bin/uvicorn probe_d7_server_fixed:app --port 8124` |
| `probe_watchdog.py` | watchdog 6.0.0 shared-key trap: one loader's `unschedule` kills the sibling's hot-reload | `.venv/bin/python probe_watchdog.py` |
| `probe_watchdog_fix.py` | `remove_handler_for_watch` fix: sibling keeps firing; double-remove raises KeyError; re-schedule works after last handler removed | `.venv/bin/python probe_watchdog_fix.py` |
| `pnserve_probe.py` | V1-V6 native-`pn.serve` re-verification that justified the topology switch: per-session factory, background send, thread bridge, unpatched destroy hooks + timing, SIGINT/SIGTERM shutdown behavior, `websocket_origin` allowlist. Runs on plain panel — no extras needed | `.venv/bin/python pnserve_probe.py` |

Expected observations (one line each):

- `d4_probe.py`: candidate A renders both messages in the open tab; C (naive direct call) fails/misrenders.
- `probe_d7_server.py`: `d7_probe_log.jsonl` shows `session_created` but zero destroy events ever, even minutes after tab close.
- `probe_d7_server_fixed.py`: disconnect logged immediately on tab close; session discarded and destroy hooks run 15-35s later.
- `probe_watchdog.py`: VERDICT lines — stop_watching kills sibling: True; start_watching restart kills sibling: True (the bug).
- `probe_watchdog_fix.py`: h2 still fires after h1 removed; double-remove → KeyError; re-schedule after last removal → OK.
- `pnserve_probe.py`: destroy fires unpatched 15-24s after close; pn.serve returns ~2ms after SIGINT; SIGTERM bypasses everything.

Note: the watchdog probes hardcode the worktree path in `WORKTREE`; the server probes write
`.jsonl` logs next to themselves.

Note: `d4_probe.py`, `probe_d7_server.py` and `probe_d7_server_fixed.py` document the
ABANDONED FastAPI + `add_application` topology (replaced by native `pn.serve` per the
migration plan's Panel-native principle, 2026-07-24) — running them now requires
`pip install "panel[fastapi]"` + uvicorn, no longer project dependencies. Unlike the D7
probes, D4's FINDING (the `loop.call_soon_threadsafe` thread→session bridge) remains
current live code in `panel_app.py`'s `_on_doc_change` — only its serving harness used
the old topology.
