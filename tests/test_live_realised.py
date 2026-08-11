"""Tests for `claudia.live_realised`, pinned against REAL fills captured 2026-08-06.

`tests/fixtures/live_fills_2026-08-06.json` is the real execution history from the live
account (account identifiers stripped). It was captured with a 30-day request, but every
fill in it falls on 2026-08-03..08-06 — inside the 7-day window `IBKRClient.get_trades()`
actually uses — so it is representative of what production sees, not wider than it.
Using it rather than hand-written rows is
deliberate: the defect this module was written around — the opening commission being
released at close rather than at fill — produced a difference of **4.60 on one day and
0.12 on another**. Invented fixtures would have been built from whatever convention the
author already believed, and would have agreed with the bug.

The three validation legs from
`docs/plans/2026-08-06-gateway-session-lifecycle-owner.md`'s sibling P&L work are all
asserted here against figures obtained independently of this code:

  * Flex, on settled days — -3,516.98 and 590.80
  * ledger `realizedpnl`, on today — 945.52
  * IBKR's own position quantities — the trust check
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from claudia.live_realised import (
    LiveFill,
    days_after,
    parse_fills,
    reconstruct,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "live_fills_2026-08-06.json"

# The fixture is REAL account data — 24 executions with prices, sizes and order ids — so
# it is git-ignored and lives only on machines that captured it (and on Drive). This repo
# is public; publishing a trading history to prove an arithmetic convention is not a trade
# worth making.
#
# Skipping rather than failing is the point: a fresh clone must produce a clean run, not a
# wall of red that trains everyone to ignore it. What is lost is stated plainly instead of
# hidden — these tests are the only ones pinning the commission convention against real
# Flex figures, so a run without them is weaker and says so.
pytestmark = pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason=(
        "tests/fixtures/live_fills_2026-08-06.json is absent — it is git-ignored account "
        "data. Re-capture it from a live gateway to run the reconstruction tests."
    ),
)

@pytest.fixture
def fills():
    """The real 23 executions, typed."""
    return parse_fills(json.loads(_FIXTURE.read_text()))


def _conid(fills, symbol):
    """The conid the fixture uses for `symbol`."""
    return next(f.conid for f in fills if f.symbol == symbol)


@pytest.fixture
def positions(fills):
    """IBKR's positions at the instant the fixture was captured, by real conid.

    ES is **flat** because ClaudIA's own 15:41:41 order closed the short. These are read
    from `/portfolio/{id}/positions`, not derived here — a trust check against numbers
    this code produced would check nothing.
    """
    return {
        _conid(fills, "ES"): 0.0,    # closed by ClaudIA's fill; every leg in-window
        _conid(fills, "CL"): 0.0,    # flat, every leg in-window
        _conid(fills, "CRM"): 0.0,   # opened before the window -> declined
        _conid(fills, "GLD"): 50.0,  # opened before the window -> declined
    }


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_the_fixture_is_the_real_execution_history(fills):
    """24 fills, four instruments — a canary on the fixture being replaced by a toy."""
    assert len(fills) == 24
    assert {f.symbol for f in fills} == {"ES", "CL", "CRM", "GLD"}


def test_a_sell_parses_as_a_negative_quantity(fills):
    """Short opens must not be a special case anywhere downstream."""
    sells = [f for f in fills if f.signed_quantity < 0]
    assert sells and all(not f.is_buy for f in sells)


def test_the_multiplier_is_derived_per_contract(fills):
    """ES 50, CL 1000, equities 1 — from `net_amount / price / size`, never a table."""
    by_symbol = {f.symbol: f.multiplier for f in fills}
    assert by_symbol["ES"] == 50.0
    assert by_symbol["CL"] == 1000.0
    assert by_symbol["GLD"] == 1.0


def test_a_fill_without_a_usable_price_is_dropped_not_defaulted():
    """A defaulted multiplier understates a futures fill by 50x or 1000x, silently."""
    assert parse_fills([{"conid": 1, "size": 1, "price": 0, "net_amount": 5}]) == ()
    assert parse_fills([{"conid": 1, "size": 1}]) == ()


# ── Leg 1: agreement with Flex on settled days ───────────────────────────────


@pytest.mark.parametrize(
    "day,asset,expected",
    [("20260803", "FUT", -3516.98), ("20260804", "FUT", 590.80)],
)
def test_reconstruction_reproduces_flex_to_the_cent(fills, positions, day, asset, expected):
    """The commission convention, pinned.

    Charging the opening commission at fill time instead of at close gave -3,521.58 and
    590.68 for these two days — off by 4.60 and 0.12. Flex says -3,516.98 and 590.80.
    """
    result = reconstruct(fills, positions)
    assert result.realised[(day, asset)] == pytest.approx(expected, abs=0.005)


# ── Leg 2: agreement with ledger realizedpnl on today ────────────────────────


def test_todays_realised_matches_the_ledger(fills, positions):
    """1,841.04 across two ES round trips, matching IBKR at the moment of capture.

      * short opened 08-05 22:22:57 @ 7765.00, closed 08-06 13:34:12 @ 7746.00 -> 945.52
      * short opened 08-06 13:36:27 @ 7755.00, closed 08-06 15:41:41 @ 7737.00 -> 895.52
        (the second close is ClaudIA's own order, ref CLAUDIA-1786030884173)

    Ledger `realizedpnl` and the ES position's `realizedPnl` both read **1841.04** at
    capture time — two vendor figures this reconstruction did not produce.
    """
    result = reconstruct(fills, positions)
    assert result.total_for_day("20260806") == pytest.approx(1841.04, abs=0.005)
    assert result.by_type_for_day("20260806") == {"FUT": pytest.approx(1841.04, abs=0.005)}


def test_a_short_opened_and_closed_across_two_days_is_realised_on_the_closing_day():
    """The open was 2026-08-05, the close 2026-08-06, and the P&L belongs to the close.

    A same-day-only window would find no opening leg and report nothing.
    """
    fills = parse_fills([
        {"execution_id": "a", "conid": 1, "symbol": "ES", "sec_type": "FUT", "side": "S",
         "size": 1, "price": "7765.00", "net_amount": 388250.0, "commission": "2.24",
         "trade_time": "20260805-22:22:57"},
        {"execution_id": "b", "conid": 1, "symbol": "ES", "sec_type": "FUT", "side": "B",
         "size": 1, "price": "7746.00", "net_amount": 387300.0, "commission": "2.24",
         "trade_time": "20260806-13:34:12"},
    ])
    result = reconstruct(fills, {1: 0.0})
    assert result.total_for_day("20260805") == 0.0
    assert result.total_for_day("20260806") == pytest.approx(945.52, abs=0.005)


# ── Leg 3: the trust check ───────────────────────────────────────────────────


def test_contracts_opened_before_the_window_are_declined(fills, positions):
    """The whole STK discrepancy, and why it must not be published.

    IGV, CRM and GLD were all opened before the fill window, so their closing fills have
    no opening leg. Reconstructed STK for 2026-08-04 came out at 0.00 against Flex's
    -3,249.70 — an understatement of the entire position.
    """
    result = reconstruct(fills, positions)
    assert set(result.declined) >= {"CRM", "GLD"}
    assert "20260804" in result.declined_days
    assert result.total_for_day("20260804") is None


def test_a_declined_contract_contributes_nothing(fills, positions):
    """Excluded, not merely flagged — a flagged-but-included figure is still wrong."""
    result = reconstruct(fills, positions)
    assert ("20260804", "STK") not in result.realised


def test_trusted_contracts_survive_a_neighbours_decline(fills, positions):
    """FUT on 08-03 is trustworthy even though equities on 08-04 are not.

    Declining a whole dataset because one instrument cannot be matched would throw away
    the days that reconcile exactly.
    """
    result = reconstruct(fills, positions)
    assert result.realised[("20260803", "FUT")] == pytest.approx(-3516.98, abs=0.005)


def test_the_check_is_skipped_only_when_positions_are_not_supplied(fills):
    """Passing None is for unit-testing the FIFO, never for production use."""
    assert reconstruct(fills, None).declined == ()


def test_the_reconstruction_carries_the_fills_it_was_built_from(fills, positions):
    """The executions travel with the result, so one fetch can serve every reader.

    The dashboard needs the same fills twice — once for realised P&L, once to price the
    open lots — and IBKR asks that `/iserver/account/trades` be called once a session.
    Carrying them here is what keeps that one call, and keeps both surfaces reasoning
    about the same executions rather than two fetches taken moments apart.
    """
    result = reconstruct(fills, positions)
    assert result.fills == tuple(sorted(fills, key=lambda f: (f.trade_day, f.execution_id)))


# ── The gap the bridge has to cover ──────────────────────────────────────────


def test_the_gap_is_every_day_after_flex_not_just_today(fills):
    """Flex reached 2026-08-04 while fills existed on 08-05 AND 08-06.

    A bridge that assumed "today" would have left a whole trading day missing.
    """
    assert days_after(date(2026, 8, 4), fills) == ("20260805", "20260806")


def test_no_flex_coverage_at_all_means_every_day_qualifies(fills):
    """A store with no Flex data must not silently report an empty gap."""
    assert days_after(None, fills)[0] == "20260803"


def test_a_fully_covered_dataset_leaves_no_gap(fills):
    """Once Flex catches up, the reconstruction must add nothing and double-count nothing."""
    assert days_after(date(2026, 8, 6), fills) == ()


# ── The `position` field trap ────────────────────────────────────────────────


def test_the_fill_position_field_is_not_used():
    """It is the CURRENT position stamped on every historical row, not a running one.

    ES read `-1` on all ten of its fills, including ones where the running total was 0 or
    -2. `LiveFill` deliberately has no field for it, so it cannot be reached for by
    mistake.
    """
    assert not hasattr(LiveFill, "position")
    assert "position" not in LiveFill.__dataclass_fields__


# -- Evidence: an order STAGED BY CLAUDIA, filled, and correctly accounted -----


def _raw_fills():
    """The fixture as raw dicts, so `order_ref` is visible (LiveFill does not carry it)."""
    return json.loads(_FIXTURE.read_text())


def test_the_fixture_contains_a_real_claudia_staged_fill():
    """Evidence of the full chain: propose -> stage -> Touch ID -> dialog -> place -> FILL.

    Captured live 2026-08-06 15:41:41 UTC. `order_ref` carries ClaudIA's local-id
    convention (`CLAUDIA-<epoch_ms>`), which is what `dashboard_data.LiveOrder.
    is_claudia_staged` keys on, and it is the only thing distinguishing an order ClaudIA
    staged from one placed in TWS or IBKR Mobile. Every other fill in this fixture was
    placed by hand and carries no `order_ref` at all.
    """
    staged = [f for f in _raw_fills() if str(f.get("order_ref", "")).startswith("CLAUDIA-")]
    assert len(staged) == 1, "the ClaudIA-staged fill must stay in the fixture"
    fill = staged[0]
    assert fill["order_description"] == "Bot 1 @ 7737.00 on CME"
    assert fill["trade_time"] == "20260806-15:41:41"
    assert fill["sec_type"] == "FUT"
    assert fill["order_id"] == 692840840


def test_claudia_staged_fill_closed_the_short_and_realised_895_52(fills, positions):
    """The round trip ClaudIA closed, reconstructed independently.

    Short opened 13:36:27 @ 7755.00, closed by ClaudIA's order 15:41:41 @ 7737.00:
    `(7755.00 - 7737.00) x 50 - 2.24 - 2.24 = 895.52`.

    Added to the earlier 945.52 round trip, today totals **1,841.04** — which IBKR
    reported independently on BOTH ledger `realizedpnl` and the ES position's
    `realizedPnl` at the moment of capture.
    """
    result = reconstruct(fills, positions)
    assert result.total_for_day("20260806") == pytest.approx(1841.04, abs=0.005)
    assert result.by_type_for_day("20260806") == {"FUT": pytest.approx(1841.04, abs=0.005)}


def test_only_order_ref_identifies_a_claudia_order():
    """`/iserver/account/trades` carries no other ClaudIA marker.

    Measured 2026-08-06: `order_ref` is ABSENT from every hand-placed fill, so a union of
    field names over a ClaudIA-free history does not contain it at all. Code that assumes
    the key exists on every row will KeyError the moment it meets a manual trade.
    """
    raw = _raw_fills()
    with_ref = [f for f in raw if "order_ref" in f]
    assert len(with_ref) == 1
    assert all("order_ref" not in f for f in raw if f["trade_time"] < "20260806-15:41:41")


def test_fetch_fills_delegates_to_the_client_not_a_raw_request():
    """The warmup bug this delegation exists to avoid.

    `IBKRClient.get_trades()` retries once when the first call returns empty, because a
    fresh brokerage session answers `/iserver/account/trades` with `[]` until the
    subscription is primed. A hand-rolled GET skips that and returns nothing on exactly
    the poll after a login — which reads as "no trades today" rather than as a fault.
    """
    from unittest.mock import MagicMock

    from claudia.live_realised import fetch_fills

    client = MagicMock()
    client.get_trades.return_value = json.loads(_FIXTURE.read_text())
    fills = fetch_fills(client)

    client.get_trades.assert_called_once_with()
    assert len(fills) == 24


def test_fetch_fills_survives_an_empty_response():
    """No trades in the window is an ordinary answer, not an error."""
    from unittest.mock import MagicMock

    from claudia.live_realised import fetch_fills

    client = MagicMock()
    client.get_trades.return_value = []
    assert fetch_fills(client) == ()
