"""Tests for claudia/panel_chart.py — the external candlestick chart pane (Task 10.1).

Two layers, both server-free:
  * build_chart_object — pure hvplot/HoloViews assembly, driven by a fixture OHLCV
    DataFrame (DatetimeIndex + lowercase columns, mirroring the real cache output).
    Returns a holoviews.Layout; assertions read its element data directly rather than
    poking Bokeh glyph renderers. Design rationale: local plan archive,
    docs/plans/archive/pnl-dashboard/2026-08-03-holoviews-charting-layer-design.md (git-ignored, not in this
    repo — see CLAUDE.md Conventions).
  * build_chart_pane / _on_load — the Panel component and its Load handler, exercised
    by grabbing the live Button's on_click callback (conftest _get_click_callback) and
    awaiting it with a patched claudia.panel_app._get_toolkit. No Panel server, no
    IBKR, no Drive.
"""

from pathlib import Path
from typing import Any

import pandas as pd
import panel as pn
import pytest

from claudia.panel_chart import build_chart_pane
from tests.conftest import _get_click_callback


def _sample_df() -> pd.DataFrame:
    """Four daily bars: rows 0-1 up (close >= open), rows 2-3 down (close < open)."""
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame(
        {
            "open": [10.0, 11.0, 20.0, 21.0],
            "high": [12.0, 13.0, 22.0, 23.0],
            "low": [9.0, 10.0, 18.0, 19.0],
            "close": [11.0, 12.0, 19.0, 20.0],  # up, up, down, down
            "volume": [100, 200, 300, 400],
        },
        index=idx,
    )


def _iter_tree(node):
    """Walk a Panel layout depth-first, yielding every object in it."""
    yield node
    for child in getattr(node, "objects", []):
        yield from _iter_tree(child)


def _first(pane, kind):
    """The first object in the tree matching `predicate`."""
    return next(n for n in _iter_tree(pane) if isinstance(n, kind))


def _button(pane):
    """The button in the pane whose label matches."""
    return _first(pane, pn.widgets.Button)


def _chart(pane):
    """The chart pane inside the built column."""
    return _first(pane, pn.pane.HoloViews)


def _status(pane):
    """The status markdown pane inside the built column."""
    return _first(pane, pn.pane.Markdown)


def _selects(pane):
    """[Period, Bar] in layout order."""
    return [n for n in _iter_tree(pane) if isinstance(n, pn.widgets.Select)]


def hv_title(obj):
    """The title opt on the price Overlay (the Layout itself carries none)."""
    import holoviews as hv

    price = obj.Overlay.I if isinstance(obj, hv.Layout) else obj
    return hv.Store.lookup_options("bokeh", price, "plot").kwargs.get("title") or ""


# ── shared helpers ────────────────────────────────────────────────────────────


def _ms(td: pd.Timedelta) -> float:
    """A datetime as epoch milliseconds, the unit the bokeh x-axis uses."""
    return float(td / pd.Timedelta(milliseconds=1))


# ── build_chart_pane (composition) ────────────────────────────────────────────


def test_build_chart_pane_has_controls_status_and_chart():
    """The pane ships controls, a status line and a chart placeholder before any data loads."""
    pane = build_chart_pane()
    assert isinstance(pane, pn.Column)
    nodes = list(_iter_tree(pane))
    assert len([n for n in nodes if isinstance(n, pn.widgets.TextInput)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.widgets.Select)]) == 2
    assert len([n for n in nodes if isinstance(n, pn.widgets.Button)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.pane.HoloViews)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.pane.Markdown)]) >= 1
    # empty placeholder until first load
    assert _chart(pane).object is None


# ── _on_load behavior ─────────────────────────────────────────────────────────


def _mock_toolkit(*, cached: bool, df):
    """A stub toolkit whose cache and history responses can be posed."""
    from unittest.mock import MagicMock

    tk = MagicMock()
    tk._cache.check.return_value = cached
    tk._cache.load.return_value = df
    tk.execute.return_value = ("summary", None)
    return tk


@pytest.mark.asyncio
async def test_on_load_cache_hit_renders_figure_without_fetching():
    """A cache hit renders straight from the cache and makes no IBKR call."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=True, df=_sample_df())
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    tk.execute.assert_not_called()  # cache hit — no IBKR fetch
    # Pin the cache-key contract: timeframe is uppercased ("1d" → "1D") and the
    # today-date is the 4th key. A regression that dropped either would still
    # render (mocks ignore args) but break the real cache key.
    from datetime import date

    today = str(date.today())
    tk._cache.check.assert_called_once_with("AAPL", "1D", "6m", today)
    tk._cache.load.assert_called_once_with("AAPL", "1D", "6m", today)
    assert [type(e).__name__ for e in _chart(pane).object] == ["Overlay", "Bars"]
    assert "Loaded 4 bars for AAPL" in _status(pane).object


@pytest.mark.asyncio
async def test_on_load_strips_and_uppercases_symbol():
    """The typed symbol is normalised before it is used as a cache or lookup key."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=False, df=_sample_df())
    pane = build_chart_pane()
    _first(pane, pn.widgets.TextInput).value = "  aapl "  # untrimmed, lowercase
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    _name, inputs = tk.execute.call_args.args
    assert inputs["symbol"] == "AAPL"  # normalized before the fetch
    assert "Loaded 4 bars for AAPL" in _status(pane).object


