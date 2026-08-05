"""Pure data layer for the live trading dashboard — **no Panel import, by design.**

This module is one half of the dashboard seam: it knows IBKR response shapes and SQL,
and nothing about widgets. `claudia/panel_dashboard.py` is the other half — it knows
widgets and nothing about IBKR or SQL. `claudia/dashboard_poller.py` joins them by
calling into here on a background task and caching a `DashboardSnapshot` the Panel
layer reads synchronously.

Everything here is synchronous and blocking (HTTP or SQLite). Callers on the Panel
event loop MUST go through `asyncio.to_thread` — blocking the shared loop freezes
every session (`docs/panel/panel-reference.md` §3.3).

## Where each number comes from — verified 2026-08-04, not assumed

| Figure | Source |
|---|---|
| Net liq, cash, market values, unrealised P&L | `client.get_account_ledger()` |
| Realised P&L "now" | ledger `realizedpnl` alone — **not** `futuresonlypnl`, see `LedgerSnapshot` |
| Positions | `client.get_positions()`, **paged** (page 0 returns only 30) |
| Realised week / month / YTD | `SUM(flex_trade.fifo_pnl_realized)` bucketed by `trade_date_iso` |
| Round trips, winners/losers | `flex_lot` — closed lots, see `round_trip_stats` |

`ClaudeToolkit.execute()` is deliberately **not** used: it returns rendered markdown,
not data (`claude_tools.py` `_get_ledger` formats its own table). Reading
`toolkit.client.*` directly is what makes structured values available at all; the
alternative — regex-scraping rendered output, as `opening_status.py` must do for the
chat block — is not a pattern to copy into a new module.

Likewise the account id is resolved **once** by the caller (the poller) and passed in.
`ClaudeToolkit._first_account_id()` hits `/portfolio/accounts` on every toolkit call,
and that endpoint is rate-limited to roughly one request per five seconds.

## The realised-P&L rule — do not re-derive it

```sql
SELECT SUM(fifo_pnl_realized) FROM flex_trade
 WHERE source='flex' AND trade_date_iso BETWEEN ? AND ?
```

**No open/close filter.** Settled 2026-08-04 against IBKR's own `SymbolSummary` in 20 of
20 archived statements and its annual statements in 6 of 6 years. Two traps this project
fell into before measuring, both now guarded by ibkr_core_mcp's audit gate:

1. Filtering on `open_close_indicator` is wrong — a buy that closes a short and opens a
   long is flagged `O` and still realises.
2. `flex_lot` is **pre-wash-sale** tax-lot detail; summing it overstates losses.
   `Trade == Lot + WashSale`.

Full reasoning: `ibkr_core_mcp/docs/flex-query-reference.md` § How to compute realised P&L.

`trade_date_iso` is used rather than the plan's `trade_date` purely because it is
ISO-formatted and therefore comparable to a `datetime.date` without reformatting. Both
columns are indexed (`flex_store._INDEXED`) and both are non-null on every `source='flex'`
row (verified 2026-08-04: 0 of 1,101). `flex_lot` has **neither** `trade_date_iso` nor
`source` — it carries only the compact `YYYYMMDD` `trade_date`, and every lot is
Flex-derived — so `round_trip_stats` formats its own bounds. That asymmetry is the
reason the two query helpers do not share a date predicate.

## IBKR's "day" is a session, not a calendar day — and that is already handled

`trade_date` is **not** the calendar date of the fill. IBKR rolls it forward to the
trading day the *session* belongs to, and the boundary differs by asset class. Measured
across this account's 1,101 Flex executions on 2026-08-04, every boundary bracketed
cleanly with no overlap:

| Asset | latest same-day fill | earliest rolled-forward fill | boundary |
|---|---|---|---|
| `FUT` | 16:59:40 | 18:00:00 (91 of 91 rolled) | **18:00 ET** — CME Globex opens 17:00 CT |
| `STK` | 19:23:31 | 20:09:37 | **20:00 ET** — IBKR's overnight session |
| `CASH` | 15:17:59 | 17:28:09 | **17:00 ET** — IdealPro FX day roll |
| `OPT` | 15:57:04 | none observed | no evening fills, so no evidence |

The `FUT` gap between 17:00 and 18:00 ET is CME's daily maintenance break, which is why
the boundary lands exactly on the session open. `STK` matches IBKR's published wording
verbatim — *"Trades executed between 8:00pm and 12:00am will carry a trade date of the
following day"*
(https://www.interactivebrokers.com/campus/trading-lessons/overnight-trading-in-tws/).
Two independent boundaries landing on their venue's ET session times also confirms the
statement's `dateTime` is ET. One apparent `FUT` exception — 2026-05-25 00:06:53 filed to
05-26 — is Memorial Day: the session rolled to the next *trading* day, not the next
calendar day, which is the same rule.

**Nothing here needs replicating, and that is the point.** Every window in this module
buckets on `trade_date`, so it inherits IBKR's session convention for free and by
construction. Re-deriving a roll rule in Python would be a second, drifting definition of
"day" sitting next to the authoritative one — the class of mistake the realised-P&L rule
above already cost this project once.

What the convention *does* change is what a reader should be told: after 18:00 ET a
futures fill already belongs to tomorrow as far as these windows are concerned, while the
ledger figure beside them is a **calendar** day and does not follow that roll. That is
measured, not assumed — see `REALISED_LEDGER_WINDOW` below, where seven reads spanning
both the 18:00 ET futures roll and the 20:00 ET stock roll never moved off -2,656.11.
So the two realised figures on the dashboard differ on the day boundary as well as on the
cost basis (`RealisedWindow`), and `panel_dashboard.coverage_line` states both on the
surface rather than leaving a reader to discover them by subtraction.

## The T+1 gap is real and is surfaced, not papered over

Flex never has today. The live Client Portal rows (`source='live'`) carry **no
`trade_date` at all**, by design: the CP timestamp is UTC, so a 20:56 ET fill reads as
the next day and cannot be bucketed until the statement confirms it. So the
Flex-derived windows exclude today, while the ledger figure includes it. Those two
numbers have different as-of times; `FlexCoverage` carries both facts so the UI can
say so. An unlabelled pair that disagrees is the exact failure this module exists to
avoid.

## Currency

Every figure carries its ISO code — `$` is shared by USD/MXN/CAD/AUD/HKD/SGD, so a
wrong-currency price reads as an ordinary one. `RealisedWindow.currencies` reports what
the window actually contained rather than assuming USD: the store holds EUR and CHF
trades from 2024 and 2025 alongside the USD ones (measured 2026-08-04), even though
2026 YTD happens to be 100% USD.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ibkr_core_mcp import IBKRClient

log = logging.getLogger(__name__)

# IBKR returns positions 30 to a page and gives no total count, so "is there more?" is
# "did this page come back full?". The cap exists so a malformed response that keeps
# returning full pages cannot spin forever; 40 pages is 1,200 positions.
_POSITIONS_PAGE_SIZE = 30
_MAX_POSITION_PAGES = 40

# The ledger key IBKR uses for its synthetic all-currency aggregate.
_BASE_KEY = "BASE"


def _as_float(value: Any) -> float:
    """Coerce an IBKR numeric field to float; 0.0 for None, "" or anything unparseable.

    IBKR ships numbers as float, int, or numeric string depending on the endpoint and,
    occasionally, on the field. Mirrors the `_f` helper in `claude_tools._get_ledger`
    so the dashboard and the chat block cannot disagree about what a blank means.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Date windows ──────────────────────────────────────────────────────────────


