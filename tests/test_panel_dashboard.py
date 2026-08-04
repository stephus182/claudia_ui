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

from datetime import UTC, date, datetime, timedelta

import panel as pn
import pytest

# Tabulator needs its extension loaded before the widget can render. Importing the module
# under test does not do it — panel_app owns the single pn.extension call — so the suite
# loads it here, matching what a served session provides.
pn.extension("tabulator")

from claudia import dashboard_data as dd  # noqa: E402
from claudia import panel_dashboard as pdash  # noqa: E402
from claudia.dashboard_poller import STALE_AFTER  # noqa: E402

_NOW = datetime(2026, 8, 6, 15, 30, tzinfo=UTC)
_TODAY = date(2026, 8, 6)


def _window(name, start, end, total, count, by_asset=None, currencies=("USD",)):
    """A `RealisedWindow` with sensible defaults for the fields a test does not pin."""
    return dd.RealisedWindow(
        name=name, start=start, end=end, total=total, trade_count=count,
        by_asset=by_asset or {"FUT": total}, currencies=currencies,
    )


def _stats(lots=4, wins=2, losses=2, scratches=0, gross_win=1322.0, gross_loss=-4329.38):
    """A `RoundTripStats` with defaults matching the data-layer fixture."""
    return dd.RoundTripStats(
        start=date(2026, 8, 3), end=_TODAY, closed_lots=lots, winners=wins,
        losers=losses, scratches=scratches, gross_win=gross_win, gross_loss=gross_loss,
    )


def _ledger(**over):
    """A `LedgerSnapshot` in USD with the fields the KPI strip reads."""
    fields = {
        "currency": "USD", "net_liquidation": 100000.0, "cash": 25000.0,
        "settled_cash": 24000.0, "stock_market_value": 60000.0,
        "futures_market_value": 0.0, "unrealised_pnl": -3638.52,
        "realised_pnl": 412.10, "futures_only_pnl": -3516.98,
    }
    fields.update(over)
    return dd.LedgerSnapshot(**fields)


def _positions(*specs):
    """Positions from `(symbol, qty, unrealised)` triples, or one default position."""
    specs = specs or (("ESU6", 1.0, -1000.0),)
    return dd.parse_positions([
        {"conid": i, "ticker": sym, "contractDesc": sym, "assetClass": "FUT",
         "position": qty, "avgCost": 6500.0, "mktPrice": 6480.0, "mktValue": 324000.0,
         "unrealizedPnl": upl, "realizedPnl": 0.0, "currency": "USD"}
        for i, (sym, qty, upl) in enumerate(specs)
    ])


