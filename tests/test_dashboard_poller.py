"""Tests for claudia/dashboard_poller.py.

The behaviour worth pinning is not "does it fetch" — it is what happens when it
*cannot*. A dashboard that quietly keeps a broken number on screen, stamped with the
current time, is the failure this module was written to make impossible, so most of what
follows is failure-path testing:

  * a client that raises must not kill the loop, and must not reset `age_seconds`,
  * the account id must be resolved once, not once per poll (rate limit),
  * a failed resolution must not be cached,
  * the Flex half must survive the IBKR half failing, and vice versa.

Since 2026-08-06 the account half also runs only when the session owner reports `LIVE`
(plan S4). The helpers below inject a live owner by default — otherwise every test here
would exercise the gate rather than the behaviour it was written for — and the gate itself
is asserted explicitly at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from claudia import dashboard_data as dd
from claudia.dashboard_poller import POLL_INTERVAL, STALE_AFTER, DashboardPoller


def _owner(live: bool = True):
    """A stand-in session owner; `state().is_live` is all the poller consults."""
    state = MagicMock()
    state.is_live = live
    state.detail = "live" if live else "the gateway holds no session"
    owner = MagicMock()
    owner.state.return_value = state
    return owner

_TODAY = date(2026, 8, 6)


@pytest.fixture
def db(tmp_path):
    """A store holding two 2026 trades and one closed lot, enough to fill every window."""
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)"
        )
        # `asset_category` is present on the REAL flex_lot (verified against the live
        # store 2026-08-06: FUT 296, STK 405, OPT 4, FUND 2). A fixture without it is a
        # double weaker than its dependency, and kept the per-type breakdown untested.
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-08-03", "flex", "FUT", "USD", -3516.98),
                ("2026-08-04", "flex", "STK", "USD", 250.0),
                (None, "live", "FUT", "USD", None),
            ],
        )
        w.execute("INSERT INTO flex_lot VALUES ('20260803', 'FUT', -3516.98)")
    return path


# The live payload shape, BASE row included: its `currency` field is the literal
# "BASE" (measured 2026-08-04), which is why the poller carries a base-currency hint
# taken from /portfolio/accounts instead.
_LEDGER = {
    "USD": {"currency": "USD", "netliquidationvalue": 100000.0,
            "cashbalance": 25000.0, "unrealizedpnl": -1234.5,
            "realizedpnl": 99.0, "futuresonlypnl": -3516.98},
    "EUR": {"currency": "EUR", "netliquidationvalue": 10.0},
    "BASE": {"currency": "BASE", "netliquidationvalue": 100010.0},
}

_ORDER = {
    "orderId": 314390101, "ticker": "AAPL", "side": "BUY", "totalSize": 1,
    "remainingQuantity": 1, "price": 100.0, "orderType": "Limit",
    "timeInForce": "GTC", "status": "Submitted", "order_ref": "CLAUDIA-178594",
}

_POSITION = {"conid": 1, "ticker": "ESU6", "contractDesc": "ESU6", "assetClass": "FUT",
             "position": 1.0, "mktValue": 324000.0, "unrealizedPnl": -1234.5,
             "currency": "USD"}

# Sentinel for FakeClient(trades=...): "this endpoint raises", which is a different
# state from "it answered, with nothing" and the two must not share a value.
_RAISE = object()


def _trade(conid, side, size, price, day="20260806", seq="01", multiplier=1.0):
    """One `/iserver/account/trades` row, in IBKR's own shape."""
    return {
        "execution_id": f"{day}.{seq}", "conid": conid, "symbol": "TEST",
        "sec_type": "STK", "side": side, "size": size, "price": price,
        "net_amount": price * size * multiplier, "trade_time": f"{day}-12:00:00",
    }