def week_start(today: date) -> date:
    """Monday of `today`'s week (requirement: trading week is Monday → Friday)."""
    return today - timedelta(days=today.weekday())


def month_start(today: date) -> date:
    """First calendar day of `today`'s month."""
    return today.replace(day=1)


def year_start(today: date) -> date:
    """January 1st of `today`'s year — the YTD boundary IBKR's annual statements use."""
    return today.replace(month=1, day=1)


# ── Account ledger ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerSnapshot:
    """One currency row of `/portfolio/{accountId}/ledger`, typed.

    `realised_pnl` and `futures_only_pnl` come straight from the ledger's `realizedpnl`
    and `futuresonlypnl`. **The ledger endpoint documents neither field's time window** —
    its published description is "Returns the realized profit and loss for positions in
    the given currency" and `futuresonlypnl` carries no description at all (scraped
    2026-08-04,
    https://ibkrcampus.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger.md).

    The window came from the *positions* endpoint instead, because `realizedpnl` is
    exactly the sum of the per-position `realizedPnl` — see `REALISED_LEDGER_WINDOW`
    below, which holds that evidence and from which the UI label is derived.

    ⚠ **`futures_only_pnl` is not the futures half of a realised split.** Measured
    against the live account 2026-08-04 in two reads an hour apart, it was **exactly**
    `futuremarketvalue` both times (-11,607.50, then -11,223.30) while `realizedpnl`
    did not move at all (-2,656.11). Computing `realised_pnl - futures_only_pnl` as an
    "equities" residual would therefore be wrong by an order of magnitude and in the
    wrong direction. The field is carried verbatim under IBKR's own name and no
    arithmetic is done on it; the per-asset split that *is* exact comes from
    `RealisedWindow.by_asset`. Full evidence: `panel_dashboard.ledger_markdown`.

    `other_currencies` lists the non-BASE currency codes present in the same response
    that are *not* this row, so a UI can disclose that other balances exist rather than
    presenting a single-currency view as the whole account.
    """

    currency: str
    net_liquidation: float
    cash: float
    settled_cash: float
    stock_market_value: float
    futures_market_value: float
    unrealised_pnl: float
    realised_pnl: float
    futures_only_pnl: float
    other_currencies: tuple[str, ...] = ()


