"""Tests for claudia/panel_dashboard.py — built against stub snapshots, no I/O.

Three groups, in descending order of how much they matter:

1. **Safety regression guards.** The positions table must stay `disabled=True` with no
   click/edit handler bound. Hard Rule 1 says a rendered surface can never become an
   order path, and `Tabulator` cells are editable by default, so this is asserted rather
   than assumed.
2. **Honesty guards.** Stale data must be visibly stale; the ledger figure and the
   Flex-derived windows must be labelled apart; a window's realised total and its
   round-trip counts must come from the same window; every money figure carries an ISO
   code and never a bare `$`.
3. Rendering mechanics.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import panel as pn
import pytest

import claudia.panel_dashboard as pdash
from claudia import palette

# Tabulator needs its extension loaded before the widget can render. Importing the module
# under test does not do it — panel_app owns the single pn.extension call — so the suite
# loads it here, matching what a served session provides.
pn.extension("tabulator")


from claudia import dashboard_data as dd  # noqa: E402
from claudia.dashboard_poller import POLL_INTERVAL, STALE_AFTER  # noqa: E402

_NOW = datetime(2026, 8, 6, 15, 30, tzinfo=UTC)
_TODAY = date(2026, 8, 6)


def _window(start, end, total, count, by_asset=None, currencies=("USD",)):
    """A `RealisedWindow` with sensible defaults for the fields a test does not pin."""
    return dd.RealisedWindow(
        start=start,
        end=end,
        total=total,
        trade_count=count,
        by_asset=by_asset or {"FUT": total},
        currencies=currencies,
    )


def _stats(lots=4, wins=2, losses=2, scratches=0, gross_win=1322.0, gross_loss=-4329.38):
    """A `RoundTripStats` with defaults matching the data-layer fixture."""
    return dd.RoundTripStats(
        start=date(2026, 8, 3),
        end=_TODAY,
        closed_lots=lots,
        winners=wins,
        losers=losses,
        scratches=scratches,
        gross_win=gross_win,
        gross_loss=gross_loss,
    )


def _ledger(**over):
    """A `LedgerSnapshot` in USD with the fields the KPI strip reads."""
    fields: dict[str, Any] = {
        "currency": "USD",
        "net_liquidation": 100000.0,
        "cash": 25000.0,
        "settled_cash": 24000.0,
        "stock_market_value": 60000.0,
        # Matches the default position book below, so the fixture reconciles. A
        # non-reconciling case is built explicitly by the test that wants one.
        "futures_market_value": 0.0,
        "unrealised_pnl": -1000.0,
        "realised_pnl": 412.10,
        "futures_only_pnl": -3516.98,
    }
    fields.update(over)
    return dd.LedgerSnapshot(**fields)


def _positions(*specs):
    """Positions from `(symbol, qty, unrealised)` triples, or one default position.

    `avgCost` is per contract and `avgPrice` per unit — the shape a real futures row has
    (CL SEP2026 measured 80,932.36 against 80.93236 on 2026-08-04), so a test that reads
    the wrong one fails here rather than on screen.
    """
    specs = specs or (("ESU6", 1.0, -1000.0),)
    return dd.parse_positions(
        [
            {
                "conid": i,
                "ticker": sym,
                "contractDesc": sym,
                "assetClass": "FUT",
                "position": qty,
                "avgCost": 324000.0,
                "avgPrice": 6480.0,
                "multiplier": 50.0,
                "mktPrice": 6480.0,
                "mktValue": 324000.0,
                "unrealizedPnl": upl,
                "realizedPnl": 0.0,
                "currency": "USD",
            }
            for i, (sym, qty, upl) in enumerate(specs)
        ]
    )


def _with_entry(positions, **entries):
    """Attach economic entries by symbol, for the columns that compare the two bases."""
    return tuple(
        replace(p, economic_entry=entries[p.symbol]) if p.symbol in entries else p
        for p in positions
    )


def _snapshot(**over):
    """A fully-populated snapshot; override any field per test."""
    fields = {
        "as_of": _NOW,
        "ledger": _ledger(),
        "positions": _positions(),
        "week": _window(date(2026, 8, 3), _TODAY, -2194.98, 5, {"FUT": -3516.98, "STK": 1322.0}),
        "month": _window(date(2026, 8, 1), _TODAY, -2294.98, 6),
        "ytd": _window(
            date(2026, 1, 1),
            _TODAY,
            -4006.18,
            8,
            {"FUT": -3616.98, "STK": 1122.0, "OPT": -1511.20},
            currencies=("EUR", "USD"),
        ),
        "stats": {
            "week": _stats(),
            "month": _stats(lots=5),
            "ytd": _stats(lots=42, wins=20, losses=22),
        },
        "series": tuple(
            dd.RealisedPoint(date(2026, 8, d), v, c)
            for d, v, c in [
                (3, -3516.98, -3516.98),
                (4, 1071.75, -2445.23),
                (5, 0.0, -2445.23),
                (6, 250.25, -2194.98),
            ]
        ),
        "coverage": dd.FlexCoverage(through=date(2026, 8, 5), live_pending=9),
        "error": None,
    }
    fields.update(over)
    return dd.DashboardSnapshot(**fields)


@pytest.fixture
def view():
    """A freshly-built dashboard, refreshed from the default snapshot."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    return v


# ── 1. Safety regression guards (Hard Rule 1) ─────────────────────────────────


def test_positions_table_is_not_editable(view):
    """Tabulator cells are editable by default; an editable P&L table is a hazard."""
    assert view._positions.disabled is True


def test_no_click_or_edit_handler_is_bound_to_the_positions_table(view):
    """A rendered surface must never become an order path.

    Asserted on the widget's own callback registries rather than on a comment: if a
    future change binds `on_click`/`on_edit` to reach an order function, this fails
    first. Removing the assertion is a deliberate act; forgetting the rule is not.
    """
    assert not view._positions._on_click_callbacks
    assert not view._positions._on_edit_callbacks


def test_module_reaches_no_order_path_and_no_io():
    """panel_dashboard must not reach order_flow, the toolkit, or the IBKR client.

    Checked over the parsed AST — imports and identifiers — rather than over the raw
    text, which would match this module's own prose about the rule it is enforcing.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(pdash.__file__ or "").read_text(encoding="utf-8"))
    imported: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    for forbidden in (
        "claudia.order_flow",
        "claudia.panel_order_flow",
        "ibkr_core_mcp",
        "sqlite3",
        "requests",
    ):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported), (
            f"panel_dashboard imports {forbidden}"
        )
    for forbidden in (
        "place_order",
        "modify_order",
        "cancel_order",
        "reply_order",
        "execute",
        "get_positions",
        "get_account_ledger",
    ):
        assert forbidden not in identifiers, f"panel_dashboard calls {forbidden}"


# ── 2. Honesty guards ─────────────────────────────────────────────────────────


def test_fresh_data_reads_as_live(view):
    """A recent poll renders as live, without the stale warning."""
    assert "live" in view._freshness.object
    assert "STALE" not in view._freshness.object


def test_stale_data_is_loudly_marked():
    """Past STALE_AFTER the line changes wording — silence here is the worst failure."""
    snap = _snapshot()
    line = pdash.freshness_line(snap, _NOW + timedelta(seconds=STALE_AFTER + 1))
    assert "STALE" in line
    assert "1m 01s" in line


def test_an_errored_poll_is_stale_regardless_of_age():
    """A failed poll marks the data stale immediately, not only once it ages out."""
    line = pdash.freshness_line(_snapshot(error="IBKR unavailable: boom"), _NOW)
    assert "STALE" in line and "boom" in line


def test_a_long_connection_error_does_not_bury_the_word_stale():
    """The real urllib3 string wrapped onto two lines and hid STALE (seen live)."""
    real = (
        "IBKR unavailable: HTTPSConnectionPool(host='localhost', port=59999): Max "
        "retries exceeded with url: /v1/api/portfolio/accounts (Caused by "
        "NewConnectionError(\"HTTPSConnection(host='localhost', port=59999): Failed to "
        'establish a new connection: [Errno 61] Connection refused"))'
    )
    line = pdash.freshness_line(_snapshot(error=real), _NOW)
    assert "STALE" in line
    assert "Caused by" not in line
    assert len(line) < 200
    assert "IBKR unavailable" in line  # the failure is still named, not swallowed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("short", "short"),
        ("a\n  b   c", "a b c"),
        ("head (Caused by tail)", "head"),
        ("x" * 300, "x" * (pdash._MAX_REASON_CHARS - 1) + "…"),
    ],
)
def test_short_reason(raw, expected):
    """A long connection error is trimmed to fit a status line, without hiding the failure."""
    assert pdash.short_reason(raw) == expected


def test_never_polled_says_so_rather_than_showing_a_flat_account():
    """Before the first poll the strip says it is waiting, not an account of zeros."""
    snap = dd.empty_snapshot(now=_NOW, error="Dashboard has not polled yet.")
    assert "waiting for the first poll" in pdash.freshness_line(snap, _NOW)


def test_coverage_line_states_the_t_plus_one_gap(view):
    """The ledger figure includes today; the Flex windows cannot. Say so on the surface."""
    line = view._pnl_coverage.object
    assert "2026-08-05" in line
    assert "today is never in it" in line
    assert "9 fill(s) today not yet in a statement" in line
    assert "The tile above is today only" in line


def test_coverage_line_names_the_session_day_boundary(view):
    """IBKR's "day" is a session, not a calendar day, and the two figures differ on it.

    Measured 2026-08-04 across this account's 1,101 executions: `trade_date` rolls
    forward at 18:00 ET for futures, 20:00 ET for stock, 17:00 ET for FX. A reader
    comparing the two figures at 19:00 needs to be told that.

    This asserted "the tile follows the calendar day" until 2026-08-05, when the ledger
    accumulator was measured rolling in the late ET evening instead
    (`dashboard_data.REALISED_LEDGER_WINDOW`, note 6). The line must NOT claim a
    calendar boundary: for the last hours of a day the tile reads "Realised today"
    while already showing tomorrow, and a user told otherwise would read that reset as
    a fault.

    It must NOT name a specific hour either. An intermediate version said "between
    21:55 and 22:31 ET"; a 37-read watch ending on that upper bound then found the
    field unmoved, killing the fixed-hour reading. A time the user can check against
    the clock is falsifiable, so a wrong one is worse than none.

    Both assertions are absences, which is the point — here the regression is a claim
    reappearing, not a claim going missing.
    """
    line = view._pnl_coverage.object
    assert "session" in line
    assert "18:00 ET" in line and "20:00 ET" in line and "17:00 ET" in line
    assert "at an hour that varies" in line
    assert "calendar day" not in line
    assert "21:55" not in line and "22:31" not in line


def test_coverage_line_discloses_the_cost_basis_difference_without_overstating_it(view):
    """The Flex windows are IBKR's statement basis; the tile is IBKR's real-time
    `avgCost`. Both must be named, because a reader reconciling the tile against the
    week window without being told will conclude one of them is broken.

    **What this test stopped asserting on 2026-08-05.** It used to call the basis gap
    "the largest of the three" on the strength of a projection: that the 2026-08-04 CRM
    close worth -2,810.47 on the ledger would be worth roughly -252.60 on Flex. When the
    statement arrived it read -2,810.47 — the same to the cent. The disclosure now says
    the bases are *defined* differently and have agreed wherever both priced the same
    close, which is all that was measured. Overstating a caveat misleads in the same way
    as omitting one.
    """
    line = " ".join(view._pnl_coverage.object.split())
    assert "statement" in line
    assert "real-time average cost" in line
    assert "do not add up and are not meant to" in line
    # the corrected claim, and the absence of the disproved one
    assert "agreed to the cent" in line
    assert "252.60" not in line


def test_coverage_line_without_pending_fills_omits_the_warning():
    """With no fills awaiting a statement, the pending-fills warning is left off."""
    snap = _snapshot(coverage=dd.FlexCoverage(through=date(2026, 8, 5), live_pending=0))
    assert "not yet in a statement" not in pdash.coverage_line(snap)


def test_coverage_line_with_no_flex_data():
    """With no Flex data the line says so rather than describing an empty window."""
    assert "no Flex data" in pdash.coverage_line(_snapshot(coverage=None))


def test_stats_block_labels_the_two_bases_apart(view):
    """Execution-basis P&L and lot-basis counts must never be readable as one figure."""
    view._window.value = "Weekly"
    text = view._pnl_stats.object
    assert "Settled realised P&L" in text
    assert "Closed round trips (lots)" in text
    assert "pre-wash-sale" in text and "must never be read as realised P&L" in text


def test_the_settled_block_never_claims_to_be_the_windows_realised_pnl(view):
    """Flex is T+1, so this block is structurally incapable of being the week's P&L.

    On 2026-08-06 it read -6,175.88 for a week whose realised was -16,480.46, and until
    that date it was titled "realised & round trips" directly beneath the bridged total —
    two figures for one window, differing by ten thousand, with nothing to explain why.
    The heading and the footnote now both say it is the settled statement view.
    """
    view._window.value = "Weekly"
    text = view._pnl_stats.object
    assert "settled by IBKR statement" in text
    assert "Settled only" in text
    assert "Not the window's realised P&L" in text


def test_each_window_shows_its_own_round_trip_counts(view):
    """A count cannot be sliced from a wider window — the Phase 3 smoke defect.

    Selecting YTD once rendered the *week's* "1 closed round trip" under a YTD heading
    beside a 636-execution total. Every window now carries its own stats.
    """
    view._window.value = "Weekly"
    assert "| Closed round trips (lots) | 4 |" in view._pnl_stats.object
    view._window.value = "YTD"
    assert "| Closed round trips (lots) | 42 |" in view._pnl_stats.object
    assert "20 / 22" in view._pnl_stats.object


def test_ledger_block_does_not_invent_an_equities_residual(view):
    """`realizedpnl - futuresonlypnl` is not a documented equities total; don't show one.

    Measured live 2026-08-04, `futuresonlypnl` was *exactly* `futuremarketvalue` and
    four times the realised figure, so a computed residual would have been wrong by an
    order of magnitude. It is listed among the market-value rows for that reason.
    """
    text = view._ledger_detail.object
    assert "futuresonlypnl" in text
    assert "-3,516.98 USD" in text
    assert "not a documented equities total" in " ".join(text.split())
    assert text.index("futuresonlypnl") < text.index(dd.realised_ledger_label())


def test_realised_ledger_tile_follows_the_measured_window(view):
    """The tile label is derived from the constant, not written twice.

    Settled 2026-08-04 to "day"; the tile must track that from one edit rather than
    carrying its own copy of the claim.
    """
    assert view._tiles["realised_ledger"].label == dd.realised_ledger_label()
    assert view._tiles["realised_ledger"].label == "Realised today"


def test_every_money_string_carries_an_iso_code_and_no_bare_dollar(view):
    """`$` is shared by USD/MXN/CAD/AUD/HKD/SGD — a wrong-currency price looks ordinary."""
    view._window.value = "Weekly"
    rendered = "\n".join(
        [
            view._freshness.object,
            view._positions_status.object,
            view._pnl_stats.object,
            view._pnl_coverage.object,
            view._ledger_detail.object,
        ]
    )
    assert "$" not in rendered
    assert "USD" in rendered
    for tile in view._tiles.values():
        assert "$" not in tile.format


def test_a_mixed_currency_window_is_labelled_mixed_not_usd():
    """The YTD fixture spans EUR and USD; its total must not be stamped USD.

    Given a series spanning several ISO weeks: the YTD view regroups to weekly points, and
    the shared fixture's four August days all fall inside ONE week — which correctly draws
    no chart, leaving the title this test is about with nothing to assert against.
    """
    spread = tuple(
        dd.RealisedPoint(date(2026, 7, d), 100.0, 100.0 * i)
        for i, d in enumerate((6, 13, 20), start=1)
    )
    view = pdash.build_dashboard()
    view.refresh(_snapshot(series=spread), now=_NOW)
    view._window.value = "YTD"
    assert "mixed" in view._pnl_stats.object
    # The chart's title is the assertion that matters; an earlier `... or True` line here
    # asserted nothing at all, which is worse than no line.
    assert "mixed" in view._pnl_chart.object.Overlay.I.opts.get("plot").kwargs["title"]


def test_an_empty_window_borrows_the_accounts_currency_rather_than_guessing():
    """A window that realised nothing has no currency; the ledger's is known, so use it."""
    empty = _window(date(2026, 8, 3), _TODAY, 0.0, 0, by_asset={}, currencies=())
    assert empty.currency_label == ""

    v = pdash.build_dashboard()
    v.refresh(_snapshot(week=empty, month=empty, ytd=empty, series=()), now=_NOW)
    v._window.value = "Weekly"
    assert v._tiles["realised_week"].format == "{value:+,.2f} USD"
    assert "+0.00 USD" in v._pnl_stats.object


