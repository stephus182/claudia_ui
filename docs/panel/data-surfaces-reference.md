# Panel data surfaces reference — graphs, tables & indicators

**What Panel offers for ClaudIA's data surfaces: tables, indicators, additional graphs, side
windows, and the wiring that lets the chatbot drive them (or lets them run on their own).**
Living document — started 2026-07-24, **substantially updated 2026-08-04** when the live
dashboard shipped the first `Tabulator`, the first `Number` tiles, the first `pn.extension` call
and the first notifications.

It began as a starting point rather than a design, and that is still true of §4–§6. What has
changed is that §1, §3, §7 and §8 are now records of shipped behaviour rather than a menu — the
traps in §8 numbered 16 and above were all measured against a live account, not scraped.

Companion docs in this folder:
- [`panel-reference.md`](panel-reference.md) — how ClaudIA uses Panel **today** (serving model,
  session lifecycle, the `MessageSink` seam, shipped widget gotchas). Read it first.
- [`ui-design-reference.md`](ui-design-reference.md) — styling surface, the shadow-DOM
  constraint, the restyle proposal. Anything visual belongs there, not here.

Versions described: **panel 1.9.3**, **bokeh 3.9.2**, **pandas 3.0.5**, Python 3.11.

---

## 0. Evidence key

CLAUDE.md's "API Docs First" rule applies. Every claim below carries its evidence level:

| Tag | Meaning |
|---|---|
| **[S]** | **Scraped** from the official Panel docs on **2026-07-24**; URL in §10 |
| **[P]** | **Probed** against the *installed* panel 1.9.3 in `.venv` — the source of truth for our runtime behavior |
| **[C]** | **Code** in this repo, cited `file:line` |
| **[?]** | **Unverified** — a proposal, an open question, or something that needs a live check before it is relied on |

Where a scraped doc and the installed package disagree, the installed package wins and the
disagreement is called out (there is one — §6.5).

---

## 1. Status: what exists today

| Surface | State |
|---|---|
| Candlestick chart | **Shipped.** HoloViews/hvplot (`pn.pane.HoloViews`, superseded 2026-08-03 — §1.1 D1), cache-backed, own Load button, STK only. [`panel_chart.py`](../../claudia/panel_chart.py) **[C]** |
| Status dots | **Shipped.** `pn.indicators.BooleanStatus` ×3 **[C]** |
| Tables | **Shipped 2026-08-04.** One `Tabulator` — the dashboard's positions table: `disabled=True`, `formatters`, `text_align`, `header_filters`, `header_tooltips`, `pagination='local'`, and `.style.map` for signed P&L. [`panel_dashboard.py`](../../claudia/panel_dashboard.py) **[C]** |
| Value indicators | **Shipped 2026-08-04.** Six `pn.indicators.Number` KPI tiles with sign-threshold `colors` **[C]** |
| Second chart | **Shipped 2026-08-04.** Realised-P&L cumulative area + daily bars, a second `pn.pane.HoloViews` **[C]** |
| Notifications | **Shipped 2026-08-04.** `pn.state.notifications` toasts on the fresh↔stale transition only **[C]** |
| Any chatbot-driven surface | **None.** Both the chart pane and the dashboard are deliberately decoupled from the conversation **[C]** |
| `pn.extension(...)` | **Called once, 2026-08-04**, at import time in [`panel_app.py`](../../claudia/panel_app.py): `pn.extension("tabulator", notifications=True)` plus `pn.config.reconnect = True`. `design=`/`theme=` deliberately left to the restyle track **[C]** |

The baseline this document was written against — *one chart, three dots, and no extension call* — held until **2026-08-04**, when the live dashboard landed. §5–§8 below are still largely unbuilt; §1's table is the live inventory, and §7 marks what the dashboard closed.

### 1.1 Decisions taken (2026-07-24)

Unlike §5–§8, these were **settled** — confirmed by the user after reviewing §2.1's dependency
findings — and were not to be re-litigated without new evidence. **D1 was re-litigated and
reversed on 2026-08-03** (below); D2 and D3 still stand, unchanged.

**D1 — SUPERSEDED 2026-08-03. HoloViews is the chart engine.**
D1 originally read *"Bokeh is the chart engine. No new charting dependency."* It was
reversed on 2026-08-03.

Read the reversal honestly: **D1's own stated trigger — "repeating the same glyph
scaffolding for a third and fourth chart" — never fired.** There was still one chart. The
actual grounds were (a) the `[?]` below resolving in HoloViews' favour, (b) hvplot's
`.ohlc()` proving immune by construction to the `vbar`-width bug fixed here by hand in
`a51b454`, (c) a measured dependency cost of three pure-Python packages, and (d) a change
of objective — live-streaming P&L surfaces and linked views, where hand-built Bokeh is
expensive.

What D1 got right and still stands: nothing then-wanted was out of Bokeh's reach, and the
trade was verbosity rather than capability. That remained true right up to the reversal.

