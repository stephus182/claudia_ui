"""Tests for claudia/opening_status.py — UI-free builders for the Panel opening
status message (Task 5.3). Fixtures mirror the real shapes: toolkit.execute
returns (text, None) 2-tuples (claude_tools.py:1048); get_trade_date_coverage /
get_market_calendar_context return the dict shapes the removed Chainlit app.py:426-513
consumed (the port's parity source)."""

from unittest.mock import MagicMock, patch

import pytest

from claudia.opening_status import (
    BROKERAGE_SESSION_DOWN,
    OFFLINE_STATUS,
    build_trade_lines,
    gather_status_block,
)


def _make_toolkit(flex: bool = True) -> MagicMock:
    toolkit = MagicMock()
    toolkit._config.flex_token = "tok" if flex else ""
    toolkit._config.flex_query_id = "qid" if flex else ""
    toolkit._store.get_market_calendar_context.return_value = None
    return toolkit


_MKT = {
    "today": "2026-07-23",
    "is_trading_day": True,
    "last_trading_day": "2026-07-22",
    "next_trading_day": "2026-07-24",
    "holidays_by_exchange": {"XNYS": ["2026-12-25"], "CME": []},
    "futures": {
        "note": "CME futures trade nearly 23h/day.",
        "maintenance_break_ct": "16:00-17:00 CT",
        "cme_open_nyse_closed": ["2026-11-27"],
        "product_groups": {
            "equity_index": {
                "exchange": "CME",
                "globex_hours_ct": "17:00-16:00",
                "products": ["ES", "NQ", "YM", "RTY", "MES"],
                "note": "daily maintenance 16:00-17:00",
            }
        },
    },
}


@pytest.mark.asyncio
async def test_gather_status_block_happy_path_contains_all_four_sections():
    toolkit = MagicMock()
    toolkit.client.ping.return_value = True
    toolkit.execute.side_effect = lambda name, inputs: (f"{name} text", None)
    with patch("claudia.opening_status.get_live_pnl_text", return_value="pnl text"):
        block, offline = await gather_status_block(toolkit)
    assert offline is False
    assert "**Account Summary**\nget_account_summary text" in block
    assert "**Open Positions**\nget_positions text" in block
    assert "**Account P&L**\npnl text" in block
    assert "**Live Orders**\nget_live_orders text" in block


@pytest.mark.asyncio
async def test_gather_status_block_offline_when_nothing_answers():
    """Neither the brokerage session nor the account endpoints — the real offline case.

    Every status call must be SKIPPED: `toolkit.execute` swallows exceptions into error
    strings, so calling it here would render a column of error blobs under real headings.
    """
    toolkit = MagicMock()
    toolkit.client.ping.return_value = False
    toolkit.client.get_accounts.return_value = []
    block, offline = await gather_status_block(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS
    toolkit.execute.assert_not_called()


@pytest.mark.asyncio
async def test_account_data_is_shown_when_only_the_brokerage_session_is_down():
    """The state that used to put a contradiction on screen.

    Measured live 2026-08-04: `ping()` False, `/portfolio/{id}/ledger` serving live
    figures, `/iserver/account/orders` returning `{"error": "Bad Request: no bridge"}`.
    The chat said "IBKR gateway not connected" while the dashboard drew live balances
    from the endpoints that were answering. The account block must be rendered, the
    reason for the missing half named, and `get_live_orders` NOT called — its "no bridge"
    400 under a **Live Orders** heading reads as an account fault rather than a session
    one.
    """
    toolkit = MagicMock()
    toolkit.client.ping.return_value = False
    toolkit.client.get_accounts.return_value = [{"accountId": "U1234567"}]
    toolkit.execute.side_effect = lambda name, inputs: (f"{name} text", None)
    with patch("claudia.opening_status.get_live_pnl_text", return_value="pnl text"):
        block, offline = await gather_status_block(toolkit)
    assert offline is True  # order actions really are unavailable
    assert "**Account Summary**\nget_account_summary text" in block
    assert "**Open Positions**\nget_positions text" in block
    assert block.endswith(BROKERAGE_SESSION_DOWN)
    assert "live" in BROKERAGE_SESSION_DOWN
    assert "**Live Orders**" not in block
    assert [c.args[0] for c in toolkit.execute.call_args_list] == [
        "get_account_summary", "get_positions",
    ]


@pytest.mark.asyncio
async def test_offline_message_and_dashboard_agree_that_there_is_no_data():
    """One outage, one story. The blank dashboard and this line must not contradict."""
    assert "no account data" in OFFLINE_STATUS
    assert "dashboard is blank" in OFFLINE_STATUS
    assert "not connected" not in OFFLINE_STATUS  # the claim that was too broad


@pytest.mark.asyncio
async def test_gather_status_block_offline_when_ping_raises():
    toolkit = MagicMock()
    toolkit.client.ping.side_effect = ConnectionError("gateway down")
    block, offline = await gather_status_block(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS


def test_build_trade_lines_flex_not_configured_still_appends_calendar():
    toolkit = _make_toolkit(flex=False)
    toolkit._store.get_market_calendar_context.return_value = _MKT
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "Flex not configured" in status
    toolkit._store.get_trade_date_coverage.assert_not_called()
    # app.py:511 subtlety: the calendar block lands in trade_context even when
    # Flex is unconfigured — (trade_context or "") + _cal_block.
    assert context is not None
    assert "## Market Calendar" in context
    assert "NYSE: 2026-12-25" in context
    assert "CME Futures: no holidays this year/next" in context
    assert "Equity Index (CME): 17:00-16:00 [ES, NQ, YM, RTY…]" in context
    assert "CME open when NYSE is closed: 2026-11-27" in context


def test_build_trade_lines_flex_configured_with_data():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": "2024-01-02",
        "newest": "2026-07-22",
        "total_trades": 1234,
        "days_since_newest": 1,
    }
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "1234 trades" in status
    assert "last refreshed 2026-07-22" in status
    assert "connect IBKR to refresh" not in status
    assert context is not None
    assert "## Trade History" in context
    assert "1234 executions from 2024-01-02 to 2026-07-22" in context


def test_build_trade_lines_offline_notes_connect_to_refresh():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": "2024-01-02",
        "newest": "2026-07-22",
        "total_trades": 1234,
        "days_since_newest": 1,
    }
    status, _context = build_trade_lines(toolkit, ibkr_offline=True)
    assert "(1d ago) — connect IBKR to refresh" in status