class FakeClient:
    """An IBKRClient stand-in with per-method call counters and injectable failures.

    `fail_from` makes every call from the Nth ledger fetch onwards raise, which is how
    the "loop survives, age grows" property is exercised without any real network.
    """

    def __init__(self, fail_from: int | None = None, accounts=None, trades=(), positions=None):
        """Configure the fake. `accounts` defaults to a single valid account."""
        self.fail_from = fail_from
        self.accounts_calls = 0
        self.ledger_calls = 0
        self.position_pages: list[int] = []
        self.order_calls = 0
        self.trade_calls = 0
        self.trades = trades
        self._positions = [_POSITION] if positions is None else positions
        self._accounts = (
            [{"accountId": "U1234567", "currency": "USD"}] if accounts is None else accounts
        )

    def get_accounts(self):
        """Return the configured account list, counting the call."""
        self.accounts_calls += 1
        return self._accounts

    def get_account_ledger(self, account_id):
        """Return the canned ledger, or raise once `fail_from` is reached."""
        self.ledger_calls += 1
        if self.fail_from is not None and self.ledger_calls >= self.fail_from:
            raise ConnectionError("gateway went away")
        return _LEDGER

    def get_positions(self, account_id, page=0):
        """Return the configured positions on page 0 and nothing after it."""
        self.position_pages.append(page)
        return self._positions if page == 0 else []

    def get_live_orders(self):
        """Return one working order, counting the call.

        The fake had no such method until 2026-08-05, so `fetch_orders` caught the
        resulting AttributeError and every test silently saw `orders=None` — which is
        how the poller's order handling went entirely uncovered.
        """
        self.order_calls += 1
        return [_ORDER]

    def get_trades(self):
        """Return the canned executions, or raise if configured to.

        The fake had no such method until 2026-08-10, so `fetch_fills` caught the
        AttributeError and every test ran against a poller that could never see a
        current fill — a double weaker than its dependency, and the reason the entry
        wiring could go stale without a red test.
        """
        self.trade_calls += 1
        if self.trades is _RAISE:
            raise ConnectionError("trades endpoint unreachable")
        return self.trades


def _poller(db, client, **kw):
    """A poller wired to the fixture store with a frozen trading day."""
    kw.setdefault("session", _owner())
    return DashboardPoller(client, db, today_provider=lambda: _TODAY, **kw)


# ── A successful poll ─────────────────────────────────────────────────────────


async def test_poll_populates_every_section(db):
    """One poll fills ledger, positions and all four Flex sections."""
    p = _poller(db, FakeClient())
    await p._poll_once()
    snap = p.snapshot()

    assert snap.error is None
    assert snap.ledger is not None and snap.ledger.currency == "USD"
    assert [pos.symbol for pos in snap.positions] == ["ESU6"]
    assert snap.week is not None and snap.week.total == pytest.approx(-3266.98)
    assert snap.ytd is not None and snap.ytd.trade_count == 2
    assert set(snap.stats) == {"week", "month", "ytd"}
    assert snap.stats["week"].closed_lots == 1
    assert snap.coverage is not None and snap.coverage.live_pending == 1
    assert snap.age_seconds() < 5


async def test_snapshot_before_the_first_poll_says_so(db):
    """The pre-poll snapshot carries an honest error, not silently-zero money."""
    snap = _poller(db, FakeClient()).snapshot()
    assert snap.ledger is None
    assert snap.week is None
    assert "not polled yet" in (snap.error or "")


async def test_windows_follow_the_injected_trading_day(db):
    """The week window is Monday→today for the provider's date, not the wall clock."""
    p = _poller(db, FakeClient())
    await p._poll_once()
    week = p.snapshot().week
    assert week is not None
    assert (week.start, week.end) == (date(2026, 8, 3), _TODAY)


# ── Failure paths ─────────────────────────────────────────────────────────────


async def test_a_failed_poll_keeps_the_previous_data_and_ages_it(db):
    """The plan's core assertion: the loop survives and `age_seconds` grows.

    A snapshot whose account half failed must keep the *previous* `as_of`. Bumping it
    would make numbers that froze minutes ago read as one second old.
    """
    client = FakeClient(fail_from=2)
    p = _poller(db, client)

    await p._poll_once()
    good = p.snapshot()
    assert good.error is None

    await p._poll_once()  # this one raises inside the client
    stale = p.snapshot()

    assert stale.as_of == good.as_of  # not refreshed — the whole point
    assert stale.ledger == good.ledger  # previous numbers retained, not blanked
    assert stale.positions == good.positions
    assert "gateway went away" in (stale.error or "")
    assert stale.age_seconds(good.as_of + timedelta(seconds=90)) == pytest.approx(90.0)


async def test_flex_sections_still_refresh_when_ibkr_is_down(db):
    """A logged-out gateway must not blank the realised windows — they are local."""
    p = _poller(db, FakeClient(fail_from=1))
    await p._poll_once()
    snap = p.snapshot()
    assert snap.ledger is None  # never got one
    assert snap.week is not None and snap.week.total == pytest.approx(-3266.98)
    assert "IBKR unavailable" in (snap.error or "")


