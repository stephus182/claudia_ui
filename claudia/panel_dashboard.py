"""Panel components for the live trading dashboard — **widgets only, no IBKR, no SQL.**

The other half of the seam described in `claudia/dashboard_data.py`: this module reads a
`DashboardSnapshot` and renders it. It performs no I/O of any kind, which is what makes
it testable against a stub snapshot before it is wired to anything, and what keeps the
5-second repaint free of blocking work on the shared event loop.

Layout, as decided in the plan:

```
KPI strip  (always visible, across the top)
Chat  |  Tabs( Chart · Positions · Orders · Fills · P&L )
```

The tabs are built here; the Chart tab's contents are passed in, because that pane is
`claudia/panel_chart.py`'s and is driven by its own Load button.

## Safety — this is a display surface, and structurally so

Hard Rule 1 stands: ClaudIA cannot place, modify or cancel orders, and staging is a
physical button through `order_flow.py`'s two gates. Nothing in this module imports an
order path, and the positions table is built with `disabled=True` and **no**
`on_click`/`on_edit` handler at all. `Tabulator` cells are editable by default — an
editable P&L table would be a data-integrity hazard even before it was a safety one —
and `tests/test_panel_dashboard.py` asserts both the flag and the absence of callbacks
as a regression guard rather than trusting this comment.

## Two honesty rules the layout enforces

**Staleness is visible.** `DashboardSnapshot.age_seconds()` drives a status line that
changes wording and colour once the account data passes `STALE_AFTER`. A trading surface
showing stale numbers with no indication is the worst failure available here.

**The two realised figures are labelled apart.** The ledger figure includes today; the
Flex-derived week/month/YTD figures cannot (IBKR publishes a day's trades T+1, so today
is never in the dataset, and live Client Portal rows carry no trade date at all). They
will therefore disagree, legitimately, and the freshness line says so. Similarly,
round-trip win/loss counts come from `flex_lot` while the P&L total comes from
`flex_trade` — the panel labels which is which, because lot-derived P&L would silently
overstate losses by the wash-sale-disallowed amount.

## Currency

Every money figure carries its ISO code, never a bare `$` — that symbol is shared by
USD/MXN/CAD/AUD/HKD/SGD, and a wrong-currency price reads as an ordinary one. Where a
window spans several currencies the label is `mixed` rather than a code the total does
not actually have.

The one case with **no** code is a window that realised nothing: it has no currency, so
the view substitutes the account's own base currency from the ledger, and renders a bare
number when there is no ledger either. Substitute a known currency or none — never a
plausible-looking placeholder, which is the mistake the ledger's `"BASE"` row already
caused once (`dashboard_data.parse_ledger`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import holoviews as hv

# Side-effect import: registers the bokeh renderer and installs the DataFrame `.hvplot`
# accessor. Same load-bearing import as claudia/panel_chart.py — see that module's note.
import hvplot.pandas  # noqa: F401
import numpy as np
import pandas as pd
import panel as pn
from bokeh.models.widgets.tables import NumberFormatter

from claudia.dashboard_data import (
    RECONCILE_TOLERANCE,
    DashboardSnapshot,
    RealisedPoint,
    RealisedWindow,
    Reconciliation,
    RoundTripStats,
    display_symbol,
    order_display_name,
    position_display_name,
    realised_ledger_label,
    reconcile,
    weekly_series,
)
from claudia.dashboard_poller import STALE_AFTER
from claudia.live_realised import execution_time_et
from claudia.palette import (
    DOWN_COLOR,
    FLAT_COLOR,
    PNL_FLAT_BAND,
    UP_COLOR,
    pnl_color,
)
from claudia.panel_markdown import safe_markdown, safe_toast

log = logging.getLogger(__name__)


# `Number.colors` is scanned in reverse and the last match wins, so the earliest
# threshold a value satisfies is the colour applied (verified 2026-08-04 by reading
# `panel.widgets.indicators.Number._process_param_change`). Half a cent of dead band
# around zero keeps a flat P&L neutral instead of red: `value <= threshold` would
# otherwise paint an exactly-zero figure as a loss.
_PNL_COLORS = [
    (-PNL_FLAT_BAND, DOWN_COLOR),
    (PNL_FLAT_BAND, FLAT_COLOR),
    (float("inf"), UP_COLOR),
]


_TILE_WIDTH = 165
_CHART_HEIGHT = 260
# Near-parity with the cumulative row, not the half-height strip it was until 2026-09-23
# (user, looking at the live pane: "~same height to be more readable"). The daily row
# carries the per-day detail the cumulative row deliberately hides, so squeezing it made
# the chart's own subject the hardest part to read: a -21.31 day and a +2,922.96 day
# shared 130px. The cumulative keeps a slight edge because it is the headline figure.
_BAR_ROW_HEIGHT = 230

# Major y ticks per row. Bokeh's default chose seven labels for the old 130px strip, which
# is what "too dense" was. A cap rather than explicit tick values: the windows span three
# orders of magnitude (a week of scratches to a year of futures), so fixed positions would
# be wrong for most of them while a cap reads well at any range.
_Y_TICK_COUNT = 5

# Which asset categories roll up into the "equities & options" side of the futures split.
# FUT is everything else. Named here rather than inline so the two call sites cannot drift.
_NON_FUTURES = ("STK", "OPT", "FUND", "CASH")

# Radio-button label -> the snapshot attribute and `breakdowns`/`stats` key it selects.
# Every realised window the P&L pane can show, shortest first. One mapping, so a window's
# realised total and its round-trip counts can never be picked from different windows.
#
# "Daily" is not like the other three and the pane branches on it (gap #69). It shows the
# PENDING window: the executions IBKR has not put on a statement yet, decided by execution
# id, which carry NO day because Flex is the only source of a trade date. The operator
# named the tab "Daily" (2026-09-24): most days that is exactly today's closes, and the
# heading under the tab states what it really holds ("Not yet on a statement · Flex through
# <date>"), which can include an earlier day while a statement is late. So it has no
# `RealisedWindow`, no curve and no settled block; it is `snapshot.pending` alone. See
# `_refresh_pending`.
_PENDING_LABEL = "Daily"
_WINDOW_KEYS = {_PENDING_LABEL: "pending", "Weekly": "week", "Monthly": "month", "YTD": "ytd"}
_WINDOW_LABELS = tuple(_WINDOW_KEYS)


# ── Formatting ────────────────────────────────────────────────────────────────


def fmt_money(value: float | None, currency: str) -> str:
    """A balance as `12,345.67 USD`. `—` for None, never a bare currency symbol.

    An empty `currency` renders the number alone. That case is not an oversight: an
    empty realised window has no currency to state (`RealisedWindow.currency_label`),
    and a bare `0.00` is honest where `0.00 USD` on a EUR account would not be.
    """
    if value is None:
        return "—"
    return f"{value:,.2f} {currency}".rstrip()


def fmt_signed(value: float | None, currency: str) -> str:
    """A P&L figure as `-3,516.98 USD` / `+250.25 USD`, with an explicit sign.

    The sign is always shown: a profit and a loss must not be distinguishable only by a
    minus that is easy to miss in a dense row of numbers. An empty `currency` renders
    the number alone — see `fmt_money`.
    """
    if value is None:
        return "—"
    return f"{value:+,.2f} {currency}".rstrip()


def fmt_age(seconds: float) -> str:
    """A poll age as `3s` / `2m 14s` / `1h 05m` — compact enough for a status line."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"


# How much of a poll error to show on the status line. An unreachable gateway produces a
# ~230-character urllib3 string ("HTTPSConnectionPool(host=…): Max retries exceeded with
# url: … (Caused by NewConnectionError(…))"), which wrapped onto two lines and buried the
# word STALE when it was first rendered live (2026-08-04). The full text is in the log;
# the strip needs the fact, not the traceback.
_MAX_REASON_CHARS = 110


