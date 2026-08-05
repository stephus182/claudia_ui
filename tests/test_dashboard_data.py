"""Tests for claudia/dashboard_data.py — the dashboard's pure data layer.

The realised-P&L fixture is the point of this module. It contains, deliberately:

  * a wash-sale-zeroed close (realises exactly 0.00),
  * an *opening* leg that realises (open_close_indicator='O' with a non-zero figure),
  * a live Client Portal row with no trade_date at all,
  * a non-USD trade,

because each of those is a way the plan's verified rule can be broken by a plausible
"improvement": adding an open/close filter, summing flex_lot instead, bucketing live
rows, or stamping USD on a mixed window. Every one of those mistakes was made for real
during the 2026-08-04 dataset rebuild.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from claudia import dashboard_data as dd

# ── Fixture database ──────────────────────────────────────────────────────────

_TODAY = date(2026, 8, 6)  # a Thursday; its week starts Monday 2026-08-03

# (trade_date_iso, source, asset_category, currency, open_close, fifo_pnl_realized)
_TRADES = [
    # --- this week (Mon 2026-08-03 onwards) ---
    ("2026-08-03", "flex", "FUT", "USD", "C", -3516.98),
    ("2026-08-03", "flex", "FUT", "USD", "O", 0.0),
    # An OPENING leg that realises: a buy closing a short and opening a long. Filtering
    # on open_close_indicator would drop this and understate the week by 1071.75.
    ("2026-08-04", "flex", "STK", "USD", "O", 1071.75),
    # A wash-sale-zeroed close: a real closing trade whose entire loss is disallowed.
    ("2026-08-05", "flex", "STK", "USD", "C", 0.0),
    ("2026-08-06", "flex", "STK", "USD", "C", 250.25),
    # --- earlier this month, before this week ---
    ("2026-08-01", "flex", "FUT", "USD", "C", -100.00),
    # --- earlier this year, before this month ---
    ("2026-03-10", "flex", "OPT", "USD", "C", -1511.20),
    ("2026-02-02", "flex", "STK", "EUR", "C", -200.00),
    # --- last year: must never reach a YTD window ---
    ("2025-12-31", "flex", "FUT", "USD", "C", 9999.99),
    # --- a live row: no trade_date, no realised figure ---
    (None, "live", "FUT", "USD", None, None),
]

# (trade_date YYYYMMDD, fifo_pnl_realized)
_LOTS = [
    ("20260803", -3516.98),
    ("20260804", 1071.75),
    ("20260805", -812.40),  # the pre-wash-sale detail behind the 0.00 trade above
    ("20260806", 250.25),
    ("20260801", -100.00),
    ("20251231", 9999.99),
]


@pytest.fixture
def store(tmp_path):
    """A miniature ibkr_core_mcp store carrying the traps described in the module docstring.

    Only the columns the dashboard queries are created — the real tables have ~90 each,
    and reproducing them would test the schema generator rather than these functions.
    Returns a read-only connection opened through the production `connect` helper, so
    the read-only-URI path is exercised by every test rather than being assumed.
    """
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, trade_date TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, open_close_indicator TEXT,"
            " fifo_pnl_realized REAL)"
        )
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (iso, iso.replace("-", "") if iso else None, src, cat, ccy, oc, pnl)
                for iso, src, cat, ccy, oc, pnl in _TRADES
            ],
        )
        w.executemany("INSERT INTO flex_lot VALUES (?, ?)", _LOTS)
    conn = dd.connect(path)
    yield conn
    conn.close()


# ── Date windows ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 8, 6), date(2026, 8, 3)),  # Thursday -> Monday
        (date(2026, 8, 3), date(2026, 8, 3)),  # Monday -> itself
        (date(2026, 8, 9), date(2026, 8, 3)),  # Sunday -> the Monday that started it
    ],
)
def test_week_start_is_monday(today, expected):
    assert dd.week_start(today) == expected


def test_month_and_year_start():
    assert dd.month_start(date(2026, 8, 6)) == date(2026, 8, 1)
    assert dd.year_start(date(2026, 8, 6)) == date(2026, 1, 1)


# ── Realised windows: the verified rule ───────────────────────────────────────


def test_week_total_matches_a_plain_unfiltered_sum(store):
    """The week total is the plain sum over flex rows — no open/close filter.

    -3516.98 + 0.00 + 1071.75 + 0.00 + 250.25. Pinned against a sum computed in SQL
    from the same table rather than against a hand-typed constant, so the assertion
    still means something if the fixture changes.
    """
    win = dd.realised_window(store, date(2026, 8, 3), _TODAY)
    expected = store.execute(
        "SELECT SUM(fifo_pnl_realized) FROM flex_trade WHERE source='flex'"
        " AND trade_date_iso BETWEEN '2026-08-03' AND '2026-08-06'"
    ).fetchone()[0]
    assert win.total == pytest.approx(expected)
    assert win.total == pytest.approx(-2194.98)


def test_opening_leg_that_realises_is_counted(store):
    """An open_close_indicator='O' row with a non-zero figure must be in the total.

    This is trap #1 from the rule: a buy that closes a short and opens a long is flagged
    'O' and still realises. Asserted by comparing against the same window computed with
    the wrong filter — the two must differ by exactly that row.
    """
    win = dd.realised_window(store, date(2026, 8, 3), _TODAY)
    with_filter = store.execute(
        "SELECT SUM(fifo_pnl_realized) FROM flex_trade WHERE source='flex'"
        " AND open_close_indicator LIKE '%C%'"
        " AND trade_date_iso BETWEEN '2026-08-03' AND '2026-08-06'"
    ).fetchone()[0]
    assert win.total - with_filter == pytest.approx(1071.75)


def test_wash_sale_zeroed_close_contributes_zero_not_the_lot_loss(store):
    """A 0.00 close stays 0.00 — flex_lot's -812.40 must not leak into the total.

    Trap #2: flex_lot is pre-wash-sale detail, Trade == Lot + WashSale. Summing lots
    for the same week gives a materially worse number, and the test states that gap
    explicitly so nobody "fixes" the window query by pointing it at flex_lot.
    """
    win = dd.realised_window(store, date(2026, 8, 3), _TODAY)
    lot_sum = store.execute(
        "SELECT SUM(fifo_pnl_realized) FROM flex_lot"
        " WHERE trade_date BETWEEN '20260803' AND '20260806'"
    ).fetchone()[0]
    assert lot_sum == pytest.approx(-3007.38)
    assert win.total != pytest.approx(lot_sum)


def test_live_rows_are_excluded_entirely(store):
    """The live row has no trade_date and must not be counted, in any window."""
    ytd = dd.realised_window(store, date(2026, 1, 1), _TODAY)
    flex_rows = store.execute(
        "SELECT COUNT(*) FROM flex_trade WHERE source='flex' AND trade_date_iso >= '2026-01-01'"
    ).fetchone()[0]
    assert ytd.trade_count == flex_rows
    assert ytd.trade_count == 8  # every 2026 flex row; the live row and 2025 excluded


def test_year_boundary_excludes_last_years_trade(store):
    """2025-12-31's +9999.99 must not reach a YTD window that starts 2026-01-01."""
    ytd = dd.realised_window(store, date(2026, 1, 1), _TODAY)
    assert ytd.total == pytest.approx(-2194.98 - 100.00 - 1511.20 - 200.00)


