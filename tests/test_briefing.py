"""Startup briefing builders and renderer.

No test here touches the network — the builders take plain data by design, which is the
same property that makes them safe to call during session start.
"""

from __future__ import annotations

from datetime import date

from claudia import briefing as br


def test_closures_lists_only_exchanges_closed_today() -> None:
    """Only today's closures, labelled from the existing 20-entry map."""
    mkt = {
        "holidays_by_exchange": {
            "XTKS": ["2026-09-21", "2026-09-23"],
            "XMEX": ["2026-09-16"],
            "XNYS": ["2026-11-26"],
        }
    }
    out = br.build_closures(mkt, today=date(2026, 9, 21))
    assert isinstance(out, br.Ready)
    assert [c.code for c in out.items] == ["XTKS"]
    assert out.items[0].label == "TSE Tokyo"


def test_closures_ready_and_empty_when_nothing_is_closed() -> None:
    """Measured 2026-09-15: no tracked exchange was closed. That is a POSITIVE result
    and must be Ready(()), never Unavailable."""
    mkt = {"holidays_by_exchange": {"XTKS": ["2026-09-21"]}}
    out = br.build_closures(mkt, today=date(2026, 9, 15))
    assert out == br.Ready(items=())


def test_closures_unavailable_when_the_calendar_is_missing() -> None:
    """No calendar is a failed read, and must never read as "nothing closed"."""
    out = br.build_closures(None, today=date(2026, 9, 15))
    assert isinstance(out, br.Unavailable)
    assert "calendar" in out.reason.lower()


def test_closures_unavailable_when_the_calendar_lacks_its_key() -> None:
    """A dict that arrived but carries no holiday map is a failed read, not a quiet day."""
    out = br.build_closures({}, today=date(2026, 9, 15))
    assert isinstance(out, br.Unavailable)


def test_closures_skips_non_string_keys_without_crashing_on_the_sort() -> None:
    """A malformed key must not crash the whole builder via `sorted()`.

    The filter that drops non-string keys has to run BEFORE `sorted()`, not merely
    guard the loop body: `sorted()` compares every entry against every other entry up
    front, so a single `int`/`str` (or `None`/`str`) key pair anywhere in the dict raises
    `TypeError` before the first well-formed entry is even reached — an in-loop
    `isinstance` continue placed after the sort call is unreachable and protects
    nothing. This is the exact mixed-key shape that reproduced the crash.
    """
    mkt = {
        "holidays_by_exchange": {
            "XTKS": ["2026-09-21"],
            1: ["2026-09-21"],
            None: [],
        }
    }
    out = br.build_closures(mkt, today=date(2026, 9, 21))
    assert isinstance(out, br.Ready)
    assert [c.code for c in out.items] == ["XTKS"]


class _Row:
    """Minimal stand-in for dashboard_data.Position — only what the builder reads."""

    def __init__(
        self,
        symbol: str,
        description: str,
        quantity: float,
        expiry: str | None,
        asset_class: str = "FUT",
    ) -> None:
        """Build a row with only the fields `build_expiries` reads."""
        self.symbol = symbol
        self.description = description
        self.quantity = quantity
        self.expiry = expiry
        self.asset_class = asset_class


def test_expiries_reports_a_future_inside_the_horizon() -> None:
    """The real 2026-09-15 case: ES Sep expiring on the 18th, three days out."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260918")]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.symbol == "ES"
    assert item.expiry == date(2026, 9, 18)
    assert item.days_left == 3


def test_expiries_excludes_a_future_beyond_the_horizon() -> None:
    """Dec is months away and would be permanent noise in the briefing. This does NOT pin
    the horizon_days boundary despite the name — Dec is months out, not one day past the
    cutoff. See test_expiries_excludes_a_contract_one_day_past_the_horizon for that."""
    rows = [_Row("ES", "ES       DEC2026", -1.0, "20261218")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_includes_a_contract_expiring_today() -> None:
    """Pins the lower boundary: days_left == 0 (expires TODAY) must be INCLUDED — this is
    exactly the day a roll warning matters most."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260915")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.days_left == 0


