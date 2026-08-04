"""External candlestick chart pane for the Panel UI (Task 10.1).

A self-contained component the user drives independently of the conversation:
symbol / period / bar controls + a Load button that fetches OHLCV (Drive-cache
first, IBKR on miss) and renders a HoloViews candlestick. STK only — the fetch tool
resolves STK conids (no sec_type parameter). Every failure path (IBKR offline,
empty DataFrame, unknown symbol, unexpected exception) surfaces an honest status
message and never crashes the session.

Data flow (verified 2026-07-24 — research doc
docs/panel/2026-07-24-candlestick-chart-pane-research.md):
  toolkit._cache.check / load(symbol, bar.upper(), period, end) return whether the
  bars are cached and the OHLCV DataFrame respectively (DatetimeIndex + lowercase
  open/high/low/close/volume columns — ibkr_core_mcp indicators.py:11,42). On a
  cache miss, toolkit.execute("fetch_market_data", {...}) fetches from IBKR and
  populates the parquet cache, returning only a human-readable SUMMARY string
  (claude_tools.py:1142) — the raw bars are read back from the cache.

`build_chart_object` (this module) builds the chart with `hvplot` from three separate
calls — `.hvplot.ohlc()` for the price row (wick Segments + body Rectangles; measured
2026-08-03: `type(df.hvplot.ohlc(...))` is an `Overlay` of exactly those two elements,
nothing else), `sma.hvplot.line()` for the SMA Curve overlaid on top of it, and
`.hvplot.bar()` for the volume row below — into a single `holoviews.Layout`. The
`import hvplot.pandas` below is load-bearing (see the comment at the import site for
why); `build_chart_pane` renders the resulting Layout through `pn.pane.HoloViews`,
whose `object` the tests assert against directly rather than poking Bokeh glyph
renderers. Design rationale for the hvplot/HoloViews rewrite
and the decisions cited by name in this module (D2, the 300px price-row height, the
linked_axes/shared_axes distinction): local plan archive,
docs/plans/2026-08-03-holoviews-charting-layer-design.md (git-ignored, not in this
repo — see CLAUDE.md Conventions).

`_get_toolkit` is imported lazily inside `_on_load` to avoid a panel_app <->
panel_chart import cycle: panel_app imports `build_chart_pane` at module top for its
session-root composition, so this module must never import panel_app at module top.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

# Imported for its side effects; MUST NOT be removed as an unused import. Two distinct
# effects, both measured 2026-08-03: importing `hvplot` at all registers the bokeh
# renderer (hv.Store.renderers, a dict, goes {} -> {'bokeh': BokehRenderer(...)}), which
# is why no hv.extension() call appears in this module; the `.pandas` suffix is what adds
# the DataFrame/Series `.hvplot` accessor used below — `import hvplot` alone does not.
import hvplot.pandas  # noqa: F401
import pandas as pd
import panel as pn
from ibkr_core_mcp import indicators

from claudia.panel_markdown import safe_markdown

log = logging.getLogger(__name__)

_UP_COLOR = "#26a69a"  # teal — close >= open
_DOWN_COLOR = "#ef5350"  # red — close < open

# Passed to hvplot.ohlc()'s bar_width= for the candle bodies only (build_chart_object) --
# the volume bars are a separate .hvplot.bar() call that takes no bar_width and uses
# hv.Bars' own 0.8 default instead (verified by reading both call sites, 2026-08-03).
# hvplot multiplies this fraction by np.min(np.diff(x)) -- the data's own minimum bar
# gap -- so 0.7 always leaves a visible gap between candles regardless of the selected
# bar size (1d/1h/30m). hvplot derives this from the data directly; no caller in this
# module computes a width itself.
_BODY_WIDTH_FRACTION = 0.7

# Overlay period for the moving average. Values come from ibkr_core_mcp.indicators --
# this repo renders, that repo computes (decision D2).
_SMA_PERIOD = 20

# Volume subplot height in px. The price row above has no explicit height set here, so it
# renders at holoviews' own ElementPlot default of 300px (measured 2026-08-03 via
# hv.render(...).select({"type": bokeh.plotting.figure})) rather than the 360px the old
# hand-built Bokeh figure used -- deliberate, user decision 2026-08-03, taking
# 300 + 120 = 420px total over restoring 360 for the price row.
_VOLUME_HEIGHT = 120


def build_chart_object(df: pd.DataFrame, title: str) -> Any:
    """Build the HoloViews chart object from an OHLCV DataFrame.

    Returns an `hv.Layout` of two stacked figures (`.cols(1)`): a price `hv.Overlay`
    of Segments (wicks) + Rectangles (bodies) + Curve (the SMA overlay) on top, and a
    volume `hv.Bars` subplot below. The Segments/Rectangles shape measured 2026-08-03
    via `type(df.hvplot.ohlc(...))`. The `Any` return isn't caused by
    holoviews: `df: pd.DataFrame` is already `Any` here, because pandas itself sits on
    mypy's ignore_missing_imports list (pyproject.toml) — `reveal_type(df)` is `Any`
    before `.hvplot` is even reached, so `df.hvplot` and everything chained off it stay
    `Any` regardless of whether holoviews/hvplot ship stubs (verified 2026-08-03: a
    nonexistent method chained onto `df.hvplot` raises no mypy error). Keep this
    function small for that reason: anything reached through an untyped third-party
    object is `Any` here, so this function gets no type checking at all.

    `df` is indexed by a DatetimeIndex with lowercase open/high/low/close/volume columns.
    Candle bodies are sized by hvplot from the data's own bar spacing — specifically
    `np.min(np.diff(x)) * bar_width` (hvplot/converter.py, `ohlc()`) — which is why this
    module does not compute a width itself. That is MIN, not the MEDIAN the old
    `_body_width_ms` helper used: the two agreed on uniform data and on the
    weekend-gap fixture that covered it, but diverged whenever the single smallest gap
    in the frame wasn't also the median gap (verified 2026-08-03, before deletion: three
    daily bars plus one trailing half-day bar gave hvplot ~30,240,000ms and
    `_body_width_ms` ~60,480,000ms — 2x apart). See commit a51b454 for the smear bug
    that made a hand-computed width necessary under raw Bokeh in the first place.
    (Several checked-in docs cite that fix as `794d7c0`; that hash is not a commit that
    exists in this repository — checked via `git cat-file -t` 2026-08-03. `a51b454` is
    what `git log -S _body_width_ms` actually finds.)
    """
    if len(df) == 1:
        # hvplot sizes candles from np.min(np.diff(x)); one row makes np.diff empty and
        # numpy raises "zero-size array to reduction operation minimum" (verified above,
        # 2026-08-03). Caught here so any caller gets an actionable message instead of a
        # numpy internals string. A 0-row frame does not reach this function either way --
        # _on_load's `df.empty` check returns before it calls any chart builder.
        raise ValueError("Cannot chart a single bar - need at least 2 bars.")
    candles = df.hvplot.ohlc(
        # y= pins the OHLC columns BY NAME. Required, not decorative: hvplot 0.12.2's
        # own docstring (hvplot/plotting/core.py) says the default (y=None) is
        # ["open", "high", "low", "close"], but converter.py's `ohlc()` actually does
        # `o, h, l, c = [col for col in data.columns if col != x][:4]` when y is None —
        # POSITIONAL, not by name. Verified 2026-08-03: with y= omitted and `volume`
        # moved before `open` in the DataFrame's column order, this silently charted
        # volume-vs-low and colored every candle red — no error raised anywhere. Do not
        # delete this as "redundant" on the strength of that docstring line. The web
        # reference IS accurate where it explains the mechanism in prose ("the first four
        # non-datetime columns correspond to the O, H, L and C components", scraped
        # 2026-08-03); it is only the `y` parameter line, read alone, that reads as a
        # name-based guarantee.
        y=["open", "high", "low", "close"],
        bar_width=_BODY_WIDTH_FRACTION,
        pos_color=_UP_COLOR,
        neg_color=_DOWN_COLOR,
    )
    # .rename is load-bearing, not cosmetic: hvplot labels a Series curve from
    # Series.name, and indicators.sma returns a Series named 'close' despite a docstring
    # promising 'sma_{period}' (verified 2026-08-03: `indicators.sma(df, 20).name ==
    # "close"` — its body is `df["close"].rolling(period).mean()`, which never renames).
    # Without it the overlay is labelled 'close' and collides with the price series.
    # Renaming here rather than upstream keeps the label a presentation concern (D2) and
    # avoids an API change for that function's other callers.
    sma = indicators.sma(df, _SMA_PERIOD).rename(f"sma_{_SMA_PERIOD}")
    price = (candles * sma.hvplot.line(color="orange")).opts(title=title)
    # .hvplot.bar keeps a continuous datetime axis (NOT a categorical FactorRange) -- a
    # categorical one would break x-range sharing with the price row regardless of which
    # knob is named for it (verified 2026-08-03: pairing a datetime element with a
    # categorical one under shared_axes=True yields distinct Range1d/FactorRange objects,
    # not a shared one).
    #
    # That sharing itself comes from HoloViews' own Layout `shared_axes` (default True),
    # NOT from Panel: `pn.pane.HoloViews(linked_axes=...)` makes no difference either way
    # (True and False both share; even a bare hv.render with no Panel pane at all shares),
    # while `.opts(shared_axes=False)` does turn it off (all verified 2026-08-03). Panel's
    # `linked_axes` links separate sibling panes within a Panel layout (its own param
    # doc) -- a mechanism this module never exercises, since only one HoloViews pane is
    # ever built here.
    #
    # Bar width verified 2026-08-03 at 0.8x the MINIMUM gap between x-values (bokeh
    # VBar.width via hv.render, checked on uniform 1D/1h/30min frames and an irregular
    # one) -- the same MIN-based pattern this function's own docstring above documents for
    # the candle bodies: hv.Bars' default `bar_width` style option is 0.8, scaled by
    # np.min(np.diff(x)) in holoviews/plotting/bokeh/chart.py's BarPlot.get_data. So the
    # volume row cannot smear the way the hand-built candle bodies once did.
    volume = df["volume"].hvplot.bar(height=_VOLUME_HEIGHT)
    return (price + volume).cols(1)


def build_chart_pane() -> pn.Column:
    """Return the self-contained candlestick pane.

    Layout: a control Row (symbol / period / bar / Load), a Markdown status line,
    and a HoloViews pane (empty until the first successful load). The Load button
    drives an async handler that resolves the process toolkit, reads the OHLCV
    bars from the Drive cache (fetching from IBKR on a miss), and reassigns the
    HoloViews pane's `object` to a fresh Layout. The toolkit is resolved at click time
    (not construction): the process singleton is built by panel_app's background
    session init, which may not have finished when this pane is composed.
    """
    # label= (not name=): panel 1.9 PendingDeprecationWarns on Widget.name, which
    # would break the suite's 1-warning gate — label is its supported replacement.
    symbol = pn.widgets.TextInput(label="Symbol", value="AAPL")
    period = pn.widgets.Select(
        label="Period", options=["1m", "3m", "6m", "1y", "2y"], value="6m"
    )
    bar = pn.widgets.Select(label="Bar", options=["1d", "1h", "30m"], value="1d")
    load_btn = pn.widgets.Button(label="Load chart", color="primary")
    status = safe_markdown("STK only. Enter a symbol and click **Load chart**.")
    # pn.pane.HoloViews, not pn.pane.Bokeh: the pane's `object` is the declarative
    # HoloViews Layout, which tests assert against directly instead of poking Bokeh glyph
    # renderers, and the pane generates widgets for HoloMap/DynamicMap key dimensions --
    # which the planned live-streaming surfaces will need. Requires no pn.extension()
    # call: holoviews is not among panel's extension names (verified 2026-08-03).
    #
    # linked_axes is NOT why the price and volume rows share an x-range -- that is
    # holoviews' own Layout `shared_axes` (default True), which applies with no Panel pane
    # involved at all. Panel's linked_axes links axes ACROSS separate panes in a Panel
    # layout, which this module never does. Measured 2026-08-03; kept only because it is
    # the documented default and harmless.
    chart = pn.pane.HoloViews(None, linked_axes=True)
    # Title of whatever is currently drawn, or None before the first success. A failed
    # load deliberately leaves the previous chart up (losing a chart to a typo is worse),
    # which means the title and the symbol box can disagree -- observed live 2026-08-03 as
    # a chart reading "AAPL 30m (1m)" beside an input reading "ZZQQXX". This is what lets
    # the failure message say which of the two the user is actually looking at.
    shown_label: str | None = None

    async def _on_load(event: Any) -> None:
        """Load bars for the entered symbol and rebuild the candlestick figure.

        Checks the parquet cache first and fetches on a miss — a multi-second IBKR wait,
        hence the spinner, which is cleared in `finally` on every path. An empty result and
        a failure are both reported honestly in the status line, and a failure additionally
        names the chart still on screen so a stale title cannot be mistaken for the symbol
        that was just requested.
        """
        nonlocal shown_label
        # loading spinner first — the fetch-on-miss path is a multi-second IBKR wait
        # (actionable-buttons research). Always cleared in the finally.
        load_btn.loading = True
        fetch_summary: str | None = None
        try:
            # Deferred import breaks the panel_app <-> panel_chart cycle (module docstring).
            from claudia.panel_app import _get_toolkit

            toolkit = _get_toolkit()
            sym = (symbol.value or "").strip().upper()  # value is str | None
            tf = bar.value.upper()
            end = str(date.today())  # matches ibkr_core_mcp _TODAY(); keys the cache

            # Blocking Drive/IBKR calls go through to_thread so the event loop (and the
            # loading spinner) stay responsive.
            if not await asyncio.to_thread(
                toolkit._cache.check, sym, tf, period.value, end
            ):
                status.object = f"Fetching {sym}…"
                # execute returns (text_result, None) -- ClaudeToolkit.execute's declared
                # signature is `-> tuple[str, None]`, the second slot a legacy figure
                # field that is always None. The text is the ONLY explanation of a failed
                # fetch: an unresolvable symbol returns "Could not resolve conid for X
                # (as STK). Is IBKR connected?" while the cache stays empty, so the
                # `load` below then raises a CacheMissError naming an internal cache key.
                # Discarding this text is why an unknown symbol used to surface
                # "No cached file for ZZQQXX_30M_1M_2026-08-03" (found live 2026-08-03).
                fetch_summary, _ = await asyncio.to_thread(
                    toolkit.execute,
                    "fetch_market_data",
                    {"symbol": sym, "period": period.value, "bar": bar.value},
                )
            df = await asyncio.to_thread(
                toolkit._cache.load, sym, tf, period.value, end
            )
            if df is None or df.empty:
                status.object = f"No data for {sym}."
                return
            label = f"{sym} {bar.value} ({period.value})"
            chart.object = build_chart_object(df, label)
            shown_label = label
            status.object = f"Loaded {len(df)} bars for {sym}."
        except Exception as exc:
            # symbol.value (not a try-local) so the message is safe even if the
            # failure fired before any local was bound (e.g. _get_toolkit raising).
            log.exception("Chart load failed for %s", symbol.value)
            failed = (symbol.value or "").strip().upper()
            # Attribute the fetch's own words rather than presenting them as the root
            # cause: when a fetch ran, its text is the most proximate explanation we have,
            # and in the pathological case where it reports success yet the load still
            # failed, quoting it makes that contradiction visible instead of hiding it.
            reason = f"fetch reported: {fetch_summary}" if fetch_summary else str(exc)
            # Both sources already punctuate their own sentences -- the fetch text ends
            # "Is IBKR connected?" and the 1-row guard ends "need at least 2 bars." --
            # so appending unconditionally produced "connected?." and "bars..".
            if not reason.endswith((".", "?", "!")):
                reason += "."
            still = f" Still showing {shown_label}." if shown_label else ""
            status.object = f"✕ Could not load {failed} — {reason}{still}"
        finally:
            load_btn.loading = False

    load_btn.on_click(_on_load)

    return pn.Column(
        pn.Row(symbol, period, bar, load_btn),
        status,
        chart,
        sizing_mode="stretch_both",
    )
