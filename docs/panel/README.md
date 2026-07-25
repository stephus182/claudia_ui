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
- `docs/plans/2026-07-22-panel-migration.md` — the migration execution plan
  (task-by-task). These research docs are the *reference material* it and the restyle plan
  cite, not the plan itself.
- `docs/probes/` — runnable verification scripts (D4/D7/pn.serve/watchdog probes).

## Index

### Living references — updated in place

- [Panel implementation reference](panel-reference.md) — **how ClaudIA uses Panel today.**
  Serving model (`pn.serve`, `websocket_origin`, SIGTERM translation), session lifecycle (the
  init gate, the `_init_lock` data-integrity rationale, the V4 destroy contract), the layout
  tree, the `MessageSink` seam, the widget idioms and gotchas that cost real debugging time,
  status dots, the chart pane, how the 103 Panel tests drive buttons with no browser, and the
  version/dependency state. Every claim cites a `file:line`.
- [Panel UI design & styling reference](ui-design-reference.md) — **the styling surface.** The
  honest visual baseline (there is currently *no* CSS, theme, template or `pn.extension()`
  call anywhere), the shadow-DOM constraint that dictates every styling choice, Panel's
  scraped styling surface (designs, themes, the eight `--design-*` tokens, templates,
  `ChatInterface`'s own appearance parameters), the open design questions, a **proposed**
  direction for the Track D restyle, and the official-source URL index.
- [Panel data surfaces reference](data-surfaces-reference.md) — **graphs, tables, indicators and
  the wiring to drive them.** Started 2026-07-24, before any of this work began: the component
  inventory filtered to trading surfaces (Tabulator/Perspective, Trend/Number/LinearGauge, the
  Bokeh-and-ECharts-only dependency reality), the `pn.extension()` gate and its extension-name
  list, side-window tooling (`FloatPanel`/`Modal`/`GridStack`/`Tabs` vs. templates vs. a second
  `pn.serve` slug), the connectivity surface (stream/patch, periodic callbacks, `pn.bind`, URL
  sync, reconnect, thread bridge), and a **sketch** — not a design — for chatbot-piloted vs.
  independently-driven surfaces. Every claim tagged scraped / probed / code / unverified.

### Research — point-in-time, not updated

- [2026-07-24 — External candlestick chart pane (Phase 10)](2026-07-24-candlestick-chart-pane-research.md)
  — Bokeh candlestick via `segment`+`vbar` glyphs (no hvplot — bokeh already installed);
  `pn.pane.Bokeh` embed + `pane.object=` refresh; OHLCV from `toolkit._cache.load` (parquet,
  DatetimeIndex + lowercase columns) with fetch-on-miss; symbol/period/bar controls +
  `loading` spinner; side-by-side placement. All APIs verified live.
- [2026-07-24 — PineScript copy/inject + actionable-button capabilities](2026-07-24-pinescript-and-actionable-buttons-research.md)
  — `pine_set_source` sidecar contract; real client-side clipboard via `js_on_click`
  (injection-safe, localhost = secure context); ```pine detection regex; and a full
  actionable-button reference (Button params incl. `loading` spinner / `icon` /
  `description`; `pn.state.notifications` toasts; template modal for destructive confirms)
  for future reconnect / end-session / launch actions.

## Migration smoke screenshots — local only

Captured during the migration's live smokes and kept as visual evidence / restyle reference.
They live in `docs/panel/screenshots/`, which is **git-ignored** — local + Google Drive, never
committed (same treatment as `docs/plans/`). Reference them as plain paths, never as markdown
links: a link would be broken for anyone who clones the repo.

- `screenshots/dots-check.png` / `screenshots/dots-full.png` — the `BooleanStatus` connectivity
  dots rendering in a live session (Task 6.2 smoke).
- `screenshots/tv-offline-smoke.png` — the TV-offline path degrading honestly in the live UI.

Because they are not in the repo, `ui-design-reference.md` §1 describes the baseline **in
prose** rather than relying on the image being available.

## Cross-referenced verified findings

Originally recorded in the migration plan and summarized here. **These now live in full, with
`file:line` citations to the code that depends on them, in
[`panel-reference.md`](panel-reference.md)** — start there. The summary is kept for quick
orientation:

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