@pytest.mark.asyncio
async def test_on_load_cache_miss_fetches_then_loads():
    """A cache miss fetches, then renders what it fetched."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=False, df=_sample_df())
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    tk.execute.assert_called_once()
    name, inputs = tk.execute.call_args.args
    assert name == "fetch_market_data"
    assert inputs == {"symbol": "AAPL", "period": "6m", "bar": "1d"}
    tk._cache.load.assert_called_once()
    assert [type(e).__name__ for e in _chart(pane).object] == ["Overlay", "Bars"]


@pytest.mark.asyncio
async def test_on_load_empty_df_shows_honest_no_data_and_leaves_chart():
    """No data says so and leaves the previous chart standing rather than blanking it."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=True, df=pd.DataFrame())
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    assert "No data for AAPL" in _status(pane).object
    assert _chart(pane).object is None  # unchanged — no figure rendered
    assert _button(pane).loading is False


@pytest.mark.asyncio
async def test_on_load_exception_is_caught_as_honest_error():
    """A load failure becomes a visible error line, never an unhandled callback exception."""
    from unittest.mock import patch

    pane = build_chart_pane()
    btn = _button(pane)
    cb = _get_click_callback(btn)
    with patch("claudia.panel_app._get_toolkit", side_effect=RuntimeError("boom")):
        await cb(None)  # must not raise

    assert "✕ Could not load AAPL" in _status(pane).object
    assert "boom" in _status(pane).object
    assert _chart(pane).object is None
    assert btn.loading is False  # cleared in finally


@pytest.mark.asyncio
async def test_on_load_single_row_frame_is_honest_error_not_a_crash():
    """A 1-row cache hit reaches build_chart_object's own ValueError guard.

    Before the pane swap this guard had no production caller (nothing between it and
    the cache), so it was unreachable from the UI. _on_load now calls build_chart_object
    directly, so a 1-row DataFrame is a real path a user can hit -- this pins that it
    surfaces as the same honest status-line message every other _on_load failure uses,
    not a raw traceback or a crash.
    """
    from unittest.mock import patch

    tk = _mock_toolkit(cached=True, df=_sample_df().iloc[:1])
    pane = build_chart_pane()
    btn = _button(pane)
    cb = _get_click_callback(btn)
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)  # must not raise

    assert "✕ Could not load AAPL" in _status(pane).object
    assert "Cannot chart a single bar - need at least 2 bars." in _status(pane).object
    assert _chart(pane).object is None
    assert btn.loading is False  # cleared in finally


@pytest.mark.asyncio
async def test_on_load_sets_loading_during_and_clears_after():
    """The loading indicator is set for the duration of the load and cleared afterwards."""
    from unittest.mock import patch

    pane = build_chart_pane()
    btn = _button(pane)
    cb = _get_click_callback(btn)
    seen = {}

    def _capture_loading(*_a, **_k):
        """Record the loading flag when read, so the during-load state can be asserted."""
        seen["loading"] = btn.loading
        return _sample_df()

    tk = _mock_toolkit(cached=True, df=_sample_df())
    tk._cache.load.side_effect = _capture_loading
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    assert seen["loading"] is True  # spinner on during the blocking load
    assert btn.loading is False  # cleared after


# ── build_chart_object (HoloViews) ────────────────────────────────────────────


def _price(obj):
    """The price Overlay (candles + SMA).

    Accepts either a bare Overlay or a Layout. build_chart_object has returned a Layout
    unconditionally since the volume subplot was added, so the bare-Overlay branch below
    no longer fires for any call in this file -- kept anyway because it is still the
    structurally correct behavior for a bare Overlay, not because anything today
    exercises it.

    Dispatches on TYPE, not hasattr: HoloViews' dynamic attribute access answers
    `hasattr(overlay, "Overlay")` with True on a bare Overlay too, resolving to an EMPTY
    `:Overlay` rather than the Overlay itself (verified 2026-08-03 directly against a
    bare hv.Overlay: `hasattr(ov, "Overlay")` is True, `ov.Overlay` is an empty
    `:Overlay`, `isinstance(ov, hv.Layout)` is correctly False) -- hasattr cannot tell
    the two shapes apart, isinstance can.

    An earlier version of this docstring pointed to a hasattr-dispatching run of every
    build_chart_object test below as evidence: all of them failed on the empty element.
    Re-run 2026-08-03 after the volume subplot landed: every test in this section now
    passes under hasattr-dispatch too, because build_chart_object no longer ever hands
    this function a bare Overlay to get wrong. The isinstance/hasattr choice stopped
    being observable in this file's tests, not stopped being correct -- see the
    direct-Overlay check above for why it still is. (This replaces a claim invalidated
    by the same kind of change its own last sentence warned about -- no count here
    either, for the same reason.)
    """
    import holoviews as hv

    return obj.Overlay.I if isinstance(obj, hv.Layout) else obj


def _rects(obj):
    """The candle-body element."""
    return _price(obj).Rectangles.I


def _body_width_bars_of(obj) -> float:
    """The candle body width, in bars, taken from the built figure (gap #76: the x axis is
    the bar sequence, so a width is a fraction of one bar, not a span of milliseconds)."""
    d = _rects(obj).data
    return float(d["ubound"].iloc[0] - d["lbound"].iloc[0])


def test_build_chart_object_has_wicks_and_bodies():
    """The figure carries both the wick segments and the candle bodies."""
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df(), "AAPL 1d (6m)")
    names = [type(e).__name__ for e in _price(obj)]
    assert "Segments" in names  # wicks
    assert "Rectangles" in names  # bodies
    assert len(_rects(obj).data) == 4  # one body per bar
    assert len(_price(obj).Segments.I.data) == 4  # one wick per bar


def test_build_chart_object_body_width_is_070_of_one_bar():
    # hvplot derives the width from the data's own bar spacing (np.min(np.diff(x)) *
    # bar_width, see build_chart_object's docstring). Until gap #76 (2026-09-25) x was the
    # datetime index, so this read 0.7 x 24h on the daily fixture; on the bar-sequence axis
    # the spacing is a constant one bar, so the width is a constant 0.7 bar.
    """Body width is 70% of one bar, which is what stops candles touching."""
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_sample_df(), "T")
    assert _body_width_bars_of(layout) == pytest.approx(0.7)