async def test_a_missing_store_does_not_stop_the_account_half(tmp_path):
    """No store.db: the ledger still polls, the Flex sections are simply absent."""
    p = DashboardPoller(FakeClient(), tmp_path / "nope.db", today_provider=lambda: _TODAY,
                        session=_owner())
    await p._poll_once()
    snap = p.snapshot()
    assert snap.ledger is not None
    assert snap.week is None and snap.coverage is None and snap.stats == {}
    assert snap.error is None  # the IBKR half succeeded; that is what `error` reports


async def test_a_failed_flex_read_carries_the_previous_windows_forward(db):
    """A failed local read means no NEW information, not that the information is gone.

    The Flex dataset changes at most once a day, so blanking the realised windows because
    SQLite was momentarily unavailable throws away the best available answer. An earlier
    version did exactly that: it spread an empty dict into the new snapshot and let every
    window default to None.
    """
    p = _poller(db, FakeClient())
    await p._poll_once()
    good = p.snapshot()
    assert good.week is not None

    p._read_flex = lambda _rec=None: None  # type: ignore[method-assign]
    await p._poll_once()
    after = p.snapshot()

    assert after.week is good.week
    assert after.ytd is good.ytd
    assert after.stats is good.stats
    assert after.coverage is good.coverage
    assert after.ledger is not None  # the account half is unaffected


async def test_a_failed_flex_read_during_an_ibkr_failure_keeps_both_halves(db):
    """Both reads failing must not blank a snapshot that was complete a second ago."""
    p = _poller(db, FakeClient(fail_from=2))
    await p._poll_once()
    good = p.snapshot()

    p._read_flex = lambda _rec=None: None  # type: ignore[method-assign]
    await p._poll_once()
    after = p.snapshot()

    assert after.week is good.week
    assert after.ledger == good.ledger
    assert after.as_of == good.as_of  # still not refreshed
    assert "gateway went away" in (after.error or "")


async def test_an_empty_store_is_not_treated_as_a_failed_read(tmp_path):
    """A successful read that finds nothing is a different claim from a failed one.

    `_read_flex` returns every section key on success and None on failure, so an empty store
    yields real (empty) windows rather than silently carrying stale ones forward.
    """
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)"
        )
        # `asset_category` is present on the REAL flex_lot (verified against the live
        # store 2026-08-06: FUT 296, STK 405, OPT 4, FUND 2). A fixture without it is a
        # double weaker than its dependency, and kept the per-type breakdown untested.
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
    p = DashboardPoller(FakeClient(), path, today_provider=lambda: _TODAY, session=_owner())
    await p._poll_once()
    snap = p.snapshot()
    assert snap.week is not None and snap.week.trade_count == 0
    assert snap.coverage == dd.FlexCoverage(through=None, live_pending=0)


async def test_loop_survives_a_client_that_raises_and_keeps_polling(db):
    """The task must outlive a failing poll — a frozen dashboard is a silent failure."""
    client = FakeClient(fail_from=2)
    p = _poller(db, client, interval=0.01)
    p.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if client.ledger_calls >= 4:
                break
        assert client.ledger_calls >= 4, "loop stopped after the first failure"
        assert p._task is not None and not p._task.done()
    finally:
        p.stop()


async def test_loop_survives_a_poll_that_raises_outright(db, caplog):
    """Even a bug in `_poll_once` itself is logged and retried, never fatal."""
    p = _poller(db, FakeClient(), interval=0.01)
    calls = []

    async def _boom():
        """Raise on the first two polls, then succeed."""
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("kaboom")

    p._poll_once = _boom  # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        p.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(calls) >= 4:
                    break
        finally:
            p.stop()
    assert len(calls) >= 4
    assert "kaboom" in caplog.text


# ── Account id resolution ─────────────────────────────────────────────────────


async def test_account_id_is_resolved_once_across_many_polls(db):
    """/portfolio/accounts is rate-limited at ~1 req/5s — resolve once, then cache."""
    client = FakeClient()
    p = _poller(db, client)
    for _ in range(5):
        await p._poll_once()
    assert client.accounts_calls == 1
    assert client.ledger_calls == 5
    assert p.account_id == "U1234567"