def test_window_splits_by_asset_class(store):
    """FUT vs STK vs OPT — the futures/equities split the requirements ask for."""
    ytd = dd.realised_window(store, date(2026, 1, 1), _TODAY)
    assert ytd.by_asset["FUT"] == pytest.approx(-3616.98)
    assert ytd.by_asset["STK"] == pytest.approx(1122.00)
    assert ytd.by_asset["OPT"] == pytest.approx(-1511.20)
    assert ytd.asset_total("STK", "OPT") == pytest.approx(-389.20)
    assert ytd.asset_total("NOPE") == 0.0


def test_currency_label_reports_mixed_rather_than_assuming_usd(store):
    """A window holding EUR and USD is labelled 'mixed', never stamped USD."""
    ytd = dd.realised_window(store, date(2026, 1, 1), _TODAY)
    assert set(ytd.currencies) == {"EUR", "USD"}
    assert ytd.currency_label == "mixed"

    week = dd.realised_window(store, date(2026, 8, 3), _TODAY)
    assert week.currency_label == "USD"


def test_an_empty_window_states_no_currency_rather_than_guessing_usd(store):
    """Nothing realised means no currency to state — and "USD" would be a guess.

    The same refusal `parse_ledger` makes. The view substitutes the account's own base
    currency when it has one; `fmt_signed` renders a bare number when it does not.
    """
    win = dd.realised_window(store, date(2026, 6, 1), date(2026, 6, 7))
    assert (win.total, win.trade_count, win.by_asset) == (0.0, 0, {})
    assert win.currency_label == ""


# ── Realised series ───────────────────────────────────────────────────────────


def test_series_is_daily_with_a_running_total(store):
    """One point per trading day, cumulative running forward, non-trading days absent."""
    pts = dd.realised_series(store, date(2026, 8, 3), _TODAY)
    assert [p.day for p in pts] == [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
    ]
    assert pts[0].realised == pytest.approx(-3516.98)  # both 08-03 rows summed
    assert pts[-1].cumulative == pytest.approx(-2194.98)
    assert pts[-1].cumulative == pytest.approx(
        dd.realised_window(store, date(2026, 8, 3), _TODAY).total
    )