def test_build_chart_object_body_width_is_the_same_at_every_bar_size():
    # The smear regression (a51b454, not 794d7c0 -- see build_chart_object's docstring)
    # restated for the bar-sequence axis (gap #76): 30m, 1h and daily candles are all one
    # bar apart on that axis, so their bodies are all 0.7 bar wide — none can smear into
    # its neighbour, whatever the clock spacing. (Until 2026-09-25 this asserted strictly
    # narrower bodies for finer bars, which was the datetime axis's own arithmetic.)
    """Body width is 0.7 bar at every bar size, so no size can smear."""
    from claudia.panel_chart import build_chart_object

    def width(freq):
        """The candle body width from the built figure, in bars."""
        idx = pd.date_range("2024-01-01 09:30", periods=6, freq=freq)
        df = pd.DataFrame(
            {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0},
            index=idx,
        )
        return _body_width_bars_of(build_chart_object(df, "T"))

    assert width("30min") == pytest.approx(0.7)
    assert width("1h") == pytest.approx(0.7)
    assert width("1D") == pytest.approx(0.7)


def test_build_chart_object_colors_up_and_down_bodies():
    # hvplot encodes the partition as ONE dim expression, not two glyphs. The fixture is
    # up, up, down, down -- so applying the expression must yield teal, teal, red, red.
    """Up and down candles get their own colours."""
    import holoviews as hv

    from claudia.palette import DOWN_COLOR, UP_COLOR
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df(), "T")
    rects = _rects(obj)
    color = hv.Store.lookup_options("bokeh", rects, "style").kwargs["color"]
    assert list(color.apply(rects)) == [UP_COLOR, UP_COLOR, DOWN_COLOR, DOWN_COLOR]


def test_build_chart_object_carries_the_title():
    """The requested title reaches the figure."""
    import holoviews as hv

    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df(), "AAPL 1d (6m)")
    title = hv.Store.lookup_options("bokeh", _price(obj), "plot").kwargs.get("title")
    assert title == "AAPL 1d (6m)"


def test_build_chart_object_is_column_order_independent():
    # hvplot's own ohlc(x=None, y=None, ...) binds OHLC columns BY POSITION when y is
    # omitted (hvplot/converter.py: `o, h, l, c = [c for c in data.columns if c != x][:4]`).
    # build_chart_object pins y=["open","high","low","close"] specifically to defeat
    # that -- this test is what would fail if that y= were ever "cleaned up" as
    # redundant. Move volume before open/high/low/close (a real risk: nothing in the
    # cache contract fixes column order) and require identical geometry and colors.
    """Columns are bound by name — hvplot binds some positionally, which plots wrong series."""
    import holoviews as hv

    from claudia.panel_chart import build_chart_object

    canonical = _sample_df()
    reordered = canonical[["volume", "open", "high", "low", "close"]]

    canon_obj = build_chart_object(canonical, "T")
    reord_obj = build_chart_object(reordered, "T")
    canon_rects = _rects(canon_obj)
    reord_rects = _rects(reord_obj)

    # The column VALUES survive either way -- .data keeps all five named columns whatever
    # gets bound as kdims -- so this first assert passes even against the broken builder.
    # It is here to rule out a mangled frame, not to catch the binding bug.
    assert reord_rects.data[["open", "close"]].equals(canon_rects.data[["open", "close"]])
    # THIS is the assert that catches it: without y=, reordering yields
    # ['lbound','volume','ubound','low'] instead of ['lbound','open','ubound','close'].
    assert [str(k) for k in reord_rects.kdims] == [str(k) for k in canon_rects.kdims]

    canon_color = hv.Store.lookup_options("bokeh", canon_rects, "style").kwargs["color"]
    reord_color = hv.Store.lookup_options("bokeh", reord_rects, "style").kwargs["color"]
    assert list(reord_color.apply(reord_rects)) == list(canon_color.apply(canon_rects))
    # _body_width_bars_of takes the Overlay (it re-derives Rectangles.I via _price/_rects
    # internally), not an already-extracted Rectangles element -- passing canon_rects/
    # reord_rects here would raise (Rectangles has no further .Rectangles to descend into).
    assert _body_width_bars_of(reord_obj) == pytest.approx(_body_width_bars_of(canon_obj))


def test_build_chart_object_body_width_ignores_an_irregular_clock_gap():
    # hvplot's own width formula is `np.min(np.diff(x)) * bar_width` (converter.py,
    # verified 2026-08-03). On the datetime axis a trailing half-day bar made the min gap
    # 12h against a 24h median, and this test pinned the min-based result (12h x 0.7).
    # On the bar-sequence axis (gap #76) every gap is one bar, so the same fixture yields
    # 0.7 bar — an irregular clock gap can no longer narrow every body on the chart.
    """An irregular clock gap changes nothing: every body is 0.7 bar."""
    from claudia.panel_chart import build_chart_object

    idx = pd.to_datetime(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-03 12:00"], format="mixed"
    )
    df = pd.DataFrame(
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0},
        index=idx,
    )

    assert _body_width_bars_of(build_chart_object(df, "T")) == pytest.approx(0.7)


def test_build_chart_object_rejects_single_row_frame():
    # hvplot sizes candles from np.min(np.diff(x)); a 1-row frame gives np.diff an empty
    # array and numpy raises "zero-size array to reduction operation minimum". 0 rows and
    # 2 rows are both fine. We convert it to an honest failure rather than letting a numpy
    # internals message reach the chart pane's status line.
    """A one-bar frame is refused rather than rendered with a nonsense width."""
    from claudia.panel_chart import build_chart_object

    with pytest.raises(ValueError, match="at least 2 bars"):
        build_chart_object(_sample_df().iloc[:1], "T")