**RESOLVED 2026-08-03 [P]:** `hvplot.ohlc()` exists natively, returning an `Overlay` of
`Segments` (wicks) + `Rectangles` (bodies) — the same two primitives that were assembled by
hand here. Body width is `np.min(np.diff(x)) * bar_width`: at hvplot's own default
(`bar_width=0.5` — confirmed in both the installed docstring and the live
[hvPlot reference](https://hvplot.holoviz.org/ref/api/manual/hvplot.hvPlot.ohlc.html)), that
measures out to exactly **0.5x** the minimum bar spacing, stable across 1D/1h/30min/5min
fixtures and a weekend-gap fixture alike (verified 2026-08-03) — so the smear bug cannot
recur at any timeframe. **This project does not use that default**: `panel_chart.py` passes
`bar_width=0.7` (`_BODY_WIDTH_FRACTION`), so 0.7x — not 0.5x — is what actually ships; the
0.5x figure above is hvplot's own default, verified in isolation to confirm the mechanism,
not this project's behavior.
⚠ Column binding, not width, is the real trap here — and it is not undocumented, just easy
to miss: the bare `y` parameter entry (`Default is ["open", "high", "low", "close"]`)
reads as name-based in isolation, but the same reference page spells out the actual
mechanism a few lines below — "the first four non-datetime columns correspond to O, H, L,
C" — which matches `hvplot/converter.py`'s source exactly: when `y` is omitted, columns
bind **positionally**, not by matching column names against that default list.
`claudia/panel_chart.py` passes `y=` explicitly because nothing else pins the cache
DataFrame's column order (`test_build_chart_object_is_column_order_independent` covers it).

**D2 — Rendering lives in `claudia_ui`; computation stays in `ibkr_core_mcp`.**
The split already exists in our own code: indicator *computation* is `ibkr_core_mcp/indicators.py`,
indicator *rendering* is `panel_chart.py`. Any charting library, if one is ever added, goes in this
repo. Three reasons: `ibkr_core_mcp` is usable standalone and should not make data consumers pay
for a presentation library; pushing rendering down reintroduces exactly the coupling the
`MessageSink` seam was built to remove; and a mis-rendered chart should be fixable in the repo that
owns the UI, without a cross-repo change.

**D3 — One scenario is explicitly carved out for later: chart → PNG → ClaudIA's own vision.**
Rendering a chart to a static image *so the model can look at it* is a genuinely different use case
from displaying it to the user, and it is the one case where a second library (matplotlib) would be
weighed on its own merits rather than against D1. **Not scheduled, not designed** — recorded so the
idea is not lost and so it is not confused with the display path.

**Follow-up, independent of all three — CLOSED 2026-07-24:** `bokeh` is imported directly at
[`panel_chart.py:36`](../../claudia/panel_chart.py#L36) but was declared nowhere in
`pyproject.toml`, resolving only because Panel depends on it. Now declared as `bokeh>=3.8`
([`pyproject.toml:12-23`](../../pyproject.toml#L12-L23)), with no upper bound here so panel
remains the single place that caps it. **Floor raised 3.7 → 3.8 on 2026-07-31:** panel 1.9.3's
metadata still says `bokeh<3.10,>=3.7.0`, but panel 1.9.0's release notes drop support for
Bokeh 3.7, and `pn.config.reconnect` (§ below) needs ≥3.8 regardless. The same release-notes
read added `pandas>=2.2` — another direct import of `panel_chart.py` that was declared nowhere.
Full record: `panel-reference.md` §11 "Upstream release checkpoint".

---

## 2. Component inventory

Panel 1.9.3 ships 37 panes, 63 widgets, 15 layouts, 10 indicators, 9 templates and 6 chat
components **[S]**. Only the ones relevant to trading data surfaces are listed here.

### 2.1 Graphs

| Component | Extra Python dep? | Installed here? | Notes |
|---|---|---|---|
| `pn.pane.Bokeh` | declared directly (`bokeh>=3.8`), also a panel dependency | **yes** — bokeh 3.9.2 **[P]** | Full control, most code. No longer what the candlestick pane uses (superseded 2026-08-03 — §1.1 D1) |
| `pn.pane.ECharts` | none for **raw dict** specs; `pyecharts` only if you pass pyecharts objects **[S]** | echarts JS **is bundled** with panel **[P]**; `pyecharts` **not installed** **[P]** | Accepts an ECharts spec as a plain dict. Params: `object`, `options`, `renderer` (`canvas`/`svg`), `theme` (`default`/`dark`/`light`) **[S]** |
| `pn.pane.Plotly` | `plotly` | **not installed** **[P]** | Constructing the empty pane works; rendering a figure needs the package |
| `pn.pane.HoloViews` | `holoviews` (+ `hvplot` for the DataFrame API) | **yes** — holoviews 1.23.1 + hvplot 0.12.2 **[P]**, added 2026-08-03 | The high-level route, and now what the candlestick pane uses (§1.1 D1) |
| `pn.pane.Matplotlib` | `matplotlib` | **not installed** **[P]** | Static images |
| `pn.pane.Vega` | `altair` for specs | **not installed** **[P]** | |

**The practical consequence, as it stood on 2026-07-24 (before D1 was superseded):** at that
time, adding a second chart type cost zero new Python dependencies only if it was Bokeh or
ECharts; everything else, including the HoloViews route eventually taken, added a dependency to
`pyproject.toml`. The "Installed here?" column above reflects *today's* actual state, not that
snapshot — read the two together to see what changed.

→ **This table informed D1 (§1.1) as originally decided.** D1 itself is now superseded; the
table is kept as the record of what was known at decision time, not as a live menu.

⚠ Note that ECharts' *JavaScript* being bundled is not the same as a Python dependency: the
bundle ships inside the installed panel package (`panel/dist/bundled/echarts/echarts@6.0.0`)
**[P]**, so no `pip install` and no CDN fetch is needed — but `pn.extension('echarts')` is (§3).

### 2.2 Tables

**`pn.widgets.Tabulator` is the one to use.** It is a widget, not a pane — the data round-trips
to Python.

Core parameters **[S]**: `value` (DataFrame), `formatters`, `editors`, `editables`, `filters`,
`header_filters`, `hidden_columns`, `frozen_columns` / `frozen_rows`, `groupby`, `sorters`,
`sortable`, `pagination` (`'local'`/`'remote'`), `page_size`, `initial_page_size`, `selection`,
`selectable` (`True`/`False`/`'checkbox'`/`'checkbox-single'`/`'toggle'`/`int`),
`selectable_rows` (callable), `row_content` (callable → expandable detail row, may be async),
`buttons`, `text_align` / `header_align`, `header_tooltips`, `theme`, `configuration`,
`layout` (`'fit_data_table'` default).

Verified methods and callbacks **[P]** (all present on 1.9.3):

| API | Purpose |
|---|---|
| `.stream(df, rollover=…, follow=…)` | Append rows, transmitting only the new data **[S]** |
| `.patch({col: [(idx, value), …]}, as_index=…)` | Update existing cells in place **[S]** |
| `.on_click(cb)` | `CellClickEvent` with `.column`, `.row`, `.value` **[S]** |
| `.on_edit(cb)` | `TableEditEvent` with `.column`, `.row`, `.value`, `.old` **[S]** |
| `.add_filter(...)` / `.remove_filter(...)` | Static or widget-bound filters **[S]** |
| `.download(...)` / `.download_menu()` | Client-side CSV/JSON export **[S]** |
| `.style` | A **pandas `Styler`** — per-cell conditional formatting **[P]** |
| `.current_view` / `.selected_dataframe` | Post-filter view / selected rows as DataFrames **[S]** |

**`.style` is the direct answer to the P&L-color gap for tabular data.** The official streaming
example does exactly this **[S]**:

```python
def color_negative_red(val):
    return 'color: %s' % ('red' if val < 0 else 'green')

tabulator.style.map(color_negative_red)
```

⚠ On **pandas 3.0.5** the old `Styler.applymap` is gone — `.map` is the API **[P]**. The scraped
example already uses `.map`, so it is correct as written.

**`pn.pane.Perspective`** — a pivot/analytics grid (group_by, split_by, aggregates, expressions,
filters, its own `.stream`/`.patch` **[P]**). No Python package needed; the JS is bundled **[P]**.
Heavier and more opinionated than Tabulator; the right choice only if genuine pivoting is wanted.

**`pn.pane.DataFrame` / `pn.widgets.DataFrame`** — the plain HTML render and the legacy Bokeh
`DataTable`. Fine for a static dump, no streaming/patching story. Prefer Tabulator.

### 2.3 Indicators

All 10 confirmed present as `pn.indicators.*` on 1.9.3 **[P]**: `BooleanStatus`, `Dial`, `Gauge`,
`LinearGauge`, `LoadingSpinner`, `Number`, `Progress`, `TooltipIcon`, `Tqdm`, `Trend`.

| Indicator | Key params **[S]** | Fit for ClaudIA |
|---|---|---|
| `Trend` | `data` (dict of arrays or DataFrame), `plot_x`/`plot_y`, `plot_type` (`line`/`bar`/`step`/`area`), `plot_color`, `pos_color` (default `#5cb85c`), `neg_color` (default `#d9534f`), `value`, `value_change`, `layout` (`column`/`row`) | **The best fit.** Value + sparkline + signed change, and it has **`.stream(data, rollover=…)`** **[P]**. `value`/`value_change` default to `'auto'` — computed from the data |
| `Number` | `value`, `format` (`'{value}'`), `colors` as `[(threshold, color), …]`, `default_color`, `font_size`, `title_size`, `nan_format` | KPI tile; thresholds give colour for free |
| `LinearGauge` | `bounds`, `value`, `format`, `colors` (fractions or list), `show_boundaries`, `needle_color`, `unfilled_color`, `horizontal` | Compact gauge — e.g. utilization against a limit |
| `Gauge` / `Dial` | radial equivalents | Bigger, more decorative |
| `BooleanStatus` | `value`, `color` | Already shipped as the connectivity dots **[C]** |
| `Progress` / `LoadingSpinner` / `Tqdm` | | Long-task feedback; `Button.loading` already covers the shipped cases **[C]** |
| `TooltipIcon` | `value` (tooltip text) | A candidate fix for the unlabelled-dots gap (`ui-design-reference.md` §8.2) **[?]** |

`Number`, `LinearGauge` and `Trend` all construct without any extension call **[P]**.

---

## 3. `pn.extension()` — the gate

**ClaudIA calls `pn.extension()` exactly once**, at import time in `claudia/panel_app.py`
**[C]** — added 2026-08-04 with the dashboard, which needs `tabulator`:

```python
pn.extension("tabulator", notifications=True)
pn.config.reconnect = True
```

`notifications=True` is not cosmetic: `pn.state.notifications` is `None` without it, so
`reconnect` has no way to tell the user anything, and the dashboard's stale toast would be a
silent no-op. This resolves open question 3 (§9) and question 5 — `reconnect` is now on.

Everything that shipped before it (Bokeh panes, buttons, `BooleanStatus`, `ChatInterface`,
`pn.pane.HoloViews`) still needs no extension call, so the ordering constraint is the only
thing that changed: the call must run before any session Document exists, which is why it sits
at module import rather than inside the session factory.

Two independent name sources, both enumerated from the installed package **[P]**:

**From `pn.extension._imports` (18):** `ace`, `codeeditor`, `deckgl`, `echarts`, `filedropper`,
`ipywidgets`, `jsoneditor`, `katex`, `mathjax`, `modal`, `perspective`, `plotly`, `tabulator`,
`terminal`, `texteditor`, `vega`, `vizzu`, `vtk`.

**From `ReactiveHTML` subclasses (3):** `floatpanel`, `gridstack`, `notifications`.

So the surfaces in this doc that require an extension argument are:
**`tabulator`**, **`perspective`**, **`echarts`**, **`plotly`**, **`modal`**, **`floatpanel`**,
**`gridstack`**, **`notifications`**.

Three things to know before adding the call:

1. **An unrecognized name does not raise** — it logs `'<name> extension not recognized and will
   be skipped'` via `param.warning` (`panel/config.py:834`) **[P]**. A typo therefore produces a
   silently non-functional component plus one extra warning in the test output. Assert on the
   loaded extensions rather than trusting the string.
2. **The JS is served locally, not from a CDN.** Panel's resource mode is `'server'` and bundled
   assets resolve to `static/extensions/panel/…` on our own Tornado server; every extension named
   above has a real bundle directory inside the installed package **[P]**
   (`panel/io/resources.py:113,115` + `bundled_files()`). A resource with no local bundle falls
   back to its CDN URL — so anything added via `css_files=[…]` (for example the font-awesome
   stylesheet the Tabulator `buttons` feature needs **[S]**) *is* an external fetch and will fail
   on a disconnected machine.
3. **`pn.extension(...)` is also where `design=`, `theme=`, `notifications=True`,
   `defer_load=True` and `reconnect=True` are set** — i.e. this one call is shared with the Track
   D restyle decisions (`ui-design-reference.md` §8.3). Adding it for a table should not silently
   pre-empt those; keep the call in one place and change it deliberately.

---

## 4. Side windows & surface tooling

Three genuinely different meanings of "side window". They have different costs and different
state semantics — do not conflate them.

### 4.1 In-page containers (same session, same page)

| Component | Ext.? | What it gives |
|---|---|---|
| `pn.Tabs` | no | `active` index (settable from Python), `dynamic=True` renders only the active tab, `closable`, `tabs_location` (`above`/`below`/`left`/`right`) **[S]** |
| `pn.Card` | no | Collapsible titled section — `collapsed`, `collapsible`, `header`, `title`, `hide_header` **[S]**. The natural unit for "one indicator group" |
| `pn.Accordion` | no | Several cards, one open at a time **[S]** |
| `pn.layout.GridStack` | **`gridstack`** | **User-draggable, user-resizable** dashboard grid — `allow_drag`, `allow_resize`, `ncols`/`nrows`, 2D-slice assignment API **[S]** |
| `pn.layout.FloatPanel` | **`floatpanel`** | Draggable floating window (jsPanel). `contained=False` → free-floating; `position`, `offsetx/y`, `theme`, `status` (`normalized`/`maximized`/`minimized`/`smallified`/`closed`), `config` passthrough to jsPanel **[S]**. This is the closest thing to a real "side window" without leaving the page |
| `pn.layout.Modal` | **`modal`** | Params `open`, `show_close_button`, `background_close`, `scroll`; methods `.show()`, `.hide()`, `.toggle()`, `.create_button()` **[P]** |
| `pn.Swipe` | no | Before/after comparison of two components with a draggable divider **[S]** — e.g. two chart states |
| `pn.pane.Placeholder` | no | A swappable slot: `.object = …`, `.update(…)`, or use as a context manager for temporary content **[S]**. **The clean primitive for a chatbot-driven surface** — the container stays in the layout tree while its contents are replaced wholesale |

### 4.2 Page chrome (templates)

Templates supply `header`, `sidebar`, `main`, `right_sidebar` and `modal` areas — all five
confirmed on `FastListTemplate` in 1.9.3 **[P]**, `right_sidebar` included **[S]**. ClaudIA uses
no template today **[C]**. This is Track D territory (`ui-design-reference.md` §8.5); noted here
only because a `right_sidebar` is the obvious home for indicator tiles.

⚠ Template area contents are **fixed once rendered** — to change them later you must insert a
regular Panel layout (e.g. a `Column` or `Placeholder`) and mutate *that* **[S]**.

### 4.3 Genuinely separate windows (separate sessions)

`pn.serve` accepts a **dict of slug → app**, verified in the installed signature
(`panels: TViewableFuncOrPath | dict[str, TViewableFuncOrPath]`) **[P]**:

```python
pn.serve({'claudia': _build_session_root, 'charts': _build_charts_root},
         title={'claudia': 'ClaudIA', 'charts': 'ClaudIA — Charts'})
```

This is how a chart/table window opens in its **own browser tab** at its own URL **[S]**.

Two consequences that matter more than the syntax:

- **Serve a *function*, never an object.** A served object is shared across sessions, so one
  user's widget change mutates everyone's **[S]**. ClaudIA already serves a callable **[C]** —
  keep that.
- **A separate tab is a separate session.** It shares no param state, no `ChatInterface`, and its
  own `on_session_destroyed`. Anything shared has to go through a process-wide channel:
  a module singleton (ClaudIA's existing pattern **[C]**), `pn.state.cache`, or the SQLite store.
  ⚠ `pn.state.cache` is process-wide, so the `_init_lock` data-integrity reasoning in
  [`panel_app.py:751-761`](../../claudia/panel_app.py#L751-L761) applies to it too — a second
  session's writes must not race the GDrive DB swap.

Also available on the same server, if a surface should be reachable outside a Panel session:
custom Tornado `RequestHandler` routes via a `ROUTES` list **[S]**, and `static_dirs=` for files
(neither is used today **[C]**).

---

## 5. Connectivity — getting data in and updates out

### 5.1 The update matrix

Three ways to push new data into a live surface, cheapest last:

| Mechanism | Applies to | Cost |
|---|---|---|
| **Replace** — `pane.object = new_figure` | Any pane; what the chart pane does today ([`panel_chart.py:163`](../../claudia/panel_chart.py#L163)) **[C]** | Whole object re-serialized |
| **Stream** — `.stream(data, rollover=…, follow=…)` | `Tabulator` **[P]**, `Perspective` **[P]**, `Trend` **[P]**, bokeh `ColumnDataSource` **[P]** | Only new rows on the wire **[S]** |
| **Patch** — `.patch({col: [(idx, val)]})` | `Tabulator` **[P]**, `Perspective` **[P]**, `ColumnDataSource` **[P]** | Only changed cells |

⚠ **`Tabulator.patch()` does not trigger a `value` parameter update** — fire
`widget.param.trigger('value')` yourself if something is watching it **[S]**.
⚠ `patch(as_index=True)` is the default (locate rows by DataFrame index); pass `as_index=False`
for positional indexes **[S]**.

### 5.2 Periodic refresh

`pn.state.add_periodic_callback(cb, period, count=…, timeout=…)` returns a `PeriodicCallback`
with `.start()`, `.stop()`, `.counter`, and a `running` param that can be linked to a Toggle
**[S]**. It is session-scoped and auto-cleaned.

**This repo already has the non-obvious part solved:** register with `start=False` and start it
from `pn.state.onload`, or a held-event replay raises a per-session bokeh `ValueError`
(`panel-reference.md` §6; [`panel_app.py:937-945`](../../claudia/panel_app.py#L937-L945)) **[C]**.
The official docs make the same point differently — with `pn.serve` you cannot start a periodic
callback before the app is served, so it must be created inside the app function **[S]**.

Every official streaming example — Bokeh, Tabulator, Perspective, `Trend` — is
`add_periodic_callback` + `.stream(...)` **[S]**. That is the sanctioned pattern; there is no
separate streaming API to learn.

### 5.3 Push from outside the event loop

Already established and verified in this repo: `loop.call_soon_threadsafe(partial(...))` is the
thread → session bridge ([`panel_app.py:804-816`](../../claudia/panel_app.py#L804-L816), probe
`docs/probes/d4_probe.py`) **[C]**. Any surface fed by `ExecutionListener`'s WebSocket or by a
watchdog thread must go through it. `pn.state.execute` exists for scheduling onto the loop **[P]**.

And the standing rule: **blocking work goes through `asyncio.to_thread`** — blocking the shared
loop freezes every session (`panel-reference.md` §3.3, §6) **[C]**.

### 5.4 Binding UI → data

- `pn.bind(fn, widget)` / `@pn.depends` — reactive re-render on param change **[S]**
- `param.watch(cb, 'value')` — low-level; **this is how the test suite drives buttons headlessly**
  (`tests/conftest.py`, `panel-reference.md` §10) **[C]**
- `.link(target, bidirectional=True, value='running')` — high-level Python link **[S]**
- `.jslink(...)` / `.jscallback(...)` — **client-side**, no server round-trip **[S]**. Related:
  the shipped `js_on_click` clipboard write, which is injection-safe because bokeh serializes args
  into JS variables rather than concatenating ([`panel_pinescript.py:105-107`](../../claudia/panel_pinescript.py#L105-L107)) **[C]**
- `pn.cache` decorator — memoize by inputs with `max_items`, `policy` (`LRU`/`LFU`/`FIFO`), `ttl`
  **[S]**. Relevant if a table re-derives from the same parquet cache the chart pane reads

### 5.5 Deep-linking a surface via the URL

`pn.state.location` exposes `pathname`, `search`, `hash`, `href`, `protocol`, `port`, `hostname`,
`reload`, plus `.sync(parameterized, {…})`, `.unsync(...)` and `.update_query(...)` **[P]**.
`sync` binds a `Parameterized` object's params to query parameters, so
`?symbol=AAPL&bar=1d` can restore a chart's state **[S]**.

⚠ **Doc/package discrepancy:** the how-to calls the parameter **`hash_`** **[S]**; the installed
1.9.3 `Location` class exposes it as **`hash`** **[P]**. Use `hash`.
⚠ `reload=True` is for independent apps, `False` for single-page apps **[S]** — for a separate
chart tab (§4.3) that distinction is a real choice, not a detail.

### 5.6 Session and connection state

| API | Verified | Use |
|---|---|---|
| `pn.state.on_session_created(cb)` | **[P]** | **Global only** — must be registered before the server starts (in `pn.serve`'s caller for us) **[S]** |
| `pn.state.on_session_destroyed(cb)` | **[P]** | Already used, with a four-property contract (`panel-reference.md` §3.3) **[C]** |
| `pn.state.busy` / `pn.state.sync_busy(indicator)` | **[P]** | Global "server is working" signal; `sync_busy` drives a `BooleanIndicator`'s `value` with no manual `pn.bind` **[S]** |
| `pn.state.cache` | **[P]** | Process-wide dict — see the §4.3 warning |
| `pn.state.session_args` | **[P]** | Query args at session start |
| `pn.config.reconnect` | **[P]** (default `False`) | **Automatic WebSocket reconnect** with exponential backoff at 1/2/4/8/16/32 s, or `"prompt"` for user-initiated. Requires panel ≥1.8 + bokeh ≥3.8 **[S]** — we are on 1.9.3/3.9.2 **[P]**, so it is available and currently off |
| `pn.config.defer_load` | **[P]** (default `False`) | Defers bound functions until after first render, so a slow surface does not delay the page **[S]** |

`reconnect` needs `notifications=True` to show its messages **[S]**, and `pn.state.notifications`
is `None` unless notifications are enabled (already recorded in the buttons research).

---

## 6. Piloting a surface from the chatbot — design sketch **[?]**

**Nothing here is built or agreed.** It is written down so the options are on the table with their
constraints attached.

The shipped architecture already contains the right seam. `agent.py` imports **no Panel symbol**
and touches the sink at exactly six call sites (`panel-reference.md` §5) **[C]**, and there are
two working precedents for UI actions that originate in the model's output:

- **Order proposals** — the agent calls a strict-schema `propose_*` tool, and `agent.py` hands
  the validated `tool_use.input` to `MessageSink.send_order_proposal` **[C]**
- **PineScript blocks** — detection lives **entirely in the sink** (`panel_sink.py:94-102`), so
  `agent.py` was not modified at all **[C]**

### Option A — sink-side detection of a fenced spec block *(recommended starting point)*

The agent emits e.g. a ```` ```chart-spec ```` / ```` ```table-spec ```` block; `PanelMessageSink`
detects it exactly as it detects ```` ```pine ````, validates it, and routes it to a surface
controller that owns a `pn.pane.Placeholder` (§4.1). **`agent.py` stays untouched**, and the
existing 10 sink tests extend naturally.

### Option B — extend the `MessageSink` protocol

Add `render_table(spec)` / `render_chart(spec)` to
[`message_sink.py`](../../claudia/message_sink.py). Cleaner typing and an explicit contract, but
it changes the protocol *and* `agent.py`'s emission path — a bigger blast radius on a
safety-critical file.

### Option C — a UI-local display tool merged into `tools=`

Same mechanism the TradingView sidecar tools use. Not forbidden by the Hard Rules (those name
`place_order`/`modify_order`/`cancel_order`/`reply_order`), but it puts surface control inside the
model's tool loop, which is harder to constrain and to test than a rendered block.

**Constraints that apply to all three:**

1. A rendered surface must **never become an order path**. Hard Rule 1 stands: staging is a
   physical button click through `order_flow.py`'s gates. A Tabulator `on_click` or `on_edit`
   handler must not reach an order function.
2. **Order parameters are immutable** — a surface that displays proposed orders must show the
   user's exact values, with no rounding for display that could be mistaken for the real value.
3. `Tabulator` is **editable by default** — `disabled=True` (or an explicit `editors` map) unless
   editing is genuinely wanted **[S]**.
4. Whatever the model emits is **untrusted input**. Validate the spec; never pass model-authored
   strings into `configuration`, `js_on_click` code, or HTML that reaches the page unescaped.

### Independent mode

Already demonstrated: the chart pane's Load button owns its own fetch and never consults the
conversation ([`panel_chart.py:130`](../../claudia/panel_chart.py#L130)) **[C]**. Any new surface
should keep that dual capability — user-drivable *and* chatbot-drivable — rather than depending on
the agent to function.

---

## 7. Candidate surfaces for ClaudIA **[?]**

1–2 **SHIPPED 2026-08-04** (live dashboard); 3–5 still unbuilt:

1. **Positions table** — ✅ **shipped.** `Tabulator` + `.style.map` for signed P&L, in
   `claudia/panel_dashboard.py`. ⚠ The blocker recorded here was *"the data arrives as
   pre-formatted text from ibkr_core_mcp"* — **that turned out to be a property of
   `ClaudeToolkit.execute()`, not of the data.** `toolkit.client.get_positions()` /
   `get_account_ledger()` return structured dicts; only `execute()` renders markdown. So the
   answer was to read the client directly, and open question 1 (§9) is closed without any
   change in the other repo. Two traps found doing it: `get_positions` **pages at 30** and page 0
   alone silently shows a partial book, and the ledger's `BASE` row reports its `currency` as the
   literal `"BASE"` (§8, gotcha 16).
2. **Account/P&L tiles** — ✅ **shipped**, as six `Number` tiles with sign-threshold `colors`.
   `Number.colors` is scanned in **reverse** and the last match wins, so the earliest threshold a
   value satisfies is the colour applied (`Number._process_param_change`, read 2026-08-04); the
   comparison is `value <= threshold`, so a dead band around zero is needed or a flat P&L renders
   red. `Trend` was considered and **not** used: its `value_change` badge is a percentage, and
   P&L has no honest denominator.
3. **Live execution feed** — `Tabulator.stream(...)` fed from `ExecutionListener` through the
   verified `call_soon_threadsafe` bridge (§5.3). Still unbuilt; the dashboard polls at 15s
   instead, which is sufficient for account state but not for a fill-by-fill tape.
4. **Indicator overlays on the candlestick** — **volume subplot, MA (SMA) overlay, and zoom sync
   between the two rows shipped 2026-08-03** (`panel_chart.py`, via the HoloViews/hvplot engine —
   §1.1 D1 — not the "pure Bokeh, no new dependency" originally planned here). Hover tooltips come
   along by default from hvplot's own tool set (verified 2026-08-03: `HoverTool` is on the
   rendered figure's toolbar with no code in this module asking for it). **Crosshair is the one
   piece of the original list still unbuilt.**
5. **A separate charts window** — `pn.serve({...})` (§4.3), if charts should live in their own tab
   beside TradingView.

---

## 8. Gotchas index

Every item is scraped or probed, not inferred:

| # | Gotcha | Tag |
|---|---|---|
| 1 | `pn.extension('typo')` warns and skips — it does not raise (`panel/config.py:834`) | **[P]** |
| 2 | Extension JS is bundled and served locally; only non-bundled resources (e.g. font-awesome via `css_files`) hit a CDN | **[P]/[S]** |
| 3 | `Tabulator.patch()` does not trigger a `value` update — `param.trigger('value')` manually | **[S]** |
| 4 | `Tabulator.configuration` is **not responsive** — instantiation-time only | **[S]** |
| 5 | `Tabulator` cells are **editable by default**; set `disabled=True` | **[S]** |
| 6 | `Tabulator` `buttons` icons need an external font-awesome stylesheet in a server context | **[S]** |
| 7 | pandas 3.0.5 removed `Styler.applymap` — use `.style.map` | **[P]** |
| 8 | `Location.hash_` in the docs is `Location.hash` in the installed package | **[S] vs [P]** |
| 9 | `pn.serve` a **function**, not an object, or all sessions share state | **[S]** |
| 10 | A second served app = a second session: no shared param state, only process-wide channels | **[S]** |
| 11 | Template area contents are fixed once rendered — nest a mutable layout | **[S]** |
| 12 | Periodic callbacks: `start=False` + `pn.state.onload`, else a double-registration `ValueError` | **[C]** |
| 13 | `pn.state.on_session_created` is **global-only** — cannot be registered from inside a session | **[S]** |
| 14 | `plotly` / `matplotlib` / `pyecharts` are **not installed**; `holoviews` and `hvplot` **now are** (added 2026-08-03 — §1.1 D1) — Bokeh, ECharts, and HoloViews/hvplot are the currently-available chart routes | **[P]** |
| 15 | Nothing inside `ChatInterface` is reachable by page-level CSS (7 shadow roots deep) — see `ui-design-reference.md` §2 | **[C]** |
| 16 | The ledger's `BASE` row reports its own `currency` as the literal string **`"BASE"`**, not the base currency's code — IBKR's docs read the other way. Take the base currency from `/portfolio/accounts` instead | **[P]** live 2026-08-04 |
| 17 | `Number.colors` is scanned in **reverse**, last match wins, comparison is `value <= threshold` — so an exactly-zero P&L renders in the negative colour without a dead band | **[P]** |
| 18 | `Number` renders `nan_format` **inside** the format string, so a signed money format yields the tile `"+— USD"` for a `None` value. Drop the format when the value is absent | **[P]** |
| 19 | `Tabulator.style` is a real pandas `Styler` that **survives `value` reassignment** and rebinds to the new frame — apply `.style.map` once at build time, not on every repaint | **[P]** |
| 20 | `pn.state.notifications` is a read-only **property**; to fake it in a test, patch the attribute on `type(pn.state)`, not on the instance | **[P]** |
| 21 | `pn.Tabs(dynamic=True)` renders only the active tab, but widget **identity survives deactivation** and params set on a hidden tab's widget are present when it is shown again — a repaint can write to all tabs unconditionally | **[P]** |
| 22 | A **single-point** time series renders with a millisecond-scale x-axis (`0ms / 500ms / 0ms`) under a full-width bar. Two points is a hard floor for any dated chart | **[P]** live 2026-08-04 |
| 23 | `pn.extension._loaded_extensions` is the only proof an extension took effect — assert on it, never on the argument string (see gotcha 1) | **[P]** |
| 24 | Ledger `realizedpnl` has no documented window, but it is **exactly** the sum of the per-position `realizedPnl`, which IBKR *does* document — *"the total profit made today through trades"*. When an endpoint won't define a field, look for another endpoint that reports the same number | **[P]** live 2026-08-04 |
| 25 | The positions endpoint's `avgCost` is **not** the FIFO purchase average — it carries earlier closes' disallowed losses (GLD: 380.3654 on FIFO vs **383.270899** reported, the difference being 290.55 over 100 replacement shares to six decimals). So a P&L derived from it is a different quantity from `flex_trade.fifo_pnl_realized` and the two must never be added | **[P]** live 2026-08-04 |
| 26 | `avgCost` is per **contract**, `avgPrice` per **unit**. A price column must use `avgPrice`, or a futures row shows an entry three orders of magnitude off the last price in the next column (CL: 80,932.36 beside a last of 75.14) | **[P]** live 2026-08-04 |
| 27 | The positions endpoint serves **lean rows** on some polls: the same CL SEP2026 came back with no `ticker` and no `multiplier`, then complete minutes later. Never default a missing `multiplier` to 1 — derive it as `avgCost / avgPrice` and publish nothing if you cannot; the default put +0.00472 on screen where the answer was +4.72 | **[P]** live 2026-08-04 |

---

## 9. Open questions

1. ~~**Where does structured position/P&L data come from?**~~ **CLOSED 2026-08-04.** It was never
   an ibkr_core_mcp question: `ClaudeToolkit.execute()` renders markdown, but `toolkit.client.*`
   returns structured dicts. Read the client. See §7.1.
2. **In-page or separate window?** `GridStack`/`FloatPanel` versus a second `pn.serve` slug — a
   real fork with different state semantics (§4). **Still open**, but no longer blocking: the
   dashboard's components are standalone factories, so re-parenting is a layout change.
3. ~~**Who owns the `pn.extension(...)` call?**~~ **CLOSED 2026-08-04.** `claudia/panel_app.py`,
   at module import, and it is the only one. `design`/`theme` were deliberately left out so the
   restyle track still owns them (§3).
4. **Chatbot-driven: block detection or protocol extension?** (§6 A vs B). Still open — the
   dashboard is user-driven and polls on its own, which was the deliberate first increment.
5. ~~**Should `reconnect=True` be turned on regardless?**~~ **CLOSED 2026-08-04** — on, with
   `notifications=True` alongside it, since `reconnect` cannot surface anything without it.
6. **How is a data surface tested headlessly?** **Partly answered.** A `Tabulator`'s server-side
   state is fully assertable — `value`, `disabled`, `formatters`, `text_align`,
   `_on_click_callbacks`/`_on_edit_callbacks`, and `style.ctx` after `style._compute()` (which is
   how the P&L colouring is tested). `stream`/`patch` round-trips are still unexercised here: the
   dashboard replaces `value` wholesale, because the position book is small and a full replace
   cannot drift from the source the way an incremental patch can.

---

## 10. Sources

Scraped **2026-07-24** via Firecrawl; each URL returned content on that date. Complements — does
not duplicate — the styling/template URL index in `ui-design-reference.md` §9.

| Topic | URL |
|---|---|
| Component gallery (all 37 panes / 63 widgets / 15 layouts / 10 indicators) | https://panel.holoviz.org/reference/index.html |
| Tabulator (params, formatters, editors, selection, filtering, streaming, patching, configuration) | https://panel.holoviz.org/reference/widgets/Tabulator.html |
| Perspective | https://panel.holoviz.org/reference/panes/Perspective.html |
| DataFrame pane | https://panel.holoviz.org/reference/panes/DataFrame.html |
| Bokeh pane | https://panel.holoviz.org/reference/panes/Bokeh.html |
| ECharts pane | https://panel.holoviz.org/reference/panes/ECharts.html |
| Plotly pane | https://panel.holoviz.org/reference/panes/Plotly.html |
| HoloViews pane | https://panel.holoviz.org/reference/panes/HoloViews.html |
| Placeholder pane | https://panel.holoviz.org/reference/panes/Placeholder.html |
| Trend indicator | https://panel.holoviz.org/reference/indicators/Trend.html |
| Number indicator | https://panel.holoviz.org/reference/indicators/Number.html |
| LinearGauge / Gauge / Dial indicators | https://panel.holoviz.org/reference/indicators/LinearGauge.html · https://panel.holoviz.org/reference/indicators/Gauge.html · https://panel.holoviz.org/reference/indicators/Dial.html |
| TooltipIcon indicator | https://panel.holoviz.org/reference/indicators/TooltipIcon.html |
| FloatPanel layout | https://panel.holoviz.org/reference/layouts/FloatPanel.html |
| Modal layout | https://panel.holoviz.org/reference/layouts/Modal.html |
| Tabs layout | https://panel.holoviz.org/reference/layouts/Tabs.html |
| Card layout | https://panel.holoviz.org/reference/layouts/Card.html |
| GridStack layout | https://panel.holoviz.org/reference/layouts/GridStack.html |
| Swipe layout | https://panel.holoviz.org/reference/layouts/Swipe.html |
| FastListTemplate (header/sidebar/main/right_sidebar/modal) | https://panel.holoviz.org/reference/templates/FastListTemplate.html |
| How-to index | https://panel.holoviz.org/how_to/index.html |
| Register session callbacks (index) | https://panel.holoviz.org/how_to/callbacks/index.html |
| Periodic callbacks | https://panel.holoviz.org/how_to/callbacks/periodic.html |
| Session start/end callbacks | https://panel.holoviz.org/how_to/callbacks/session.html |
| Async callbacks | https://panel.holoviz.org/how_to/callbacks/async.html |
| Defer bound functions / long-running tasks | https://panel.holoviz.org/how_to/callbacks/defer_load.html · https://panel.holoviz.org/how_to/callbacks/load.html |
| Schedule global tasks | https://panel.holoviz.org/how_to/callbacks/schedule.html |
| Streaming examples — Bokeh / Tabulator / Perspective / Indicator | https://panel.holoviz.org/how_to/callbacks/examples/streaming_bokeh.html · https://panel.holoviz.org/how_to/callbacks/examples/streaming_tabulator.html · https://panel.holoviz.org/how_to/callbacks/examples/streaming_perspective.html · https://panel.holoviz.org/how_to/callbacks/examples/streaming_indicator.html |
| Access session state (index) | https://panel.holoviz.org/how_to/state/index.html |
| Access and manipulate the URL | https://panel.holoviz.org/how_to/state/url.html |
| Sync widgets and URL (example) | https://panel.holoviz.org/how_to/state/examples/sync_url.html |
| Busyness state | https://panel.holoviz.org/how_to/state/busy.html |
| Link parameters with callbacks (index) | https://panel.holoviz.org/how_to/links/index.html |
| Python links via `.watch` / `.link` | https://panel.holoviz.org/how_to/links/watchers.html · https://panel.holoviz.org/how_to/links/links.html |
| JS links | https://panel.holoviz.org/how_to/links/jslinks.html |
| Interactivity / `pn.bind` (index) | https://panel.holoviz.org/how_to/interactivity/index.html · https://panel.holoviz.org/how_to/interactivity/bind_component.html |
| Serving multiple applications | https://panel.holoviz.org/how_to/server/multiple.html |
| Re-connecting to a session | https://panel.holoviz.org/how_to/server/reconnect.html |
| Custom server endpoints | https://panel.holoviz.org/how_to/server/endpoints.html |
| Serving static files | https://panel.holoviz.org/how_to/server/static_files.html |
| Launching a server dynamically | https://panel.holoviz.org/how_to/server/programmatic.html |
| Concurrency | https://panel.holoviz.org/how_to/concurrency/index.html |
| Caching (`pn.cache`, memoization) | https://panel.holoviz.org/how_to/caching/index.html · https://panel.holoviz.org/how_to/caching/memoization.html |
| Tabulator JS options (for `configuration`) | https://tabulator.info/docs/6.4/options |
| Apache ECharts option reference | https://echarts.apache.org/en/index.html |
| jsPanel options (for `FloatPanel.config`) | https://jspanel.de/#options/overview |

**Installed-package probes [P]** were run against `.venv` (panel 1.9.3) in this session and cover:
`pn.extension._imports`, the `ReactiveHTML` extension names, class availability, construction of
each component without extra dependencies, third-party dependency presence, `panel/dist/bundled/*`
contents, `RESOURCE_MODE`, the `pn.state`/`pn.config` surface, `pn.serve`'s signature, and the
`Tabulator`/`Perspective`/`Trend`/`Location`/`Modal`/`FastListTemplate` APIs. They are ad-hoc
session probes, **not** committed under `docs/probes/` — anything that becomes load-bearing should
be promoted there per that folder's convention.