def test_series_excludes_live_rows_and_other_years(store):
    """The YTD curve starts in 2026 and never picks up the untimed live row."""
    pts = dd.realised_series(store, date(2026, 1, 1), _TODAY)
    assert all(p.day.year == 2026 for p in pts)
    # 7 distinct 2026 trading days (08-03's two rows collapse into one point).
    assert len(pts) == 7


# ── Round trips ───────────────────────────────────────────────────────────────


def test_round_trip_stats_counts_lots_not_executions(store):
    """Winners/losers come from flex_lot — the true round trips (D1)."""
    st = dd.round_trip_stats(store, date(2026, 8, 3), _TODAY)
    assert (st.closed_lots, st.winners, st.losers, st.scratches) == (4, 2, 2, 0)
    assert st.gross_win == pytest.approx(1322.00)
    assert st.gross_loss == pytest.approx(-4329.38)
    assert st.win_rate == pytest.approx(50.0)


def test_round_trip_stats_uses_compact_dates_for_flex_lot(store):
    """flex_lot has no trade_date_iso — the bounds must be YYYYMMDD or it matches nothing.

    A window written in ISO would return zero lots against real data while raising no
    error at all, so this pins the format rather than the count alone.
    """
    st = dd.round_trip_stats(store, date(2026, 1, 1), _TODAY)
    assert st.closed_lots == 5  # the 2025 lot is excluded
    assert dd.round_trip_stats(store, date(2025, 1, 1), date(2025, 12, 31)).closed_lots == 1


def test_win_rate_excludes_scratches_and_is_none_when_nothing_closed(store):
    """A 0.00 lot is neither a win nor a loss; an empty window has no rate at all."""
    empty = dd.round_trip_stats(store, date(2026, 6, 1), date(2026, 6, 7))
    assert (empty.closed_lots, empty.win_rate) == (0, None)

    scratch = dd.RoundTripStats(
        start=date(2026, 1, 1), end=date(2026, 1, 2),
        closed_lots=3, winners=1, losers=1, scratches=1,
        gross_win=10.0, gross_loss=-5.0,
    )
    assert scratch.win_rate == pytest.approx(50.0)


# ── Flex coverage / the T+1 gap ───────────────────────────────────────────────


def test_flex_coverage_reports_the_gap(store):
    """`through` is the newest Flex date; `live_pending` counts unbucketed live fills."""
    cov = dd.flex_coverage(store)
    assert cov.through == date(2026, 8, 6)
    assert cov.live_pending == 1


def test_flex_coverage_on_an_empty_store(tmp_path):
    """No Flex rows at all: `through` is None rather than a crash or a fake date."""
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)"
        )
    conn = dd.connect(path)
    try:
        assert dd.flex_coverage(conn) == dd.FlexCoverage(through=None, live_pending=0)
    finally:
        conn.close()


def test_connect_is_read_only(store):
    """The store handle cannot write. A display surface must not be able to corrupt it."""
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.execute("INSERT INTO flex_trade (source) VALUES ('flex')")


def test_build_flex_sections_wires_every_window(store):
    """One threaded hop produces week/month/ytd/stats/series/coverage, all consistent."""
    s = dd.build_flex_sections(store, _TODAY)
    assert set(s) == {"week", "month", "ytd", "stats", "series", "coverage"}
    assert s["week"].start == date(2026, 8, 3)
    assert s["month"].start == date(2026, 8, 1)
    assert s["ytd"].start == date(2026, 1, 1)
    assert s["month"].total == pytest.approx(s["week"].total - 100.00)
    assert s["series"][-1].cumulative == pytest.approx(s["ytd"].total)
    assert set(s["stats"]) == {"week", "month", "ytd"}
    assert s["stats"]["week"].start == date(2026, 8, 3)
    assert s["stats"]["ytd"].start == date(2026, 1, 1)


def test_round_trip_stats_are_computed_per_window_not_reused(store):
    """Each window gets its OWN counts — reuse put week figures under a YTD heading.

    Caught in the Phase 3 smoke: a single `week_stats` field rendered "1 closed round
    trip" beneath a "YTD" title while the YTD total beside it covered 636 executions.
    A count cannot be sliced out of a wider window, so it must be queried per window.
    """
    s = dd.build_flex_sections(store, _TODAY)
    assert s["stats"]["week"].closed_lots == 4
    assert s["stats"]["ytd"].closed_lots == 5
    assert s["stats"]["week"].closed_lots != s["stats"]["ytd"].closed_lots