# SETTLED 2026-08-04: ledger `realizedpnl` is **today's** realised P&L — a calendar day,
# not a trading session, and not a cumulative total.
#
# The ledger endpoint does not document the window (see LedgerSnapshot). The positions
# endpoint does, and the two are the same number:
#
# (1) IDENTITY. Read together at 19:55 ET, the five per-position `realizedPnl` values
#     (GLD -436.44, CL +1,945.28, CRM -2,810.47, ES -1,354.48, IGV 0.00) summed to
#     **exactly** the ledger's -2,656.11. So whatever the positions endpoint means by
#     `realizedPnl`, the ledger reports its sum.
#
# (2) IBKR'S OWN WORDS for that field: "Returns the total profit made today through
#     trades" (scraped 2026-08-04,
#     https://ibkrcampus.com/docs/web-api/v1/endpoints/portfolio/positions.md, and
#     identically on .../positions-new.md).
#
# (3) NEGATIVE CONTROL. IGV realised -154.44 on 2026-07-30 and has never been flat, yet
#     reported `realizedPnl` 0.00 — while every instrument traded *today* reported a
#     non-zero figure. That rules out every since-inception reading in one observation.
#
# (4) ARITHMETIC. GLD's reported `avgCost` 383.270899 times today's 50-share sale at
#     374.57 gives -435.04, which is -436.44 after commission: the reported figure, from
#     today's fill alone. Nothing from any earlier day is in it.
#
# (5) NOT A SESSION. IBKR's own `trade_date` rolls at 18:00 ET for futures and 20:00 ET
#     for stock (the session table in the module docstring), so "the trading session"
#     was the live rival reading. Seven reads at 17:51, 18:08, 18:49, 19:02, 19:55,
#     20:02 and 20:02 ET returned exactly -2,656.11 throughout, while `unrealizedpnl`
#     moved freely. The futures roll would have cleared CL and ES at 18:00; the stock
#     roll would have cleared GLD and CRM at 20:00. Neither happened.
#
# The one thing NOT observed is the reset instant itself — a cross-midnight read, which
# no single session can produce. "day" therefore names the window IBKR documents and the
# measurements confirm, and claims nothing about the exact moment it turns over. The
# label reads "Realised today" either way, so that residue changes no user-visible text.
#
# Deliberately a module constant rather than a string buried in the UI, so the tile,
# the ledger table and the tests all relabel from one edit.
REALISED_LEDGER_WINDOW = "day"

_REALISED_LEDGER_LABELS = {
    "unverified": "Realised (ledger)",
    "day": "Realised today",
    "cumulative": "Realised (cumulative)",
}


def realised_ledger_label(window: str = REALISED_LEDGER_WINDOW) -> str:
    """The tile label matching `REALISED_LEDGER_WINDOW`.

    Falls back to the neutral "Realised (ledger)" for an unknown window value: a label
    that overclaims is worse than one that says less, and this figure sits beside
    Flex-derived numbers with a different as-of time.
    """
    return _REALISED_LEDGER_LABELS.get(window, _REALISED_LEDGER_LABELS["unverified"])


