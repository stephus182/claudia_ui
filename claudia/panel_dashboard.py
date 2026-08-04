"""Panel components for the live trading dashboard — **widgets only, no IBKR, no SQL.**

The other half of the seam described in `claudia/dashboard_data.py`: this module reads a
`DashboardSnapshot` and renders it. It performs no I/O of any kind, which is what makes
it testable against a stub snapshot before it is wired to anything, and what keeps the
5-second repaint free of blocking work on the shared event loop.

Layout, as decided in the plan:

```
KPI strip  (always visible, across the top)
Chat  |  Tabs( Chart · Positions · P&L )
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
    DashboardSnapshot,
    RealisedPoint,
    RealisedWindow,
    RoundTripStats,
    realised_ledger_label,
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
    """A balance as `12,345.67 USD`. `—` for None, never a bare currency symbol."""
    if value is None:
        return "—"
    return f"{value:,.2f} {currency}"


def fmt_signed(value: float | None, currency: str) -> str:
    """A P&L figure as `-3,516.98 USD` / `+250.25 USD`, with an explicit sign.

    The sign is always shown: a profit and a loss must not be distinguishable only by a
    minus that is easy to miss in a dense row of numbers.
    """
    if value is None:
        return "—"
    return f"{value:+,.2f} {currency}"


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
    """The T+1 disclosure: what the Flex-derived windows do and do not include.

    The ledger figure includes today and the Flex windows cannot, so the two will
    disagree. An unlabelled pair that disagrees is exactly the failure this whole track
    exists to avoid, so the gap is stated in words on the surface itself rather than
    left for the reader to infer.
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
        f"**{cov.through.isoformat()}** (T+1 — never includes today){pending}. "
        f"The ledger figure above does include today._"
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
    "Symbol", "Class", "Qty", "Avg cost", "Last", "Market value", "Unrealised", "Ccy"
]
# The columns `.style.map` colours by sign. Named once so the styler and the empty-frame
# builder cannot disagree about which columns exist.
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