# ── Ledger parsing ────────────────────────────────────────────────────────────


def _ledger_row(**over):
    """A minimal ledger currency object with the fields the dashboard reads."""
    row = {
        "currency": "USD",
        "netliquidationvalue": 100000.0,
        "cashbalance": 25000.5,
        "settledcash": 24000.0,
        "stockmarketvalue": 60000.0,
        "futuremarketvalue": 0.0,
        "unrealizedpnl": -3638.52,
        "realizedpnl": 412.10,
        "futuresonlypnl": -3516.98,
    }
    row.update(over)
    return row


def test_the_real_ledger_shape_resolves_to_usd_not_base():
    """The exact payload the live account returns — the shape that broke this once.

    Measured 2026-08-04: `{'USD': {'currency': 'USD', ...}, 'BASE': {'currency':
    'BASE', ...}}`. The BASE row's `currency` field is the literal string "BASE", not
    the base currency's code, contrary to how IBKR's documentation reads. Trusting it
    put **"57,600.71 BASE"** on the KPI strip on the first real run — a currency label
    that is not a currency, on a money figure.
    """
    snap = dd.parse_ledger({
        "USD": _ledger_row(currency="USD", netliquidationvalue=57600.71),
        "BASE": _ledger_row(currency="BASE", netliquidationvalue=57600.71),
    })
    assert snap is not None
    assert snap.currency == "USD"
    assert snap.currency != "BASE"
    assert snap.net_liquidation == 57600.71
    assert "BASE" not in snap.other_currencies


def test_parse_ledger_prefers_the_account_base_currency_hint():
    """`/portfolio/accounts`'s `currency` is authoritative and beats every other rule."""
    snap = dd.parse_ledger(
        {
            "BASE": _ledger_row(currency="BASE", netliquidationvalue=999.0),
            "USD": _ledger_row(),
            "EUR": _ledger_row(currency="EUR", netliquidationvalue=10.0),
        },
        base_currency="usd",  # lower case: the hint is normalised, not trusted verbatim
    )
    assert snap is not None
    assert snap.currency == "USD"
    assert snap.net_liquidation == 100000.0  # the USD row, not BASE's 999.0
    assert snap.other_currencies == ("EUR",)


def test_a_base_hint_is_never_used_as_a_currency_label():
    """A caller passing "BASE" must not make "BASE" the label — it is not a currency."""
    snap = dd.parse_ledger(
        {"BASE": _ledger_row(currency="BASE"), "USD": _ledger_row()},
        base_currency="BASE",
    )
    assert snap is not None
    assert snap.currency == "USD"


def test_parse_ledger_falls_back_to_a_base_row_that_names_a_real_key():
    """A BASE row naming a currency that has its own row is still honoured (rule 3)."""
    snap = dd.parse_ledger({
        "BASE": _ledger_row(currency="CHF"),
        "CHF": _ledger_row(currency="CHF", cashbalance=7.0),
        "EUR": _ledger_row(currency="EUR"),
    })
    assert snap is not None
    assert (snap.currency, snap.cash) == ("CHF", 7.0)


def test_parse_ledger_will_not_label_a_lone_base_row():
    """A BASE row alone naming nothing resolvable is unlabelable — return None.

    Showing its numbers under "BASE" would put a non-currency on a money figure, and
    inventing USD would be a guess. The caller renders "ledger unavailable", which is
    the only true statement available.
    """
    assert dd.parse_ledger({"BASE": _ledger_row(currency="BASE")}) is None


def test_parse_ledger_accepts_a_single_currency_with_no_base_row():
    snap = dd.parse_ledger({"USD": _ledger_row()})
    assert snap is not None
    assert snap.currency == "USD"
    assert snap.other_currencies == ()


def test_parse_ledger_refuses_to_guess_between_currencies():
    """Several currencies, no BASE row: there is no evidence which is the account's.

    Returning None makes the UI say "ledger unavailable", which is true. Picking the
    largest balance, or USD-if-present, would put a guess on a money figure.
    """
    assert dd.parse_ledger({"USD": _ledger_row(), "EUR": _ledger_row(currency="EUR")}) is None


@pytest.mark.parametrize("payload", [None, {}, {"USD": "not-a-dict"}, "nonsense"])
def test_parse_ledger_handles_junk(payload):
    assert dd.parse_ledger(payload) is None


def test_parse_ledger_coerces_string_and_missing_numbers():
    """IBKR ships numbers as float, int or numeric string; blanks must read as 0.0."""
    snap = dd.parse_ledger({"USD": {"currency": "USD", "cashbalance": "1234.56",
                                    "netliquidationvalue": None}})
    assert snap is not None
    assert snap.cash == pytest.approx(1234.56)
    assert snap.net_liquidation == 0.0
    assert snap.unrealised_pnl == 0.0


