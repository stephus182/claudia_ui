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
from claudia.live_realised import LiveFill

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
# `asset_category` is on the REAL flex_lot — verified against the live store 2026-08-06
# (FUT 296, STK 405, OPT 4, FUND 2). A fixture without it is a double weaker than its
# dependency: it let `realised_by_type` and `bridged_by_type` pass here while failing
# against the real schema, which is exactly the class of gap this project has been bitten
# by before.
_LOTS = [
    ("20260803", "FUT", -3516.98),
    ("20260804", "FUT", 1071.75),
    ("20260805", "STK", -812.40),  # the pre-wash-sale detail behind the 0.00 trade above
    ("20260806", "FUT", 250.25),
    ("20260801", "STK", -100.00),
    ("20251231", "FUT", 9999.99),
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
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (iso, iso.replace("-", "") if iso else None, src, cat, ccy, oc, pnl)
                for iso, src, cat, ccy, oc, pnl in _TRADES
            ],
        )
        w.executemany("INSERT INTO flex_lot VALUES (?, ?, ?)", _LOTS)
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
    """The trading week starts Monday, whichever day it is read on."""
    assert dd.week_start(today) == expected


def test_month_and_year_start():
    """Month and year windows start on the first calendar day."""
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
    """One threaded hop produces every section, all consistent."""
    s = dd.build_flex_sections(store, _TODAY)
    assert set(s) == {"week", "month", "ytd", "stats", "breakdowns", "series", "coverage"}
    assert s["week"].start == date(2026, 8, 3)
    assert s["month"].start == date(2026, 8, 1)
    assert s["ytd"].start == date(2026, 1, 1)
    assert s["month"].total == pytest.approx(s["week"].total - 100.00)
    assert s["series"][-1].cumulative == pytest.approx(s["ytd"].total)
    assert set(s["stats"]) == {"week", "month", "ytd"}
    assert s["stats"]["week"].start == date(2026, 8, 3)
    assert s["stats"]["ytd"].start == date(2026, 1, 1)


def test_breakdowns_carry_a_day_window_that_the_flex_windows_cannot(store):
    """`day` exists only among the breakdowns, and that asymmetry is the point.

    `realised_window` and `round_trip_stats` are Flex-only, and Flex never has today — it
    was two days behind on 2026-08-06. A Flex-derived "today" would therefore be
    permanently empty, so the daily win rate the KPI strip needs can only come from a
    bridged breakdown.
    """
    s = dd.build_flex_sections(store, _TODAY)
    assert set(s["breakdowns"]) == {"day", "week", "month", "ytd"}
    assert set(s["stats"]) == {"week", "month", "ytd"}   # no "day" here, deliberately


def test_breakdowns_are_flex_only_without_a_reconstruction(store):
    """A logged-out session keeps its settled history instead of showing nothing."""
    s = dd.build_flex_sections(store, _TODAY)
    assert s["breakdowns"]["week"].bridged_days == ()
    assert s["breakdowns"]["week"].incomplete is False


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
    """A single-currency response resolves without a BASE row to disambiguate against."""
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
    """A malformed or empty payload resolves to None rather than a half-built snapshot."""
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
    """The resolved account id reaches the client and the currency hint picks the right row."""
    class _Client:
        """Records the account id it was asked for."""

        def __init__(self):
            """Start with no recorded account id."""
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
    """The settled half of the book: Flex rows only, through 2026-08-07.

    Today's executions are never in here — they arrive as `live_fills`, which is the
    whole point of the boundary.

    GLD  — built over two days and partly sold today, so it reconstructs to 50 shares
           only when the live sale is supplied.
    IGV  — untouched today; reconstructs from Flex alone.
    OLD  — a position whose opening fills are not in the store at all, which is what a
           history that predates the Flex coverage looks like.
    DUPa/DUPb — two conids that have both traded under the ticker "DUP", the case that
           made symbol matching untenable.
    CL   — two open lots averaging 77.185 as of the last statement, every one of which
           was closed and replaced on 2026-08-10.
    """
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (conid TEXT, symbol TEXT, underlying_symbol TEXT,"
            " source TEXT, trade_date TEXT, trade_date_iso TEXT, date_time TEXT,"
            " quantity REAL, trade_price REAL)"
        )
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # GLD: +60 @ 100, +40 @ 110. Today's -50 arrives as a live fill, not a row.
                ("1", "GLD", None, "flex", "20260601", "2026-06-01", "20260601;100000", 60.0, 100.0),
                ("1", "GLD", None, "flex", "20260602", "2026-06-02", "20260602;100000", 40.0, 110.0),
                # IGV: +100 @ 90, nothing today
                ("2", "IGV", None, "flex", "20260610", "2026-06-10", "20260610;100000", 100.0, 90.0),
                # OLD: only a partial sale is on record, never the opening buy
                ("3", "OLD", None, "flex", "20260701", "2026-07-01", "20260701;100000", -5.0, 50.0),
                # Two contracts that have both traded under the ticker "DUP"
                ("4", "DUPa", "DUP", "flex", "20260601", "2026-06-01", "20260601;100000", 10.0, 10.0),
                ("5", "DUPb", "DUP", "flex", "20260601", "2026-06-01", "20260601;110000", 10.0, 20.0),
                # CL: the 2026-08-10 case. Flex's last word is two open lots averaging
                # 77.185 — and every one of them was closed and replaced *today*.
                ("6", "CLU6", "CL", "flex", "20260807", "2026-08-07", "20260807;150000", 1.0, 77.47),
                ("6", "CLU6", "CL", "flex", "20260807", "2026-08-07", "20260807;150100", 1.0, 76.90),
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