def short_reason(error: str | None) -> str:
    """A poll error trimmed to something a status line can carry.

    Cuts at `(Caused by` first — urllib3's own summary sits before it and is the useful
    half — then collapses whitespace and caps the length. Never returns the empty string
    for a non-empty error: a truncation that hid the failure entirely would be worse
    than the wrapping it fixes.
    """
    if not error:
        return ""
    text = " ".join(error.split("(Caused by")[0].split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return text


# Where the numbers in a breakdown table came from, stated under the table itself.
#
# `_FLEX_SOURCE_NOTE` is true of the settled windows and FALSE of the pending window,
# which is why it is a parameter rather than a constant baked into the renderer. Nothing
# pending comes from either table: no statement covers those executions yet, so every
# figure there is reconstructed from the account's own executions. Shipped with the wrong
# note attached on 2026-08-07 and caught in the browser the same hour — the table was right
# and the sentence under it was a confident lie about where the money came from.
_FLEX_SOURCE_NOTE = (
    "_Net is `flex_trade` (statement basis); gross and counts are `flex_lot` "
    "(pre-wash-sale lot detail). They are different quantities and need not tie._"
)
_LIVE_SOURCE_NOTE = (
    "_Reconstructed FIFO from your own executions — not `flex_trade`, not `flex_lot`, "
    "not settled by IBKR. No statement covers these executions yet._"
)


def breakdown_table(window: Any, currency: str = "", note: str = _FLEX_SOURCE_NOTE) -> str:
    """Per-asset-class detail for the P&L pane: money, counts and averages together.

    Lives in the pane rather than the KPI strip because it answers a different question.
    The strip answers "how am I doing right now" in four glanceable percentages; this
    answers "what actually happened", and needs nine columns to do it honestly.

    **The count and the money must be read together, and this is why both are here.** One
    win of 3,000 against five losses of 120 is a 17% win rate and a good day; this
    account's own week showed STK at 0 wins from 23 lots, which reads as a catastrophe
    until the average loss turns out to be about 141 while FUT's 33% sat on losses an
    order of magnitude larger. A surface showing only the rate, or only the net, reports
    the opposite of what happened in both cases.

    `Net` and the lot-derived columns come from **different tables** and will not always
    reconcile: `net` is `flex_trade` (the figure that ties to IBKR's annual statements),
    while gross win/loss and the counts are `flex_lot`, which is pre-wash-sale detail.
    The header says so rather than leaving a reader to discover it by subtraction.
    """
    if window is None or not window.rows:
        return "_No closed trades in this window._"
    ccy = f" ({currency})" if currency else ""
    flag = (
        "  ⚠ **incomplete** — a contract could not be reconstructed\n\n"
        if getattr(window, "incomplete", False)
        else ""
    )
    lines = [
        flag,
        f"| Type | Net{ccy} | Gross win | Gross loss | W | L | Win % | Avg win | Avg loss |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def num(v: float | None) -> str:
        """A money cell, or an em dash when the figure does not exist.

        None and 0.00 are different claims: "there were no winning lots" versus "the
        winning lots averaged nothing". Only the second is a number.
        """
        return "—" if v is None else f"{v:,.2f}"

    for r in window.rows:
        rate = "—" if r.win_rate is None else f"{r.win_rate:.0f}%"
        lines.append(
            f"| **{r.asset_class}** | {r.net:,.2f} | {r.gross_win:,.2f} | "
            f"{r.gross_loss:,.2f} | {r.winners} | {r.losers} | {rate} | "
            f"{num(r.average_win)} | {num(r.average_loss)} |"
        )
    lines.append(f"| **Total** | **{window.net:,.2f}** | | | | | | | |")
    lines.append("")
    lines.append(note)
    return "\n".join(lines)


def pending_heading(snapshot: DashboardSnapshot | None, stale: bool = False) -> str:
    """The Daily tab's heading: what it holds, where the figures come from, and age.

    Gap #69 (operator 2026-09-24): the executions here are the ones whose id is not yet a
    Flex `execution_key`: non-Flex daily realised trades, in the operator's words. They
    carry **no trade date** — IBKR states one only in its statement — so the heading says
    "non-Flex", names the statement they are measured against, and never says "today". The Daily tab before gap #69 did, by dating fills on their UTC
    timestamp, which is not the trade date for a futures fill between 18:00 ET and UTC
    midnight.

    A stale line is prepended rather than the table being blanked, and this is the one
    place the module's usual "stale account data is not drawn at all" rule is traded for a
    warning. A resting order can fill while our gateway is unreachable, so a stale pending
    figure is a **floor**, not a wrong number — and a floor stated as a floor is worth
    more than a blank. It must never be left to pass as current, which is what the line
    says.
    """
    if snapshot is None:
        return "**Daily realised — non-Flex**"
    cov = snapshot.coverage
    against = (
        f"Flex through **{cov.through.isoformat()}**"
        if cov is not None and cov.through is not None
        else "no statement in the local store yet"
    )
    warn = (
        "**⚠ Not current — the gateway is unreachable or the last poll failed. Any fill "
        "since then is missing, so the figures below are a floor.**\n\n"
        if stale
        else ""
    )
    return (
        f"{warn}**Daily realised — non-Flex** · not yet on a statement ({against}) · "
        "reconstructed from your own executions. IBKR states a fill's trade date only in "
        "its statement (T+1), so these carry no trade date yet and appear in no dated "
        "window."
    )


def pending_table(snapshot: DashboardSnapshot | None, currency: str = "") -> str:
    """The Daily tab of the P&L pane: realised P&L not yet on a statement, per type.

    Renders the same nine-column template as the pane's other three windows, against
    `snapshot.pending` — **non-Flex by construction**: every figure is reconstructed from
    the account's own executions (`live_realised`) and not settled by anyone.

    Four states, and the middle two are the reason this function exists rather than a
    bare `breakdown_table` call:

    * **never polled** — say so;
    * **unreadable** (`pending is None`) — the gateway was unreachable, so what is pending
      is *unknowable*. Saying "nothing pending" here would be a fabricated claim;
    * **declined** — the executions were read, but a contract's reconstructed position
      disagreed with IBKR's, so every figure it touched was withdrawn. With no rows left
      there is nothing for `breakdown_table`'s row-level ⚠ to mark, and an empty window
      reads as "nothing happened". On 2026-08-10 that is exactly what it said, on a session
      that realised +8,441.12 — seven CL executions, all declined because the fill window
      opened mid-position;
    * **nothing pending** — every closed round trip in view is already on a statement.
    """
    if snapshot is None or not snapshot.breakdowns:
        return "_Pending P&L: waiting for the first poll…_"
    pending = snapshot.pending
    if pending is None:
        return (
            "**Pending P&L cannot be computed — live fill data unavailable.**\n\n"
            "_It is reconstructed from your own executions, which come from the IBKR "
            "gateway. No statement covers them yet either._"
        )
    if not pending.rows and pending.incomplete:
        return (
            "**Pending realised P&L could not be reconstructed.**\n\n"
            "_At least one contract with executions not yet on a statement could not be "
            "reproduced from the executions in view — its position does not agree with "
            "IBKR's — so every figure it touched was withdrawn rather than reported short. "
            "The account ledger below carries IBKR's own realised figure._"
        )
    if not pending.rows:
        return "_No closed round trips awaiting a statement._"
    return breakdown_table(pending, currency, note=_LIVE_SOURCE_NOTE)


def freshness_line(snapshot: DashboardSnapshot, now: datetime | None = None) -> str:
    """The account-data freshness sentence, including the stale warning.

    Three states, and the middle one is why this function exists at all:

    * never polled — say so, rather than showing an empty strip that looks like a
      flat account;
    * stale (older than `STALE_AFTER`, or the last poll errored) — lead with the age
      and the reason, in bold, so it cannot be read past;
    * fresh — a quiet one-liner.

    `now` is injectable so the stale wording is testable without sleeping.
    """
    if snapshot.ledger is None and snapshot.error and "not polled yet" in snapshot.error:
        return "_Account data: waiting for the first poll…_"
    age = snapshot.age_seconds(now)
    if age > STALE_AFTER or snapshot.error:
        reason = f" — {short_reason(snapshot.error)}" if snapshot.error else ""
        return f"**⚠ Account data is STALE — last updated {fmt_age(age)} ago{reason}**"
    return f"_Account data live — updated {fmt_age(age)} ago._"


def coverage_line(snapshot: DashboardSnapshot) -> str:
    """The disclosure: the three ways the ledger tile and the Flex windows differ.

    The two realised figures sit next to each other on one screen and will not
    reconcile. An unlabelled pair that disagrees is exactly the failure this whole track
    exists to avoid, so all three reasons are stated in words on the surface itself
    rather than left for a reader to discover by subtraction:

    1. **Coverage** — Flex is T+1 and never includes today; the ledger is today only.
    2. **Day boundary** — Flex buckets on IBKR's session date (18:00 ET futures /
       20:00 ET stock / 17:00 ET FX); the ledger figure rolls once, late in the ET
       evening, on IBKR's accounting boundary.
    3. **Cost basis** — Flex is the statement basis, the ledger is IBKR's real-time
       `avgCost`: different quantities by construction, with no guarantee they agree.

    Only the first two were disclosed before 2026-08-04.

    **Bullet 2 said "a calendar day" until 2026-08-05, and that was wrong in the one
    direction a trader would notice.** The ledger accumulator was measured that evening
    rolling in the late ET evening (`dashboard_data.REALISED_LEDGER_WINDOW`, note 6), so
    for the last hours of a calendar day the tile labelled "Realised today" can already
    be showing *tomorrow* — typically 0.00, right after a day that realised something.
    Telling the user it followed the calendar would have made that reset look like a
    data fault, or worse, be believed.

    **No specific hour appears below, and that is deliberate.** The first draft of this
    line said "between 21:55 and 22:31 ET", from a bracket that held at the time. A
    37-read watch ending exactly on the upper bound then showed the field unmoved, which
    killed the fixed-hour reading the bracket assumed: the roll is a broker-side event
    whose hour varies. A disclosure naming a time the user could check against the clock
    would be worse than the vague one — it would be falsifiable and false.

    **Bullet 3 was softened on 2026-08-05, on evidence.** It used to tell the user the
    basis difference was "the largest of the three" and cite CRM as a close where the
    two differ by an order of magnitude. That rested on a projection — that Flex would
    report about -252.60 for the 2026-08-04 CRM sale where the ledger reported -2,810.47.
    The statement arrived reading **-2,810.47**, the same figure to the cent
    (`dashboard_data.RealisedWindow` carries the full measurement). Coverage and the day
    boundary are the differences actually observed to move these numbers; the basis
    difference is real in definition and so far unobserved in size, and the text now says
    only that much. A disclosure that overstates is still a disclosure that misleads.
    """
    cov = snapshot.coverage
    if cov is None or cov.through is None:
        return "_Realised windows: no Flex data in the local store._"
    return (
        f"_Realised week/month/YTD come from the Flex dataset through "
        f"**{cov.through.isoformat()}** (IBKR publishes a day's trades T+1, so today is "
        f"never in it) — executions not yet on a statement are under **Daily**, "
        f"never in these windows — and are "
        f"IBKR's **statement** figures. The tile above is today only, on IBKR's "
        f"**real-time average cost**. The two do not add up and are not meant to: they "
        f"cover different periods, and they use different day boundaries — Flex buckets "
        f"on IBKR's **session** date, which rolls at 18:00 ET for futures, 20:00 ET for "
        f"stock and 17:00 ET for FX, so an evening fill already belongs to tomorrow, "
        f"while the tile rolls once on IBKR's own accounting boundary — **late in the "
        f"ET evening, at an hour that varies, not at midnight** — so for the last hours "
        f"of a day the tile can already be on tomorrow. The two cost bases are also "
        f"defined differently, though where both have priced the same close they have "
        f"agreed to the cent (measured 2026-08-04 and 2026-08-05)._"
    )


# ── The realised-P&L chart ────────────────────────────────────────────────────


def realised_frame(points: tuple[RealisedPoint, ...]) -> pd.DataFrame:
    """`RealisedPoint`s as a DataFrame with `day` / `realised` / `cumulative` columns.

    Column names are pinned here and passed explicitly to every hvplot call below —
    hvplot binds some arguments positionally, so relying on column order is how a chart
    silently plots the wrong series (the lesson `panel_chart.build_chart_object`
    records for `.ohlc()`).

    **`cumulative` is re-based to these points, and that is the whole point of computing
    it here rather than reading `RealisedPoint.cumulative`** (found on screen 2026-09-23).
    The poller computes **one YTD series** and `_selected_window` slices it by date;
    slicing the days does not re-base the running total, so the stored `cumulative` stays
    anchored to 1 January. A pane headed *"Monthly (2026-09-01 → 2026-09-23)"* therefore
    drew a red curve ending at **-15,788.07** while the table beside it reported the month
    at **+3,127.43**. Measured the same day: Weekly, Monthly and YTD **all** ended on
    -15,788.07 — every window drew the YTD endpoint, so the curve's colour was always the
    year's sign, never the selected window's.

    **No P&L figure changes, and none may.** `dashboard_data` is untouched;
    `RealisedPoint.cumulative` still means what it always meant. This function stops
    reading a year-anchored field to describe a month. Verified against the live store
    before the change: `cumsum(realised)` reproduces the stored `cumulative` **bit-exactly**
    over all 131 YTD points (max delta 0.0000000000), so the one window that was already
    correct cannot move, and each window's re-based endpoint equals `realised_window.total`
    — the table's own figure — to the cent.
    """
    return pd.DataFrame(
        {
            "day": pd.to_datetime([p.day for p in points]),
            "realised": [p.realised for p in points],
            "cumulative": pd.Series([p.realised for p in points], dtype="float64").cumsum(),
        }
    )


def realised_chart_note(points: tuple[RealisedPoint, ...], currency: str, unit: str = "day") -> str:
    """Why the chart is absent, when it is. Empty string when a chart was drawn.

    A window can legitimately hold fewer than two trading days — a Monday-start week
    read on a Tuesday, with Flex still T+1, has exactly one. Saying so and printing the
    single figure beats both an empty frame and the broken axis a one-point series
    produces (observed live 2026-08-04: a single point made bokeh fall back to a
    **millisecond** x-axis reading "0ms / 500ms / 0ms" under a full-width grey bar).
    """
    if not points:
        return "_No realised P&L in this window._"
    if len(points) == 1:
        only = points[0]
        # `unit` because the YTD view regroups to weeks: a year-to-date pane in early
        # January legitimately holds one bucket, and calling that bucket a day would
        # misstate how much trading it covers.
        opened = "beginning" if unit == "week" else ""
        return (
            f"_Only one trading {unit} in this window — **{opened}{only.day.isoformat()}: "
            f"{fmt_signed(only.realised, currency)}**. A curve needs at least two "
            f"points, so none is drawn._"
        )
    return ""


def _split_at_zero(step: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`step` as two frames — the part at or above zero, and the part at or below it.

    **Why this is cheap, and only became cheap once the line was stepped.** A stepped line
    is horizontal runs joined by vertical jumps, so it can only cross zero *on a vertical
    jump*, where both endpoints share an x. The crossing is therefore `(that x, 0)`
    exactly, with **no interpolation and no invented date**. On a sloped line the crossing
    falls between two trading days and the x has to be guessed.

    **Two columns, because the line and the fill need opposite treatments** — found live
    2026-09-23, when the monthly pane showed a hole between Sep 8 and Sep 13 where the
    cumulative had sat at +1,929 the whole time, with stray diagonals either side of it.
    The frames were right; only the fill's rendering was not.

    * `line` is NaN outside its half. A `Line` glyph honours NaN by **breaking**, which is
      what stops the curve being drawn flat along zero while it is on the other side.
    * `fill` is **clipped**, never NaN. An `Area` renders as a `Patch` — *one closed
      polygon* — so a NaN inside it does not open a gap, it **tears the polygon**, and the
      torn edges were the diagonals. Filling `0 .. max(y, 0)` covers exactly the above-zero
      region and needs no gap at all.

    Both share one `day` column, so the halves register against one x-axis and meet exactly
    at the crossing rather than being drawn as two misaligned series. A half whose `line`
    is entirely NaN — a window that never went under, or never came back — draws nothing,
    so a wholly-winning window carries no empty red element.
    """
    x = step["day"].to_numpy()
    y = step["cumulative"].to_numpy()
    xs: list[Any] = [x[0]]
    ys: list[float] = [float(y[0])]
    for i in range(1, len(y)):
        if (y[i - 1] > 0 > y[i]) or (y[i - 1] < 0 < y[i]):
            xs.append(x[i])
            ys.append(0.0)
        xs.append(x[i])
        ys.append(float(y[i]))
    values = np.array(ys)
    return (
        pd.DataFrame(
            {
                "day": xs,
                "fill": np.maximum(values, 0.0),
                "line": np.where(values >= 0, values, np.nan),
            }
        ),
        pd.DataFrame(
            {
                "day": xs,
                "fill": np.minimum(values, 0.0),
                "line": np.where(values <= 0, values, np.nan),
            }
        ),
    )


def _money_axis() -> Any:
    """A y-axis tick formatter that reads as money, built fresh for each row.

    Until 2026-09-23 both rows used Bokeh's `BasicTickFormatter` default, so a window
    totalling 6,050.39 showed an axis reading `6000` while every other figure in this app
    goes through `fmt_signed` with an ISO code.

    `0,0` — thousands separator, no decimals. Ticks are a scale, not a statement of the
    figure: the cents belong in the table above the chart and in the bars' tooltip, both of
    which carry them. The currency is not repeated per tick either; the chart title already
    names it once, which is where a unit belongs.

    A new formatter per call, never a module-level constant: it is a Bokeh model and a
    Bokeh model belongs to one Document.
    """
    from bokeh.models.formatters import NumeralTickFormatter

    return NumeralTickFormatter(format="0,0")


def _break_even() -> Any:
    """The zero rule, on whichever row it is overlaid.

    Break-even is the one reference a P&L chart cannot do without, and until 2026-09-23
    neither row had it: the rendered Bokeh model carried no `Span` at all, so on a window
    whose curve crossed zero the reader had to trace across to the axis to find where.

    **`FLAT_COLOR`, not green or red.** Zero is neither profit nor loss, and the palette
    already holds the colour that means exactly that — the one the KPI tiles use inside
    the dead band. Dashed and one pixel, so it reads as a reference and not as a series.

    Built fresh per call: an `HLine` renders to a Bokeh `Span`, and a Bokeh model belongs
    to one Document, so a module-level constant would be shared across figures. (In Bokeh
    3.9.2 that `Span` is filed under `figure.renderers`, not `figure.center` — measured,
    and the reason the test helper uses `figure.select`.)
    """
    return hv.HLine(0).opts(color=FLAT_COLOR, line_width=1, line_dash="dashed")


def _step_frame(df: pd.DataFrame) -> pd.DataFrame:
    """`df` expanded so a line drawn through it HOLDS each value until the next day.

    **Why the expansion rather than an option.** HoloViews documents `interpolation` on
    `Curve` — *"whether to linearly interpolate the curve values or to draw discrete
    steps"*, one of `linear` / `steps-mid` / `steps-pre` / `steps-post`
    (https://holoviews.org/reference/elements/bokeh/Curve.html, scraped 2026-09-23) — and
    hvPlot exposes the same through `.step(where=...)`
    (https://hvplot.holoviz.org/reference/pandas/step.html, hvPlot 0.12.2, same date).
    Measured that day, the two are equivalent: both render `[1000, 3000, 0]` as
    `[1000, 1000, 3000, 3000, 0]`.
    **`Area` accepts neither** — it raises `ValueError: Unexpected option 'interpolation'
    for Area type`. A linear fill beneath a stepped line cuts across every corner, so the
    fill and the line are built from one pre-stepped frame instead.

    **Why the cumulative row then carries no tooltip.** The expansion puts two points on
    each day. The first holds the value the day *opened* with, so a tooltip there reports
    e.g. Sep 8's cumulative as the figure it had before Sep 8 traded. The daily bars below
    already hover correctly — one point per day — and the curve's final value is the total
    printed in the table directly above the chart, so nothing is lost by staying silent
    here rather than being ambiguous.

    `steps-post` shape: x repeated and shifted forward, y repeated and shifted back, which
    is what makes the value persist rightward until the next observation replaces it.

    **The trailing day is not decoration.** `steps-post` holds each value until the *next*
    observation, so with N points only N-1 get any width — and the one left out is the
    last, which is the window total the table above the chart is reporting. Seen in a
    screenshot the day the step shape shipped: the total was a zero-width hairline against
    the right edge while every other day occupied its own span. One extra point carrying
    the same value gives the final day the same treatment as the rest. A day rather than
    some other interval because that is what the series is measured in, and the claim it
    makes — "this total stood for the day it was struck" — is one the statement supports.
    """
    days = df["day"].to_numpy()
    values = df["cumulative"].to_numpy()
    # The tail is one BUCKET wide, taken from the series' own minimum spacing rather than
    # hardcoded to a day: the YTD view's points are weekly, and a one-day tail would draw
    # the year's closing figure as a sliver. Minimum, not last, because trading days are
    # unevenly spaced — the same idiom hvplot uses to size bars.
    bucket = np.min(np.diff(days)) if len(days) > 1 else np.timedelta64(1, "D")
    return pd.DataFrame(
        {
            "day": np.append(np.repeat(days, 2)[1:], days[-1] + bucket),
            "cumulative": np.append(np.repeat(values, 2)[:-1], values[-1]),
        }
    )


def _money_hover(field: str, label: str) -> Any:
    """A tooltip that reads like money on a daily series, built fresh for each figure.

    Two reasons this is explicit rather than hvplot's default. The default promoted every
    frame column to a tooltip, including the internal `_bar_color`, so hovering a bar
    offered the reader a hex code. And it rendered the day as `%F %T` — a 00:00:00 on a
    series whose points are dates — with the money as a bare float.

    A new `HoverTool` per call, never a module-level constant: a Bokeh model belongs to one
    Document, and sharing one across figures is how a pane silently fails to render.
    """
    from bokeh.models import HoverTool

    return HoverTool(
        tooltips=[("Day", "@day{%F}"), (label, f"@{field}{{+0,0.00}}")],
        formatters={"@day": "datetime"},
    )


def build_realised_chart(points: tuple[RealisedPoint, ...], title: str) -> Any:
    """Cumulative realised P&L over the window, with daily realisations beneath.

    Returns an `hv.Layout` of two stacked rows sharing an x-range (HoloViews' own
    `Layout.shared_axes`, not anything Panel does — see `panel_chart`'s note), or None
    when there are fewer than two points. The caller renders None as the explanatory
    note above rather than as an empty axis pretending to be a flat week.

    **Two points is a hard floor, not a nicety.** A single-point series renders with a
    millisecond-scale time axis and a full-width bar — measured live 2026-08-04, and
    the reason this guard exists rather than being inferred from the one-bar guard in
    `panel_chart.build_chart_object` (which raises for a different reason: hvplot sizing
    candles off `np.min(np.diff(x))` on an empty diff).

    Only days that traded appear, so the cumulative line steps between them. That is the
    honest shape: nothing was realised in between, and interpolating a smooth slope
    across non-trading days would imply observations no statement covers.

    Returns `Any` for the same reason `panel_chart.build_chart_object` does — pandas is
    on mypy's `ignore_missing_imports` list, so everything chained off `.hvplot` is `Any`
    and gets no type checking. Keep this function small for that reason.
    """
    if len(points) < 2:
        return None
    df = realised_frame(points)
    # A Curve carries ONE colour, so the cumulative row is coloured by where the window
    # ENDS — "did this period finish up or down", which is the question that row answers.
    # Colour it per-segment only if someone asks for the zero crossing to be visible.
    # Stepped, not sloped, and both halves built from the same stepped frame. Neither
    # carries a tooltip — see `_step_frame`.
    #
    # ⚠ `sort_date=False` is LOAD-BEARING, not tidiness. hvPlot defaults it to True and
    # sorts by the datetime x; a stepped series carries TWO rows per day and the tie order
    # between them is arbitrary under that sort. Reordering them turns a vertical segment
    # into a diagonal — which is exactly what the pane kept showing after the step, split
    # and clip fixes were all correct, because the frames handed to hvPlot were right every
    # time and the element's own data came back scrambled (measured 2026-09-23).
    #
    # Coloured by WHERE THE CURVE IS, not by where the window ends. Until 2026-09-23 the
    # row took one colour from its final value, so a week that sat at -2,050 for two days
    # and closed at +855 drew the underwater stretch green, and a month that opened -871
    # drew its first week green. Above zero is a profit and below zero is a loss whatever
    # the window later did. On a stepped line the crossing is exact — see `_split_at_zero`.
    step = _step_frame(df)
    cumulative = _break_even()
    for frame, colour in zip(_split_at_zero(step), (UP_COLOR, DOWN_COLOR), strict=True):
        if not frame["line"].notna().any():
            continue  # never went that side of zero — draw nothing, not an empty layer
        cumulative = (
            cumulative
            * frame.hvplot.area(
                x="day", y="fill", alpha=0.20, color=colour, hover=False, sort_date=False
            )
            * frame.hvplot.line(
                x="day",
                y="line",
                color=colour,
                line_width=2,
                hover=False,
                sort_date=False,
            )
        )
    # Per bar, by that day's own sign. Passing a COLUMN NAME maps it straight onto Bokeh's
    # `fill_color` field with `transform=Unspecified` — no colormap — so the hex values are
    # used literally (verified 2026-09-23 against the rendered glyph, not inferred from the
    # hvplot docs). `.assign` returns a copy, so the frame the rows came from is untouched.
    #
    # `hover=False` then an explicit tool, rather than hvplot's inferred one: given the
    # frame, hvplot promoted `_bar_color` to a tooltip and offered the reader that
    # column's raw hex value (measured 2026-09-23 off the rendered Bokeh model). Owning the
    # tooltip fixes that at the root and lets the figures read as money rather than as
    # bare floats with a meaningless 00:00:00 beside them.
    bars = df.assign(_bar_color=[pnl_color(float(v)) for v in df["realised"]])
    daily = (
        bars.hvplot.bar(
            x="day", y="realised", height=_BAR_ROW_HEIGHT, color="_bar_color", hover=False
        ).opts(tools=[_money_hover("realised", "Realised")])
        * _break_even()
    )
    # `xlabel=""` on both rows: the ticks are dates, so the word "day" said nothing and was
    # printed under each row, costing two lines of the vertical space the bars now use.
    return (
        cumulative.opts(
            title=title,
            height=_CHART_HEIGHT,
            yticks=_Y_TICK_COUNT,
            yformatter=_money_axis(),
            xlabel="",
            ylabel="Cumulative",
        )
        + daily.opts(
            title="",
            yticks=_Y_TICK_COUNT,
            yformatter=_money_axis(),
            xlabel="",
            ylabel="Realised",
        )
    ).cols(1)


# ── Positions table ───────────────────────────────────────────────────────────

# Column order, and it encodes a reading order rather than a schema.
#
# Identity first (Symbol, Name), then size, then the two entry levels, then price, then
# the money. **`Class` sits second-to-last, immediately before `Ccy`** — moved there
# 2026-08-07 (user): it is factual metadata about the instrument, not a number a trader
# acts on, and it was occupying the second column where the instrument's name belongs.
#
# `Name` is IBKR's own description, next to the ticker precisely so a row cannot be acted
# on from an ambiguous symbol — a ticker is not a unique key, and IGV once priced a US
# ETF in MXN on exactly that assumption.
_POSITION_COLUMNS = [
    "Symbol",
    "Name",
    "Qty",
    "Avg entry",
    "IBKR basis",
    "Basis Δ",
    "Last",
    "Change",
    "% Change",
    "Market value",
    "Unrealised",
    "% Unrealised",
    "Class",
    "Ccy",
]
# The columns `.style.map` colours by sign. Named once so the styler and the empty-frame
# builder cannot disagree about which columns exist.
#
# "Basis Δ" is deliberately **not** here. Its sign says which way IBKR's basis leans, not
# whether anything is good or bad, and the green/red map on this surface means profit and
# loss. Colouring it would assert a judgement the number does not carry.
# Signed columns are split by the format they RENDER through, because the neutral band is
# half the smallest unit a cell can display and the two formats differ by a factor of 100.
# Keeping one list cost a real defect on 2026-09-23 — see `_percent_sign_style`.
_MONEY_SIGNED_COLUMNS = ["Unrealised", "Change"]
_PERCENT_SIGNED_COLUMNS = ["% Unrealised", "% Change"]

# Per-column pixel widths. Needed because the table carries fourteen columns in a pane
# that is about half the window: left to size themselves, `Name` alone took the room the
# money columns needed and everything from `Basis Δ` rightwards was clipped away
# unreachably (2026-08-07).
#
# `Name` is capped rather than given its natural width — IBKR's own values run to 28
# characters ("ISHARES EXPANDED TECH-SOFTWA") and it is context, not a figure anyone
# reads precisely; the full string is in the cell and the header tooltip explains the
# truncation is IBKR's. Every numeric column is wide enough for its formatted value plus
# a sign, since a money column that ellipsises is worse than one that scrolls.
_POSITION_WIDTHS = {
    "Symbol": 78,
    "Name": 190,
    "Qty": 70,
    "Avg entry": 92,
    "IBKR basis": 92,
    "Basis Δ": 86,
    "Last": 88,
    "Change": 86,
    "% Change": 86,
    "Market value": 108,
    "Unrealised": 96,
    "% Unrealised": 100,
    "Class": 62,
    "Ccy": 58,
}

# Display precision. IBKR returns full float precision — the live account rendered
# `383.270899` and `374.09762575` as an average cost and a last price (observed
# 2026-08-04), which is noise on a trading surface and makes a column impossible to
# scan. Formatting here rather than in `positions_frame` deliberately: the DataFrame
# keeps real floats, so the column still sorts numerically and the `.style` sign
# colouring still sees a number.
#
# Money to 2dp; prices to 8dp with trailing zeros optional (`[000000]`), which shows
# 374.0976 for an equity and a bare 6480 for an ES contract rather than 6480.0000.
#
# Eight, not four. This is the browser-side twin of `order_confirm.price_text_safe` — the
# one price rendering in the system that Python cannot perform, because numbro formats in
# the page — and at four it disagreed with it: a 6E limit of 1.08455 read 1.0846 and a ZN
# stop of 110.171875 read 110.1719 on the working-order book, while the card and the Gate 2
# dialog showed both exactly (audit 2026-09-13 finding A-3, on the surface the first fix
# missed; review 2026-09-14). Eight covers every instrument this account quotes, with the
# finest at six, and the optional form means nothing gains trailing zeros. A test asserts
# this against what the shared formatter really produces rather than against a number.
_MONEY_FORMAT = "0,0.00"
_PRICE_FORMAT = "0,0.00[000000]"
_QTY_FORMAT = "0,0.[00000000]"  # fractional-share and futures quantities alike
# Percentages to 2dp with an explicit sign, so a gain and a loss are never distinguished
# only by a minus that is easy to miss in a dense numeric column — the same rule
# `fmt_signed` applies to money.
# Numbro (which Bokeh's NumberFormatter uses) treats "%" as a PERCENTAGE format: it
# multiplies the value by 100 before rendering. So the frame stores the fraction — 0.0412
# — and this renders it as "+4.12%". Storing 4.12 and formatting with "%" would print
# "+412.00%". Verified in a browser, not inferred from the format string.
_PERCENT_FORMAT = "+0,0.00%"
# A price DIFFERENCE, not a price: same precision as `_PRICE_FORMAT` but with the
# sign always shown. "Change" rendered 8.77 next to "% Change" +2.25 when this was
# missing (seen in the browser 2026-08-07) — one signed, one not, for the same move.
_SIGNED_PRICE_FORMAT = "+0,0.00[000000]"

# Currency symbols the money columns may prefix. **USD only, deliberately.**
#
# The standing rule is never a bare `$` — it is shared by USD/MXN/CAD/AUD/HKD/SGD, and a
# wrong-currency price reads as an ordinary one, which is how IGV once showed a US ETF
# priced in MXN. The symbol is added here because the user weighed that against this
# account specifically (2026-08-07): those currencies have never been traded, the Name
# column now spells the instrument out, and the `Ccy` column carries the ISO code beside
# every figure.
#
# What keeps it safe is that it is **conditional, not hardcoded** — which is what "when
# applicable" asked for. A Bokeh formatter is per COLUMN, not per row, so a table holding
# two currencies cannot symbol them differently; `_money_formats` therefore adds a symbol
# only when the whole book agrees on one currency it knows, and renders bare numbers
# otherwise. A currency absent from this map is not guessed at.
_CURRENCY_SYMBOLS = {"USD": "$"}


def book_currency(snapshot: DashboardSnapshot) -> str:
    """The one currency every open position shares, or `""` when they differ.

    `""` is the honest answer for a mixed book *and* for an empty one: in both cases
    there is no single currency the money columns could be labelled with, and guessing
    the account's base currency would put a symbol on figures denominated in another.
    """
    currencies = {p.currency for p in snapshot.positions if p.currency}
    return currencies.pop() if len(currencies) == 1 else ""


def _money_formats(currency: str) -> dict[str, str]:
    """Number formats for the money columns, symbolled when the book allows it.

    Returns the bare formats for a mixed book, an empty one, or any currency not in
    `_CURRENCY_SYMBOLS` — never a symbol chosen by assumption.
    """
    sym = _CURRENCY_SYMBOLS.get(currency, "")
    return {
        "Avg entry": f"{sym}{_PRICE_FORMAT}",
        "IBKR basis": f"{sym}{_PRICE_FORMAT}",
        "Basis Δ": f"{sym}{_MONEY_FORMAT}",
        "Last": f"{sym}{_PRICE_FORMAT}",
        # The sign leads the symbol: "+$0.18", not "$+0.18".
        "Change": f"+{sym}{_PRICE_FORMAT}" if sym else _SIGNED_PRICE_FORMAT,
        "Market value": f"{sym}{_MONEY_FORMAT}",
        "Unrealised": f"{sym}{_MONEY_FORMAT}",
    }


# Rows per displayed page. Distinct from `dashboard_data._POSITIONS_PAGE_SIZE`, which is
# IBKR's own 30-per-request paging — this is purely how many rows the table shows at once.
# Paginate rather than grow: the data layer follows every IBKR page, so a large book would
# otherwise stretch the pane past the viewport and push the reconciliation line — the one
# thing on this tab that must not be missed — off screen.
_POSITIONS_ROWS_PER_PAGE = 15

# What each column actually is, on hover. Two of these are genuinely ambiguous on a
# trading surface: "Avg cost" is IBKR's `avgCost`, which for a futures position is per
# contract including the multiplier, and "Unrealised" is open P&L, not the day's move.
_POSITION_TOOLTIPS = {
    "Symbol": "IBKR's ticker; for a future the exchange local symbol (ESU6) once its contract "
    "info has been read, so the month is in the symbol itself (2026-09-10).",
    "Name": "IBKR's own instrument name, so a row is never acted on from an ambiguous "
    "ticker alone. IBKR truncates this field itself (IGV arrives as 'ISHARES "
    "EXPANDED TECH-SOFTWA'); a short name is theirs, not a display limit. Blank "
    "when IBKR omits it, which happens on the same lean rows that omit ticker. For a "
    "future the contract month follows the name (E-mini S&P 500 · Sep18'26), from IBKR's "
    "fullName (2026-09-10).",
    "Qty": "Signed position size. Negative is short.",
    "Avg entry": "Where the position was actually entered: average price of the open "
    "lots, FIFO over this account's own fills, excluding commission. Blank "
    "when it could not be reconstructed exactly — never estimated.",
    "IBKR basis": "IBKR avgPrice — the cost basis their P&L uses. Includes commission "
    "and any cost-basis adjustment, so it is a fiscal figure, not a level "
    "that was ever traded at.",
    "Basis Δ": "(IBKR basis - avg entry) * qty * multiplier: exactly how much of the "
    "Unrealised column comes from the basis rather than from the market.",
    "Last": "Live top-of-book (snapshot field 31) when available, otherwise IBKR's "
    "cached mktPrice. Market value and Unrealised beside it are IBKR's own, "
    "computed on their cached price, so they lag this column slightly.",
    "Change": "Snapshot field 82 — last price minus the previous trading day's close. "
    "IBKR's figure, not computed here. Blank until the contract's stream has "
    "opened, which takes one poll.",
    "% Change": "Snapshot field 83 — the same difference as a percentage, IBKR's own. "
    "This is the DAY's move; Unrealised is the move since you entered.",
    "Market value": "IBKR mktValue. For futures the ledger reports this as open P&L "
    "rather than notional.",
    "Unrealised": "Open P&L on the position. Not the day's change, and not realised.",
    "% Unrealised": "Unrealised over IBKR's cost basis (avgCost x qty). BOTH halves are "
    "IBKR's, so this ties to their screen. A percentage against 'Avg "
    "entry' would be a different number — on IGV the two bases differ by "
    "669.62. Short positions divide by the absolute basis, so a "
    "profitable short reads positive.",
    "Class": "IBKR assetClass — STK, FUT, OPT, CASH.",
    "Ccy": "The position's own currency — it need not be the account's base currency.",
}


# A never-polled snapshot, used only to give the Tabulator its column headers at build
# time, so `positions_frame`'s empty-frame path runs on construction rather than first
# appearing on a live session with no positions. `as_of` is a fixed sentinel rather than
# `datetime.now()`: nothing reads it, and a module-level clock read is an import-time side
# effect that would make import order observable.
_EMPTY = DashboardSnapshot(as_of=datetime.min.replace(tzinfo=UTC))


_ORDER_COLUMNS = [
    "Order",
    "Symbol",
    "Name",
    "Side",
    "Qty",
    "Filled",
    "Limit",
    "Stop",
    "Type",
    "TIF",
    "Outside RTH",
    "Status",
    "Origin",
]

_ORDER_NUMERIC = ["Qty", "Filled", "Limit", "Stop"]
"""The order columns that are numbers, and so need the same treatment as the positions
table's: a format and right alignment. Without them IBKR's raw floats render as `1.0` and
`6000.5` against `1` and `6,000.50` two tabs away — the same unscannable column the
positions table already fixed once.

**No currency column.** `/iserver/account/orders` is not documented to carry one and
`parse_orders` extracts none, so `Limit` renders as a bare number — the same thing
`fmt_money` does for an unknown currency, and the same choice `order_flow._price_suffix`
makes on the approval screen. Inventing a code from an unverified field would be worse
than omitting it.
"""

_ORDER_TOOLTIPS = {
    "Order": "IBKR's order id — the identity `propose_cancel` / `propose_modify` need.",
    "Symbol": "IBKR's ticker; for a future the exchange local symbol (ESU6) once its contract "
    "info has been read, so the month is in the symbol itself (2026-09-10).",
    "Name": "IBKR's companyName; for a future the contract month follows, from IBKR's "
    "description1 without its bracketed multiplier (E-mini S&P 500 · Sep18'26).",
    "Qty": "Total order size, not the remainder.",
    "Filled": "How much has executed. Shows 0 when IBKR reports no remaining quantity, "
    "which is the safe direction to be wrong in.",
    "Limit": "The resting limit price where the order type has one. No currency: the "
    "live-order feed does not carry one, so none is claimed.",
    "Stop": "The stop (trigger) price, from IBKR's auxPrice / stop_price — a stop's price is "
    "not a limit, so it has its own column (2026-09-04).",
    "Outside RTH": "Whether the order may act outside regular trading hours, as IBKR reports it. "
    "Decides when a stop on a US future can trigger. '—' = not reported by IBKR "
    "for this order (measured on futures rows) — not 'No'.",
    "Status": "IBKR's own status string, verbatim.",
    "Origin": "ClaudIA for orders staged through this app; external for TWS, mobile or the "
    "web portal — those cannot be modified or cancelled through the API.",
}


def _yes_no_or_dash(value: bool | None) -> str:
    """Render a three-state attribute honestly: True → Yes, False → No, None → '—'.

    The dash is "IBKR did not report it", which on a trading surface must never read as
    "No" (unknown is not a negative claim).
    """
    if value is None:
        return "—"
    return "Yes" if value else "No"


def orders_frame(snapshot: DashboardSnapshot) -> pd.DataFrame:
    """Working orders as the DataFrame the `Tabulator` renders.

    Always the full column set even with no rows, for the same reason as
    `positions_frame`: a zero-column `Tabulator` renders as a blank rectangle and reads
    as a broken widget rather than as an empty book.

    **An empty frame here means "no working orders", and it is the caller's job to only
    show it when that is true.** `snapshot.orders is None` means the book was never
    established — see `orders_status_line`. Drawing an empty table for a failed lookup
    would tell a trader they have nothing resting, which is the most dangerous sentence
    this panel could say by accident.
    """
    rows = [
        {
            "Order": o.order_id,
            "Symbol": display_symbol(o.symbol, o.sec_type, o.conid, snapshot.identities),
            "Name": order_display_name(o),
            "Side": o.side,
            "Qty": o.quantity,
            "Filled": o.filled,
            "Limit": o.price,
            "Stop": o.stop_price,
            "Type": o.order_type,
            # Blank = order status has not given a TIF yet (gap #70): a dash, like any
            # other unknown on this table, never a value.
            "TIF": o.tif or "—",
            "Outside RTH": _yes_no_or_dash(o.outside_rth),
            "Status": o.status,
            "Origin": "ClaudIA" if o.is_claudia_staged else "external",
        }
        for o in (snapshot.orders or ())
    ]
    return pd.DataFrame(rows, columns=_ORDER_COLUMNS)


def orders_status_line(snapshot: DashboardSnapshot) -> str:
    """One line above the table, distinguishing empty from unknown.

    Three states, because there are three: the book was read and is empty, the book was
    read and has orders, or it could not be read at all. The third is not an empty book
    and must never render as one — orders come from `/iserver/*`, which can be down while
    the `/portfolio/*` figures on the other tabs are perfectly live.
    """
    if snapshot.orders is None:
        return (
            "_**Order book unavailable** — the brokerage session is not answering. "
            "This is not the same as having no working orders; nothing is claimed here._"
        )
    if not snapshot.orders:
        return "_No working orders._"
    staged = sum(1 for o in snapshot.orders if o.is_claudia_staged)
    return f"_{len(snapshot.orders)} working order(s) — {staged} staged by ClaudIA._"


# ── Fills tab (gap #68, 2026-09-25) ─────────────────────────────────────────────

_FILL_COLUMNS = [
    "Executed (ET)",
    "Side",
    "Qty",
    "Symbol",
    "Price",
    "Venue",
    "Commission",
    "Order ref",
    "Execution ID",
]
"""The executions not yet on a statement, one row per IBKR execution.

Membership is the P&L pane's Daily rule (gap #69): an execution is listed if and only if
its id is not yet a Flex `execution_key`. No trade date is derived or shown — "Executed"
is IBKR's UTC clock read in Eastern time, a clock reading and not a trade date — and a
row leaves the tab when its id appears on a statement, never on a clock. The operator's
purpose (2026-09-24): "an immediate glance and confirmation with execution IDs (real)",
so the id is full and verbatim, and the chat's fill message carries the same one.

**No currency column**, as on the Orders tab: `/iserver/account/trades` carries no
currency field (its documented response has none; the 2026-09-25 capture had none), so
`Price` and `Commission` are bare numbers rather than a code guessed from the listing.
"""

_FILL_NUMERIC = ["Qty", "Price", "Commission"]

_FILL_TOOLTIPS = {
    "Executed (ET)": "IBKR's execution time (documented as UTC) read in New York time, with "
    "its Eastern date. A clock reading, not a trade date: IBKR states the trade date only "
    "in its statement (gap #69), and an evening futures fill belongs to the next one.",
    "Side": "BUY or SELL, from IBKR's B / S.",
    "Qty": "Shares or contracts executed, always positive; the side says which way.",
    "Symbol": "IBKR's ticker; for a future the exchange local symbol (ESU6) once its contract "
    "info has been read, so the month is in the symbol itself (2026-09-10).",
    "Price": "The execution price as IBKR reports it. No currency: the trades feed carries "
    "none, so none is claimed.",
    "Venue": "IBKR's `exchange`: where the execution happened (DARK, IBKRATS, CME…).",
    "Commission": "IBKR's `commission` for this execution, a bare number for the same reason "
    "as the price.",
    "Order ref": "The `cOID` given at placement — `CLAUDIA-…` for an order staged here. '—' = "
    "IBKR reports none, as for an order placed in TWS, mobile or the web portal.",
    "Execution ID": "IBKR's execution id, verbatim. The same id is on the chat's fill "
    "message and on `/iserver/account/trades`; a fill is on a statement once this id is "
    "a Flex execution_key, and then it leaves this tab.",
}


def fills_frame(snapshot: DashboardSnapshot) -> pd.DataFrame:
    """The executions not yet on a statement, as the DataFrame the `Tabulator` renders.

    Rows come from `snapshot.pending.fills` in the order the data layer gives them
    (newest first) and are not re-sorted here. Always the full column set with no rows
    for an empty or unreadable window — a zero-column `Tabulator` renders as a blank
    rectangle — and it is `fills_status_line`'s job to say which of the two it is.
    """
    pending = snapshot.pending
    rows = [
        {
            "Executed (ET)": execution_time_et(f.trade_time, with_date=True) or "—",
            "Side": "BUY" if f.is_buy else "SELL",
            "Qty": abs(f.signed_quantity),
            "Symbol": display_symbol(f.symbol, f.asset_class, f.conid, snapshot.identities),
            "Price": f.price,
            "Venue": f.exchange or "—",
            "Commission": f.commission,
            "Order ref": f.order_ref or "—",
            "Execution ID": f.execution_id,
        }
        for f in (pending.fills if pending is not None else ())
    ]
    return pd.DataFrame(rows, columns=_FILL_COLUMNS)


def fills_status_line(snapshot: DashboardSnapshot, stale: bool = False) -> str:
    """One line above the Fills table: which of four states it is in, and against what.

    * **waiting** — nothing has been polled yet;
    * **unavailable** — `pending is None`: the executions could not be read from the
      gateway. Not the same claim as "no fills", and never rendered as one (the order
      book's rule);
    * **empty** — every execution in IBKR's window is already on a statement;
    * **rows** — with the count and the statement the membership is measured against.

    `stale` prepends the Daily tab's warning rather than blanking the list: a resting
    order can fill while the gateway is unreachable, so a stale list is a floor, and a
    floor stated as a floor is worth more than a blank. The line names the Flex
    statement, never "today": no trade date is asserted here (gap #69).
    """
    if not snapshot.breakdowns:
        return "_Fills: waiting for the first poll…_"
    pending = snapshot.pending
    if pending is None:
        return (
            "_**Fills unavailable** — the executions could not be read from the IBKR "
            "gateway. This is not the same as having no fills; nothing is claimed here._"
        )
    cov = snapshot.coverage
    against = (
        f"Flex through **{cov.through.isoformat()}**"
        if cov is not None and cov.through is not None
        else "no statement in the local store yet"
    )
    warn = (
        "**⚠ Not current — the gateway is unreachable or the last poll failed. Any fill "
        "since then is missing, so this list is a floor.**\n\n"
        if stale
        else ""
    )
    if not pending.fills:
        return (
            f"{warn}_No executions awaiting a statement — every fill in IBKR's window is on "
            f"a statement ({against})._"
        )
    return (
        f"{warn}_{len(pending.fills)} execution(s) not yet on a statement ({against}) — "
        "IBKR's own record, execution ids verbatim. A row leaves this list when its id "
        "appears on a statement, never on a clock._"
    )


def _as_fraction(percent: float | None) -> float | None:
    """A percentage as the fraction numbro's `%` format expects. None stays None.

    IBKR reports 83 (Change %) as 2.39 meaning 2.39%, and `pct_unrealised` is computed
    the same way. Numbro multiplies by 100 when it sees `%` in a format string, so the
    frame must hold 0.0239 for the cell to read "+2.39%". Dividing here rather than
    changing what the properties mean: `Position.pct_unrealised` stays a percentage,
    which is what every caller and test reads it as — only the display layer converts.
    """
    return None if percent is None else percent / 100.0


def positions_frame(snapshot: DashboardSnapshot) -> pd.DataFrame:
    """Open positions as the DataFrame the `Tabulator` renders.

    Always returns the full column set, even with no rows: a `Tabulator` handed a
    zero-column frame renders as a blank rectangle with no headers, which reads as a
    broken widget rather than as an empty book.

    **"Avg entry" leads and "IBKR basis" follows**, which is the opposite of the order
    the two were added in and is the point of the column. The entry is where the
    position was actually taken; the basis is a fiscal figure that absorbs commission
    and cost-basis adjustments. A trader reading left to right should meet the tradeable
    number first.

    Both are per *unit*. The table used to show IBKR's `avgCost`, which is per
    *contract*: CL SEP2026 rendered an "Avg cost" of 80,932.36 beside a "Last" of 75.14
    (measured 2026-08-04). `Position.average_price` is the per-unit field.

    An entry that could not be reconstructed with certainty renders as `None`, not as a
    guess and not as zero — `economic_entries` declines rather than approximates, and
    the blank is what that decision looks like on screen.
    """
    rows = [
        {
            "Symbol": display_symbol(p.symbol, p.asset_class, p.conid, snapshot.identities),
            "Name": position_display_name(p),
            "Qty": p.quantity,
            "Avg entry": p.economic_entry,
            "IBKR basis": p.average_price,
            "Basis Δ": p.basis_delta_value,
            "Last": p.last_price,
            "Change": p.quote.change if p.quote else None,
            "% Change": _as_fraction(p.quote.change_pct if p.quote else None),
            "Market value": p.market_value,
            "Unrealised": p.unrealised_pnl,
            "% Unrealised": _as_fraction(p.pct_unrealised),
            "Class": p.asset_class,
            "Ccy": p.currency,
        }
        for p in snapshot.positions
    ]
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in _POSITION_COLUMNS})
    return pd.DataFrame(rows, columns=_POSITION_COLUMNS)


# Below this, in the position's own currency, the gap between IBKR's basis and the
# actual entry is commission and rounding rather than anything a trader should act on.
# CL measured 4.72 on two contracts on 2026-08-04, which is what a clean position looks
# like; IGV measured 669.62, which is not. Set at 50 so the line stays quiet on ordinary
# positions and speaks up on the ones where the basis has genuinely moved away from the
# fills. A per-position threshold, not a total: one distorted position matters even in a
# book whose net is small.
_BASIS_NOTE_THRESHOLD = 50.00


def quote_note(snapshot: DashboardSnapshot) -> str:
    """The two-price-sources disclosure, and the non-live-feed warnings.

    `Last`/`Change`/`% Change` are a live top-of-book quote; `Market value` and
    `Unrealised` are IBKR's own, computed on the cached price its positions endpoint
    serves. **They will not tie**, and that is accepted rather than hidden: forcing
    agreement would mean either recomputing IBKR's figures (losing the reconciliation
    against their screen, which is the point of those columns) or showing a price
    measured flat to the tick across three minutes of an open session. A lag stated is
    fine; a lag concealed is the failure this pane is written against (user, 2026-08-07).

    Three conditions get named individually because each means something different, and
    none of them is "the price is fine":

    * **not live** — a delayed, frozen or unsubscribed feed (6509 not starting `R`);
    * **prior close** — field 31 came back `C`-prefixed, so it is yesterday's close and
      not a trade that happened today;
    * **halted** — `H`-prefixed; there is a price but no tradeable market behind it.

    Silent when every position has a live quote, so the line appears only when it has
    something to say.
    """
    quoted = [p for p in snapshot.positions if p.quote is not None]
    if not snapshot.positions:
        return ""
    if not quoted:
        return "_Last is IBKR's cached price — no live quote yet. Streams open on the next poll._"
    stale = [p.symbol for p in quoted if not p.quote.is_live]  # type: ignore[union-attr]
    closes = [p.symbol for p in quoted if p.quote.last_is_close]  # type: ignore[union-attr]
    halted = [p.symbol for p in quoted if p.quote.halted]  # type: ignore[union-attr]
    parts = [
        "_**Last / Change / % Change** are live top-of-book. **Market value** and "
        "**Unrealised** are IBKR's own, on the cached price their positions endpoint "
        "serves, so they lag Last slightly and will not tie to it exactly._"
    ]
    if stale:
        parts.append(f"_⚠ Not a real-time feed: {', '.join(sorted(stale))}._")
    if closes:
        parts.append(
            f"_⚠ Last is the previous close, not a trade today: {', '.join(sorted(closes))}._"
        )
    if halted:
        parts.append(f"_⚠ Trading halted: {', '.join(sorted(halted))}._")
    return "  \n".join(parts)


def basis_note(snapshot: DashboardSnapshot) -> str:
    """State in words where IBKR's basis has drifted materially from the real entry.

    The column carries every position; this line names the ones worth looking at, and
    only those. A trader scanning a book does not read a column of small numbers looking
    for a big one — and the number that matters here is not the per-share difference but
    what it does to the Unrealised column beside it, which is what this states.

    Silent when nothing crosses `_BASIS_NOTE_THRESHOLD`, including when nothing could be
    reconstructed at all. A note that fires on every position would be scrolled past
    within a day, and one that announced "no drift" for a book it could not check would
    be claiming a verification it never ran.
    """
    material = sorted(
        (
            (p, value)
            for p in snapshot.positions
            if (value := p.basis_delta_value) is not None and abs(value) >= _BASIS_NOTE_THRESHOLD
        ),
        key=lambda pair: abs(pair[1]),
        reverse=True,
    )
    if not material:
        return ""
    parts = [
        f"**{p.symbol}** {fmt_signed(value, p.currency)} "
        f"(entry {p.economic_entry:,.4f} vs basis {p.average_price:,.4f})"
        for p, value in material
    ]
    return (
        "⚠ _IBKR's cost basis differs materially from where these positions were "
        "actually entered, so that much of their Unrealised P&L is basis rather than "
        "market: " + " · ".join(parts) + "._"
    )


def reconciliation_line(rec: Reconciliation) -> str:
    """One line saying whether the positions and the ledger agree, and by how much.

    Three outcomes, and the middle one is why this is not a boolean:

    * **not checked** — no ledger or no positions. Says so. Rendering "reconciled" for
      a check that never ran would be the worst of the three.
    * **mixed currency** — a position denominated outside the ledger's currency makes
      the sum invalid, so no delta is claimed. IGV once priced a US ETF in MXN.
    * **checked** — the delta, with a pass/fail against `RECONCILE_TOLERANCE`.

    A failure leads with lag rather than asserting a data error: the usual cause is
    `get_positions` going stale while a fast futures leg keeps ticking in the ledger
    (measured, `dashboard_data.RECONCILE_TOLERANCE`), not a wrong number.
    """
    if not rec.checked:
        return "_Position/ledger reconciliation: not checked (no ledger or no positions)._"
    if rec.mixed_currency:
        return (
            "⚠ _Positions span more than one currency, so they cannot be summed against "
            f"the {rec.currency} ledger. No reconciliation claimed._"
        )
    summed = fmt_signed(rec.positions_total, rec.currency)
    ledger = fmt_signed(rec.ledger_total, rec.currency)
    if rec.agrees:
        return (
            f"_Reconciles with the ledger: positions sum {summed}, ledger {ledger} "
            f"(delta {rec.delta:,.2f} {rec.currency})._"
        )
    return (
        f"**⚠ Positions do not reconcile with the ledger: {summed} summed vs {ledger} "
        f"— a gap of {rec.delta:,.2f} {rec.currency}.** Most often `get_positions` has "
        f"gone stale while a fast-moving leg kept ticking in the ledger. A gap past "
        f"{RECONCILE_TOLERANCE:,.2f} is beyond ordinary drift, so verify against IBKR "
        f"before trading on either figure."
    )


# Half of 0.01%, the smallest move `_PERCENT_FORMAT` can render. The percentage columns
# hold FRACTIONS (`_as_fraction`: 2.39% is stored as 0.0239), so money's half-cent band is
# a hundred times too wide for them.
_PERCENT_FLAT_BAND = 0.00005


def _sign_style(value: Any) -> str:
    """Green/red/neutral CSS for one signed MONEY cell; nothing at all for a non-number.

    Defers to `pnl_color` so the tiles, the tables and the chart cannot disagree. This
    carried its own strict-sign rule until 2026-09-23, which differed from the tiles inside
    the half-cent band: a cell displaying `-0.00` was painted red.

    **Money only** — use `_percent_sign_style` for the fraction-valued columns.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return f"color: {pnl_color(float(value))}"


def _percent_sign_style(value: Any) -> str:
    """The same rule at the percentage columns' own precision.

    **Why this is not `_sign_style` (found reviewing 2026-09-23's diff, not by the suite).**
    That function began deferring to `pnl_color`, whose default band is half a cent because
    money shows two decimals. These columns store a fraction and render it through
    `"+0,0.00%"`, so the identical numeric band spanned ±0.5 **percent**: a position up
    0.49% displayed `+0.49%` and was painted neutral grey. On a quiet day that is most of
    the column.

    The rule itself never changed — a cell that displays as zero is not a loss — only the
    scale it is measured at.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return f"color: {pnl_color(float(value), flat_band=_PERCENT_FLAT_BAND)}"


# ── Round-trip stats ──────────────────────────────────────────────────────────


def stats_markdown(
    window: RealisedWindow | None,
    stats: RoundTripStats | None,
    label: str,
    currency: str | None = None,
) -> str:
    """The **settled** statement view: what Flex has confirmed, and nothing newer.

    Flex is T+1 and structurally cannot contain the most recent day(s): on 2026-08-06 it
    was two days behind, so this block read -6,175.88 for a week whose realised was
    -16,480.46. Until that date it was titled "realised & round trips" and sat beneath a
    total that included unsettled fills, two figures for one window that differed by ten
    thousand. Caught by rendering the page in a browser.

    Since gap #69 (2026-09-24) every dated figure on the pane is settled — the breakdown
    table above is Flex too — and what is not yet on a statement is the Daily tab,
    never added to a dated one. So this block and the table agree on their source; the
    footnote says so and points at Pending for the rest.

    Two different bases appear here on purpose, and are labelled as such:

    * **P&L totals** come from `flex_trade` — the authoritative net figure, wash sales
      already netted in.
    * **Round-trip counts** come from `flex_lot` — genuine open→close trips. Executions
      would inflate the denominator with opening legs and wash-sale-zeroed closes that
      are neither a win nor a loss (measured 2026-08-04 on 2026 YTD: 636 executions with
      346 zeros, against 360 closed lots with none).

    Showing lot-derived money as "realised" would silently overstate losses by the
    disallowed amount, so the gross win/loss figures below are explicitly marked
    lot-basis and are never presented as the realised total.
    """
    if window is None or stats is None:
        return "_No trade data in the local store._"
    # `currency` lets the caller substitute the account's base currency for a window
    # that realised nothing and therefore has no currency of its own.
    ccy = currency if currency is not None else window.currency_label
    lines = [
        f"#### {label}",
        "",
        "| | |",
        "|---|---|",
        f"| Settled realised P&L | **{fmt_signed(window.total, ccy)}** |",
        f"| Futures | {fmt_signed(window.asset_total('FUT'), ccy)} |",
        f"| Equities & options | {fmt_signed(window.asset_total(*_NON_FUTURES), ccy)} |",
        f"| Executions | {window.trade_count} |",
        f"| Closed round trips (lots) | {stats.closed_lots} |",
    ]
    if stats.closed_lots:
        rate = "—" if stats.win_rate is None else f"{stats.win_rate:.1f}%"
        lines += [
            f"| Winners / losers | {stats.winners} / {stats.losers}"
            + (f" · {stats.scratches} scratch" if stats.scratches else "")
            + f" — **{rate}** win rate |",
            f"| Gross win / loss (lot basis) | {fmt_signed(stats.gross_win, ccy)} / "
            f"{fmt_signed(stats.gross_loss, ccy)} |",
        ]
    lines += [
        "",
        "_**Settled only** — every figure here comes from the Flex statement dataset "
        "(IBKR publishes a day's trades T+1, so today is never in it). Executions not yet "
        "on a statement are under **Daily**._\n\n"
        "_Totals are execution-basis (`flex_trade`, the authoritative settled figure). "
        "Win/loss counts and gross figures are lot-basis (`flex_lot`), pre-wash-sale, "
        "and must never be read as realised P&L._",
    ]
    return "\n".join(lines)


def ledger_markdown(snapshot: DashboardSnapshot) -> str:
    """The ledger detail block, including IBKR's own futures-only split.

    `futuresonlypnl` is reported verbatim under IBKR's own field name. The residual
    (`realizedpnl - futuresonlypnl`) is deliberately **not** computed and labelled
    "equities". `realizedpnl`'s scope is now settled (today — see
    `dashboard_data.REALISED_LEDGER_WINDOW`), but `futuresonlypnl` still carries no
    published description at all, and nothing establishes that the two are
    complementary. The exact per-asset split is available — from the Flex windows, where
    it is measured rather than inferred — and that is where it is shown.

    That restraint was vindicated live on 2026-08-04, in **two** reads an hour apart at
    different market values:

        read 1   futuremarketvalue  -11,607.50   futuresonlypnl  -11,607.50
                 unrealizedpnl      -11,618.32   realizedpnl      -2,656.11
        read 2   futuremarketvalue  -11,223.30   futuresonlypnl  -11,223.30
                 unrealizedpnl      -11,202.04   realizedpnl      -2,656.11

    `futuresonlypnl` is **exactly** `futuremarketvalue` both times — not close to it,
    equal — while `realizedpnl` did not move at all between them. Treating it as the
    futures half of a realised split would have been wrong by an order of magnitude and
    in the wrong direction. Two reads on one futures-heavy account is still evidence
    rather than proof, which is why the field is displayed under IBKR's own name with
    the observation attached and no arithmetic performed on it.
    """
    led = snapshot.ledger
    if led is None:
        return "_Account ledger unavailable._"
    extra = (
        f"\n\n_Other currency balances also held: {', '.join(led.other_currencies)}._"
        if led.other_currencies
        else ""
    )
    return (
        f"#### Account ledger ({led.currency})\n\n"
        f"| | |\n|---|---|\n"
        f"| Net liquidation | **{fmt_money(led.net_liquidation, led.currency)}** |\n"
        f"| Cash | {fmt_money(led.cash, led.currency)} |\n"
        f"| Settled cash | {fmt_money(led.settled_cash, led.currency)} |\n"
        f"| Stock market value | {fmt_money(led.stock_market_value, led.currency)} |\n"
        f"| Futures market value | {fmt_money(led.futures_market_value, led.currency)} |\n"
        f"| Unrealised P&L | **{fmt_signed(led.unrealised_pnl, led.currency)}** |\n"
        f"| IBKR `futuresonlypnl` | {fmt_signed(led.futures_only_pnl, led.currency)} |\n"
        f"| {realised_ledger_label()} | **{fmt_signed(led.realised_pnl, led.currency)}** |"
        f"{extra}\n\n"
        "_`realizedpnl` is today's realised P&L on IBKR's real-time average cost — it "
        'equals the sum of the per-position `realizedPnl`, which IBKR defines as "the '
        'total profit made today through trades" (measured and cross-checked '
        "2026-08-04). `futuresonlypnl` still carries no published description. Measured "
        "live the same day, `futuresonlypnl` was **exactly** "
        "`futuremarketvalue` and four times the realised figure, so it is listed here "
        "among the market-value rows under IBKR's own field name rather than treated as "
        "a realised split. No residual is computed: `realizedpnl - futuresonlypnl` is "
        "not a documented equities total — the measured per-asset split is in the "
        "windows above._"
    )


# ── The view ──────────────────────────────────────────────────────────────────


class DashboardView:
    """The dashboard's widgets plus the one `refresh(snapshot)` that repaints them all.

    Two public attributes, both Panel layouts for the caller to place where it likes:

    * `kpi_strip` — the tile row and the freshness line. `panel_app` puts it at the top
      of the session root so account state is glanceable from any tab.
    * `tabs` — `Tabs(Chart · Positions · Orders · Fills · P&L)`.

    They are separate rather than one component because they belong in different places
    in the layout; keeping them as standalone factories is also what makes re-parenting
    (a `FloatPanel`, a `GridStack` cell, a second `pn.serve` route) a layout change
    rather than a rewrite.

    Built with no data — `refresh` is what fills it — so a session renders instantly and
    populates on the poller's first snapshot rather than blocking page load on IBKR.

    Holds the last snapshot so the P&L tab's window selector can re-render without
    waiting for the next poll: a radio click must feel immediate, and the data it needs
    is already in memory.
    """

    def __init__(self, chart_pane: Any = None) -> None:
        """Build every widget. `chart_pane` becomes the Chart tab; None gives a stub.

        The chart pane is injected rather than imported so this module has no dependency
        on `panel_chart`, and so the tab structure is testable without building a pane
        that reaches for the process toolkit.
        """
        self._snapshot: DashboardSnapshot | None = None
        # Tri-state: None = no successful poll yet, so no transition has happened and no
        # toast is owed. See `_notify_staleness`.
        self._was_stale: bool | None = None

        self._tiles: dict[str, pn.indicators.Number] = {
            "net_liq": self._tile("Net liquidation"),
            "cash": self._tile("Cash"),
            "unrealised": self._tile("Unrealised P&L", signed=True),
            "realised_ledger": self._tile(realised_ledger_label(), signed=True),
            "realised_week": self._tile("Realised this week", signed=True),
        }
        # Tiles only. A win-rate grid lived at the right end of this row until 2026-08-07
        # and was removed as clutter (user): a small table wedged beside five Number
        # indicators reads as an afterthought, and neither figure it carried is lost —
        # the week is in the P&L pane, and what is not yet on a statement is its Pending
        # window (gap #69; it was a Daily window until 2026-09-24).
        self._freshness = safe_markdown("_Account data: waiting for the first poll…_")
        self.kpi_strip = pn.Column(
            pn.Row(*self._tiles.values(), sizing_mode="stretch_width"),
            self._freshness,
            sizing_mode="stretch_width",
        )

        # disabled=True is the Hard Rule 1 / data-integrity guard: Tabulator cells are
        # editable by default. No on_click or on_edit handler is bound anywhere in this
        # module, and the test suite asserts that emptiness directly.
        self._positions = pn.widgets.Tabulator(
            positions_frame(_EMPTY),
            disabled=True,
            show_index=False,
            # `fit_data` + explicit widths, NOT `fit_data_stretch`. The table went from
            # ten columns to fourteen on 2026-08-07 and the pane holds roughly half the
            # window, so under stretch everything from "Basis Δ" rightwards — every money
            # column — was cut off at the pane edge. Seen in the browser, which is the
            # only place it was visible: every string was correct and no test could fail.
            # `fit_data` sizes to the widths below and scrolls horizontally instead.
            # Measured at a 1920px viewport: 1292px of table in a 940px pane, so the
            # scroll is real rather than theoretical, and no width tuning closes a gap
            # that size without ellipsising money (user: a 27" screen removes the
            # constraint, so it is not pursued further).
            layout="fit_data",
            widths=_POSITION_WIDTHS,
            sizing_mode="stretch_width",
            height=380,
            formatters={
                "Qty": NumberFormatter(format=_QTY_FORMAT),
                **{c: NumberFormatter(format=f) for c, f in _money_formats("").items()},
                "% Unrealised": NumberFormatter(format=_PERCENT_FORMAT),
                "Change": NumberFormatter(format=_SIGNED_PRICE_FORMAT),
                "% Change": NumberFormatter(format=_PERCENT_FORMAT),
            },
            text_align=dict.fromkeys(
                [
                    "Qty",
                    "Avg entry",
                    "IBKR basis",
                    "Basis Δ",
                    "Last",
                    "Change",
                    "% Change",
                    "Market value",
                    "Unrealised",
                    "% Unrealised",
                ],
                "right",
            ),
            # Read-only affordances only. Filtering, sorting and paging change what is
            # displayed and nothing else — none of them can reach an order path, which
            # is the line Hard Rule 1 draws. `disabled=True` above still governs edits.
            header_filters=True,
            header_tooltips=dict(_POSITION_TOOLTIPS),
            pagination="local",
            page_size=_POSITIONS_ROWS_PER_PAGE,
        )
        # `.style` is a real pandas Styler and it survives every `value` reassignment,
        # rebinding to the new frame (verified 2026-08-04 against panel 1.9.3/pandas
        # 3.0.5), so the colour map is applied once here rather than on each repaint.
        # Panel types it `Any | None`; it is None only before the widget has a value,
        # which cannot be the case here since the constructor above was given a frame.
        styler = self._positions.style
        assert styler is not None  # noqa: S101 - narrowing for mypy, not a runtime guarantee
        styler.map(_sign_style, subset=_MONEY_SIGNED_COLUMNS)
        styler.map(_percent_sign_style, subset=_PERCENT_SIGNED_COLUMNS)
        self._positions_status = safe_markdown("_Positions: waiting for the first poll…_")
        self._reconciliation = safe_markdown("")
        self._basis_note = safe_markdown("")
        self._quote_note = safe_markdown("")
        # Currency the money formatters currently carry. Reassigning
        # `formatters` rebuilds the table, so it happens only on a change.
        self._money_currency = ""

        # Per-type detail for the selected window. Month and year live here rather than
        # on the KPI strip: the strip is for the two windows a trader checks constantly,
        # this is for the ones they study.
        self._pnl_breakdown = safe_markdown("_No closed trades in this window._")
        self._window = pn.widgets.RadioButtonGroup(
            # color=, not button_type=: `button_type` PendingDeprecationWarns on panel
            # 1.9 and the suite gates on warnings (same finding as Widget.name).
            label="Window",
            options=list(_WINDOW_LABELS),
            value="Weekly",
            color="light",
        )
        # Same Hard Rule 1 shape as the positions table: disabled=True and NO on_click /
        # on_edit handler bound anywhere. An order row is the one place in this app where
        # a click could plausibly be wired to "cancel this" — it must not be. Cancelling
        # goes through propose_cancel and both gates, never a table cell.
        self._orders = pn.widgets.Tabulator(
            orders_frame(_EMPTY),
            disabled=True,
            show_index=False,
            layout="fit_data_stretch",
            sizing_mode="stretch_width",
            height=300,
            formatters={
                "Qty": NumberFormatter(format=_QTY_FORMAT),
                "Filled": NumberFormatter(format=_QTY_FORMAT),
                "Limit": NumberFormatter(format=_PRICE_FORMAT),
                "Stop": NumberFormatter(format=_PRICE_FORMAT),
            },
            text_align=dict.fromkeys(_ORDER_NUMERIC, "right"),
            header_tooltips=dict(_ORDER_TOOLTIPS),
        )
        self._orders_status = safe_markdown("_Orders: waiting for the first poll…_")

        # The Fills tab (gap #68): the same Hard Rule 1 shape, disabled=True and NO
        # on_click / on_edit handler. A fill row is read, checked against the chat and
        # IBKR's record, and never acted on from here.
        self._fills = pn.widgets.Tabulator(
            fills_frame(_EMPTY),
            disabled=True,
            show_index=False,
            layout="fit_data_stretch",
            sizing_mode="stretch_width",
            height=300,
            formatters={
                "Qty": NumberFormatter(format=_QTY_FORMAT),
                "Price": NumberFormatter(format=_PRICE_FORMAT),
                "Commission": NumberFormatter(format=_PRICE_FORMAT),
            },
            text_align=dict.fromkeys(_FILL_NUMERIC, "right"),
            header_tooltips=dict(_FILL_TOOLTIPS),
        )
        self._fills_status = safe_markdown("_Fills: waiting for the first poll…_")

        # Sits between the window selector and the breakdown, and carries text only for
        # the Daily tab: which statement it is measured against, that the figures are
        # reconstructed from the account's own executions, and whether they are current.
        # Empty for the other three, whose provenance is the settled block further down.
        self._pnl_source_note = safe_markdown("")

        self._window.param.watch(self._on_window_change, "value")
        self._pnl_chart = pn.pane.HoloViews(None, sizing_mode="stretch_width")
        self._pnl_chart_note = safe_markdown("")
        self._pnl_stats = safe_markdown("_Realised P&L: waiting for the first poll…_")
        self._pnl_coverage = safe_markdown("")
        self._ledger_detail = safe_markdown("")

        # dynamic=True renders only the active tab, so the candlestick figure and the
        # realised-P&L figure are not both serialised on every page load. Verified
        # 2026-08-04 that this is safe with the repaint: widget identity survives
        # deactivation and a param set on a hidden tab's widget is present when that tab
        # is activated again — `refresh` can keep writing to all three unconditionally.
        self.tabs = pn.Tabs(
            ("Chart", chart_pane if chart_pane is not None else pn.Column()),
            (
                "Positions",
                pn.Column(
                    self._positions_status,
                    self._reconciliation,
                    self._basis_note,
                    self._quote_note,
                    self._positions,
                    sizing_mode="stretch_both",
                ),
            ),
            ("Orders", pn.Column(self._orders_status, self._orders, sizing_mode="stretch_both")),
            # Before P&L (operator 2026-09-25): a fill is looked for the moment it
            # happens; its P&L is the next tab's business.
            ("Fills", pn.Column(self._fills_status, self._fills, sizing_mode="stretch_both")),
            (
                "P&L",
                pn.Column(
                    self._window,
                    self._pnl_source_note,
                    self._pnl_breakdown,
                    self._pnl_chart,
                    self._pnl_chart_note,
                    self._pnl_stats,
                    self._pnl_coverage,
                    self._ledger_detail,
                    sizing_mode="stretch_both",
                ),
            ),
            dynamic=True,
            sizing_mode="stretch_both",
        )

    # ── Construction helpers ────────────────────────────────────────────────

    def _tile(self, label: str, signed: bool = False, fmt: str | None = None) -> Any:
        """One KPI tile. `label=` not `name=` — `Widget.name` deprecation-warns on 1.9.

        Signed tiles carry the sign-threshold colours; balance tiles stay neutral, since
        colouring a cash balance green would imply a judgement the number does not make.
        The format string is rewritten on refresh to carry the live ISO currency code.

        `default_color` must be a string — param rejects None (`Number.default_color` is
        a `String` parameter, verified 2026-08-04). It is what a tile with **no value**
        renders in, since `Number`'s threshold scan skips a None value entirely, so
        "inherit" is what keeps a not-yet-polled tile theme-coloured rather than forcing
        the package default of black onto a dark background.
        """
        return pn.indicators.Number(
            value=None,
            label=label,
            format=fmt or "{value:,.2f}",
            font_size="19pt",
            title_size="10pt",
            nan_format="—",
            colors=list(_PNL_COLORS) if signed else None,
            default_color="inherit",
            width=_TILE_WIDTH,
        )

    # ── Refresh ─────────────────────────────────────────────────────────────

    def refresh(self, snapshot: DashboardSnapshot, now: datetime | None = None) -> None:
        """Repaint every surface from one snapshot. Synchronous, no I/O.

        Called from a 5-second `pn.state.add_periodic_callback` on the session's own
        event loop, so it must stay cheap: everything it needs is already computed in
        the snapshot by the poller's background task.

        Never raises. A repaint that throws inside a periodic callback takes the timer
        down with it and freezes the dashboard silently — the exact failure mode the
        freshness line exists to expose, so it must not be caused by the repainter
        itself. A failure is logged and the previous frame stays on screen.

        **Once the account data is stale it is not drawn at all** (`without_account`).
        The alternative — last known figures under a STALE banner — asks the reader to
        notice a line of text before trusting a number, and a number that is minutes old
        looks exactly like one that is current. Blank is unambiguous; the status line
        still says how long it has been. The Flex-derived windows keep rendering
        throughout, because they never depended on the gateway.

        Staleness is four missed polls, not one: a single timed-out request must not
        blank a working dashboard for a second and then fill it back in.
        """
        try:
            self._snapshot = snapshot
            display = snapshot.without_account() if self.is_stale(snapshot, now) else snapshot
            self._refresh_tiles(display, now)
            self._refresh_positions(display)
            self._refresh_orders(display)
            self._refresh_fills(display, self.is_stale(snapshot, now))
            self._refresh_pnl(display, now)
            self._notify_staleness(snapshot, now)
        except Exception:
            log.exception("Dashboard repaint failed; leaving the previous frame up")

    def is_stale(self, snapshot: DashboardSnapshot, now: datetime | None = None) -> bool:
        """Whether the account half of `snapshot` should be treated as untrustworthy.

        One definition, shared by the status line and the notification, so the toast and
        the text on screen can never disagree about whether the data is good.
        """
        return bool(snapshot.error) or snapshot.age_seconds(now) > STALE_AFTER

    def _notify_staleness(self, snapshot: DashboardSnapshot, now: datetime | None = None) -> None:
        """Toast on the fresh↔stale transition only — never on every poll.

        The status line is always right, but only if you are looking at it. A trading
        surface losing its feed while the user reads the chat is worth interrupting for
        once; repeating it every five seconds would train them to dismiss it, which
        would cost more than the notification buys.

        Silent before the first successful poll: "the dashboard has not polled yet" is
        the normal first second of every session, not an incident.

        `pn.state.notifications` is None outside a served session (and would be None
        anyway had `notifications=True` not been passed to `pn.extension`), so the guard
        also covers the whole test suite and any headless embedding.
        """
        if snapshot.ledger is None and self._was_stale is None:
            return  # nothing has been established yet, so nothing has changed
        stale = self.is_stale(snapshot, now)
        previous, self._was_stale = self._was_stale, stale
        if previous is None or previous == stale:
            # `previous is None` is the FIRST established state, not a transition.
            # Toasting there fired "Account data is live again" one second into every
            # session, announcing a recovery from nothing.
            return
        notifications = pn.state.notifications
        if notifications is None:
            return
        if stale:
            # `safe_toast`, not `notifications.error`: the body is assigned with innerHTML
            # and this one interpolates IBKR/exception text (audit 2026-09-13, finding A-1).
            safe_toast(
                notifications,
                "error",
                f"Account data is stale — {short_reason(snapshot.error) or 'no recent poll'}",
                duration=0,  # 0 = sticky: a stale trading surface should not self-dismiss
            )
        else:
            safe_toast(notifications, "success", "Account data is live again.", duration=4000)

    def _refresh_tiles(self, snapshot: DashboardSnapshot, now: datetime | None) -> None:
        """KPI strip: ledger balances, live unrealised, the two realised figures.

        `ccy` is the empty string when there is no ledger, never a guessed "USD". Every
        tile whose value comes from that missing ledger is set to None, and `_set` drops
        the format for a None value, so the code is not rendered in that case anyway —
        but a placeholder currency sitting in a local is one edit away from being shown.
        """
        led = snapshot.ledger
        ccy = led.currency if led else ""
        self._set(
            self._tiles["net_liq"], led.net_liquidation if led else None, f"{{value:,.2f}} {ccy}"
        )
        self._set(self._tiles["cash"], led.cash if led else None, f"{{value:,.2f}} {ccy}")
        self._set(
            self._tiles["unrealised"], led.unrealised_pnl if led else None, f"{{value:+,.2f}} {ccy}"
        )
        self._tiles["realised_ledger"].label = realised_ledger_label()
        self._set(
            self._tiles["realised_ledger"],
            led.realised_pnl if led else None,
            f"{{value:+,.2f}} {ccy}",
        )

        # An empty week has no currency of its own (`currency_label` returns ""), so the
        # account's own base currency stands in — known, not assumed.
        #
        # The figure is the Flex week, `snapshot.week`, the same source as the P&L pane's
        # Weekly window, and the tile names the statement it runs through (gap #69). Until
        # 2026-08-06 the tile and the pane read different weeks, ten thousand apart, side
        # by side; from then until 2026-09-24 both were "bridged" with live fills dated by
        # their UTC timestamp, which is not the trade date. Executions not yet on a
        # statement are under the pane's Daily tab, never added to a dated figure.
        week = snapshot.week
        week_ccy = (week.currency_label or ccy) if week else ccy
        cov = snapshot.coverage
        self._tiles["realised_week"].label = (
            f"Realised this week · through {cov.through.isoformat()}"
            if cov is not None and cov.through is not None
            else "Realised this week"
        )
        self._set(
            self._tiles["realised_week"],
            week.total if week else None,
            f"{{value:+,.2f}} {week_ccy}".rstrip(),
        )
        self._freshness.object = freshness_line(snapshot, now)

    @staticmethod
    def _set(tile: Any, value: float | None, fmt: str) -> None:
        """Update one tile's value and format together, in a single param transaction.

        `param.update` rather than two assignments: setting `value` and `format`
        separately renders one intermediate frame with the new number under the old
        currency code, which on a multi-currency account is a wrong-currency figure on
        screen — briefly, but shown.

        A **None** value drops the format entirely. `Number` renders `nan_format`
        *inside* the format string, so a signed money format produced the tile
        `"+— USD"` on a disconnected gateway (seen live 2026-08-04) — a currency code
        and a plus sign attached to a number that does not exist. An absent value now
        renders as a bare em dash, which is the whole of what is known.
        """
        tile.param.update(value=value, format="{value}" if value is None else fmt)

    def _refresh_orders(self, snapshot: DashboardSnapshot) -> None:
        """Orders tab: the working book, and a line saying which of three states it is in.

        Added 2026-08-05. The book had been visible only in the chat's opening message —
        printed once at startup and never updated — so a full place → modify → cancel run
        during a session left the dashboard unchanged throughout. (Positions were correct
        the whole time: a resting limit order is not a position, and it only reaches that
        table on a fill.)
        """
        self._orders.value = orders_frame(snapshot)
        self._orders_status.object = orders_status_line(snapshot)

    def _refresh_fills(self, snapshot: DashboardSnapshot, stale: bool) -> None:
        """Fills tab: the executions not yet on a statement, and a line saying which of
        four states the list is in (gap #68, 2026-09-25).

        `pending` survives `without_account`, so a stale account half keeps the rows and
        the line says they are a floor — the Daily tab's rule, because a resting order can
        fill while the gateway is unreachable and a blank would hide that possibility.
        """
        self._fills.value = fills_frame(snapshot)
        self._fills_status.object = fills_status_line(snapshot, stale)

    def _refresh_positions(self, snapshot: DashboardSnapshot) -> None:
        """Positions tab: the table, a count/currency summary, and the reconciliation line."""
        self._apply_money_symbol(snapshot)
        self._positions.value = positions_frame(snapshot)
        self._reconciliation.object = reconciliation_line(reconcile(snapshot))
        self._basis_note.object = basis_note(snapshot)
        self._quote_note.object = quote_note(snapshot)
        count = len(snapshot.positions)
        if count == 0 and snapshot.ledger is None:
            self._positions_status.object = "_Positions unavailable — IBKR not connected._"
            return
        currencies = sorted({p.currency for p in snapshot.positions if p.currency})
        ccy_note = f" · {', '.join(currencies)}" if len(currencies) > 1 else ""
        total = sum(p.unrealised_pnl for p in snapshot.positions)
        summary = (
            f"_{count} open position(s){ccy_note} · unrealised "
            f"{fmt_signed(total, currencies[0] if len(currencies) == 1 else 'mixed')}_"
            if count
            else "_No open positions._"
        )
        self._positions_status.object = summary

    def _apply_money_symbol(self, snapshot: DashboardSnapshot) -> None:
        """Re-symbol the money columns when the book's currency changes. Usually a no-op.

        Guarded on the currency rather than run every poll. `formatters` is one of
        `BaseTable._manual_params`, so assigning it pushes a column update to the model
        on every assignment — read from panel 1.9's `Tabulator._update_columns`, which
        also shows that the *configuration* rebuild is skipped when no formatter value is
        a `str` or `dict`; these are `NumberFormatter` instances, so it takes that early
        return. So the cost is a column update, not a full table rebuild — cheaper than
        first assumed, and still pointless to repeat 5,760 times a day for an answer that
        changes only when a position in a new currency opens or the last one in an old
        currency closes.

        The `""` case matters as much as the symbol: a mixed book, an empty book, or a
        currency this app has no symbol for all fall back to bare numbers, so a figure
        is never rendered under a currency it does not belong to.
        """
        currency = book_currency(snapshot)
        if currency == self._money_currency:
            return
        self._money_currency = currency
        self._positions.formatters = {
            **dict(self._positions.formatters),
            **{c: NumberFormatter(format=f) for c, f in _money_formats(currency).items()},
        }

    def _refresh_pending(self, snapshot: DashboardSnapshot, now: datetime | None) -> None:
        """The Daily selection (the pending window): realised P&L not yet on a statement.

        Three of the pane's surfaces are deliberately blanked rather than filled:

        * **the curve** — pending executions carry no day (gap #69: Flex is the only
          source of a trade date), so there is nothing to place on a dated axis;
        * **its note**, which exists only to explain a curve that is not drawn;
        * **the settled block** — it reports what IBKR has confirmed in a statement, and
          no statement covers these executions. Rendering it here would put settled
          figures under a heading saying "Not yet on a statement".

        The currency is *not* blanked, and is the reason this reads `self._snapshot` —
        the snapshot **before** `without_account` stripped the ledger — so the money keeps
        its ISO code once the account half goes stale. The
        figures themselves are not account-derived and are not stripped either way; the
        heading is what says they may be a floor.
        """
        original = self._snapshot or snapshot
        ccy = original.ledger.currency if original.ledger else ""
        self._pnl_source_note.object = pending_heading(original, self.is_stale(original, now))
        self._pnl_breakdown.object = pending_table(original, ccy)
        self._pnl_chart.object = None
        self._pnl_chart_note.object = ""
        self._pnl_stats.object = ""
        self._pnl_coverage.object = coverage_line(snapshot)
        self._ledger_detail.object = ledger_markdown(snapshot)

    def _refresh_pnl(self, snapshot: DashboardSnapshot, now: datetime | None = None) -> None:
        """P&L tab: the realised chart for the selected window, stats, and disclosures.

        An empty window reports no currency of its own, so the account's base currency
        (from the ledger) stands in — and when there is no ledger either, nothing is
        stated. Same rule as the KPI strip: substitute a *known* currency or none.
        """
        label = str(self._window.value)
        if label == _PENDING_LABEL:
            self._refresh_pending(snapshot, now)
            return
        self._pnl_source_note.object = ""
        window, points, stats = self._selected_window(snapshot)
        account_ccy = snapshot.ledger.currency if snapshot.ledger else ""
        if window is None:
            self._pnl_chart.object = None
            self._pnl_chart_note.object = ""
            self._pnl_stats.object = "_No trade data in the local store._"
        else:
            ccy = window.currency_label or account_ccy
            title = (
                f"Realised P&L — {label} "
                f"({window.start.isoformat()} → {window.end.isoformat()}"
                + (f", {ccy})" if ccy else ")")
            )
            self._pnl_chart.object = build_realised_chart(points, title)
            self._pnl_chart_note.object = realised_chart_note(
                points, ccy, unit="week" if _WINDOW_KEYS.get(label) == "ytd" else "day"
            )
            cov = snapshot.coverage
            through = (
                f" through {cov.through.isoformat()}"
                if cov is not None and cov.through is not None
                else ""
            )
            self._pnl_stats.object = stats_markdown(
                window, stats, f"{label} — settled by IBKR statement{through}", currency=ccy
            )
        key = _WINDOW_KEYS.get(label)
        self._pnl_breakdown.object = breakdown_table(
            snapshot.breakdowns.get(key) if key else None, account_ccy
        )
        self._pnl_coverage.object = coverage_line(snapshot)
        self._ledger_detail.object = ledger_markdown(snapshot)

    def _selected_window(
        self, snapshot: DashboardSnapshot
    ) -> tuple[RealisedWindow | None, tuple[RealisedPoint, ...], RoundTripStats | None]:
        """The selected window, its slice of the curve, and **its own** round-trip stats.

        The poller computes one YTD series and this slices it, rather than issuing three
        queries — the week and month curves are suffixes of the same data. Stats are not
        sliced: they are counts computed per window by the data layer, and picking them
        by the same key is what stops week figures appearing under a YTD heading.
        """
        key = _WINDOW_KEYS.get(str(self._window.value))
        window = getattr(snapshot, key) if key else None
        if window is None:
            return None, (), None
        points = tuple(p for p in snapshot.series if window.start <= p.day <= window.end)
        if key == "ytd":
            # One resolution per view. A year of daily bars is 131 hairlines in a few
            # hundred pixels (seen 2026-09-23); aggregating only the bars would leave the
            # two rows at different resolutions on one shared x-axis, which reads worse
            # than either. `weekly_series` regroups without changing the money — its total
            # equals the daily total, which is the invariant that makes this safe.
            points = weekly_series(points)
        return window, points, snapshot.stats.get(key or "")

    def _on_window_change(self, _event: Any) -> None:
        """Re-render the P&L tab when the window selector changes, without a new poll."""
        if self._snapshot is not None:
            self._refresh_pnl(self._snapshot)


def build_dashboard(chart_pane: Any = None) -> DashboardView:
    """Build the dashboard. `chart_pane` is `panel_chart.build_chart_pane()`'s Column."""
    return DashboardView(chart_pane=chart_pane)