def test_with_neither_a_window_currency_nor_a_ledger_no_code_is_stated():
    """Nothing known: render the bare number. Never a placeholder ISO code."""
    empty = _window(date(2026, 8, 3), _TODAY, 0.0, 0, by_asset={}, currencies=())
    v = pdash.build_dashboard()
    v.refresh(_snapshot(week=empty, month=empty, ytd=empty, series=(), ledger=None), now=_NOW)
    v._window.value = "Weekly"
    assert v._tiles["realised_week"].format == "{value:+,.2f}"
    assert "+0.00 |" in v._pnl_stats.object
    assert "USD" not in v._pnl_stats.object


def test_formatters_omit_a_trailing_space_for_an_unknown_currency():
    """An unknown currency renders a bare number with no dangling space."""
    assert pdash.fmt_money(1234.5, "") == "1,234.50"
    assert pdash.fmt_signed(-1234.5, "") == "-1,234.50"


def test_signed_figures_always_show_their_sign():
    """A P&L figure always carries its sign, so profit and loss differ by more than a minus."""
    assert pdash.fmt_signed(250.25, "USD") == "+250.25 USD"
    assert pdash.fmt_signed(-3516.98, "USD") == "-3,516.98 USD"
    assert pdash.fmt_signed(0.0, "USD") == "+0.00 USD"
    assert pdash.fmt_signed(None, "USD") == "—"
    assert pdash.fmt_money(100000.0, "EUR") == "100,000.00 EUR"
    assert pdash.fmt_money(None, "EUR") == "—"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (3.4, "3s"),
        (59.9, "60s"),
        (60, "1m 00s"),
        (134, "2m 14s"),
        (3600, "1h 00m"),
        (4500, "1h 15m"),
        (-5, "0s"),
    ],
)
def test_age_formatting(seconds, expected):
    """Poll ages render compactly across seconds, minutes and hours."""
    assert pdash.fmt_age(seconds) == expected


# ── 3. Rendering mechanics ────────────────────────────────────────────────────


def test_tabs_are_named_chart_positions_orders_pnl():
    """The tab set is the one the layout specifies. All P&L lives under ONE tab, chosen
    by the window selector — Daily included (user, 2026-08-07)."""
    v = pdash.build_dashboard(chart_pane=pn.Column(pn.pane.Markdown("chart")))
    assert list(v.tabs._names) == ["Chart", "Positions", "Orders", "P&L"]
    assert isinstance(v.tabs, pn.Tabs)


def test_kpi_strip_holds_five_number_tiles_and_nothing_else(view):
    """Five money tiles, and no sixth object wedged in beside them.

    A win-rate grid sat at the right end of this row until 2026-08-07 and was removed as
    clutter. Neither figure it carried is lost: the week is in the P&L tab's breakdown,
    the day is the P&L pane's Daily window.
    """
    row = list(view.kpi_strip[0])
    tiles = [o for o in row if isinstance(o, pn.indicators.Number)]
    assert [t.label for t in tiles] == [
        "Net liquidation",
        "Cash",
        "Unrealised P&L",
        dd.realised_ledger_label(),
        "Realised this week",
    ]
    assert len(row) == len(tiles)


def test_tiles_carry_the_live_currency_in_their_format(view):
    """Each tile's format carries the account's live ISO code, never a bare symbol."""
    assert view._tiles["net_liq"].value == 100000.0
    assert view._tiles["net_liq"].format == "{value:,.2f} USD"
    assert view._tiles["unrealised"].format == "{value:+,.2f} USD"
    assert view._tiles["realised_week"].value == pytest.approx(-2194.98)


def test_tiles_are_empty_and_neutral_before_the_first_poll():
    """Unpolled tiles render an em dash in a neutral colour, not a zero in red."""
    v = pdash.build_dashboard()
    assert all(t.value is None for t in v._tiles.values())
    assert all(t.nan_format == "—" for t in v._tiles.values())


def test_a_missing_ledger_blanks_the_account_tiles_rather_than_showing_zero():
    """Absent data must read as absent — a 0.00 net liquidation is a different claim."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(ledger=None, positions=()), now=_NOW)
    assert v._tiles["net_liq"].value is None
    assert v._tiles["unrealised"].value is None
    assert v._tiles["realised_week"].value == pytest.approx(-2194.98)  # local, still known


def test_a_blank_tile_shows_no_currency_code_and_no_sign():
    """`Number` renders nan_format inside the format string — "+— USD" was the result.

    A plus sign and an ISO code attached to a value that does not exist is a claim
    about nothing. An absent figure renders as a bare em dash.
    """
    v = pdash.build_dashboard()
    v.refresh(_snapshot(ledger=None, positions=()), now=_NOW)
    for key in ("net_liq", "cash", "unrealised", "realised_ledger"):
        assert v._tiles[key].format == "{value}", key
    # A tile that does have a value keeps its currency code.
    assert v._tiles["realised_week"].format == "{value:+,.2f} USD"


def test_pnl_colors_are_neutral_at_exactly_zero():
    """`value <= threshold` would paint a flat P&L red without the dead band."""
    tile = pdash.build_dashboard()._tiles["unrealised"]
    assert tile.colors is not None
    thresholds = dict(tile.colors)
    assert thresholds[-0.005] == palette.DOWN_COLOR
    assert thresholds[0.005] == palette.FLAT_COLOR
    assert thresholds[float("inf")] == palette.UP_COLOR


def test_numeric_columns_are_formatted_and_right_aligned(view):
    """IBKR returns full float precision — `383.270899` is noise on a trading surface.

    Formatting is a Tabulator display concern, never a change to the frame: the values
    stay real floats so the column sorts numerically and `.style` still sees a number.
    """
    fmts = view._positions.formatters
    # The fixture book is entirely USD, so every money column carries the symbol
    # (2026-08-07). `_money_formats` is the single source of that decision, and the
    # bare format is still the tail of each — the symbol is a prefix, not a rewrite.
    usd = pdash._money_formats("USD")
    for col, bare in (
        ("Market value", pdash._MONEY_FORMAT),
        ("Unrealised", pdash._MONEY_FORMAT),
        ("Avg entry", pdash._PRICE_FORMAT),
        ("IBKR basis", pdash._PRICE_FORMAT),
        ("Basis Δ", pdash._MONEY_FORMAT),
    ):
        assert fmts[col].format == usd[col], col
        assert fmts[col].format.endswith(bare), col
    assert view._positions.text_align["Unrealised"] == "right"
    # The underlying frame is untouched — display precision must not become data.
    assert view._positions.value["Unrealised"].dtype.kind == "f"


# ── The economic entry: the real level, beside IBKR's fiscal one ──────────────


def test_positions_table_leads_with_the_entry_not_the_basis():
    """A trader reading left to right must meet the tradeable number first."""
    columns = pdash._POSITION_COLUMNS
    assert columns.index("Avg entry") < columns.index("IBKR basis")
    assert "Avg cost" not in columns


def test_basis_column_is_per_unit_not_per_contract():
    """The table used to render `avgCost`, which is per contract for anything with a
    multiplier — CL showed 80,932.36 beside a last price of 75.14 (measured 2026-08-04).

    The fixture's futures row carries avgCost 324,000 against avgPrice 6,480, so a
    regression to the per-contract field is a 50x error here rather than a subtle one.
    """
    frame = pdash.positions_frame(_snapshot())
    assert frame["IBKR basis"].iloc[0] == pytest.approx(6480.0)
    assert frame["Last"].iloc[0] == pytest.approx(6480.0)


def test_an_unreconstructed_entry_renders_blank_not_zero():
    """`economic_entries` declines rather than approximating; the blank is that decision.

    Zero would read as a tradeable level, and 0.0 in a price column beside a real last
    price is worse than an empty cell.
    """
    frame = pdash.positions_frame(_snapshot())
    assert frame["Avg entry"].iloc[0] is None
    assert frame["Basis \u0394"].iloc[0] is None


def test_reconstructed_entry_and_delta_reach_the_frame():
    """6,480 basis against a 6,400 entry, 1 contract, multiplier 50 -> 4,000."""
    snap = _snapshot(positions=_with_entry(_positions(), ESU6=6400.0))
    frame = pdash.positions_frame(snap)
    assert frame["Avg entry"].iloc[0] == pytest.approx(6400.0)
    assert frame["Basis \u0394"].iloc[0] == pytest.approx(4000.0)


def test_basis_note_is_silent_when_nothing_drifts():
    """Below the threshold the difference is commission, and a note per position would
    be scrolled past within a day."""
    snap = _snapshot(positions=_with_entry(_positions(), ESU6=6479.9))
    assert pdash.basis_note(snap) == ""


def test_basis_note_is_silent_when_nothing_could_be_reconstructed():
    """No claim either way — announcing "no drift" for a book we never checked would be
    claiming a verification that never ran."""
    assert pdash.basis_note(_snapshot()) == ""


def test_basis_note_names_the_position_and_the_money():
    """The number that matters is what the basis does to Unrealised, not per share."""
    snap = _snapshot(positions=_with_entry(_positions(), ESU6=6400.0))
    note = pdash.basis_note(snap)
    assert "ESU6" in note
    assert "+4,000.00 USD" in note
    assert "6,480.0000" in note and "6,400.0000" in note


def test_basis_note_orders_by_size_of_the_distortion():
    """A trader scanning a book needs the worst one first, not the alphabetical one."""
    positions = _with_entry(
        _positions(("AAA", 1.0, -10.0), ("BBB", 1.0, -10.0)), AAA=6478.0, BBB=6400.0
    )
    note = pdash.basis_note(_snapshot(positions=positions))
    assert note.index("BBB") < note.index("AAA")


def test_basis_note_reaches_the_positions_tab(view):
    """Wired into the repaint, not merely defined."""
    view.refresh(_snapshot(positions=_with_entry(_positions(), ESU6=6400.0)), now=_NOW)
    assert "ESU6" in view._basis_note.object
    assert view._basis_note in list(view.tabs[1])


# ── Offline is blank, not last-known ─────────────────────────────────────────


def test_stale_account_data_is_blanked_not_left_on_screen(view):
    """User call 2026-08-04: "blank showing it's offline, no ambiguity".

    Last known figures under a STALE banner ask the reader to notice a line of text
    before trusting a number, and a number that is minutes old looks exactly like one
    that is current.
    """
    later = _NOW + timedelta(seconds=STALE_AFTER + 1)
    view.refresh(_snapshot(), now=later)
    assert view._tiles["net_liq"].value is None
    assert view._tiles["cash"].value is None
    assert view._tiles["unrealised"].value is None
    assert view._tiles["realised_ledger"].value is None
    assert len(view._positions.value) == 0
    assert "unavailable" in view._ledger_detail.object


def test_blanking_keeps_the_flex_windows_which_never_needed_the_gateway(view):
    """The asymmetry is the point: local SQLite did not go offline with IBKR.

    Blanking the realised windows too would invent an outage in the half of the
    dashboard that is still perfectly good.
    """
    later = _NOW + timedelta(seconds=STALE_AFTER + 1)
    view.refresh(_snapshot(), now=later)
    assert view._tiles["realised_week"].value == pytest.approx(-2194.98)
    assert "Settled realised P&L" in view._pnl_stats.object
    assert "today is never in it" in view._pnl_coverage.object


def test_blanking_still_says_how_long_it_has_been(view):
    """Blank without a duration is just a broken-looking screen."""
    later = _NOW + timedelta(seconds=STALE_AFTER + 1)
    view.refresh(_snapshot(), now=later)
    assert "STALE" in view._freshness.object
    assert "1m 01s" in view._freshness.object


def test_a_single_missed_poll_does_not_blank_a_working_dashboard(view):
    """Four missed polls, not one — a blip must not flash the screen empty and back."""
    view.refresh(_snapshot(), now=_NOW + timedelta(seconds=POLL_INTERVAL + 1))
    assert view._tiles["net_liq"].value == pytest.approx(100000.0)
    assert len(view._positions.value) == 1


def test_without_account_leaves_the_snapshot_itself_untouched():
    """The poller's record keeps ageing; only the *view* declines to draw it.

    `as_of` must survive so the status line can say how long it has been — clearing it
    would make a stale dashboard look freshly polled, the one failure this whole surface
    exists to prevent.
    """
    snap = _snapshot()
    blanked = snap.without_account()
    assert blanked.as_of == snap.as_of
    assert blanked.ledger is None and blanked.positions == ()
    assert blanked.week is snap.week and blanked.coverage is snap.coverage
    assert snap.ledger is not None and snap.positions  # original unchanged


# ── Enrichments (2026-08-04): reconciliation, filters, paging, notifications ──


def test_reconciliation_line_is_rendered_on_the_positions_tab(view):
    """Structured figures, so the check cannot fail to parse the way the chat block's can."""
    text = view._reconciliation.object
    assert "Reconciles with the ledger" in text
    assert "USD" in text and "$" not in text


