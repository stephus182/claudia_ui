# Panel documentation & UI-design research

Single home for all **Panel framework docs, UI-design research, and verified API findings**
gathered during the Chainlit→Panel migration and everything built on it since — the live
dashboard, the chart pane, the UI customisation track. Established 2026-07-24 at the user's
request ("keep all Panel Doc, Panel UI design docs in one documentation folder for future
use"). The deep restyle (Track D, `docs/project-status.md` Known Gap #23) is planned and
executed from here.

**Convention (per CLAUDE.md "API Docs First"):** every claim in these docs is backed by an
authoritative source — the *installed* package (the panel version is the source of truth for
our behaviour), *local* source (sidecars), or an official web doc (scraped via Firecrawl, URL
and scrape date cited). Each reference carries an evidence key or a sources section of its own.
Probe scripts referenced in the docs live in the session scratchpad and, when load-bearing,
are committed under `docs/probes/`.

Distinct from:
- `docs/plans/2026-07-22-panel-migration.md` — the migration execution plan (task-by-task,
  local-only, git-ignored). These references are the *material* it and the restyle work cite,
  not the plan itself.
- `docs/probes/` — runnable verification scripts (D4/D7/`pn.serve`/watchdog), indexed by
  `docs/probes/README.md`.
- `docs/connectivity.md` and `docs/startup-flow.md` — what the action bar's lights *report*
  and how a session comes up; this folder covers how they are *rendered*.

## Where to start

| Question | Open |
|---|---|
| How does ClaudIA serve, build and tear down a session? What is the layout tree? | `panel-reference.md` §1–§5 |
| A widget behaves oddly (buttons, uploads, periodic callbacks, thread delivery) | `panel-reference.md` §6 |
| How do the action bar's buttons and the System log work? | `panel-reference.md` §7–§8, `ui-customisation-reference.md` §2.6 |
| The candlestick chart pane | `panel-reference.md` §9 |
| How to test a Panel surface without a browser, and what proves a served page renders | `panel-reference.md` §10 |
| **What can be styled, and how?** (shadow DOM, designs, themes, tokens, templates) | `ui-design-reference.md` §2–§4 |
| **What is the current look, honestly, and what is proposed for the restyle?** | `ui-design-reference.md` §1 and §8 |
| **Change the theme, avatar, user label, input row, footer buttons** | `ui-customisation-reference.md` §2 |
| What is the next cheap UI change, and what does phase 2 cost? | `ui-customisation-reference.md` §4–§5 |
| Should this be a function, a `Viewer`, a `PyComponent`, or a JS component? | `component-model-reference.md` §6–§9 |
| Which table / indicator / graph component, and what it costs at runtime | `data-surfaces-reference.md` §2–§3 |
| A `Tabulator`, `Number` or notification gotcha | `data-surfaces-reference.md` §8 |

## Index

### Living references — updated in place

Versions described by all five, as of their last revision: **panel 1.9.3**, **bokeh 3.9.2**,
**param 2.4.1**, **pandas 3.0.5**, Python 3.11 (`pip show panel bokeh param pandas` is the
check; site docs were at 1.9.4 when `ui-customisation-reference.md` was last scraped,
2026-09-02, and its §6 records the delta).

- [Panel implementation reference](panel-reference.md) — **how ClaudIA uses Panel today.**
  Serving model (`pn.serve`, `websocket_origin`, SIGTERM translation); the module map
  (`panel_app` · `panel_system_log` · `panel_action_bar` · `panel_sink` · `panel_order_flow` ·
  `panel_chart` · `panel_pinescript` · `message_sink`); session lifecycle (the init gate, the
  `_init_lock` data-integrity rationale, the V4 destroy contract, cleanup); the layout tree;
  the `MessageSink` seam; the widget idioms and gotchas that cost real debugging time; the
  status lights as the **action bar's buttons** and the **System log** (§7–§8, both since
  2026-09-03 — the dots and the "System" chat message with the action buttons are retired, the
  original table is kept for the record); the **HoloViews chart pane** (§9, with the positional
  column-binding trap and the live production case where min-spacing beats median); testing
  without a browser (§10 — the `_get_click_callback` idiom, asserting on a HoloViews chart,
  **what does and does not prove a served app renders**; the per-file test table there was
  counted 2026-08-03 and drifts — `pytest --collect-only -q tests/test_panel_*.py` is the
  current figure, 379 on 2026-09-10); versions and dependencies with an upstream release
  checkpoint (§11). Every claim cites a `file:line`.
