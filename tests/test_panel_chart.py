"""Tests for claudia/panel_chart.py — the external candlestick chart pane (Task 10.1).

Two layers, both server-free:
  * build_candlestick_figure — pure Bokeh glyph assembly, driven by a fixture OHLCV
    DataFrame (DatetimeIndex + lowercase columns, mirroring the real cache output).
  * build_chart_pane / _on_load — the Panel component and its Load handler, exercised
    by grabbing the live Button's on_click callback (conftest _get_click_callback) and
    awaiting it with a patched claudia.panel_app._get_toolkit. No Panel server, no
    IBKR, no Drive.
"""

import pandas as pd
import panel as pn
import pytest
from bokeh.models import Segment, VBar
from bokeh.models.annotations import Title
from bokeh.models.renderers import GlyphRenderer
from bokeh.plotting import figure

from claudia.panel_chart import (
    _DOWN_COLOR,
    _FALLBACK_BAR_WIDTH_MS,
    _UP_COLOR,
    _body_width_ms,
    build_candlestick_figure,
    build_chart_pane,
)
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
    yield node
    for child in getattr(node, "objects", []):
        yield from _iter_tree(child)


def _first(pane, kind):
    return next(n for n in _iter_tree(pane) if isinstance(n, kind))


def _button(pane):
    return _first(pane, pn.widgets.Button)


def _chart(pane):
    return _first(pane, pn.pane.Bokeh)


def _status(pane):
    return _first(pane, pn.pane.Markdown)


# ── build_candlestick_figure (pure) ──────────────────────────────────────────


def _glyph_renderers(fig):
    return [r for r in fig.renderers if isinstance(r, GlyphRenderer)]


def test_build_candlestick_figure_returns_datetime_figure_with_title():
    from bokeh.models import DatetimeAxis

    fig = build_candlestick_figure(_sample_df(), "AAPL 1d (6m)")
    assert isinstance(fig, figure)
    assert isinstance(fig.xaxis[0], DatetimeAxis)
    assert isinstance(fig.title, Title)
    assert fig.title.text == "AAPL 1d (6m)"


def test_build_candlestick_figure_has_segment_and_two_vbars():
    fig = build_candlestick_figure(_sample_df(), "T")
    renderers = _glyph_renderers(fig)
    segments = [r for r in renderers if isinstance(r.glyph, Segment)]
    vbars = [r for r in renderers if isinstance(r.glyph, VBar)]
    assert len(segments) == 1  # wicks
    assert len(vbars) == 2  # up + down bodies
    assert len(fig.renderers) >= 3


def test_build_candlestick_figure_partitions_up_and_down_rows():
    fig = build_candlestick_figure(_sample_df(), "T")
    vbars = [r for r in _glyph_renderers(fig) if isinstance(r.glyph, VBar)]
    up = next(r for r in vbars if r.glyph.fill_color == _UP_COLOR)
    down = next(r for r in vbars if r.glyph.fill_color == _DOWN_COLOR)

    # Two rows in each partition.
    assert len(up.data_source.data[up.glyph.x]) == 2
    assert len(down.data_source.data[down.glyph.x]) == 2

    # vbar is (x, width, top=open, bottom=close): the up partition carries the two
    # up rows' opens (10, 11), the down partition the two down rows' opens (20, 21).
    assert sorted(up.data_source.data[up.glyph.top]) == [10.0, 11.0]
    assert sorted(down.data_source.data[down.glyph.top]) == [20.0, 21.0]


# ── body-width scaling (the fixed-12h-smear fix) ──────────────────────────────


def _ms(td: pd.Timedelta) -> float:
    return td / pd.Timedelta(milliseconds=1)


def test_body_width_scales_with_bar_spacing():
    day = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    hour = pd.to_datetime(["2024-01-01 09:00", "2024-01-01 10:00", "2024-01-01 11:00"])
    half = pd.to_datetime(["2024-01-01 09:00", "2024-01-01 09:30", "2024-01-01 10:00"])

    assert _body_width_ms(day) == pytest.approx(_ms(pd.Timedelta(hours=24)) * 0.7)
    assert _body_width_ms(hour) == pytest.approx(_ms(pd.Timedelta(hours=1)) * 0.7)
    assert _body_width_ms(half) == pytest.approx(_ms(pd.Timedelta(minutes=30)) * 0.7)
    # The fixed-12h bug is gone: intraday bodies are strictly narrower than daily,
    # so 1h/30m candles no longer overlap into a smear.
    assert _body_width_ms(half) < _body_width_ms(hour) < _body_width_ms(day)