def test_a_failed_reconciliation_leads_with_lag_not_with_a_data_error():
    """The usual cause is `get_positions` going stale while a futures leg keeps ticking."""
    v = pdash.build_dashboard()
    v.refresh(
        _snapshot(
            positions=_positions(("CL", 1.0, -1000.0)), ledger=_ledger(unrealised_pnl=-5000.0)
        ),
        now=_NOW,
    )
    text = v._reconciliation.object
    assert "do not reconcile" in text
    assert "gone stale while a fast-moving leg" in text
    assert "verify against IBKR" in text


def test_an_unrun_reconciliation_does_not_claim_a_pass():
    """A check that never ran says so — rendering "reconciled" would be the worst outcome."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=(), ledger=None), now=_NOW)
    assert "not checked" in v._reconciliation.object


def test_a_cross_currency_book_claims_no_reconciliation():
    """A book spanning currencies cannot be summed against one ledger, so no delta is claimed."""
    rows = dd.parse_positions(
        [
            {"ticker": "AAPL", "position": 1.0, "unrealizedPnl": 10.0, "currency": "USD"},
            {"ticker": "SAP", "position": 1.0, "unrealizedPnl": -4.0, "currency": "EUR"},
        ]
    )
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=rows), now=_NOW)
    assert "No reconciliation claimed" in v._reconciliation.object


def test_positions_table_has_read_only_affordances_only(view):
    """Filtering, sorting and paging change the view; none can reach an order path."""
    assert view._positions.header_filters is True
    assert view._positions.pagination == "local"
    assert view._positions.page_size == pdash._POSITIONS_ROWS_PER_PAGE
    assert set(view._positions.header_tooltips) <= set(pdash._POSITION_COLUMNS)
    # The Hard Rule 1 guarantees are unchanged by any of the above.
    assert view._positions.disabled is True
    assert not view._positions._on_click_callbacks
    assert not view._positions._on_edit_callbacks


def test_tabs_render_only_the_active_one(view):
    """dynamic=True — and the repaint still reaches widgets in hidden tabs."""
    assert view.tabs.dynamic is True
    view.tabs.active = 0  # hide Positions and P&L
    view.refresh(_snapshot(positions=_positions(("XYZ", 3.0, 42.0))), now=_NOW)
    assert list(view._positions.value["Symbol"]) == ["XYZ"]
    view.tabs.active = 1
    # Located by identity, not by index: the Positions column has gained panes twice
    # now, and an index here turns a layout change into a confusing failure elsewhere.
    assert view._positions in list(view.tabs[1])  # identity survives deactivation


class _Notifications:
    """Captures the notification calls a served session would make."""

    def __init__(self):
        """Start with no recorded calls."""
        self.errors: list[str] = []
        self.successes: list[str] = []

    def error(self, message, duration=None):
        """Record an error toast."""
        self.errors.append(message)

    def success(self, message, duration=None):
        """Record a success toast."""
        self.successes.append(message)


@pytest.fixture
def toasts(monkeypatch):
    """Install a capturing `pn.state.notifications` for the duration of a test."""
    captured = _Notifications()
    # `pn.state.notifications` is a read-only property, so the patch goes on the class:
    # replacing the descriptor with a plain object makes instance access return it.
    monkeypatch.setattr(type(pn.state), "notifications", captured, raising=False)
    return captured


def test_the_browser_side_price_format_keeps_as_many_decimals_as_the_python_one():
    """The tables are the third rendering of a price, and the only one Python cannot reach.

    `NumberFormatter` formats in the browser with numbro, so it cannot call
    `order_confirm.price_text_safe` like the card and the dialog do. It can still agree
    with it. At `0,0.00[00]` it did not: a 6E limit of 1.08455 rendered `1.0846` and a ZN
    stop of 110.171875 rendered `110.1719` on the working-order book — the audit's A-3
    finding, on the surface it was not fixed on (review 2026-09-14).

    Asserted against what the shared formatter actually produces, not against a number
    typed here, so the two cannot drift apart quietly.
    """
    from ibkr_core_mcp.order_confirm import price_text_safe

    optional = pdash._PRICE_FORMAT[pdash._PRICE_FORMAT.index("[") :].count("0")
    mandatory = pdash._PRICE_FORMAT[: pdash._PRICE_FORMAT.index("[")].split(".")[1].count("0")
    allowed = mandatory + optional

    for finest in (1.08455, 110.171875, 3.001):
        needed = len(price_text_safe(finest).split(".")[1])
        assert allowed >= needed, (
            f"the table rounds {finest} to {allowed} decimals; the card shows {needed}"
        )


def test_going_stale_toasts_once_not_every_poll(toasts):
    """The status line is always right — but only if you are looking at it.

    Repeating the toast every five seconds would train the user to dismiss it, which
    costs more than the notification buys.
    """
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)  # first good poll: silent
    assert toasts.errors == [] and toasts.successes == []

    stale = _snapshot(error="IBKR unavailable: boom")
    v.refresh(stale, now=_NOW)
    v.refresh(stale, now=_NOW)
    v.refresh(stale, now=_NOW)
    assert len(toasts.errors) == 1
    assert "stale" in toasts.errors[0]


def test_a_stale_toast_escapes_the_ibkr_error_text(toasts):
    """The stale toast interpolates IBKR/exception text into a body notyf assigns with
    `innerHTML` (audit 2026-09-13, finding A-1). One escape is the control there — the
    toast path has no `html_decode` step, unlike the pane path.
    """
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v.refresh(_snapshot(error="boom <img src=x onerror=alert(1)>"), now=_NOW)

    assert toasts.errors, "expected one stale toast"
    assert "<img" not in toasts.errors[0], f"raw markup reached the toast: {toasts.errors[0]!r}"
    assert "&lt;img" in toasts.errors[0], f"toast text was not escaped: {toasts.errors[0]!r}"


def test_recovery_toasts_once(toasts):
    """Coming back from stale toasts exactly once, not on every poll."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v.refresh(_snapshot(error="boom"), now=_NOW)
    v.refresh(_snapshot(), now=_NOW)
    v.refresh(_snapshot(), now=_NOW)
    assert len(toasts.successes) == 1
    assert "live again" in toasts.successes[0]


def test_the_first_seconds_of_a_session_do_not_toast(toasts):
    """ "Has not polled yet" is the normal first second of every session, not an incident."""
    v = pdash.build_dashboard()
    v.refresh(dd.empty_snapshot(now=_NOW, error="Dashboard has not polled yet."), now=_NOW)
    v.refresh(dd.empty_snapshot(now=_NOW, error="Dashboard has not polled yet."), now=_NOW)
    assert toasts.errors == []


def test_age_alone_makes_a_snapshot_stale(toasts):
    """No error, just an old poll — still stale, and still worth one toast."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    late = _NOW + timedelta(seconds=STALE_AFTER + 1)
    assert v.is_stale(_snapshot(), late)
    v.refresh(_snapshot(), now=late)
    assert len(toasts.errors) == 1


def test_notifications_absent_outside_a_served_session(monkeypatch):
    """`pn.state.notifications` is None headlessly — the repaint must not care."""
    monkeypatch.setattr(type(pn.state), "notifications", None, raising=False)
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v.refresh(_snapshot(error="boom"), now=_NOW)  # must not raise


def test_positions_frame_has_headers_even_with_no_rows():
    """A zero-column frame renders as a blank rectangle — that reads as a broken widget."""
    frame = pdash.positions_frame(dd.empty_snapshot(now=_NOW))
    assert list(frame.columns) == pdash._POSITION_COLUMNS
    assert len(frame) == 0


def test_position_columns_are_pinned_in_reading_order():
    """The literal order, so a reorder is a deliberate act rather than a silent one.

    The assertion above compares the frame against `_POSITION_COLUMNS` — the same
    constant it is built from — so it passes for ANY ordering and cannot notice a
    change. Adding Name and % Unrealised and moving Class ran green through it on
    2026-08-07. This one spells the order out.

    Identity first (Symbol, Name), then size, the two entry levels, price, then money.
    `Class` sits by `Ccy` at the end: factual metadata, not a number acted on (user).
    """
    assert pdash._POSITION_COLUMNS == [
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


def test_pct_unrealised_is_ibkr_over_ibkr_and_ties_to_their_screen():
    """Both halves IBKR's, so the column reconciles against IBKR (user, 2026-08-07).

    Pinned to the live GLD row measured that day, at IBKR's **full precision**. The
    identity `mktValue - avgCost*qty == unrealizedPnl` held to the cent on all three
    live positions, which is what licenses this denominator — and confirms `avgCost` is
    per CONTRACT, so the multiplier must not be reapplied.

    ⚠ The first version of this test used `avgCost: 383.22` — the value as *displayed*
    at 2dp — and the identity missed by 0.25. IBKR's actual figure is 383.215004. A
    fixture built from rounded output tests the rounding, not the arithmetic.
    """
    p = dd.parse_positions(
        [
            {
                "conid": 1,
                "ticker": "GLD",
                "name": "SPDR GOLD SHARES",
                "contractDesc": "GLD",
                "assetClass": "STK",
                "position": 50,
                "avgCost": 383.215004,
                "avgPrice": 383.215004,
                "multiplier": 0.0,
                "mktPrice": 398.869812,
                "mktValue": 19943.49,
                "unrealizedPnl": 782.74,
                "realizedPnl": 0,
                "currency": "USD",
            }
        ]
    )[0]
    assert p.cost_basis == pytest.approx(19160.7502)
    assert p.pct_unrealised == pytest.approx(4.085, abs=0.001)
    # The identity that licenses the denominator.
    assert p.cost_basis is not None
    assert p.market_value - p.cost_basis == pytest.approx(p.unrealised_pnl, abs=0.01)
    # IBKR sends multiplier 0.0 on a stock row; `parse_positions` normalises it to 1.0,
    # which is what keeps Basis Δ from blanking on every equity position.
    assert p.multiplier == 1.0


def test_a_profitable_short_reads_positive_not_inverted():
    """Dividing by a negative basis would render a gain as a loss."""
    p = dd.parse_positions(
        [
            {
                "conid": 2,
                "ticker": "SH",
                "name": "SHORT S&P500",
                "contractDesc": "SH",
                "assetClass": "STK",
                "position": -10,
                "avgCost": -100.0,
                "avgPrice": 100.0,
                "multiplier": 1,
                "mktPrice": 90.0,
                "mktValue": -900.0,
                "unrealizedPnl": 100.0,
                "realizedPnl": 0,
                "currency": "USD",
            }
        ]
    )[0]
    assert p.pct_unrealised == pytest.approx(10.0)


def test_a_zero_basis_yields_no_percentage_rather_than_a_division():
    """A basis of zero is not a basis — and it is this column's denominator."""
    p = dd.parse_positions(
        [
            {
                "conid": 3,
                "ticker": "X",
                "contractDesc": "X",
                "assetClass": "STK",
                "position": 10,
                "avgCost": 0.0,
                "avgPrice": 0.0,
                "multiplier": 1,
                "mktPrice": 5.0,
                "mktValue": 50.0,
                "unrealizedPnl": 50.0,
                "realizedPnl": 0,
                "currency": "USD",
            }
        ]
    )[0]
    assert p.cost_basis is None
    assert p.pct_unrealised is None