def _live_fill(conid, symbol, signed_quantity, price, day="20260810", seq="01"):
    """One execution as `/iserver/account/trades` reports it — conid and all."""
    return LiveFill(
        execution_id=f"{day}.{seq}",
        conid=conid,
        symbol=symbol,
        asset_class="FUT" if symbol == "CL" else "STK",
        signed_quantity=signed_quantity,
        price=price,
        commission=0.0,
        multiplier=1000.0 if symbol == "CL" else 1.0,
        trade_day=day,
    )


# The seven CL executions of 2026-08-10, in order, as IBKR reported them. The position
# ends the day the same size it started it — 2 long — which is exactly why this is here.
_CL_2026_08_10 = [
    _live_fill(6, "CL", -2.0, 78.21, seq="01"),
    _live_fill(6, "CL", +1.0, 78.70, seq="02"),
    _live_fill(6, "CL", +1.0, 78.70, seq="03"),
    _live_fill(6, "CL", -1.0, 81.99, seq="04"),
    _live_fill(6, "CL", -1.0, 81.82, seq="05"),
    _live_fill(6, "CL", +1.0, 82.00, seq="06"),
    _live_fill(6, "CL", +1.0, 82.10, seq="07"),
]


def test_economic_entry_follows_a_live_fill(fills):
    """A fill newer than the statement dataset is part of the book, not an extra.

    Without today's sale the reconstruction would hold 100 shares against IBKR's 50 and
    be declined; with it, the FIFO average of the surviving lots is 108.0.
    """
    entries = dd.economic_entries(
        fills, [_entry_pos(1, "GLD", 50.0)], [_live_fill(1, "GLD", -50.0, 120.0)]
    )
    assert entries[1] == pytest.approx(108.0)


def test_economic_entry_is_todays_lots_when_a_position_was_replaced_intraday(fills):
    """The 2026-08-10 CL defect: same quantity, entirely different lots.

    CL closed its two 77.185-average lots and reopened two at 82.00 and 82.10 within one
    session, so the quantity check — the only safety check there is — passes against a
    book that is four dollars a barrel out of date. The published entry was 77.185 while
    IBKR's own basis said 82.0524, and the positions pane turned that gap into a
    +9,734.72 USD claim that the unrealised P&L was "basis rather than market". It was
    not: the fills and the basis agree to the commission.
    """
    entries = dd.economic_entries(fills, [_entry_pos(6, "CL", 2.0)], _CL_2026_08_10)
    assert entries[6] == pytest.approx(82.05)


def test_economic_entry_declines_when_the_live_fills_are_unknown(fills):
    """No fills in hand means no claim — stored history alone can be a day stale.

    `None` is "we could not read the executions", which is not the same claim as "there
    were none" (`()`), and the CL case is what the difference costs: the stored history
    reproduces IBKR's quantity all by itself and would certify the wrong lots.
    """
    assert dd.economic_entries(fills, [_entry_pos(6, "CL", 2.0)], None) == {}


