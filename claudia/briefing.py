"""Pure builders for the startup briefing message.

No Panel import, no network call, no IBKR call. Every fact comes from data already in the
process: the dashboard poller's cached snapshot and the market-calendar dict
`opening_status` already fetches. That is what makes the operator's first hard rule —
NEVER BREAK STARTUP (2026-09-15) — structural rather than a promise: there is nothing here
that can block, hang, or fail slowly.

The second hard rule — never present a failed read as "nothing today" — is carried by the
types rather than by discipline. A section is one of three constructors and **only `Ready`
has items**, so `Ready(items=())` ("we looked, there is genuinely nothing") and
`Unavailable(reason)` ("we could not look") cannot be collapsed by a renderer. This is the
same reasoning as `DashboardSnapshot.orders` being `tuple | None`: `()` and a failed lookup
are opposite claims.

Design: `docs/plans/2026-09-15-morning-briefing-sources.md` §7.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeAlias, TypeVar

from claudia.opening_status import _EXCHANGE_LABELS

T = TypeVar("T")


@dataclass(frozen=True)
class Ready(Generic[T]):
    """The read succeeded. `items` may legitimately be empty — that is a positive result."""

    items: tuple[T, ...]


@dataclass(frozen=True)
class Degraded:
    """The read could not run yet, but the reason is known and benign (e.g. not polled)."""

    reason: str
    as_of: str | None = None


@dataclass(frozen=True)
class Unavailable:
    """The read was attempted or its input was absent. NEVER render this as "nothing"."""

    reason: str


@dataclass(frozen=True)
class ClosedExchange:
    """One exchange closed today: its MIC and the label the operator reads."""

    code: str
    label: str


ClosureSection: TypeAlias = "Ready[ClosedExchange] | Degraded | Unavailable"


def build_closures(mkt: Mapping[str, object] | None, today: date) -> ClosureSection:
    """Exchanges closed on `today`, from `get_market_calendar_context`'s dict.

    Takes a `Mapping`, not a `dict`: `dict` is **invariant** in its value type, so a caller
    holding a `dict[str, dict[str, list[str]]]` — which is what both the store and a test
    literal produce — could not pass it to a `dict[str, object]` parameter under mypy
    strict. `Mapping` is covariant and accepts it.

    `mkt` is None when the calendar could not be read; an empty or malformed dict is the
    same failure and must not be reported as "nothing closed today".

    Holidays arrive as bare ISO date strings with no names (measured 2026-09-15:
    `["2026-01-01", "2026-02-16", ...]`). The operator ruled the missing name cosmetic the
    same day — "knowing it's closed is what matters" — so no name resolution is attempted.
    """
    if mkt is None:
        return Unavailable(reason="market calendar could not be read")
    raw = mkt.get("holidays_by_exchange")
    if not isinstance(raw, dict):
        return Unavailable(reason="market calendar carried no holiday map")
    stamp = today.isoformat()
    closed: list[ClosedExchange] = []
    for code, days in sorted(raw.items()):
        if not isinstance(code, str):
            continue
        if not isinstance(days, (list, tuple)) or stamp not in days:
            continue
        closed.append(ClosedExchange(code=code, label=_EXCHANGE_LABELS.get(code, code)))
    return Ready(items=tuple(closed))