async def test_base_currency_comes_from_the_account_not_the_ledger(db):
    """The BASE ledger row reports currency "BASE"; /portfolio/accounts reports "USD".

    Without the hint this multi-currency fixture is genuinely ambiguous, so the tile
    would read "BASE" or nothing at all. With it, the ledger resolves to USD.
    """
    p = _poller(db, FakeClient())
    await p._poll_once()
    assert p.base_currency == "USD"
    ledger = p.snapshot().ledger
    assert ledger is not None
    assert ledger.currency == "USD"
    assert ledger.net_liquidation == 100000.0  # the USD row, not BASE's 100010.0


async def test_an_account_without_a_currency_field_still_resolves(db):
    """A missing `currency` must not block the poll — the ledger rules still apply."""
    p = _poller(db, FakeClient(accounts=[{"accountId": "U1"}]))
    await p._poll_once()
    assert p.account_id == "U1"
    assert p.base_currency is None


async def test_a_failed_resolution_is_not_cached(db):
    """A poll during a logged-out gateway must retry, not poison the poller."""
    client = FakeClient(accounts=[])
    p = _poller(db, client)
    await p._poll_once()
    assert p.account_id is None
    assert "No IBKR account" in (p.snapshot().error or "")

    client._accounts = [{"accountId": "U7654321"}]
    await p._poll_once()
    assert p.account_id == "U7654321"
    assert p.snapshot().error is None


async def test_account_rows_without_an_id_are_skipped(db):
    """A malformed first entry must not become the account id."""
    client = FakeClient(accounts=[{"accountId": ""}, {}, {"accountId": "U999"}])
    p = _poller(db, client)
    await p._poll_once()
    assert p.account_id == "U999"


async def test_positions_are_paged_through_the_data_layer(db):
    """Paging is the data layer's job; the poller must actually go through it."""
    client = FakeClient()
    p = _poller(db, client)
    await p._poll_once()
    assert client.position_pages == [0]


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def test_start_is_idempotent_and_restarts_a_finished_task(db):
    """Matches ConnectivityChecker.start(): no double task, but a dead one is replaced."""
    p = _poller(db, FakeClient(), interval=60)
    p.start()
    first = p._task
    p.start()
    assert p._task is first
    p.stop()
    await asyncio.sleep(0)
    p.start()
    assert p._task is not first
    p.stop()


async def test_stop_without_start_is_safe(db):
    """Stopping a poller that never started is a no-op, not an error."""
    _poller(db, FakeClient()).stop()  # must not raise


def test_stale_threshold_is_derived_from_the_interval():
    """One constant, not two — a hand-set threshold would drift from the poll rate."""
    assert STALE_AFTER == POLL_INTERVAL * 4
    assert POLL_INTERVAL == 15.0


async def test_snapshot_is_replaced_wholesale_never_mutated(db):
    """A reader can only ever see a complete snapshot, old or new — never a half one."""
    p = _poller(db, FakeClient())
    await p._poll_once()
    first = p.snapshot()
    await p._poll_once()
    assert p.snapshot() is not first
    assert isinstance(first, dd.DashboardSnapshot)
    with pytest.raises(Exception):  # frozen dataclass  # noqa: B017
        first.as_of = datetime.now(UTC)  # type: ignore[misc]


# ── Economic entry reconstruction ─────────────────────────────────────────────


async def test_a_failed_entry_reconstruction_does_not_cost_the_poll(db):
    """Degrade one column, never the panel.

    The poller fixture's `flex_trade` has none of the columns the reconstruction reads,
    so this exercises the real failure — a query against a store that cannot answer it —
    rather than a patched exception. The poll must still publish a live snapshot with
    its positions, ledger and realised windows intact.
    """
    p = _poller(db, FakeClient())
    await p._poll_once()
    snap = p.snapshot()
    assert snap.error is None
    assert snap.ledger is not None
    assert len(snap.positions) == 1
    assert snap.positions[0].economic_entry is None
    assert snap.week is not None