def test_build_trade_lines_no_data_yet():
    toolkit = _make_toolkit()
    # Real empty-store return shape from SQLiteStore.get_trade_date_coverage (store.py:355).
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": None,
        "newest": None,
        "total_trades": 0,
        "gaps": [],
    }
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "no data yet" in status
    assert context is not None
    assert "sync_flex_trades" in context


def test_build_trade_lines_coverage_error_degrades_to_syncing():
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.side_effect = RuntimeError("db locked")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert status == "Trade history: syncing…"
    assert context is None  # calendar mock returns None → nothing appended


def test_build_trade_lines_calendar_error_is_swallowed():
    toolkit = _make_toolkit(flex=False)
    toolkit._store.get_market_calendar_context.side_effect = RuntimeError("boom")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "Flex not configured" in status
    assert context is None


# ── positions ↔ ledger reconciliation (2026-08-03 live finding) ───────────────

_POSITIONS = """Open positions (4):

| Symbol | Qty | Mkt Val | Unrealized P&L |
|---|---|---|---|
| GLD | 100.0 | $37,174.66 | **-$1,152.43** |
| CL       SEP2026 | 2.0 | $162,280.00 | **+$415.28** |
| CRM | 50.0 | $9,245.00 | **-$3,009.91** |
| IGV | 100.0 | $9,871.97 | **+$108.55** |"""

_LEDGER = """Account Ledger (USD):
  Net Liquidation Value : **$68,827.42**
  Cash Balance          : $12,559.65
  Stock Market Value    : $56,291.63
  Unrealized P&L        : **-$3,638.52**
  Realized P&L          : **+$590.80**"""


def test_reconcile_is_silent_when_the_numbers_agree():
    """The real 2026-08-03 figures: -3,638.51 summed vs -3,638.52 in the ledger.

    Guards against a vacuous pass. The first version of this test went green
    because the row regex lacked re.MULTILINE, matched nothing, and the function
    returned None for "cannot parse" -- indistinguishable from "agrees". The
    parser assertion below is what makes the silence meaningful.
    """
    from claudia.opening_status import _POSITION_ROW, reconcile_positions_against_ledger

    assert len(_POSITION_ROW.findall(_POSITIONS)) == 4  # all four rows actually parsed
    assert reconcile_positions_against_ledger(_POSITIONS, _LEDGER) is None


def test_reconcile_flags_a_real_discrepancy():
    from claudia.opening_status import reconcile_positions_against_ledger

    ledger = _LEDGER.replace("-$3,638.52", "-$3,010.00")  # well past the threshold
    warning = reconcile_positions_against_ledger(_POSITIONS, ledger)
    assert warning is not None
    assert "628.51" in warning
    assert "USD" in warning  # never a bare $