def test_build_chart_object_accepts_two_row_frame():
    """Two bars is the smallest frame that has a spacing to measure, and is accepted."""
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df().iloc[:2], "T")
    assert len(_rects(obj).data) == 2


def _long_df(n: int = 40) -> pd.DataFrame:
    """A frame long enough for a 20-period SMA to produce real values."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1D")
    close = [10.0 + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in close],
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [100.0 + i for i in range(n)],
        },
        index=idx,
    )


def test_the_price_figure_carries_candles_and_nothing_else():
    """No indicator is drawn on the price figure (operator, 2026-09-23).

    A 20-period SMA used to be overlaid on every chart, hard-coded as `_SMA_PERIOD = 20` with
    no setting behind it. The pane is for studying historical candles; an overlay nobody chose
    is noise, and indicators come back only once their setup is understood and chosen. The
    price overlay must hold exactly the two elements `.hvplot.ohlc()` builds: wicks
    (Segments) and bodies (Rectangles).
    """
    from claudia import panel_chart
    from claudia.panel_chart import build_chart_object

    price = _price(build_chart_object(_long_df(), "T"))
    assert sorted(type(e).__name__ for e in price) == ["Rectangles", "Segments"]
    assert not hasattr(panel_chart, "_SMA_PERIOD")


def test_build_chart_object_adds_a_volume_subplot():
    """Volume is drawn as its own row beneath price."""
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_long_df(), "T")
    assert [type(e).__name__ for e in layout] == ["Overlay", "Bars"]
    assert list(layout.Bars.I.dimension_values("volume")) == pytest.approx(
        _long_df()["volume"].tolist()
    )


def test_volume_subplot_keeps_a_continuous_x_axis():
    # x-range sharing between the two rows comes from HoloViews' Layout shared_axes (see
    # test_layout_figures_share_one_x_range for why it isn't Panel's linked_axes).
    # shared_axes still needs matching range kinds to actually share: a categorical
    # (FactorRange) volume axis paired with the datetime candle axis would silently break
    # the sync (verified 2026-08-03: pairing a datetime element with a categorical one
    # under shared_axes=True yields two distinct range objects, not one shared one).
    """The volume row keeps the same continuous axis as price — since gap #76 the numeric
    bar-sequence axis (`test_candles_sit_on_the_bar_sequence_with_no_gap_across_a_weekend`
    asserts the two rows share one Range1d)."""
    import holoviews as hv
    from bokeh.models import FactorRange

    from claudia.panel_chart import build_chart_object

    fig = hv.render(build_chart_object(_long_df(), "T").Bars.I, backend="bokeh")
    assert not isinstance(fig.x_range, FactorRange)


def test_layout_figures_share_one_x_range():
    # The zoom-sync claim, asserted rather than trusted -- but not the mechanism first
    # attributed to it here. pn.pane.HoloViews(linked_axes=...) makes NO difference to
    # whether the two figures share an x_range (verified 2026-08-03: True and False both
    # share, and even a bare hv.render with no Panel pane at all shares). The real knob
    # is HoloViews' own Layout `shared_axes` (default True): `.opts(shared_axes=False)`
    # is what actually turns sharing off, proven below. `linked_axes=True` stays on the
    # pane because that pane is how this chart ships in the app, not because it does
    # anything measurable in this test.
    """Both rows share one x-range, so zooming price zooms volume with it."""
    import holoviews as hv
    import panel as pn
    from bokeh.plotting import figure as bk_figure

    from claudia.panel_chart import build_chart_object

    pane = pn.pane.HoloViews(build_chart_object(_long_df(), "T"), linked_axes=True)
    # Model.select returns Iterable[Model] (bokeh 3.9.2's own annotation, via
    # core.query.find) -- a generator, not a list -- so len() needs the list() first.
    figs = [f for f in pane.get_root().select({"type": bk_figure}) if isinstance(f, bk_figure)]
    assert len(figs) == 2
    assert figs[0].x_range is figs[1].x_range

    # The actual cause, isolated: a FRESH object (not the one already rendered above --
    # re-rendering the same object/pane a second time is not a supported pattern and was
    # observed to corrupt the first pane's own already-built models) with
    # shared_axes=False, rendered with no Panel pane at all, does NOT share -- proving
    # shared_axes (not linked_axes) is what the assertion above is really exercising.
    unshared_obj = build_chart_object(_long_df(), "T").opts(shared_axes=False)
    unshared = hv.render(unshared_obj, backend="bokeh")
    unshared_figs = list(unshared.select({"type": bk_figure}))
    assert len(unshared_figs) == 2
    assert unshared_figs[0].x_range is not unshared_figs[1].x_range


def test_build_chart_object_stacks_price_over_volume():
    # (price + volume) alone lays the two figures out SIDE BY SIDE by default -- a
    # 2-element Layout's default column count is 4 (verified 2026-08-03: monkeypatching
    # Layout.cols to a no-op and inspecting layout._max_cols on the result). .cols(1) is
    # what forces the stacked, single-column layout instead: removing it changes
    # GridPlot.children row/col positions from [(0,0),(1,0)] (stacked) to [(0,0),(0,1)]
    # (side by side) -- verified both ways before writing this assertion.
    """Price sits above volume in the layout, one column wide."""
    import holoviews as hv
    from bokeh.plotting import figure as bk_figure

    from claudia.panel_chart import _VOLUME_HEIGHT, build_chart_object

    fig = hv.render(build_chart_object(_long_df(), "T"), backend="bokeh")
    figs = [(row, col, child) for child, row, col in fig.children if isinstance(child, bk_figure)]
    assert sorted((row, col) for row, col, _ in figs) == [(0, 0), (1, 0)]
    volume_fig = next(child for row, col, child in figs if row == 1)
    assert volume_fig.height == _VOLUME_HEIGHT


# ── failure-path messaging (2026-08-03 live-test findings) ────────────────────


@pytest.mark.asyncio
async def test_on_load_unknown_symbol_reports_the_fetch_reason_not_the_cache_key():
    """An unresolvable symbol must surface IBKR's explanation, not a cache key.

    Found live 2026-08-03: loading a nonexistent symbol showed
    "No cached file for ZZQQXX_30M_1M_2026-08-03" -- the cache layer's own key,
    which tells a user nothing. fetch_market_data already returns the real reason
    ("Could not resolve conid for ZZQQXX (as STK). Is IBKR connected?"); _on_load
    was discarding it and then reporting the downstream CacheMissError instead.
    """
    from unittest.mock import patch

    tk = _mock_toolkit(cached=False, df=None)
    tk.execute.return_value = (
        "Could not resolve conid for ZZQQXX (as STK). Is IBKR connected?",
        None,
    )
    tk._cache.load.side_effect = RuntimeError("No cached file for ZZQQXX_30M_1M_2026-08-03")
    pane = build_chart_pane()
    _first(pane, pn.widgets.TextInput).value = "ZZQQXX"
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    msg = _status(pane).object
    assert "Could not resolve conid for ZZQQXX" in msg
    assert "No cached file" not in msg  # the cache key must not reach the user
    assert _button(pane).loading is False


@pytest.mark.asyncio
async def test_on_load_failure_names_the_chart_still_displayed():
    """A failed load leaves the previous chart up; the status must say so.

    Found live 2026-08-03: after a failed load the chart still read
    "AAPL 30m (1m)" while the symbol box read "ZZQQXX" -- title and input
    disagreeing with nothing to reconcile them. Keeping the chart is deliberate
    (losing it on a typo is worse), so the status line names what is on screen.
    """
    from unittest.mock import patch

    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))

    tk_ok = _mock_toolkit(cached=True, df=_sample_df())
    with patch("claudia.panel_app._get_toolkit", return_value=tk_ok):
        await cb(None)
    assert _chart(pane).object is not None

    tk_bad = _mock_toolkit(cached=False, df=None)
    tk_bad.execute.return_value = ("Could not resolve conid for ZZQQXX (as STK).", None)
    tk_bad._cache.load.side_effect = RuntimeError("No cached file for ZZQQXX")
    _first(pane, pn.widgets.TextInput).value = "ZZQQXX"
    with patch("claudia.panel_app._get_toolkit", return_value=tk_bad):
        await cb(None)

    msg = _status(pane).object
    assert "ZZQQXX" in msg
    assert "Still showing AAPL 1d (6m)" in msg
    assert _chart(pane).object is not None  # chart deliberately NOT cleared


@pytest.mark.asyncio
async def test_on_load_first_ever_failure_has_no_still_showing_clause():
    """Nothing has been charted yet, so there is nothing to still be showing."""
    from unittest.mock import patch

    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", side_effect=RuntimeError("boom")):
        await cb(None)

    msg = _status(pane).object
    assert "boom" in msg
    assert "Still showing" not in msg


@pytest.mark.asyncio
async def test_on_load_failure_message_does_not_double_punctuate():
    """Both reason sources already end their own sentence.

    The fetch text ends "Is IBKR connected?" and build_chart_object's guard ends
    "need at least 2 bars." -- appending a period unconditionally produced
    "connected?." and "bars..".
    """
    from unittest.mock import patch

    tk = _mock_toolkit(cached=False, df=None)
    tk.execute.return_value = ("Could not resolve conid for Z (as STK). Is IBKR connected?", None)
    tk._cache.load.side_effect = RuntimeError("No cached file")
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)
    assert "?." not in _status(pane).object

    tk2 = _mock_toolkit(cached=True, df=_sample_df().iloc[:1])
    pane2 = build_chart_pane()
    cb2 = _get_click_callback(_button(pane2))
    with patch("claudia.panel_app._get_toolkit", return_value=tk2):
        await cb2(None)
    assert ".." not in _status(pane2).object


# ── bar-size disclosure (2026-08-03 matrix finding) ───────────────────────────


def _idx(*, minutes: float, n: int = 10, first_gap: float | None = None):
    """A DatetimeIndex with a given regular spacing, optionally an odd first gap."""
    stamps = [pd.Timestamp("2026-01-05 09:30")]
    for i in range(1, n):
        step = first_gap if (i == 1 and first_gap is not None) else minutes
        stamps.append(stamps[-1] + pd.Timedelta(minutes=step))
    return pd.DatetimeIndex(stamps)


def test_infer_bar_label_recognises_the_standard_bars():
    """Each standard bar size is inferred from the actual spacing between rows."""
    from claudia.panel_chart import _infer_bar_label

    assert _infer_bar_label(_idx(minutes=30)) == "30m"
    assert _infer_bar_label(_idx(minutes=60)) == "1h"
    assert _infer_bar_label(_idx(minutes=1440)) == "1d"


def test_infer_bar_label_uses_median_not_min():
    """An hourly series opens with a half-hour bar at the RTH boundary.

    Measured live 2026-08-03: every "1h" request returns min gap 30m, median 60m.
    A min-based rule would call all five of them "30m" and raise a false alarm on
    data that is genuinely hourly.
    """
    from claudia.panel_chart import _infer_bar_label

    assert _infer_bar_label(_idx(minutes=60, first_gap=30)) == "1h"


def test_infer_bar_label_is_robust_to_weekend_gaps():
    # Daily bars with a 72h weekend gap: the median stays 24h.
    """A weekend gap does not fool the inference into reporting a larger bar."""
    from claudia.panel_chart import _infer_bar_label

    idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"])
    assert _infer_bar_label(idx) == "1d"


def test_infer_bar_label_marks_nonstandard_spacing_approximate():
    """Live 3m/30m had a 120m median -- not a bar size the UI offers."""
    from claudia.panel_chart import _infer_bar_label

    assert _infer_bar_label(_idx(minutes=120)) == "~2h"


@pytest.mark.asyncio
async def test_on_load_discloses_when_ibkr_returns_a_coarser_bar():
    """1y/30m returns DAILY bars -- verified live 2026-08-03 as byte-identical to 1y/1d.

    The pane used to title that "AAPL 30m (1y)" over daily candles and say nothing.
    """
    from unittest.mock import patch

    daily = pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
        index=_idx(minutes=1440, n=30),
    )
    tk = _mock_toolkit(cached=True, df=daily)
    pane = build_chart_pane()
    _selects(pane)[1].value = "30m"  # [Period, Bar]
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    status = _status(pane).object
    assert "IBKR returned 1d bars, not the 30m requested" in status
    title = hv_title(_chart(pane).object)
    assert "1d" in title and "requested 30m" in title


@pytest.mark.asyncio
async def test_on_load_says_nothing_when_the_bar_size_matches():
    """No warning is shown when IBKR returned the bar size that was asked for."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=True, df=_sample_df())  # daily fixture, 1d requested
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with patch("claudia.panel_app._get_toolkit", return_value=tk):
        await cb(None)

    status = _status(pane).object
    assert "Loaded 4 bars for AAPL" in status
    assert "IBKR returned" not in status
    assert "requested" not in hv_title(_chart(pane).object)