def test_economic_entry_ignores_fills_the_statement_already_covers(fills):
    """Flex owns everything through its coverage date; a fill from there is a duplicate.

    The CL lots of 2026-08-07 are in the fixture as Flex rows. Handing them back as live
    fills must change nothing — counting both would double the position and decline it.
    """
    settled = [
        _live_fill(6, "CL", 1.0, 77.47, day="20260807", seq="90"),
        _live_fill(6, "CL", 1.0, 76.90, day="20260807", seq="91"),
    ]
    entries = dd.economic_entries(fills, [_entry_pos(6, "CL", 2.0)], settled)
    assert entries[6] == pytest.approx(77.185)


def test_economic_entry_from_flex_alone(fills):
    """FIFO over Flex fills alone reconstructs the open-lot average."""
    entries = dd.economic_entries(fills, [_entry_pos(2, "IGV", 100.0)], [])
    assert entries[2] == pytest.approx(90.0)


def test_economic_entry_declines_when_the_quantity_does_not_reconstruct(fills):
    """The one safety check: if we cannot reproduce IBKR's own quantity, we say nothing.

    A position whose opening fills predate the stored history reconstructs to the wrong
    size, and a wrong size means a wrong average. Absence is the honest answer.
    """
    assert dd.economic_entries(fills, [_entry_pos(3, "OLD", 20.0)], []) == {}


def test_economic_entry_tells_two_contracts_sharing_a_ticker_apart(fills):
    """A ticker is not a unique key — so the fills are keyed on conid, which is one.

    Both conids have traded under "DUP". Executions carry the conid IBKR settled them
    against, so neither position has to be declined for the other's ambiguity, and no
    symbol is ever matched.
    """
    entries = dd.economic_entries(
        fills,
        [_entry_pos(4, "DUPa", 11.0), _entry_pos(5, "DUPb", 10.0)],
        [_live_fill(4, "DUP", 1.0, 30.0)],
    )
    assert entries[4] == pytest.approx((10 * 10.0 + 30.0) / 11)
    assert entries[5] == pytest.approx(20.0)


def test_economic_entry_ignores_positions_that_are_flat(fills):
    """A flat position has no entry price and is left out entirely."""
    assert dd.economic_entries(fills, [_entry_pos(1, "GLD", 0.0)], []) == {}


def test_with_economic_entries_leaves_unreconstructed_positions_at_none():
    """An unreconstructed position keeps `economic_entry` None — a blank cell, never a guess."""
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
    """A raw position row is typed field by field."""
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
    """A lean row with no ticker falls back to `contractDesc` rather than showing blank."""
    (pos,) = dd.parse_positions([_position_row(ticker="", contractDesc="CLU6")])
    assert pos.symbol == "CLU6"


def test_parse_positions_skips_non_mapping_rows():
    """Junk entries inside the list are skipped without affecting the real rows."""
    assert dd.parse_positions(["junk", None, _position_row()]) == dd.parse_positions(
        [_position_row()]
    )


class _PagingClient:
    """A client that serves `pages` in order and records which pages were asked for."""

    def __init__(self, pages):
        """Serve the given pages in order and record which page numbers were asked for."""
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
    """A short page ends paging — IBKR reports no total, so that is the only signal."""
    client = _PagingClient([[_position_row()]])
    assert len(dd.fetch_positions(client, "U1")) == 1
    assert client.asked == [0]


def test_fetch_positions_stops_on_an_empty_first_page():
    """An empty first page ends paging immediately."""
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
    """Age tracks elapsed time and clamps at zero for a clock that ran backwards."""
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    snap = dd.DashboardSnapshot(as_of=now)
    assert snap.age_seconds(now + timedelta(seconds=45)) == pytest.approx(45.0)
    assert snap.age_seconds(now - timedelta(seconds=10)) == 0.0


def test_empty_snapshot_carries_an_error_and_no_data():
    """The pre-first-poll snapshot carries its reason and no account figures."""
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
    """A gap beyond the tolerance is reported as a real disagreement, with the tolerance pinned."""
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


# ── parse_orders (2026-08-05) ─────────────────────────────────────────────────


def test_parse_orders_reads_a_working_order():
    """A working order is typed, with nothing filled and ClaudIA attribution from the local id."""
    orders = dd.parse_orders([{
        "orderId": 314390101, "ticker": "AAPL", "side": "BUY", "totalSize": 1,
        "remainingQuantity": 1, "price": 100.0, "orderType": "Limit",
        "timeInForce": "GTC", "status": "Submitted", "order_ref": "CLAUDIA-178594",
    }])
    assert len(orders) == 1
    o = orders[0]
    assert o.order_id == "314390101" and o.symbol == "AAPL"
    assert o.filled == 0.0 and o.quantity == 1.0
    assert o.is_claudia_staged is True


