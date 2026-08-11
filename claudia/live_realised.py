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
those into realised P&L per day and per asset class — and, critically, refuses to publish
whenever it cannot prove the result.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ibkr_core_mcp import IBKRClient

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
# division of labour — the bridge covers recent round trips, Flex owns everything older.
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
    trade_day: str  # YYYYMMDD, as IBKR reports it — its session date, not a calendar day

    @property
    def is_buy(self) -> bool:
        """Whether this fill increased the position."""
        return self.signed_quantity > 0


def parse_fills(rows: Sequence[Any]) -> tuple[LiveFill, ...]:
    """Type raw `/iserver/account/trades` rows, skipping any that cannot be used.

    A row missing a price, size or `net_amount` cannot yield a multiplier and therefore
    cannot produce a money figure, so it is dropped with a warning rather than defaulted:
    a multiplier defaulted to 1 on a futures fill understates by 50x or 1000x, silently.
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
            out.append(
                LiveFill(
                    execution_id=str(row.get("execution_id") or ""),
                    conid=int(row["conid"]),
                    symbol=str(row.get("symbol") or ""),
                    asset_class=str(row.get("sec_type") or ""),
                    signed_quantity=size if side == "B" else -size,
                    price=price,
                    commission=float(row.get("commission") or 0.0),
                    multiplier=multiplier,
                    trade_day=str(row.get("trade_time") or "")[:8],
                )
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.warning("Skipping unusable fill %r: %s", row.get("execution_id"), exc)
    out.sort(key=lambda f: (f.trade_day, f.execution_id))
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
    `flex_lot` counts the same way — so a count taken here is comparable with the one
    taken from Flex, which is the whole point of bridging them into a single window.
    """

    day: str
    asset_class: str
    symbol: str
    quantity: float
    entry: float
    exit: float
    pnl: float


@dataclass(frozen=True)
class Reconstruction:
    """Realised P&L per (day, asset class), plus what could not be trusted.

    `declined` names the contracts whose reconstructed position did not reproduce IBKR's,
    meaning their closing fills had no opening leg inside the window. Their contributions
    are **excluded** from `realised`, and `declined_days` marks every day they touched, so
    a caller can render "incomplete" rather than a total that is quietly too small.
    """

    realised: Mapping[tuple[str, str], float] = field(default_factory=dict)
    round_trips: tuple[RoundTrip, ...] = ()
    declined: tuple[str, ...] = ()
    declined_days: frozenset[str] = frozenset()
    # The executions this was built from, in order. Carried so a single fetch can serve
    # every reader — `/iserver/account/trades` is documented as a once-a-session call,
    # and the open-lot pricing in `dashboard_data.economic_entries` needs the same rows.
    fills: tuple[LiveFill, ...] = ()

    def stats_for(self, day: str, asset_class: str) -> tuple[int, int, int, float, float]:
        """`(winners, losers, scratches, gross_win, gross_loss)` for one day and class.

        Counted over lot closes, so it is directly comparable with the same figures taken
        from Flex's `flex_lot` — a bridged window mixes the two and they must mean the
        same thing. Scratches (exactly 0.00) are counted separately for the same reason
        `TypeBreakdown.win_rate` excludes them: a flat lot is neither won nor lost.
        """
        rts = [r for r in self.round_trips if r.day == day and r.asset_class == asset_class]
        wins = [r.pnl for r in rts if r.pnl > 0]
        losses = [r.pnl for r in rts if r.pnl < 0]
        return (
            len(wins), len(losses), len(rts) - len(wins) - len(losses),
            round(sum(wins), 2), round(sum(losses), 2),
        )

    def total_for_day(self, day: str) -> float | None:
        """Realised across all asset classes for `day`, or None if it is not trustworthy."""
        if day in self.declined_days:
            return None
        return round(sum(v for (d, _), v in self.realised.items() if d == day), 2)

    def by_type_for_day(self, day: str) -> Mapping[str, float]:
        """`{asset_class: realised}` for `day`. Empty when nothing realised."""
        return {t: round(v, 2) for (d, t), v in self.realised.items() if d == day and v}


