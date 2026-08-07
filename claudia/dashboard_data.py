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
alternative — regex-scraping rendered output, which the retired chat account block had
to do because rendered markdown was all it had — is not a pattern to copy anywhere.

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
ledger figure beside them does not follow that roll: it rolls once on IBKR's own
accounting boundary, late in the ET evening, at an hour that varies. That is measured,
not assumed — see `REALISED_LEDGER_WINDOW` below, where seven reads spanning both the
18:00 ET futures roll and the 20:00 ET stock roll never moved off -2,656.11, and where
note (6) kills midnight ET, midnight UTC and a fixed clock hour in turn. Calling it a
"calendar" day was wrong and is corrected there.

So the two realised figures on the dashboard differ on the day boundary as well as on
coverage, and `panel_dashboard.coverage_line` states both on the surface rather than
leaving a reader to discover them by subtraction. They are *also* defined on different
cost bases — but that difference has never been observed to move them apart, and
`RealisedWindow` carries the measurement that corrected the earlier claim that it did.

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
from dataclasses import dataclass, field, replace
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

# Quantity tolerance for "the same position". Quantities are exact in principle but pass
# through float arithmetic on both sides; fractional shares make an integer test wrong,
# and a strict `==` on floats would silently decline every reconstruction it should
# accept. Far below any real fractional-share fill.
_QTY_EPSILON = 1e-6


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