# ── gap #21 (2026-09-25): a Load click is a real tool run, and leaves a real record ─────


def _rows(db: Path) -> list[tuple[str, str, str, str, str]]:
    """Every `tool` row in the store, read back raw: the DB is what an audit reads."""
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows: list[tuple[str, str, str, str, str]] = conn.execute(
            "SELECT session_id, role, tool_name, content, tool_result_json FROM messages "
            "WHERE role = 'tool' ORDER BY id"
        ).fetchall()
    return rows


@pytest.mark.asyncio
async def test_a_cache_miss_load_leaves_a_ui_button_tool_row_before_the_cache_is_read(tmp_path):
    """The pane's fetch used to call `toolkit.execute` directly and leave nothing. Now it
    goes through `record_and_execute`: a `tool` row stamped `ui_button`, carrying the
    session id, is on disk BEFORE the cache is read back — so a bar-cache entry can always
    be explained, and a failing render cannot cost the record."""
    from unittest.mock import patch

    from claudia.conversation_store import ConversationStore
    from claudia.tool_record import UI_BUTTON_ORIGIN

    db = tmp_path / "chart.db"
    store = ConversationStore(db)
    store.create_session("s-chart")
    tk = _mock_toolkit(cached=False, df=_sample_df())
    rows_when_loaded: list[int] = []
    df = _sample_df()

    def _load(*_a: object, **_k: object) -> pd.DataFrame:
        """Stand in for the cache read, noting how many tool rows exist at that moment."""
        rows_when_loaded.append(len(_rows(db)))
        return df

    tk._cache.load.side_effect = _load
    pane = build_chart_pane(session_id="s-chart")
    cb = _get_click_callback(_button(pane))
    with (
        patch("claudia.panel_app._get_toolkit", return_value=tk),
        patch("claudia.panel_app.session_store", return_value=store),
    ):
        await cb(None)

    rows = _rows(db)
    assert len(rows) == 1, rows
    session_id, role, tool_name, content, result_json = rows[0]
    assert (session_id, role, tool_name, content) == (
        "s-chart",
        "tool",
        "fetch_market_data",
        UI_BUTTON_ORIGIN,
    )
    assert result_json == '"summary"', result_json  # the text, as the model loop writes it
    assert rows_when_loaded == [1], "the row must exist before the cache is read"
    assert [type(e).__name__ for e in _chart(pane).object] == ["Overlay", "Bars"]


