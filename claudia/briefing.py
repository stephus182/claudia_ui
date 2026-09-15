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
"""The three constructors are a shared vocabulary across every section builder in this
module, not a per-function contract: `build_closures` below only ever returns `Ready` or
`Unavailable` (there is nothing about "today's closures" that is a benign not-yet-available
state), while Task 3's expiries builder does return `Degraded`. A caller that matches on
`build_closures` alone will see a `Degraded` branch it can never reach — that is intentional,
not a sign the branch belongs here."""


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

    A key in `holidays_by_exchange` that is not a string is dropped rather than turned into
    an `Unavailable`: the other, well-formed entries are still true statements about today,
    and a real closure this function would otherwise suppress is worse than one dropped
    junk key. Unlike a missing/malformed top-level dict — which means the whole read failed
    and nothing here can be trusted — a single bad key is a local defect in one entry, not
    evidence the rest of the map is wrong. The non-string keys **must be filtered out before
    `sorted()` runs**, not merely skipped inside the loop: `sorted()` compares every key
    against every other key, so one `int`/`str` (or `None`/`str`) pair anywhere in the dict
    raises `TypeError` before a single iteration — an in-loop guard placed after the sort
    can never run.
    """
    if mkt is None:
        return Unavailable(reason="market calendar could not be read")
    raw = mkt.get("holidays_by_exchange")
    if not isinstance(raw, dict):
        return Unavailable(reason="market calendar carried no holiday map")
    stamp = today.isoformat()
    string_keyed = [(code, days) for code, days in raw.items() if isinstance(code, str)]
    closed: list[ClosedExchange] = []
    for code, days in sorted(string_keyed):
        if not isinstance(days, (list, tuple)) or stamp not in days:
            continue
        closed.append(ClosedExchange(code=code, label=_EXCHANGE_LABELS.get(code, code)))
    return Ready(items=tuple(closed))