def test_reconcile_tolerance_absorbs_futures_drift_but_not_a_missing_position():
    """$250, calibrated against live evidence rather than picked for neatness.

    Measured 2026-08-03: `get_positions` went stale while CL kept ticking in the
    ledger, leaving the two $59.99 apart, unchanged, for 52 seconds. A 5-cent
    alarm would fire through most of CL's ~23h session on correct data. $250 is
    about 4x the largest observed lag and still well under any missing position
    in this account.
    """
    from claudia.opening_status import (
        _RECONCILE_TOLERANCE,
        reconcile_positions_against_ledger,
    )

    assert _RECONCILE_TOLERANCE == 250.00

    # the real observed futures lag must stay silent (sum -3,638.51)
    for drift in ("-$3,578.52", "-$3,698.50"):  # ~60 USD either side
        assert reconcile_positions_against_ledger(
            _POSITIONS, _LEDGER.replace("-$3,638.52", drift)
        ) is None

    # a dropped IGV position (+108.55) still hides under the threshold -- stated,
    # not pretended otherwise; that is the cost of the wider tolerance
    assert reconcile_positions_against_ledger(
        _POSITIONS, _LEDGER.replace("-$3,638.52", "-$3,747.06")
    ) is None

    # a dropped CRM position (-3,009.91) is caught
    warning = reconcile_positions_against_ledger(
        _POSITIONS, _LEDGER.replace("-$3,638.52", "-$628.60")
    )
    assert warning is not None and "3,009.91 USD" in warning


def test_reconcile_stays_silent_when_it_cannot_parse():
    """Fail safe, not loud: an unparseable block must not cry wolf.

    These are display strings owned by another repo. A format change should
    silently disable the check, not produce a false integrity alarm on every
    session start.
    """
    from claudia.opening_status import reconcile_positions_against_ledger

    assert reconcile_positions_against_ledger("no positions here", _LEDGER) is None
    assert reconcile_positions_against_ledger(_POSITIONS, "no ledger here") is None
    assert reconcile_positions_against_ledger("", "") is None
    assert reconcile_positions_against_ledger("Open positions (0):", _LEDGER) is None


def test_reconcile_warning_names_the_currency_caveat():
    """The positions table shows a bare $ with no ISO code.

    A position denominated in another currency would make the sum invalid and
    trip this check. IGV once priced a US ETF in MXN, so that is a real path,
    and the warning must not assert a data error it cannot distinguish from a
    currency mix.
    """
    from claudia.opening_status import reconcile_positions_against_ledger

    warning = reconcile_positions_against_ledger(
        _POSITIONS, _LEDGER.replace("-$3,638.52", "-$3,010.00")
    )
    assert "currency" in warning.lower()


def test_reconcile_tolerance_survives_binary_float():
    """Exactly-at-tolerance money must not trip the alarm.

    Summing the real positions gives -3638.5099999999998, not -3638.51, so a delta
    of exactly five cents measures 0.0500000000001819. Comparing that raw against
    0.05 fires a false integrity warning by 1.8e-13.
    """
    from claudia.opening_status import reconcile_positions_against_ledger

    vals = [-1152.43, 415.28, -3009.91, 108.55]
    raw_delta = abs(sum(vals) - (-3888.51))
    assert raw_delta > 250.00  # the trap this guards
    assert round(raw_delta, 2) == 250.00

    assert reconcile_positions_against_ledger(
        _POSITIONS, _LEDGER.replace("-$3,638.52", "-$3,888.51")
    ) is None


@pytest.mark.asyncio
async def test_gather_status_block_surfaces_a_reconciliation_mismatch():
    """The check must actually run in the opening status, not just exist."""
    toolkit = MagicMock()
    toolkit.client.ping.return_value = True
    toolkit.execute.side_effect = lambda name, inputs: (
        (_POSITIONS, None) if name == "get_positions" else (f"{name} text", None)
    )
    bad_ledger = _LEDGER.replace("-$3,638.52", "-$3,010.00")
    with patch("claudia.opening_status.get_live_pnl_text", return_value=bad_ledger):
        block, offline = await gather_status_block(toolkit)

    assert offline is False
    assert "does not reconcile" in block
    assert "628.51 USD" in block
    # ordering: the warning sits with the numbers it is about, above Live Orders
    assert block.index("does not reconcile") < block.index("**Live Orders**")


@pytest.mark.asyncio
async def test_gather_status_block_silent_when_figures_reconcile():
    toolkit = MagicMock()
    toolkit.client.ping.return_value = True
    toolkit.execute.side_effect = lambda name, inputs: (
        (_POSITIONS, None) if name == "get_positions" else (f"{name} text", None)
    )
    with patch("claudia.opening_status.get_live_pnl_text", return_value=_LEDGER):
        block, _ = await gather_status_block(toolkit)

    assert "does not reconcile" not in block