# SETTLED 2026-08-04, and CORRECTED 2026-08-05: ledger `realizedpnl` is **one day's**
# realised P&L — not a trading session, and not a cumulative total. The 2026-08-04 work
# below called that day a *calendar* day. It is not one, and the correction is at the
# bottom of this block: the accumulator rolls in the late evening ET, hours before
# midnight, so between the roll and midnight the tile already shows the next day.
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
# (6) THE RESET — the thing (5) could not observe, measured 2026-08-05 while reconciling
#     a CL round trip. It is NOT midnight, and the "calendar day" wording above was
#     wrong. Three observations, each a read of this field:
#
#       * 2026-08-04 20:02 ET  the figure still held the round turn closed at 20:56 ET
#                              on 08-03 (CL +1,945.28), so no roll had happened yet.
#       * 2026-08-05 21:55 ET  the figure held only fills from 22:31:53 ET on 08-04
#                              onward — so that roll HAD happened by 22:31:53 ET on
#                              08-04, while a fill at 22:31 ET on 08-04 is still 08-04
#                              by the calendar. Midnight ET is dead.
#       * 2026-08-05 21:55 ET  trade-date-08-05 fills were still present, and 01:55 UTC
#                              on 08-06 had already passed. Midnight UTC is dead.
#
#     Which left one candidate: a fixed clock time in 21:55 ET < roll <= 22:31:53 ET.
#     **It was tested and it is dead too.** A one-a-minute watch held the field under
#     observation from 21:55:10 to 22:31:53 ET on 08-05 — 37 consecutive reads, ending
#     exactly on the bound — and it never moved. The roll happened before 22:31:53 ET on
#     08-04 and had not happened by 22:31:53 ET on 08-05; no single clock time satisfies
#     both.
#
#     So the boundary is a broker-side accounting event in the late ET evening whose
#     hour VARIES night to night. That is the honest end state: the window is one IBKR
#     accounting day, its roll is not midnight in any timezone and not a fixed hour, and
#     the exact trigger is unknown. Do not put a specific time in user-facing text —
#     an earlier draft of `panel_dashboard.coverage_line` said "between 21:55 and 22:31
#     ET" on the strength of the bracket above, one watch before that bracket died.
#
# (7) NEGATIVE CONTROL for a rival rule — "resets whenever the position goes flat".
#     CL went flat at 22:32 ET on 08-04 and reopened at 12:08 ET on 08-05, yet the
#     08-05 figure still carries the realisations from *before* that flat. Ruled out.
#
# What the accumulator measures, also 2026-08-05 and exact to the cent: realisation is
# booked at the CLOSING fill, priced ENTRY -> EXIT at the traded prices, carrying BOTH
# legs' commissions. It is not settlement-relative. CL's 2 lots opened at 80.84/81.02 on
# trade date 08-04 and closed at 74.78/74.70 on 08-05 — across the 08-04 settlement of
# 75.77 — and the reported figure uses 80.84/81.02, not 75.77. Daily variation margin
# moves cash; it does not re-base this number. Full arithmetic: docs/project-status.md
# § Live Test Log, 2026-08-05.
#
# "day" therefore still names the window, and the label reads "Realised today" either
# way — but the roll is a broker accounting boundary in the late ET evening, not
# midnight, and `panel_dashboard.coverage_line` now says so rather than saying "calendar".
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

    The positions table renders `symbol`, `asset_class`, `quantity`, `average_price`,
    `economic_entry`, `market_price`, `market_value`, `unrealised_pnl` and `currency`.
    `conid`, `average_cost`, `multiplier` and `realised_pnl` are carried but not
    displayed — deliberately, and named here so a reader does not go looking for where
    they are shown: `conid` is the contract's only unambiguous identity (a ticker is not
    a unique key, and IGV once priced a US ETF in MXN because of it), and `realised_pnl`
    is the sibling of the figure beside it.

    ⚠ **`average_cost` is per *contract*; `average_price` is per *unit*.** For anything
    with a multiplier they are different numbers, and only the second one is a price.
    Measured 2026-08-04 on the live account, CL SEP2026 reported `avgCost` **80,932.36**
    against `avgPrice` **80.93236** and `mktPrice` **75.14** — so the table, which was
    rendering `avgCost`, put an entry level three orders of magnitude off its own last
    price in the next column. `average_price` is what the table shows now; `average_cost`
    is kept because it is the field IBKR's own P&L arithmetic uses.

    `economic_entry` is **ours, not IBKR's**: the average price of the lots still open,
    FIFO-matched over this account's own fills, with no cost basis adjustment of any
    kind. It is None whenever it could not be reconstructed with certainty — see
    `economic_entries`, which would rather show nothing than a plausible wrong entry.
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
    average_price: float = 0.0
    multiplier: float | None = None
    economic_entry: float | None = None
    # IBKR's `name`: the instrument's descriptive name, so a row cannot be misread off a
    # ticker alone. NOT `fullName`, which despite the name is the contract *label* —
    # measured live 2026-08-07: GLD name='SPDR GOLD SHARES' fullName='GLD', and
    # CL name='Light Sweet Crude Oil' fullName="CL Sep'26". **IBKR truncates this field
    # itself** (IGV came back 'ISHARES EXPANDED TECH-SOFTWA', 28 chars, cut mid-word), so
    # a short value here is IBKR's doing and must not be "completed" by us. Empty string
    # when absent — the lean futures row that omits `ticker` and `multiplier` can omit
    # this too, and a blank cell is the honest rendering of a field IBKR did not send.
    name: str = ""
    # Live top-of-book, attached by `with_quotes` after the positions are fetched. None
    # means no quote was obtained for this contract — a gateway failure, an unopened
    # stream on the first poll, or a contract IBKR returned nothing for. It is NOT a
    # price of zero and NOT a reason to blank the row: `market_price` is still IBKR's
    # own figure and remains the fallback.
    quote: Quote | None = None

    @property
    def last_price(self) -> float:
        """The price to show as `Last`: the live quote when there is one, else IBKR's.

        **These are two different numbers and the gap is real money.** Measured live
        2026-08-07 during RTH, IBKR's `mktPrice` on the positions endpoint against the
        same instant's top-of-book: GLD 398.7787 vs 399.00, CL 77.73 vs 77.56, IGV
        102.9661 vs 102.89. The positions endpoint is cached — IBKR says so itself, in
        the description of the endpoint built to replace it ("provides near-real time
        updates and removes caching otherwise found in the
        /portfolio/{accountId}/positions/{pageId} endpoint") — and it was measured flat
        to the tick across seven samples over three minutes of an open session.

        A `C`-prefixed last (prior close) or a non-live feed still populates this: it is
        the best available price and blanking it would be worse. The distinction is
        carried on the `Quote` and disclosed by the pane, not hidden by silently
        preferring the other stale number.

        ⚠ `market_value` and `unrealised_pnl` are IBKR's and computed on IBKR's cached
        price, so they will NOT tie exactly to this column. That divergence is lag, it
        is accepted deliberately (user, 2026-08-07: keep the IBKR figures reconcilable
        and state the lag), and `quote_note` is what states it.
        """
        if self.quote is not None and self.quote.last is not None:
            return self.quote.last
        return self.market_price

    @property
    def cost_basis(self) -> float | None:
        """IBKR's own cost basis for the whole position: `average_cost * quantity`.

        **`average_cost` is per contract, so the multiplier must NOT be applied again.**
        That is not an inference — the identity `market_value - cost_basis ==
        unrealised_pnl` was checked against the live account on 2026-08-07 and held to
        the cent on all three open positions, including the futures row where a
        wrongly-applied 1000x multiplier would have been unmissable:

            GLD  50 x    383.22 ->  19,938.93 - 19,161.00 =   778.18 == IBKR's uPnL
            CL    2 x 76,697.36 -> 155,460.01 -153,394.72 = 2,065.29 == IBKR's uPnL
            IGV 100 x     97.63 ->  10,296.61 - 9,763.42  =   533.19 == IBKR's uPnL

        None when `average_cost` or `quantity` is zero: a basis of zero is not a basis,
        and it is the denominator of `pct_unrealised` below.
        """
        if not self.average_cost or not self.quantity:
            return None
        return self.average_cost * self.quantity

    @property
    def pct_unrealised(self) -> float | None:
        """Unrealised P&L as a percentage of IBKR's cost basis.

        **Both halves are IBKR's** — its `unrealizedPnl` over its own `average_cost` —
        which is the whole point of the column and a deliberate choice (user,
        2026-08-07). This surface also carries `economic_entry`, the FIFO average of the
        open lots, and a percentage computed against *that* would be a different and
        equally defensible number: on IGV the two bases differ by 669.62. Mixing them —
        IBKR's P&L over our entry — would be neither, and would reconcile with nothing.
        The rule chosen for this dashboard is that a figure presented as IBKR's must be
        checkable against IBKR's own screen.

        Absolute value in the denominator so a **short** position reports the sign of its
        own P&L and never inverts it. This one is defensive, not measured: the live book
        held no short on 2026-08-07, so which sign IBKR puts on `avgCost` for a short
        position is **unverified here**. `abs()` makes the column correct either way —
        with a negative `avgCost` the product is already positive, and with a positive
        one the absolute value rescues it — which is precisely why it is written this way
        rather than after checking. Verify against a real short before claiming more.
        """
        basis = self.cost_basis
        if basis is None:
            return None
        return self.unrealised_pnl / abs(basis) * 100.0

    @property
    def basis_delta(self) -> float | None:
        """`average_price - economic_entry` per unit, or None when either is unknown.

        Positive means IBKR carries a *higher* basis than the position was actually
        entered at, so IBKR's `unrealised_pnl` reads worse than the fills alone imply.

        None when `average_price` is 0.0 as well as when the entry is unreconstructed:
        a missing basis is not a basis of zero, and subtracting the entry from it would
        turn an absent field into a confident, enormous number.

        ⚠ **On a position that has been partly sold, some of this is lot-matching
        convention rather than adjustment.** `economic_entry` is FIFO over open lots;
        IBKR's basis did not move when 50 of 100 GLD shares were sold on 2026-08-04,
        which an average-cost figure does not and a FIFO one would. So the two are not
        guaranteed to be answering with the same lot convention, and the delta is
        honestly "the gap between these two numbers", not "the tax adjustment". The
        distinction is why nothing here is labelled a wash-sale figure and why the
        column is disclosed rather than alarmed on. It does not weaken the large cases:
        GLD's whole delta is 7.69 while IGV's is 669.62, and no choice of lot convention
        moves a number that size.
        """
        if self.economic_entry is None or not self.average_price:
            return None
        return self.average_price - self.economic_entry

    @property
    def basis_delta_value(self) -> float | None:
        """`basis_delta` in money — exactly the gap between the two unrealised P&Ls.

        `(average_price - economic_entry) * quantity * multiplier`. On the live account
        2026-08-04 this was +669.62 on IGV: IBKR showed +437.41 unrealised where the
        fills alone imply roughly +1,107. That is the number a sizing decision turns on,
        which is why it is money and not a per-share difference.

        **None when the multiplier could not be established**, which is not a
        hypothetical: IBKR served a *lean* CL SEP2026 row on one poll and a full one on
        the next, minutes apart (measured 2026-08-04 — the lean row had no `ticker`
        either, so the symbol column fell back to `contractDesc`). Defaulting a missing
        multiplier to 1 published +0.00472 where the answer was +4.72, understating it
        by a factor of a thousand and silently. A blank cell is recoverable; a money
        figure off by 1000x on a futures row is not.
        """
        delta = self.basis_delta
        if delta is None or not self.multiplier:
            return None
        return delta * self.quantity * self.multiplier


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
        # `avgCost` is per contract, `avgPrice` per unit, and their ratio *is* the
        # multiplier. Deriving it from two fields on the same row keeps the three
        # internally consistent; when neither route works the multiplier stays None and no
        # money figure is computed from it (see `Position.basis_delta_value`).
        #
        # ⚠ **IBKR sends `multiplier: 0.0` on equities — not a missing field.** Measured on
        # the live account 2026-08-06: GLD and IGV both reported `multiplier=0.0` while ES
        # SEP2026 reported `50.0`. The `or` below is therefore load-bearing rather than
        # defensive: 0.0 is falsy, so equities fall through to the `avgCost / avgPrice`
        # ratio and land on 1.0. Rewriting it as `if raw_multiplier is not None` — the
        # obvious "tidy-up" — would take every equity to a zero multiplier.
        #
        # The multiplier is always OBTAINED, never assumed. Three independent routes agree
        # on this account (measured 2026-08-06): IBKR's own field, `avgCost / avgPrice`,
        # and `net_amount / price / size` off a fill. All three give ES 50, CL 1000,
        # STK 1 — recorded here as observations that any future change must still
        # reproduce, not as constants to look up. A hardcoded table would be a second,
        # drifting definition of a contract property IBKR already publishes, and would be
        # wrong the first time a contract's terms changed.
        avg_cost = _as_float(row.get("avgCost"))
        raw_multiplier = _as_float(row.get("multiplier"))
        avg_price = _as_float(row.get("avgPrice"))
        if not avg_price and avg_cost and raw_multiplier:
            avg_price = avg_cost / raw_multiplier
        multiplier = raw_multiplier or (
            avg_cost / avg_price if avg_price and avg_cost else None
        )
        out.append(
            Position(
                conid=int(_as_float(row.get("conid"))),
                symbol=str(row.get("ticker") or "").strip() or desc,
                description=desc,
                name=str(row.get("name") or "").strip(),
                asset_class=str(row.get("assetClass") or "").strip(),
                quantity=_as_float(row.get("position")),
                average_cost=avg_cost,
                market_price=_as_float(row.get("mktPrice")),
                market_value=_as_float(row.get("mktValue")),
                unrealised_pnl=_as_float(row.get("unrealizedPnl")),
                realised_pnl=_as_float(row.get("realizedPnl")),
                currency=str(row.get("currency") or "").strip().upper(),
                average_price=avg_price,
                multiplier=multiplier,
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


# ── Economic entry: what the position was actually entered at ─────────────────
#
# IBKR's `avgPrice` is a *basis*, and a basis is a fiscal object: it absorbs costs and
# adjustments that have nothing to do with where the trade was entered. Measured
# 2026-08-04 on the live account, IGV carried a basis of 97.634216 against fills whose
# open lots average **90.938050** — 6.70 a share, 669.62 on the position. A trader
# reading the basis as an entry level is reading a number that was never traded at.
#
# So the entry is reconstructed from the account's own fills, and the *method* is chosen
# to match the realised windows rather than to match IBKR: FIFO over open lots, at fill
# price, with no adjustment of any kind. `flex_trade.fifo_pnl_realized` is FIFO, so the
# lots this leaves open are exactly the ones those windows have not yet realised. A
# running-average reconstruction was tried first and rejected — it disagreed with IBKR
# on every position, including CL where the only real difference is commission, which
# means it was measuring the method rather than the adjustment.


def _fifo_open_average(fills: Sequence[tuple[float, float]]) -> tuple[float | None, float]:
    """FIFO the `(signed_quantity, price)` fills and return `(open average, open qty)`.

    Oldest first. A fill on the same side as the position opens a lot; a fill on the
    opposite side consumes lots from the front. Reaching flat clears the queue, so the
    next fill starts a fresh position rather than averaging against a closed one — the
    reason a six-year history does not contaminate a position opened last week.

    Returns `(None, 0.0)` for a flat book: there is no entry price for a position that
    is not held, and returning 0.0 would put a tradeable-looking level on screen.
    """
    lots: list[list[float]] = []  # [remaining signed qty, price]
    position = 0.0
    for quantity, price in fills:
        if not quantity:
            continue
        if position == 0.0 or (position > 0) == (quantity > 0):
            lots.append([quantity, price])
        else:
            remaining = abs(quantity)
            while remaining > _QTY_EPSILON and lots:
                lot = lots[0]
                take = min(remaining, abs(lot[0]))
                lot[0] += -take if lot[0] > 0 else take
                remaining -= take
                if abs(lot[0]) < _QTY_EPSILON:
                    lots.pop(0)
            # A reversal through zero leaves `remaining` unconsumed; it becomes the
            # opening lot of the new, opposite-side position.
            if remaining > _QTY_EPSILON:
                lots.append([remaining if quantity > 0 else -remaining, price])
        position = round(position + quantity, 8)
    held = sum(lot[0] for lot in lots)
    if abs(held) < _QTY_EPSILON:
        return None, 0.0
    return sum(lot[0] * lot[1] for lot in lots) / held, held


def economic_entries(
    conn: sqlite3.Connection, positions: Sequence[Position]
) -> dict[int, float]:
    """Average entry price per conid, for the positions it can reconstruct *exactly*.

    A conid is in the result only when the reconstruction independently reproduces
    IBKR's own reported quantity to `_QTY_EPSILON`. That check is the whole safety
    argument: it is one line, it is not a heuristic, and it catches every way this can
    go wrong at once — a position opened before the stored history begins, a transfer or
    corporate action that never appeared as a fill, a split, a symbol mismatch on the
    live rows. Absence from the dict means "not established", and the view shows nothing
    rather than a plausible wrong entry. On a trading surface those are not close to
    equivalent.

    **Live rows are matched by symbol, because they carry no `conid`** (measured
    2026-08-04: all nine of them). Flex states the conid on the same row T+1, so this
    only ever affects today's fills — but a ticker is not a unique key, so any symbol
    that could belong to more than one held contract disqualifies *every* position it
    touches rather than being resolved by a guess. Futures are matched on
    `underlying_symbol` too, since the live feed reports `CL` where Flex reports `CLU6`.

    Prices are fill prices, deliberately excluding commission: the question this answers
    is "where did I get in", which is a level on a chart, not a cost. IBKR's basis
    includes commission, so a position with no adjustment at all still shows a small
    delta — CL measured +0.00236 a barrel, 4.72 on two contracts, which is what
    commission looks like and is exactly why the column is disclosed rather than alarmed
    on.
    """
    wanted = {p.conid: p for p in positions if p.conid and abs(p.quantity) > _QTY_EPSILON}
    if not wanted:
        return {}

    # Symbols each candidate conid has traded under, for matching the conid-less live
    # rows. Built for all candidates at once so ambiguity is visible across the account.
    aliases: dict[int, set[str]] = {}
    placeholders = ",".join("?" * len(wanted))
    for row in conn.execute(
        f"SELECT conid, symbol, underlying_symbol FROM flex_trade "
        f"WHERE conid IN ({placeholders})",
        [str(c) for c in wanted],
    ):
        names = aliases.setdefault(int(row["conid"]), set())
        names.update(str(n).strip().upper() for n in (row["symbol"], row["underlying_symbol"]) if n)

    ambiguous = {
        conid
        for conid, names in aliases.items()
        for other, other_names in aliases.items()
        if other != conid and names & other_names
    }

    live_rows = [
        (str(r["symbol"] or "").strip().upper(), _as_float(r["quantity"]), _as_float(r["trade_price"]))
        for r in conn.execute(
            "SELECT symbol, quantity, trade_price FROM flex_trade "
            "WHERE source != 'flex' ORDER BY date_time"
        )
    ]

    entries: dict[int, float] = {}
    for conid, position in wanted.items():
        if conid in ambiguous:
            log.debug("Economic entry declined for conid %s: symbol shared with another position", conid)
            continue
        fills = [
            (_as_float(r["quantity"]), _as_float(r["trade_price"]))
            for r in conn.execute(
                "SELECT quantity, trade_price FROM flex_trade "
                "WHERE conid = ? AND source = 'flex' ORDER BY trade_date, date_time",
                (str(conid),),
            )
        ]
        names = aliases.get(conid, set())
        fills += [(q, price) for symbol, q, price in live_rows if symbol in names]
        average, held = _fifo_open_average(fills)
        if average is None or abs(held - position.quantity) > _QTY_EPSILON:
            log.debug(
                "Economic entry declined for conid %s: reconstructed %s vs IBKR %s",
                conid, held, position.quantity,
            )
            continue
        entries[conid] = average
    return entries


def with_economic_entries(
    positions: Sequence[Position], entries: Mapping[int, float]
) -> tuple[Position, ...]:
    """Attach reconstructed entries to positions, leaving unreconstructed ones at None."""
    return tuple(
        replace(p, economic_entry=entries[p.conid]) if p.conid in entries else p
        for p in positions
    )


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
    never be added.** Both are realised P&L, over different windows (this one is T+1 and
    excludes today; the ledger's is today only) and bucketed on different day boundaries
    — those two differences are permanent and are the reason the figures on screen will
    not tie out.

    They are also *defined* on different cost bases, which is the reason for the detail
    below. How far apart that drives them is now measured rather than assumed, and the
    answer was: not at all, in the one case where both sides priced the same trade.
    See "What the CRM close settled" at the end.

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

    **What the CRM close settled (measured 2026-08-05, and it corrected this docstring).**
    The 2026-08-04 sale of 50 CRM was the first case where both sides priced the same
    trade, so it was left as an open question with two possible answers: if Flex reported
    roughly **-252.60**, the two bases recognise the disallowed loss at different times
    and diverge by an order of magnitude; if it reported roughly **-2,810**, they
    recognise it identically. The statement arrived the next morning reading
    **-2,810.467842** at trade level, against the ledger's **-2,810.47** — agreement to
    the cent, and the lot rows sum to the same figure over 15 lots, all opened 2026-06-03
    (the replacement shares), carrying no wash-sale row on the close.

    So the -252.60 figure this docstring used to assert was a *projection*, not a
    measurement: it assumed the statement basis excluded the disallowed loss. It does
    not — IBKR incorporates that loss into the replacement shares' basis on **both**
    sides, exactly as its published rule says, and both recognise it on the same day.

    What survives the correction, stated no more strongly than the evidence supports:
    the two bases are different by construction and are not guaranteed to agree, and the
    GLD/CRM `avgCost` measurements above remain exact — but they measure `avgCost`
    against a *reconstructed FIFO purchase average* (`economic_entries`), which is not
    the statement basis. No measurement here shows the two **reported** realised figures
    disagreeing. They may; it has not been observed.
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
    # `None` means the order book was NOT established — not that it is empty. The two are
    # opposite claims on a trading surface: an empty tuple asserts "you have nothing
    # resting", which is exactly the wrong thing to show when the lookup failed. Orders
    # come from `/iserver/*` and the account half from `/portfolio/*`; those fail
    # independently (measured 2026-08-04, ledger live while orders returned "no bridge"),
    # so this field carries its own outcome rather than inheriting the account's.
    orders: tuple[LiveOrder, ...] | None = None
    week: RealisedWindow | None = None
    month: RealisedWindow | None = None
    ytd: RealisedWindow | None = None
    # One RoundTripStats per window, keyed "week"/"month"/"ytd" to match the three
    # attributes above. A single set of stats reused across windows renders week figures
    # under a YTD heading — caught in the Phase 3 smoke, and the reason these are keyed
    # rather than singular.
    stats: Mapping[str, RoundTripStats] = field(default_factory=dict)
    # Per-asset-class breakdowns keyed "day"/"week"/"month"/"ytd". Unlike `stats` above
    # these are BRIDGED: they include the days Flex has not delivered yet, so "day" is a
    # real figure rather than a permanently empty one. The KPI strip reads day/week; the
    # P&L pane reads month/ytd.
    breakdowns: Mapping[str, BridgedWindow] = field(default_factory=dict)
    series: tuple[RealisedPoint, ...] = ()
    coverage: FlexCoverage | None = None
    error: str | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since this snapshot was taken. Never negative."""
        ref = now or datetime.now(UTC)
        return max(0.0, (ref - self.as_of).total_seconds())

    def without_account(self) -> DashboardSnapshot:
        """A copy with the IBKR-derived half removed, Flex windows untouched.

        What the view renders once the account data has gone genuinely stale. The poller
        keeps carrying the last known ledger and positions — that is deliberate and
        stays, because `as_of` must keep ageing for the status line to be able to say
        *how long* it has been — but a trading surface should not keep displaying
        figures it can no longer vouch for. Numbers that are minutes old look exactly
        like numbers that are current, and the difference is only in a line of text
        above them.

        The Flex windows are **not** cleared, and that asymmetry is the point: they come
        from a local SQLite file that has nothing to do with the gateway. Blanking them
        because IBKR went away would be inventing an outage in the half of the dashboard
        that is still perfectly good.
        """
        return replace(self, ledger=None, positions=(), orders=None)


@dataclass(frozen=True)
class LiveOrder:
    """One working order from `/iserver/account/orders`.

    Added 2026-08-05 after a live place → modify → cancel run: the order book was
    visible only in the chat's opening message, printed once and never updated, so an
    order staged, modified and cancelled during a session left no trace on the dashboard.
    Positions were correct throughout — a resting limit order is not a position — but
    nothing on screen tracked the book.

    `origin` preserves the distinction the chat block already drew: orders placed through
    ClaudIA carry a `CLAUDIA-` local id, while anything from TWS, mobile or the web portal
    does not and is read-only through this API.
    """

    order_id: str
    symbol: str
    side: str
    quantity: float
    filled: float
    price: float | None
    order_type: str
    tif: str
    status: str
    origin: str = ""

    @property
    def is_claudia_staged(self) -> bool:
        """Whether ClaudIA staged this order, by its local-id convention."""
        return self.origin.upper().startswith("CLAUDIA-")


def parse_orders(rows: Sequence[Any]) -> tuple[LiveOrder, ...]:
    """Type IBKR's live-order rows, skipping any row that carries no order id.

    IBKR's field names differ from the ones it accepts on placement — `orderDesc`,
    `remainingQuantity`, `totalSize` — and a live-measured wrinkle it shares with the
    positions endpoint: the same order can come back with `side` as `B` and `orderType`
    as `LIMIT` on one call and the placement vocabulary on another. Nothing here
    normalises that: this layer reports what IBKR said, and the comparison logic in
    `order_flow` owns the equivalences.
    """
    out: list[LiveOrder] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        order_id = row.get("orderId") or row.get("order_id")
        if order_id in (None, ""):
            continue
        total = _as_float(row.get("totalSize") or row.get("quantity") or 0)
        # A blank `remainingQuantity` means "not reported", NOT "nothing remaining".
        # `_as_float` maps None to 0.0, so reading it directly would compute
        # filled = total and render an untouched resting order as fully filled — and
        # IBKR does serve lean rows with fields missing (measured on the positions
        # endpoint, 2026-08-04). Unknown falls back to the full size, i.e. 0 filled,
        # which is the safe direction to be wrong in on a trading surface.
        raw_remaining = row.get("remainingQuantity")
        remaining = total if raw_remaining in (None, "") else _as_float(raw_remaining)
        out.append(
            LiveOrder(
                order_id=str(order_id),
                symbol=str(row.get("ticker") or row.get("symbol") or "").strip(),
                side=str(row.get("side") or "").strip(),
                quantity=total,
                filled=max(0.0, total - remaining),
                price=_as_float(row["price"]) if row.get("price") not in (None, "") else None,
                order_type=str(row.get("orderType") or row.get("origOrderType") or "").strip(),
                tif=str(row.get("timeInForce") or row.get("tif") or "").strip(),
                status=str(row.get("status") or row.get("order_status") or "").strip(),
                origin=str(row.get("order_ref") or row.get("orderRef") or "").strip(),
            )
        )
    return tuple(out)


# The three top-of-book fields the positions table renders, plus the availability flag.
# Tag meanings are IBKR's, from the snapshot endpoint reference:
#   31   Last Price — "may contain one of the following prefixes: C - Previous day's
#        closing price. H - Trading has halted."
#   82   Change     — difference between the last price and the previous trading day's close
#   83   Change %   — the same difference as a percentage
#   6509 Availability — first char R=RealTime, D=Delayed, N=NotSubscribed, Z=Frozen,
#        Y=FrozenDelayed, O=API agreement incomplete
# Source: https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-market-data/get-md-snapshot.md
_QUOTE_FIELDS = ("31", "82", "83", "6509")

# Prefixes IBKR may put on field 31. Stripped to recover the number, and each one is
# recorded rather than discarded — a prior close and a halted print are both "not a
# live trade", which is the distinction this whole quote layer exists to make.
_LAST_PREFIXES = {"C": "last_is_close", "H": "halted"}


@dataclass(frozen=True)
class Quote:
    """Live top-of-book for one contract, from `/iserver/marketdata/snapshot`.

    Every field is optional because **the first snapshot request for a conid returns no
    prices at all** — it only opens IServer's stream for that instrument ("This initial
    request will not deliver any data, but rather makes the stream available for future
    snapshot requests"). The poller does not sleep-and-retry inside a poll to paper over
    that: it publishes a quote-less row and the next 15-second tick carries the prices.
    Blocking the shared event loop for a second per new contract would be a worse trade
    than one tick of blank cells.

    `status` is the raw 6509 string and `is_live` is the only interpretation of it.

    Note what that does and does not buy: a delayed or frozen price **is** still shown in
    the `Last` column, because it is the best price available and a blank would be worse.
    What `is_live` prevents is it passing *unremarked* — `panel_dashboard.quote_note`
    names the symbols whose feed is not real-time, beside the table. The flag is a
    disclosure mechanism, not a filter.
    """

    conid: int
    last: float | None = None
    change: float | None = None
    change_pct: float | None = None
    status: str = ""
    last_is_close: bool = False
    halted: bool = False

    @property
    def is_live(self) -> bool:
        """True only for a real-time feed (6509 beginning `R`).

        Absence is not liveness: no 6509 at all returns False. D (delayed), N (not
        subscribed), Z/Y (frozen) are all explicitly not live, and a delayed price shown
        as current is the failure this flag exists to prevent.
        """
        return self.status.startswith("R")


def _quote_number(raw: Any) -> tuple[float | None, str]:
    """A snapshot value as `(number, prefix)`. IBKR sends these as strings.

    Returns `(None, "")` for anything unparseable — an empty string, `"n/a"`, or a field
    IBKR simply omitted. One malformed value blanks that field only; it must never
    discard the rest of the quote.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""
    prefix = text[0] if text[0] in _LAST_PREFIXES else ""
    try:
        return float(text[1:] if prefix else text), prefix
    except ValueError:
        return None, ""


def parse_quotes(rows: Sequence[Any]) -> dict[int, Quote]:
    """Snapshot rows into `{conid: Quote}`. Never raises on a malformed row."""
    out: dict[int, Quote] = {}
    for row in rows or []:
        try:
            conid = int(_as_float(row.get("conid")))
        except (TypeError, ValueError):
            continue
        last, prefix = _quote_number(row.get("31"))
        change, _ = _quote_number(row.get("82"))
        change_pct, _ = _quote_number(row.get("83"))
        out[conid] = Quote(
            conid=conid,
            last=last,
            change=change,
            change_pct=change_pct,
            status=str(row.get("6509") or "").strip(),
            last_is_close=prefix == "C",
            halted=prefix == "H",
        )
    return out


def fetch_quotes(client: IBKRClient, conids: Sequence[int]) -> dict[int, Quote]:
    """Top-of-book for `conids`. Blocking HTTP — call via `asyncio.to_thread`.

    One request. The endpoint caps at 100 conids and 50 fields; this asks for four
    fields and a book larger than 100 open positions is not a case this dashboard has,
    so the list is passed whole and the cap is documented rather than defended against.

    Returns `{}` for an empty request rather than calling IBKR with no conids.
    """
    if not conids:
        return {}
    return parse_quotes(client.get_market_snapshot(list(conids), list(_QUOTE_FIELDS)))


def with_quotes(
    positions: Sequence[Position], quotes: Mapping[int, Quote]
) -> tuple[Position, ...]:
    """Attach each position's quote by conid. A position with no quote is left untouched.

    Matched on `conid` and never on symbol: a ticker is not a unique key, and this repo
    has already priced a US ETF in MXN by assuming it was.
    """
    return tuple(replace(p, quote=quotes.get(p.conid)) for p in positions)


def fetch_orders(client: IBKRClient) -> tuple[LiveOrder, ...] | None:
    """Working orders, or **None when the book could not be established**.

    None and `()` are different claims and the caller must keep them apart: `()` says
    "nothing is resting", None says "unknown". Returning `()` on failure would put an
    empty order table on screen and assert the first while meaning the second.

    The brokerage session can be down while the account endpoints answer perfectly —
    measured 2026-08-04, `/portfolio/{id}/ledger` live while `/iserver/account/orders`
    returned HTTP 400 `{"error": "Bad Request: no bridge"}`. That is the case this None
    exists for, and it is why an orders failure must not blank the account half.
    """
    try:
        return parse_orders(client.get_live_orders())
    except Exception as exc:
        log.warning("Dashboard order poll failed: %s", exc)
        return None


def empty_snapshot(now: datetime | None = None, error: str | None = None) -> DashboardSnapshot:
    """A snapshot carrying no data — what the poller serves before its first poll."""
    return DashboardSnapshot(as_of=now or datetime.now(UTC), error=error)


# Reconciliation tolerance between the summed positions and the ledger's unrealised P&L.
#
# NOT a rounding allowance. The two come from different IBKR endpoints, and they do not
# agree while a fast leg is moving: measured 2026-08-03, CL (2 contracts x 1,000 bbl, one
# $0.01 tick = $20.00) moved +$140 and the two sat **$59.99 apart, unchanged, for 52
# seconds** — a real lag, not a sampling race. $250 is about four times the largest gap
# observed, chosen by the user on that evidence.
#
# This is now the only copy. It was one of a pair until 2026-08-05, restated rather than
# imported because the other lived in a module that reaches for `ClaudeToolkit` at import
# time; that copy went with the chat-side reconciliation when the opening account
# statement was retired.
#
# ⚠ Still calibrated against ONE quiet observation. An attempt to measure it under load on
# 2026-08-05 produced 14 samples at exactly 0.00 — but the account held no futures leg and
# equities were pre-open, so nothing was moving and the tolerance was never exercised. A
# flat delta on a frozen market is not evidence that this number is right; it remains
# unvalidated under a fast leg.
RECONCILE_TOLERANCE = 250.00


@dataclass(frozen=True)
class Reconciliation:
    """Whether the summed positions agree with the ledger's unrealised P&L.

    Two different IBKR endpoints produce these, and they must agree to rounding. This
    works on the structured figures the poller already holds, so it cannot fail to parse
    and it can compare currencies rather than guessing at a bare `$`.

    It is the **only** such check since 2026-08-05. The chat used to run a second one that
    re-parsed its own rendered markdown, because rendered markdown was all the opening
    block had; that block and its reconciliation were retired together when the dashboard
    took over the account figures.

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
        reconciles perfectly.
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
    conn: sqlite3.Connection, today: date, reconstruction: Any = None
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

    `reconstruction` is an optional `live_realised.Reconstruction`. When supplied, the
    per-type breakdowns extend past Flex's coverage into the days it has not delivered —
    which is what makes a *daily* win rate possible at all. Without it the breakdowns are
    Flex-only, which is the correct behaviour for a session with no gateway.
    """
    bounds = {
        "week": (week_start(today), today),
        "month": (month_start(today), today),
        "ytd": (year_start(today), today),
    }
    # "day" is deliberately not in `bounds`: `realised_window` and `round_trip_stats` are
    # Flex-only, and Flex never has today (it was two days behind on 2026-08-06). A
    # Flex-derived "today" would always be empty, so the day window exists only in the
    # bridged breakdowns below, which is the only place it can be accurate.
    breakdown_bounds = {"day": (today, today), **bounds}
    coverage = flex_coverage(conn)
    return {
        **{name: realised_window(conn, lo, hi) for name, (lo, hi) in bounds.items()},
        "stats": {name: round_trip_stats(conn, lo, hi) for name, (lo, hi) in bounds.items()},
        "breakdowns": {
            name: bridged_by_type(conn, lo, hi, reconstruction, coverage.through)
            for name, (lo, hi) in breakdown_bounds.items()
        },
        "series": realised_series(conn, *bounds["ytd"]),
        "coverage": coverage,
    }


# -- Win/loss breakdown by asset class ----------------------------------------


@dataclass(frozen=True)
class TypeBreakdown:
    """Realised performance for one asset class over one window.

    **Two different sources, deliberately, and they must stay labelled apart.**

    * `net` is the money figure, from `flex_trade.fifo_pnl_realized` — the rule settled
      against IBKR's own annual statements 6/6 years exactly.
    * `gross_win` / `gross_loss` / the counts come from `flex_lot`, which is
      **pre-wash-sale** tax-lot detail (`Trade == Lot + WashSale`). Summing lots as the
      money figure overstates losses by the disallowed amount.

    So `gross_win + gross_loss` will not always equal `net`, and that is correct rather
    than a bug. A UI showing both must say which is which — the same rule
    `RoundTripStats` already carries.

    A lot is the right unit for a *count*: it is a genuine round trip (open -> close),
    whereas an execution list includes opening legs and wash-sale-zeroed closes that are
    neither a win nor a loss.
    """

    asset_class: str
    net: float
    gross_win: float
    gross_loss: float
    winners: int
    losers: int
    scratches: int

    @property
    def closed_lots(self) -> int:
        """Round trips that closed in this window, decided or not."""
        return self.winners + self.losers + self.scratches

    @property
    def win_rate(self) -> float | None:
        """Winners as a percentage of *decided* lots, or None when nothing decided.

        Scratches are excluded from the denominator: a lot realising exactly 0.00 is
        neither won nor lost, and counting it as a loss understates the rate. None rather
        than 0.0 for an empty window, so the UI renders an em dash instead of a 0% that
        reads like a catastrophic week.
        """
        decided = self.winners + self.losers
        return 100.0 * self.winners / decided if decided else None

    @property
    def win_loss_ratio(self) -> float | None:
        """Gross win over gross loss, or None when nothing was lost.

        None rather than infinity: a window with no losing lot has no ratio, and
        rendering one invites a comparison that does not exist.
        """
        return self.gross_win / abs(self.gross_loss) if self.gross_loss else None

    @property
    def average_win(self) -> float | None:
        """Mean size of a winning lot, or None when there were none."""
        return self.gross_win / self.winners if self.winners else None

    @property
    def average_loss(self) -> float | None:
        """Mean size of a losing lot (negative), or None when there were none.

        Reported beside `average_win` because **the count and the money can tell opposite
        stories, and both are needed to read a window correctly.** One win of 3,000
        against five losses of 600 is a 17% win rate — dreadful by count — and break-even
        in money. The inverse happens too: this account's own week showed STK at 0 wins
        from 23 lots, which reads as a catastrophe until the average loss turns out to be
        about 141, while FUT's 33% win rate sat on losses an order of magnitude larger.

        Splitting by asset class is not cosmetic for the same reason: a futures lot and an
        equity lot differ by their contract multiplier alone, so pooling them makes the
        averages describe nothing real.
        """
        return self.gross_loss / self.losers if self.losers else None


def realised_by_type(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[TypeBreakdown, ...]:
    """Per-asset-class realised performance between `start` and `end` inclusive.

    Returns a row **only for asset classes that actually did something** in the window —
    no zero rows for classes that were never traded. FUT and STK dominate this account;
    OPT or CASH appear when, and only when, they realised. Ordered by absolute net so the
    class that moved the money most is first.

    The two halves are queried separately because the tables disagree on shape, not by
    accident:

    * `flex_trade` has `trade_date_iso` (ISO) and `source`; the sum is filtered to
      `source='flex'` with **no open/close filter** — a buy that closes a short and opens
      a long is flagged `O` and still realises.
    * `flex_lot` has **neither** column. It carries only the compact `YYYYMMDD`
      `trade_date`, and every lot is Flex-derived, so it needs its own bounds format and
      no source predicate.
    """
    lo_iso, hi_iso = start.isoformat(), end.isoformat()
    lo_c, hi_c = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    nets = {
        str(r["asset_category"] or "?"): float(r["net"] or 0.0)
        for r in conn.execute(
            "SELECT asset_category, SUM(fifo_pnl_realized) AS net FROM flex_trade "
            "WHERE source = 'flex' AND trade_date_iso BETWEEN ? AND ? "
            "GROUP BY asset_category",
            (lo_iso, hi_iso),
        )
    }
    lots = {
        str(r["asset_category"] or "?"): r
        for r in conn.execute(
            """
            SELECT asset_category,
                   SUM(CASE WHEN fifo_pnl_realized > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN fifo_pnl_realized < 0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN COALESCE(fifo_pnl_realized, 0) = 0 THEN 1 ELSE 0 END) AS flat,
                   SUM(CASE WHEN fifo_pnl_realized > 0 THEN fifo_pnl_realized ELSE 0 END) AS gw,
                   SUM(CASE WHEN fifo_pnl_realized < 0 THEN fifo_pnl_realized ELSE 0 END) AS gl
              FROM flex_lot
             WHERE trade_date BETWEEN ? AND ?
             GROUP BY asset_category
            """,
            (lo_c, hi_c),
        )
    }

    out = [
        TypeBreakdown(
            asset_class=asset,
            net=round(nets.get(asset, 0.0), 2),
            gross_win=round(float(lot["gw"] or 0.0) if lot is not None else 0.0, 2),
            gross_loss=round(float(lot["gl"] or 0.0) if lot is not None else 0.0, 2),
            winners=int(lot["wins"] or 0) if lot is not None else 0,
            losers=int(lot["losses"] or 0) if lot is not None else 0,
            scratches=int(lot["flat"] or 0) if lot is not None else 0,
        )
        for asset in sorted(set(nets) | set(lots))
        if (lot := lots.get(asset)) is not None or nets.get(asset)
    ]
    return tuple(sorted(out, key=lambda b: -abs(b.net)))


@dataclass(frozen=True)
class BridgedWindow:
    """Per-type realised performance over a window, Flex **plus** the days it lacks.

    The Flex dataset is authoritative and always behind — measured 2026-08-06 it was two
    days behind, so "today's win rate" read from Flex alone was not merely stale, it was
    *empty*, and an empty window renders as a dash rather than as the +1,841.04 the
    account had actually made. That is the gap this type closes.

    `incomplete` is the honesty flag. It is set when a contract in the window could not be
    reconstructed (its opening leg was outside the fill window), meaning the reconstructed
    part is missing some closes. The figures shown are then a floor, not a total, and the
    UI must say so — a silently-short number on a P&L surface is the failure this whole
    module is written against.
    """

    rows: tuple[TypeBreakdown, ...] = ()
    bridged_days: tuple[str, ...] = ()
    incomplete: bool = False
    # Whether a reconstruction was available at all — NOT whether it found anything.
    # `rows == ()` is otherwise two opposite claims wearing the same shape: "nothing
    # closed" and "the gateway was shut, so the days Flex does not cover are unknowable".
    # The day window is where that matters, because no statement ever covers today
    # (IBKR publishes a day's trades T+1), so a gateway-down day window is *always* empty
    # and would otherwise read as a flat, uneventful session. Same rule as
    # `DashboardSnapshot.orders` being `tuple | None`: empty and unknown must not share a
    # representation on a surface a trader acts on.
    reconstructed: bool = False
    # The window's last day — for the day window, the day it *is*. Carried so a heading
    # can name the window it labels instead of deriving a date from `as_of`, which is a
    # different clock reading: `as_of` is stamped when the poll COMPLETES, after
    # `_read_flex` has already called `date.today()`, and a failed poll republishes the
    # PREVIOUS `as_of` on purpose so staleness stays visible. Both make the two disagree —
    # the first across a local midnight, the second for as long as polling is down.
    day: date | None = None

    @property
    def net(self) -> float:
        """Realised across every asset class in the window."""
        return round(sum(r.net for r in self.rows), 2)

    def for_type(self, asset_class: str) -> TypeBreakdown | None:
        """The row for one asset class, or None when it did nothing in this window."""
        return next((r for r in self.rows if r.asset_class == asset_class), None)


def bridged_by_type(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    reconstruction: Any = None,
    coverage_through: date | None = None,
) -> BridgedWindow:
    """`realised_by_type` for the window, extended with the days Flex has not delivered.

    Args:
        conn: Read-only store connection.
        start: First day of the window, inclusive.
        end: Last day of the window, inclusive.
        reconstruction: A `live_realised.Reconstruction`, or None to return Flex alone.
        coverage_through: The newest Flex `trade_date_iso`. Days after it are taken from
            the reconstruction; days up to it are taken from Flex. **Nothing is taken from
            both**, which is what stops a bridged window double-counting the moment Flex
            catches up — the failure mode a naive "add today's live P&L" would hit the
            following morning.

    Returns:
        A `BridgedWindow`. Rows are ordered by absolute net, largest first.
    """
    flex_rows = {r.asset_class: r for r in realised_by_type(conn, start, end)}
    if reconstruction is None:
        return BridgedWindow(rows=tuple(flex_rows.values()), day=end)

    cutoff = coverage_through.strftime("%Y%m%d") if coverage_through else ""
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    days = sorted({
        d for (d, _) in reconstruction.realised if lo <= d <= hi and d > cutoff
    })

    merged: dict[str, TypeBreakdown] = dict(flex_rows)
    for day in days:
        for asset, net in reconstruction.by_type_for_day(day).items():
            wins, losses, flat, gw, gl = reconstruction.stats_for(day, asset)
            base = merged.get(asset)
            merged[asset] = TypeBreakdown(
                asset_class=asset,
                net=round((base.net if base else 0.0) + net, 2),
                gross_win=round((base.gross_win if base else 0.0) + gw, 2),
                gross_loss=round((base.gross_loss if base else 0.0) + gl, 2),
                winners=(base.winners if base else 0) + wins,
                losers=(base.losers if base else 0) + losses,
                scratches=(base.scratches if base else 0) + flat,
            )

    incomplete = any(lo <= d <= hi and d > cutoff for d in reconstruction.declined_days)
    return BridgedWindow(
        rows=tuple(sorted(merged.values(), key=lambda b: -abs(b.net))),
        bridged_days=tuple(days),
        incomplete=incomplete,
        reconstructed=True,
        day=end,
    )
