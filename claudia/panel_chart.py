"""External candlestick chart pane for the Panel UI (Task 10.1).

A self-contained component the user drives independently of the conversation:
symbol / period / bar controls + a Load button that fetches OHLCV (Drive-cache
first, IBKR on miss) and renders a Bokeh candlestick. STK only — the fetch tool
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

The candlestick recipe (a Bokeh high/low `segment` for the wicks plus two `vbar`
glyphs — teal up, red down — partitioned by close >= open) is verified-live in the
research doc. `_get_toolkit` is imported lazily inside `_on_load` to avoid a
panel_app <-> panel_chart import cycle: panel_app imports `build_chart_pane` at
module top for its session-root composition, so this module must never import
panel_app at module top.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import pandas as pd
import panel as pn
from bokeh.plotting import figure

from claudia.panel_markdown import safe_markdown

log = logging.getLogger(__name__)

_UP_COLOR = "#26a69a"  # teal — close >= open
_DOWN_COLOR = "#ef5350"  # red — close < open
_WICK_COLOR = "#666"

# Body = 0.7x the bar spacing, so there is always a visible gap between candles.
_BODY_WIDTH_FRACTION = 0.7
# <2-row frames have no defined spacing; fall back to a daily-scale body.
_FALLBACK_BAR_WIDTH_MS = 12 * 60 * 60 * 1000


def _body_width_ms(index: pd.DatetimeIndex) -> float:
    """Candle-body width in ms, scaled to the data's own bar spacing.

    A Bokeh vbar `width` is in x-axis data units (ms on a datetime axis), so it
    MUST track the actual bar interval: a fixed daily-scale width turns 1h/30m
    candles into an overlapping smear (12x / 24x their spacing). Deriving the
    width from the DataFrame's median spacing keeps this a pure function of the
    data yet correct for every selectable bar size. Median (not mean) is robust
    to weekend / overnight gaps; the <2-row branch covers the degenerate frame
    where spacing is undefined.
    """
    if len(index) >= 2:
        med = pd.Series(index).diff().dropna().median()
        return float(med / pd.Timedelta(milliseconds=1)) * _BODY_WIDTH_FRACTION
    return float(_FALLBACK_BAR_WIDTH_MS)


def build_candlestick_figure(df: pd.DataFrame, title: str) -> figure:
    """Build a Bokeh candlestick figure from an OHLCV DataFrame.

    Pure helper — no server / IBKR / cache access, unit-testable with a fixture
    DataFrame. `df` is indexed by a DatetimeIndex with lowercase
    open/high/low/close columns. Up bars (close >= open) are teal, down bars red;
    wicks are drawn as high-low segments.
    """
    inc = df["close"] >= df["open"]
    width = _body_width_ms(df.index)
    # The inline call-arg ignore below is because x_axis_type is a bokeh
    # construction-only option (FigureOptions, _figure.py:1143), not a Plot model
    # property, so bokeh's property-typed init doesn't enumerate it and mypy flags
    # it; the runtime and the official docs both accept it (verified live
    # 2026-07-24). Targeted to this one call, not a module/blanket ignore.
    p = figure(  # type: ignore[call-arg]
        x_axis_type="datetime",
        sizing_mode="stretch_width",
        height=360,
        title=title,
    )
    p.segment(df.index, df["high"], df.index, df["low"], color=_WICK_COLOR)
    p.vbar(
        df.index[inc],
        width,
        df["open"][inc],
        df["close"][inc],
        fill_color=_UP_COLOR,
        line_color=_UP_COLOR,
    )
    p.vbar(
        df.index[~inc],
        width,
        df["open"][~inc],
        df["close"][~inc],
        fill_color=_DOWN_COLOR,
        line_color=_DOWN_COLOR,
    )
    return p


def build_chart_pane() -> pn.Column:
    """Return the self-contained candlestick pane.

    Layout: a control Row (symbol / period / bar / Load), a Markdown status line,
    and a Bokeh pane (empty until the first successful load). The Load button
    drives an async handler that resolves the process toolkit, reads the OHLCV
    bars from the Drive cache (fetching from IBKR on a miss), and reassigns the
    Bokeh pane's `object` to a fresh figure. The toolkit is resolved at click time
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
    chart = pn.pane.Bokeh(None)

    async def _on_load(event: Any) -> None:
        """Load bars for the entered symbol and rebuild the candlestick figure.

        Checks the parquet cache first and fetches on a miss — a multi-second IBKR wait,
        hence the spinner, which is cleared in `finally` on every path. An empty result and
        a failure are both reported honestly in the status line rather than leaving the
        previous chart up as if it were current.
        """
        # loading spinner first — the fetch-on-miss path is a multi-second IBKR wait
        # (actionable-buttons research). Always cleared in the finally.
        load_btn.loading = True
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
                # execute returns a SUMMARY string and populates the cache; an
                # IBKR-offline / unknown-symbol miss leaves the cache empty and the
                # load below raises, caught as an honest error.
                await asyncio.to_thread(
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
            chart.object = build_candlestick_figure(
                df, f"{sym} {bar.value} ({period.value})"
            )
            status.object = f"Loaded {len(df)} bars for {sym}."
        except Exception as exc:
            # symbol.value (not a try-local) so the message is safe even if the
            # failure fired before any local was bound (e.g. _get_toolkit raising).
            log.exception("Chart load failed for %s", symbol.value)
            status.object = f"✕ Could not load {(symbol.value or '').strip().upper()}: {exc}"
        finally:
            load_btn.loading = False

    load_btn.on_click(_on_load)

    return pn.Column(
        pn.Row(symbol, period, bar, load_btn),
        status,
        chart,
        sizing_mode="stretch_both",
    )