def test_name_is_ibkrs_description_and_blank_when_absent():
    """`name` is the description; `fullName` is the contract LABEL despite its name.

    Measured live 2026-08-07: GLD name='SPDR GOLD SHARES' fullName='GLD'; CL
    name='Light Sweet Crude Oil' fullName="CL Sep'26". The lean futures row that omits
    `ticker` can omit `name` too, and a blank cell is the honest rendering of that.
    """
    full, lean = dd.parse_positions(
        [
            {
                "conid": 1,
                "ticker": "CL",
                "name": "Light Sweet Crude Oil",
                "fullName": "CL Sep'26",
                "contractDesc": "CL SEP2026",
                "assetClass": "FUT",
                "position": 2,
                "avgCost": 76697.36,
                "avgPrice": 76.69736,
                "multiplier": 1000,
                "mktPrice": 77.73,
                "mktValue": 155460.01,
                "unrealizedPnl": 2065.29,
                "realizedPnl": 0,
                "currency": "USD",
            },
            {
                "conid": 2,
                "contractDesc": "CL SEP2026",
                "assetClass": "FUT",
                "position": 1,
                "avgCost": 0.0,
                "avgPrice": 0.0,
                "mktPrice": 77.73,
                "mktValue": 77730.0,
                "unrealizedPnl": 0.0,
                "realizedPnl": 0,
                "currency": "USD",
            },
        ]
    )
    assert full.name == "Light Sweet Crude Oil"  # not "CL Sep'26"
    assert lean.name == ""  # absent, not guessed from contractDesc


def test_positions_table_is_populated_and_summarised(view):
    """The table renders the positions and the summary line counts them."""
    assert list(view._positions.value["Symbol"]) == ["ESU6"]
    assert "1 open position(s)" in view._positions_status.object
    assert "-1,000.00 USD" in view._positions_status.object


def test_positions_summary_distinguishes_empty_from_unavailable():
    """An empty book and an unavailable one read differently — they are opposite claims."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=()), now=_NOW)
    assert "No open positions" in v._positions_status.object

    v.refresh(_snapshot(positions=(), ledger=None), now=_NOW)
    assert "IBKR not connected" in v._positions_status.object


def test_multi_currency_positions_are_labelled_mixed():
    """A cross-currency total is labelled mixed rather than stamped with one code."""
    rows = dd.parse_positions(
        [
            {"ticker": "AAPL", "position": 1.0, "unrealizedPnl": 10.0, "currency": "USD"},
            {"ticker": "SAP", "position": 1.0, "unrealizedPnl": -4.0, "currency": "EUR"},
        ]
    )
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=rows), now=_NOW)
    assert "EUR, USD" in v._positions_status.object
    assert "mixed" in v._positions_status.object


def test_pnl_colouring_is_bound_to_the_unrealised_column(view):
    """`.style.map` gives the P&L cells their red/green without any CSS."""
    view._positions.style._compute()
    ctx = view._positions.style.ctx
    assert any(("color", palette.DOWN_COLOR) in styles for styles in ctx.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, f"color: {palette.UP_COLOR}"),
        (-5.0, f"color: {palette.DOWN_COLOR}"),
        (0.0, f"color: {palette.FLAT_COLOR}"),
        ("text", ""),
        (True, ""),
    ],
)
def test_sign_style(value, expected):
    """Positive, negative and zero cells get their own colours, and non-numbers get none."""
    assert pdash._sign_style(value) == expected


def test_chart_is_built_for_the_selected_window(view):
    """The chart is drawn from the window the selector names."""
    import holoviews as hv

    view._window.value = "Weekly"
    week_obj = view._pnl_chart.object
    assert isinstance(week_obj, hv.Layout)
    view._window.value = "YTD"
    assert view._pnl_chart.object is not week_obj


def test_switching_the_window_repaints_without_a_new_poll(view):
    """A radio click must feel immediate — the data is already in memory."""
    view._window.value = "Monthly"
    assert "Monthly" in view._pnl_stats.object
    assert "| Closed round trips (lots) | 5 |" in view._pnl_stats.object


def test_window_selector_before_any_snapshot_does_not_raise():
    """Changing the window before the first poll is a no-op, not an error."""
    v = pdash.build_dashboard()
    v._window.value = "YTD"  # no snapshot yet
    assert v._pnl_chart.object is None


def test_realised_frame_pins_column_names():
    """hvplot binds some arguments positionally — column identity must not drift."""
    pts = (dd.RealisedPoint(date(2026, 8, 3), -1.0, -1.0),)
    frame = pdash.realised_frame(pts)
    assert list(frame.columns) == ["day", "realised", "cumulative"]


def test_empty_series_draws_nothing_rather_than_a_flat_line():
    """An empty axis pretending to be a flat week is worse than an honest message."""
    assert pdash.build_realised_chart((), "t") is None
    v = pdash.build_dashboard()
    v.refresh(_snapshot(series=(), week=None, month=None, ytd=None), now=_NOW)
    assert v._pnl_chart.object is None
    assert "No trade data" in v._pnl_stats.object


def test_a_single_point_window_explains_itself_instead_of_drawing_a_broken_axis():
    """One trading day made bokeh fall back to a millisecond x-axis — observed live.

    A Monday-start week read on a Tuesday, with Flex still T+1, legitimately has one
    point. The chart is suppressed and the figure is stated in words instead.
    """
    one = (dd.RealisedPoint(date(2026, 8, 3), -3516.98, -3516.98),)
    assert pdash.build_realised_chart(one, "t") is None
    assert "at least two" in pdash.realised_chart_note(one, "USD")
    assert "-3,516.98 USD" in pdash.realised_chart_note(one, "USD")

    v = pdash.build_dashboard()
    v.refresh(
        _snapshot(
            series=one,
            week=_window(date(2026, 8, 3), date(2026, 8, 4), -3516.98, 4),
        ),
        now=_NOW,
    )
    v._window.value = "Weekly"
    assert v._pnl_chart.object is None
    assert "Only one trading day" in v._pnl_chart_note.object
    # The stats table must still render — only the curve is suppressed.
    assert "Settled realised P&L" in v._pnl_stats.object


def test_the_chart_note_is_empty_when_a_chart_was_drawn(view):
    """The explanatory note is empty when there is a chart to look at."""
    view._window.value = "Weekly"
    assert view._pnl_chart.object is not None
    assert view._pnl_chart_note.object == ""


def test_the_chart_note_reports_an_empty_window():
    """An empty window is explained in words instead of an empty axis."""
    assert "No realised P&L" in pdash.realised_chart_note((), "USD")


def test_chart_layout_has_two_stacked_rows():
    """The realised chart is the cumulative curve over the daily bars."""
    import holoviews as hv

    pts = tuple(dd.RealisedPoint(date(2026, 8, d), 1.0 * d, 1.0 * d) for d in (3, 4, 5, 6))
    layout = pdash.build_realised_chart(pts, "title")
    assert isinstance(layout, hv.Layout)
    assert len(list(layout)) == 2


def test_refresh_never_raises_and_keeps_the_previous_frame(view, caplog):
    """A repaint that throws inside a periodic callback would freeze the dashboard."""
    before = view._tiles["net_liq"].value

    class _Exploding:
        """A snapshot stand-in whose first attribute access raises."""

        def __getattr__(self, name):
            """Raise on any attribute the repainter reaches for."""
            raise RuntimeError("bad snapshot")

    with caplog.at_level("ERROR"):
        view.refresh(_Exploding())
    assert view._tiles["net_liq"].value == before
    assert "repaint failed" in caplog.text


def test_stats_markdown_handles_a_window_with_no_lots():
    """A window with no closed lots omits the win/loss rows rather than printing zeros."""
    text = pdash.stats_markdown(
        _window(date(2026, 8, 3), _TODAY, 0.0, 0, {}),
        _stats(lots=0, wins=0, losses=0, gross_win=0.0, gross_loss=0.0),
        "Week",
    )
    assert "Closed round trips (lots) | 0" in text
    assert "win rate" not in text


def test_stats_markdown_with_no_data():
    """With no window at all the block says so."""
    assert "No trade data" in pdash.stats_markdown(None, None, "Week")


def test_ledger_markdown_with_no_ledger():
    """With no ledger the block says it is unavailable rather than rendering blanks."""
    assert "unavailable" in pdash.ledger_markdown(_snapshot(ledger=None))


def test_ledger_markdown_discloses_other_currency_balances():
    """Other currency balances are disclosed, so one currency is not read as the account."""
    snap = _snapshot(ledger=_ledger(other_currencies=("CHF", "EUR")))
    assert "CHF, EUR" in pdash.ledger_markdown(snap)


# ── Orders tab (2026-08-05) ───────────────────────────────────────────────────
#
# Added after a live place → modify → cancel run left the dashboard unchanged throughout:
# the working book existed only in the chat's opening message, printed once at startup and
# never updated. Positions were correct the whole time — a resting limit order is not a
# position — so the gap was a missing view, not a stale one.


def _order(**kw):
    """A LiveOrder with the fields the order table renders."""
    from claudia.dashboard_data import LiveOrder

    base: dict[str, Any] = {
        "order_id": "314390101",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1.0,
        "filled": 0.0,
        "price": 100.0,
        "order_type": "LMT",
        "tif": "GTC",
        "status": "Submitted",
        "origin": "",
    }
    base.update(kw)
    return LiveOrder(**base)


@pytest.mark.parametrize(("tif", "shown"), [("DAY", "DAY"), ("GTC", "GTC"), ("", "—")])
def test_the_tif_column_shows_a_dash_for_an_unknown_tif(tif, shown):
    """Gap #70: an unknown TIF (order status unread or failed) is a dash, never a blank cell
    that could read as "no TIF", and never the live-orders row's raw value."""
    frame = pdash.orders_frame(_snapshot(orders=(_order(tif=tif),)))
    assert frame["TIF"].tolist() == [shown]


def test_orders_frame_keeps_its_columns_when_the_book_is_empty():
    """A zero-column Tabulator renders as a blank rectangle — that reads as a broken
    widget, not as an empty book (same reason positions_frame does this)."""
    frame = pdash.orders_frame(_snapshot(orders=()))
    assert list(frame.columns) == pdash._ORDER_COLUMNS
    assert len(frame) == 0


def test_orders_frame_renders_a_working_order():
    """A working order renders across the full column set."""
    frame = pdash.orders_frame(_snapshot(orders=(_order(),)))
    row = frame.iloc[0]
    assert row["Order"] == "314390101"
    assert row["Symbol"] == "AAPL"
    assert row["Limit"] == 100.0
    assert row["Status"] == "Submitted"


def test_an_unavailable_book_is_not_reported_as_an_empty_one():
    """The distinction this whole feature turns on. `None` means the lookup failed;
    saying "no working orders" there would tell a trader nothing is resting when
    something might be."""
    unknown = pdash.orders_status_line(_snapshot(orders=None))
    empty = pdash.orders_status_line(_snapshot(orders=()))

    assert unknown != empty
    assert "unavailable" in unknown.lower()
    # Not merely "avoids the phrase" — it names the distinction outright, which is what
    # stops a reader filling the gap with the wrong one of the two.
    assert "not the same as having no working orders" in unknown.lower()
    assert empty.lower().strip("_ ") == "no working orders."


def test_orders_status_counts_claudia_staged_separately():
    """The status line separates ClaudIA-staged orders from external ones."""
    snap = _snapshot(orders=(_order(origin="CLAUDIA-1785941569825"), _order(order_id="2")))
    line = pdash.orders_status_line(snap)
    assert "2 working order(s)" in line
    assert "1 staged by ClaudIA" in line


def test_the_orders_table_is_read_only_and_has_no_handlers(view):
    """Hard Rule 1. An order row is the one place a click could plausibly be wired to
    "cancel this" — it must not be. Cancelling goes through propose_cancel and both
    gates, never a table cell."""
    assert view._orders.disabled is True
    assert not view._orders._on_click_callbacks
    assert not view._orders._on_edit_callbacks


def test_orders_blank_when_the_account_half_goes_stale(view):
    """`without_account` clears the book along with the ledger and positions: it comes
    from the same gateway, so continuing to show it would be the stale-figures failure
    the rest of the dashboard already refuses."""
    snap = _snapshot(orders=(_order(),))
    assert snap.without_account().orders is None


