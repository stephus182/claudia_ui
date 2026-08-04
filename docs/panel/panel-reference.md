# Panel implementation reference

**How ClaudIA uses Panel today.** Living document — updated in place as the code changes.
Every claim below cites a `file:line` in this repo or the installed package version; nothing
is stated from memory (CLAUDE.md "API Docs First").

Companion docs in this folder:
- `ui-design-reference.md` — the styling/design surface and the official-source link index.
- The two dated `2026-07-24-*.md` files — point-in-time *research* from the migration
  (chart pane, PineScript/actionable buttons). Not updated in place.

Versions this document describes: **panel 1.9.3**, **bokeh 3.9.2**, Python 3.11.

---

## 1. Serving model

ClaudIA is served by Panel's own first-class Tornado server. There is **no FastAPI, no
uvicorn, and no ASGI wrapper** — the standing "Panel-native, no workarounds" rule
(2026-07-24) means any Panel workaround triggers a stop-and-reflect rather than a patch.

```python
pn.serve(
    _build_session_root,
    port=_PANEL_PORT,
    show=False,
    title="ClaudIA",
    websocket_origin=[f"localhost:{_PANEL_PORT}", f"127.0.0.1:{_PANEL_PORT}"],
)
```
— [`panel_app.py:980-988`](../../claudia/panel_app.py#L980-L988)

| Detail | Value | Why it matters |
|---|---|---|
| Port | `CLAUDIA_PANEL_PORT`, default `8001` ([`:62`](../../claudia/panel_app.py#L62)) | |
| `pn.serve` target | a **callable**, not an object — invoked once per browser session | Module-level singletons stay process-wide; per-session state is built inside the factory |
| `websocket_origin` | **both** `localhost:<port>` and `127.0.0.1:<port>` | Panel's default allowlist is `localhost:<port>` only; reaching the app via `127.0.0.1` without this returns a **403 websocket refusal** (probe-verified) |
| SIGTERM | translated to SIGINT at [`:978`](../../claudia/panel_app.py#L978) | Panel installs its own SIGINT handler *inside* `pn.serve`; translating routes launchd/scripts through the same `io_loop.stop()` → serve-returns path |
| Shutdown | final `upload_db` in the `finally` at [`:989-996`](../../claudia/panel_app.py#L989-L996) | The loop is stopped by then, so a synchronous blocking upload is safe ("V5 contract") |

Run with `python -m claudia.panel_app` (or `./start-claudia.sh`, which also starts the IBKR
gateway). Serving behavior was verified by `docs/probes/pnserve_probe.py`.

---

## 2. Module map

| Module | Lines | Responsibility |
|---|---|---|
| [`claudia/panel_app.py`](../../claudia/panel_app.py) | 1000 | `pn.serve` entry, session lifecycle, status dots, action buttons, screenshot upload, layout root |
| [`claudia/panel_sink.py`](../../claudia/panel_sink.py) | 132 | `PanelMessageSink` — agent output → `ChatInterface` |
| [`claudia/panel_order_flow.py`](../../claudia/panel_order_flow.py) | 165 | Order/cancel/modify proposals → buttons → `order_flow.py` cores |
| [`claudia/panel_chart.py`](../../claudia/panel_chart.py) | 259 | External HoloViews candlestick pane |
| [`claudia/panel_pinescript.py`](../../claudia/panel_pinescript.py) | 152 | ` ```pine ` copy/inject buttons |
| [`claudia/message_sink.py`](../../claudia/message_sink.py) | 43 | The UI-decoupling protocol — **zero Panel imports** |

`claudia/agent.py` imports **no Panel symbol at all**; see §5. Only the `panel_chart.py` row
above was re-measured 2026-08-03 (`wc -l`), for this branch's own change; the other line
counts were not re-verified and may also have drifted.

---

## 3. Session lifecycle

Three layers, each with a contract that was verified rather than assumed.

### 3.1 Per-session factory

`pn.serve` calls [`_build_session_root()`](../../claudia/panel_app.py#L923) once per browser
session, which calls [`_build_chat_app()`](../../claudia/panel_app.py#L580). Only the chat
surface is built synchronously — everything heavy (GDrive download, `ConversationStore`,
`ContextLoader`, the agent) runs in a background `_init_session()` task
([`:748-911`](../../claudia/panel_app.py#L748-L911)).

### 3.2 Input gating

`_init_done = asyncio.Event()` ([`:606`](../../claudia/panel_app.py#L606)). The chat callback
`_on_user_input` does `await _init_done.wait()` first
([`:639`](../../claudia/panel_app.py#L639)), so a message typed during startup **waits for
init instead of racing it or erroring**. The event is set in `_init_session`'s `finally`.

### 3.3 Session destroy — the "V4 contract"

`pn.state.on_session_destroyed(_on_session_destroyed)` is registered at
[`:636`](../../claudia/panel_app.py#L636), deliberately **before** the callback wiring and the
init task, so even a session whose init later fails still gets cleaned up.

The hook ([`:608-631`](../../claudia/panel_app.py#L608-L631)) must respect all four verified
properties:

1. **Sync only** — Panel does not await it.
2. Fires **15–32 s after disconnect**, not immediately.
3. Runs **on the shared event loop** — blocking here freezes *every* live session. It
   schedules `_run_session_cleanup` as a task and returns immediately.
4. **`pn.state.curdoc` is `None`** — so **no UI calls** from this path.

Verified by `docs/probes/probe_d7_server_fixed.py`.

### 3.4 Task reference-keeping

`_cleanup_tasks` / `_background_tasks` ([`:81`, `:85`](../../claudia/panel_app.py#L81-L85))
and `_session["init_task"]` hold strong references, because the event loop only weak-refs
tasks (ruff RUF006). Dropping the reference can garbage-collect a live cleanup mid-flight.

### 3.5 `_init_lock` — a data-integrity lock, not a performance one

The GDrive DB download **must** complete before `ConversationStore` first opens the DB file.
`asyncio.to_thread` opens a yield window that the old synchronous Chainlit path did not have:
without the lock, session B could see `_gdrive_sync` already set, skip the wait, and open
sqlite on the file the download thread is about to atomically replace — B would hold the
**unlinked inode and silently lose its writes**. The lock serializes
check + download + first-store-open. Full rationale at
[`:751-761`](../../claudia/panel_app.py#L751-L761).

### 3.6 Cleanup

[`_run_session_cleanup`](../../claudia/panel_app.py#L535) stops the doc watcher, closes the
session in the store, generates the session report and uploads `claudia.db` — the last two via
`asyncio.to_thread` — and returns a one-line status string. **No UI calls** (it serves the
destroy path as well as the End Session button).

---

## 4. Layout tree

The complete UI structure, verbatim from
[`_build_session_root`](../../claudia/panel_app.py#L950-L957):

```
pn.Row(                              sizing_mode="stretch_both"
├── pn.Column(
│   ├── pn.Row(BooleanStatus×3, FileInput)     ← status dots + screenshot upload
│   └── pn.chat.ChatInterface                  ← the conversation
└── build_chart_pane()                         ← pn.Column: controls / status / pn.pane.HoloViews
)
```

**There is no template, no sidebar, no header, and no modal.** Split ratio and
side-by-side-vs-tabs are explicitly deferred restyle decisions
([`:946-949`](../../claudia/panel_app.py#L946-L949)). See `ui-design-reference.md`.

---

## 5. The MessageSink seam

The safety-critical agent loop — streaming, tool routing, the hardcoded safety block, the
`propose_*` tool handlers — knows nothing about Panel.

[`claudia/message_sink.py`](../../claudia/message_sink.py) is a pure `typing.Protocol` module
with **zero Panel imports**, defining `ToolStepHandle` (mutable `input`/`output` +
`__aenter__`/`__aexit__`) and `MessageSink` (six methods: `send_message`, `tool_step`,
`send_max_tokens_warning`, `send_order_proposal`, `send_cancel_proposal`,
`send_modify_proposal`).

`claudia/agent.py` imports it **only under `TYPE_CHECKING`** (`agent.py:43`), takes it as a
constructor parameter (`agent.py:494`), and touches it at exactly six call sites:
`agent.py:714, 756, 798, 803, 805, 807`. Swapping UI frameworks means writing one new sink.

[`PanelMessageSink`](../../claudia/panel_sink.py#L77) **duck-types** the protocol (no explicit
inheritance). Notable behavior:

- `send_message` sends as `user="ClaudIA"`, then auto-detects ` ```pine ` blocks and renders
  Copy/Inject buttons ([`:94-102`](../../claudia/panel_sink.py#L94-L102)). Detection lives in
  the **sink** path specifically so `agent.py` stays untouched.
- `tool_step` constructs a real `pn.chat.ChatStep` — Panel's equivalent of Chainlit's
  `cl.Step` — and sends it as a chat message ([`:104-112`](../../claudia/panel_sink.py#L104-L112)).
- [`_PanelToolStepHandle`](../../claudia/panel_sink.py#L21) translates protocol
  attribute-sets into `ChatStep.stream()` calls. Consecutive string `.stream()` calls
  concatenate with no separator, so it supplies its own `"\n\n"`
  ([`:59-62`](../../claudia/panel_sink.py#L59-L62)).
- It **deliberately leaves `failed_title` unset** — verified live that setting it suppresses
  `ChatStep`'s own automatic exception-message streaming, because that `self.stream(exc_msg)`
  call in `__exit__` is gated on `failed_title` being `None`. Leaving it unset yields a
  correct auto-title *and* the real error text, for free
  ([`:29-33`](../../claudia/panel_sink.py#L29-L33)).
- The three proposal methods use **deferred imports** of `claudia.panel_order_flow` to avoid a
  `panel_app ↔ panel_sink` import cycle ([`:122-132`](../../claudia/panel_sink.py#L122-L132)).

Wiring: `panel_app.py` constructs the sink with `tv_bridge_getter=lambda: _tv_bridge`, which
reads the live module global **at click time**, so a TradingView instance launched *after* a
` ```pine ` message rendered is still picked up ([`panel_sink.py:90-92`](../../claudia/panel_sink.py#L90-L92)).

---

## 6. Widget idioms & gotchas

Read this section before touching any Panel code here. Each item cost real debugging time.

### `label=` / `color=`, never `name=` / `button_type=`
`Widget.name` raises `PendingDeprecationWarning` on Panel 1.9.3 (probe-verified), which would
break the test suite's 1-warning gate. `label=` is the supported replacement. Applies to
`Button`, `BooleanStatus`, `TextInput`, `Select`.

Three details from the 1.9.0 release notes + a 2026-07-31 probe of the installed package:
`label`, `color` and `variant` are the **canonical** parameters and `name`, `button_type`,
`button_style` the aliases (not the other way round); the warning names its removal
target — *"deprecated and will be removed in version 2.0"*; and it fires **only for
constructor keywords**. `pn.widgets.Button(name="x")` warns, `btn.name = "x"` after
construction is silent, so the warning gate cannot catch the assignment form.
[`panel_app.py:286-288`](../../claudia/panel_app.py#L286-L288),
[`panel_chart.py:119-120`](../../claudia/panel_chart.py#L119-L120)

### Disable-first async click handlers
Every button disables **before** the `await`, not in a `finally`. Re-enabling differs by risk:

| Module | Re-enables on failure? | Why |
|---|---|---|
| `panel_order_flow.py` | **No** | Safety-critical — one live order. [`:56-60`](../../claudia/panel_order_flow.py#L56-L60) |
| `panel_pinescript.py` | **Yes** | Inject is idempotent (it just sets editor source); the commonest failure is "TV not launched yet", recoverable on the same button. [`:112-116`](../../claudia/panel_pinescript.py#L112-L116) |

### Periodic callbacks: `start=False` + `onload`
```python
cb = pn.state.add_periodic_callback(_refresh, period=5000, start=False)
pn.state.onload(cb.start)
```
Starting at build time registers the callback doc-side before the `ServerSession` exists; the
held `SessionCallbackAdded` event is then replayed at unhold on top of the session-init sweep
— a double delivery raising a per-session bokeh `ValueError: A callback of the same type has
already been added with this ID`. Deferring the start to `onload` delivers it exactly once and
still registers in `pn.state._periodic[curdoc]`, preserving session auto-cleanup.
[`panel_app.py:937-945`](../../claudia/panel_app.py#L937-L945)

### Thread → session delivery
The watchdog doc-change alert fires in a plain OS thread. The verified bridge is
`loop.call_soon_threadsafe(partial(chat.send, ...))`, guarded by `except RuntimeError` for a
closed loop. [`panel_app.py:804-816`](../../claudia/panel_app.py#L804-L816); probe
`docs/probes/d4_probe.py`.

### File upload: standalone `FileInput`, not `ChatInterface(widgets=[...])`
`ChatInterface`'s native file tab **unpacks its upload wrapper before the callback sees it** —
mime type and filename are lost and a bare `BytesIO` is delivered. Screenshots therefore use a
standalone `pn.widgets.FileInput(accept="image/*")` plus a `param.watch` watcher, whose public
`value`/`mime_type`/`filename` params carry full metadata. `accept=` is a client-side hint
only; the watcher re-checks the mime type server-side. Metadata is snapshotted at watcher entry
*before* the init await, so a second upload can't cross-wire its mime type onto the first one's
bytes. Resetting needs **both** `file_input.clear()` (client) and
`param.update(value=None, mime_type=None, filename=None)` (server).
[`panel_app.py:665-746`](../../claudia/panel_app.py#L665-L746)

The widget is handed to the layout root via a plain attribute `chat._claudia_file_input`, read
back through the typed accessor [`_screenshot_file_input`](../../claudia/panel_app.py#L915) so
the single `type: ignore` lives in one place.

### `js_on_click` args are injection-safe
```python
copy_btn.js_on_click(args={"code": code}, code="navigator.clipboard.writeText(code)")
```
Bokeh serializes `code` into a JS variable of the same name — it is **not** concatenated into
the JS string, so arbitrary PineScript source cannot break out.
[`panel_pinescript.py:105-107`](../../claudia/panel_pinescript.py#L105-L107)

This is also the only real client-side clipboard write in the app, and the concrete
Panel-over-Chainlit win from the migration.

### Blocking work always via `asyncio.to_thread`
Drive calls, IBKR calls, sqlite-heavy work. The destroy-hook rationale generalizes: blocking
the shared loop freezes every live session.

---

## 7. Status dots

[`_make_status_indicators`](../../claudia/panel_app.py#L284) builds one
`pn.indicators.BooleanStatus` per service, keyed exactly like
`ConnectivityChecker.get_status()`. [`_apply_status`](../../claudia/panel_app.py#L295) maps:

| `ServiceStatus` | `value` | `color` | Appearance |
|---|---|---|---|
| `OK` | `True` | `"success"` | lit green |
| `ERROR` | `True` | `"danger"` | lit red |
| `UNKNOWN` | `False` | `"dark"` | unlit gray — not-yet-checked, **not** an error |

The mapping dict is `Literal`-typed so mypy accepts assignment into `BooleanStatus.color`'s
`Literal[...]` parameter ([`:301-307`](../../claudia/panel_app.py#L301-L307)).

**Two different intervals — do not conflate them:**

- The **UI** re-reads the checker's cached status every **5 s**
  (`add_periodic_callback(..., period=5000)`, [`:944`](../../claudia/panel_app.py#L944)).
- The **checker** polls the actual services every **60 s**
  (`POLL_INTERVAL = 60`, [`status.py:31`](../../claudia/status.py#L31)) — that interval is set
  by IBKR's `/tickle` keepalive requirement, not by the UI.

Alerts are separate from dots: `ConnectivityChecker` pushes pre-formatted text to an async
subscriber that calls `chat.send` directly, which is safe because the checker's poll task runs
on the same process-wide loop as the session ([`:314-323`](../../claudia/panel_app.py#L314-L323)).

---

## 8. Action buttons

Sent as one `pn.Row` inside a chat message
([`_send_action_buttons`](../../claudia/panel_app.py#L326)):

| Button | Shown when | `color` |
|---|---|---|
| End Session | always | `light` |
| Start IBKR Gateway | IBKR offline | `primary` |
| Launch TradingView | TradingView offline | `warning` |

All three stream progress messages, drive blocking work through `asyncio.to_thread`, and
force-refresh the connectivity checker afterwards. Launch TradingView rebuilds the bridge under
`_tv_bridge_lock` and re-wires the checker and the agent's tool merge.

---

## 9. Chart pane

Self-contained and **decoupled from the conversation** — driven by its own Load button.

- **HoloViews/hvplot, not hand-built Bokeh glyphs** (superseded 2026-08-03 — see
  `data-surfaces-reference.md` D1). `build_chart_object`
  ([`panel_chart.py:92-175`](../../claudia/panel_chart.py#L92-L175)) makes three separate
  hvplot calls — `df.hvplot.ohlc(...)` for the price row (wick `Segments` + body
  `Rectangles`), `sma.hvplot.line(...)` for the 20-period SMA `Curve` overlaid on top of it,
  and `df["volume"].hvplot.bar(...)` for the volume row below — combined into one
  `holoviews.Layout` via `(price + volume).cols(1)` and rendered by `pn.pane.HoloViews`
  (not `pn.pane.Bokeh`).
- **Candle/bar width is derived by hvplot itself**, not computed by this module: both
  `.ohlc()` and `.bar()` scale their default width by `np.min(np.diff(x))` — the data's own
  **minimum** bar spacing — so 1h/30m candles can no longer smear the way the old hand-built
  `p.vbar` recipe did before the fix in commit `a51b454` (some docs cited that fix as
  `794d7c0`; that hash is not a commit in this repository). `bar_width=0.7`
  (`_BODY_WIDTH_FRACTION`, [`:74`](../../claudia/panel_chart.py#L74)) is the only width knob
  this module passes; the volume row uses `hv.Bars`' own default instead.
- **Colors**: `_UP_COLOR = "#26a69a"`, `_DOWN_COLOR = "#ef5350"`
  ([`:62-63`](../../claudia/panel_chart.py#L62-L63)) are passed to `.ohlc()` as
  `pos_color`/`neg_color` and only reach the candle **bodies** (`Rectangles`) — the wicks
  (`Segments`) render at hvplot's own default black regardless (verified 2026-08-03 by
  inspecting the rendered glyphs' style). There is no wick-color constant in this module; the
  old `_WICK_COLOR = "#666"` was deleted along with the hand-built recipe, not renamed. These
  two are still the only hardcoded hex colors anywhere in `claudia/` (grepped 2026-08-03).
- ⚠ **`.ohlc()` binds the OHLC columns by POSITION, not by name.** `converter.py` does
  `o, h, l, c = [col for col in data.columns if col != x][:4]` when `y is None`. Measured
  2026-08-03: move `volume` ahead of `open` in the frame and it silently charts
  **volume-vs-low with every candle red**, raising nothing. `build_chart_object` therefore
  passes `y=["open", "high", "low", "close"]` explicitly, and a test
  (`test_build_chart_object_is_column_order_independent`) pins it. **Do not delete that
  argument as redundant**: the hvPlot reference's `y` parameter line reads as a name-based
  guarantee (*"Field names of the OHLC fields. Default is `["open","high","low","close"]`"*),
  and only its surrounding prose states the positional rule — the isolated line is what a
  reader checks.
- **Data**: cache-first via `toolkit._cache.check/load` (parquet, `DatetimeIndex`, lowercase
  columns), fetching from IBKR on a miss. `toolkit.execute` returns `tuple[str, None]` —
  a text result and a legacy always-`None` slot — **not** bars, hence the direct cache read.
  That text is load-bearing on the failure path (below), so do not discard it.
- **Failure messaging** (rebuilt 2026-08-03 after the live run, `ba6c83e`): a failed load
  quotes the fetch's own words — `✕ Could not load ZZQQXX — fetch reported: Could not resolve
  conid for ZZQQXX (as STK). Is IBKR connected? Still showing AAPL 1d (6m).` Two deliberate
  choices there. It **attributes** rather than asserts a root cause, so the pathological
  "fetch reported success yet the load still failed" case stays visible. And it **names the
  chart still on screen**, because a failed load deliberately leaves the previous chart up —
  losing a chart to a typo is worse — which otherwise leaves the title and the symbol box
  disagreeing with nothing to reconcile them.
- **A 1-row frame is refused, not drawn.** hvplot sizes candles from `np.min(np.diff(x))`;
  one row makes `np.diff` empty and numpy raises. `build_chart_object` converts that into
  `ValueError("Cannot chart a single bar - need at least 2 bars.")` so the status line shows
  something actionable instead of numpy internals. 0 rows and 2 rows are both fine.
- **Refresh** by reassigning `chart.object`; the Load button sets `loading = True` first and
  always clears it in `finally`.
- **STK only.**

### Why min-spacing beats median — a live production case, not a theoretical one

The deleted `_body_width_ms` used **median** spacing; hvplot uses **min**. Review treated the
difference as a corner case. The 2026-08-03 live run found it occurring for real **[P]**.

IBKR downsamples a long intraday request. Asking for `6m` at `30m` returned 267 bars whose
gaps were nothing like 30 minutes — measured histogram: 150min ×102, 1290min ×80, 90min ×21,
240min ×21. So `min = 90min` while `median = 240min`:

```
deleted _body_width_ms:  0.7 × median(240min)  = 168min body
actual minimum gap between bars                =  90min
                                    168 > 90  →  overlapping candles
```

The old helper would have smeared a chart you can load today. hvplot's min-based width gives
a 0.700 ratio with no overlap at every timeframe tested (1d, 6m/30m, 1m/30m). Two corollaries
worth keeping: **min is strictly safer than median** here, because `min ≤ every gap` makes
overlap arithmetically unreachable; and the risk it *does* carry is the opposite one — a
single anomalously *small* gap shrinks every body (a duplicate timestamp gives width 0).
`ibkr_core_mcp` sorts and de-duplicates its bars, so that is guarded upstream rather than here.

### The full matrix — measured, and it changes two conclusions

All fifteen `period × bar` combinations the UI offers, swept against a live gateway
2026-08-03 **[P]**:

| period | bar | bars | min gap | median gap | median rule |
|---|---|---|---|---|---|
| 1m | 1d | 20 | 1440m | 1440m | ok |
| 1m | 1h | 147 | 30m | 60m | **would smear** |
| 1m | 30m | 273 | 30m | 30m | ok |
| 3m | 1h | 427 | 30m | 60m | **would smear** |
| 3m | 30m | 244 | 30m | 120m | **would smear (2.80×)** |
| 6m | 1h | 861 | 30m | 60m | **would smear** |
| 6m | 30m | 267 | 90m | 240m | **would smear (1.87×)** |
| 1y | 1h | 1408 | 30m | 60m | **would smear** |
| 2y | 1h | 3000 | 30m | 60m | **would smear** |
| 1y/2y | 30m | 250 / 499 | 1440m | 1440m | ok — *because it is daily data* |

**1. The median rule fails on 7 of 15, not on one edge case.** Every `1h` request smears
(5/5) — a "1h" series has a 30-minute *minimum* gap from the half-hour bar at the RTH open,
so `0.7 × median(60m) = 42m` overflows a 30m slot by 1.40×. `3m/30m` is the worst at 2.80×.
hvplot's min-based width measured **exactly 0.7000 on all fifteen**, zero overlaps, no spread.

**2. `1y/30m` and `2y/30m` do not return 30-minute bars — they return daily ones.** Verified
byte-identical index and close values to the same period at `1d`, on AAPL and TSLA. Neither
IBKR nor `ibkr_core_mcp` discloses it: the fetch summary reads *"Fetched TSLA **30M** (1y)
from IBKR: 250 bars"*. Until 2026-08-03 the chart repeated the claim in its title.

`_infer_bar_label` ([`panel_chart.py`](../../claudia/panel_chart.py)) now derives the bar size
from the frame's own **median** spacing and the pane titles what the data actually is:

```
AAPL 1d (1y) — requested 30m
Loaded 250 bars for AAPL. ⚠ IBKR returned 1d bars, not the 30m requested.
```

Median and not min here for the same reason the *width* wants min and not median: an hourly
series' 30-minute opening gap would make a min-based rule call all five `1h` combinations
"30m" and fire a false alarm on correct data. Non-standard spacings report as `~2h` so they
cannot be mistaken for a selectable bar.

See `ui-design-reference.md` §7 for why the color choice matters.

---

## 10. Testing Panel without a browser

123 tests across five files, none of which starts a server or a browser (re-counted
2026-08-03 via `pytest --collect-only`, per file — counts drift with every change to these
files, so re-count rather than trust this table).

| File | Tests | Covers |
|---|---|---|
| `tests/test_panel_app.py` | 60 | Factory/callback wiring, Drive-DB-before-store ordering, init failure paths, doc versioning, opening status, watchdog alert delivery, singleton lifecycle, cleanup + destroy hook, all three action buttons, Flex sync, status dots, screenshot upload |
| `tests/test_panel_pinescript.py` | 18 | Block-extraction edge cases, per-block closure correctness, `js_on_click` args, inject success/failure classification |
| `tests/test_panel_chart.py` | 28 | Pane composition, `_on_load` cache/fetch/error/spinner paths and failure messaging, `build_chart_object` HoloViews assembly (wicks/bodies/SMA/volume, width scaling, column-order independence, 1-row refusal) |
| `tests/test_panel_sink.py` | 10 | Message routing, pine detection, `ChatStep` streaming + failure, proposal delegation |
| `tests/test_panel_order_flow.py` | 7 | Each proposal type: buttons rendered, confirm calls the right core, dismiss disables without executing |

**The idiom that makes this possible** —
[`tests/conftest.py`](../../tests/conftest.py):

- `_find_buttons(chat)` walks chat messages for `pn.widgets.Button` objects.
- `_get_click_callback(button)` reads `button.param.watchers["clicks"]["value"]` and filters on
  `onlychanged is False`. Verified live against panel 1.9.3: `Button.on_click(cb)` is
  implemented as `self.param.watch(cb, 'clicks', onlychanged=False)` and **there is no
  `_on_click` attribute**. Panel's internal sync watchers are always `onlychanged=True`, so
  that flag reliably isolates the production callback. Calling `.fn` and awaiting it exercises
  exactly what a real click would.

Three autouse fixtures in `test_panel_app.py` prevent tests from building a real
`ConnectivityChecker`/`ExecutionListener` or hitting Flex/TradingView, with
`@pytest.mark.real_flex_sync` / `real_tv_connect` escape hatches registered in
`pyproject.toml`.

### Asserting on a HoloViews chart

The chart moved from `pn.pane.Bokeh` to `pn.pane.HoloViews` on 2026-08-03, and that
**improved** the assertion surface rather than complicating it. `pane.object` is now the
declarative `holoviews.Layout`, so tests read the data being drawn instead of poking glyph
renderers:

```python
[type(e).__name__ for e in pane.object]        # ['Overlay', 'Bars']
_price(obj).Rectangles.I.data                  # candle count, lbound/ubound per body
_price(obj).Curve.Sma_20.vdims[0].name         # 'sma_20'
hv.Store.lookup_options("bokeh", rects, "style").kwargs["color"].apply(rects)
                                               # per-row ['#26a69a', …, '#ef5350']
```

Two traps this file hit, both worth knowing before writing such a test **[P]**:

- ⚠ **`hasattr` cannot distinguish an `Overlay` from a `Layout`.** HoloViews' dynamic
  attribute access answers `hasattr(overlay, "Overlay")` with **`True`** and returns an
  *empty* `:Overlay`, so a `hasattr`-based helper silently resolves to an element with no
  data. Dispatch on `isinstance(obj, hv.Layout)`. The symptom is `AssertionError` /
  `KeyError: 'ubound'` / `KeyError: 'color'`, never something that names the real cause.
- ⚠ **Bokeh's `Model.select()` returns a generator**, annotated `Iterable[Model]` — `len()`
  on it raises `TypeError: object of type 'generator' has no len()`. Wrap in `list(...)`.

Candle-geometry assertions need `pytest.approx`: at daily spacing the measured body is
`16:47:59.999998`, one microsecond under an exact `Timedelta(hours=24) * 0.7`, because
`hv.Dataset` exposes the index as `datetime64[us]` and the sampling arithmetic truncates
twice.

### Verifying a *served* app — beyond `get_root()`

`pn.pane.HoloViews(obj).get_root()` succeeding in a bare process proves an import side
effect, **not** that a served app renders — inside a Panel server, JS loading is governed by
`pn.extension()`, a different mechanism. Two checks that look decisive and are not, both
tried on 2026-08-03:

- Grepping the saved HTML for `"HoloViews"` / `"hv.plotting"` returns **0** for a page that
  renders perfectly — those are Python namespace strings that never appear in serialized
  Bokeh JSON.
- `GET`ting the served URL and looking for `docs_json` also fails: a Panel server returns a
  small HTML shell and delivers the document over **WebSocket**.

What does work — pull the live session and inspect the real document:

```python
from bokeh.client import pull_session
srv = pn.serve({"/c": app}, port=5601, show=False, threaded=True)
doc = pull_session(url="http://localhost:5601/c").document
Counter(type(m).__name__ for m in doc.roots[0].references())
# figure: 2, GlyphRenderer: 4, ColumnDataSource: 4, GridPlot: 1  → heights [120, 300]
```

Or, from a driven browser, query `window.Bokeh.documents` directly — which is how the
zoom-sync claim was settled (same `x_range` object; mutating one figure's range moved both).
That is stronger than a screenshot, because it reads the state rather than its rendering.

Full suite: `pytest` (**780 passed, 3 skipped** — re-run 2026-08-03). Gates:
`ruff check claudia/ tests/ && mypy claudia/`, both clean on the same run.

---

## 11. Versions & dependencies

| Package | Declared | Installed |
|---|---|---|
| `panel` | `>=1.9` ([`pyproject.toml:11`](../../pyproject.toml#L11)) — lower bound only | **1.9.3** |
| `bokeh` | `>=3.8` ([`pyproject.toml:12-23`](../../pyproject.toml#L12-L23)) — lower bound only | **3.9.2** |
| `pandas` | `>=2.2` ([`pyproject.toml:24-29`](../../pyproject.toml#L24-L29)) — lower bound only | **3.0.5** |
| `panel-material-ui` | not declared | 0.14.1 (transitive via panel; **imported nowhere**) |

**Resolved 2026-07-24 (was a known gap):** [`panel_chart.py:36`](../../claudia/panel_chart.py#L36)
imports `bokeh.plotting` directly, but `bokeh` used to appear nowhere in `pyproject.toml` — it
resolved only because panel depends on it, so a panel release dropping or renaming that dependency
would have broken the chart pane at import time. It is now a declared direct dependency, with no
upper bound, leaving panel as the single place that caps the version.

mypy note: panel and bokeh both ship `py.typed`, so `ignore_missing_imports` is **not** needed
for them — `pandas` does not, and is one of the three modules in the override list
([`pyproject.toml:127-139`](../../pyproject.toml#L127-L139); line reference corrected
2026-07-31, it pointed at the ruff ignore list).

**Import cost, measured 2026-08-03:** `import hvplot.pandas`
([`panel_chart.py:51`](../../claudia/panel_chart.py#L51)) is the dominant cost of importing
`claudia.panel_chart` at all, paid once at `panel_app` import (`panel_app.py` imports
`build_chart_pane` at module top — that module's own docstring). Measured via
`python -X importtime -c "import claudia.panel_chart"`, six fresh-process runs (one ~1.3s
outlier excluded as system jitter): `hvplot.pandas`'s own cumulative import time ranged
**~536–584ms**, against a `claudia.panel_chart` total of **~692–753ms** — roughly 75–80% of
the total. Reported as a range because it did not settle on a single figure across runs, not
because it is imprecise.

### Upstream release checkpoint — 2026-07-31

Source: <https://panel.holoviz.org/about/releases.html> — fetched whole (101 version sections,
1.9.3 down to 0.1.3). **Depth is not uniform, deliberately:** 1.9.0–1.9.3 read line by line,
1.8.0–1.8.10 scanned for deprecations, compatibility notes and anything naming a component this
app uses; the pre-1.8 sections were not reviewed (they describe versions this project has never
run). Every finding below is cross-checked against the installed dist metadata, a probe of the
installed package, or PyPI — never against the notes alone.

**No upgrade is pending.** 1.9.3 is the newest Panel release on both the releases page and
PyPI (uploaded 2026-06-01); the venv is already on it. Everything below came out of that read.

| Finding | Where it landed |
|---|---|
| **Release notes run ahead of packaging metadata — twice.** 1.9.0 says "Dropped support for Bokeh 3.7", yet 1.9.3's metadata still declares `bokeh<3.10,>=3.7.0`. 1.8.4 announced that from 1.9.0 "pandas will no longer be installed by default", yet 1.9.3 still declares `pandas>=1.2`. Copying panel's metadata floor — which is what the 2026-07-24 note did — inherits a floor panel itself no longer supports | `bokeh>=3.7` → **`>=3.8`** ([`pyproject.toml:12-23`](../../pyproject.toml#L12-L23)) |
| **`pandas` was an undeclared direct import** — same class as the bokeh gap closed 2026-07-24. [`panel_chart.py:34`](../../claudia/panel_chart.py#L34) imports it, nothing in `pyproject.toml` did; it resolved only via panel (announced for removal) and ibkr_core_mcp (installed separately, not a declared dependency here) | added **`pandas>=2.2`** ([`pyproject.toml:24-29`](../../pyproject.toml#L24-L29)) |
| **The `name`/`button_type`/`button_style` deprecations have a removal target: Panel 2.0.** The warning text is explicit ("deprecated and will be removed in version 2.0"). All three fire `PendingDeprecationWarning` **only when passed as constructor keywords** — attribute assignment after construction is silent, so the suite's warning gate cannot catch a late `w.name = …` | §6, `label=`/`color=` item |
| **`color`/`variant` are the canonical parameters, `button_type`/`button_style` the aliases** — the 2026-07-24 research doc had the direction reversed | corrected in place, [`2026-07-24-pinescript-and-actionable-buttons-research.md`](2026-07-24-pinescript-and-actionable-buttons-research.md) §B1 |
| **1.9.0 switched Panel's HTML sanitization from `bleach` to `nh3` (#8503).** Checked against our own XSS control: [`panel_markdown.py`](../../claudia/panel_markdown.py) does not use Panel's sanitizer at all — it closes the vector with markdown-it's `html: False` plus `html.escape`. The engine swap does not touch it | no change needed |
| **`panel-material-ui>=0.10.0` is now a hard runtime dependency of panel** (not an extra), and 1.9.0's reference gallery was rewritten to use it. It is still imported nowhere here | noted in the version table above; relevant to `ui-design-reference.md` |

Also read and deliberately **not** acted on: wildcard routes for `pn.serve` (1.9.0 — our single
`_build_session_root` needs no dynamic routing), `PANEL_CDN_ROOT` / `pn.config.cdn_root`
(1.8.10), `ChatFeed.feed_type` (1.9.0), and the Tabulator/Plotly/Vega fix streams (no Tabulator,
Plotly or Vega in this app yet — see `data-surfaces-reference.md`).

#### Dependency sweep, same day

`pip list --outdated` over the venv, with each candidate's changelog read before it moved.
**Applied** (patch releases, gates re-run green afterwards — 765 passed, 3 skipped): `bokeh`
3.9.1 → **3.9.2**, `anthropic` 0.120.0 → **0.120.2**, `panel-material-ui` 0.14.0 → **0.14.1**,
`ruff` 0.16.0 → **0.16.1**. **Held back, each for a stated reason:**

| Held | Why |
|---|---|
| `mcp` 1.28.1 (2.0.0 available) | The `<2` pin is deliberate — 2.0.0 removed the `Server` decorators `ibkr_core_mcp/mcp_server.py` uses. Newly relevant: `anthropic` 0.120.2 added "support mcp sdk v2 alongside v1", so the Anthropic SDK is no longer part of what blocks that port |
| `websockets` 16.1.1 (17.0.1 available) | Major. 17.0 deprecates the legacy asyncio implementation, requires Python ≥3.11 and makes several args keyword-only. `ibkr_core_mcp/streaming.py` already uses the modern API (`websockets.connect(…, additional_headers=…)`, positional `send`), so it **reads** as compatible — but it is the live P&L/execution path and only a live gateway session can prove it. Not a claudia_ui dependency; it belongs to ibkr_core_mcp |
| `snowballstemmer` 2.2.0 (3.1.1 available) | **Must not move** — Crawl4AI pins `~=2.2`. Upgrading breaks the web-scraper tools |
| `playwright`, `openai`, `huggingface_hub`, `trimesh`, `filelock`, `tqdm`, `pydantic_core`, `cryptography`, `uvicorn` | Incidental transitives of the crawl4ai / google-auth / mcp stacks. Nothing here imports them directly; they move when their parent asks |

Tools checked the same day: `ibkr_core_mcp` clean and in sync with its origin (editable 1.2.2,
44 tools); the **TradingView sidecar was 60 commits behind with 4 npm vulnerabilities — since
upgraded** to `55534aa`, 0 vulnerabilities, 84 tools, all 16 curated names intact. That upgrade
silently broke the CDP-port override (the sidecar renamed the env var) and the fix is in
[`tradingview.py`](../../claudia/tradingview.py); full record in
[`docs/tradingview-reference.md`](../tradingview-reference.md) § Upgrading the sidecar.

---

## 12. See also

- `ui-design-reference.md` — styling surface, shadow-DOM constraint, official-source index
- [`docs/connectivity.md`](../connectivity.md) — what the status dots are reporting
- [`docs/startup-flow.md`](../startup-flow.md) — startup phase by phase
- [`docs/order-api-reference.md`](../order-api-reference.md) — the full order-staging spec
  behind `panel_order_flow.py`
- [`docs/probes/`](../probes/README.md) — the runnable scripts behind the serving, thread-bridge
  and destroy-hook findings
- `docs/plans/2026-07-22-panel-migration.md` — the full migration record (local-only,
  git-ignored)
