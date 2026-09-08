# Research: external candlestick chart pane (Phase 10) — Panel/Bokeh direct

Scope confirmed by the user 2026-07-24: **"the chart pane is the functional deliverable
(not deferred-restyle layout polish), and it uses Panel/Bokeh directly — it is the goal."**
NO inline-in-chat charts. Memory: `project-phase10-external-charts`. All findings below are
verified against the installed package / local source (probe: `scratchpad/` inline runs).

---

## 1. Charting stack — NO new dependency

`bokeh 3.9.1` is ALREADY installed (Panel dependency); `hvplot`/`holoviews` are NOT. A
candlestick is drawn with Bokeh glyphs directly — **no hvplot needed** (Panel-native,
no-unnecessary-deps). Verified-live recipe:

```python
from bokeh.plotting import figure
inc = df["close"] >= df["open"]
# Bokeh vbar `width` is the FULL body width in x-axis DATA units (ms on a datetime
# axis) — NOT a half-width, and NOT a fixed pixel width. It must therefore scale
# with the bar spacing: a fixed 12h body is fine for 1d bars but is 12x the spacing
# for 1h and 24x for 30m, so intraday candles overlap into an unreadable smear.
# SHIPPED (794d7c0): derive it from the data's own median spacing so the helper
# stays a pure function of the DataFrame yet is correct for every bar size.
w = (df.index.to_series().diff().dropna().median() / pd.Timedelta("1ms")) * 0.7
p = figure(x_axis_type="datetime", sizing_mode="stretch_width", height=360,
           title=f"{symbol} {bar} ({period})")
p.segment(df.index, df["high"], df.index, df["low"], color="#666")          # wicks
p.vbar(df.index[inc],  w, df["open"][inc],  df["close"][inc],
       fill_color="#26a69a", line_color="#26a69a")                          # up bars
p.vbar(df.index[~inc], w, df["open"][~inc], df["close"][~inc],
       fill_color="#ef5350", line_color="#ef5350")                         # down bars
```

> **Correction (2026-07-24, code-quality review):** the first cut used a fixed
> `w = 12*60*60*1000` described as a "half-width"; both terms were wrong. Bokeh's
> `vbar width` is the full body width in data units, and a fixed value smears the
> selectable `1h`/`30m` options. The shipped `_body_width_ms` (median-spacing ×0.7,
> `<2`-row fallback) is the correct recipe above.

Embed in Panel via `pane = pn.pane.Bokeh(p)`; **refresh** by reassigning
`pane.object = new_figure` (verified — no error, updates in place; this is the Load-button
mechanism). Panel's websocket pushes the new figure to the browser.

## 2. OHLCV data flow — cache is the source of truth

**Critical:** `fetch_market_data` (the agent tool, `claude_tools.py:90`) returns a
human-readable SUMMARY, **not raw bars** — the raw OHLCV DataFrame is cached to Drive as
parquet. The chart reads the DataFrame from the cache:

- `GDriveCache.check(symbol, timeframe, period, end) -> bool` (`cache.py:251`) — is it cached?
- `GDriveCache.load(symbol, timeframe, period, end) -> pd.DataFrame` (`cache.py:263`).
- The DataFrame is indexed by a **DatetimeIndex** (`claude_tools.py` uses `df.index[0].date()`)
  with **lowercase** columns `open/high/low/close/volume` (verified: `indicators.py:11,42`
  do `df["close"]`, `df["high"]`, `df["low"]`).
- `timeframe` = `bar.upper()` (e.g. `"1D"`), `period`/`end` as passed to the tool.
- `toolkit._cache` is the live `GDriveCache` (constructor-set, `claude_tools.py:1029`) — same
  sanctioned reach-in as Tasks 5.3/5.5.

**Load-on-demand flow:** on the Load button →
1. `_get_toolkit()` (process singleton — build lazily at click time, NOT at pane
   construction: the toolkit is built in `_init_session`'s background init, which may not
   have finished when `_build_session_root` runs).
2. If `not toolkit._cache.check(sym, tf, period, end)` → `toolkit.execute("fetch_market_data",
   {"symbol":…, "period":…, "bar":…})` to populate the cache (returns a summary string;
   blocking → `asyncio.to_thread`). Requires IBKR connected — on failure it returns an error
   string, surface it in the pane.
3. `df = toolkit._cache.load(sym, tf, period, end)` (blocking → `to_thread`).
4. Build the Bokeh figure (§1) → `pane.object = fig`.
Every failure path (IBKR offline, empty df, bad symbol) → honest message in a status area,
never a crash. STK only (the tool resolves STK conids — no sec_type param; note in UI).

## 3. Controls (verified-live widgets)

- `pn.widgets.TextInput(name="Symbol", value="AAPL")`
- `pn.widgets.Select(name="Period", options=["1m","3m","6m","1y","2y"], value="6m")`
  (the tool takes a freeform lowercase-unit string, e.g. `"6m"`, `"1y"`, `"30d"` — presets
  cover the common cases; freeform can come later).
- `pn.widgets.Select(name="Bar", options=["1d","1h","30m"], value="1d")`
- `pn.widgets.Button(label="Load chart", color="primary")` — `loading=True` during the
  fetch/load (the `loading` spinner from the actionable-buttons research — multi-second IBKR
  waits), reset in `finally`.
- A `pn.pane.Markdown` status line for errors / "loading {sym}…".

## 4. Placement (function-first; refinable in restyle)

`_build_session_root` currently returns `pn.Column(pn.Row(*indicators), chat,
sizing_mode="stretch_both")`. Compose the chart as a dedicated pane BESIDE the chat:
`pn.Row(chat_column, chart_pane, sizing_mode="stretch_both")` — chat left, chart right —
OR a `pn.Tabs(("Chat", …), ("Chart", …))` if horizontal space is tight. Exact split ratio /
tabs-vs-split is a restyle refinement; the functional requirement is a working, refreshable
candlestick pane that the user drives independently of the conversation. Recommend the
side-by-side Row for the first cut (both visible at once).

## 5. Scope boundary (function-first)

IN: symbol/period/bar controls, Load button, candlestick render, honest errors, refresh.
DEFERRED to restyle/future (collect, don't build): volume subplot, moving-average/indicator
overlays, crosshair/hover tooltips, zoom-sync, multi-symbol, non-STK (FUT/OPT) charts,
theme-matched colors, freeform period input. Note them; keep Task 10.1 to the working pane.
