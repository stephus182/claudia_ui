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
    _UP_COLOR,
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
    tk._cache.load.assert_called_once()
    assert isinstance(_chart(pane).object, figure)
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
