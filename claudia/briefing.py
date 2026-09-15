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

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Generic, Protocol, TypeAlias, TypeVar, assert_never

from claudia.contract_identity import ContractIdentity
from claudia.opening_status import EXCHANGE_LABELS

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
        closed.append(ClosedExchange(code=code, label=EXCHANGE_LABELS.get(code, code)))
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
    `dashboard_data.Position`, stated structurally so a test double needs no cast.

    All four members are read-only `@property`, not plain attributes: `Position` is a
    frozen dataclass, so mypy treats its fields as read-only, and a Protocol declaring a
    plain (settable) attribute is not satisfied by a read-only one. A property requirement
    is still satisfied by an ordinary mutable attribute (e.g. the `_Row` test double), so
    this costs nothing on the test side while letting the real, frozen `Position` satisfy
    the Protocol too.
    """

    @property
    def symbol(self) -> str:
        """The instrument's ticker, or IBKR's `contractDesc` fallback."""
        ...

    @property
    def conid(self) -> int:
        """The contract's IBKR conid — its only unambiguous identity. Used to look up an
        already-resolved `ContractIdentity` on the snapshot; never fetched here."""
        ...

    @property
    def description(self) -> str:
        """IBKR's `contractDesc` — the field that disambiguates a contract month."""
        ...

    @property
    def quantity(self) -> float:
        """Position size, signed. Zero means flat."""
        ...

    @property
    def expiry(self) -> str | None:
        """IBKR's `expiry` as `YYYYMMDD`, or None for an instrument that does not expire."""
        ...


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
    identities: Mapping[int, ContractIdentity] | None = None,
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

    "Flat" means `quantity == 0` exactly — not any falsy value. A row whose quantity is not
    a finite number (`NaN` or `+/-inf`) is skipped on the same defensive reasoning as an
    unparseable expiry: it is not observed in IBKR's real payload, but a size the operator
    cannot act on must not render as a position, and a non-finite quantity is a distinct
    defect from being flat rather than a variant of it.

    `identities` is the `DashboardSnapshot.identities` mapping the poller already
    resolved (design 2026-09-10, gap #37) — **no I/O happens here**. When a row's conid
    has an entry, `ExpiringContract` carries the identity's `local_symbol`/`name` (IB's
    own strings, e.g. `ESU6` / `E-mini S&P 500`) instead of the row's raw `symbol`/
    `description` (`ES` / `ES       SEP2026`). `identities=None` behaves exactly like an
    empty mapping, and a row whose conid is absent from it — no futures on the book, or
    a per-conid read that failed — falls back to the row's own fields: fail-soft, so a
    missing identity never blanks or drops the row. Resolved here, at build time, so the
    renderer (`_render_expiries`) stays dumb and needs no change.
    """
    if positions is None:
        return Degraded(reason="positions have not been polled yet")
    identity_map = identities or {}
    out: list[ExpiringContract] = []
    for row in positions:
        if not math.isfinite(row.quantity) or row.quantity == 0:
            continue
        when = _parse_ibkr_date(row.expiry)
        if when is None:
            continue
        days_left = (when - today).days
        if days_left < 0 or days_left > horizon_days:
            continue
        identity = identity_map.get(row.conid)
        out.append(
            ExpiringContract(
                symbol=identity.local_symbol if identity is not None else row.symbol,
                description=identity.name if identity is not None else row.description,
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


def _format_quantity(quantity: float) -> str:
    """Format a position size the way a trader reads one: signed, comma-grouped, never
    scientific notation, never silently rounded.

    `f"{-1234567.0:+g}"` renders `'-1.23457e+06'` — 6 significant figures then scientific
    notation, which is not a number an operator can read or act on (code review, 2026-09-15).
    This project's standing rule against silent rounding of a trader-facing figure applies
    here just as it does to an order parameter.

    `f"{quantity:,}"` (no type character) is Python's shortest round-tripping decimal string
    for the float, with `,` grouping applied — so a whole quantity keeps every digit
    (`1,234,567`) and a fractional one keeps its fraction exactly as given (`0.5`, `1.5`);
    nothing is truncated to a fixed number of decimals. It only reaches scientific notation
    at `1e16` and above, a magnitude no real position size approaches. The trailing `.0`
    Python adds to a whole float is stripped, since a position size is conventionally
    written without one.
    """
    text = f"{quantity:,}"
    if text.endswith(".0"):
        text = text[:-2]
    if not text.startswith("-"):
        text = f"+{text}"
    return text


def _degraded_reason(section: Degraded) -> str:
    """The Degraded sentence fragment: the reason, plus the staleness stamp when the
    caller set one. No builder in this module sets `as_of` today — `build_expiries`
    always passes a bare `reason` — but staleness is exactly the context an operator wants
    on a degraded section, so the renderer surfaces it the moment a caller starts setting
    one rather than silently dropping a field that already exists on the type.
    """
    if section.as_of is None:
        return section.reason
    return f"{section.reason} (as of {section.as_of})"


def _render_expiries(section: ExpirySection, escape: Callable[[str], str]) -> str:
    """The expiries section. Degraded and Unavailable carry ⚠ and their reason; only a
    `Ready` with no items may say the positive "nothing expiring".

    The `elif isinstance(section, Ready)` plus a final `assert_never` (rather than an
    unconditional `else` for the content path) is deliberate: if a fourth section
    constructor is ever added and happens to carry an `.items` attribute, it must not
    fall through and render silently as if it were `Ready` — that is exactly the bug class
    this module exists to prevent. mypy flags the `else` as unreachable only while the
    union really is exhausted; add a constructor and both mypy and `assert_never` fail loudly.
    """
    if isinstance(section, Degraded):
        return f"⚠ Expiring positions unavailable — {_degraded_reason(section)}."
    elif isinstance(section, Unavailable):
        return f"⚠ Expiring positions could not be read — {section.reason}."
    elif isinstance(section, Ready):
        if not section.items:
            return "No position expiring in the next week."
        lines = ["**Expiring soon:**"]
        for c in section.items:
            if c.days_left == 0:
                when = "today"
            elif c.days_left == 1:
                when = "1 day"
            else:
                when = f"{c.days_left} days"
            # IBKR-supplied strings: escaped. Our own literals and the formatted date are not.
            lines.append(
                f"- {escape(c.symbol)} ({escape(c.description.strip())}) — "
                f"{_format_quantity(c.quantity)} — expires {c.expiry.isoformat()}, {when}"
            )
        return "\n".join(lines)
    else:
        assert_never(section)


def _render_closures(section: ClosureSection, escape: Callable[[str], str]) -> str:
    """The closures section. Same exhaustiveness shape as `_render_expiries` — see there.

    `escape` is applied to `label` even though every label in play today traces to our own
    20-entry `EXCHANGE_LABELS` map or its own-literal fallback (`EXCHANGE_LABELS.get(code,
    code)` in `build_closures`), never to IBKR-supplied text. That provenance argument holds
    only as long as `get_market_calendar_context()` keeps being called with no arguments at
    its one call site — nothing pins that, and a future caller passing a derived exchange
    list would silently break it with no test going red. Escaping here removes the need to
    keep that argument true forever: §10's point is not having to reason about provenance.
    """
    if isinstance(section, Degraded):
        return f"⚠ Exchange closures unavailable — {_degraded_reason(section)}."
    elif isinstance(section, Unavailable):
        return f"⚠ Exchange closures could not be read — {section.reason}."
    elif isinstance(section, Ready):
        if not section.items:
            return "All tracked exchanges open today."
        names = ", ".join(escape(c.label) for c in section.items)
        return f"**Closed today:** {names}."
    else:
        assert_never(section)


class BriefingSnapshot(Protocol):
    """What `_build_briefing_text` reads off `DashboardSnapshot` — its three relevant
    fields only (`error`/`positions` for the trust guard, `identities` for naming).

    All three members are declared as read-only `@property`, not plain attributes: the
    real `DashboardSnapshot` this is checked against is a frozen dataclass, and mypy
    treats a frozen dataclass's fields as read-only. A Protocol with a plain (settable)
    attribute is not satisfied by a read-only one — a property is.
    """

    @property
    def error(self) -> str | None:
        """Set when the IBKR half of the poll failed; None on a clean read."""
        ...

    @property
    def positions(self) -> tuple[ExpiringRow, ...]:
        """Open positions, meaningful only when `error` is None."""
        ...

    @property
    def identities(self) -> Mapping[int, ContractIdentity]:
        """Futures contracts already named in IB's own strings, keyed by conid — read
        once per poll by `DashboardPoller._read_identities`, never fetched here."""
        ...


def positions_for_briefing(
    snapshot: BriefingSnapshot | None,
) -> Sequence[ExpiringRow] | None:
    """Positions to brief on, or None when they must not be trusted.

    `DashboardPoller.snapshot()` never returns None: before its first poll it serves
    `empty_snapshot(error="Dashboard has not polled yet.")`, whose `positions` is `()`.
    An empty tuple from *that* snapshot means "we have not looked"; an empty tuple from a
    clean poll means "nothing is open". They are opposite claims, and `error` is the only
    field that tells them apart — so this function keys on `error`, never on emptiness.
    """
    if snapshot is None:
        return None
    if snapshot.error is not None:
        return None
    return snapshot.positions


def render_briefing(briefing: Briefing, escape: Callable[[str], str]) -> str:
    """One Markdown block. Each section renders its own state; no section can borrow
    another's. A `Degraded` or `Unavailable` section always carries ⚠ and its reason, and
    never the positive "we looked and found nothing" sentence.

    `escape` is applied to every string of unpinned provenance and is **required, with no
    default**: a security control that a caller can forget by omission is not a control.
    `panel_app._build_briefing_text` passes `panel_markdown.escape_markup` — what
    `docs/security-architecture.md` §10 requires of a surface that shows text — wired and
    verified 2026-09-15. That verification also measured the extra escape pass as
    redundant: the resulting bokeh model text was byte-identical with and without it,
    because the chat feed already renders through `safe_markdown` (`renderers=
    [safe_markdown]`), which itself escapes. So passing `escape_markup` here is
    defence-in-depth, not a correctness requirement — see `_build_briefing_text`'s
    docstring for the check itself. Tests that care about wording rather than escaping
    pass `str` explicitly, which makes the choice visible at every call site.
    """
    return "\n\n".join(
        (
            _render_expiries(briefing.expiries, escape),
            _render_closures(briefing.closures, escape),
        )
    )
