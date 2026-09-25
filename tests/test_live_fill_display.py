"""The display fields of a live fill and the one UTC→ET clock rule (gap #68, Fills tab).

Fixture-free on purpose: `tests/test_live_realised.py` skips as a whole wherever its
git-ignored account fixture is absent (every CI run), so the rules pinned here would be
unguarded there. The raw rows below are shaped exactly as `/iserver/account/trades` sent
them on 2026-09-25 (the F round trip placed for gap #72), with the account id removed.
"""

from __future__ import annotations

import inspect

from claudia import execution_listener, live_realised
from claudia.live_realised import LiveFill, execution_time_et, parse_fills


def _raw(**over):
    """One raw execution as IBKR sent it on 2026-09-25 (BUY 1 F on IBKRATS)."""
    row = {
        "execution_id": "0000f73b.6ab682e8.01.01",
        "symbol": "F",
        "supports_tax_opt": "1",
        "side": "B",
        "order_description": "Bot 1 @ 12.685 on IBKRATS",
        "trade_time": "20260925-15:53:23",
        "trade_time_r": 1790351603000,
        "size": 1.0,
        "price": "12.685",
        "order_ref": "claudecode-gap72-buy-155311",
        "submitter": None,
        "exchange": "IBKRATS",
        "commission": "0.13",
        "net_amount": 12.685,
        "company_name": "FORD MOTOR CO",
        "contract_description_1": "F",
        "sec_type": "STK",
        "listing_exchange": "NYSE",
        "conid": 9599491,
        "conidEx": "9599491",
        "clearing_id": "IB",
        "clearing_name": "IB",
        "liquidation_trade": "0",
    }
    row.update(over)
    return row


def test_a_fill_carries_its_venue_and_order_ref_verbatim():
    """The Fills tab shows where a fill executed and the order it came from, as IBKR
    reports both — the two strings a trader checks against the chat message."""
    fill = parse_fills([_raw()])[0]
    assert fill.exchange == "IBKRATS"
    assert fill.order_ref == "claudecode-gap72-buy-155311"


def test_an_external_fill_has_an_empty_order_ref_not_the_word_none():
    """IBKR sends `order_ref: null` for an order placed outside the API (measured
    2026-09-25 on a fill from the web portal); a cell must not read "None"."""
    fill = parse_fills([_raw(order_ref=None)])[0]
    assert fill.order_ref == ""


def test_the_display_fields_are_optional_so_a_bare_row_still_parses():
    """A row without the display keys still parses: they carry no money and decide
    nothing, so their absence must not cost the reconstruction a fill."""
    row = _raw()
    del row["exchange"], row["order_ref"]
    fill = parse_fills([row])[0]
    assert fill.execution_id == "0000f73b.6ab682e8.01.01"
    assert (fill.exchange, fill.order_ref) == ("", "")


def test_every_existing_construction_of_a_live_fill_still_works():
    """The new fields default, so nothing that builds a `LiveFill` by hand changes."""
    fill = LiveFill(
        execution_id="x",
        conid=1,
        symbol="F",
        asset_class="STK",
        signed_quantity=1.0,
        price=12.685,
        commission=0.13,
        multiplier=1.0,
        trade_time="20260925-15:53:23",
    )
    assert (fill.exchange, fill.order_ref) == ("", "")


# ── The clock rule ──────────────────────────────────────────────────────────────


def test_execution_time_et_converts_ibkr_utc_to_new_york():
    """IBKR documents `trade_time` as UTC; the operator reads Eastern. 15:53:23Z on a
    September day is 11:53:23 EDT."""
    assert execution_time_et("20260925-15:53:23") == "11:53:23 ET"


def test_execution_time_et_can_carry_the_date_the_fill_happened_on_in_eastern_time():
    """The Fills tab shows the execution date and time (spec 2026-09-24, part 4): the
    ET date, so an evening fill is dated the evening it happened, not the UTC morning."""
    assert execution_time_et("20260925-15:53:23", with_date=True) == "2026-09-25 11:53:23 ET"
    # 01:10Z on 15 Jan is 20:10 EST on 14 Jan: the date shown must follow the zone.
    assert execution_time_et("20260115-01:10:00", with_date=True) == "2026-01-14 20:10:00 ET"


def test_an_unparseable_time_renders_blank_never_a_guess():
    """Anything but IBKR's documented `YYYYMMDD-HH:MM:SS` renders as nothing."""
    assert execution_time_et("") == ""
    assert execution_time_et("2026-09-25 15:53:23") == ""
    assert execution_time_et("garbage", with_date=True) == ""


def test_the_chat_report_and_the_fills_tab_share_one_clock_rule():
    """The two surfaces exist to check each other (operator 2026-09-24), so their times
    must come from one conversion, not two that agree today. The listener imports the
    rule and keeps no `strptime` of its own."""
    assert vars(execution_listener)["execution_time_et"] is live_realised.execution_time_et
    assert "strptime" not in inspect.getsource(execution_listener.ExecutionReport.from_event)


def test_a_fill_carries_ibkrs_contract_description():
    """`contract_description_1` is the month text of a future ("Dec18 '26", "Nov'26",
    measured 2026-09-25) and the ticker of a stock; the Fills tab's Name column appends it
    to the contract's name for a future, as the Orders tab appends `description1`."""
    assert parse_fills([_raw()])[0].description == "F"
    assert parse_fills([_raw(contract_description_1="Dec18 '26")])[0].description == "Dec18 '26"


def test_the_description_is_optional_and_never_the_word_none():
    """Absent or null, it parses as an empty string, like the other display fields."""
    assert parse_fills([_raw(contract_description_1=None)])[0].description == ""
    row = _raw()
    del row["contract_description_1"]
    assert parse_fills([row])[0].description == ""
