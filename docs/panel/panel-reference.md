# Panel implementation reference

**How ClaudIA uses Panel today.** Living document — updated in place as the code changes.
Every claim below cites a `file:line` in this repo or the installed package version; nothing
is stated from memory (CLAUDE.md "API Docs First").

Companion docs in this folder:
- `ui-design-reference.md` — the styling/design surface and the official-source link index.
- The two dated `2026-07-24-*.md` files — point-in-time *research* from the migration
  (chart pane, PineScript/actionable buttons). Not updated in place.

Versions this document describes: **panel 1.9.3**, **bokeh 3.9.1**, Python 3.11.

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
| [`claudia/panel_chart.py`](../../claudia/panel_chart.py) | 182 | External Bokeh candlestick pane |
| [`claudia/panel_pinescript.py`](../../claudia/panel_pinescript.py) | 152 | ` ```pine ` copy/inject buttons |
| [`claudia/message_sink.py`](../../claudia/message_sink.py) | 43 | The UI-decoupling protocol — **zero Panel imports** |

`claudia/agent.py` imports **no Panel symbol at all**; see §5.

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
└── build_chart_pane()                         ← pn.Column: controls / status / pn.pane.Bokeh
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

- **Bokeh directly**, no hvplot/holoviews: a candlestick is `p.segment` for the high-low wicks
  plus **two** `p.vbar` calls partitioned on `close >= open`
  ([`panel_chart.py:88-104`](../../claudia/panel_chart.py#L88-L104)).
- **`vbar` width is in x-axis data units** (ms on a datetime axis), so it must track the actual
  bar interval — a fixed daily width turns 1h/30m candles into an overlapping smear (12×/24×
  their spacing). [`_body_width_ms`](../../claudia/panel_chart.py#L50) uses the index's
  **median** spacing × `0.7` (median is robust to weekend/overnight gaps), with a daily
  fallback for frames of fewer than 2 rows. This was a shipped bug fix, commit `794d7c0`.
- **Data**: cache-first via `toolkit._cache.check/load` (parquet, `DatetimeIndex`, lowercase
  columns), fetching from IBKR on a miss. `fetch_market_data` returns a summary, not bars —
  hence the direct cache read.
- **Refresh** by reassigning `chart.object`; the Load button sets `loading = True` first and
  always clears it in `finally`.
- **STK only.**

Colors are the only hardcoded palette in the codebase:
`_UP_COLOR = "#26a69a"`, `_DOWN_COLOR = "#ef5350"`, `_WICK_COLOR = "#666"`
([`:40-42`](../../claudia/panel_chart.py#L40-L42)). See `ui-design-reference.md` §7 for why
that matters.

---

## 10. Testing Panel without a browser

103 tests across five files, none of which starts a server or a browser.

| File | Tests | Covers |
|---|---|---|
| `tests/test_panel_app.py` | 54 | Factory/callback wiring, Drive-DB-before-store ordering, init failure paths, doc versioning, opening status, watchdog alert delivery, singleton lifecycle, cleanup + destroy hook, all three action buttons, Flex sync, status dots, screenshot upload |
| `tests/test_panel_pinescript.py` | 18 | Block-extraction edge cases, per-block closure correctness, `js_on_click` args, inject success/failure classification |
| `tests/test_panel_chart.py` | 14 | Pure figure construction, width scaling, cache hit/miss, empty-frame honesty, `loading` lifecycle |
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

Full suite: `pytest` (765 passed, 3 skipped — re-run 2026-07-31). Gates:
`ruff check claudia/ tests/ && mypy claudia/`.

---

## 11. Versions & dependencies

| Package | Declared | Installed |
|---|---|---|
| `panel` | `>=1.9` ([`pyproject.toml:11`](../../pyproject.toml#L11)) — lower bound only | **1.9.3** |
| `bokeh` | `>=3.8` ([`pyproject.toml:12-23`](../../pyproject.toml#L12-L23)) — lower bound only | **3.9.1** |
| `pandas` | `>=2.2` ([`pyproject.toml:24-29`](../../pyproject.toml#L24-L29)) — lower bound only | **3.0.5** |
| `panel-material-ui` | not declared | 0.14.0 (transitive via panel; **imported nowhere**) |

**Resolved 2026-07-24 (was a known gap):** [`panel_chart.py:36`](../../claudia/panel_chart.py#L36)
imports `bokeh.plotting` directly, but `bokeh` used to appear nowhere in `pyproject.toml` — it
resolved only because panel depends on it, so a panel release dropping or renaming that dependency
would have broken the chart pane at import time. It is now a declared direct dependency, with no
upper bound, leaving panel as the single place that caps the version.

mypy note: panel and bokeh both ship `py.typed`, so `ignore_missing_imports` is **not** needed
for them — `pandas` does not, and is one of the three modules in the override list
([`pyproject.toml:127-139`](../../pyproject.toml#L127-L139); line reference corrected
2026-07-31, it pointed at the ruff ignore list).

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