def test_a_blank_remaining_quantity_does_not_read_as_fully_filled():
    """Regression, caught in review 2026-08-05 before it shipped.

    `_as_float(None)` is 0.0, so reading `remainingQuantity` directly turned a missing
    field into "nothing remaining" and therefore filled == total — rendering an untouched
    resting order as fully filled. IBKR does serve lean rows with fields absent (measured
    on the positions endpoint). Unknown must mean 0 filled, not complete.
    """
    for blank in (None, ""):
        orders = dd.parse_orders([{
            "orderId": "1", "ticker": "CL", "side": "SELL", "totalSize": 2,
            "remainingQuantity": blank, "status": "PreSubmitted",
        }])
        assert orders[0].filled == 0.0, f"blank {blank!r} read as filled"

    # absent entirely — same answer
    orders = dd.parse_orders([{"orderId": "1", "ticker": "CL", "totalSize": 2}])
    assert orders[0].filled == 0.0


def test_a_partial_fill_is_reported_as_such():
    """Filled quantity is total minus remaining."""
    orders = dd.parse_orders([{
        "orderId": "1", "ticker": "CL", "totalSize": 5, "remainingQuantity": 2,
    }])
    assert orders[0].filled == 3.0


def test_rows_without_an_order_id_are_skipped():
    """IBKR's order feed carries non-order rows; one with no id cannot be acted on or
    even named, so it must not occupy a line in the book."""
    orders = dd.parse_orders([{"ticker": "AAPL"}, None, "junk", {"orderId": "7"}])
    assert [o.order_id for o in orders] == ["7"]


def test_an_external_order_is_not_attributed_to_claudia():
    """An order with no ClaudIA local id is reported as external."""
    orders = dd.parse_orders([{"orderId": "1", "ticker": "CL", "order_ref": ""}])
    assert orders[0].is_claudia_staged is False


def test_fetch_orders_returns_none_when_the_lookup_fails(monkeypatch):
    """None is "unknown", () is "nothing resting" — the whole point of the field."""
    class _Boom:
        """A client whose order lookup always fails."""
        def get_live_orders(self):
            """Fail the way a downed brokerage session does."""
            raise RuntimeError("no bridge")

    assert dd.fetch_orders(_Boom()) is None


# ── Contract multiplier: obtained from IBKR, never assumed ───────────────────


def test_equities_report_multiplier_zero_and_must_normalise_to_one():
    """IBKR sends `multiplier: 0.0` on equities — measured live 2026-08-06.

    GLD and IGV both reported 0.0 while ES SEP2026 reported 50.0. `parse_positions`
    relies on 0.0 being falsy to fall through to the `avgCost / avgPrice` ratio. This
    pins that behaviour, because the obvious tidy-up — `if raw_multiplier is not None` —
    would take every equity position to a zero multiplier and silence its money figures.
    """
    rows = [{
        "conid": 1, "ticker": "GLD", "contractDesc": "GLD", "assetClass": "STK",
        "position": 50.0, "multiplier": 0.0,
        "avgPrice": 383.215004, "avgCost": 383.215004,
        "mktPrice": 391.4, "mktValue": 19570.0, "unrealizedPnl": 0.0, "realizedPnl": 0.0,
        "currency": "USD",
    }]
    assert dd.parse_positions(rows)[0].multiplier == 1.0


def test_a_futures_multiplier_is_taken_from_ibkrs_own_field():
    """ES SEP2026 reported 50.0 live. The value is obtained, never looked up in a table.

    A hardcoded multiplier table would be a second, drifting definition of a contract
    property IBKR already publishes.
    """
    rows = [{
        "conid": 2, "ticker": "ES", "contractDesc": "ES  SEP2026", "assetClass": "FUT",
        "position": -1.0, "multiplier": 50.0,
        "avgPrice": 7754.9552, "avgCost": 387747.76,
        "mktPrice": 7755.0, "mktValue": -387750.0, "unrealizedPnl": -20.23,
        "realizedPnl": 945.52, "currency": "USD",
    }]
    assert dd.parse_positions(rows)[0].multiplier == 50.0