@pytest.mark.asyncio
async def test_a_raising_fetch_is_recorded_and_still_reported_honestly(tmp_path):
    """An attempted call is a fact: the row records the failure, the status line still says
    what went wrong, and the chart stays empty."""
    from unittest.mock import patch

    from claudia.conversation_store import ConversationStore

    db = tmp_path / "chart.db"
    store = ConversationStore(db)
    store.create_session("s-chart")
    tk = _mock_toolkit(cached=False, df=_sample_df())
    tk.execute.side_effect = RuntimeError("IBKR down")
    pane = build_chart_pane(session_id="s-chart")
    cb = _get_click_callback(_button(pane))
    with (
        patch("claudia.panel_app._get_toolkit", return_value=tk),
        patch("claudia.panel_app.session_store", return_value=store),
    ):
        await cb(None)

    rows = _rows(db)
    assert len(rows) == 1 and "RuntimeError: IBKR down" in rows[0][4], rows
    assert "IBKR down" in str(_first(pane, pn.pane.Markdown).object) or any(
        "IBKR down" in str(getattr(m, "object", "")) for m in _iter_tree(pane)
    )
    assert _chart(pane).object is None
    tk._cache.load.assert_not_called()


@pytest.mark.asyncio
async def test_a_pane_without_a_session_still_fetches_and_writes_nothing():
    """The seam's contract for a caller with nowhere to write: the tool runs, no row."""
    from unittest.mock import patch

    tk = _mock_toolkit(cached=False, df=_sample_df())
    pane = build_chart_pane()
    cb = _get_click_callback(_button(pane))
    with (
        patch("claudia.panel_app._get_toolkit", return_value=tk),
        patch("claudia.panel_app.session_store") as lookup,
    ):
        await cb(None)
    tk.execute.assert_called_once()
    lookup.assert_not_called()
    assert [type(e).__name__ for e in _chart(pane).object] == ["Overlay", "Bars"]


