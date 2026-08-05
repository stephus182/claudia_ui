"""Tests for claudia/panel_chart.py — the external candlestick chart pane (Task 10.1).

Two layers, both server-free:
  * build_chart_object — pure hvplot/HoloViews assembly, driven by a fixture OHLCV
    DataFrame (DatetimeIndex + lowercase columns, mirroring the real cache output).
    Returns a holoviews.Layout; assertions read its element data directly rather than
    poking Bokeh glyph renderers. Design rationale: local plan archive,
    docs/plans/2026-08-03-holoviews-charting-layer-design.md (git-ignored, not in this
    repo — see CLAUDE.md Conventions).
  * build_chart_pane / _on_load — the Panel component and its Load handler, exercised
    by grabbing the live Button's on_click callback (conftest _get_click_callback) and
    awaiting it with a patched claudia.panel_app._get_toolkit. No Panel server, no
    IBKR, no Drive.
"""

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
    return td / pd.Timedelta(milliseconds=1)


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


def _body_width_ms_of(obj) -> float:
    """The candle body width, in milliseconds, taken from the built figure."""
    d = _rects(obj).data
    return (d["ubound"].iloc[0] - d["lbound"].iloc[0]) / pd.Timedelta(milliseconds=1)


def test_build_chart_object_has_wicks_and_bodies():
    """The figure carries both the wick segments and the candle bodies."""
    from claudia.panel_chart import build_chart_object

    obj = build_chart_object(_sample_df(), "AAPL 1d (6m)")
    names = [type(e).__name__ for e in _price(obj)]
    assert "Segments" in names  # wicks
    assert "Rectangles" in names  # bodies
    assert len(_rects(obj).data) == 4  # one body per bar
    assert len(_price(obj).Segments.I.data) == 4  # one wick per bar


def test_build_chart_object_body_width_is_070_of_bar_spacing():
    # hvplot derives the width from the data's own bar spacing (np.min(np.diff(x)) *
    # bar_width, see build_chart_object's docstring), so 0.7 x 24h for the daily
    # fixture. NOT exactly equal to Timedelta(hours=24)*0.7 -- float rounding puts it
    # fractionally under -- hence approx.
    """Body width is 70% of bar spacing, which is what stops candles touching."""
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_sample_df(), "T")
    expected = _ms(pd.Timedelta(hours=24)) * 0.7
    assert _body_width_ms_of(layout) == pytest.approx(expected)


def test_build_chart_object_body_width_tracks_intraday_spacing():
    # The smear regression (a51b454, not 794d7c0 -- see build_chart_object's docstring)
    # restated for the new engine: 30m candles must be strictly narrower than 1h, which
    # must be strictly narrower than daily.
    """Intraday bars size their bodies off intraday spacing, not a daily assumption."""
    from claudia.panel_chart import build_chart_object

    def width(freq):
        """The candle body width from the built figure, in milliseconds."""
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
    """Up and down candles get their own colours."""
    import holoviews as hv

    from claudia.panel_chart import _DOWN_COLOR, _UP_COLOR, build_chart_object

    obj = build_chart_object(_sample_df(), "T")
    rects = _rects(obj)
    color = hv.Store.lookup_options("bokeh", rects, "style").kwargs["color"]
    assert list(color.apply(rects)) == [_UP_COLOR, _UP_COLOR, _DOWN_COLOR, _DOWN_COLOR]


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
    # _body_width_ms_of takes the Overlay (it re-derives Rectangles.I via _price/_rects
    # internally), not an already-extracted Rectangles element -- passing canon_rects/
    # reord_rects here would raise (Rectangles has no further .Rectangles to descend into).
    assert _body_width_ms_of(reord_obj) == pytest.approx(_body_width_ms_of(canon_obj))


def test_build_chart_object_body_width_uses_min_not_median_spacing():
    # hvplot's own width formula is `np.min(np.diff(x)) * bar_width` (converter.py,
    # verified 2026-08-03) -- MIN, not the MEDIAN the old, now-deleted `_body_width_ms`
    # helper used (Task 7). On a weekend-gap fixture (Thu, Fri, Mon, Tue -- one 72h
    # outlier gap among three) min and median agree at 24h, so that shape of fixture
    # does not exercise the difference -- confirmed 2026-08-03 by running
    # build_chart_object directly on that fixture before `_body_width_ms` was deleted:
    # hvplot's width came out at exactly 24h * 0.7, matching what the old median-based
    # helper asserted. A trailing half-day bar is the case that DOES diverge: it makes
    # the min gap 12h while the median gap stays 24h. Assert what hvplot ACTUALLY does
    # (min-based) here, not parity with the deleted helper -- they are genuinely
    # different statistics and are not expected to agree on this fixture.
    """Minimum spacing sets the width, so a single tight gap cannot make bodies overlap."""
    from claudia.panel_chart import build_chart_object

    idx = pd.to_datetime(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-03 12:00"], format="mixed"
    )
    df = pd.DataFrame(
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0},
        index=idx,
    )

    hv_width = _body_width_ms_of(build_chart_object(df, "T"))
    assert hv_width == pytest.approx(_ms(pd.Timedelta(hours=12)) * 0.7)
    # Restate the divergence explicitly: the deleted median-based helper would have
    # returned 24h here, not the 12h min hvplot actually uses -- these are not expected
    # to be the same value. Hardcoded rather than calling the deleted `_body_width_ms`
    # directly.
    old_median_based_width = _ms(pd.Timedelta(hours=24)) * 0.7
    assert hv_width != pytest.approx(old_median_based_width)


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