- [Panel UI design & styling reference](ui-design-reference.md) — **the styling surface and
  the restyle proposal.** §1 is the honest visual baseline, as a table of what is and is not
  set: still no `.css` file, no `stylesheets=`, no `design=`, no template — but
  `pn.extension("tabulator", notifications=True)` since 2026-08-04, and a **session-scoped**
  `theme` since 2026-09-02 (deliberately not on `pn.extension()`, which would silence the
  `?theme=` override); §2 the **shadow-DOM constraint** that dictates every styling choice (a
  `ChatInterface` message sits at a 7-level shadow-DOM depth, so page-level CSS never reaches it);
  §3 Panel's scraped styling surface (`stylesheets`/`css_classes` and the `raw_css`
  deprecation, the `design` parameter, light/dark themes, the eight `--design-*` tokens,
  templates); §4 `ChatInterface`'s own appearance parameters; §5 the actionable-button
  vocabulary; §6 what is already proven to work in this app; §7 where the current design has
  no answer; §8 the **proposed** Track D direction — *a proposal, not a decision* — ordered by
  cost (inline P&L colour, `pn.extension(design=…)`, one token set shared by chat and chart, a
  template for page chrome, split-vs-tabs, styling inside `ChatInterface` last; §8.2 "label
  the dots" closed 2026-09-03 by the action bar); §9 the official-source URL index (scraped
  2026-07-24, and `docs/api-reference.md` points here rather than duplicating it); §10 what
  is deferred (the chart features that shipped 2026-08-03 are struck through there).
- [UI customisation reference](ui-customisation-reference.md) — **what is set and how to
  change it**: the record of Track D as actually executed, Panel parameters only. §1 what
  phase 1 changed (theme via `CLAUDIA_THEME` + `?theme=`, ClaudIA's avatar, the human's label,
  Send-only footer, no reaction icons, the intro card, the pinned layout), with the
  2026-09-02 and 2026-09-03 live smokes and the 2026-09-04 independent review record; §2 the
  how-tos, §2.6 being the **System log + action bar** (the routing rule for what goes to the
  log and what stays in the chat, the button table, the "+" attachment button deferral); §3
  the executed findings each setting rests on (the theme is session-scoped in `pn.config`;
  the Fast template's theme switch is a page reload, i.e. a new session — never add it as a
  "toggle"); §4 a costed **menu** of the next easy changes; §5 the phase-2 candidates with
  their real cost (page template, buttons under the box, presets, screenshot gallery; the
  pinned layout and the intro card shipped the same evening, the dot labels were superseded by
  the action bar); §6 its own dated source index. Our own code is
  cited by **symbol**, not by line — every line anchor in this doc rotted twice on its first
  day.
