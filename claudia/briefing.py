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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Generic, Protocol, TypeAlias, TypeVar

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


@dataclass(frozen=True)
class ExpiringContract:
    """One open position near its expiry. `days_left` is calendar days, not sessions."""

    symbol: str
    description: str
    expiry: date
    days_left: int
    quantity: float


class ExpiringRow(Protocol):
    """What `build_expiries` reads off a position row — a subset of
    `dashboard_data.Position`, stated structurally so a test double needs no cast."""

    symbol: str
    description: str
    quantity: float
    expiry: str | None


ExpirySection: TypeAlias = "Ready[ExpiringContract] | Degraded | Unavailable"

DEFAULT_HORIZON_DAYS = 7
"""How far ahead an expiry is worth mentioning. A week covers the roll window without
making the briefing a standing fixture."""


def _parse_ibkr_date(value: str | None) -> date | None:
    """IBKR's `YYYYMMDD` → date, or None when absent or malformed."""
    if not value or len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def build_expiries(
    positions: Sequence[ExpiringRow] | None,
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> ExpirySection:
    """Open positions expiring within `horizon_days`, nearest deadline first.

    `positions` is None when the dashboard poller has not published a snapshot yet — that
    is `Degraded`, never an empty `Ready`, because "we have not looked" and "nothing is
    expiring" are opposite claims.

    **Not keyed on `asset_class`.** The scope ruling of 2026-09-15 is futures-first, but
    any instrument that carries an expiry is picked up, so options later are a predicate
    change rather than a rewrite. The expiry comes from IBKR's own payload, never from a
    contract-specific rule of ours.

    A row whose expiry will not parse is skipped rather than failing the section: the other
    rows are still true, and a section-wide failure would overstate one bad field. This is
    the same local-defect-vs-whole-read distinction `build_closures` makes.
    """
    if positions is None:
        return Degraded(reason="positions have not been polled yet")
    out: list[ExpiringContract] = []
    for row in positions:
        if not row.quantity:
            continue
        when = _parse_ibkr_date(row.expiry)
        if when is None:
            continue
        days_left = (when - today).days
        if days_left < 0 or days_left > horizon_days:
            continue
        out.append(
            ExpiringContract(
                symbol=row.symbol,
                description=row.description,
                expiry=when,
                days_left=days_left,
                quantity=row.quantity,
            )
        )
    out.sort(key=lambda c: (c.days_left, c.symbol))
    return Ready(items=tuple(out))


@dataclass(frozen=True)
class Briefing:
    """The whole briefing: one section per topic, each carrying its own state.

    Adding a section here means adding it to `render_briefing` and to the positive-sentence
    map in `test_a_degraded_section_is_never_rendered_as_an_empty_one`, which fails loudly
    until you do.
    """

    expiries: ExpirySection
    closures: ClosureSection