def test_fetch_ledger_passes_the_account_id_and_currency_hint_through():
    class _Client:
        """Records the account id it was asked for."""

        def __init__(self):
            self.seen = None

        def get_account_ledger(self, account_id):
            """Return the live ledger shape and remember the account id."""
            self.seen = account_id
            return {"USD": _ledger_row(), "BASE": _ledger_row(currency="BASE"),
                    "EUR": _ledger_row(currency="EUR")}

    client = _Client()
    snap = dd.fetch_ledger(client, "U1234567", "USD")
    assert client.seen == "U1234567"
    assert snap is not None and snap.currency == "USD"


def test_realised_ledger_window_is_the_measured_calendar_day():
    """Settled 2026-08-04: ledger `realizedpnl` is today's realised P&L.

    The ledger endpoint documents no window, but `realizedpnl` was measured to be
    exactly the sum of the per-position `realizedPnl`, which IBKR does document as
    "the total profit made today through trades". Seven reads spanning both the 18:00 ET
    futures roll and the 20:00 ET stock roll never moved, ruling out the session
    reading; IGV — realised on 2026-07-30, never flat, reporting 0.00 — ruled out every
    cumulative reading. Full evidence at the constant.
    """
    assert dd.REALISED_LEDGER_WINDOW == "day"
    assert dd.realised_ledger_label() == "Realised today"


def test_realised_ledger_label_falls_back_rather_than_overclaiming():
    """An unknown window must render the label that claims least, not raise or guess."""
    assert dd.realised_ledger_label("unverified") == "Realised (ledger)"
    assert dd.realised_ledger_label("cumulative") == "Realised (cumulative)"
    assert dd.realised_ledger_label("something-else") == "Realised (ledger)"
    # "session" was the rival reading and is now disproven: it must not survive as a
    # key, or a future edit could quietly relabel the tile with a window we ruled out.
    assert "session" not in dd._REALISED_LEDGER_LABELS
    assert dd.realised_ledger_label("session") == "Realised (ledger)"


# ── Economic entry ────────────────────────────────────────────────────────────


@pytest.fixture
def fills(tmp_path):
    """A store of raw fills, carrying every case `economic_entries` must decline on.

    GLD  — built over two days, partly sold, and then sold again *today* through a
           conid-less live row. The reconstruction must follow it to 50 shares.
    IGV  — untouched today; reconstructs from Flex alone.
    OLD  — a position whose opening fills are not in the store at all, which is what a
           history that predates the Flex coverage looks like.
    DUPa/DUPb — two conids that have both traded under the ticker "DUP", so a live row
           saying "DUP" could belong to either.
    """
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (conid TEXT, symbol TEXT, underlying_symbol TEXT,"
            " source TEXT, trade_date TEXT, date_time TEXT, quantity REAL,"
            " trade_price REAL)"
        )
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # GLD: +60 @ 100, +40 @ 110, then -50 (FIFO eats the 100s)
                ("1", "GLD", None, "flex", "20260601", "20260601;100000", 60.0, 100.0),
                ("1", "GLD", None, "flex", "20260602", "20260602;100000", 40.0, 110.0),
                # today's fill: live rows carry NO conid, only a symbol
                (None, "GLD", None, "live", None, "20260804;140000", -50.0, 120.0),
                # IGV: +100 @ 90, nothing today
                ("2", "IGV", None, "flex", "20260610", "20260610;100000", 100.0, 90.0),
                # OLD: only a partial sale is on record, never the opening buy
                ("3", "OLD", None, "flex", "20260701", "20260701;100000", -5.0, 50.0),
                # Two contracts sharing a ticker
                ("4", "DUPa", "DUP", "flex", "20260601", "20260601;100000", 10.0, 10.0),
                ("5", "DUPb", "DUP", "flex", "20260601", "20260601;110000", 10.0, 20.0),
            ],
        )
    conn = dd.connect(path)
    yield conn
    conn.close()


def _entry_pos(conid, symbol, quantity, **over):
    """A `Position` carrying only the fields `economic_entries` and the deltas read."""
    fields = {
        "conid": conid, "symbol": symbol, "description": symbol, "asset_class": "STK",
        "quantity": quantity, "average_cost": 0.0, "market_price": 0.0,
        "market_value": 0.0, "unrealised_pnl": 0.0, "realised_pnl": 0.0,
        "currency": "USD", "average_price": 0.0, "multiplier": 1.0,
    }
    fields.update(over)
    return dd.Position(**fields)


