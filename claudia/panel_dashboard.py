"""Panel components for the live trading dashboard — **widgets only, no IBKR, no SQL.**

The other half of the seam described in `claudia/dashboard_data.py`: this module reads a
`DashboardSnapshot` and renders it. It performs no I/O of any kind, which is what makes
it testable against a stub snapshot before it is wired to anything, and what keeps the
5-second repaint free of blocking work on the shared event loop.

Layout, as decided in the plan:

```
KPI strip  (always visible, across the top)
Chat  |  Tabs( Chart · Positions · Orders · P&L )
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
Flex-derived week/month/YTD figures cannot (Flex is T+1, and live Client Portal rows
carry no trade date at all). They will therefore disagree, legitimately, and the
freshness line says so. Similarly, round-trip win/loss counts come from `flex_lot` while
the P&L total comes from `flex_trade` — the panel labels which is which, because
lot-derived P&L would silently overstate losses by the wash-sale-disallowed amount.

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

# Side-effect import: registers the bokeh renderer and installs the DataFrame `.hvplot`
# accessor. Same load-bearing import as claudia/panel_chart.py — see that module's note.
import hvplot.pandas  # noqa: F401
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
    realised_ledger_label,
    reconcile,
)
from claudia.dashboard_poller import STALE_AFTER
from claudia.panel_markdown import safe_markdown

log = logging.getLogger(__name__)

# Shared with claudia/panel_chart.py's candles so a green number and a green candle mean
# the same thing across the app.
_UP_COLOR = "#26a69a"
_DOWN_COLOR = "#ef5350"
_FLAT_COLOR = "#8a8a8a"

# `Number.colors` is scanned in reverse and the last match wins, so the earliest
# threshold a value satisfies is the colour applied (verified 2026-08-04 by reading
# `panel.widgets.indicators.Number._process_param_change`). Half a cent of dead band
# around zero keeps a flat P&L neutral instead of red: `value <= threshold` would
# otherwise paint an exactly-zero figure as a loss.
_PNL_COLORS = [(-0.005, _DOWN_COLOR), (0.005, _FLAT_COLOR), (float("inf"), _UP_COLOR)]

_TILE_WIDTH = 165
_CHART_HEIGHT = 260
_BAR_ROW_HEIGHT = 130

# Which asset categories roll up into the "equities & options" side of the futures split.
# FUT is everything else. Named here rather than inline so the two call sites cannot drift.
_NON_FUTURES = ("STK", "OPT", "FUND", "CASH")

# Radio-button label -> the snapshot attribute and `stats` key it selects. One mapping,
# so a window's realised total and its round-trip counts can never be picked from
# different windows.
_WINDOW_KEYS = {"Week": "week", "Month": "month", "YTD": "ytd"}
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
    pending = (
        f" · **{cov.live_pending} fill(s) today not yet in a statement**"
        if cov.live_pending
        else ""
    )
    return (
        f"_Realised week/month/YTD come from the Flex dataset through "
        f"**{cov.through.isoformat()}** (T+1 — never includes today){pending}, and are "
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
    """
    return pd.DataFrame(
        {
            "day": pd.to_datetime([p.day for p in points]),
            "realised": [p.realised for p in points],
            "cumulative": [p.cumulative for p in points],
        }
    )


def realised_chart_note(points: tuple[RealisedPoint, ...], currency: str) -> str:
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
        return (
            f"_Only one trading day in this window — **{only.day.isoformat()}: "
            f"{fmt_signed(only.realised, currency)}**. A curve needs at least two "
            f"points, so none is drawn._"
        )
    return ""


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
    cumulative = df.hvplot.area(
        x="day", y="cumulative", alpha=0.20, color=_UP_COLOR, hover=False
    ) * df.hvplot.line(x="day", y="cumulative", color=_UP_COLOR, line_width=2)
    daily = df.hvplot.bar(
        x="day", y="realised", height=_BAR_ROW_HEIGHT, color=_FLAT_COLOR
    )
    return (
        cumulative.opts(title=title, height=_CHART_HEIGHT) + daily.opts(title="")
    ).cols(1)