def parse_ledger(
    ledger: Mapping[str, Any] | None, base_currency: str | None = None
) -> LedgerSnapshot | None:
    """Resolve the account's base-currency row from a `get_account_ledger()` response.

    IBKR keys the response by currency code plus a synthetic `BASE` aggregate.
    Resolution order, each step evidence-backed and with no guess at the end:

    1. `base_currency` — the `currency` field of the account's own `/portfolio/accounts`
       entry, which is authoritative and which the poller already has. Used when that
       code has a row of its own; per-currency keys carry the authoritative values
       (`claude_tools._get_ledger` makes the same choice).
    2. Exactly one non-`BASE` row — then there is nothing to disambiguate.
    3. The `BASE` row's own `currency` field, but **only** when it names a real
       per-currency key. See the warning below for why that is a fallback and not the
       first rule.
    4. Otherwise None.

    ⚠ **The `BASE` row's `currency` field is the literal string `"BASE"`, not the base
    currency's code.** Measured against the live account 2026-08-04:
    `{'USD': {'currency': 'USD', ...}, 'BASE': {'currency': 'BASE', ...}}`. IBKR's
    documentation reads the other way — "May return 'BASE' to show your base currency"
    alongside "currency: Returns the currency's symbol" — and this function originally
    trusted it, which put **"57,600.71 BASE"** on the KPI strip on the first real run.
    Unit tests could not catch it: they asserted the documented shape. Hence the order
    above, and hence step 3 requires the named code to resolve to an actual row.

    Returns None for an empty or non-mapping response as well.
    """
    if not isinstance(ledger, Mapping) or not ledger:
        return None
    rows = {k: v for k, v in ledger.items() if isinstance(v, Mapping)}
    if not rows:
        return None
    others = sorted(k for k in rows if k != _BASE_KEY)

    hint = (base_currency or "").strip().upper()
    if hint and hint in rows and hint != _BASE_KEY:
        return _ledger_row(hint, rows[hint], tuple(c for c in others if c != hint))
    if len(others) == 1:
        return _ledger_row(others[0], rows[others[0]], ())
    base_row = rows.get(_BASE_KEY)
    if base_row is not None:
        code = str(base_row.get("currency") or "").strip().upper()
        if code and code != _BASE_KEY and code in rows:
            return _ledger_row(code, rows[code], tuple(c for c in others if c != code))
    return None


def _ledger_row(
    currency: str, data: Mapping[str, Any], other_currencies: tuple[str, ...]
) -> LedgerSnapshot:
    """Build one `LedgerSnapshot` from a raw ledger currency object."""
    return LedgerSnapshot(
        currency=currency,
        net_liquidation=_as_float(data.get("netliquidationvalue")),
        cash=_as_float(data.get("cashbalance")),
        settled_cash=_as_float(data.get("settledcash")),
        stock_market_value=_as_float(data.get("stockmarketvalue")),
        futures_market_value=_as_float(data.get("futuremarketvalue")),
        unrealised_pnl=_as_float(data.get("unrealizedpnl")),
        realised_pnl=_as_float(data.get("realizedpnl")),
        futures_only_pnl=_as_float(data.get("futuresonlypnl")),
        other_currencies=other_currencies,
    )


def fetch_ledger(
    client: IBKRClient, account_id: str, base_currency: str | None = None
) -> LedgerSnapshot | None:
    """Pull and parse the account ledger. Blocking HTTP — call via `asyncio.to_thread`.

    `base_currency` comes from the account's `/portfolio/accounts` entry — see
    `parse_ledger` for why the ledger's own `BASE` row cannot supply it.
    """
    return parse_ledger(client.get_account_ledger(account_id), base_currency)