def test_fifo_open_average_prices_the_lots_still_open():
    """A partial sale consumes the OLDEST lots, so the newest are what remains.

    +60 @ 100 then +40 @ 110, sell 50: FIFO eats 50 of the 100s, leaving 10 @ 100 and
    40 @ 110 -> (10*100 + 40*110) / 50 = 108.0. An average-cost method would answer
    104.0 here, which is why the two are not interchangeable.
    """
    average, held = dd._fifo_open_average([(60.0, 100.0), (40.0, 110.0), (-50.0, 120.0)])
    assert held == pytest.approx(50.0)
    assert average == pytest.approx(108.0)


def test_fifo_open_average_returns_none_when_flat():
    """A flat book has no entry price, and 0.0 would look like a tradeable level."""
    assert dd._fifo_open_average([(10.0, 5.0), (-10.0, 7.0)]) == (None, 0.0)


def test_fifo_open_average_resets_after_going_flat():
    """A closed position must not contaminate the next one — the six-year-history case."""
    average, held = dd._fifo_open_average(
        [(10.0, 5.0), (-10.0, 7.0), (4.0, 200.0)]
    )
    assert held == pytest.approx(4.0)
    assert average == pytest.approx(200.0)


def test_fifo_open_average_handles_a_reversal_through_zero():
    """Selling through flat opens a short at the reversing fill's price, not an average."""
    average, held = dd._fifo_open_average([(10.0, 5.0), (-15.0, 9.0)])
    assert held == pytest.approx(-5.0)
    assert average == pytest.approx(9.0)


def test_economic_entry_follows_a_conid_less_live_fill(fills):
    """Live rows carry no conid (measured 2026-08-04), so they match on symbol.

    Without the live row the reconstruction would hold 100 shares against IBKR's 50 and
    be declined; with it, the FIFO average of the surviving lots is 108.0.
    """
    entries = dd.economic_entries(fills, [_entry_pos(1, "GLD", 50.0)])
    assert entries[1] == pytest.approx(108.0)


def test_economic_entry_from_flex_alone(fills):
    entries = dd.economic_entries(fills, [_entry_pos(2, "IGV", 100.0)])
    assert entries[2] == pytest.approx(90.0)


def test_economic_entry_declines_when_the_quantity_does_not_reconstruct(fills):
    """The one safety check: if we cannot reproduce IBKR's own quantity, we say nothing.

    A position whose opening fills predate the stored history reconstructs to the wrong
    size, and a wrong size means a wrong average. Absence is the honest answer.
    """
    assert dd.economic_entries(fills, [_entry_pos(3, "OLD", 20.0)]) == {}


def test_economic_entry_declines_on_a_shared_ticker(fills):
    """A ticker is not a unique key — the IGV/MXN lesson, applied to the live rows.

    Both conids have traded under "DUP", so a conid-less live row naming "DUP" could
    belong to either. Every position the ambiguity touches is declined rather than one
    of them being picked.
    """
    assert dd.economic_entries(fills, [_entry_pos(4, "DUPa", 10.0), _entry_pos(5, "DUPb", 10.0)]) == {}


def test_economic_entry_ignores_positions_that_are_flat(fills):
    assert dd.economic_entries(fills, [_entry_pos(1, "GLD", 0.0)]) == {}


def test_with_economic_entries_leaves_unreconstructed_positions_at_none():
    positions = (_entry_pos(1, "GLD", 50.0), _entry_pos(3, "OLD", 20.0))
    out = dd.with_economic_entries(positions, {1: 108.0})
    assert out[0].economic_entry == pytest.approx(108.0)
    assert out[1].economic_entry is None


def test_basis_delta_is_none_without_a_reconstructed_entry():
    """No entry means no comparison — not a delta of zero, which would read as agreement."""
    position = _entry_pos(1, "GLD", 50.0, average_price=110.0)
    assert position.basis_delta is None
    assert position.basis_delta_value is None



def test_multiplier_is_derived_when_ibkr_omits_it():
    """IBKR served a lean CL row with no `multiplier` and a full one minutes later.

    `avgCost / avgPrice` is the multiplier by definition, and both come from the same
    row, so deriving it keeps the three fields consistent. Defaulting to 1 instead put
    +0.00472 on screen where the answer was +4.72 (measured live 2026-08-04).
    """
    (position,) = dd.parse_positions([
        {"conid": 9, "ticker": "CL", "assetClass": "FUT", "position": 2.0,
         "avgCost": 80932.36, "avgPrice": 80.93236, "currency": "USD"}
    ])
    assert position.multiplier == pytest.approx(1000.0)
    assert position.average_price == pytest.approx(80.93236)