def test_the_dashboard_has_an_orders_tab(view):
    """The order book has its own tab."""
    assert list(view.tabs._names) == ["Chart", "Positions", "Orders", "P&L"]


def test_the_orders_table_formats_its_numbers_like_the_positions_table(view):
    """Numeric order columns carry a format and right alignment — they had neither.

    Raw IBKR floats rendered as `1.0` and `6000.5` beside the positions table's `1` and
    `6,000.50`, which is the unscannable column that table already fixed once. The
    read-only guarantees these sit alongside are asserted by
    `test_order_table_is_read_only_no_click_or_edit_handlers`, not repeated here.
    """
    assert set(view._orders.formatters) == {"Qty", "Filled", "Limit", "Stop"}
    assert view._orders.text_align == {
        "Qty": "right",
        "Filled": "right",
        "Limit": "right",
        "Stop": "right",
    }
    assert set(view._orders.header_tooltips) <= set(pdash._ORDER_COLUMNS)


# -- The P&L pane's Daily window ----------------------------------------------


def _bd(asset, winners, losers, net=0.0):
    """One TypeBreakdown with just the fields these tables read."""
    return dd.TypeBreakdown(
        asset_class=asset,
        net=net,
        gross_win=0.0,
        gross_loss=0.0,
        winners=winners,
        losers=losers,
        scratches=0,
    )


def _snap_with(
    day_rows=(), week_rows=(), incomplete=False, reconstructed=True, ledger=None, as_of=None
):
    """A snapshot carrying only the bridged breakdowns these surfaces consume."""
    return dd.DashboardSnapshot(
        as_of=as_of or datetime.now(UTC),
        ledger=ledger,
        breakdowns={
            "day": dd.BridgedWindow(
                rows=tuple(day_rows), incomplete=incomplete, reconstructed=reconstructed
            ),
            "week": dd.BridgedWindow(rows=tuple(week_rows), reconstructed=reconstructed),
        },
    )


def test_the_daily_tab_shows_todays_realised_by_asset_class():
    """The requirement: today's realised P&L per type, non-Flex, on its own tab."""
    out = pdash.daily_table(
        _snap_with(day_rows=[_bd("FUT", 2, 0, net=1841.04), _bd("STK", 0, 1, net=-141.29)]),
        "USD",
    )
    assert "| **FUT** | 1,841.04 |" in out
    assert "| **STK** | -141.29 |" in out
    assert "**Total** | **1,699.75**" in out
    assert "Net (USD)" in out


def test_a_gateway_outage_never_reads_as_a_flat_day():
    """ "Nothing closed" and "we could not look" are opposite claims.

    No statement covers today, so an unreachable gateway leaves today genuinely
    unknowable — and it is the one day no later data can contradict.
    """
    out = pdash.daily_table(_snap_with(reconstructed=False))
    assert "cannot be computed" in out
    assert "round trip" not in out


def test_a_quiet_day_is_stated_as_one():
    """A reachable gateway that found no closes is an answer, not a failure."""
    out = pdash.daily_table(_snap_with(reconstructed=True))
    assert "No closed round trips today" in out
    assert "cannot be computed" not in out


def test_before_the_first_poll_the_daily_tab_claims_nothing():
    """Waiting, no trades, and no gateway are three states and must read as three."""
    assert "waiting" in pdash.daily_table(None)
    assert "waiting" in pdash.daily_table(dd.empty_snapshot())


def test_an_incomplete_day_is_marked_not_silently_short():
    """A floor presented as a total is the failure this whole track exists to prevent."""
    out = pdash.daily_table(_snap_with(day_rows=[_bd("FUT", 1, 0, net=100.0)], incomplete=True))
    assert "⚠" in out and "incomplete" in out


def test_a_day_that_could_not_be_reconstructed_is_not_reported_as_quiet():
    """Declined and quiet are opposite claims, and 2026-08-10 published the wrong one.

    Every one of that day's seven CL executions closed against a lot opened before the
    fill window, so the reconstruction declined the whole contract and the window came
    back with no rows — and `incomplete` set. The pane read the empty rows alone and
    called it a session with no closed round trips, on a day that realised +8,441.12.

    A window with no rows is only quiet if nothing in it was declined.
    """
    out = pdash.daily_table(_snap_with(incomplete=True), "USD")
    assert "No closed round trips today" not in out
    assert "could not be reconstructed" in out


def test_the_heading_names_the_window_it_labels_not_the_poll_time():
    """`as_of` and the day window are read at different moments and can disagree.

    `as_of` is stamped when the poll *completes*, after `_read_flex` has already called
    `date.today()` — so a poll straddling local midnight dates them a day apart. And a
    failed poll republishes the *previous* `as_of` on purpose, to keep staleness visible,
    which would peg the heading to an older day than the figures beneath it for as long
    as polling stays down. The heading follows the window.
    """
    snap = dd.DashboardSnapshot(
        as_of=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        breakdowns={"day": dd.BridgedWindow(rows=(), reconstructed=True, day=date(2026, 8, 7))},
    )
    assert "2026-08-07" in pdash.daily_heading(snap)
    assert "2026-08-06" not in pdash.daily_heading(snap)


def test_the_heading_falls_back_to_as_of_with_no_day_window():
    """No day window means no figures to mislabel, and no better answer available."""
    stamp = datetime(2026, 8, 7, 18, 30, tzinfo=UTC)
    snap = dd.DashboardSnapshot(as_of=stamp, breakdowns={"week": dd.BridgedWindow()})
    assert stamp.astimezone().strftime("%a %Y-%m-%d") in pdash.daily_heading(snap)


def test_the_daily_heading_names_the_day_and_the_source():
    """The date comes from `as_of` in local time — the clock the poller builds the day
    window with, so heading and window can never name different days."""
    stamp = datetime(2026, 8, 7, 18, 30, tzinfo=UTC)
    out = pdash.daily_heading(_snap_with(as_of=stamp))
    assert stamp.astimezone().strftime("%a %Y-%m-%d") in out
    assert "your own executions" in out
    assert "T+1" in out


def test_a_stale_daily_figure_is_labelled_a_floor_not_blanked():
    """A resting order can fill while the gateway is unreachable, so a stale figure is
    short, not wrong — and must not pass as current."""
    out = pdash.daily_heading(_snap_with(), stale=True)
    assert "Not current" in out and "floor" in out
    assert "Not current" not in pdash.daily_heading(_snap_with(), stale=False)


def test_the_daily_table_never_claims_a_flex_provenance():
    """The table was right and the sentence under it was a confident lie.

    `breakdown_table`'s footnote names `flex_trade` and `flex_lot` as the sources. That
    is true of the settled windows and FALSE of the day window: no statement covers
    today, so every figure there is reconstructed from the account's own executions.
    Shipped attached to the day window on 2026-08-07 and caught in the browser the same
    hour — which is why the note is now a parameter and this test exists.
    """
    out = pdash.daily_table(_snap_with(day_rows=[_bd("FUT", 0, 2, net=-1629.44)]), "USD")
    assert "statement basis" not in out
    assert "pre-wash-sale" not in out
    assert "Reconstructed FIFO from your own executions" in out
    # The settled windows keep the note that is true of them.
    assert "statement basis" in pdash.breakdown_table(
        dd.BridgedWindow(rows=(_bd("FUT", 1, 0, net=100.0),)), "USD"
    )


def test_daily_is_the_first_window_option_not_a_separate_tab():
    """All P&L under one tab, chosen by the selector, shortest window first (user)."""
    v = pdash.build_dashboard()
    assert list(v._window.options) == ["Daily", "Weekly", "Monthly", "YTD"]
    assert v._window.value == "Weekly"


def test_selecting_daily_repaints_the_pane_from_the_held_snapshot():
    """A radio click must feel immediate — the data is already in memory."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v._window.value = "Daily"
    assert "Today —" in v._pnl_source_note.object
    assert isinstance(v._pnl_breakdown.object, str)


def test_daily_draws_no_curve_and_no_settled_block():
    """One day is a point, not a shape — and no statement covers today, so the
    settled block would put a week's confirmed figures under a "Daily" heading."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v._window.value = "Daily"
    assert v._pnl_chart.object is None
    assert v._pnl_chart_note.object == ""
    assert v._pnl_stats.object == ""


def test_leaving_daily_clears_its_source_note():
    """The note is about today only; it must not linger over a settled window."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(), now=_NOW)
    v._window.value = "Daily"
    v._window.value = "Weekly"
    assert v._pnl_source_note.object == ""
    assert "Today —" not in v._pnl_stats.object


# -- The P&L pane's per-type detail -------------------------------------------


def _full_bd(asset, net, gw, gl, wins, losses):
    """A TypeBreakdown with every column the pane renders."""
    return dd.TypeBreakdown(
        asset_class=asset,
        net=net,
        gross_win=gw,
        gross_loss=gl,
        winners=wins,
        losers=losses,
        scratches=0,
    )


def test_the_pane_shows_money_counts_and_averages_together():
    """Rate alone and net alone each report the opposite of what happened.

    Measured on this account's own year: FUT won 55% of trades and still lost money,
    while STK won 14% and lost less. Either figure in isolation misleads.
    """
    win = dd.BridgedWindow(
        rows=(
            _full_bd("FUT", -17015.98, 161517.42, -178533.40, 159, 132),
            _full_bd("STK", -3203.71, 11794.38, -27604.68, 14, 83),
        )
    )
    out = pdash.breakdown_table(win, "USD")
    assert "55%" in out and "1,015.83" in out and "-1,352.53" in out  # FUT
    assert "14%" in out and "842.46" in out and "-332.59" in out  # STK
    assert "**-20,219.69**" in out, "the total must be the sum of the rows"


def test_the_pane_states_that_net_and_lots_come_from_different_tables():
    """`flex_trade` and `flex_lot` are different quantities and need not tie.

    Leaving a reader to discover that by subtraction is how a correct pair of numbers
    gets reported as a bug.
    """
    out = pdash.breakdown_table(dd.BridgedWindow(rows=(_full_bd("FUT", 1.0, 1.0, 0.0, 1, 0),)))
    assert "flex_trade" in out and "flex_lot" in out and "wash-sale" in out


def test_the_pane_marks_an_incomplete_window():
    """Over-communicate rather than fail silently."""
    win = dd.BridgedWindow(rows=(_full_bd("FUT", 1.0, 1.0, 0.0, 1, 0),), incomplete=True)
    assert "incomplete" in pdash.breakdown_table(win)


def test_an_absent_average_renders_a_dash_not_a_zero():
    """No winning lot means there is no average win; 0.00 would claim a break-even trade."""
    out = pdash.breakdown_table(dd.BridgedWindow(rows=(_full_bd("STK", -10.0, 0.0, -10.0, 0, 3),)))
    assert "—" in out


def test_an_empty_window_says_so():
    """A month with no closes must not render an empty table shell."""
    assert "No closed trades" in pdash.breakdown_table(dd.BridgedWindow())
    assert "No closed trades" in pdash.breakdown_table(None)


def test_the_daily_tab_emits_no_html_tags():
    """`safe_markdown` escapes HTML, so a tag reaches the screen as literal text.

    Caught in a browser 2026-08-06 on the block this tab replaced: `<sub>` markup
    rendered as "100% <sub>2W/0L</sub>". No unit test could see it, because the string
    itself was correct — only the rendered page was wrong. The guard moves with the
    surface rather than being deleted with it.
    """
    snap = _snap_with(day_rows=[_bd("FUT", 2, 0, net=1841.04)])
    for out in (
        pdash.daily_table(snap, "USD"),
        pdash.daily_heading(snap),
        pdash.daily_heading(snap, stale=True),
    ):
        assert "<" not in out and ">" not in out


def test_the_week_tile_and_the_pnl_pane_report_the_same_week():
    """Two totals for one window on one screen is the worst failure available here.

    Until 2026-08-06 the KPI tile read Flex-only (-6,175.88) while the P&L pane read the
    bridged figure (-16,480.46) — a ten-thousand difference, visible side by side. Both
    must now come from the same bridged window.
    """
    week = dd.BridgedWindow(
        rows=(
            dd.TypeBreakdown("FUT", -13230.76, 4864.86, -18095.62, 5, 5, 0),
            dd.TypeBreakdown("STK", -3249.70, 0.0, -3249.70, 0, 23, 0),
        )
    )
    v = pdash.build_dashboard()
    snap = dd.DashboardSnapshot(as_of=_NOW, breakdowns={"week": week})
    v.refresh(snap, now=_NOW)

    assert v._tiles["realised_week"].value == pytest.approx(week.net, abs=0.005)
    assert f"{week.net:,.2f}" in pdash.breakdown_table(week)


# -- Live quotes on the positions table ---------------------------------------


def _quoted(**over):
    """One position carrying a live quote, with the fields these tests read."""
    base = {
        "conid": 1,
        "ticker": "GLD",
        "name": "SPDR GOLD SHARES",
        "contractDesc": "GLD",
        "assetClass": "STK",
        "position": 50,
        "avgCost": 383.215004,
        "avgPrice": 383.215004,
        "multiplier": 0.0,
        "mktPrice": 398.7787,
        "mktValue": 19943.49,
        "unrealizedPnl": 782.74,
        "realizedPnl": 0,
        "currency": "USD",
    }
    p = dd.parse_positions([base])[0]
    return replace(p, quote=dd.Quote(conid=1, **over)) if over else p


def test_last_prefers_the_live_quote_over_ibkrs_cached_price():
    """The gap is real money: measured 398.7787 cached against 399.00 live."""
    p = _quoted(last=399.00, change=9.33, change_pct=2.39, status="RivB")
    assert p.last_price == 399.00
    assert p.market_price == 398.7787  # IBKR's own is kept, not overwritten


def test_last_falls_back_to_ibkr_when_there_is_no_quote():
    """A missing quote must not blank the column — IBKR's price is still a price."""
    assert _quoted().last_price == pytest.approx(398.7787)