async def test_entries_are_attached_when_the_store_can_answer(tmp_path):
    """The wiring itself: a store with fills must reach `Position.economic_entry`.

    The fixture's position is conid 1, **one** unit. Buy at 100, buy at 110, sell one:
    FIFO consumes the 100 and leaves the 110, so the published entry is 110.0. An
    average-cost reconstruction would answer 105.0 here, which is why the number is
    pinned rather than merely asserted non-None.
    """
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, trade_date TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL, conid TEXT,"
            " symbol TEXT, underlying_symbol TEXT, date_time TEXT, quantity REAL,"
            " trade_price REAL)"
        )
        # `asset_category` is present on the REAL flex_lot (verified against the live
        # store 2026-08-06: FUT 296, STK 405, OPT 4, FUND 2). A fixture without it is a
        # double weaker than its dependency, and kept the per-type breakdown untested.
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade (source, conid, symbol, trade_date, date_time,"
            " quantity, trade_price) VALUES ('flex', '1', 'TEST', ?, ?, ?, ?)",
            [("20260601", "20260601;100000", 1.0, 100.0),
             ("20260602", "20260602;100000", 1.0, 110.0),
             ("20260603", "20260603;100000", -1.0, 130.0)],
        )
    p = _poller(path, FakeClient(positions=[{**_POSITION, "position": 1.0}]))
    await p._poll_once()
    position = p.snapshot().positions[0]
    assert position.conid == 1
    assert position.economic_entry == pytest.approx(110.0)


def _replaced_intraday_store(tmp_path):
    """A store whose newest statement leaves 2 lots averaging 105.0, and stops there."""
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, trade_date TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL, conid TEXT,"
            " symbol TEXT, underlying_symbol TEXT, date_time TEXT, quantity REAL,"
            " trade_price REAL)"
        )
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, asset_category TEXT,"
                  " fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade (source, conid, symbol, trade_date, trade_date_iso,"
            " date_time, quantity, trade_price) VALUES ('flex', '1', 'TEST', ?, ?, ?, ?, ?)",
            [("20260605", "2026-06-05", "20260605;100000", 1.0, 100.0),
             ("20260605", "2026-06-05", "20260605;100100", 1.0, 110.0)],
        )
    return path


async def test_a_position_replaced_since_the_statement_publishes_todays_lots(tmp_path):
    """The 2026-08-10 CL defect, through the poller: today's fills must reach the entry.

    The store's last word is 2 lots averaging 105.0. Today the account sold both and
    bought two more at 200.0 — so IBKR still reports 2, the stored book still
    reconstructs 2, and the quantity check cannot tell the two apart. Only the fills
    can, and 105.0 published here is the live defect exactly.
    """
    p = _poller(
        _replaced_intraday_store(tmp_path),
        FakeClient(
            positions=[{**_POSITION, "position": 2.0}],
            trades=[_trade(1, "S", 2, 150.0, seq="01"),
                    _trade(1, "B", 1, 200.0, seq="02"),
                    _trade(1, "B", 1, 200.0, seq="03")],
        ),
    )
    await p._poll_once()
    assert p.snapshot().positions[0].economic_entry == pytest.approx(200.0)


async def test_entries_decline_when_the_executions_cannot_be_read(tmp_path):
    """A gateway that will not answer for fills buys silence, not the stored book.

    Same store and the same 2-unit position, with the trades endpoint failing. The
    stored history reproduces IBKR's quantity on its own, so this is precisely the state
    in which a plausible wrong entry gets published — the column goes blank instead.
    """
    p = _poller(
        _replaced_intraday_store(tmp_path),
        FakeClient(positions=[{**_POSITION, "position": 2.0}], trades=_RAISE),
    )
    await p._poll_once()
    snap = p.snapshot()
    assert snap.error is None
    assert snap.positions[0].economic_entry is None


# ── The order book ────────────────────────────────────────────────────────────


async def test_a_successful_poll_populates_the_order_book(db):
    """Orders reach the snapshot at all — untested until 2026-08-05."""
    p = _poller(db, FakeClient())
    await p._poll_once()
    snap = p.snapshot()

    assert snap.orders is not None, "None means the book was never established"
    assert [o.order_id for o in snap.orders] == ["314390101"]
    assert snap.orders[0].is_claudia_staged is True


async def test_a_stale_republish_carries_the_order_book_forward(db):
    """A failed poll keeps the last known book, exactly as it keeps ledger and positions.

    `orders` was omitted from `_publish_stale` until 2026-08-05 and defaulted to None —
    which on that field asserts "the book was never established", the opposite of what
    ledger and positions were saying in the very same snapshot. It was invisible because
    the view blanks the whole account half on `error`; that made the two routes agree by
    accident, not by design.
    """
    client = FakeClient(fail_from=2)
    p = _poller(db, client)

    await p._poll_once()
    good = p.snapshot()
    assert good.orders is not None

    await p._poll_once()  # raises inside the client
    stale = p.snapshot()

    assert stale.error, "this poll must have failed"
    assert stale.orders == good.orders, "the last known book must survive, like the ledger"
    assert stale.as_of == good.as_of


