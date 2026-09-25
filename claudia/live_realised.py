"""Realised P&L for the days Flex has not delivered yet, reconstructed from raw fills.

## Why this exists

The Flex dataset is the authoritative source for realised P&L, and it is **T+1 at best**.
Measured 2026-08-06 it was two days behind: newest `trade_date_iso` was 2026-08-04 while
fills existed on both 08-05 and 08-06. So a dashboard showing "realised this week" from
Flex alone was silently missing two trading days, one of which contained a +945.52 round
trip.

The live Client Portal side cannot fill that gap directly:

* `flex_trade` rows with `source='live'` carry **`realized_pnl: null`** and
  **`asset_category: ''`** — no P&L and no type.
* `/iserver/account/pnl/partitioned` returned `{"upnl": {}}` — unrealised only.
* Ledger `realizedpnl` is a single scalar for **today only**, with no per-instrument or
  per-type breakdown, and nothing for 08-05.

What *is* available is every execution, from `/iserver/account/trades`. This module FIFOs
those into realised P&L per closing execution — and, critically, refuses to publish
whenever it cannot prove the result.

## Pending is decided by execution id, never by a date (gap #69, 2026-09-24)

IBKR states a fill's trade date only in the Flex statement. A live fill carries a UTC
`trade_time` and nothing else, and its UTC date differs from the trade date on evening
futures fills (18:00 ET is the next CME trade date). So this module never derives a day:
an execution is **pending** if and only if its id is not yet a Flex `execution_key`
(`Reconstruction.pending`), and pending P&L is shown as its own line, never placed in a
dated window. The earlier rule, "UTC date after Flex's newest trade date", dropped a futures
fill between 18:00 ET and UTC midnight for a day, and could count a winter after-hours
stock fill twice.

## The three things that make it correct

**1. The opening commission is released at CLOSE, not at fill.** This is IBKR's
convention and it was the entire reason an earlier attempt disagreed with Flex. Measured
2026-08-06 against the Flex dataset:

| day | commission at fill | commission at close | Flex |
|---|---|---|---|
| 2026-08-03 FUT | -3,521.58 | **-3,516.98** | -3,516.98 |
| 2026-08-04 FUT | 590.68 | **590.80** | 590.80 |

Deferring reproduces Flex to the cent on every settled day, and independently matches
ledger `realizedpnl` for today.

**2. The multiplier is derived, never looked up.** `net_amount / price / size` — IBKR's
own arithmetic inverted. It yields ES 50, CL 1000, STK 1 on this account, agreeing with
both IBKR's `multiplier` field and the `avgCost / avgPrice` ratio. A hardcoded table
would be a second, drifting definition of a contract property IBKR already publishes.

**3. The trust check — the reconstruction must reproduce IBKR's own position.** Same rule
as `dashboard_data.economic_entries`, and it is what keeps a partial fill window from
producing a confident wrong answer. Measured 2026-08-06:

| symbol | reconstructed | IBKR | verdict |
|---|---|---|---|
| ES | -1 | -1 | trusted |
| CL | 0 | 0 | trusted |
| IGV / CRM / GLD | 0 / -50 / -50 | 100 / 0 / 50 | **declined** |

All three equities were opened *before* the fill window, so their closing fills had no
opening leg to match. Reconstructed STK for 2026-08-04 came out at **0.00 against Flex's
-3,249.70** — an understatement of the whole position, and exactly the kind of plausible
number that must never reach a screen. Untrusted contracts are excluded and named.

## ⚠ Two traps measured on the live data

* **The `position` field on a fill is NOT a running position.** It is the *current*
  position stamped onto every historical row — ES read `-1` on all ten of its fills,
  including ones where the running total was 0 or -2. Compute the running total yourself.
* **`days=1` returns nothing** even on a day with fills, while a wider request returns
  them (measured 2026-08-06). This module does not choose the window —
  `IBKRClient.get_trades()` owns it — but the quirk is recorded because it makes "no
  fills today" an unreliable signal from any narrow call.
* **IBKR advises calling this endpoint "once per session"**, which is worth weighing
  against how often the dashboard polls (see the note in `dashboard_poller`).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo


class TradeSource(Protocol):
    """Whatever can answer `/iserver/account/trades` — `fetch_fills`' only read.

    Declared as the supertype `IBKRClient.get_trades` returns — `list[dict]` on the core
    pinned in `core-ref.txt`, `list[Trade | dict]` from 2026-09-17, both
    `Sequence[Mapping[str, Any]]` (`list` is invariant, so `list[dict]` would refuse the
    second). The dashboard poller's test double satisfies it structurally, and so does the
    real client.
    """

    def get_trades(self) -> Sequence[Mapping[str, Any]]:
        """Recent fills in IBKR's own window, one row per execution."""
        ...