def test_the_multiplier_falls_back_to_the_cost_price_ratio():
    """`avgCost / avgPrice` is the second independent route, and it agreed on all three
    live positions on 2026-08-06 (ES 50, GLD 1, IGV 1)."""
    rows = [{
        "conid": 3, "ticker": "CL", "contractDesc": "CL  SEP2026", "assetClass": "FUT",
        "position": 1.0,  # multiplier field absent entirely
        "avgPrice": 80.84, "avgCost": 80840.0,
        "mktPrice": 75.0, "mktValue": 75000.0, "unrealizedPnl": 0.0, "realizedPnl": 0.0,
        "currency": "USD",
    }]
    assert dd.parse_positions(rows)[0].multiplier == 1000.0


# -- realised_by_type: the FUT/STK breakdown ----------------------------------


def _breakdown_db(tmp_path):
    """A store with FUT and STK activity, plus an OPT lot that realised nothing."""
    path = tmp_path / "bd.db"
    with sqlite3.connect(path) as w:
        w.execute("CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
                  " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)")
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.executemany("INSERT INTO flex_trade VALUES (?,?,?,?,?)", [
            ("2026-08-03", "flex", "FUT", "USD", -3516.98),
            ("2026-08-04", "flex", "FUT", "USD", 590.80),
            ("2026-08-04", "flex", "STK", "USD", -3249.70),
            ("2026-08-04", "live", "FUT", "USD", 999999.0),   # live rows are excluded
            ("2026-07-01", "flex", "FUT", "USD", 111.0),      # outside the window
        ])
        w.executemany("INSERT INTO flex_lot VALUES (?,?,?)", [
            ("20260803", "FUT", -3516.98),
            ("20260804", "FUT", 1945.28),
            ("20260804", "FUT", -1354.48),
            ("20260804", "STK", -3249.70),
            ("20260804", "OPT", 0.0),        # a scratch: neither won nor lost
        ])
    return dd.connect(path)


def test_breakdown_splits_by_asset_class(tmp_path):
    """The FUT/STK split the requirement asks for, from one window."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6))
    by = {r.asset_class: r for r in rows}
    assert by["FUT"].net == pytest.approx(-2926.18, abs=0.005)
    assert by["STK"].net == pytest.approx(-3249.70, abs=0.005)


def test_breakdown_takes_money_from_trades_and_counts_from_lots(tmp_path):
    """The two-source rule, pinned.

    `flex_lot` is pre-wash-sale detail (`Trade == Lot + WashSale`); summing it as the
    money figure overstates losses. So `net` must come from `flex_trade` even when the
    lot subtotals are available and look usable.
    """
    fut = next(r for r in dd.realised_by_type(
        _breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6)) if r.asset_class == "FUT")
    assert fut.net == pytest.approx(-2926.18, abs=0.005)          # flex_trade
    assert fut.gross_win == pytest.approx(1945.28, abs=0.005)     # flex_lot
    assert fut.gross_loss == pytest.approx(-4871.46, abs=0.005)   # flex_lot
    assert fut.winners == 1 and fut.losers == 2


def test_breakdown_excludes_live_rows_and_other_windows(tmp_path):
    """`source='live'` carries no realised P&L, and a 999,999 row must not leak in."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6))
    assert all(abs(r.net) < 10000 for r in rows)


def test_only_asset_classes_that_traded_appear(tmp_path):
    """No zero rows for classes never traded — "do not over-engineer, keep it clean"."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 3))
    assert [r.asset_class for r in rows] == ["FUT"]


def test_a_class_that_only_scratched_still_appears(tmp_path):
    """OPT closed a lot at exactly 0.00 — activity with no money is still activity."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 4), date(2026, 8, 4))
    opt = next(r for r in rows if r.asset_class == "OPT")
    assert opt.closed_lots == 1 and opt.scratches == 1 and opt.net == 0.0


def test_scratches_are_excluded_from_the_win_rate(tmp_path):
    """A lot realising exactly 0.00 is neither won nor lost.

    Counting it as a loss would understate the rate; `win_rate` is None when nothing was
    decided, so the UI renders an em dash rather than a 0% that reads like a disaster.
    """
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 4), date(2026, 8, 4))
    assert next(r for r in rows if r.asset_class == "OPT").win_rate is None
    assert next(r for r in rows if r.asset_class == "FUT").win_rate == pytest.approx(50.0)


