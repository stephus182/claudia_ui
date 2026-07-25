# Panel data surfaces reference — graphs, tables & indicators

**What Panel offers for the data surfaces ClaudIA does not have yet: tables, indicators,
additional graphs, side windows, and the wiring that lets the chatbot drive them (or lets them
run on their own).** Living document — started 2026-07-24.

This is a **starting point, not a design**. Nothing in §5–§8 has been built or agreed; ClaudIA's
only data surface today is the candlestick pane
([`claudia/panel_chart.py`](../../claudia/panel_chart.py)). The doc exists so that when the work
starts, the API surface, the version facts and the traps are already established rather than
rediscovered.

Companion docs in this folder:
- [`panel-reference.md`](panel-reference.md) — how ClaudIA uses Panel **today** (serving model,
  session lifecycle, the `MessageSink` seam, shipped widget gotchas). Read it first.
- [`ui-design-reference.md`](ui-design-reference.md) — styling surface, the shadow-DOM
  constraint, the restyle proposal. Anything visual belongs there, not here.

Versions described: **panel 1.9.3**, **bokeh 3.9.1**, **pandas 3.0.5**, Python 3.11.

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
| Candlestick chart | **Shipped.** Bokeh `segment` + two `vbar`, cache-backed, own Load button, STK only. [`panel_chart.py`](../../claudia/panel_chart.py) **[C]** |
| Status dots | **Shipped.** `pn.indicators.BooleanStatus` ×3. [`panel_app.py:284`](../../claudia/panel_app.py#L284) **[C]** |
| Any table | **None.** No `Tabulator`, no `Perspective`, no `DataFrame` pane anywhere in `claudia/` **[C]** |
| Any value indicator | **None** beyond the three status dots **[C]** |
| Any chatbot-driven surface | **None.** The chart pane is deliberately decoupled from the conversation — the user drives it **[C]** |
| `pn.extension(...)` | **Never called** anywhere in the repo **[C]** — see §3, this gates most of what follows |

So the honest baseline for this document: **one chart, three dots, and no extension call.**

### 1.1 Decisions taken (2026-07-24)

Unlike §5–§8, these are **settled** — confirmed by the user after reviewing §2.1's dependency
findings. Do not re-litigate them without new evidence.

**D1 — Bokeh is the chart engine. No new charting dependency.**
Every item on the deferred chart list is native Bokeh: volume subplot (a second figure), MA and
indicator overlays (`p.line`), crosshair and hover (`CrosshairTool` / `HoverTool`), zoom
synchronization (a shared `x_range`), multi-symbol comparison, theme-matched colors. Two other
"deferred chart features" are not charting questions at all — non-STK instruments is conid
resolution in the data layer, and freeform period entry is a widget. **Nothing currently wanted is
out of Bokeh's reach**, so the trade is not capability, it is verbosity: hand-built glyphs mean
more code we own and must test. The `vbar`-width bug (`794d7c0`) is the concrete example of that
cost, and it is accepted deliberately in favor of a light dependency structure.

The trigger that would reopen D1 is evidence, not preference: repeating the same glyph scaffolding
for a third and fourth chart. One hand-built chart is not enough evidence to justify an
abstraction. If it is reopened, hvPlot/HoloViews' actual OHLC support is the thing to verify
first — it has **not** been checked **[?]**.

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
`pyproject.toml`, resolving only because Panel depends on it. Now declared as `bokeh>=3.7`
([`pyproject.toml:12-17`](../../pyproject.toml#L12-L17)) — floor matched to panel 1.9.3's own
(`bokeh<3.10,>=3.7.0`), with no upper bound here so panel remains the single place that caps it.

---

## 2. Component inventory

Panel 1.9.3 ships 37 panes, 63 widgets, 15 layouts, 10 indicators, 9 templates and 6 chat
components **[S]**. Only the ones relevant to trading data surfaces are listed here.

### 2.1 Graphs

| Component | Extra Python dep? | Installed here? | Notes |
|---|---|---|---|
| `pn.pane.Bokeh` | none (bokeh is transitive via panel) | **yes** — bokeh 3.9.1 **[P]** | What the candlestick pane already uses. Full control, most code |
| `pn.pane.ECharts` | none for **raw dict** specs; `pyecharts` only if you pass pyecharts objects **[S]** | echarts JS **is bundled** with panel **[P]**; `pyecharts` **not installed** **[P]** | Accepts an ECharts spec as a plain dict. Params: `object`, `options`, `renderer` (`canvas`/`svg`), `theme` (`default`/`dark`/`light`) **[S]** |
| `pn.pane.Plotly` | `plotly` | **not installed** **[P]** | Constructing the empty pane works; rendering a figure needs the package |
| `pn.pane.HoloViews` | `holoviews` (+ `hvplot` for the DataFrame API) | **not installed** **[P]** | The high-level route. Adds two dependencies |
| `pn.pane.Matplotlib` | `matplotlib` | **not installed** **[P]** | Static images |
| `pn.pane.Vega` | `altair` for specs | **not installed** **[P]** | |

**The practical consequence:** today, adding a second chart type costs **zero new Python
dependencies only if it is Bokeh or ECharts**. Everything else adds a dependency to
`pyproject.toml` — worth deciding deliberately, given the repo already has one undeclared-bokeh
gap (`panel-reference.md` §11).

→ **This table informed D1 (§1.1), which is settled: Bokeh, no new charting dependency.** The rest
of the table is kept for the reopening case, not as a live menu.

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

**ClaudIA calls `pn.extension()` nowhere** **[C]**. That is fine for what is shipped (Bokeh
panes, buttons, `BooleanStatus`, `ChatInterface` all work without it), but **most of §2 needs
it**.

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
| `pn.config.reconnect` | **[P]** (default `False`) | **Automatic WebSocket reconnect** with exponential backoff at 1/2/4/8/16/32 s, or `"prompt"` for user-initiated. Requires panel ≥1.8 + bokeh ≥3.8 **[S]** — we are on 1.9.3/3.9.1 **[P]**, so it is available and currently off |
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

- **Order proposals** — the agent emits a JSON block, `agent.py` strips it and hands the dict to
  `MessageSink.send_order_proposal` **[C]**
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

Ordered by value-to-cost, all unbuilt:

1. **Positions / orders table** — `Tabulator` + `.style.map` for signed P&L. Closes half of the
   "P&L is colorless" gap (`ui-design-reference.md` §1) for tabular data, with no CSS and no
   restyle project. ⚠ **The blocker is data, not display**: those numbers currently arrive as
   *pre-formatted text* from ibkr_core_mcp (`ui-design-reference.md` §4), so a table needs a
   structured source — either a different toolkit call or a change in the other repo. Settle that
   before designing the table.
2. **Account/P&L tiles** — `Number` with `colors` thresholds, or `Trend` for value + sparkline +
   signed change. Same data caveat.
3. **Live execution feed** — `Tabulator.stream(...)` fed from `ExecutionListener` through the
   verified `call_soon_threadsafe` bridge (§5.3).
4. **Indicator overlays on the candlestick** — already on the deferred list
   (`ui-design-reference.md` §10: volume subplot, MAs, crosshair/hover, zoom sync). Pure Bokeh, no
   new dependency.
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
| 14 | `plotly` / `holoviews` / `hvplot` / `matplotlib` / `pyecharts` are **not installed** — Bokeh and ECharts are the dependency-free chart routes | **[P]** |
| 15 | Nothing inside `ChatInterface` is reachable by page-level CSS (7 shadow roots deep) — see `ui-design-reference.md` §2 | **[C]** |

---

## 9. Open questions

1. **Where does structured position/P&L data come from?** (§7.1) — the single biggest unknown, and
   it is an ibkr_core_mcp question, not a Panel one.
2. **In-page or separate window?** `GridStack`/`FloatPanel` versus a second `pn.serve` slug — a
   real fork with different state semantics (§4).
3. **Who owns the `pn.extension(...)` call?** Adding `tabulator` also means deciding `design`,
   `theme`, `notifications`, `defer_load`, `reconnect` in the same breath (§3).
4. **Chatbot-driven: block detection or protocol extension?** (§6 A vs B).
5. **Should `reconnect=True` be turned on regardless?** It is free, available on our versions, and
   a trading session losing its socket is a real cost (§5.6). Needs a live test, not a decision on
   paper.
6. **How is a data surface tested headlessly?** The button-click idiom is established
   (`panel-reference.md` §10); `stream`/`patch`/`on_edit` round-trips are not, and Tabulator's
   client-side behavior may not be observable server-side at all.

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
