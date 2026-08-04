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
    win = dd.realised_window(store, "week", date(2026, 8, 3), _TODAY)
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
    win = dd.realised_window(store, "week", date(2026, 8, 3), _TODAY)
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
    win = dd.realised_window(store, "week", date(2026, 8, 3), _TODAY)
    lot_sum = store.execute(
        "SELECT SUM(fifo_pnl_realized) FROM flex_lot"
        " WHERE trade_date BETWEEN '20260803' AND '20260806'"
    ).fetchone()[0]
    assert lot_sum == pytest.approx(-3007.38)
    assert win.total != pytest.approx(lot_sum)


def test_live_rows_are_excluded_entirely(store):
    """The live row has no trade_date and must not be counted, in any window."""
    ytd = dd.realised_window(store, "ytd", date(2026, 1, 1), _TODAY)
    flex_rows = store.execute(
        "SELECT COUNT(*) FROM flex_trade WHERE source='flex' AND trade_date_iso >= '2026-01-01'"
    ).fetchone()[0]
    assert ytd.trade_count == flex_rows
    assert ytd.trade_count == 8  # every 2026 flex row; the live row and 2025 excluded


def test_year_boundary_excludes_last_years_trade(store):
    """2025-12-31's +9999.99 must not reach a YTD window that starts 2026-01-01."""
    ytd = dd.realised_window(store, "ytd", date(2026, 1, 1), _TODAY)
    assert ytd.total == pytest.approx(-2194.98 - 100.00 - 1511.20 - 200.00)


def test_window_splits_by_asset_class(store):
    """FUT vs STK vs OPT — the futures/equities split the requirements ask for."""
    ytd = dd.realised_window(store, "ytd", date(2026, 1, 1), _TODAY)
    assert ytd.by_asset["FUT"] == pytest.approx(-3616.98)
    assert ytd.by_asset["STK"] == pytest.approx(1122.00)
    assert ytd.by_asset["OPT"] == pytest.approx(-1511.20)
    assert ytd.asset_total("STK", "OPT") == pytest.approx(-389.20)
    assert ytd.asset_total("NOPE") == 0.0


def test_currency_label_reports_mixed_rather_than_assuming_usd(store):
    """A window holding EUR and USD is labelled 'mixed', never stamped USD."""
    ytd = dd.realised_window(store, "ytd", date(2026, 1, 1), _TODAY)
    assert set(ytd.currencies) == {"EUR", "USD"}
    assert ytd.currency_label == "mixed"

    week = dd.realised_window(store, "week", date(2026, 8, 3), _TODAY)
    assert week.currency_label == "USD"


def test_empty_window_is_zero_and_labelled_usd(store):
    """A window with no trades reports 0.00/0 trades and a usable label, not blank."""
    win = dd.realised_window(store, "week", date(2026, 6, 1), date(2026, 6, 7))
    assert (win.total, win.trade_count, win.by_asset) == (0.0, 0, {})
    assert win.currency_label == "USD"


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
        dd.realised_window(store, "week", date(2026, 8, 3), _TODAY).total
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


def test_realised_ledger_label_never_overclaims():
    """The default label makes no claim about a window IBKR does not document."""
    assert dd.REALISED_LEDGER_WINDOW == "unverified"
    assert dd.realised_ledger_label() == "Realised (ledger)"
    assert dd.realised_ledger_label("session") == "Realised today"
    assert dd.realised_ledger_label("cumulative") == "Realised (cumulative)"
    assert dd.realised_ledger_label("something-else") == "Realised (ledger)"


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