log = logging.getLogger(__name__)

# The fill window is **not ours to choose**: `IBKRClient.get_trades()` owns it and asks
# for `days=7`.
#
# ⚠ That is the client's choice, NOT a documented ceiling. IBKR's page says the endpoint
# "Returns a list of trades for the currently selected account for current day and six
# previous days" and shows an example passing `days=3`; it documents **no maximum**
# (read 2026-08-06,
# https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/trades.md).
# A `days=30` request on this account was accepted and returned rows, but every fill it
# returned fell inside seven days anyway, so that measurement does NOT establish whether
# a wider window is honoured. Treat "7" as what we currently ask for, not as a limit.
#
# What follows from it either way: a position opened before the window has no opening leg
# in view, so its closes are declined rather than half-counted. That is the intended
# division of labour — this covers recent round trips, Flex owns everything older.
FILL_WINDOW_DAYS = 7


@dataclass(frozen=True)
class LiveFill:
    """One execution from `/iserver/account/trades`, typed.

    `signed_quantity` is the only interpretation applied at parse time: IBKR sends `B`/`S`
    (no `SS` on this account, verified over all 24 captured fills), and everything
    downstream reasons in signed quantities so a short open is not a special case.
    """

    execution_id: str
    conid: int
    symbol: str
    asset_class: str
    signed_quantity: float
    price: float
    commission: float
    multiplier: float
    # IBKR's `trade_time` verbatim: "Traded date time in UTC", `YYYYMMDD-HH:MM:SS`. It
    # ORDERS fills for the FIFO and nothing else. It is a time, not a trade date: IBKR
    # states a fill's trade date only in the Flex statement (gap #69, operator 2026-09-24),
    # and the UTC date differed from it on 29 of 1,223 historical fills (evening futures,
    # holiday sessions, after-hours funds). Never group, bucket or compare by it.
    trade_time: str
    # Display only, for the Fills tab (gap #68, 2026-09-25): IBKR's `exchange` ("the
    # exchange the order was executed on") and `order_ref` (the `cOID` given at placement;
    # `null` for an order placed outside the API, measured 2026-09-25). Neither decides
    # anything, so a row without them still parses.
    exchange: str = ""
    order_ref: str = ""

    @property
    def is_buy(self) -> bool:
        """Whether this fill increased the position."""
        return self.signed_quantity > 0


_ET = ZoneInfo("America/New_York")


def execution_time_et(trade_time: str, with_date: bool = False) -> str:
    """IBKR's UTC `trade_time` (`YYYYMMDD-HH:MM:SS`) as a clock reading in New York.

    The one conversion behind every surface that shows a fill's time — the chat report and
    the Fills tab — so the two cannot disagree by a rule. IBKR documents the field as "the
    UTC format of the trade time"
    (https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/trades.md). With
    `with_date` the Eastern date is included, and it is the date in that zone: 01:10Z on
    15 January is 20:10 ET on the 14th. Anything but the documented format renders blank,
    never a guess. This is a clock reading, not a trade date (gap #69): IBKR states the
    trade date only in its statement.
    """
    try:
        moment = datetime.strptime(trade_time.strip(), "%Y%m%d-%H:%M:%S")
    except ValueError:
        return ""
    local = moment.replace(tzinfo=UTC).astimezone(_ET)
    return local.strftime("%Y-%m-%d %H:%M:%S ET" if with_date else "%H:%M:%S ET")


