"""Tests for claudia/opening_status.py — UI-free builders for the Panel opening
status message (Task 5.3). Fixtures mirror the real shapes: toolkit.execute
returns (text, None) 2-tuples (claude_tools.py:1048); get_trade_date_coverage /
get_market_calendar_context return the dict shapes the removed Chainlit app.py:426-513
consumed (the port's parity source)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from claudia.opening_status import (
    BROKERAGE_SESSION_DOWN,
    OFFLINE_STATUS,
    build_trade_lines,
    gather_session_state,
)


def _make_toolkit(flex: bool = True) -> MagicMock:
    """A stub toolkit whose Flex configuration and market calendar can be posed."""
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
async def test_a_healthy_session_says_nothing_because_the_dashboard_says_it_all():
    """2026-08-05, user's call: every account figure is externalised to the dashboard.

    Account Summary, Open Positions and Account P&L went that morning — Chainlit-era
    blocks, written when chat was the only surface. The working order book went that
    afternoon, once the dashboard's Orders tab polled it live. Same argument each time:
    chat prints once, the dashboard polls every 15s, so a startup copy is stale on
    arrival, and two surfaces disagreeing about the account is the exact failure the
    2026-08-04 status work was about.

    So a healthy start contributes NO text — the empty string, not a heading with nothing
    under it — and, just as importantly, no IBKR data call. `ping()` is the whole probe.
    """
    toolkit = MagicMock()
    toolkit.client.ping.return_value = True
    toolkit.execute.side_effect = lambda name, inputs: (f"{name} text", None)
    caveat, offline = await gather_session_state(toolkit)

    assert (caveat, offline) == ("", False)
    # No account fetch of any kind at startup — not the three retired blocks, and since
    # this afternoon not get_live_orders either.
    toolkit.execute.assert_not_called()


@pytest.mark.asyncio
async def test_gather_session_state_offline_when_nothing_answers():
    """Neither the brokerage session nor the account endpoints — the real offline case.

    Every status call must be SKIPPED: `toolkit.execute` swallows exceptions into error
    strings, so calling it here would render a column of error blobs under real headings.
    """
    toolkit = MagicMock()
    toolkit.client.ping.return_value = False
    toolkit.client.get_accounts.return_value = []
    block, offline = await gather_session_state(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS
    toolkit.execute.assert_not_called()


@pytest.mark.asyncio
async def test_account_data_is_shown_when_only_the_brokerage_session_is_down():
    """The state that used to put a contradiction on screen.

    Measured live 2026-08-04: `ping()` False, `/portfolio/{id}/ledger` serving live
    figures, `/iserver/account/orders` returning `{"error": "Bad Request: no bridge"}`.
    The chat said "IBKR gateway not connected" while the dashboard drew live balances
    from the endpoints that were answering.

    This is the one state that still produces text, and it is why the caveat survived the
    2026-08-05 cleanup that removed every rendered figure: the dashboard cannot report a
    session that is down — it just shows the balances it *can* still reach — so nothing
    else on screen names the missing half. The message must say what is unavailable and
    why, and must not imply the account is unreadable when it plainly is.
    """
    toolkit = MagicMock()
    toolkit.client.ping.return_value = False
    toolkit.client.get_accounts.return_value = [{"accountId": "U1234567"}]
    toolkit.execute.side_effect = lambda name, inputs: (f"{name} text", None)
    block, offline = await gather_session_state(toolkit)

    assert offline is True  # order actions really are unavailable
    assert block == BROKERAGE_SESSION_DOWN
    # The account IS readable here, and the dashboard is drawing it — so the message must
    # point at that rather than imply there is no data.
    assert "dashboard" in BROKERAGE_SESSION_DOWN
    assert "**Live Orders**" not in block
    # `get_live_orders` must NOT be called: without a brokerage session it returns IBKR's
    # "no bridge" 400, and rendering that under a heading reads as an account fault.
    toolkit.execute.assert_not_called()


@pytest.mark.asyncio
async def test_offline_message_and_dashboard_agree_that_there_is_no_data():
    """One outage, one story. The blank dashboard and this line must not contradict."""
    assert "no account data" in OFFLINE_STATUS
    assert "dashboard is blank" in OFFLINE_STATUS
    assert "not connected" not in OFFLINE_STATUS  # the claim that was too broad


@pytest.mark.asyncio
async def test_gather_session_state_offline_when_ping_raises():
    """An unreachable gateway is reported as offline with the standard caveat, not as an error."""
    toolkit = MagicMock()
    toolkit.client.ping.side_effect = ConnectionError("gateway down")
    block, offline = await gather_session_state(toolkit)
    assert offline is True
    assert block == OFFLINE_STATUS


def test_build_trade_lines_flex_not_configured_still_appends_calendar():
    """With Flex unconfigured no coverage lookup runs, but the calendar still reaches context."""
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
    """A covered dataset reports its trade count and refresh date, with no refresh nudge."""
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
    """Offline, the line says the data cannot be refreshed rather than implying it is current."""
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
    """An empty dataset says so instead of reporting zero trades as a fact about the account."""
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
    """A coverage lookup failure degrades to a neutral syncing line rather than failing."""
    toolkit = _make_toolkit()
    toolkit._store.get_trade_date_coverage.side_effect = RuntimeError("db locked")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert status == "Trade history: syncing…"
    assert context is None  # calendar mock returns None → nothing appended


def test_build_trade_lines_calendar_error_is_swallowed():
    """A calendar failure is swallowed — it is optional context, not a reason to fail a session."""
    toolkit = _make_toolkit(flex=False)
    toolkit._store.get_market_calendar_context.side_effect = RuntimeError("boom")
    status, context = build_trade_lines(toolkit, ibkr_offline=False)
    assert "Flex not configured" in status
    assert context is None


# ── "integrity validated" has to mean something (2026-08-05) ──────────────────
#
# The opening line has always ended "…, integrity validated" and the system prompt has
# always told the model "Dataset is complete and verified — no missing imports". Nothing
# checked. The only thing consulted, `get_trade_date_coverage`, calls itself an ACTIVITY
# REPORT in its own docstring — it counts trades and finds date gaps. Claiming validation
# on the strength of a row count is the "heuristic dressed as a fact" this project
# explicitly forbids, and it was being asserted to the user AND to the model.


_VALIDATED_AT = datetime(2026, 8, 5, 12, 18, tzinfo=UTC)


def _outcome(ok: bool = True, empty: bool = False, reused: bool = False):
    """A `ValidationOutcome` in the shape `validate_dataset_daily` returns."""
    from claudia.flex_sync import DatasetCheck, DatasetValidity, ValidationOutcome

    if empty:
        validity = DatasetValidity((), empty=True)
    elif ok:
        validity = DatasetValidity((DatasetCheck("file integrity", True, "ok"),))
    else:
        validity = DatasetValidity(
            (DatasetCheck("execution_key is unique", False, "75 duplicated key(s)"),)
        )
    return ValidationOutcome(validity=validity, validated_at=_VALIDATED_AT, reused=reused)


def _covered_toolkit():
    """A stub toolkit reporting a covered, non-stale Flex dataset."""
    toolkit = _make_toolkit()
    toolkit._config.sqlite_path = "/tmp/store.db"
    toolkit._store.get_trade_date_coverage.return_value = {
        "oldest": "2024-01-02", "newest": "2026-07-22",
        "total_trades": 1234, "days_since_newest": 1,
    }
    return toolkit


def test_the_validated_claim_is_made_only_when_the_checks_actually_ran():
    """The word "validated" appears only when the checks really ran — the claim is earned."""
    with patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome(ok=True)):
        status, context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)
    assert "integrity validated" in status
    assert context is not None and "verified" in context


def test_a_failing_dataset_says_so_instead_of_claiming_validation():
    """A failing dataset is reported as failing, never as validated."""
    with patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome(ok=False)):
        status, context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    assert "integrity validated" not in status
    assert "75 duplicated key(s)" in status
    # and the model must not be told the dataset is complete and verified
    assert context is not None
    assert "complete and verified" not in context
    assert "FAILED" in context or "failed" in context


def test_an_unvalidated_dataset_makes_no_claim_either_way():
    """`empty` is neither a pass nor a failure. A first run has nothing to validate and
    must not open with an integrity alarm — nor with a validation it did not perform."""
    with patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome(empty=True)):
        status, _context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)
    assert "integrity validated" not in status
    assert "duplicated" not in status


def test_validation_blowing_up_never_takes_down_the_opening_status():
    """A validation crash degrades the line rather than failing session init."""
    with patch("claudia.opening_status.validate_dataset_daily", side_effect=RuntimeError("boom")):
        status, _context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)
    assert "1234 trades" in status
    assert "integrity validated" not in status  # unproven is not proven


# ── "already updated on T — say so, don't check again" (user rule, 2026-08-05) ─


def test_a_reused_verdict_says_when_it_was_proven_and_that_nothing_was_rechecked():
    """Transparency over implication. Flex is T+1 and the store is pulled once a day, so
    the second session of the day validates nothing — and a bare "integrity validated"
    would be true of the data while implying a check that did not just happen."""
    with patch("claudia.opening_status.validate_dataset_daily",
               return_value=_outcome(ok=True, reused=True)):
        status, context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    assert "not re-checked" in status
    assert "unchanged since" in status
    local = _VALIDATED_AT.astimezone().strftime("%H:%M")
    assert local in status          # the time it was PROVEN, in the reader's timezone
    assert local in (context or "")  # and the model is told the same thing


def test_a_fresh_verdict_does_not_claim_to_be_reused():
    """A freshly-proven verdict is not described as reused."""
    with patch("claudia.opening_status.validate_dataset_daily",
               return_value=_outcome(ok=True, reused=False)):
        status, _ = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    assert "integrity validated" in status
    assert "not re-checked" not in status


def test_the_line_reports_when_the_store_was_updated_not_the_newest_trade_date():
    """These are different things and T+1 puts a day between them. The line used to read
    "last refreshed 2026-08-04" for a store updated on 08-05 at 08:18 — telling the user
    it was a day staler than it was."""
    from claudia.flex_sync import LastImport

    imported = LastImport(at=datetime(2026, 8, 5, 12, 18, tzinfo=UTC),
                          filename="flex_U1675699_2026-08-05.xml", trade_count=105)
    with (
        patch("claudia.opening_status.last_import", return_value=imported),
        patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome()),
    ):
        status, _ = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    stamp = imported.at.astimezone().strftime("%Y-%m-%d %H:%M")
    assert f"updated {stamp}" in status
    assert "last refreshed" not in status  # the mislabel is gone, not merely supplemented


def test_a_store_that_never_recorded_an_import_keeps_the_old_wording():
    """No import log is not a reason to print nothing — fall back rather than go silent."""
    with (
        patch("claudia.opening_status.last_import", return_value=None),
        patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome()),
    ):
        status, _ = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    assert "last refreshed 2026-07-22" in status


def test_the_update_stamp_names_its_timezone():
    """The same rule that forbids a bare $ forbids a bare clock time."""
    from claudia.flex_sync import LastImport

    imported = LastImport(at=datetime(2026, 8, 5, 12, 18, tzinfo=UTC), filename="x", trade_count=1)
    with (
        patch("claudia.opening_status.last_import", return_value=imported),
        patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome()),
    ):
        status, _ = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    zone = imported.at.astimezone().strftime("%Z")
    assert zone and zone in status


def test_the_model_is_told_the_same_update_time_as_the_user():
    """Two surfaces, one fact. The status line was corrected on 2026-08-05 while the
    system-prompt copy still read "Last refreshed: {newest trade date}" — so the model
    was reasoning about staleness from a date a full day behind what the user could see.
    """
    from claudia.flex_sync import LastImport

    imported = LastImport(at=datetime(2026, 8, 5, 12, 18, tzinfo=UTC), filename="x", trade_count=105)
    with (
        patch("claudia.opening_status.last_import", return_value=imported),
        patch("claudia.opening_status.validate_dataset_daily", return_value=_outcome()),
    ):
        status, context = build_trade_lines(_covered_toolkit(), ibkr_offline=False)

    stamp = imported.at.astimezone().strftime("%Y-%m-%d %H:%M")
    assert context is not None
    assert stamp in context and stamp in status      # same moment on both surfaces
    assert "Last refreshed: 2026-07-22" not in context  # the mislabel, gone from here too
    assert "newest trade date 2026-07-22" in context    # stated as what it is