def test_change_columns_are_blank_without_a_quote_never_zero():
    """0.00 asserts the instrument did not move; blank says we do not know."""
    frame = pdash.positions_frame(_snapshot(positions=(_quoted(),)))
    assert frame["Change"].iloc[0] is None
    assert frame["% Change"].iloc[0] is None


def test_the_pane_states_that_ibkrs_figures_lag_the_live_price():
    """A lag stated is fine; a lag concealed is the failure this pane is written against."""
    note = pdash.quote_note(
        _snapshot(positions=(_quoted(last=399.00, change=9.33, change_pct=2.39, status="RivB"),))
    )
    assert "lag" in note
    assert "Market value" in note and "Unrealised" in note
    # No warning rows when everything is a live real-time feed.
    assert "⚠" not in note


def test_a_non_live_feed_is_named_on_screen():
    """Delayed/frozen/unsubscribed rendered as live is a wrong number on a trade screen."""
    note = pdash.quote_note(_snapshot(positions=(_quoted(last=399.00, status="DZ"),)))
    assert "⚠" in note and "real-time" in note and "GLD" in note


def test_a_prior_close_and_a_halt_are_reported_separately():
    """They mean different things: no trade today, versus no market right now."""
    close = pdash.quote_note(
        _snapshot(positions=(_quoted(last=399.00, status="RivB", last_is_close=True),))
    )
    halt = pdash.quote_note(
        _snapshot(positions=(_quoted(last=399.00, status="RivB", halted=True),))
    )
    assert "previous close" in close and "halted" not in close
    assert "halted" in halt and "previous close" not in halt


def test_the_note_is_silent_with_no_positions():
    """Nothing to disclose about a book that is empty."""
    assert pdash.quote_note(_snapshot(positions=())) == ""


def test_every_position_column_has_a_width():
    """Fourteen columns in a half-window pane clip silently without explicit widths.

    A column added without a width would be sized by Tabulator and can push the money
    columns off-screen again — which is invisible to every other test, because the
    string values are all correct and only the rendered page is wrong (seen in the
    browser 2026-08-07).
    """
    assert set(pdash._POSITION_WIDTHS) == set(pdash._POSITION_COLUMNS)


def test_the_positions_table_scrolls_rather_than_clipping():
    """`fit_data_stretch` squeezed fourteen columns and hid six with no scrollbar."""
    v = pdash.build_dashboard()
    assert v._positions.layout == "fit_data"


def test_percentages_are_stored_as_fractions_for_the_numbro_format():
    """Numbro's "%" format multiplies by 100 — the frame must hold the fraction.

    `Position.pct_unrealised` stays a percentage (4.12); only the display layer divides,
    so every caller and test reads the property as the percentage it is named after.
    Storing 4.12 here would render "+412.00%" — correct string, correct property, wrong
    cell, and invisible to any assertion on the property itself.
    """
    p = _quoted(last=399.00, change=9.33, change_pct=2.39, status="RivB")
    frame = pdash.positions_frame(_snapshot(positions=(p,)))
    assert p.pct_unrealised == pytest.approx(4.085, abs=0.001)  # property: percent
    assert frame["% Unrealised"].iloc[0] == pytest.approx(0.04085, abs=0.00001)  # cell: fraction
    assert frame["% Change"].iloc[0] == pytest.approx(0.0239)
    assert pdash._PERCENT_FORMAT.endswith("%")


# -- Currency symbol on the money columns -------------------------------------


def _pos_ccy(ccy, conid=1):
    """One position in a given currency, for the book-currency tests."""
    return dd.parse_positions(
        [
            {
                "conid": conid,
                "ticker": f"S{conid}",
                "contractDesc": f"S{conid}",
                "assetClass": "STK",
                "position": 1,
                "avgCost": 10.0,
                "avgPrice": 10.0,
                "multiplier": 1,
                "mktPrice": 11.0,
                "mktValue": 11.0,
                "unrealizedPnl": 1.0,
                "realizedPnl": 0,
                "currency": ccy,
            }
        ]
    )[0]


def test_a_single_currency_book_gets_its_symbol():
    """ "When applicable" — the user's phrasing, and the safety property (2026-08-07)."""
    assert pdash.book_currency(_snapshot(positions=(_pos_ccy("USD"),))) == "USD"
    assert pdash._money_formats("USD")["Market value"].startswith("$")
    assert pdash._money_formats("USD")["Change"].startswith("+$")


def test_a_mixed_book_gets_no_symbol_at_all():
    """A Bokeh formatter is per COLUMN, not per row: two currencies cannot be
    symbolled differently, so neither is symbolled. `$` on a EUR figure is exactly the
    failure the never-a-bare-$ rule exists for — IGV once showed a US ETF in MXN."""
    mixed = _snapshot(positions=(_pos_ccy("USD", 1), _pos_ccy("EUR", 2)))
    assert pdash.book_currency(mixed) == ""
    assert pdash._money_formats("")["Market value"] == pdash._MONEY_FORMAT


def test_an_unknown_currency_is_never_guessed_at():
    """A currency with no entry in the symbol map renders bare, not as dollars."""
    assert pdash._money_formats("SGD")["Market value"] == pdash._MONEY_FORMAT
    assert pdash._money_formats("CAD")["Last"] == pdash._PRICE_FORMAT


def test_an_empty_book_has_no_currency_to_label_with():
    """Guessing the account's base currency would symbol figures that do not exist."""
    assert pdash.book_currency(_snapshot(positions=())) == ""


def test_the_symbol_follows_the_book_when_it_changes():
    """The guard must not pin the first currency it ever saw."""
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=(_pos_ccy("USD"),)), now=_NOW)
    assert v._positions.formatters["Market value"].format.startswith("$")
    v.refresh(_snapshot(positions=(_pos_ccy("USD", 1), _pos_ccy("EUR", 2))), now=_NOW)
    assert not v._positions.formatters["Market value"].format.startswith("$")


# ── Outside RTH column (2026-09-04) ───────────────────────────────────────────


def test_order_columns_show_outside_rth_right_after_tif():
    """TIF and the outside-RTH attribute together decide when a resting order can act;
    they sit side by side so a trader reads both in one glance."""
    cols = pdash._ORDER_COLUMNS
    assert cols.index("Outside RTH") == cols.index("TIF") + 1


def test_orders_frame_renders_outside_rth_as_yes_no_or_not_reported():
    """True → Yes, False → No, None → '—' (IBKR did not report it — never a bare 'No')."""
    frame = pdash.orders_frame(
        _snapshot(
            orders=(
                _order(order_id="1", outside_rth=True),
                _order(order_id="2", outside_rth=False),
                _order(order_id="3", outside_rth=None),
            )
        )
    )
    assert list(frame["Outside RTH"]) == ["Yes", "No", "—"]
    assert "not reported" in pdash._ORDER_TOOLTIPS["Outside RTH"].lower()


def test_order_columns_show_stop_right_after_limit():
    """A stop's resting price is not a limit; the book says which one it is."""
    cols = pdash._ORDER_COLUMNS
    assert cols.index("Stop") == cols.index("Limit") + 1
    assert "Stop" in pdash._ORDER_NUMERIC


def test_orders_frame_renders_the_stop_price():
    """The live ES stop of 2026-09-04: no limit, stop 7735 — rendered in its own column."""
    frame = pdash.orders_frame(
        _snapshot(
            orders=(
                _order(
                    order_id="853170745",
                    symbol="ES",
                    order_type="Stop",
                    price=None,
                    stop_price=7735.0,
                ),
            )
        )
    )
    row = frame.iloc[0]
    assert row["Stop"] == 7735.0
    assert pd.isna(row["Limit"])


def _es_identity():
    """The ES Sep 2026 identity as the poller caches it."""
    from claudia.contract_identity import ContractIdentity

    return ContractIdentity(649180671, "ESU6", "SEP26", "2026-09-18", "E-mini S&P 500", 50.0, "USD")


def test_positions_frame_shows_the_local_symbol_and_the_month_for_a_future():
    """Gap #37: `ESU6` in Symbol once the identity is known, the month in Name; a stock
    keeps its ticker and plain name."""
    fut = dd.Position(
        conid=649180671,
        symbol="ES",
        description="ES SEP2026",
        asset_class="FUT",
        quantity=1.0,
        average_cost=380000.0,
        market_price=7601.0,
        market_value=380050.0,
        unrealised_pnl=50.0,
        realised_pnl=0.0,
        currency="USD",
        average_price=7600.0,
        multiplier=50.0,
        name="E-mini S&P 500",
        full_name="ES Sep18'26",
    )
    stk = dd.Position(
        conid=9,
        symbol="F",
        description="F",
        asset_class="STK",
        quantity=10.0,
        average_cost=10.0,
        market_price=11.0,
        market_value=110.0,
        unrealised_pnl=10.0,
        realised_pnl=0.0,
        currency="USD",
        average_price=10.0,
        name="Ford Motor Co",
        full_name="F",
    )
    frame = pdash.positions_frame(
        _snapshot(positions=(fut, stk), identities={649180671: _es_identity()})
    )
    assert list(frame["Symbol"]) == ["ESU6", "F"]
    assert list(frame["Name"]) == ["E-mini S&P 500 · Sep18'26", "Ford Motor Co"]
    without = pdash.positions_frame(_snapshot(positions=(fut,), identities={}))
    assert list(without["Symbol"]) == ["ES"]


def test_orders_frame_has_a_name_column_after_symbol_and_uses_the_local_symbol():
    """The Orders tab names the contract like the Positions tab does."""
    fut = _order(
        order_id="1217252288",
        symbol="ES",
        order_type="STP",
        price=None,
        stop_price=7900.0,
        conid=649180671,
        sec_type="FUT",
        company_name="E-mini S&P 500",
        description1="Sep18'26(50)",
    )
    stk = _order(conid=265598, sec_type="STK", company_name="APPLE INC", description1="AAPL")
    frame = pdash.orders_frame(_snapshot(orders=(fut, stk), identities={649180671: _es_identity()}))
    assert list(frame.columns)[:3] == ["Order", "Symbol", "Name"]
    assert list(frame["Symbol"]) == ["ESU6", "AAPL"]
    assert list(frame["Name"]) == ["E-mini S&P 500 · Sep18'26", "APPLE INC"]


# ── gap #59: the realised chart must colour by sign ──────────────────────────
#
# Measured 2026-09-23 before the fix: a window whose cumulative ran -100 -> -400 rendered
# Area and Curve in #26a69a (the UP colour) and every daily bar in #8a8a8a. A losing month
# drew green and a +2,000 day looked identical to a -2,000 day. Classic convention, per the
# user 2026-09-23: positive green, negative red, on both rows.
#
# The cumulative curve is ONE line, so it takes one colour: the sign of where the window
# ENDS. The bars are coloured per bar.


def _chart_colors(layout):
    """(area_color, curve_color) from the cumulative overlay of a realised chart.

    Selected by exact type, not by position: the overlay also carries the break-even
    `HLine`, whose neutral colour is not a verdict on the window. `type(...) is` rather
    than isinstance because **hv.Area subclasses hv.Curve**.
    """
    import holoviews as hv

    overlay = next(iter(layout))
    wanted = {hv.Area: None, hv.Curve: None}
    for sub in overlay:
        if type(sub) in wanted and wanted[type(sub)] is None:
            wanted[type(sub)] = sub.opts.get("style").kwargs.get("color")
    return wanted[hv.Area], wanted[hv.Curve]


def _bar_colors(layout):
    """Per-bar fill colours as Bokeh will actually render them.

    **Two shapes, both correct** (measured 2026-09-23). When the bars do not all share a
    colour, HoloViews maps a per-row `color` column onto `fill_color`. When they DO all
    share one — a straight losing week, which is the common real case — it collapses to a
    literal scalar and emits no column at all. A helper that only understood the first
    shape passed the mixed test while leaving the uniform one unverified.
    """
    import holoviews as hv

    fig = hv.render(list(layout)[1], backend="bokeh")
    for r in fig.renderers:
        glyph = getattr(r, "glyph", None)
        src = getattr(r, "data_source", None)
        if glyph is None or src is None or not hasattr(glyph, "fill_color"):
            continue
        if "color" in src.data:
            return list(src.data["color"])
        if isinstance(glyph.fill_color, str):
            return [glyph.fill_color] * len(src.data["realised"])
    raise AssertionError("no bar fill colour reached the Bokeh glyph")


def test_a_losing_window_draws_the_cumulative_curve_red():
    """A month that lost money must not render in the up colour."""
    pts = tuple(dd.RealisedPoint(date(2026, 8, d), -100.0, -100.0 * (d - 2)) for d in (3, 4, 5, 6))
    area, curve = _chart_colors(pdash.build_realised_chart(pts, "losing"))
    assert area == palette.DOWN_COLOR, f"area drew {area}"
    assert curve == palette.DOWN_COLOR, f"curve drew {curve}"