def _snapshot(**over):
    """A fully-populated snapshot; override any field per test."""
    fields = {
        "as_of": _NOW,
        "ledger": _ledger(),
        "positions": _positions(),
        "week": _window("week", date(2026, 8, 3), _TODAY, -2194.98, 5,
                        {"FUT": -3516.98, "STK": 1322.0}),
        "month": _window("month", date(2026, 8, 1), _TODAY, -2294.98, 6),
        "ytd": _window("ytd", date(2026, 1, 1), _TODAY, -4006.18, 8,
                       {"FUT": -3616.98, "STK": 1122.0, "OPT": -1511.20},
                       currencies=("EUR", "USD")),
        "stats": {"week": _stats(), "month": _stats(lots=5), "ytd": _stats(lots=42, wins=20, losses=22)},
        "series": tuple(
            dd.RealisedPoint(date(2026, 8, d), v, c)
            for d, v, c in [(3, -3516.98, -3516.98), (4, 1071.75, -2445.23),
                            (5, 0.0, -2445.23), (6, 250.25, -2194.98)]
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

    for forbidden in ("claudia.order_flow", "claudia.panel_order_flow", "ibkr_core_mcp",
                      "sqlite3", "requests"):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported), (
            f"panel_dashboard imports {forbidden}"
        )
    for forbidden in ("place_order", "modify_order", "cancel_order", "reply_order",
                      "execute", "get_positions", "get_account_ledger"):
        assert forbidden not in identifiers, f"panel_dashboard calls {forbidden}"


# ── 2. Honesty guards ─────────────────────────────────────────────────────────


def test_fresh_data_reads_as_live(view):
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
        "establish a new connection: [Errno 61] Connection refused\"))"
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
    assert pdash.short_reason(raw) == expected


def test_never_polled_says_so_rather_than_showing_a_flat_account():
    snap = dd.empty_snapshot(now=_NOW, error="Dashboard has not polled yet.")
    assert "waiting for the first poll" in pdash.freshness_line(snap, _NOW)


def test_coverage_line_states_the_t_plus_one_gap(view):
    """The ledger figure includes today; the Flex windows cannot. Say so on the surface."""
    line = view._pnl_coverage.object
    assert "2026-08-05" in line
    assert "never includes today" in line
    assert "9 fill(s) today not yet in a statement" in line
    assert "The ledger figure above does include today" in line


def test_coverage_line_without_pending_fills_omits_the_warning():
    snap = _snapshot(coverage=dd.FlexCoverage(through=date(2026, 8, 5), live_pending=0))
    assert "not yet in a statement" not in pdash.coverage_line(snap)


def test_coverage_line_with_no_flex_data():
    assert "no Flex data" in pdash.coverage_line(_snapshot(coverage=None))


def test_stats_block_labels_the_two_bases_apart(view):
    """Execution-basis P&L and lot-basis counts must never be readable as one figure."""
    view._window.value = "Week"
    text = view._pnl_stats.object
    assert "Realised P&L (executions)" in text
    assert "Closed round trips (lots)" in text
    assert "pre-wash-sale and must never be read as realised P&L" in text


def test_each_window_shows_its_own_round_trip_counts(view):
    """A count cannot be sliced from a wider window — the Phase 3 smoke defect.

    Selecting YTD once rendered the *week's* "1 closed round trip" under a YTD heading
    beside a 636-execution total. Every window now carries its own stats.
    """
    view._window.value = "Week"
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
    assert text.index("futuresonlypnl") < text.index(pdash.realised_ledger_label())


def test_realised_ledger_tile_uses_the_unverified_label(view):
    """Until measured live, the tile must not claim the figure is same-day."""
    assert view._tiles["realised_ledger"].label == "Realised (ledger)"
    assert dd.REALISED_LEDGER_WINDOW == "unverified"


def test_every_money_string_carries_an_iso_code_and_no_bare_dollar(view):
    """`$` is shared by USD/MXN/CAD/AUD/HKD/SGD — a wrong-currency price looks ordinary."""
    view._window.value = "Week"
    rendered = "\n".join([
        view._freshness.object, view._positions_status.object,
        view._pnl_stats.object, view._pnl_coverage.object, view._ledger_detail.object,
    ])
    assert "$" not in rendered
    assert "USD" in rendered
    for tile in view._tiles.values():
        assert "$" not in tile.format


def test_a_mixed_currency_window_is_labelled_mixed_not_usd(view):
    """The YTD fixture spans EUR and USD; its total must not be stamped USD."""
    view._window.value = "YTD"
    assert "mixed" in view._pnl_stats.object
    assert "mixed" in str(view._pnl_chart.object.keys()) or True  # title checked below
    assert "mixed" in view._pnl_chart.object.Overlay.I.opts.get("plot").kwargs["title"]


def test_signed_figures_always_show_their_sign():
    assert pdash.fmt_signed(250.25, "USD") == "+250.25 USD"
    assert pdash.fmt_signed(-3516.98, "USD") == "-3,516.98 USD"
    assert pdash.fmt_signed(0.0, "USD") == "+0.00 USD"
    assert pdash.fmt_signed(None, "USD") == "—"
    assert pdash.fmt_money(100000.0, "EUR") == "100,000.00 EUR"
    assert pdash.fmt_money(None, "EUR") == "—"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (3.4, "3s"), (59.9, "60s"), (60, "1m 00s"), (134, "2m 14s"),
     (3600, "1h 00m"), (4500, "1h 15m"), (-5, "0s")],
)
def test_age_formatting(seconds, expected):
    assert pdash.fmt_age(seconds) == expected


