# Chart pane — continuous candles and a readable volume row: what the libraries guarantee

**Date:** 2026-09-25 · **For:** gaps #76 (candle continuity) and #75 (volume row) in
`docs/project-status.md` · **Status:** research only, nothing built. Every claim below is
either a verbatim quote from a vendor page (scraped copy in `.firecrawl/charting/`, git-ignored)
or a fact executed against the installed libraries on this date: **hvPlot 0.12.2, HoloViews
1.23.1, Bokeh 3.9.2** (the same three the chart pane renders with).

## The requirement (operator, 2026-09-25, verbatim)

> "To recap the market data chart needs: Continuous candles, matching industry standards.
> Volume: color match the candles, and continuity. make the volume proportion a better fit /
> non logarithmic."

"Continuity" for the volume row means it rides the same bar-sequence axis as the candles;
"non logarithmic" means a plain linear axis with human numbers instead of the `5.000e+5`
notation that reads like a log scale. The pane is for historical study (operator 2026-09-23),
so all of this is presentation; no data changes.

## What is on screen today, and why

`build_chart_object` (`claudia/panel_chart.py`) plots `df.hvplot.ohlc(...)` on the frame's
`DatetimeIndex`, so the x-axis is a continuous datetime scale and every weekend and holiday is
an empty stretch. The volume row is `df["volume"].hvplot.bar(height=_VOLUME_HEIGHT)`: one
colour, default axis formatter, on the same datetime axis. Both facts are by construction, not
by accident — the 2026-08-03 comment in that function records why the volume row must keep
the **same axis type** as the price row (a datetime element paired with a categorical one
under `shared_axes=True` yields distinct ranges, so the rows stop moving together).

## 1. The x axis can be the bar sequence — hvPlot

`hvplot.hvPlot.ohlc` reference (https://hvplot.holoviz.org/ref/api/manual/hvplot.hvPlot.ohlc.html,
scraped 2026-09-25):

> **x** string, optional — Field name to draw x coordinates from. If not specified, the index
> is used. Normally refers to date values.

> **bar_width**: number, optional — Bar width. Default is 0.5.

> By default `ohlc` will assume the `index` OR the first datetime column should be mapped to
> the x-axis and the first four non-datetime columns correspond to the O (open), H (high),
> L (low) and C (close) components.

**Executed:** an OHLC frame on a plain `RangeIndex` (0, 1, 2) renders as `Segment` + `Quad`
glyphs on a `Range1d` x-range — an integer axis is accepted, and because hvplot sizes bodies
from `np.min(np.diff(x))` (see the function's docstring), the body width becomes a constant
`bar_width` of one bar. On such an axis there is nothing between consecutive bars to leave a
gap in: continuity follows from the axis, for daily and intraday bars alike (overnight and
weekend gaps vanish the same way).

`hvplot.help('ohlc')` (executed) lists the option that keeps the date readable on hover:

> **hover_cols** : list or str, default=[] — Additional columns to add to the hover tool or
> 'all' which will include all columns (including indexes if `use_index=True`).

So the plotting frame carries the bar number as `x` and the date as an ordinary column named
in `hover_cols`; the tooltip shows the date while the axis counts bars.

## 2. Dates as tick labels on that axis — HoloViews

HoloViews *Customizing Plots* (https://holoviews.org/user_guide/Customizing_Plots.html, scraped
2026-09-25):

> The number and locations of ticks can be set in three main ways: Number of ticks … List of
> tick positions … List of tick positions and labels: A list of tuples of the form
> (position, label)

with the example `xticks=[(0, 'zero'), (50, 'fifty'), (100, 'one hundred')]`. And on formatters:

> Tick formatting works very differently in different backends, however the `xformatter` and
> `yformatter` options try to minimize these differences. Tick formatters may be defined in
> one of three formats: A classic format string such as `'%d'`, `'%.3f'` … A
> `bokeh.models.TickFormatter` in bokeh …

`hvplot.help('ohlc')` (executed) confirms hvPlot forwards both:

> **xticks/yticks/cticks** : int or list or np.ndarray or None — Ticks along x-axis and
> y-axis, as an integer, list of ticks positions, Numpy ndarray, or list of tuples of the tick
> positions and labels. Also accepts a Bokeh `Ticker` instance …

> **xformatter/yformatter** : str or bokeh.TickFormatter or None — Formatter for the x-axis
> and y-axis (accepts printf formatter, e.g. '%.3f', and bokeh TickFormatter).

Consequence for #76: tick positions are chosen bar numbers (the first bar of each month for
daily data, or every *k* bars) and each label is that bar's date, formatted as the current
axis formats it (`Jul 01`, `Aug 15`…). Every label is a real bar, never an interpolated date.