# ── Positions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """One open position, typed from `/portfolio/{accountId}/positions/{page}`.

    `description` is IBKR's `contractDesc` — the only field that disambiguates a futures
    contract month (`ESU6`) from its root, and the one a trader reads. `symbol` falls
    back to it when IBKR omits `ticker`.

    The positions table renders `symbol`, `asset_class`, `quantity`, `average_cost`,
    `market_price`, `market_value`, `unrealised_pnl` and `currency`. `conid` and
    `realised_pnl` are carried but not yet displayed — deliberately, and named here so a
    reader does not go looking for where they are shown: `conid` is the contract's only
    unambiguous identity (a ticker is not a unique key, and IGV once priced a US ETF in
    MXN because of it), and `realised_pnl` is the sibling of the figure beside it. Both
    are free to carry and would otherwise force a data-layer change on the first
    drill-down.
    """

    conid: int
    symbol: str
    description: str
    asset_class: str
    quantity: float
    average_cost: float
    market_price: float
    market_value: float
    unrealised_pnl: float
    realised_pnl: float
    currency: str


def parse_positions(rows: Sequence[Any]) -> tuple[Position, ...]:
    """Type a raw positions payload, skipping fully-closed rows.

    IBKR keeps returning a position with `position: 0` for the rest of the session after
    it is closed. Those are not open positions and must not appear in a table headed
    "Positions" — but a row with a *missing* position field is a parse problem, not a
    closed trade, so it is kept with quantity 0.0 rather than silently dropped.

    The parameter is `Sequence[Any]`, not `Sequence[Mapping]`, because this is a decoded
    JSON payload: annotating it as already-typed would make the `isinstance` guard below
    statically unreachable (mypy says so) while the runtime hazard it guards — IBKR
    returning a bare string or null inside the list — would remain.
    """
    out: list[Position] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if "position" in row and _as_float(row.get("position")) == 0.0:
            continue
        desc = str(row.get("contractDesc") or "").strip()
        out.append(
            Position(
                conid=int(_as_float(row.get("conid"))),
                symbol=str(row.get("ticker") or "").strip() or desc,
                description=desc,
                asset_class=str(row.get("assetClass") or "").strip(),
                quantity=_as_float(row.get("position")),
                average_cost=_as_float(row.get("avgCost")),
                market_price=_as_float(row.get("mktPrice")),
                market_value=_as_float(row.get("mktValue")),
                unrealised_pnl=_as_float(row.get("unrealizedPnl")),
                realised_pnl=_as_float(row.get("realizedPnl")),
                currency=str(row.get("currency") or "").strip().upper(),
            )
        )
    return tuple(out)


def fetch_positions(client: IBKRClient, account_id: str) -> tuple[Position, ...]:
    """Fetch **all** positions, following IBKR's 30-per-page pagination.

    Page 0 returns only the first 30 (`client.get_positions` docstring). A dashboard that
    read page 0 alone would silently show a subset of the book, which on a trading
    surface is worse than showing nothing. Paging stops on the first short page — IBKR
    reports no total — and is capped at `_MAX_POSITION_PAGES` so a response that never
    shortens cannot spin forever.

    Blocking HTTP — call via `asyncio.to_thread`.
    """
    rows: list[Mapping[str, Any]] = []
    for page in range(_MAX_POSITION_PAGES):
        chunk = client.get_positions(account_id, page=page)
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < _POSITIONS_PAGE_SIZE:
            break
    else:
        log.warning(
            "Positions paging hit the %d-page cap — the table may be truncated",
            _MAX_POSITION_PAGES,
        )
    return parse_positions(rows)


# ── Realised P&L from the Flex dataset ────────────────────────────────────────


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the ibkr_core_mcp store **read-only**.

    `mode=ro` is a real guarantee, not decoration: this is a display surface and the
    same file is being written by the Flex sync in another thread. A read-only handle
    cannot corrupt it and cannot be turned into a write path by a later edit. Verified
    2026-08-04 against the live WAL database — a read-only URI connection reads it fine.

    `row_factory` is `sqlite3.Row` so the query helpers below can address columns by
    name, matching `SQLiteStore._connect`'s convention.
    """
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class RealisedWindow:
    """Realised P&L over one date window, from the Flex dataset.

    `total` is the plain sum of `fifo_pnl_realized` with no open/close filter — the rule
    settled against IBKR's own statements (module docstring). `by_asset` splits it by
    `asset_category` (`FUT` / `STK` / `OPT` / `FUND` / `CASH`), which is what the
    "futures vs equities" requirement asks for.

    `currencies` is what the window actually contained, not an assumption. When it holds
    more than one code the total is a sum across currencies and the UI must say so
    rather than stamping one ISO code on it.

    There is no `name` field: `start`/`end` identify the window completely, and the
    caller already knows which one it asked for. A name carried here and never read is
    a second place for "week" to be wrong.

    ⚠ **This is not the same quantity as `LedgerSnapshot.realised_pnl`, and the two must
    never be added or expected to agree.** Both are realised P&L; they are computed on
    different cost bases, so the same closing trade can produce very different figures:

    * This one is IBKR's *statement* basis — `flex_trade.fifo_pnl_realized`, the number
      that reconciled to IBKR's annual statements 6/6 years exactly.
    * The ledger's is IBKR's *real-time* basis, priced against the positions endpoint's
      `avgCost`, which is not the FIFO purchase average. Measured 2026-08-04: GLD's
      100 shares cost 380.3654 on FIFO but IBKR carried `avgCost` **383.270899** —
      higher by 2.905499, which is to six decimal places the 290.55 the 2026-06-11
      close realised, spread over the 100 replacement shares. CRM showed the same
      pattern for its 2026-06-22 close (2,555.66 over 50 shares). Both of those closes
      report `fifo_pnl_realized = 0.0` at trade level, so on this side the P&L lands on
      the earlier date at zero and on the later date in full, while on the ledger side
      it lands wholly on the later date. That is the mechanism IBKR describes for a
      disallowed loss — "the disallowed loss is incorporated into the calculations of
      the gain or loss on the replacement shares and recognized"
      (https://www.interactivebrokers.com/en/support/tax-wash-sales.php, read
      2026-08-04). Our Flex extract carries no wash-sale note code, so the *mechanism*
      is inferred from IBKR's published rule; the *divergence* is measured and exact.

    The consequence is concrete, not theoretical: on 2026-08-04 the ledger reported
    -2,810.47 realised on CRM while the same day's closing trades move roughly -252.60
    at FIFO cost. `panel_dashboard.coverage_line` states this on the surface, because a
    trader who tries to reconcile the KPI tile against the week window will otherwise
    conclude one of them is broken.
    """

    start: date
    end: date
    total: float
    trade_count: int
    by_asset: Mapping[str, float] = field(default_factory=dict)
    currencies: tuple[str, ...] = ()

    @property
    def currency_label(self) -> str:
        """ISO code for `total`, "mixed" across several currencies, or "" when unknown.

        An **empty** window returns the empty string rather than "USD". Nothing was
        realised, so there is no currency to state — and stating one anyway would be
        exactly the guess `parse_ledger` refuses to make two hundred lines up. The view
        substitutes the account's own base currency when it has one; `fmt_money` and
        `fmt_signed` render a bare number when it does not.
        """
        if len(self.currencies) == 1:
            return self.currencies[0]
        if not self.currencies:
            return ""
        return "mixed"

    def asset_total(self, *categories: str) -> float:
        """Sum of `by_asset` over the named categories (missing ones count as 0.0)."""
        return sum(self.by_asset.get(c, 0.0) for c in categories)