def test_body_width_median_is_robust_to_weekend_gaps():
    # Thu, Fri, Mon, Tue — the Fri→Mon 72h gap must NOT inflate the body width;
    # the median spacing stays 24h (mean would be pulled to 40h).
    idx = pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"])
    assert _body_width_ms(idx) == pytest.approx(_ms(pd.Timedelta(hours=24)) * 0.7)


def test_body_width_single_row_uses_fallback():
    assert _body_width_ms(pd.to_datetime(["2024-01-01"])) == float(_FALLBACK_BAR_WIDTH_MS)


def test_build_candlestick_figure_vbar_width_matches_spacing():
    # The daily fixture's spacing is 24h, so both vbar glyphs carry a 0.7x24h body.
    fig = build_candlestick_figure(_sample_df(), "T")
    vbars = [r for r in _glyph_renderers(fig) if isinstance(r.glyph, VBar)]
    expected = _ms(pd.Timedelta(hours=24)) * 0.7
    for r in vbars:
        assert r.glyph.width == pytest.approx(expected)


# ── build_chart_pane (composition) ────────────────────────────────────────────


def test_build_chart_pane_has_controls_status_and_chart():
    pane = build_chart_pane()
    assert isinstance(pane, pn.Column)
    nodes = list(_iter_tree(pane))
    assert len([n for n in nodes if isinstance(n, pn.widgets.TextInput)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.widgets.Select)]) == 2
    assert len([n for n in nodes if isinstance(n, pn.widgets.Button)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.pane.Bokeh)]) == 1
    assert len([n for n in nodes if isinstance(n, pn.pane.Markdown)]) >= 1
    # empty placeholder until first load
    assert _chart(pane).object is None


# ── _on_load behavior ─────────────────────────────────────────────────────────


def _mock_toolkit(*, cached: bool, df):
    from unittest.mock import MagicMock

    tk = MagicMock()
    tk._cache.check.return_value = cached
    tk._cache.load.return_value = df
    tk.execute.return_value = ("summary", None)
    return tk


@pytest.mark.asyncio
async def test_on_load_cache_hit_renders_figure_without_fetching():
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
    assert isinstance(_chart(pane).object, figure)
    assert "Loaded 4 bars for AAPL" in _status(pane).object


@pytest.mark.asyncio
async def test_on_load_strips_and_uppercases_symbol():
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
    assert isinstance(_chart(pane).object, figure)


@pytest.mark.asyncio
async def test_on_load_empty_df_shows_honest_no_data_and_leaves_chart():
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
async def test_on_load_sets_loading_during_and_clears_after():
    from unittest.mock import patch

    pane = build_chart_pane()
    btn = _button(pane)
    cb = _get_click_callback(btn)
    seen = {}

    def _capture_loading(*_a, **_k):
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
    """The price Overlay (candles, and later the SMA).

    Accepts either a bare Overlay or a Layout, so these tests do not need rewriting
    when a later task wraps the Overlay in a Layout.

    Dispatches on TYPE, not hasattr: HoloViews' dynamic attribute access answers
    `hasattr(overlay, "Overlay")` with True on a bare Overlay and returns an EMPTY
    `:Overlay`, so a hasattr check silently resolves to an element with no data.
    Verified 2026-08-03 by running every build_chart_object test below against a
    hasattr-dispatching version of this helper: each one fails on that empty element,
    with AssertionError, KeyError('ubound'/'color') or TypeError depending on how far
    it gets -- never an IndexError. No count is given deliberately: an earlier version
    of this docstring said "the 5 tests below" and was invalidated by the same commit
    that wrote it, which added two more.
    """
    import holoviews as hv

    return obj.Overlay.I if isinstance(obj, hv.Layout) else obj


def _rects(obj):
    """The candle-body element."""
    return _price(obj).Rectangles.I


def _body_width_ms_of(obj) -> float:
    d = _rects(obj).data
    return (d["ubound"].iloc[0] - d["lbound"].iloc[0]) / pd.Timedelta(milliseconds=1)


def test_build_chart_object_has_wicks_and_bodies():
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df(), "AAPL 1d (6m)")
    names = [type(e).__name__ for e in _price(obj)]
    assert "Segments" in names  # wicks
    assert "Rectangles" in names  # bodies
    assert len(_rects(obj).data) == 4  # one body per bar
    assert len(_price(obj).Segments.I.data) == 4  # one wick per bar


