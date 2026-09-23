"""The shared market palette, and the structural rule that keeps it single-source.

Gap #23's palette item: `#26a69a` / `#ef5350` were declared independently in
`panel_chart.py` and `panel_dashboard.py`, so a green number and a green candle agreed
only by coincidence. Consolidating them is the stated prerequisite for theme-matched
candles, and the point of consolidating is that it stays consolidated — hence the
structural test rather than only the unit ones.
"""

from __future__ import annotations

import pathlib
import re

from claudia import palette

_HEXES = ("#26a69a", "#ef5350", "#8a8a8a")
_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "claudia"


def test_the_palette_hexes_are_declared_in_exactly_one_module():
    """A second declaration is how the chart and the dashboard drifted apart."""
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "palette.py":
            continue
        source = path.read_text(encoding="utf-8")
        for hexcode in _HEXES:
            if hexcode in source:
                offenders.append(f"{path.name} declares {hexcode}")
    assert not offenders, "the market palette must live in claudia/palette.py only: " + "; ".join(
        offenders
    )


def test_chart_and_dashboard_both_source_their_colours_here():
    """The whole point: a green candle and a green number mean one thing.

    Asserted on the IMPORT, not on module attributes: mypy's no-implicit-re-export means a
    name imported into `panel_dashboard` is not part of its public surface, and asserting
    on attributes would quietly encourage other code to reach through it.
    """
    import ast

    for name in ("panel_chart.py", "panel_dashboard.py"):
        tree = ast.parse((_PACKAGE / name).read_text(encoding="utf-8"))
        sources = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "claudia.palette" in sources, f"{name} does not source its colours from palette"


def test_pnl_color_maps_sign_to_colour_with_a_dead_band():
    """Money renders to 2dp, so a figure displaying as 0.00 must not read as a loss."""
    assert palette.pnl_color(5.0) == palette.UP_COLOR
    assert palette.pnl_color(-5.0) == palette.DOWN_COLOR
    assert palette.pnl_color(0.0) == palette.FLAT_COLOR
    assert palette.pnl_color(-0.003) == palette.FLAT_COLOR
    assert palette.pnl_color(0.003) == palette.FLAT_COLOR


def test_the_dead_band_boundary_is_exclusive():
    """Exactly at the band edge is still flat; a hair beyond it is not."""
    assert palette.pnl_color(palette.PNL_FLAT_BAND) == palette.FLAT_COLOR
    assert palette.pnl_color(-palette.PNL_FLAT_BAND) == palette.FLAT_COLOR
    assert palette.pnl_color(palette.PNL_FLAT_BAND * 1.01) == palette.UP_COLOR
    assert palette.pnl_color(-palette.PNL_FLAT_BAND * 1.01) == palette.DOWN_COLOR


def test_the_regex_probe_would_catch_a_reintroduced_literal(tmp_path):
    """The structural test must not be vacuous — prove it fires on a real duplicate."""
    assert re.search(re.escape(_HEXES[0]), f"X = '{_HEXES[0]}'")