# ── Positions table ───────────────────────────────────────────────────────────

_POSITION_COLUMNS = [
    "Symbol", "Class", "Qty", "Avg entry", "IBKR basis", "Basis Δ",
    "Last", "Market value", "Unrealised", "Ccy",
]
# The columns `.style.map` colours by sign. Named once so the styler and the empty-frame
# builder cannot disagree about which columns exist.
#
# "Basis Δ" is deliberately **not** here. Its sign says which way IBKR's basis leans, not
# whether anything is good or bad, and the green/red map on this surface means profit and
# loss. Colouring it would assert a judgement the number does not carry.
_SIGNED_COLUMNS = ["Unrealised"]

# Display precision. IBKR returns full float precision — the live account rendered
# `383.270899` and `374.09762575` as an average cost and a last price (observed
# 2026-08-04), which is noise on a trading surface and makes a column impossible to
# scan. Formatting here rather than in `positions_frame` deliberately: the DataFrame
# keeps real floats, so the column still sorts numerically and the `.style` sign
# colouring still sees a number.
#
# Money to 2dp; prices to 4dp with trailing zeros optional (`[00]`), which shows
# 374.0976 for an equity and a bare 6480 for an ES contract rather than 6480.0000.
_MONEY_FORMAT = "0,0.00"
_PRICE_FORMAT = "0,0.00[00]"
_QTY_FORMAT = "0,0.[00000000]"  # fractional-share and futures quantities alike

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
    "Qty": "Signed position size. Negative is short.",
    "Avg entry": "Where the position was actually entered: average price of the open "
                 "lots, FIFO over this account's own fills, excluding commission. Blank "
                 "when it could not be reconstructed exactly — never estimated.",
    "IBKR basis": "IBKR avgPrice — the cost basis their P&L uses. Includes commission "
                  "and any cost-basis adjustment, so it is a fiscal figure, not a level "
                  "that was ever traded at.",
    "Basis Δ": "(IBKR basis - avg entry) * qty * multiplier: exactly how much of the "
               "Unrealised column comes from the basis rather than from the market.",
    "Last": "IBKR mktPrice at the last poll, not a live tick.",
    "Market value": "IBKR mktValue. For futures the ledger reports this as open P&L "
                    "rather than notional.",
    "Unrealised": "Open P&L on the position. Not the day's change, and not realised.",
    "Ccy": "The position's own currency — it need not be the account's base currency.",
}


# A never-polled snapshot, used only to give the Tabulator its column headers at build
# time, so `positions_frame`'s empty-frame path runs on construction rather than first
# appearing on a live session with no positions. `as_of` is a fixed sentinel rather than
# `datetime.now()`: nothing reads it, and a module-level clock read is an import-time side
# effect that would make import order observable.
_EMPTY = DashboardSnapshot(as_of=datetime.min.replace(tzinfo=UTC))


_ORDER_COLUMNS = ["Order", "Symbol", "Side", "Qty", "Filled", "Limit", "Type", "TIF", "Status", "Origin"]

_ORDER_NUMERIC = ["Qty", "Filled", "Limit"]
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
    "Qty": "Total order size, not the remainder.",
    "Filled": "How much has executed. Shows 0 when IBKR reports no remaining quantity, "
              "which is the safe direction to be wrong in.",
    "Limit": "The resting price where the order type has one. No currency: the live-order "
             "feed does not carry one, so none is claimed.",
    "Status": "IBKR's own status string, verbatim.",
    "Origin": "ClaudIA for orders staged through this app; external for TWS, mobile or the "
              "web portal — those cannot be modified or cancelled through the API.",
}


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
            "Symbol": o.symbol,
            "Side": o.side,
            "Qty": o.quantity,
            "Filled": o.filled,
            "Limit": o.price,
            "Type": o.order_type,
            "TIF": o.tif,
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
            "Symbol": p.symbol,
            "Class": p.asset_class,
            "Qty": p.quantity,
            "Avg entry": p.economic_entry,
            "IBKR basis": p.average_price,
            "Basis Δ": p.basis_delta_value,
            "Last": p.market_price,
            "Market value": p.market_value,
            "Unrealised": p.unrealised_pnl,
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
            if (value := p.basis_delta_value) is not None
            and abs(value) >= _BASIS_NOTE_THRESHOLD
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


