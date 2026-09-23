"""The market palette: what green, red and grey mean, and the one rule that picks between them.

**Why this module exists (gap #23's palette item).** `#26a69a` and `#ef5350` were declared
independently in `panel_chart.py` and `panel_dashboard.py`. Nothing held them together, so a
green candle and a green number agreed only by coincidence, and changing one surface's idea
of "up" silently left the other behind. Consolidating them is the stated prerequisite for
theme-matched candles in `docs/panel/ui-customisation-reference.md` § 4.

These are **semantics, not preferences.** Green-means-profit is not a theme knob, which is
why this is its own module rather than part of `panel_theme.py` — that one holds what a user
may change (theme, avatar, display name). A palette that reads differently in dark mode would
be a separate, deliberate decision.

`tests/test_palette.py` asserts structurally that these hex values appear in no other module
in the package. The point of consolidating is that it stays consolidated.
"""

from __future__ import annotations

UP_COLOR = "#26a69a"
"""Teal-green. A candle whose close >= open; a profit; a figure above the dead band."""

DOWN_COLOR = "#ef5350"
"""Red. A candle whose close < open; a loss; a figure below the dead band."""

FLAT_COLOR = "#8a8a8a"
"""Neutral grey. A figure inside the dead band, including exactly zero."""

PNL_FLAT_BAND = 0.005
"""Half a cent of dead band around zero, inside which a money figure reads as flat.

**The rule is half the smallest unit the cell can DISPLAY**, and this value is that rule
applied to money: two decimals, so the smallest visible unit is `0.01` and half of it is
`0.005`. Anything inside the band **displays as `0.00`**, and painting it a loss would
contradict the number printed beside it. `panel_dashboard` also expresses this band as
`Number.colors` thresholds for the KPI tiles; both come from here so the two forms cannot
disagree.

**A band is only ever right for one display format.** `panel_dashboard`'s percentage
columns hold fractions and render through `"+0,0.00%"`, so their smallest visible unit is
`0.0001` and their band is `0.00005` — a hundredth of this one. That is why `pnl_color`
takes `flat_band` rather than closing over this value: passing money's band to a fraction
painted every move under ±0.5% flat, which is the defect this parameter exists to stop
recurring.
"""


def pnl_color(value: float, *, flat_band: float = PNL_FLAT_BAND) -> str:
    """The colour a signed money figure is drawn in — green up, red down, grey flat.

    **One rule for every P&L surface**: the KPI tiles, the positions and orders tables, and
    the realised chart. Two defects came from not having it.

    Until 2026-09-23 `build_realised_chart` passed `UP_COLOR` to the cumulative line *and*
    its area unconditionally, so a window that lost money drew in the up colour; and it
    passed `FLAT_COLOR` to every daily bar, so a +2,000 day and a -2,000 day were
    indistinguishable (gap #59). Separately, the table styler carried its own strict-sign
    rule that differed from the tiles inside the dead band, so a cell displaying `-0.00` was
    painted red while the tile beside it showed neutral.

    `flat_band` defaults to money's half-cent and must be overridden for any surface that
    renders to a different precision — see `PNL_FLAT_BAND`. The band is **exclusive**:
    exactly at `flat_band` is still flat.
    """
    if value > flat_band:
        return UP_COLOR
    if value < -flat_band:
        return DOWN_COLOR
    return FLAT_COLOR