def test_multiplier_stays_unknown_when_it_cannot_be_established():
    """No multiplier and no way to derive one means no money figure — not a guess of 1."""
    (position,) = dd.parse_positions([
        {"conid": 9, "ticker": "CL", "assetClass": "FUT", "position": 2.0,
         "currency": "USD"}
    ])
    assert position.multiplier is None
    assert dd.replace(position, economic_entry=80.0).basis_delta_value is None


def test_stock_multiplier_derives_to_one():
    """`avgCost == avgPrice` for stock, so the ratio establishes 1 rather than assuming it."""
    (position,) = dd.parse_positions([
        {"conid": 1, "ticker": "GLD", "assetClass": "STK", "position": 50.0,
         "avgCost": 383.270899, "avgPrice": 383.270899, "currency": "USD"}
    ])
    assert position.multiplier == pytest.approx(1.0)


def test_basis_delta_is_none_without_a_basis():
    """A missing `average_price` is not a basis of zero — subtracting from it would turn
    an absent field into a confident, enormous number."""
    position = _entry_pos(1, "GLD", 50.0, average_price=0.0, economic_entry=380.0)
    assert position.basis_delta is None
    assert position.basis_delta_value is None


def test_basis_delta_value_applies_the_futures_multiplier():
    """Per-unit difference times size times multiplier — the money the column means.

    CL-shaped: 2 contracts, multiplier 1000, basis 80.93236 against an entry of 80.93
    is 4.72, which is what commission looks like on a clean futures position.
    """
    position = _entry_pos(9, "CL", 2.0, average_price=80.93236, economic_entry=80.93,
                    multiplier=1000.0)
    assert position.basis_delta == pytest.approx(0.00236)
    assert position.basis_delta_value == pytest.approx(4.72)


def test_basis_delta_value_is_signed_by_direction_not_by_profit():
    """A short with a basis BELOW its entry yields a positive figure, and must.

    The delta answers "how much of Unrealised is basis", and for a short position a
    lower basis inflates the P&L the same way a higher one deflates a long's.
    """
    position = _entry_pos(9, "XYZ", -100.0, average_price=9.0, economic_entry=10.0)
    assert position.basis_delta_value == pytest.approx(100.0)


# ── Positions ─────────────────────────────────────────────────────────────────


def _position_row(**over):
    """A minimal IBKR position row with the fields the dashboard reads."""
    row = {
        "conid": 495512563,
        "ticker": "ESU6",
        "contractDesc": "ESU6",
        "assetClass": "FUT",
        "position": 1.0,
        "avgCost": 6500.0,
        "mktPrice": 6480.0,
        "mktValue": 324000.0,
        "unrealizedPnl": -1000.0,
        "realizedPnl": 0.0,
        "currency": "USD",
    }
    row.update(over)
    return row


def test_parse_positions_types_the_fields():
    (pos,) = dd.parse_positions([_position_row()])
    assert (pos.symbol, pos.asset_class, pos.quantity) == ("ESU6", "FUT", 1.0)
    assert pos.unrealised_pnl == -1000.0
    assert pos.currency == "USD"


def test_parse_positions_drops_closed_rows_but_keeps_unparseable_ones():
    """position: 0 is a closed trade IBKR keeps echoing; a missing field is not."""
    rows = dd.parse_positions([
        _position_row(position=0.0),
        _position_row(ticker="AAPL"),
        {k: v for k, v in _position_row(ticker="MSFT").items() if k != "position"},
    ])
    assert [p.symbol for p in rows] == ["AAPL", "MSFT"]
    assert rows[1].quantity == 0.0


def test_parse_positions_falls_back_to_contract_desc_for_the_symbol():
    (pos,) = dd.parse_positions([_position_row(ticker="", contractDesc="CLU6")])
    assert pos.symbol == "CLU6"


def test_parse_positions_skips_non_mapping_rows():
    assert dd.parse_positions(["junk", None, _position_row()]) == dd.parse_positions(
        [_position_row()]
    )


class _PagingClient:
    """A client that serves `pages` in order and records which pages were asked for."""

    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get_positions(self, account_id, page=0):
        """Return the page at `page`, or [] past the end."""
        self.asked.append(page)
        return self.pages[page] if page < len(self.pages) else []


def test_fetch_positions_pages_past_the_first_thirty():
    """Page 0 returns only 30 — reading it alone would silently show a partial book."""
    full = [_position_row(ticker=f"S{i}") for i in range(30)]
    client = _PagingClient([full, [_position_row(ticker="LAST")]])
    out = dd.fetch_positions(client, "U1")
    assert len(out) == 31
    assert out[-1].symbol == "LAST"
    assert client.asked == [0, 1]


def test_fetch_positions_stops_on_a_short_page():
    client = _PagingClient([[_position_row()]])
    assert len(dd.fetch_positions(client, "U1")) == 1
    assert client.asked == [0]