def test_a_winning_window_draws_the_cumulative_curve_green():
    """The up case must keep the up colour."""
    pts = tuple(dd.RealisedPoint(date(2026, 8, d), 100.0, 100.0 * (d - 2)) for d in (3, 4, 5, 6))
    area, curve = _chart_colors(pdash.build_realised_chart(pts, "winning"))
    assert area == palette.UP_COLOR
    assert curve == palette.UP_COLOR


def test_daily_bars_are_coloured_one_by_one_by_their_own_sign():
    """A profitable day and a losing day must not look identical."""
    vals = [250.0, -400.0, 0.0, 175.0]
    pts = tuple(
        dd.RealisedPoint(date(2026, 8, 3 + i), v, sum(vals[: i + 1])) for i, v in enumerate(vals)
    )
    colors = _bar_colors(pdash.build_realised_chart(pts, "mixed"))
    assert colors == [
        palette.UP_COLOR,
        palette.DOWN_COLOR,
        palette.FLAT_COLOR,
        palette.UP_COLOR,
    ], colors


def test_an_all_losing_week_paints_every_bar_red():
    """The common real case: every day down. Uniform bars collapse to a scalar fill."""
    pts = tuple(dd.RealisedPoint(date(2026, 8, d), -100.0, -100.0 * (d - 2)) for d in (3, 4, 5))
    assert _bar_colors(pdash.build_realised_chart(pts, "all down")) == [palette.DOWN_COLOR] * 3


def test_an_all_winning_week_paints_every_bar_green():
    """And its mirror, so the scalar path is pinned in both directions."""
    pts = tuple(dd.RealisedPoint(date(2026, 8, d), 100.0, 100.0 * (d - 2)) for d in (3, 4, 5))
    assert _bar_colors(pdash.build_realised_chart(pts, "all up")) == [palette.UP_COLOR] * 3


def test_a_sub_cent_figure_stays_neutral_rather_than_red():
    """Money renders to 2dp: a value displaying as 0.00 must not be painted a loss."""
    assert palette.pnl_color(-0.003) == palette.FLAT_COLOR
    assert palette.pnl_color(0.0) == palette.FLAT_COLOR
    assert palette.pnl_color(-5.0) == palette.DOWN_COLOR
    assert palette.pnl_color(5.0) == palette.UP_COLOR


# ── The dead band is half the smallest DISPLAYED unit, per column ─────────────
#
# Found by reviewing 2026-09-23's own diff, not by the suite. `_sign_style` began
# deferring to `pnl_color`, whose half-cent band is derived from money's two decimals.
# The percentage columns hold FRACTIONS (`_as_fraction`: 2.39% is stored 0.0239) and
# render through `"+0,0.00%"`, so the same numeric band is 100x too wide there: every
# move smaller than ±0.5% was painted flat while the cell displayed it plainly.


@pytest.mark.parametrize("pct", [0.49, -0.49, 0.10, -0.10, 0.01, -0.01])
def test_a_visible_percentage_move_is_never_painted_flat(pct):
    """If the cell shows a move, the colour must show its direction.

    Parametrised over the band that was wrong: `0.01` is the smallest the format can
    render, `0.49` the largest that the money band swallowed.
    """
    stored = pdash._as_fraction(pct)
    expected = palette.UP_COLOR if pct > 0 else palette.DOWN_COLOR
    assert pdash._percent_sign_style(stored) == f"color: {expected}", (
        f"{pct:+.2f}% displays as a real move but was coloured flat"
    )


@pytest.mark.parametrize("pct", [0.004, -0.004, 0.0])
def test_a_percentage_that_displays_as_zero_stays_flat(pct):
    """The rule is unchanged, only its scale: what renders `+0.00%` is not a loss."""
    assert pdash._percent_sign_style(pdash._as_fraction(pct)) == f"color: {palette.FLAT_COLOR}"


def test_money_cells_keep_the_half_cent_band():
    """The money columns are untouched — a cell displaying `-0.00` is still neutral."""
    assert pdash._sign_style(-0.004) == f"color: {palette.FLAT_COLOR}"
    assert pdash._sign_style(-0.006) == f"color: {palette.DOWN_COLOR}"


def test_each_signed_column_is_styled_with_the_band_that_matches_its_format():
    """Structural: the split is what keeps the two bands from being confused again.

    Asserted over the column lists rather than over one column, so adding a signed
    column without deciding which band it takes fails here.
    """
    money = set(pdash._MONEY_SIGNED_COLUMNS)
    percent = set(pdash._PERCENT_SIGNED_COLUMNS)
    assert not money & percent, "a column cannot take both bands"
    assert all("%" in c for c in percent), "a percent band applied to a money column"
    assert not any("%" in c for c in money), "a money band applied to a percent column"


# ── The hover must not expose the chart's own plumbing ───────────────────────
#
# Found 2026-09-23 by rendering the real YTD chart and reading the Bokeh model, not by
# the suite. The per-bar colouring added that morning passes a `_bar_color` column, and
# hvplot promoted it to a hover tooltip — so hovering a bar offered the user
# `_bar_color  #ef5350`. Asserted as a CLASS (no underscore-prefixed column may reach a
# tooltip) rather than on that one name, so the next internal column cannot repeat it.


def _hover_fields(layout):
    """Every tooltip field name on every row of a rendered chart."""
    import holoviews as hv
    from bokeh.models import HoverTool

    found: list[str] = []
    for element in layout:
        fig = hv.render(element, backend="bokeh")
        for tool in fig.tools:
            # `tooltips` is a union; only the list-of-pairs form carries field names.
            if isinstance(tool, HoverTool) and isinstance(tool.tooltips, list):
                found.extend(str(label) for label, _ in tool.tooltips)
    return found


def test_no_internal_column_reaches_a_hover_tooltip():
    """A private column is an implementation detail; the user must never be shown one."""
    pts = tuple(
        dd.RealisedPoint(day=date(2026, 9, d), realised=r, cumulative=c)
        for d, r, c in [(1, 500.0, 500.0), (2, -200.0, 300.0), (3, 900.0, 1200.0)]
    )
    leaked = [f for f in _hover_fields(pdash.build_realised_chart(pts, "t")) if f.startswith("_")]
    assert not leaked, f"internal column(s) shown to the user in a tooltip: {leaked}"


# ── The curve must represent the window in its own title ─────────────────────
#
# Found live 2026-09-23 from a screenshot: the P&L tab headed "Monthly (2026-09-01 →
# 2026-09-23)" drew a red curve ending at -15,788.07 while the table beside it reported
# the month at +3,127.43. Two figures on one screen disagreeing.
#
# Cause, and it is rendering only: the poller computes ONE YTD series and
# `_selected_window` slices it by date. Slicing the days does not re-base the running
# total, so `RealisedPoint.cumulative` stayed anchored to 1 January. Measured that day:
# Weekly, Monthly and YTD all ended at -15,788.07 — every window drew the YTD endpoint,
# so the curve's colour was always the year's sign.
#
# No P&L figure changes. `dashboard_data` is untouched; the chart stops reading a
# year-anchored field for a month-long window. Verified against the live store: re-basing
# is bit-exact identical over all 131 YTD points (max delta 0.0000000000), and each
# window's re-based endpoint equals `realised_window.total` to the cent.


def _points_from(realised, start_cumulative=0.0):
    """Points carrying a YTD-anchored `cumulative`, as the poller's series really does."""
    pts, run = [], start_cumulative
    for i, r in enumerate(realised):
        run += r
        pts.append(dd.RealisedPoint(day=date(2026, 9, i + 1), realised=r, cumulative=run))
    return tuple(pts)


def _curve_values(layout):
    """The cumulative values the chart actually plots, de-stepped.

    `type(...) is hv.Curve` rather than isinstance: **hv.Area subclasses hv.Curve**, so an
    isinstance check silently selects the filled area (measured 2026-09-23). The rendered
    line holds each value across two points; the odd indices are the real observations.
    """
    import holoviews as hv

    overlay = next(iter(layout))
    curve = next(sub for sub in overlay if type(sub) is hv.Curve)
    # `line`, not `cumulative`: the row is split into an above-zero and a below-zero half
    # and each carries a NaN-masked `line` column plus a clipped `fill` one.
    drawn = [float(v) for v in curve.dimension_values("line")]
    return drawn[::2]


def test_the_curve_ends_at_this_window_s_own_total():
    """The last point of the curve is what the window made — the table's figure."""
    realised = [500.0, -200.0, 900.0]
    # Sliced out of a year that was already 19,536.96 down when this window opened.
    pts = _points_from(realised, start_cumulative=-19_536.96)
    values = _curve_values(pdash.build_realised_chart(pts, "Monthly"))
    assert values[-1] == pytest.approx(sum(realised)), (
        f"curve ends at {values[-1]:,.2f} but the window realised {sum(realised):,.2f}"
    )


def test_a_sub_window_starts_from_its_own_first_day():
    """A window opens at its first day's result, not at the year's running total."""
    pts = _points_from([500.0, -200.0, 900.0], start_cumulative=-19_536.96)
    values = _curve_values(pdash.build_realised_chart(pts, "Monthly"))
    assert values[0] == pytest.approx(500.0), f"curve opens at {values[0]:,.2f}, not the day's 500"


def test_a_profitable_month_inside_a_losing_year_draws_green():
    """The exact defect seen on screen: a month up 3,127 drew red because the year was down."""
    pts = _points_from([500.0, -200.0, 900.0], start_cumulative=-19_536.96)
    _area, curve = _chart_colors(pdash.build_realised_chart(pts, "Monthly"))
    assert curve == palette.UP_COLOR, "a winning window took the losing year's colour"


def test_a_full_series_is_unchanged_by_rebasing():
    """Back-compat: for YTD the window IS the series, so nothing may move."""
    realised = [500.0, -200.0, 900.0, -1_400.0]
    pts = _points_from(realised, start_cumulative=0.0)
    # Asserted on the frame, which is where re-basing happens — the rendered line is
    # stepped, so its point count is a rendering concern, not this invariant's.
    frame = pdash.realised_frame(pts)
    assert list(frame["cumulative"]) == pytest.approx([p.cumulative for p in pts])


# ── The daily row must be readable, not a strip under the curve ──────────────
#
# User, 2026-09-23, looking at the live pane: "I like the second bar graph to have the
# ~same height to be more readable and scale less busy (seems too dense and not very
# readable)." It was drawn at exactly half the cumulative row's height with the default
# tick density, so a -21.31 day and a +2,922.96 day shared a 130px axis carrying seven
# labels.


def test_the_daily_row_gets_comparable_height_to_the_cumulative_row():
    """The bars carry the per-day detail; a strip cannot show it."""
    assert pdash._BAR_ROW_HEIGHT >= 0.8 * pdash._CHART_HEIGHT, (
        f"daily row {pdash._BAR_ROW_HEIGHT}px against cumulative {pdash._CHART_HEIGHT}px "
        "— too short to read"
    )


def test_both_rows_cap_their_y_tick_count():
    """A bounded tick count is what stops the axis crowding as the range grows."""
    pts = _points_from([500.0, -200.0, 900.0, -1_400.0, 2_922.96])
    layout = pdash.build_realised_chart(pts, "t")
    for element in layout:
        ticks = element.opts.get("plot").kwargs.get("yticks")
        assert isinstance(ticks, int) and ticks <= 6, (
            f"yticks={ticks!r} on {type(element).__name__}"
        )


# ── The cumulative line must not invent observations between trading days ────
#
# `realised_series`' docstring already states the intent: "Only days that traded appear,
# so the cumulative line steps between them. That is the honest shape: nothing was
# realised in between, and interpolating a smooth slope across non-trading days would
# imply observations no statement covers."
#
# The rendering did not do that. Seen live 2026-09-23: Sep 15 (+5,196 cumulative) to
# Sep 21 (+3,149) was drawn as a gradual six-day decline. Nothing happened on Sep 16-20;
# the whole loss was taken on the 21st.
#
# Stepping duplicates each point, which is why the cumulative row carries no tooltip: a
# duplicate sitting at (Sep 08, 1000) would report Sep 8's cumulative as the value it had
# BEFORE that day traded. The daily bars below already hover correctly, one point per day.


def _rendered(element, column):
    """The values Bokeh will actually draw for `column`."""
    import holoviews as hv

    fig = hv.render(element, backend="bokeh")
    for r in fig.renderers:
        src = getattr(r, "data_source", None)
        if src is not None and column in src.data:
            return [float(v) for v in src.data[column]]
    return None


def test_the_cumulative_line_holds_its_value_between_trading_days():
    """A flat run then a drop — not a slope implying five days of bleeding."""
    import holoviews as hv

    pts = _points_from([1000.0, 2000.0, -3000.0])
    # `type(...) is hv.Curve`, not isinstance: **hv.Area SUBCLASSES hv.Curve**, so an
    # isinstance check silently selects the filled area instead (measured 2026-09-23).
    curve = next(e for e in next(iter(pdash.build_realised_chart(pts, "t"))) if type(e) is hv.Curve)
    drawn = [float(v) for v in curve.dimension_values("line")]
    # Six values for three days: each is held until the next, and the last is held for its
    # own day too — see `_step_frame` on why the trailing point exists.
    assert drawn == pytest.approx([1000.0, 1000.0, 3000.0, 3000.0, 0.0, 0.0]), (
        f"drawn as {drawn} — a straight run between trading days invents observations"
    )