def reconstruct(
    fills: Iterable[LiveFill], ibkr_positions: Mapping[int, float] | None = None
) -> Reconstruction:
    """FIFO the fills into realised P&L per day and asset class.

    Args:
        fills: Executions, any order — they are sorted by trade day here.
        ibkr_positions: `{conid: quantity}` as IBKR currently reports it. When supplied,
            any contract whose reconstructed position disagrees is **declined**: its
            realised is removed and every day it touched is marked untrustworthy. Passing
            None skips the check and is intended only for unit tests of the FIFO itself.

    Returns:
        A `Reconstruction`. Never raises on odd data — an unusable fill was already
        dropped by `parse_fills`.
    """
    ordered = sorted(fills, key=lambda f: (f.trade_day, f.execution_id))
    books: dict[int, deque[_Lot]] = defaultdict(deque)
    realised: dict[tuple[str, str], float] = defaultdict(float)
    per_contract_days: dict[int, set[str]] = defaultdict(set)
    per_contract_realised: dict[int, dict[tuple[str, str], float]] = defaultdict(
        lambda: defaultdict(float)
    )
    running: dict[int, float] = defaultdict(float)

    round_trips: list[RoundTrip] = []
    per_contract_trips: dict[int, list[RoundTrip]] = defaultdict(list)

    for fill in ordered:
        book = books[fill.conid]
        key = (fill.trade_day, fill.asset_class)
        remaining = fill.signed_quantity
        closing_total = 0.0   # how much of this fill closed, for commission apportioning
        trips_this_fill: list[list] = []
        running[fill.conid] += fill.signed_quantity
        per_contract_days[fill.conid].add(fill.trade_day)

        # Closing legs first: consume opposite-signed lots FIFO.
        while remaining and book and (book[0].quantity > 0) != (remaining > 0):
            lot = book[0]
            taken = min(abs(remaining), abs(lot.quantity)) * (1 if lot.quantity > 0 else -1)
            gain = (fill.price - lot.price) * taken * fill.multiplier
            # The opening commission is released in proportion to the part of the lot
            # being closed — the convention that reproduces Flex exactly.
            released = lot.commission * abs(taken / lot.quantity)
            realised[key] += gain - released
            per_contract_realised[fill.conid][key] += gain - released
            closing_total += abs(taken)
            trips_this_fill.append([abs(taken), lot.price, gain - released])
            lot.quantity -= taken
            lot.commission -= released
            remaining += taken
            if lot.quantity == 0:
                book.popleft()
            # This fill's own commission is a closing cost, charged once below.

        closed_any = remaining != fill.signed_quantity
        if closed_any:
            realised[key] -= fill.commission
            per_contract_realised[fill.conid][key] -= fill.commission
            # The closing fill's own commission is shared across the lots it closed, so a
            # per-round-trip P&L sums back to the per-day figure exactly.
            for qty, entry, pnl in trips_this_fill:
                share = fill.commission * (qty / closing_total) if closing_total else 0.0
                trip = RoundTrip(
                    day=fill.trade_day, asset_class=fill.asset_class, symbol=fill.symbol,
                    quantity=qty, entry=entry, exit=fill.price, pnl=round(pnl - share, 2),
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
    declined_days: set[str] = set()
    if ibkr_positions is not None:
        symbols = {f.conid: f.symbol for f in ordered}
        for conid, qty in running.items():
            expected = float(ibkr_positions.get(conid, 0.0))
            if abs(qty - expected) > 1e-9:
                declined.append(symbols.get(conid, str(conid)))
                declined_days |= per_contract_days[conid]
                for key, value in per_contract_realised[conid].items():
                    realised[key] -= value
                for trip in per_contract_trips[conid]:
                    round_trips.remove(trip)

    return Reconstruction(
        realised={k: v for k, v in realised.items() if abs(v) > 0.005},
        round_trips=tuple(round_trips),
        declined=tuple(sorted(declined)),
        declined_days=frozenset(declined_days),
        fills=tuple(ordered),
    )


def fetch_fills(client: IBKRClient) -> tuple[LiveFill, ...]:
    """Pull recent executions via the toolkit's own client. Blocking — use `to_thread`.

    Deliberately `client.get_trades()` rather than a raw request, and not for tidiness:
    **that method handles IBKR's two-call warmup.** A fresh brokerage session returns an
    EMPTY list on the first call to `/iserver/account/trades` and the real fills on a
    follow-up (verified live 2026-07-06, documented in `IBKRClient.get_trades`). A single
    GET therefore yields nothing on exactly the poll that follows a login — the moment the
    bridge is most needed, and a failure that would look like "no trades today" rather
    than like a bug. It also owns the `days` parameter, so the window lives in one place.

    Reaching past a maintained client into `_base` to re-implement its endpoint, which an
    earlier draft of this function did, re-created that warmup bug in a place nobody would
    think to look for it.
    """
    return parse_fills(client.get_trades())


def days_after(coverage_through: date | None, fills: Iterable[LiveFill]) -> tuple[str, ...]:
    """The trade days present in `fills` that Flex has not covered yet, oldest first.

    This is the gap the reconstruction exists to fill, and it is **not** just "today":
    measured 2026-08-06 Flex reached 2026-08-04 while fills existed on 08-05 and 08-06.
    A bridge that assumed one day would have left a whole trading day missing.

    `coverage_through` None means Flex has nothing at all, so every day qualifies.
    """
    cutoff = coverage_through.strftime("%Y%m%d") if coverage_through else ""
    return tuple(sorted({f.trade_day for f in fills if f.trade_day > cutoff}))