def test_win_loss_ratio_is_none_rather_than_infinite(tmp_path):
    """A window with no losing lot has no ratio; rendering one invites a false comparison."""
    path = tmp_path / "w.db"
    with sqlite3.connect(path) as w:
        w.execute("CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
                  " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)")
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.execute("INSERT INTO flex_trade VALUES ('2026-08-04','flex','FUT','USD',100.0)")
        w.execute("INSERT INTO flex_lot VALUES ('20260804','FUT',100.0)")
    row = dd.realised_by_type(dd.connect(path), date(2026, 8, 4), date(2026, 8, 4))[0]
    assert row.win_loss_ratio is None
    assert row.win_rate == pytest.approx(100.0)


def test_rows_are_ordered_by_how_much_money_moved(tmp_path):
    """The class that moved the account most is read first, sign-independent."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6))
    assert [r.asset_class for r in rows][:2] == ["STK", "FUT"]


def test_an_empty_window_returns_nothing(tmp_path):
    """A quiet day must render "nothing realised", never a row of zeros."""
    assert dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 5), date(2026, 8, 5)) == ()


def test_average_win_and_loss_expose_what_the_win_rate_hides(tmp_path):
    """Count and money can tell opposite stories; the UI needs both.

    Modelled on the case the requirement names: one large win against several small
    losses is a poor win RATE and a good result. A surface showing only the rate would
    report it as a bad week.
    """
    path = tmp_path / "avg.db"
    with sqlite3.connect(path) as w:
        w.execute("CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
                  " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)")
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.execute("INSERT INTO flex_trade VALUES ('2026-08-04','flex','FUT','USD',2400.0)")
        w.executemany("INSERT INTO flex_lot VALUES ('20260804','FUT',?)",
                      [(3000.0,), (-120.0,), (-120.0,), (-120.0,), (-120.0,), (-120.0,)])
    row = dd.realised_by_type(dd.connect(path), date(2026, 8, 4), date(2026, 8, 4))[0]

    assert row.win_rate == pytest.approx(16.67, abs=0.01)   # reads as a disaster
    assert row.net == pytest.approx(2400.0)                 # was in fact a good day
    assert row.average_win == pytest.approx(3000.0)
    assert row.average_loss == pytest.approx(-120.0)
    assert row.win_loss_ratio == pytest.approx(5.0)


def test_averages_are_none_rather_than_zero_when_absent(tmp_path):
    """A window with no losses has no average loss; 0.0 would read as break-even trades."""
    rows = dd.realised_by_type(_breakdown_db(tmp_path), date(2026, 8, 4), date(2026, 8, 4))
    opt = next(r for r in rows if r.asset_class == "OPT")
    assert opt.average_win is None and opt.average_loss is None


# -- bridged_by_type: Flex plus the days it has not delivered -----------------


def _bridge_rec(**kw):
    """A stand-in Reconstruction with just the surface `bridged_by_type` consumes."""
    from types import SimpleNamespace
    base = {"realised": {}, "declined_days": frozenset(), "by_type": {}, "stats": {}}
    base.update(kw)

    return SimpleNamespace(
        realised=base["realised"],
        declined_days=base["declined_days"],
        by_type_for_day=lambda d: base["by_type"].get(d, {}),
        stats_for=lambda d, a: base["stats"].get((d, a), (0, 0, 0, 0.0, 0.0)),
    )


def test_the_bridge_adds_days_flex_has_not_delivered(tmp_path):
    """Flex was two days behind on 2026-08-06; the week was wrong by 10k without this."""
    rec = _bridge_rec(
        realised={("20260806", "FUT"): 1841.04},
        by_type={"20260806": {"FUT": 1841.04}},
        stats={("20260806", "FUT"): (2, 0, 0, 1841.04, 0.0)},
    )
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6),
                           rec, date(2026, 8, 4))
    fut = w.for_type("FUT")
    assert w.bridged_days == ("20260806",)
    assert fut.net == pytest.approx(-2926.18 + 1841.04, abs=0.005)
    assert (fut.winners, fut.losers) == (1 + 2, 2 + 0)


def test_a_day_flex_already_covers_is_never_double_counted(tmp_path):
    """The failure a naive "add today's live P&L" hits the morning Flex catches up.

    `coverage_through` is the cutoff: days up to it come from Flex, days after it from
    the reconstruction, and nothing is taken from both.
    """
    rec = _bridge_rec(
        realised={("20260804", "FUT"): 590.80},
        by_type={"20260804": {"FUT": 590.80}},
        stats={("20260804", "FUT"): (1, 1, 0, 1945.28, -1354.48)},
    )
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6),
                           rec, date(2026, 8, 4))
    assert w.bridged_days == ()
    assert w.for_type("FUT").net == pytest.approx(-2926.18, abs=0.005)


def test_a_declined_contract_marks_the_window_incomplete(tmp_path):
    """A short total on a P&L surface must announce itself.

    The reconstruction excludes contracts it could not match, so the bridged figure is a
    floor rather than a total, and the UI has to say so.
    """
    rec = _bridge_rec(
        realised={("20260806", "FUT"): 100.0},
        by_type={"20260806": {"FUT": 100.0}},
        stats={("20260806", "FUT"): (1, 0, 0, 100.0, 0.0)},
        declined_days=frozenset({"20260806"}),
    )
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6),
                           rec, date(2026, 8, 4))
    assert w.incomplete is True


def test_a_decline_outside_the_window_does_not_flag_it(tmp_path):
    """Otherwise every window inherits every problem the fill history ever had."""
    rec = _bridge_rec(declined_days=frozenset({"20260701"}))
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6),
                           rec, date(2026, 8, 4))
    assert w.incomplete is False


def test_without_a_reconstruction_the_window_is_flex_alone(tmp_path):
    """A gateway-down session still shows its settled history rather than nothing."""
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 3), date(2026, 8, 6))
    assert w.bridged_days == () and w.incomplete is False
    assert w.for_type("FUT").net == pytest.approx(-2926.18, abs=0.005)


def test_a_window_records_whether_a_reconstruction_was_available(tmp_path):
    """"Nothing closed" and "we could not look" are opposite claims on a P&L surface.

    Both arrive as a window with no rows, so without this flag the Daily window would assert
    "no round trips today" during a gateway outage — the one day it cannot know that.
    """
    conn = _breakdown_db(tmp_path)
    assert dd.bridged_by_type(conn, date(2026, 8, 7),
                              date(2026, 8, 7)).reconstructed is False
    assert dd.bridged_by_type(conn, date(2026, 8, 7), date(2026, 8, 7),
                              _bridge_rec(), date(2026, 8, 4)).reconstructed is True


def test_a_reconstruction_that_found_nothing_is_still_a_reconstruction(tmp_path):
    """A quiet day is an answer. The flag reports reachability, not activity."""
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 7), date(2026, 8, 7),
                           _bridge_rec(), date(2026, 8, 4))
    assert w.rows == () and w.bridged_days == ()
    assert w.reconstructed is True


def test_a_type_traded_only_live_still_gets_a_row(tmp_path):
    """Today's first-ever trade in a class must appear, not wait for the statement."""
    rec = _bridge_rec(
        realised={("20260806", "OPT"): 42.0},
        by_type={"20260806": {"OPT": 42.0}},
        stats={("20260806", "OPT"): (1, 0, 0, 42.0, 0.0)},
    )
    w = dd.bridged_by_type(_breakdown_db(tmp_path), date(2026, 8, 6), date(2026, 8, 6),
                           rec, date(2026, 8, 4))
    assert w.for_type("OPT").net == pytest.approx(42.0)
    assert w.for_type("OPT").win_rate == pytest.approx(100.0)