# ── 3. Rendering mechanics ────────────────────────────────────────────────────


def test_tabs_are_named_chart_positions_pnl():
    v = pdash.build_dashboard(chart_pane=pn.Column(pn.pane.Markdown("chart")))
    assert list(v.tabs._names) == ["Chart", "Positions", "P&L"]
    assert isinstance(v.tabs, pn.Tabs)


def test_kpi_strip_holds_six_number_tiles(view):
    tiles = [o for o in view.kpi_strip[0] if isinstance(o, pn.indicators.Number)]
    assert len(tiles) == 6
    assert [t.label for t in tiles] == [
        "Net liquidation", "Cash", "Unrealised P&L", "Realised (ledger)",
        "Realised this week", "Win rate this week",
    ]


def test_tiles_carry_the_live_currency_in_their_format(view):
    assert view._tiles["net_liq"].value == 100000.0
    assert view._tiles["net_liq"].format == "{value:,.2f} USD"
    assert view._tiles["unrealised"].format == "{value:+,.2f} USD"
    assert view._tiles["realised_week"].value == pytest.approx(-2194.98)


def test_tiles_are_empty_and_neutral_before_the_first_poll():
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


def test_win_rate_tile_is_blank_when_nothing_closed():
    v = pdash.build_dashboard()
    v.refresh(_snapshot(stats={"week": _stats(lots=0, wins=0, losses=0)}), now=_NOW)
    assert v._tiles["win_rate"].value is None


def test_pnl_colors_are_neutral_at_exactly_zero():
    """`value <= threshold` would paint a flat P&L red without the dead band."""
    tile = pdash.build_dashboard()._tiles["unrealised"]
    thresholds = dict(tile.colors)
    assert thresholds[-0.005] == pdash._DOWN_COLOR
    assert thresholds[0.005] == pdash._FLAT_COLOR
    assert thresholds[float("inf")] == pdash._UP_COLOR


def test_numeric_columns_are_formatted_and_right_aligned(view):
    """IBKR returns full float precision — `383.270899` is noise on a trading surface.

    Formatting is a Tabulator display concern, never a change to the frame: the values
    stay real floats so the column sorts numerically and `.style` still sees a number.
    """
    fmts = view._positions.formatters
    assert fmts["Market value"].format == pdash._MONEY_FORMAT
    assert fmts["Unrealised"].format == pdash._MONEY_FORMAT
    assert fmts["Avg cost"].format == pdash._PRICE_FORMAT
    assert view._positions.text_align["Unrealised"] == "right"
    # The underlying frame is untouched — display precision must not become data.
    assert view._positions.value["Unrealised"].dtype.kind == "f"


def test_positions_frame_has_headers_even_with_no_rows():
    """A zero-column frame renders as a blank rectangle — that reads as a broken widget."""
    frame = pdash.positions_frame(dd.empty_snapshot(now=_NOW))
    assert list(frame.columns) == pdash._POSITION_COLUMNS
    assert len(frame) == 0


def test_positions_table_is_populated_and_summarised(view):
    assert list(view._positions.value["Symbol"]) == ["ESU6"]
    assert "1 open position(s)" in view._positions_status.object
    assert "-1,000.00 USD" in view._positions_status.object


def test_positions_summary_distinguishes_empty_from_unavailable():
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=()), now=_NOW)
    assert "No open positions" in v._positions_status.object

    v.refresh(_snapshot(positions=(), ledger=None), now=_NOW)
    assert "IBKR not connected" in v._positions_status.object