## 3. The volume axis in human numbers — Bokeh

`bokeh.models.NumeralTickFormatter.format` (docstring of the installed 3.9.2, executed) is a
numeral.js format string. From its own table:

| Number | Format | String |
| --- | --- | --- |
| 10000.23 | `'0,0'` | 10,000 |
| 1230974 | `'0.0a'` | 1.2m |
| 1460 | `'0 a'` | 1 k |
| -104000 | `'0a'` | -104k |

`yformatter=NumeralTickFormatter(format="0.0a")` on the volume row gives `500.0k`, `1.2m`; a
linear axis, no exponent. Tick density is a separate knob (`yticks=<int>` from § 2, or a
Bokeh `Ticker` with `desired_num_ticks`) — the current axis is not logarithmic, it is a
linear axis whose labels are wide, cramped into a short row.

## 4. Volume bars coloured like their candle — HoloViews

**Executed, not read:** an `hv.Bars` built with the colour as a value dimension —
`hv.Bars(df, kdims=["bar"], vdims=["volume", "colour"]).opts(color="colour")` — renders a
Bokeh `VBar` whose `fill_color` is `Field(field='color')`, i.e. **each bar takes its colour
from its own row** of the data source (columns `bar`, `volume`, `color`). So the volume row
can carry a colour column computed from the candle's own direction, using the same up/down
rule and the same two palette constants (`claudia/palette.py`) as the candle bodies, and the
two rows cannot disagree.

Open detail before building #75: hvPlot's `ohlc` decides "positive change" from close against
open; the exact comparator (strict or not, on an unchanged close) must be read from
`hvplot/converter.py` in the installed 0.12.2 and copied, so a doji is coloured the same in
both rows.

## 5. Design that follows, one change at a time

Order and method as the 2026-09-23 addendum set them: **#76 first, rendered and looked at,
then #75** (`feedback-a-fix-can-introduce-its-own-defect`).

**#76 — continuity — BUILT 2026-09-25 as described here** (`panel_chart._plot_frame`, `_date_ticks`, `build_chart_object`; the hover's text date lives on the wick renderer the tool is bound to, measured while building: the body Quad's source holds geometry only). Build a plotting frame from the cached one: `bar` = 0…n−1, `date` = the
old index, OHLCV columns unchanged; keep the `DatetimeIndex` frame for `_infer_bar_label`
(the title's "bars actually returned" check reads the index spacing). Price row:
`hvplot.ohlc(x="bar", y=[...], hover_cols=["date"], xticks=[(i, label), ...])`. Volume row on
the same integer `bar` key so the Layout's `shared_axes` keeps one `Range1d`. Body width
becomes the constant the docs describe. Tests: the rendered x-range is `Range1d` on both
rows; consecutive bars are one unit apart with no gap at a weekend fixture; every tick label
is the date of the bar at that position; hover carries the date.

**#75 — volume row — BUILT 2026-09-25 as described here** (the candle rule read from hvplot's converter: `open > close` is negative, a doji positive; `hover_tooltips` names the day). `hv.Bars` with a `colour` vdim from the candle rule; `yformatter`
`NumeralTickFormatter("0.0a")`; a taller row (a fixed height or a ratio of the price row —
the current `_VOLUME_HEIGHT` is the knob). Tests: `VBar.fill_color` is a `Field`; the colour
column equals the candle rule row by row; the y formatter is a `NumeralTickFormatter`.

## Sources

- hvPlot `ohlc` reference — https://hvplot.holoviz.org/ref/api/manual/hvplot.hvPlot.ohlc.html
  (`.firecrawl/charting/hvplot-ohlc.md`)
- HoloViews *Customizing Plots* — https://holoviews.org/user_guide/Customizing_Plots.html
  (`.firecrawl/charting/holoviews-customizing-plots.md`)
- Bokeh axes and formatters — https://docs.bokeh.org/en/latest/docs/user_guide/basic/axes.html,
  https://docs.bokeh.org/en/latest/docs/reference/models/formatters.html
  (`.firecrawl/charting/bokeh-axes.md`, `bokeh-formatters.md`)
- Executed against the venv on 2026-09-25: `hvplot.help('ohlc')`,
  `NumeralTickFormatter.format.__doc__`, an `hv.Bars` colour-vdim render, an `ohlc` render on a
  `RangeIndex`.