def positions_frame(snapshot: DashboardSnapshot) -> pd.DataFrame:
    """Open positions as the DataFrame the `Tabulator` renders.

    Always returns the full column set, even with no rows: a `Tabulator` handed a
    zero-column frame renders as a blank rectangle with no headers, which reads as a
    broken widget rather than as an empty book.
    """
    rows = [
        {
            "Symbol": p.symbol,
            "Class": p.asset_class,
            "Qty": p.quantity,
            "Avg cost": p.average_cost,
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
    window: RealisedWindow | None, stats: RoundTripStats | None, label: str
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
    ccy = window.currency_label
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
    "equities": IBKR documents neither field's scope, and nothing establishes that the
    two are complementary. The exact per-asset split is available — from the Flex
    windows, where it is measured rather than inferred — and that is where it is shown.

    That restraint was vindicated on the first live read (2026-08-04). The account
    returned:

        futuremarketvalue  -11,607.50      futuresonlypnl  -11,607.50
        unrealizedpnl      -11,618.32      realizedpnl      -2,656.11

    `futuresonlypnl` is **exactly** `futuremarketvalue` — not close to it, equal — and
    is four times the realised figure. Treating it as the futures half of a realised
    split would have been wrong by an order of magnitude and in the wrong direction.
    One measurement on one futures-heavy account is evidence, not proof, which is why
    the field is displayed under IBKR's own name with the observation attached and no
    arithmetic performed on it.
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
        "_IBKR documents no time window for `realizedpnl` and no description at all for "
        "`futuresonlypnl`. Measured live 2026-08-04, `futuresonlypnl` was **exactly** "
        "`futuremarketvalue` and four times the realised figure, so it is listed here "
        "among the market-value rows under IBKR's own field name rather than treated as "
        "a realised split. No residual is computed: `realizedpnl - futuresonlypnl` is "
        "not a documented equities total — the measured per-asset split is in the "
        "windows above._"
    )


# ── The view ──────────────────────────────────────────────────────────────────


class DashboardView:
    """The dashboard's widgets plus the one `refresh(snapshot)` that repaints them all.

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
                "Avg cost": NumberFormatter(format=_PRICE_FORMAT),
                "Last": NumberFormatter(format=_PRICE_FORMAT),
                "Market value": NumberFormatter(format=_MONEY_FORMAT),
                "Unrealised": NumberFormatter(format=_MONEY_FORMAT),
            },
            text_align=dict.fromkeys(
                ["Qty", "Avg cost", "Last", "Market value", "Unrealised"], "right"
            ),
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

        self._window = pn.widgets.RadioButtonGroup(
            # color=, not button_type=: `button_type` PendingDeprecationWarns on panel
            # 1.9 and the suite gates on warnings (same finding as Widget.name).
            label="Window", options=list(_WINDOW_LABELS), value="Week", color="light"
        )
        self._window.param.watch(self._on_window_change, "value")
        self._pnl_chart = pn.pane.HoloViews(None, sizing_mode="stretch_width")
        self._pnl_chart_note = safe_markdown("")
        self._pnl_stats = safe_markdown("_Realised P&L: waiting for the first poll…_")
        self._pnl_coverage = safe_markdown("")
        self._ledger_detail = safe_markdown("")

        self.tabs = pn.Tabs(
            ("Chart", chart_pane if chart_pane is not None else pn.Column()),
            ("Positions", pn.Column(self._positions_status, self._positions,
                                    sizing_mode="stretch_both")),
            ("P&L", pn.Column(self._window, self._pnl_chart, self._pnl_chart_note,
                              self._pnl_stats, self._pnl_coverage, self._ledger_detail,
                              sizing_mode="stretch_both")),
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
        """
        try:
            self._snapshot = snapshot
            self._refresh_tiles(snapshot, now)
            self._refresh_positions(snapshot)
            self._refresh_pnl(snapshot)
        except Exception:
            log.exception("Dashboard repaint failed; leaving the previous frame up")

    def _refresh_tiles(self, snapshot: DashboardSnapshot, now: datetime | None) -> None:
        """KPI strip: ledger balances, live unrealised, the two realised figures, win rate."""
        led = snapshot.ledger
        ccy = led.currency if led else "USD"
        self._set(self._tiles["net_liq"], led.net_liquidation if led else None,
                  f"{{value:,.2f}} {ccy}")
        self._set(self._tiles["cash"], led.cash if led else None, f"{{value:,.2f}} {ccy}")
        self._set(self._tiles["unrealised"], led.unrealised_pnl if led else None,
                  f"{{value:+,.2f}} {ccy}")
        self._tiles["realised_ledger"].label = realised_ledger_label()
        self._set(self._tiles["realised_ledger"], led.realised_pnl if led else None,
                  f"{{value:+,.2f}} {ccy}")

        week = snapshot.week
        self._set(self._tiles["realised_week"], week.total if week else None,
                  f"{{value:+,.2f}} {week.currency_label if week else ccy}")
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

    def _refresh_positions(self, snapshot: DashboardSnapshot) -> None:
        """Positions tab: the table plus a count/currency summary line."""
        self._positions.value = positions_frame(snapshot)
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
        """P&L tab: the realised chart for the selected window, stats, and disclosures."""
        label = str(self._window.value)
        window, points, stats = self._selected_window(snapshot)
        if window is None:
            self._pnl_chart.object = None
            self._pnl_chart_note.object = ""
            self._pnl_stats.object = "_No trade data in the local store._"
        else:
            title = (
                f"Realised P&L — {label} "
                f"({window.start.isoformat()} → {window.end.isoformat()}, "
                f"{window.currency_label})"
            )
            self._pnl_chart.object = build_realised_chart(points, title)
            self._pnl_chart_note.object = realised_chart_note(
                points, window.currency_label
            )
            self._pnl_stats.object = stats_markdown(
                window, stats, f"{label} — realised & round trips"
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


# A never-polled snapshot, used only to give the Tabulator its column headers at build
# time. Module-level so `positions_frame`'s empty-frame path is exercised on construction
# rather than first appearing on a live session with no positions.
_EMPTY = DashboardSnapshot(as_of=datetime.now(UTC))


def build_dashboard(chart_pane: Any = None) -> DashboardView:
    """Build the dashboard. `chart_pane` is `panel_chart.build_chart_pane()`'s Column."""
    return DashboardView(chart_pane=chart_pane)