def realised_window(
    conn: sqlite3.Connection, start: date, end: date
) -> RealisedWindow:
    """Realised P&L between `start` and `end` inclusive, split by asset class.

    The predicate is `source='flex' AND trade_date_iso BETWEEN ? AND ?` — no open/close
    filter, deliberately (module docstring). `source='flex'` excludes the live Client
    Portal rows, which carry no `trade_date` and no realised figure at all and would
    contribute nothing but a misleading count.
    """
    rows = conn.execute(
        """
        SELECT asset_category, currency,
               COUNT(*) AS n, SUM(fifo_pnl_realized) AS pnl
          FROM flex_trade
         WHERE source = 'flex'
           AND trade_date_iso BETWEEN ? AND ?
         GROUP BY asset_category, currency
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    by_asset: dict[str, float] = {}
    currencies: set[str] = set()
    total = 0.0
    count = 0
    for row in rows:
        pnl = float(row["pnl"] or 0.0)
        asset = str(row["asset_category"] or "?")
        by_asset[asset] = by_asset.get(asset, 0.0) + pnl
        total += pnl
        count += int(row["n"] or 0)
        if row["currency"]:
            currencies.add(str(row["currency"]).upper())
    return RealisedWindow(
        start=start,
        end=end,
        total=total,
        trade_count=count,
        by_asset=by_asset,
        currencies=tuple(sorted(currencies)),
    )


@dataclass(frozen=True)
class RealisedPoint:
    """One day on the realised-P&L curve: that day's realisation and the running total."""

    day: date
    realised: float
    cumulative: float


def realised_series(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[RealisedPoint, ...]:
    """Daily realised P&L between `start` and `end` inclusive, with a running total.

    Only days that actually traded appear — the cumulative line therefore steps between
    them rather than drawing a flat run through non-trading days. That is the honest
    shape for a realisation curve: nothing was realised on the days in between, and
    inventing zero-rows would imply the account was observed on days no statement covers.
    """
    rows = conn.execute(
        """
        SELECT trade_date_iso AS day, SUM(fifo_pnl_realized) AS pnl
          FROM flex_trade
         WHERE source = 'flex'
           AND trade_date_iso BETWEEN ? AND ?
         GROUP BY trade_date_iso
         ORDER BY trade_date_iso
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    out: list[RealisedPoint] = []
    running = 0.0
    for row in rows:
        pnl = float(row["pnl"] or 0.0)
        running += pnl
        out.append(RealisedPoint(date.fromisoformat(str(row["day"])), pnl, running))
    return tuple(out)


@dataclass(frozen=True)
class RoundTripStats:
    """Winners vs losers over closed round trips, from `flex_lot`.

    **This is a count, never a P&L total.** `flex_lot` is pre-wash-sale tax-lot detail
    (`Trade == Lot + WashSale`), so summing it overstates losses by the disallowed
    amount. The authoritative money figure is `RealisedWindow.total` from `flex_trade`;
    these two must be labelled distinctly wherever they appear together.

    Lots are the right basis for the *count* because a lot is a genuine round trip —
    open → close, with a holding period — whereas executions include opening legs and
    wash-sale-zeroed closes that are neither a win nor a loss. Measured 2026-08-04 on
    2026 YTD: 636 executions (156 win / 134 loss / 346 zero) against 360 closed lots
    (170 win / 190 loss / 0 zero).
    """

    start: date
    end: date
    closed_lots: int
    winners: int
    losers: int
    scratches: int
    gross_win: float
    gross_loss: float

    @property
    def win_rate(self) -> float | None:
        """Winners as a percentage of decided lots, or None when nothing closed.

        Scratches are excluded from the denominator: a lot that realised exactly 0.00 is
        neither won nor lost, and counting it as a loss would understate the rate. None
        rather than 0.0 for an empty window, so the UI can render "—" instead of a 0%
        win rate that reads like a catastrophic week.
        """
        decided = self.winners + self.losers
        return 100.0 * self.winners / decided if decided else None


def round_trip_stats(conn: sqlite3.Connection, start: date, end: date) -> RoundTripStats:
    """Closed-lot win/loss counts between `start` and `end` inclusive.

    Queries `flex_lot`, which has neither a `trade_date_iso` column nor a `source`
    column (verified 2026-08-04) — so the bounds are formatted to its compact `YYYYMMDD`
    `trade_date`, and there is no source predicate because every lot is Flex-derived.

    `gross_win`/`gross_loss` are lot-basis subtotals for a win/loss ratio; they are
    **not** realised P&L and their difference is not the account's realised total. See
    `RoundTripStats`.
    """
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    row = conn.execute(
        """
        SELECT COUNT(*)                                          AS n,
               SUM(CASE WHEN fifo_pnl_realized > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN fifo_pnl_realized < 0 THEN 1 ELSE 0 END) AS losses,
               SUM(CASE WHEN COALESCE(fifo_pnl_realized, 0) = 0 THEN 1 ELSE 0 END) AS flat,
               SUM(CASE WHEN fifo_pnl_realized > 0 THEN fifo_pnl_realized ELSE 0 END) AS gross_win,
               SUM(CASE WHEN fifo_pnl_realized < 0 THEN fifo_pnl_realized ELSE 0 END) AS gross_loss
          FROM flex_lot
         WHERE trade_date BETWEEN ? AND ?
        """,
        (lo, hi),
    ).fetchone()
    return RoundTripStats(
        start=start,
        end=end,
        closed_lots=int(row["n"] or 0),
        winners=int(row["wins"] or 0),
        losers=int(row["losses"] or 0),
        scratches=int(row["flat"] or 0),
        gross_win=float(row["gross_win"] or 0.0),
        gross_loss=float(row["gross_loss"] or 0.0),
    )


@dataclass(frozen=True)
class FlexCoverage:
    """How far the Flex dataset reaches, and how much has not landed in it yet.

    This is what makes the T+1 gap visible instead of implicit. `through` is the newest
    Flex `trade_date_iso`; `live_pending` counts Client Portal rows that have no
    statement yet. When `live_pending` is non-zero the Flex-derived windows are, by construction,
    missing those fills — and the ledger figure beside them is not.
    """

    through: date | None
    live_pending: int


def flex_coverage(conn: sqlite3.Connection) -> FlexCoverage:
    """Newest Flex trade date and the count of live rows still awaiting a statement."""
    newest = conn.execute(
        "SELECT MAX(trade_date_iso) AS d FROM flex_trade WHERE source = 'flex'"
    ).fetchone()["d"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM flex_trade WHERE source = 'live'"
    ).fetchone()["n"]
    return FlexCoverage(
        through=date.fromisoformat(str(newest)) if newest else None,
        live_pending=int(pending or 0),
    )


# ── The snapshot the Panel layer reads ────────────────────────────────────────


@dataclass(frozen=True)
class DashboardSnapshot:
    """Everything the dashboard renders, as of one instant.

    `as_of` is when the poll *completed*, and `age_seconds` is derived from it on every
    read. Staleness is a first-class output here, not an afterthought: a trading surface
    showing stale numbers with no indication is the worst failure this module can
    produce, so the age travels with the data rather than being tracked separately by
    whatever happens to be drawing it.

    `error` is set when the IBKR half of the poll failed. The Flex half is independent —
    it is a local SQLite read — so a snapshot can legitimately carry realised windows
    with no ledger, which is exactly what a gateway logout looks like.
    """

    as_of: datetime
    ledger: LedgerSnapshot | None = None
    positions: tuple[Position, ...] = ()
    week: RealisedWindow | None = None
    month: RealisedWindow | None = None
    ytd: RealisedWindow | None = None
    # One RoundTripStats per window, keyed "week"/"month"/"ytd" to match the three
    # attributes above. A single set of stats reused across windows renders week figures
    # under a YTD heading — caught in the Phase 3 smoke, and the reason these are keyed
    # rather than singular.
    stats: Mapping[str, RoundTripStats] = field(default_factory=dict)
    series: tuple[RealisedPoint, ...] = ()
    coverage: FlexCoverage | None = None
    error: str | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since this snapshot was taken. Never negative."""
        ref = now or datetime.now(UTC)
        return max(0.0, (ref - self.as_of).total_seconds())


def empty_snapshot(now: datetime | None = None, error: str | None = None) -> DashboardSnapshot:
    """A snapshot carrying no data — what the poller serves before its first poll."""
    return DashboardSnapshot(as_of=now or datetime.now(UTC), error=error)


# Reconciliation tolerance between the summed positions and the ledger's unrealised P&L.
#
# NOT a rounding allowance. The two come from different IBKR endpoints, and they do not
# agree while a fast leg is moving: measured 2026-08-03 in `opening_status.py`, CL (2
# contracts x 1,000 bbl, one $0.01 tick = $20.00) moved +$140 and the two sat **$59.99
# apart, unchanged, for 52 seconds** — a real lag, not a sampling race. $250 is about
# four times the largest gap observed, chosen there by the user on that evidence.
#
# The same number as `opening_status._RECONCILE_TOLERANCE`, deliberately restated rather
# than imported: that module reaches for `ClaudeToolkit` at import time, and this one is
# the pure data layer. If one moves, move both — the evidence above is shared.
RECONCILE_TOLERANCE = 250.00


@dataclass(frozen=True)
class Reconciliation:
    """Whether the summed positions agree with the ledger's unrealised P&L.

    Two different IBKR endpoints produce these, and they must agree to rounding. Unlike
    `opening_status.reconcile_positions_against_ledger`, which re-parses rendered
    markdown because that is all the chat block has, this works on the structured
    figures the poller already holds — so it cannot fail to parse and it can compare
    currencies rather than guessing at a bare `$`.

    `checked` is False when there is nothing to compare (no ledger, or no positions).
    That is not a pass: the caller must not render "reconciled" for a check that never
    ran.
    """

    checked: bool
    positions_total: float
    ledger_total: float
    currency: str
    mixed_currency: bool = False

    @property
    def delta(self) -> float:
        """Absolute gap, rounded to cents **before** comparing.

        These are cent-denominated figures that do not survive binary float exactly:
        summing the real 2026-08-03 positions yields -3638.5099999999998, so a delta
        that is exactly five cents measures as 0.0500000000001819 — enough to trip a
        tolerance of 0.05 by 1.8e-13 and raise an integrity alarm on money that
        reconciles perfectly (the same trap `opening_status` documents).
        """
        return round(abs(self.positions_total - self.ledger_total), 2)

    @property
    def agrees(self) -> bool:
        """True when the two sources agree within `RECONCILE_TOLERANCE`.

        False when the check did not run — an unverified figure is not a verified one.
        """
        return self.checked and not self.mixed_currency and self.delta <= RECONCILE_TOLERANCE


def reconcile(snapshot: DashboardSnapshot) -> Reconciliation:
    """Compare the summed position P&L against the ledger's unrealised P&L.

    Measured live 2026-08-04 on this account: positions summed -11,618.31 against a
    ledger reading -11,618.32 — one cent, which is what agreement looks like.

    A position denominated in a currency other than the ledger's makes the sum invalid,
    so that case is flagged rather than compared. `mixed_currency` is reported instead
    of a delta: IGV once priced a US ETF in MXN, so this is a real path, not a
    hypothetical, and a cross-currency sum would produce a confident wrong number.
    """
    led = snapshot.ledger
    if led is None or not snapshot.positions:
        return Reconciliation(False, 0.0, led.unrealised_pnl if led else 0.0,
                              led.currency if led else "USD")
    currencies = {p.currency for p in snapshot.positions if p.currency}
    total = sum(p.unrealised_pnl for p in snapshot.positions)
    return Reconciliation(
        checked=True,
        positions_total=total,
        ledger_total=led.unrealised_pnl,
        currency=led.currency,
        mixed_currency=bool(currencies - {led.currency}),
    )


def build_flex_sections(
    conn: sqlite3.Connection, today: date
) -> dict[str, Any]:
    """The whole SQLite half of a poll: windows, round trips, curve, coverage.

    Grouped into one function so the poller makes a single `asyncio.to_thread` hop with
    a single connection, and so the date arithmetic lives in one place rather than being
    repeated at each call site. `today` is injected rather than read from the clock,
    which is what makes the windows testable against a fixture.

    The realised curve runs from the start of the year — the widest window the
    requirements ask for — so the week/month/YTD chart views are slices of one query
    rather than three round trips.

    Round-trip stats, by contrast, are computed **per window**: they are counts, and a
    count cannot be sliced out of a wider one. Reusing the week's stats under a month or
    YTD heading puts a right number under a wrong label, which is the failure this
    project treats as worse than a missing number.
    """
    bounds = {
        "week": (week_start(today), today),
        "month": (month_start(today), today),
        "ytd": (year_start(today), today),
    }
    return {
        **{name: realised_window(conn, lo, hi) for name, (lo, hi) in bounds.items()},
        "stats": {name: round_trip_stats(conn, lo, hi) for name, (lo, hi) in bounds.items()},
        "series": realised_series(conn, *bounds["ytd"]),
        "coverage": flex_coverage(conn),
    }