def test_the_filled_area_steps_with_the_line_it_sits_under():
    """Area rejects `interpolation` outright, so a linear fill under a stepped line
    would cut across every corner. Both are built from the same stepped frame."""
    import holoviews as hv

    pts = _points_from([1000.0, 2000.0, -3000.0])
    overlay = next(iter(pdash.build_realised_chart(pts, "t")))
    area = next(e for e in overlay if type(e) is hv.Area)
    curve = next(e for e in overlay if type(e) is hv.Curve)
    assert len(area.dimension_values(0)) == len(curve.dimension_values(0)), (
        "the fill and the line are drawn at different resolutions"
    )


def test_the_cumulative_row_carries_no_tooltip():
    """A stepped line's duplicated points cannot be allowed to report a day's value."""
    import holoviews as hv
    from bokeh.models import HoverTool

    pts = _points_from([1000.0, 2000.0, -3000.0])
    overlay = next(iter(pdash.build_realised_chart(pts, "t")))
    fig = hv.render(overlay, backend="bokeh")
    assert not [t for t in fig.tools if isinstance(t, HoverTool)], (
        "the stepped cumulative row must not offer a per-day tooltip"
    )


# ── Break-even needs a line, not just a tick ─────────────────────────────────
#
# Measured off the rendered Bokeh model 2026-09-23: neither row carried a `Span`. On the
# monthly window the curve crossed zero around Sep 5 and nothing on the plot marked where
# that was — the reader had to trace across to the axis. Break-even is the one reference a
# P&L chart cannot do without, and it matters on both rows: the cumulative crosses it, and
# it is the sign boundary the daily bars are coloured by.


def _zero_spans(element):
    """Horizontal `Span`s at y=0 on a rendered element.

    `fig.select` rather than `fig.center`: in Bokeh 3.9.2 an `HLine`'s `Span` is filed
    under `fig.renderers`, not `fig.center` (measured 2026-09-23). `select` searches the
    whole model, so this survives Bokeh moving it again.
    """
    import holoviews as hv

    # `bokeh.models.annotations`, not `bokeh.models`: the re-export is untyped, so mypy
    # rejects `from bokeh.models import Span` while runtime accepts it. The class really
    # lives at `bokeh.models.annotations.geometry.Span` (measured, Bokeh 3.9.2).
    from bokeh.models.annotations import Span

    fig = hv.render(element, backend="bokeh")
    return [s for s in fig.select({"type": Span}) if s.dimension == "width" and s.location == 0]


def test_both_rows_mark_break_even():
    """Zero is where profit becomes loss; it must be visible on the plot itself."""
    pts = _points_from([1000.0, -2000.0, 900.0])
    for element in pdash.build_realised_chart(pts, "t"):
        assert _zero_spans(element), f"no break-even line on the {type(element).__name__} row"


def test_the_break_even_line_does_not_compete_with_the_data():
    """A reference line, not a series: thin, dashed, and the palette's neutral.

    `FLAT_COLOR` specifically — break-even is neither profit nor loss, so the colour the
    palette already uses for "inside the dead band" is the one that means it. Using green
    or red here would assert a direction that zero does not have.
    """
    pts = _points_from([1000.0, -2000.0, 900.0])
    span = _zero_spans(next(iter(pdash.build_realised_chart(pts, "t"))))[0]
    assert span.line_width <= 1.5, f"line_width={span.line_width} reads as a data series"
    assert span.line_dash, "a solid rule competes with the curve"
    assert span.line_color == palette.FLAT_COLOR, f"line_color={span.line_color}"


def test_the_final_step_is_as_wide_as_every_other():
    """The window's total must not be a hairline at the right edge.

    Seen in a screenshot 2026-09-23, immediately after the step fix shipped. `steps-post`
    holds each value until the NEXT observation, so with N points only N-1 of them get any
    width — and the one left out is the last, which is the window total the table above the
    chart is reporting. Every other day occupies its own span; the final day must too.
    """
    pts = _points_from([1000.0, 2000.0, -3000.0])
    step = pdash._step_frame(pdash.realised_frame(pts))
    days, values = list(step["day"]), list(step["cumulative"])
    assert values[-1] == pytest.approx(0.0), "the last value must still be the window total"
    assert days[-1] > days[-2], (
        f"the final step spans {days[-2].date()} -> {days[-1].date()} — no width"
    )


# ── Axis polish: money reads as money, and the axis is not labelled twice ────
#
# Measured off the rendered Bokeh model 2026-09-23: both rows carried a
# `BasicTickFormatter` with `format=None`, so a window totalling 6,050.39 showed an axis
# reading "6000" — while every other figure in this app goes through `fmt_signed` with an
# ISO code. And both rows carried the x-axis label "day" beneath date ticks that already
# say so, costing two lines of vertical space the bars could use.


def _y_formatters(layout):
    """The y-axis tick formatter on each row of a rendered chart."""
    import holoviews as hv

    return [hv.render(e, backend="bokeh").yaxis[0].formatter for e in layout]


def test_money_axes_are_formatted_as_money():
    """`6,000` not `6000` — the thousands separator is what makes a P&L figure scannable."""
    from bokeh.models.formatters import NumeralTickFormatter

    pts = _points_from([1000.0, -2000.0, 900.0])
    for fmt in _y_formatters(pdash.build_realised_chart(pts, "t")):
        assert isinstance(fmt, NumeralTickFormatter), f"{type(fmt).__name__} leaves raw numbers"
        assert "," in fmt.format, f"format={fmt.format!r} has no thousands separator"


def test_the_x_axis_is_not_labelled_day_under_both_rows():
    """The ticks are dates; the word adds nothing and is printed twice."""
    import holoviews as hv

    pts = _points_from([1000.0, -2000.0, 900.0])
    labels = [
        hv.render(e, backend="bokeh").xaxis[0].axis_label
        for e in pdash.build_realised_chart(pts, "t")
    ]
    assert not any(labels), f"x-axis labelled {labels}"


# ── Above zero is green, below zero is red — on the same curve ───────────────
#
# User, 2026-09-23, on the live Weekly pane: "the negative part is still green, pls check."
# The window sat at -2,050 for two days and only ended at +855, so colouring the curve by
# where the window ENDS painted the underwater stretch green. This is the zero-crossing
# question raised and deferred that morning ("we will review more in detail afterwards");
# the step shape shipped since is what makes it cheap — a stepped line only ever crosses
# zero on a VERTICAL segment, so the crossing needs no interpolation, just a point at y=0.


def _cumulative_colours(layout):
    """Every colour used by the cumulative row's areas and lines."""
    import holoviews as hv

    return {
        sub.opts.get("style").kwargs.get("color")
        for sub in next(iter(layout))
        if type(sub) in (hv.Area, hv.Curve)
    }


def test_a_window_that_ends_up_after_being_under_water_shows_both():
    """The Weekly case: two days at -2,050, ending +855. Both facts must be visible."""
    pts = _points_from([-2046.46, -21.31, 2922.96])
    assert _cumulative_colours(pdash.build_realised_chart(pts, "Weekly")) == {
        palette.UP_COLOR,
        palette.DOWN_COLOR,
    }


def test_a_wholly_losing_window_shows_no_green():
    """No spurious element: a window that never went positive draws only red."""
    pts = _points_from([-500.0, -200.0, -900.0])
    assert _cumulative_colours(pdash.build_realised_chart(pts, "t")) == {palette.DOWN_COLOR}


def test_a_wholly_winning_window_shows_no_red():
    """The mirror: a window that never went under draws only green."""
    pts = _points_from([500.0, 200.0, 900.0])
    assert _cumulative_colours(pdash.build_realised_chart(pts, "t")) == {palette.UP_COLOR}


def test_the_filled_area_contains_no_nan():
    """A `Patch` is ONE closed polygon, so a NaN inside it tears the fill open.

    Seen live 2026-09-23: the monthly pane showed a hole between Sep 8 and Sep 13 where
    the cumulative sat at +1,929 the whole time, plus stray diagonals that were the torn
    polygon's edges. The frames were correct; only the fill's rendering was not.

    The line may and must carry NaN — that is how it breaks at the crossing instead of
    being drawn flat along zero. The area instead CLIPS: filling 0..max(y, 0) covers
    exactly the above-zero region and needs no gap at all.
    """
    import holoviews as hv

    pts = _points_from([-2000.0, 3000.0, -4000.0, 5000.0])
    for element in next(iter(pdash.build_realised_chart(pts, "t"))):
        if type(element) is not hv.Area:
            continue
        fig = hv.render(element, backend="bokeh")
        for r in fig.renderers:
            src = getattr(r, "data_source", None)
            if src is None or "y" not in src.data:
                continue
            ys = [float(v) for v in src.data["y"]]
            assert not any(v != v for v in ys), "NaN inside the fill polygon tears it open"


def test_the_trailing_step_matches_the_series_own_spacing():
    """A weekly point holds for a week, not for a day.

    `_step_frame` appends one trailing point so the final value gets the same width as
    every other. At daily resolution that is a day; on the YTD view, whose points are
    weekly, a one-day tail would draw the year's closing figure as a sliver. Taken from
    the series' own **minimum** spacing — the same idiom hvplot uses to size bars.
    """
    from datetime import date as _date

    weekly = tuple(
        dd.RealisedPoint(day=_date(2026, 9, d), realised=100.0, cumulative=100.0 * i)
        for i, d in enumerate((7, 14, 21), start=1)
    )
    step = pdash._step_frame(pdash.realised_frame(weekly))
    days = list(step["day"])
    assert (days[-1] - days[-2]).days == 7, (
        f"trailing step is {(days[-1] - days[-2]).days}d on a weekly series"
    )


def test_the_ytd_view_shows_weekly_points_on_both_rows():
    """One resolution per view: the line and the histogram must agree.

    User, 2026-09-23: "the logic answer is to be consistent, so in yearly view, data
    points are weekly on both sides, line and histogram."
    """
    import holoviews as hv

    # Eight consecutive trading days spanning three ISO weeks, so daily and weekly
    # resolutions are unmistakably different. Built here rather than taken from the shared
    # fixture, whose series is too short to draw a YTD chart at all — a skipped test would
    # have asserted nothing.
    # July dates: the fixture's YTD window runs 2026-01-01 to _TODAY (2026-08-06), and
    # `_selected_window` filters to it, so points outside draw nothing at all.
    daily = tuple(
        dd.RealisedPoint(date(2026, 7, d), 100.0, 100.0 * i)
        for i, d in enumerate((6, 7, 8, 13, 14, 15, 20, 21), start=1)
    )
    view = pdash.build_dashboard()
    view.refresh(_snapshot(series=daily), now=_NOW)
    view._window.value = "YTD"
    obj = view._pnl_chart.object
    assert obj is not None, "the YTD chart did not render"

    for element in obj:
        fig = hv.render(element, backend="bokeh")
        for r in fig.renderers:
            src = getattr(r, "data_source", None)
            if src is None or "day" not in src.data:
                continue
            days = sorted({pd.Timestamp(v).date() for v in src.data["day"]})
            # Every point but the trailing one opens a week; the tail is a bucket past it.
            assert all(d.weekday() == 0 for d in days[:-1]), (
                f"YTD points are not weekly: {days[:6]}"
            )
            assert len(days) <= 4, f"YTD still drawing daily points: {len(days)}"


def test_the_single_point_note_names_the_right_unit():
    """A YTD window holding one week must not report "one trading day".

    Exposed by the weekly regrouping on 2026-09-23: the note was written when every view
    was daily. A year-to-date pane in early January legitimately holds one bucket, and
    calling that bucket a day would misstate how much trading it covers.
    """
    one = (dd.RealisedPoint(date(2026, 1, 5), 250.0, 250.0),)
    assert "trading day" in pdash.realised_chart_note(one, "USD")
    assert "trading week" in pdash.realised_chart_note(one, "USD", unit="week")


def test_the_step_frame_survives_hvplot_unreordered():
    """hvPlot must not re-sort a stepped frame — the duplicates ARE the verticals.

    Found 2026-09-23 from a screenshot: the monthly curve still drew diagonals after the
    step, split and clip fixes were all in, and the frames handed to hvPlot were correct
    every time. `hvPlot.area`/`.line` default to `sort_date=True`, which sorts by the
    datetime x; a stepped series has TWO rows per day, and the tie order between them is
    arbitrary under that sort. Reordering them turns a vertical segment into a diagonal.

    Asserted against the frame that was passed in, so the guard holds for any window rather
    than for one shape. It only bit at length, which is why the short probes all passed —
    hence a realistic series here.
    """
    pts = _points_from([-621.46, 654.38, -904.48, 290.10, 2510.62, 3266.04, -2046.46])
    step = pdash._step_frame(pdash.realised_frame(pts))
    above, _below = pdash._split_at_zero(step)

    import holoviews as hv

    for element in next(iter(pdash.build_realised_chart(pts, "t"))):
        if (
            type(element) is not hv.Area
            or element.opts.get("style").kwargs.get("color") != palette.UP_COLOR
        ):
            continue
        drawn = [round(float(v), 2) for v in element.dframe()["fill"]]
        assert drawn == [round(float(v), 2) for v in above["fill"]], (
            "hvplot reordered the stepped frame — verticals become diagonals"
        )