def test_expiries_includes_a_contract_exactly_at_the_horizon() -> None:
    """Pins the inclusive upper boundary: days_left == horizon_days (7) must be INCLUDED."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260922")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    (item,) = out.items
    assert item.days_left == 7


def test_expiries_excludes_a_contract_one_day_past_the_horizon() -> None:
    """Pins the off-by-one just past the boundary: days_left == horizon_days + 1 (8) must
    be EXCLUDED. This is the case test_expiries_excludes_a_future_beyond_the_horizon's
    December example does not actually exercise."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260923")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert out == br.Ready(items=())


def test_expiries_excludes_a_contract_that_expired_yesterday() -> None:
    """Pins the lower exclusion: days_left == -1 (expired YESTERDAY, still non-flat in the
    payload) must be EXCLUDED — a briefing reports what's ahead, not what already lapsed."""
    rows = [_Row("ES", "ES       SEP2026", -1.0, "20260914")]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert out == br.Ready(items=())


def test_expiries_sorts_by_days_left_then_symbol() -> None:
    """Nearest deadline first; a tie on days_left breaks by symbol. Pins the actual sort
    (every other test yields at most one item, so the sort itself was never exercised
    before this): [NQ@5, ES@1, CL@3, ZB@1] -> [ES(1), ZB(1), CL(3), NQ(5)]."""
    rows = [
        _Row("NQ", "NQ       SEP2026", -1.0, "20260920"),  # days_left 5
        _Row("ES", "ES       SEP2026", -1.0, "20260916"),  # days_left 1
        _Row("CL", "CL       OCT2026", -1.0, "20260918"),  # days_left 3
        _Row("ZB", "ZB       SEP2026", -1.0, "20260916"),  # days_left 1, tie with ES
    ]
    out = br.build_expiries(rows, today=date(2026, 9, 15), horizon_days=7)
    assert isinstance(out, br.Ready)
    assert [c.symbol for c in out.items] == ["ES", "ZB", "CL", "NQ"]
    assert [c.days_left for c in out.items] == [1, 1, 3, 5]


def test_expiries_excludes_a_flat_row() -> None:
    """IBKR keeps a closed contract in the payload at position 0.0 (measured 2026-09-15,
    both ES conids read 0.0 after the round trips). A flat book expires nothing."""
    rows = [_Row("ES", "ES       SEP2026", 0.0, "20260918")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_skips_a_non_finite_quantity() -> None:
    """ "Flat" means quantity == 0 exactly, not any falsy value. A NaN quantity is a
    distinct defect from being flat — it cannot arise from IBKR's real payload as far as
    observed, but a size the operator cannot act on must not render as a position, so it
    is skipped the same way an unparseable expiry is."""
    rows = [_Row("ES", "ES       SEP2026", float("nan"), "20260918")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_ignores_instruments_without_an_expiry() -> None:
    """A stock has no expiry at all, so it can never enter this section."""
    rows = [_Row("IGV", "IGV", 100.0, None, asset_class="STK")]
    assert br.build_expiries(rows, today=date(2026, 9, 15)) == br.Ready(items=())


def test_expiries_degraded_when_positions_have_not_been_polled() -> None:
    """The poller may not have published its first snapshot yet. That is not "nothing
    expiring" — it is "we have not looked"."""
    out = br.build_expiries(None, today=date(2026, 9, 15))
    assert isinstance(out, br.Degraded)
    assert "polled" in out.reason.lower()


def test_expiries_skips_an_unparseable_expiry_without_failing_the_section() -> None:
    """One bad row must not take the whole section down — the others are still true."""
    rows = [
        _Row("ES", "ES       SEP2026", -1.0, "not-a-date"),
        _Row("ES", "ES       SEP2026", -1.0, "20260918"),
    ]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    assert len(out.items) == 1


def test_expiries_is_not_keyed_on_sec_type() -> None:
    """Scope ruling 2026-09-15 is futures-first, but the DATA PATH must stay
    instrument-agnostic so options later are a predicate change, not a rewrite.
    An option row carrying an expiry must therefore be picked up by the builder."""
    rows = [_Row("SPY", "SPY 19SEP26 500 C", 1.0, "20260918", asset_class="OPT")]
    out = br.build_expiries(rows, today=date(2026, 9, 15))
    assert isinstance(out, br.Ready)
    assert len(out.items) == 1
