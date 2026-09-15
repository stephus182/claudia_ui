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