def test_build_chart_object_overlays_the_sma():
    """The moving average is overlaid on the price figure."""
    from ibkr_core_mcp import indicators

    from claudia.panel_chart import _SMA_PERIOD, build_chart_object

    df = _long_df()
    curve = _price(build_chart_object(df, "T")).Curve.Sma_20
    assert curve.vdims[0].name == "sma_20"
    sma = indicators.sma(df, _SMA_PERIOD)
    xs = list(curve.dimension_values(curve.kdims[0].name))
    ys = list(curve.dimension_values("sma_20"))
    valid = [(x, y) for x, y in zip(xs, ys, strict=True) if y == y]  # drop NaNs
    # Value parity with the source of truth, not a reimplementation of it.
    assert [y for _, y in valid] == pytest.approx(sma.dropna().tolist())
    # Alignment, not just values: rolling(20).mean() and rolling(20, center=True).mean()
    # give the SAME dropna'd value sequence for ANY input -- a centered window's average
    # is the same set of numbers as a trailing window's, just labelled at a different x
    # position (verified 2026-08-03 on this fixture and on random data: max abs diff
    # 0.0 either way). So the value check above cannot by itself catch a trailing-vs-
    # centered mixup; pin the x-position too, against indicators.sma's own index.
    got_x = pd.DatetimeIndex([x for x, _ in valid])
    assert got_x.equals(pd.DatetimeIndex(sma.dropna().index))


def test_sma_overlay_is_renamed_not_left_as_close():
    # indicators.sma's docstring claims it returns a Series named 'sma_{period}', but its
    # body is df["close"].rolling(period).mean(), so the name is actually 'close'.
    # hvplot labels a Series curve from Series.name, so without the rename the moving
    # average renders labelled 'close' and collides with the price series. This test pins
    # our rename; it deliberately also pins the upstream reality so that if indicators.sma
    # is ever fixed to match its docstring, this fails loudly rather than silently.
    """The overlay is labelled as the SMA, not left carrying the close column's name."""
    from ibkr_core_mcp import indicators

    from claudia.panel_chart import build_chart_object

    assert indicators.sma(_long_df(), 20).name == "close"
    assert _price(build_chart_object(_long_df(), "T")).Curve.Sma_20.vdims[0].name == "sma_20"


def test_build_chart_object_tolerates_frame_shorter_than_sma_period():
    # A 4-row frame makes sma(20) all-NaN. That composes and renders without a crash, so
    # there is no guard for it -- just an empty overlay.
    """A frame shorter than the SMA window still renders, without the overlay."""
    from claudia.panel_chart import build_chart_object

    values = list(_price(build_chart_object(_sample_df(), "T")).Curve.Sma_20.dimension_values("sma_20"))
    assert len(values) == 4
    assert all(v != v for v in values)  # every value NaN


def test_build_chart_object_adds_a_volume_subplot():
    """Volume is drawn as its own row beneath price."""
    from claudia.panel_chart import build_chart_object

    layout = build_chart_object(_long_df(), "T")
    assert [type(e).__name__ for e in layout] == ["Overlay", "Bars"]
    assert list(layout.Bars.Volume.dimension_values("volume")) == pytest.approx(
        _long_df()["volume"].tolist()
    )


def test_volume_subplot_keeps_a_continuous_x_axis():
    # x-range sharing between the two rows comes from HoloViews' Layout shared_axes (see
    # test_layout_figures_share_one_x_range for why it isn't Panel's linked_axes).
    # shared_axes still needs matching range kinds to actually share: a categorical
    # (FactorRange) volume axis paired with the datetime candle axis would silently break
    # the sync (verified 2026-08-03: pairing a datetime element with a categorical one
    # under shared_axes=True yields two distinct range objects, not one shared one).
    """The volume row keeps the same continuous time axis as price."""
    import holoviews as hv
    from bokeh.models import FactorRange

    from claudia.panel_chart import build_chart_object

    fig = hv.render(build_chart_object(_long_df(), "T").Bars.Volume, backend="bokeh")
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
    figs = list(pane.get_root().select({"type": bk_figure}))
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
    figs = [
        (row, col, child) for child, row, col in fig.children if isinstance(child, bk_figure)
    ]
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
    tk._cache.load.side_effect = RuntimeError(
        "No cached file for ZZQQXX_30M_1M_2026-08-03"
    )
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

    idx = pd.to_datetime(
        ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    )
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