# -- Live quotes: /iserver/marketdata/snapshot --------------------------------


def test_a_quote_parses_the_three_fields_we_render():
    """31 Last, 82 Change, 83 Change % — IBKR returns them as strings."""
    q = dd.parse_quotes([{"conid": 8314, "31": "398.87", "82": "+9.33", "83": "2.39",
                          "6509": "RivB"}])[8314]
    assert (q.last, q.change, q.change_pct) == (398.87, 9.33, 2.39)
    assert q.is_live is True


def test_a_C_prefixed_last_is_the_prior_close_not_a_trade():
    """IBKR documents 31 as possibly prefixed: C = previous day's closing price.

    Rendering that as a live last would report yesterday's close as today's price on a
    quiet or pre-open instrument, which is exactly the staleness this whole change is
    fixing. The number is kept and flagged, never silently promoted.
    """
    q = dd.parse_quotes([{"conid": 1, "31": "C398.87", "6509": "RivB"}])[1]
    assert q.last == 398.87
    assert q.last_is_close is True


def test_an_H_prefixed_last_records_the_halt():
    """H = trading halted. A price during a halt is not a tradeable level."""
    q = dd.parse_quotes([{"conid": 1, "31": "H12.50"}])[1]
    assert q.last == 12.50 and q.halted is True


