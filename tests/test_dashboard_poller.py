"""Tests for claudia/dashboard_poller.py.

The behaviour worth pinning is not "does it fetch" — it is what happens when it
*cannot*. A dashboard that quietly keeps a broken number on screen, stamped with the
current time, is the failure this module was written to make impossible, so most of what
follows is failure-path testing:

  * a client that raises must not kill the loop, and must not reset `age_seconds`,
  * the account id must be resolved once, not once per poll (rate limit),
  * a failed resolution must not be cached,
  * the Flex half must survive the IBKR half failing, and vice versa.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from claudia import dashboard_data as dd
from claudia.dashboard_poller import POLL_INTERVAL, STALE_AFTER, DashboardPoller

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
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, fifo_pnl_realized REAL)")
        w.executemany(
            "INSERT INTO flex_trade VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-08-03", "flex", "FUT", "USD", -3516.98),
                ("2026-08-04", "flex", "STK", "USD", 250.0),
                (None, "live", "FUT", "USD", None),
            ],
        )
        w.execute("INSERT INTO flex_lot VALUES ('20260803', -3516.98)")
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

_POSITION = {"conid": 1, "ticker": "ESU6", "contractDesc": "ESU6", "assetClass": "FUT",
             "position": 1.0, "mktValue": 324000.0, "unrealizedPnl": -1234.5,
             "currency": "USD"}


class FakeClient:
    """An IBKRClient stand-in with per-method call counters and injectable failures.

    `fail_from` makes every call from the Nth ledger fetch onwards raise, which is how
    the "loop survives, age grows" property is exercised without any real network.
    """

    def __init__(self, fail_from: int | None = None, accounts=None):
        """Configure the fake. `accounts` defaults to a single valid account."""
        self.fail_from = fail_from
        self.accounts_calls = 0
        self.ledger_calls = 0
        self.position_pages: list[int] = []
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
        """Return one position on page 0 and nothing after it."""
        self.position_pages.append(page)
        return [_POSITION] if page == 0 else []


def _poller(db, client, **kw):
    """A poller wired to the fixture store with a frozen trading day."""
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
    p = DashboardPoller(FakeClient(), tmp_path / "nope.db", today_provider=lambda: _TODAY)
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

    p._read_flex = lambda: None  # type: ignore[method-assign]
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

    p._read_flex = lambda: None  # type: ignore[method-assign]
    await p._poll_once()
    after = p.snapshot()

    assert after.week is good.week
    assert after.ledger == good.ledger
    assert after.as_of == good.as_of  # still not refreshed
    assert "gateway went away" in (after.error or "")


async def test_an_empty_store_is_not_treated_as_a_failed_read(tmp_path):
    """A successful read that finds nothing is a different claim from a failed one.

    `_read_flex` returns all six keys on success and None on failure, so an empty store
    yields real (empty) windows rather than silently carrying stale ones forward.
    """
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as w:
        w.execute(
            "CREATE TABLE flex_trade (trade_date_iso TEXT, source TEXT,"
            " asset_category TEXT, currency TEXT, fifo_pnl_realized REAL)"
        )
        w.execute("CREATE TABLE flex_lot (trade_date TEXT, fifo_pnl_realized REAL)")
    p = DashboardPoller(FakeClient(), path, today_provider=lambda: _TODAY)
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