# ── gap #76 (2026-09-25): continuous candles on the bar sequence, dates as tick labels ─────


def _ohlcv(index: pd.DatetimeIndex) -> pd.DataFrame:
    """A well-formed OHLCV frame on `index`, values rising so every candle is drawable."""
    n = len(index)
    base = pd.Series(range(n), index=index, dtype=float) + 100.0
    return pd.DataFrame(
        {"open": base, "high": base + 2, "low": base - 2, "close": base + 1, "volume": 1000 + base},
        index=index,
    )


def _bokeh_figures(obj) -> list[Any]:
    """The Bokeh figures a HoloViews object renders to, the candle (price) figure first."""
    import holoviews as hv
    from bokeh.plotting import figure as BokehFigure

    figs = [m for m in hv.render(obj).references() if isinstance(m, BokehFigure)]
    has_candles = [
        any(getattr(r, "glyph", None).__class__.__name__ == "Quad" for r in f.renderers)
        for f in figs
    ]
    return [f for f, c in zip(figs, has_candles, strict=True) if c] + [
        f for f, c in zip(figs, has_candles, strict=True) if not c
    ]


def _price_axis(layout):
    """The price row's x axis, rendered."""
    return _bokeh_figures(layout)[0].xaxis[0]


def test_candles_sit_on_the_bar_sequence_with_no_gap_across_a_weekend():
    """Gap #76: the x axis counts bars, so a weekend is nothing, not an empty stretch. Both
    rows share one numeric range (a categorical axis would split them, measured 2026-08-03)."""
    from claudia.panel_chart import build_chart_object

    index = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"])
    layout = build_chart_object(_ohlcv(index), "t")
    figs = _bokeh_figures(layout)
    assert len(figs) == 2
    assert [type(f.x_range).__name__ for f in figs] == ["Range1d", "Range1d"]
    assert figs[0].x_range is figs[1].x_range, "the volume row must ride the candles' axis"
    bodies = next(r for r in figs[0].renderers if r.glyph.__class__.__name__ == "Quad")
    centres = [
        (lft + rgt) / 2
        for lft, rgt in zip(
            bodies.data_source.data["left"], bodies.data_source.data["right"], strict=True
        )
    ]
    assert centres == [0.0, 1.0, 2.0, 3.0, 4.0], centres  # Friday → Monday is one step


def test_tick_labels_are_the_dates_of_real_bars_at_month_starts():
    """Every tick is a bar that exists, labelled with that bar's own date; daily data ticks at
    the first bar of each month."""
    from claudia.panel_chart import build_chart_object

    index = pd.bdate_range("2026-06-15", "2026-09-25")
    layout = build_chart_object(_ohlcv(index), "t")
    axis = _price_axis(layout)
    positions = list(axis.ticker.ticks)
    assert positions, "an explicit tick list is expected"
    assert all(0 <= p < len(index) for p in positions)
    first_of_month = [i for i in range(1, len(index)) if index[i].month != index[i - 1].month]
    assert positions == first_of_month, (positions, first_of_month)
    for pos in positions:
        assert axis.major_label_overrides[pos] == index[pos].strftime("%b %Y")


def test_intraday_ticks_fall_on_day_changes():
    """Intraday bars tick at the first bar of each day, labelled with that day."""
    from claudia.panel_chart import build_chart_object

    days = [
        pd.date_range(f"2026-09-{d} 09:30", f"2026-09-{d} 15:30", freq="30min")
        for d in (23, 24, 25)
    ]
    index = days[0].append(days[1]).append(days[2])
    layout = build_chart_object(_ohlcv(index), "t")
    axis = _price_axis(layout)
    starts = [len(days[0]), len(days[0]) + len(days[1])]
    assert list(axis.ticker.ticks) == starts
    assert [axis.major_label_overrides[p] for p in starts] == ["Sep 24", "Sep 25"]


def test_ticks_are_thinned_to_a_readable_number_on_long_ranges():
    """Two years of daily bars have 24 month starts; at most eight are labelled, every one of
    them still a real month start (thinned, never interpolated)."""
    from claudia.panel_chart import build_chart_object

    index = pd.bdate_range("2024-09-25", "2026-09-25")
    layout = build_chart_object(_ohlcv(index), "t")
    axis = _price_axis(layout)
    positions = list(axis.ticker.ticks)
    assert 2 <= len(positions) <= 8, positions
    for pos in positions:
        assert index[pos].month != index[pos - 1].month, (
            "a thinned tick must still be a month start"
        )


def test_a_short_range_with_no_month_boundary_still_gets_ticks():
    """Three weeks inside one month: no month start to tick at, so evenly spaced real bars."""
    from claudia.panel_chart import build_chart_object

    index = pd.bdate_range("2026-09-02", "2026-09-22")
    layout = build_chart_object(_ohlcv(index), "t")
    axis = _price_axis(layout)
    positions = list(axis.ticker.ticks)
    assert 2 <= len(positions) <= 8 and positions[0] == 0
    for pos in positions:
        assert axis.major_label_overrides[pos] == index[pos].strftime("%b %d")