async def test_an_unreadable_order_book_is_none_not_empty(db):
    """A failed lookup must not render as 'you have no working orders'.

    `()` and None are opposite claims: one says nothing is resting, the other says the
    book could not be read. Orders come from /iserver/* and can fail while /portfolio/*
    answers perfectly.
    """
    class _NoBridge(FakeClient):
        """A client whose /iserver order lookup fails while /portfolio still answers."""

        def get_live_orders(self):
            """Fail the way a downed brokerage session does."""
            raise RuntimeError("Bad Request: no bridge")

    p = _poller(db, _NoBridge())
    await p._poll_once()
    snap = p.snapshot()

    assert snap.orders is None
    assert snap.ledger is not None, "an orders failure must not take the account half down"


# ── The S4 gate: no account call against an unconfirmed session ──────────────


@pytest.mark.asyncio
async def test_the_account_half_is_skipped_when_the_session_is_not_live(db):
    """The defect that started the lifecycle work, pinned.

    `/portfolio/*` keeps answering HTTP 200 with figures after the brokerage session
    drops — IBKR documents no brokerage prerequisite for it — while `mktPrice`,
    `mktValue` and `unrealizedPnl` are market-data derived, and IBKR states that "Market
    Data and Trading is not possible if not authenticated". Asking "did the call raise?"
    therefore published stale figures under a fresh `as_of`.
    """
    client = FakeClient()
    poller = DashboardPoller(client, db, today_provider=lambda: _TODAY, session=_owner(live=False))

    await poller._poll_once()
    snap = poller.snapshot()

    assert client.ledger_calls == 0, "the account endpoints must not be called at all"
    assert snap.ledger is None
    assert snap.positions == ()
    assert snap.error and "IBKR not usable" in snap.error


@pytest.mark.asyncio
async def test_the_flex_half_still_runs_when_the_session_is_not_live(db):
    """Flex is local SQLite and has nothing to do with the gateway.

    Blanking it because IBKR went away would invent an outage in the half of the
    dashboard that is still perfectly good.
    """
    poller = DashboardPoller(FakeClient(), db, today_provider=lambda: _TODAY,
                             session=_owner(live=False))

    await poller._poll_once()
    snap = poller.snapshot()

    assert snap.week is not None
    assert snap.ytd is not None


# -- Fills are fetched on a trigger, not on every poll ------------------------


class _CountingClient(FakeClient):
    """A FakeClient that counts `get_trades` calls and can move its realised P&L."""

    def __init__(self, **kw):
        """Start with no trade calls recorded and a flat realised figure."""
        super().__init__(**kw)
        self.trade_calls = 0
        self.realised = 0.0

    def get_trades(self):
        """Record the call and return nothing — the count is what matters here."""
        self.trade_calls += 1
        return []

    def get_account_ledger(self, account_id):
        """The canned ledger with `realizedpnl` overridden by `self.realised`."""
        led = {k: dict(v) for k, v in super().get_account_ledger(account_id).items()}
        for row in led.values():
            row["realizedpnl"] = self.realised
        return led


@pytest.mark.asyncio
async def test_fills_are_not_refetched_while_nothing_closes(db):
    """IBKR advises calling /iserver/account/trades "once per session".

    An earlier version fetched it on every 15s poll — roughly 240 calls an hour against
    explicit advice to make one, and against a documented 1-request-per-5-seconds limit.
    The trigger is the ledger's `realizedpnl`, which moves if and only if a position
    closed, so nothing is missed by not refetching.
    """
    client = _CountingClient()
    poller = DashboardPoller(client, db, today_provider=lambda: _TODAY, session=_owner())

    for _ in range(5):
        await poller._poll_once()

    assert client.trade_calls == 1, "fills must be fetched once while realised P&L is flat"


@pytest.mark.asyncio
async def test_a_closed_position_triggers_a_refetch(db, monkeypatch):
    """`realizedpnl` moving is the only event that can change a reconstruction."""
    client = _CountingClient()
    poller = DashboardPoller(client, db, today_provider=lambda: _TODAY, session=_owner())

    await poller._poll_once()
    assert client.trade_calls == 1

    # A round trip closes: the ledger's realised figure moves.
    client.realised = 945.52
    await poller._poll_once()

    assert client.trade_calls == 2, "a new realised figure must refetch the fills"