def test_the_preflight_response_yields_no_prices_and_does_not_pretend_otherwise():
    """The FIRST snapshot call for a conid returns only the conid — it opens the stream.

    Documented at ibkrcampus web-api/trading/market-data/top-of-book-snapshots. The
    poller does not sleep-and-retry inside a poll; the next 15s tick carries the data.
    """
    q = dd.parse_quotes([{"conid": 265598, "conidEx": "265598"}])[265598]
    assert q.last is None and q.change is None and q.change_pct is None


def test_a_delayed_or_unsubscribed_feed_is_not_reported_as_live():
    """6509 first char: R=RealTime, D=Delayed, N=NotSubscribed, Z=Frozen.

    A delayed price rendered as live is a wrong number on a trading surface; the flag is
    what lets the UI say which it is instead of guessing.
    """
    assert dd.parse_quotes([{"conid": 1, "31": "5", "6509": "DZ"}])[1].is_live is False
    assert dd.parse_quotes([{"conid": 1, "31": "5", "6509": "NB"}])[1].is_live is False
    assert dd.parse_quotes([{"conid": 1, "31": "5"}])[1].is_live is False


def test_an_unparseable_field_blanks_that_field_and_keeps_the_rest():
    """One malformed value must not discard the whole quote."""
    q = dd.parse_quotes([{"conid": 1, "31": "398.87", "82": "", "83": "n/a",
                          "6509": "RivB"}])[1]
    assert q.last == 398.87 and q.change is None and q.change_pct is None


def test_with_quotes_attaches_by_conid_and_leaves_unquoted_rows_alone():
    """A position with no quote keeps its IBKR fields — blank beats wrong."""
    pos = dd.parse_positions([
        {"conid": 1, "ticker": "A", "contractDesc": "A", "assetClass": "STK",
         "position": 1, "avgCost": 10.0, "avgPrice": 10.0, "multiplier": 1,
         "mktPrice": 11.0, "mktValue": 11.0, "unrealizedPnl": 1.0, "realizedPnl": 0,
         "currency": "USD"},
        {"conid": 2, "ticker": "B", "contractDesc": "B", "assetClass": "STK",
         "position": 1, "avgCost": 20.0, "avgPrice": 20.0, "multiplier": 1,
         "mktPrice": 21.0, "mktValue": 21.0, "unrealizedPnl": 1.0, "realizedPnl": 0,
         "currency": "USD"},
    ])
    out = dd.with_quotes(pos, {1: dd.Quote(conid=1, last=11.5, change=0.5,
                                           change_pct=4.5, status="RivB")})
    assert out[0].quote is not None and out[0].quote.last == 11.5
    assert out[1].quote is None
    assert out[1].market_price == 21.0


# ── outsideRTH on the order book (2026-09-04) ────────────────────────────────
#
# Measured the same day on the live account: /iserver/account/orders carries `outsideRTH`
# (not in IBKR's doc) — False on a resting AAPL GTC limit, None on a resting ES Sep-26 GTC
# limit. Three states on the read side, and None must stay None: an unreported attribute
# rendered as "No" would be a negative claim IBKR never made.


@pytest.mark.parametrize("raw, expected", [(True, True), (False, False), (None, None)])
def test_parse_orders_reads_outside_rth_as_three_states(raw, expected):
    """True/False verbatim, None (or absent) stays None."""
    orders = dd.parse_orders([{"orderId": "1", "ticker": "ES", "totalSize": 1, "outsideRTH": raw}])
    assert orders[0].outside_rth is expected


def test_parse_orders_outside_rth_absent_or_junk_is_none():
    """Only a real boolean is a claim; anything else is 'not reported'."""
    absent = dd.parse_orders([{"orderId": "1", "ticker": "ES", "totalSize": 1}])
    junk = dd.parse_orders([{"orderId": "2", "ticker": "ES", "totalSize": 1, "outsideRTH": "yes"}])
    assert absent[0].outside_rth is None
    assert junk[0].outside_rth is None