def parse_fills(rows: Sequence[Any]) -> tuple[LiveFill, ...]:
    """Type raw `/iserver/account/trades` rows, skipping any that cannot be used.

    A row missing a price, size or `net_amount` cannot yield a multiplier and therefore
    cannot produce a money figure, so it is dropped with a warning rather than defaulted:
    a multiplier defaulted to 1 on a futures fill understates by 50x or 1000x, silently.

    A row without an `execution_id` is dropped too: whether a fill is settled is decided by
    that id alone (`Reconstruction.pending`), so a fill without one cannot be placed. Its
    contract then misses a leg and the trust check declines it rather than guessing.
    """
    out: list[LiveFill] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            size = float(row["size"])
            price = float(row["price"])
            net = float(row["net_amount"])
            if not size or not price:
                raise ValueError("zero size or price")
            multiplier = net / price / size
            side = str(row.get("side") or "").upper()
            execution_id = str(row.get("execution_id") or "")
            if not execution_id:
                raise ValueError("no execution_id")
            out.append(
                LiveFill(
                    execution_id=execution_id,
                    conid=int(row["conid"]),
                    symbol=str(row.get("symbol") or ""),
                    asset_class=str(row.get("sec_type") or ""),
                    signed_quantity=size if side == "B" else -size,
                    price=price,
                    commission=float(row.get("commission") or 0.0),
                    multiplier=multiplier,
                    trade_time=str(row.get("trade_time") or ""),
                    exchange=str(row.get("exchange") or "").strip(),
                    order_ref=str(row.get("order_ref") or "").strip(),
                )
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.warning("Skipping unusable fill %r: %s", row.get("execution_id"), exc)
    out.sort(key=lambda f: (f.trade_time, f.execution_id))
    return tuple(out)


@dataclass
class _Lot:
    """An open lot: signed quantity, entry price, and its unreleased opening commission."""

    quantity: float
    price: float
    commission: float


@dataclass(frozen=True)
class RoundTrip:
    """One closed lot: the atom both the money figure and the win/loss count derive from.

    A round trip is a *lot close*, not a fill. One fill can close several lots, and Flex's
    `flex_lot` counts the same way, so a count taken here is comparable with one taken
    from Flex. `execution_id` is the CLOSING fill's: it is what decides whether the round
    trip is settled (in Flex) or pending.
    """

    execution_id: str
    asset_class: str
    symbol: str
    quantity: float
    entry: float
    exit: float
    pnl: float


def _stats(trips: Iterable[RoundTrip]) -> tuple[int, int, int, float, float]:
    """`(winners, losers, scratches, gross_win, gross_loss)` over round trips.

    Counted over lot closes, so it is directly comparable with the same figures taken from
    Flex's `flex_lot`. Scratches (exactly 0.00) are counted separately for the same reason
    `TypeBreakdown.win_rate` excludes them: a flat lot is neither won nor lost.
    """
    pnls = [t.pnl for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return (
        len(wins),
        len(losses),
        len(pnls) - len(wins) - len(losses),
        round(sum(wins), 2),
        round(sum(losses), 2),
    )


@dataclass(frozen=True)
class PendingRealised:
    """Realised P&L of the executions Flex has not settled yet (gap #69).

    "Pending" is decided by execution id alone: an execution is pending if and only if its
    id is not a Flex `execution_key`. No date is involved, because IBKR states a fill's
    trade date only in the Flex statement, so this carries no day and must never be placed
    in a dated window: it is shown as its own line, "Not yet on a statement".

    `declined` names contracts that could not be reconstructed AND have a pending
    execution. Their P&L is excluded, so `incomplete` is set and the figure is a floor.
    """

    realised: Mapping[str, float] = field(default_factory=dict)
    round_trips: tuple[RoundTrip, ...] = ()
    fills: int = 0
    declined: tuple[str, ...] = ()

    @property
    def incomplete(self) -> bool:
        """Whether a pending execution belongs to a contract that could not be trusted."""
        return bool(self.declined)

    def stats(self, asset_class: str) -> tuple[int, int, int, float, float]:
        """`(winners, losers, scratches, gross_win, gross_loss)` for one asset class."""
        return _stats(t for t in self.round_trips if t.asset_class == asset_class)


@dataclass(frozen=True)
class Reconstruction:
    """Realised P&L per closing execution, plus what could not be trusted.

    `realised` maps each trusted closing execution id to `(asset_class, amount)`. It is
    keyed by execution, not by day, because whether an execution is settled is decided by
    its id (`pending`), and IBKR states no trade date for a live fill.

    `declined` names the contracts whose reconstructed position did not reproduce IBKR's,
    meaning their closing fills had no opening leg inside the window. Their contributions
    are **excluded** from `realised`, and `declined_executions` holds every execution they
    had, so `pending` can say "incomplete" rather than a total that is quietly too small.
    """

    realised: Mapping[str, tuple[str, float]] = field(default_factory=dict)
    round_trips: tuple[RoundTrip, ...] = ()
    declined: tuple[str, ...] = ()
    declined_executions: frozenset[str] = frozenset()
    # The executions this was built from, in order. Carried so a single fetch can serve
    # every reader: `/iserver/account/trades` is documented as a once-a-session call,
    # and the open-lot pricing in `dashboard_data.economic_entries` needs the same rows.
    fills: tuple[LiveFill, ...] = ()

    def pending(self, settled: Collection[str]) -> PendingRealised:
        """The part of this reconstruction Flex has not settled.

        Args:
            settled: The execution ids already present as Flex `execution_key`s
                (`dashboard_data.settled_execution_ids`). An execution is pending if and
                only if its id is not in it.
        """
        pending_ids = {f.execution_id for f in self.fills if f.execution_id not in settled}
        by_class: dict[str, float] = defaultdict(float)
        for eid, (asset_class, amount) in self.realised.items():
            if eid in pending_ids:
                by_class[asset_class] += amount
        symbols = {f.execution_id: f.symbol for f in self.fills}
        declined = sorted({symbols[e] for e in self.declined_executions if e in pending_ids})
        return PendingRealised(
            realised={c: round(v, 2) for c, v in by_class.items() if abs(v) > 0.005},
            round_trips=tuple(t for t in self.round_trips if t.execution_id in pending_ids),
            fills=len(pending_ids),
            declined=tuple(declined),
        )


def reconstruct(
    fills: Iterable[LiveFill], ibkr_positions: Mapping[int, float] | None = None
) -> Reconstruction:
    """FIFO the fills into realised P&L per closing execution.

    Args:
        fills: Executions, any order: they are sorted by IBKR's UTC `trade_time` here.
        ibkr_positions: `{conid: quantity}` as IBKR currently reports it. When supplied,
            any contract whose reconstructed position disagrees is **declined**: its
            realised is removed and every execution it had is marked untrustworthy. Passing
            None skips the check and is intended only for unit tests of the FIFO itself.

    Returns:
        A `Reconstruction`. Never raises on odd data — an unusable fill was already
        dropped by `parse_fills`.
    """
    ordered = sorted(fills, key=lambda f: (f.trade_time, f.execution_id))
    books: dict[int, deque[_Lot]] = defaultdict(deque)
    realised: dict[str, float] = defaultdict(float)
    asset_of: dict[str, str] = {}
    per_contract_executions: dict[int, set[str]] = defaultdict(set)
    running: dict[int, float] = defaultdict(float)

    round_trips: list[RoundTrip] = []
    per_contract_trips: dict[int, list[RoundTrip]] = defaultdict(list)

    for fill in ordered:
        book = books[fill.conid]
        key = fill.execution_id
        remaining = fill.signed_quantity
        closing_total = 0.0  # how much of this fill closed, for commission apportioning
        trips_this_fill: list[tuple[float, float, float]] = []  # (quantity, entry, pnl)
        running[fill.conid] += fill.signed_quantity
        per_contract_executions[fill.conid].add(fill.execution_id)

        # Closing legs first: consume opposite-signed lots FIFO.
        while remaining and book and (book[0].quantity > 0) != (remaining > 0):
            lot = book[0]
            taken = min(abs(remaining), abs(lot.quantity)) * (1 if lot.quantity > 0 else -1)
            gain = (fill.price - lot.price) * taken * fill.multiplier
            # The opening commission is released in proportion to the part of the lot
            # being closed — the convention that reproduces Flex exactly.
            released = lot.commission * abs(taken / lot.quantity)
            realised[key] += gain - released
            closing_total += abs(taken)
            trips_this_fill.append((abs(taken), lot.price, gain - released))
            lot.quantity -= taken
            lot.commission -= released
            remaining += taken
            if lot.quantity == 0:
                book.popleft()
            # This fill's own commission is a closing cost, charged once below.

        closed_any = remaining != fill.signed_quantity
        if closed_any:
            realised[key] -= fill.commission
            asset_of[key] = fill.asset_class
            # The closing fill's own commission is shared across the lots it closed, so the
            # round trips of one closing fill sum back to its realised figure (each trip is
            # rounded to cents, so the sum can differ from it by rounding).
            for qty, entry, pnl in trips_this_fill:
                share = fill.commission * (qty / closing_total) if closing_total else 0.0
                trip = RoundTrip(
                    execution_id=fill.execution_id,
                    asset_class=fill.asset_class,
                    symbol=fill.symbol,
                    quantity=qty,
                    entry=entry,
                    exit=fill.price,
                    pnl=round(pnl - share, 2),
                )
                round_trips.append(trip)
                per_contract_trips[fill.conid].append(trip)

        if remaining:
            # Opening (or reversing) leg. Its commission rides with the lot until close;
            # if this fill both closed and opened, the closing part already paid above and
            # the opening part carries no further commission of its own.
            book.append(
                _Lot(
                    quantity=remaining,
                    price=fill.price,
                    commission=0.0 if closed_any else fill.commission,
                )
            )

    declined: list[str] = []
    declined_executions: set[str] = set()
    if ibkr_positions is not None:
        symbols = {f.conid: f.symbol for f in ordered}
        for conid, qty in running.items():
            expected = float(ibkr_positions.get(conid, 0.0))
            if abs(qty - expected) > 1e-9:
                declined.append(symbols.get(conid, str(conid)))
                declined_executions |= per_contract_executions[conid]
                for trip in per_contract_trips[conid]:
                    round_trips.remove(trip)

    return Reconstruction(
        realised={
            eid: (asset_of[eid], amount)
            for eid, amount in realised.items()
            if eid in asset_of and eid not in declined_executions
        },
        round_trips=tuple(round_trips),
        declined=tuple(sorted(declined)),
        declined_executions=frozenset(declined_executions),
        fills=tuple(ordered),
    )


def fetch_fills(client: TradeSource) -> tuple[LiveFill, ...]:
    """Pull recent executions via the toolkit's own client. Blocking — use `to_thread`.

    Deliberately `client.get_trades()` rather than a raw request, and not for tidiness:
    **that method handles IBKR's two-call warmup.** A fresh brokerage session returns an
    EMPTY list on the first call to `/iserver/account/trades` and the real fills on a
    follow-up (verified live 2026-07-06, documented in `IBKRClient.get_trades`). A single
    GET therefore yields nothing on exactly the poll that follows a login — the moment the
    pending window is most needed, and a failure that would look like "nothing pending" rather
    than like a bug. It also owns the `days` parameter, so the window lives in one place.

    Reaching past a maintained client into `_base` to re-implement its endpoint, which an
    earlier draft of this function did, re-created that warmup bug in a place nobody would
    think to look for it.
    """
    return parse_fills(client.get_trades())