def test_fetch_positions_stops_on_an_empty_first_page():
    client = _PagingClient([[]])
    assert dd.fetch_positions(client, "U1") == ()
    assert client.asked == [0]


def test_fetch_positions_is_capped_against_a_never_shortening_response(caplog):
    """A response that always returns a full page must not spin forever."""
    class _Endless:
        """Always returns a full page, whatever the page number."""

        def get_positions(self, account_id, page=0):
            """Return a full page every time."""
            return [_position_row(ticker=f"P{page}") for _ in range(30)]

    with caplog.at_level("WARNING"):
        out = dd.fetch_positions(_Endless(), "U1")
    assert len(out) == 30 * dd._MAX_POSITION_PAGES
    assert "page cap" in caplog.text


# ── Snapshot ──────────────────────────────────────────────────────────────────


def test_snapshot_age_grows_and_never_goes_negative():
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    snap = dd.DashboardSnapshot(as_of=now)
    assert snap.age_seconds(now + timedelta(seconds=45)) == pytest.approx(45.0)
    assert snap.age_seconds(now - timedelta(seconds=10)) == 0.0


def test_empty_snapshot_carries_an_error_and_no_data():
    snap = dd.empty_snapshot(error="IBKR gateway not reachable")
    assert snap.ledger is None
    assert snap.positions == ()
    assert snap.error == "IBKR gateway not reachable"


# ── Reconciliation ────────────────────────────────────────────────────────────


def _snapshot_for_reconcile(positions, unrealised, currency="USD"):
    """A snapshot carrying just enough for `reconcile` to work on."""
    return dd.DashboardSnapshot(
        as_of=datetime(2026, 8, 6, tzinfo=UTC),
        ledger=dd.LedgerSnapshot(currency, 0.0, 0.0, 0.0, 0.0, 0.0, unrealised, 0.0, 0.0),
        positions=positions,
    )


def _pos(symbol, upl, currency="USD"):
    """One position carrying only the fields reconciliation reads."""
    (out,) = dd.parse_positions([
        {"ticker": symbol, "position": 1.0, "unrealizedPnl": upl, "currency": currency}
    ])
    return out


def test_reconcile_agrees_on_the_live_figures():
    """Measured live 2026-08-04: positions summed -11,618.31 vs ledger -11,618.32."""
    rec = dd.reconcile(_snapshot_for_reconcile(
        (_pos("GLD", -458.66), _pos("CL", -11584.72), _pos("IGV", 425.07)), -11618.32
    ))
    assert rec.checked
    assert rec.positions_total == pytest.approx(-11618.31)
    assert rec.delta == pytest.approx(0.01)
    assert rec.agrees


def test_reconcile_rounds_to_cents_before_comparing():
    """Binary float made an exactly-five-cent delta measure as 0.0500000000001819.

    Against a tolerance of 0.05 that trips by 1.8e-13 — a spurious integrity alarm on
    money that reconciles perfectly. The same trap `opening_status` documents.
    """
    rec = dd.reconcile(_snapshot_for_reconcile(
        (_pos("A", -1152.43), _pos("B", -2486.08)), -3638.56
    ))
    assert rec.positions_total == pytest.approx(-3638.51)
    assert rec.delta == 0.05  # exactly, not 0.0500000000001819


def test_reconcile_fails_past_the_tolerance():
    rec = dd.reconcile(_snapshot_for_reconcile((_pos("CL", -1000.0),), -2000.0))
    assert rec.checked and not rec.agrees
    assert rec.delta == pytest.approx(1000.0)
    assert dd.RECONCILE_TOLERANCE == 250.00


def test_reconcile_flags_a_cross_currency_book_rather_than_summing_it():
    """IGV once priced a US ETF in MXN — a cross-currency sum is a confident wrong number."""
    rec = dd.reconcile(_snapshot_for_reconcile(
        (_pos("AAPL", 10.0), _pos("SAP", -4.0, "EUR")), 6.0
    ))
    assert rec.mixed_currency
    assert not rec.agrees  # the delta happens to be 0.00, and that proves nothing


def test_an_unrun_check_is_never_a_pass():
    """No ledger or no positions: `checked` False and `agrees` False, not a green tick."""
    no_ledger = dd.DashboardSnapshot(
        as_of=datetime(2026, 8, 6, tzinfo=UTC), positions=(_pos("A", 1.0),)
    )
    assert not dd.reconcile(no_ledger).checked
    assert not dd.reconcile(no_ledger).agrees

    no_positions = _snapshot_for_reconcile((), -100.0)
    assert not dd.reconcile(no_positions).checked
    assert not dd.reconcile(no_positions).agrees