def test_multi_currency_positions_are_labelled_mixed():
    rows = dd.parse_positions([
        {"ticker": "AAPL", "position": 1.0, "unrealizedPnl": 10.0, "currency": "USD"},
        {"ticker": "SAP", "position": 1.0, "unrealizedPnl": -4.0, "currency": "EUR"},
    ])
    v = pdash.build_dashboard()
    v.refresh(_snapshot(positions=rows), now=_NOW)
    assert "EUR, USD" in v._positions_status.object
    assert "mixed" in v._positions_status.object


def test_pnl_colouring_is_bound_to_the_unrealised_column(view):
    """`.style.map` gives the P&L cells their red/green without any CSS."""
    view._positions.style._compute()
    ctx = view._positions.style.ctx
    assert any(("color", pdash._DOWN_COLOR) in styles for styles in ctx.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5.0, f"color: {pdash._UP_COLOR}"), (-5.0, f"color: {pdash._DOWN_COLOR}"),
     (0.0, f"color: {pdash._FLAT_COLOR}"), ("text", ""), (True, "")],
)
def test_sign_style(value, expected):
    assert pdash._sign_style(value) == expected


def test_chart_is_built_for_the_selected_window(view):
    import holoviews as hv

    view._window.value = "Week"
    week_obj = view._pnl_chart.object
    assert isinstance(week_obj, hv.Layout)
    view._window.value = "YTD"
    assert view._pnl_chart.object is not week_obj


def test_switching_the_window_repaints_without_a_new_poll(view):
    """A radio click must feel immediate — the data is already in memory."""
    view._window.value = "Month"
    assert "Month" in view._pnl_stats.object
    assert "| Closed round trips (lots) | 5 |" in view._pnl_stats.object


def test_window_selector_before_any_snapshot_does_not_raise():
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
            week=_window("week", date(2026, 8, 3), date(2026, 8, 4), -3516.98, 4),
        ),
        now=_NOW,
    )
    v._window.value = "Week"
    assert v._pnl_chart.object is None
    assert "Only one trading day" in v._pnl_chart_note.object
    # The stats table must still render — only the curve is suppressed.
    assert "Realised P&L (executions)" in v._pnl_stats.object


def test_the_chart_note_is_empty_when_a_chart_was_drawn(view):
    view._window.value = "Week"
    assert view._pnl_chart.object is not None
    assert view._pnl_chart_note.object == ""


def test_the_chart_note_reports_an_empty_window():
    assert "No realised P&L" in pdash.realised_chart_note((), "USD")


def test_chart_layout_has_two_stacked_rows():
    import holoviews as hv

    pts = tuple(
        dd.RealisedPoint(date(2026, 8, d), 1.0 * d, 1.0 * d) for d in (3, 4, 5, 6)
    )
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
        view.refresh(_Exploding())  # type: ignore[arg-type]
    assert view._tiles["net_liq"].value == before
    assert "repaint failed" in caplog.text


def test_stats_markdown_handles_a_window_with_no_lots():
    text = pdash.stats_markdown(
        _window("week", date(2026, 8, 3), _TODAY, 0.0, 0, {}),
        _stats(lots=0, wins=0, losses=0, gross_win=0.0, gross_loss=0.0),
        "Week",
    )
    assert "Closed round trips (lots) | 0" in text
    assert "win rate" not in text


def test_stats_markdown_with_no_data():
    assert "No trade data" in pdash.stats_markdown(None, None, "Week")


def test_ledger_markdown_with_no_ledger():
    assert "unavailable" in pdash.ledger_markdown(_snapshot(ledger=None))


def test_ledger_markdown_discloses_other_currency_balances():
    snap = _snapshot(ledger=_ledger(other_currencies=("CHF", "EUR")))
    assert "CHF, EUR" in pdash.ledger_markdown(snap)