def _sign_style(value: Any) -> str:
    """Green/red/neutral CSS for one signed cell; nothing at all for a non-number."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    if value > 0:
        return f"color: {_UP_COLOR}"
    if value < 0:
        return f"color: {_DOWN_COLOR}"
    return f"color: {_FLAT_COLOR}"


# ── Round-trip stats ──────────────────────────────────────────────────────────


def stats_markdown(
    window: RealisedWindow | None,
    stats: RoundTripStats | None,
    label: str,
    currency: str | None = None,
) -> str:
    """The trading-stats block: trade count, winners vs losers in % **and** absolute money.

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
        f"| Realised P&L (executions) | **{fmt_signed(window.total, ccy)}** |",
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
        "_Totals are execution-basis (`flex_trade`, the authoritative net figure). "
        "Win/loss counts and the gross figures are lot-basis (`flex_lot`), which is "
        "pre-wash-sale and must never be read as realised P&L._",
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
        "equals the sum of the per-position `realizedPnl`, which IBKR defines as \"the "
        "total profit made today through trades\" (measured and cross-checked "
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
    * `tabs` — `Tabs(Chart · Positions · Orders · P&L)`.

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
            "win_rate": self._tile("Win rate this week", fmt="{value:.0f}%"),
        }
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
            layout="fit_data_stretch",
            sizing_mode="stretch_width",
            height=380,
            formatters={
                "Qty": NumberFormatter(format=_QTY_FORMAT),
                "Avg entry": NumberFormatter(format=_PRICE_FORMAT),
                "IBKR basis": NumberFormatter(format=_PRICE_FORMAT),
                "Basis Δ": NumberFormatter(format=_MONEY_FORMAT),
                "Last": NumberFormatter(format=_PRICE_FORMAT),
                "Market value": NumberFormatter(format=_MONEY_FORMAT),
                "Unrealised": NumberFormatter(format=_MONEY_FORMAT),
            },
            text_align=dict.fromkeys(
                ["Qty", "Avg entry", "IBKR basis", "Basis Δ", "Last", "Market value",
                 "Unrealised"],
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
        assert styler is not None  # narrowing for mypy, not a runtime guarantee
        styler.map(_sign_style, subset=_SIGNED_COLUMNS)
        self._positions_status = safe_markdown("_Positions: waiting for the first poll…_")
        self._reconciliation = safe_markdown("")
        self._basis_note = safe_markdown("")

        self._window = pn.widgets.RadioButtonGroup(
            # color=, not button_type=: `button_type` PendingDeprecationWarns on panel
            # 1.9 and the suite gates on warnings (same finding as Widget.name).
            label="Window", options=list(_WINDOW_LABELS), value="Week", color="light"
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
            },
            text_align=dict.fromkeys(_ORDER_NUMERIC, "right"),
            header_tooltips=dict(_ORDER_TOOLTIPS),
        )
        self._orders_status = safe_markdown("_Orders: waiting for the first poll…_")
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
            ("Positions", pn.Column(self._positions_status, self._reconciliation,
                                    self._basis_note, self._positions,
                                    sizing_mode="stretch_both")),
            ("Orders", pn.Column(self._orders_status, self._orders,
                                 sizing_mode="stretch_both")),
            ("P&L", pn.Column(self._window, self._pnl_chart, self._pnl_chart_note,
                              self._pnl_stats, self._pnl_coverage, self._ledger_detail,
                              sizing_mode="stretch_both")),
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
            self._refresh_pnl(display)
            self._notify_staleness(snapshot, now)
        except Exception:
            log.exception("Dashboard repaint failed; leaving the previous frame up")

    def is_stale(self, snapshot: DashboardSnapshot, now: datetime | None = None) -> bool:
        """Whether the account half of `snapshot` should be treated as untrustworthy.

        One definition, shared by the status line and the notification, so the toast and
        the text on screen can never disagree about whether the data is good.
        """
        return bool(snapshot.error) or snapshot.age_seconds(now) > STALE_AFTER

    def _notify_staleness(
        self, snapshot: DashboardSnapshot, now: datetime | None = None
    ) -> None:
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
            notifications.error(
                f"Account data is stale — {short_reason(snapshot.error) or 'no recent poll'}",
                duration=0,  # 0 = sticky: a stale trading surface should not self-dismiss
            )
        else:
            notifications.success("Account data is live again.", duration=4000)

    def _refresh_tiles(self, snapshot: DashboardSnapshot, now: datetime | None) -> None:
        """KPI strip: ledger balances, live unrealised, the two realised figures, win rate.

        `ccy` is the empty string when there is no ledger, never a guessed "USD". Every
        tile whose value comes from that missing ledger is set to None, and `_set` drops
        the format for a None value, so the code is not rendered in that case anyway —
        but a placeholder currency sitting in a local is one edit away from being shown.
        """
        led = snapshot.ledger
        ccy = led.currency if led else ""
        self._set(self._tiles["net_liq"], led.net_liquidation if led else None,
                  f"{{value:,.2f}} {ccy}")
        self._set(self._tiles["cash"], led.cash if led else None, f"{{value:,.2f}} {ccy}")
        self._set(self._tiles["unrealised"], led.unrealised_pnl if led else None,
                  f"{{value:+,.2f}} {ccy}")
        self._tiles["realised_ledger"].label = realised_ledger_label()
        self._set(self._tiles["realised_ledger"], led.realised_pnl if led else None,
                  f"{{value:+,.2f}} {ccy}")

        # An empty week has no currency of its own (`currency_label` returns ""), so the
        # account's own base currency stands in — known, not assumed.
        week = snapshot.week
        week_ccy = (week.currency_label or ccy) if week else ccy
        self._set(self._tiles["realised_week"], week.total if week else None,
                  f"{{value:+,.2f}} {week_ccy}".rstrip())
        stats = snapshot.stats.get("week")
        self._set(self._tiles["win_rate"],
                  stats.win_rate if stats and stats.win_rate is not None else None,
                  "{value:.0f}%")
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

    def _refresh_positions(self, snapshot: DashboardSnapshot) -> None:
        """Positions tab: the table, a count/currency summary, and the reconciliation line."""
        self._positions.value = positions_frame(snapshot)
        self._reconciliation.object = reconciliation_line(reconcile(snapshot))
        self._basis_note.object = basis_note(snapshot)
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

    def _refresh_pnl(self, snapshot: DashboardSnapshot) -> None:
        """P&L tab: the realised chart for the selected window, stats, and disclosures.

        An empty window reports no currency of its own, so the account's base currency
        (from the ledger) stands in — and when there is no ledger either, nothing is
        stated. Same rule as the KPI strip: substitute a *known* currency or none.
        """
        label = str(self._window.value)
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
            self._pnl_chart_note.object = realised_chart_note(points, ccy)
            self._pnl_stats.object = stats_markdown(
                window, stats, f"{label} — realised & round trips", currency=ccy
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
        return window, points, snapshot.stats.get(key or "")

    def _on_window_change(self, _event: Any) -> None:
        """Re-render the P&L tab when the window selector changes, without a new poll."""
        if self._snapshot is not None:
            self._refresh_pnl(self._snapshot)


def build_dashboard(chart_pane: Any = None) -> DashboardView:
    """Build the dashboard. `chart_pane` is `panel_chart.build_chart_pane()`'s Column."""
    return DashboardView(chart_pane=chart_pane)
