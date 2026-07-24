# Panel documentation & UI-design research

Single home for all **Panel framework docs, UI-design research, and verified API findings**
gathered during the Chainlit→Panel migration and beyond. Established 2026-07-24 at the
user's request ("keep all Panel Doc, Panel UI design docs in one documentation folder for
future use"). The dedicated post-migration **restyle plan** draws from here.

**Convention (per CLAUDE.md "API Docs First"):** every claim in these docs is backed by an
authoritative source — the *installed* package (panel version is the source of truth for
our behavior), *local* source (sidecars), or an official web doc (scraped via firecrawl,
URL cited). Probe scripts referenced in the docs live in the session scratchpad and, when
load-bearing, are committed under `docs/probes/`.

Distinct from:
- `docs/superpowers/plans/2026-07-22-panel-migration.md` — the migration execution plan
  (task-by-task). These research docs are the *reference material* it and the restyle plan
  cite, not the plan itself.
- `docs/probes/` — runnable verification scripts (D4/D7/pn.serve/watchdog probes).

## Index

- [2026-07-24 — PineScript copy/inject + actionable-button capabilities](2026-07-24-pinescript-and-actionable-buttons-research.md)
  — `pine_set_source` sidecar contract; real client-side clipboard via `js_on_click`
  (injection-safe, localhost = secure context); ```pine detection regex; and a full
  actionable-button reference (Button params incl. `loading` spinner / `icon` /
  `description`; `pn.state.notifications` toasts; template modal for destructive confirms)
  for future reconnect / end-session / launch actions.

## Cross-referenced verified findings (recorded in the migration plan, summarized here)

- **Serving:** native `pn.serve(callable)` Tornado, one factory call per session, module
  singletons process-wide; SIGINT returns from `pn.serve` (~2ms), SIGTERM bypasses unless
  translated. `websocket_origin` defaults to `localhost:<port>` only. (migration plan
  "Re-verification COMPLETE 2026-07-24"; probe `docs/probes/pnserve_probe.py`)
- **Thread→session delivery:** `loop.call_soon_threadsafe(partial(chat.send, …))` — the
  proven idiom for pushing into a live session from an OS thread. (plan "D4 RESOLVED";
  `docs/probes/d4_probe.py`)
- **Session destroy:** `pn.state.on_session_destroyed` — sync-only, fires 15–32s after
  disconnect, runs on the event loop (blocking freezes all sessions), `curdoc` is None.
  (plan "D7 RESOLVED"; `docs/probes/probe_d7_server_fixed.py`)
- **Periodic callbacks:** `pn.state.add_periodic_callback(cb, period, start=False)` +
  `pn.state.onload(cb.start)` avoids the held-event double-registration ValueError; the
  callback is session-scoped with automatic cleanup. (plan Task 6.2)
- **Status dots:** `pn.indicators.BooleanStatus(value, color)`. **Buttons:** `label=` /
  `color=` (NOT the deprecated `name=` / `button_type=` for construction, though both
  params exist). **File upload:** standalone `pn.widgets.FileInput` + param watcher (a
  `ChatInterface` `widgets=[FileInput]` gets unpacked before the callback — does NOT work).
  (plan Tasks 6.2 / 8.1)