- [Panel component model reference](component-model-reference.md) — **how a component is
  built, wired and updated.** Started 2026-08-01. The object taxonomy (widgets / panes /
  indicators / layouts / templates / notifications) and the *real* class hierarchy underneath
  it (an Indicator is a Widget; `ChatInterface` is a list-like Layout), the Param foundation
  and its traps (`value` vs `object`, class-level parameters, the mutable-value trap
  reproduced), the exact 20-parameter `Viewable`/`Layoutable` contract, throttling, the
  **four interactivity APIs ranked** (component-level binding → `pn.bind` → `@param.depends`
  → watchers) with an honest survey of where ClaudIA stands, the functions-vs-classes
  tradeoff, the **four routes to a custom component** — including the probe-verified reason
  `PyComponent` and not `Viewer` is the right base class — `pn.panel()` resolution, layout
  semantics, and why a pane holding a *specification* is cheaper to test than one holding a
  rendering (the chart's `pn.pane.Bokeh` → `pn.pane.HoloViews` move, 2026-08-03). Read this
  before adding any component; it is the doc that prevents an architecture choice made on
  prose. Every section is tagged scraped / probed / code.
- [Panel data surfaces reference](data-surfaces-reference.md) — **graphs, tables, indicators
  and the wiring to drive them.** Started 2026-07-24 as a menu; **substantially updated
  2026-08-04** when the live dashboard shipped the first `Tabulator`, the first `Number`
  tiles, the first `pn.extension()` call and the first notifications, so §1, §3, §7 and §8
  now record shipped behaviour. The component inventory filtered to trading surfaces
  (Tabulator/Perspective, Trend/Number/LinearGauge, the dependency reality — Bokeh and
  ECharts at the time, joined by HoloViews/hvplot 2026-08-03, §1.1 D1), the `pn.extension()`
  gate and its extension-name list, side-window tooling (`FloatPanel`/`Modal`/`GridStack`/
  `Tabs` vs templates vs a second `pn.serve` slug), the connectivity surface (stream/patch,
  periodic callbacks, `pn.bind`, URL sync, reconnect, thread bridge), a **sketch** — not a
  design — for chatbot-piloted vs independently-driven surfaces, and the **gotchas index**
  (§8; 27 entries as of 2026-08-04, those numbered 16 and above measured against a live
  account rather than scraped, including the ledger `BASE` row that reports its own currency
  as the literal string `"BASE"`). Every claim tagged scraped / probed / code / unverified.

### Research — point-in-time, not updated

- [2026-07-24 — External candlestick chart pane (Phase 10)](2026-07-24-candlestick-chart-pane-research.md)
  — Bokeh candlestick via `segment`+`vbar` glyphs; `pn.pane.Bokeh` embed + `pane.object=`
  refresh; OHLCV from `toolkit._cache.load` with fetch-on-miss; symbol/period/bar controls +
  `loading` spinner; side-by-side placement. All APIs verified live that day. **The
  Bokeh-glyph route was superseded 2026-08-03** — the shipped pane renders via
  `pn.pane.HoloViews`/hvplot; see `panel-reference.md` §9 and `data-surfaces-reference.md`
  §1.1 D1. Its §5 deferred-features list is carried forward in `ui-design-reference.md` §10.
- [2026-07-24 — PineScript copy/inject + actionable-button capabilities](2026-07-24-pinescript-and-actionable-buttons-research.md)
  — `pine_set_source` sidecar contract; real client-side clipboard via `js_on_click`
  (injection-safe, localhost = secure context); the ```pine detection regex; and a full
  actionable-button reference (Button params incl. `loading` spinner / `icon` /
  `description`; `pn.state.notifications` toasts; template modal for destructive confirms).
  The "future reconnect / end-session / launch actions" it was written for exist since
  2026-09-03 as the action bar (`panel-reference.md` §8).

## Screenshots — local only

`docs/panel/screenshots/` is **git-ignored** — local + Google Drive, never committed, because
most captures show live account data (same treatment as `docs/plans/`). Reference them from
docs as plain paths, never as markdown links: a link would be broken for anyone who clones
the repo, which is why `ui-design-reference.md` §1 describes the baseline in prose. The
folder's own `README.md` (local too) is the catalog: every file, what it shows, and an honest
"contains account data?" column — register a new capture there before anything else cites it.

What the folder holds, by batch (as of 2026-09-10):

- **2026-07-24 migration smokes** — the unstyled chat-column baseline with the
  `BooleanStatus` dots, the TradingView-offline path, and the Task 8.1 upload fixture.
- **2026-08-06 live dashboard** — cold start (tiles `—`, STALE banner) through to the settled
  KPI strip and the full P&L tab against the real account.
- **2026-08-13 order gates** — Gate 1 (the Touch ID prompt) and Gate 2 (the AppKit dialog,
  BUY banner and the cancel-order variant) captured mid-flow; the `b3/` subfolder holds five
  further frames of the B3 cancel run.
- **2026-09-02 phase 1** — dark and light themes, the intro card loading and settled, the
  pinned layout at full and short window heights.
- **2026-09-03/04 System log + action bar** — the collapsed and expanded card live against
  the gateway, the reconnect sequence, the terminal-style log body, and the `default`-style
  button states on a scratch page.

## Cross-referenced verified findings

Originally recorded in the migration plan and summarized here. **These live in full, with
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
- **Theme is session-scoped** (2026-09-02): set inside the session factory, never on
  `pn.extension()` — Panel reads the global slot first, so a global theme silences `?theme=`.
  The Fast template's built-in switch is a page reload, i.e. a new ClaudIA session.
  (`ui-customisation-reference.md` §3.1)
- **Buttons:** `label=` / `color=` (NOT the deprecated `name=` / `button_type=` for
  construction, though both params exist). `color="light"` is borderless in Bokeh's CSS
  (`border-color: transparent`, verified 2026-09-04), so a neutral button on the white page is
  `"default"`. **Status dots** (retired 2026-09-03 for the action bar's buttons):
  `pn.indicators.BooleanStatus(value, color)` — no `description` parameter on 1.9.3, so no
  tooltip. **File upload:** standalone `pn.widgets.FileInput` + param watcher (a
  `ChatInterface` `widgets=[FileInput]` gets unpacked before the callback — does NOT work).
  (plan Tasks 6.2 / 8.1; `panel-reference.md` §6–§7)
- **Shadow DOM:** every Panel component renders inside its own shadow root and
  `ChatInterface` nests them 7 levels deep, so styling reaches a message only through the
  component's `stylesheets=` parameter, never through page CSS. (`ui-design-reference.md` §2)