def test_hover_carries_the_bar_date_as_text():
    """On a bar-sequence axis the tooltip must still say which day a candle is — as text,
    since Bokeh prints a raw datetime column as epoch milliseconds."""
    from claudia.panel_chart import build_chart_object

    index = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])
    layout = build_chart_object(_ohlcv(index), "t")
    fig = _bokeh_figures(layout)[0]
    hover = next(t for t in fig.tools if type(t).__name__ == "HoverTool")
    assert ("date", "@date") in hover.tooltips, hover.tooltips
    # The tooltip reads from the renderer the hover tool is bound to (the wicks; measured
    # 2026-09-25 — the body Quad's source holds geometry only), so that is where the text
    # date must be, or the browser shows "???".
    bound = hover.renderers[0]
    assert list(bound.data_source.data["date"]) == ["2026-07-01", "2026-07-02", "2026-07-03"]


# ── gap #75 (2026-09-25): the volume row — colour by its candle, human numbers, taller ─────


def _mixed_ohlcv() -> pd.DataFrame:
    """Up, down, doji, down, up — one of each shape, so a colour rule cannot pass by luck."""
    index = pd.bdate_range("2026-09-21", periods=5)
    return pd.DataFrame(
        {
            "open": [10.0, 12.0, 11.0, 11.5, 10.0],
            "high": [12.5, 12.5, 11.5, 12.0, 11.5],
            "low": [9.5, 10.5, 10.5, 10.0, 9.5],
            "close": [12.0, 11.0, 11.0, 10.0, 11.0],
            "volume": [120_000.0, 450_000.0, 1_230_000.0, 80_000.0, 300_000.0],
        },
        index=index,
    )


def _volume_figure(layout):
    """The rendered volume row."""
    return _bokeh_figures(layout)[1]


def test_volume_bars_take_the_colour_of_their_own_candle():
    """Each volume bar is filled with its candle's colour, from the same up/down rule hvplot
    applies to the bodies — read off both rendered rows, so the two cannot disagree. hvplot's
    rule (converter.py, read 2026-09-25) is `open > close` → negative; a doji is positive."""
    from claudia.palette import DOWN_COLOR, UP_COLOR
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_mixed_ohlcv(), "t")
    price, volume = _bokeh_figures(layout)[:2]
    quad = next(r for r in price.renderers if r.glyph.__class__.__name__ == "Quad")
    vbar = next(r for r in volume.renderers if r.glyph.__class__.__name__ == "VBar")
    assert vbar.glyph.fill_color.__class__.__name__ == "Field", vbar.glyph.fill_color
    body_colours = list(quad.data_source.data["color"])
    bar_colours = list(vbar.data_source.data[vbar.glyph.fill_color.field])
    assert bar_colours == body_colours
    assert bar_colours == [UP_COLOR, DOWN_COLOR, UP_COLOR, DOWN_COLOR, UP_COLOR]


def test_volume_axis_reads_in_human_numbers_on_a_linear_scale():
    """`1.2m`, `450.0k` — not `1.230e+6`; a linear axis with few ticks, not a log one."""
    from claudia.panel_chart import build_chart_object

    fig = _volume_figure(build_chart_object(_mixed_ohlcv(), "t"))
    formatter = fig.yaxis[0].formatter
    assert type(formatter).__name__ == "NumeralTickFormatter" and formatter.format == "0.0a"
    assert type(fig.y_scale).__name__ == "LinearScale"
    assert fig.yaxis[0].ticker.desired_num_ticks <= 4


def test_volume_row_has_room_and_a_stated_proportion_of_the_price_row():
    """The row is drawn at `_VOLUME_HEIGHT`, the price row at `_PRICE_HEIGHT`, and the
    proportion is the one the module states — a knob, so the operator can tune it."""
    from claudia.panel_chart import _PRICE_HEIGHT, _VOLUME_HEIGHT, build_chart_object

    price, volume = _bokeh_figures(build_chart_object(_mixed_ohlcv(), "t"))[:2]
    assert (price.height, volume.height) == (_PRICE_HEIGHT, _VOLUME_HEIGHT)
    assert _VOLUME_HEIGHT >= 150, "the row the operator called too small was 120 px"
    assert 0.35 <= _VOLUME_HEIGHT / _PRICE_HEIGHT <= 0.5


def test_volume_bars_are_as_wide_as_the_candle_bodies():
    """Volume bars sit under their candles at the same width, so the rows read as one."""
    from claudia.panel_chart import _BODY_WIDTH_FRACTION, build_chart_object

    volume = _volume_figure(build_chart_object(_mixed_ohlcv(), "t"))
    vbar = next(r for r in volume.renderers if r.glyph.__class__.__name__ == "VBar")
    assert vbar.glyph.width == pytest.approx(_BODY_WIDTH_FRACTION)


def test_volume_hover_names_the_date_and_a_readable_volume():
    """Hovering a volume bar says which day and how much, with thousands separators."""
    from claudia.panel_chart import build_chart_object

    volume = _volume_figure(build_chart_object(_mixed_ohlcv(), "t"))
    hover = next(t for t in volume.tools if type(t).__name__ == "HoverTool")
    fields = [f for f, _spec in hover.tooltips]
    assert "date" in fields and "volume" in fields, hover.tooltips
    assert any("{0,0}" in spec for _f, spec in hover.tooltips), hover.tooltips


def test_volume_row_is_exactly_as_long_as_the_price_row():
    """Operator, 2026-09-25, on the first render of #75: "Volume chart should = candle chart
    length. It's a standard and logic standard." The rows are sized by the same constant, and
    this reads the two rendered widths rather than trusting the option (hvplot's default
    gave the price row 700 px, HoloViews' default gave the new `hv.Bars` row 300 px)."""
    from claudia.panel_chart import _CHART_WIDTH, build_chart_object

    price, volume = _bokeh_figures(build_chart_object(_mixed_ohlcv(), "t"))[:2]
    assert price.width == volume.width == _CHART_WIDTH
    assert price.sizing_mode == volume.sizing_mode