def test_build_chart_object_body_width_is_070_of_bar_spacing():
    # Parity with _body_width_ms: hvplot derives the width from the data's own bar
    # spacing, so 0.7 x 24h for the daily fixture. NOT exactly equal to
    # Timedelta(hours=24)*0.7 -- float rounding puts it fractionally under -- hence approx.
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_sample_df(), "T")
    expected = _ms(pd.Timedelta(hours=24)) * 0.7
    assert _body_width_ms_of(layout) == pytest.approx(expected)


def test_build_chart_object_body_width_tracks_intraday_spacing():
    # The smear regression (a51b454, not 794d7c0 -- see build_chart_object's docstring)
    # restated for the new engine: 30m candles must be strictly narrower than 1h, which
    # must be strictly narrower than daily.
    from claudia.panel_chart import build_chart_object

    def width(freq):
        idx = pd.date_range("2024-01-01 09:30", periods=6, freq=freq)
        df = pd.DataFrame(
            {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0},
            index=idx,
        )
        return _body_width_ms_of(build_chart_object(df, "T"))

    assert width("30min") == pytest.approx(_ms(pd.Timedelta(minutes=30)) * 0.7)
    assert width("1h") == pytest.approx(_ms(pd.Timedelta(hours=1)) * 0.7)
    assert width("30min") < width("1h") < width("1D")


def test_build_chart_object_colors_up_and_down_bodies():
    # hvplot encodes the partition as ONE dim expression, not two glyphs. The fixture is
    # up, up, down, down -- so applying the expression must yield teal, teal, red, red.
    import holoviews as hv

    from claudia.panel_chart import _DOWN_COLOR, _UP_COLOR, build_chart_object

    obj = build_chart_object(_sample_df(), "T")
    rects = _rects(obj)
    color = hv.Store.lookup_options("bokeh", rects, "style").kwargs["color"]
    assert list(color.apply(rects)) == [_UP_COLOR, _UP_COLOR, _DOWN_COLOR, _DOWN_COLOR]


def test_build_chart_object_carries_the_title():
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
    # _body_width_ms_of takes the Overlay (it re-derives Rectangles.I via _price/_rects
    # internally), not an already-extracted Rectangles element -- passing canon_rects/
    # reord_rects here would raise (Rectangles has no further .Rectangles to descend into).
    assert _body_width_ms_of(reord_obj) == pytest.approx(_body_width_ms_of(canon_obj))


def test_build_chart_object_body_width_uses_min_not_median_spacing():
    # hvplot's own width formula is `np.min(np.diff(x)) * bar_width` (converter.py,
    # verified 2026-08-03) -- MIN, not the MEDIAN _body_width_ms uses. They happen to
    # agree on test_body_width_median_is_robust_to_weekend_gaps' fixture (min and
    # median are both 24h there), so that fixture does not exercise the difference.
    # This one does: a trailing half-day bar makes the min gap 12h while the median
    # gap stays 24h. Assert what hvplot ACTUALLY does (min-based), not parity with the
    # old per-bar-size helper -- they are genuinely different statistics and are not
    # expected to agree here.
    from claudia.panel_chart import _body_width_ms, build_chart_object

    idx = pd.to_datetime(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-03 12:00"], format="mixed"
    )
    df = pd.DataFrame(
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0},
        index=idx,
    )

    hv_width = _body_width_ms_of(build_chart_object(df, "T"))
    assert hv_width == pytest.approx(_ms(pd.Timedelta(hours=12)) * 0.7)
    # Restate the divergence explicitly: the old helper would have used the median gap
    # (24h) here, not the min (12h) -- these are not expected to be the same value.
    assert _body_width_ms(idx) == pytest.approx(_ms(pd.Timedelta(hours=24)) * 0.7)
    assert hv_width != pytest.approx(_body_width_ms(idx))


def test_build_chart_object_rejects_single_row_frame():
    # hvplot sizes candles from np.min(np.diff(x)); a 1-row frame gives np.diff an empty
    # array and numpy raises "zero-size array to reduction operation minimum". 0 rows and
    # 2 rows are both fine. We convert it to an honest failure rather than letting a numpy
    # internals message reach the chart pane's status line.
    from claudia.panel_chart import build_chart_object

    with pytest.raises(ValueError, match="at least 2 bars"):
        build_chart_object(_sample_df().iloc[:1], "T")


def test_build_chart_object_accepts_two_row_frame():
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df().iloc[:2], "T")
    assert len(_rects(obj).data) == 2
