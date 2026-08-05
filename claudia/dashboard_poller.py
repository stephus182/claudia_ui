"""Process-wide background poller feeding the live dashboard.

Modelled directly on `claudia/status.py`'s `ConnectivityChecker`, which already proves
this shape in production: one `asyncio.Task` for the whole process, blocking work pushed
onto worker threads with `asyncio.to_thread`, a cached result read synchronously by the
per-session Panel callbacks, and a loop that logs and continues rather than dying on a
transient failure.

Two layers, and the split is the point:

```
DashboardPoller (one asyncio.Task, process-wide, every _POLL_INTERVAL seconds)
    ├─ asyncio.to_thread -> client.get_account_ledger / get_positions   (blocking HTTP)
    └─ asyncio.to_thread -> read-only SQLite for the realised windows
                caches a DashboardSnapshot
                        v  synchronous, no I/O
pn.state.add_periodic_callback(_repaint, 5000, start=False)   <- per session
```

Sessions never touch IBKR. A hundred browser tabs cost one poll, and a slow gateway
cannot stall the shared event loop that every session's chat runs on.

## Staleness: why a failed poll does not bump `as_of`

`DashboardSnapshot.as_of` is **when the account data in it was obtained**, not when the
loop last woke up. A failed poll therefore republishes the previous ledger and positions
with their original `as_of` and an `error` attached, so `age_seconds()` keeps growing.

The alternative — stamping `as_of = now` on every loop iteration — makes a dashboard
whose numbers froze twenty minutes ago look one second old. On a trading surface that is
the worst failure available to this module, worse than showing nothing, so it is
structurally impossible here rather than merely avoided.

The Flex half is independent: it is a local read-only SQLite query that cannot fail for
gateway reasons, and its own as-of is a *date* carried by `FlexCoverage.through`. So a
snapshot can legitimately hold fresh realised windows beside a stale ledger — which is
exactly what a logged-out gateway looks like, and exactly what the UI must say.

The same principle runs the other way too: a Flex read that fails carries the **previous**
sections forward rather than blanking them (`_flex_sections`). A failed local read means
no new information, not that the information is gone — the Flex dataset changes at most
once a day, so the last good windows are still the best available answer.

## Account id

Resolved **once** and cached. `ClaudeToolkit._first_account_id()` calls
`/portfolio/accounts` on every toolkit invocation and that endpoint is rate-limited to
roughly one request per five seconds; a 15-second poll doing it every time would spend a
third of its budget re-answering a question whose answer never changes. A failed
resolution is not cached, so a poll during a logged-out gateway retries next time.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claudia.dashboard_data import (
    DashboardSnapshot,
    LedgerSnapshot,
    Position,
    build_flex_sections,
    connect,
    economic_entries,
    empty_snapshot,
    fetch_ledger,
    fetch_orders,
    fetch_positions,
    with_economic_entries,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ibkr_core_mcp import IBKRClient

log = logging.getLogger(__name__)

# 15s: fast enough that a position change shows up within one glance, slow enough to stay
# far clear of IBKR's portfolio rate limits with the account id already resolved. The
# per-session repaint runs at 5s and reads the cache, so the UI never looks frozen
# between polls — it just repeats the same numbers with a growing age.
POLL_INTERVAL = 15.0

# Age past which the UI should treat the account figures as untrustworthy. Four missed
# polls, so an ordinary slow response never trips it. Lives here, beside the interval it
# is derived from, rather than in the Panel layer where the two could drift apart.
STALE_AFTER = POLL_INTERVAL * 4


class DashboardPoller:
    """Background poller caching one `DashboardSnapshot` for the whole process.

    Lifecycle mirrors `ConnectivityChecker` exactly:

        poller.start()   — begin polling (idempotent; restarts a finished task)
        poller.snapshot() — synchronous, no I/O; safe from a Panel callback
        poller.stop()    — cancel the task

    `today_provider` is injected so the date windows are testable without freezing the
    clock globally; it is called on every poll rather than captured once, so a process
    left running across midnight rolls its windows over instead of reporting yesterday's
    week forever.
    """

    def __init__(
        self,
        client: IBKRClient,
        db_path: str | Path,
        interval: float = POLL_INTERVAL,
        today_provider: Any = None,
    ) -> None:
        """Configure the poller. Nothing is polled until `start()`.

        Args:
            client: IBKRClient for the ledger and positions. Read-only use.
            db_path: ibkr_core_mcp store.db — opened read-only, once per poll.
            interval: Seconds between polls.
            today_provider: Zero-arg callable returning today's `date`. Defaults to
                `date.today`, i.e. the local trading day, which is the basis the
                Monday→Friday week requirement is written against.
        """
        self._client = client
        self._db_path = Path(db_path)
        self._interval = interval
        self._today = today_provider or date.today
        self._account_id: str | None = None
        self._base_currency: str | None = None
        self._snapshot = empty_snapshot(error="Dashboard has not polled yet.")
        self._task: asyncio.Task[None] | None = None

    # ── Public, synchronous ─────────────────────────────────────────────────

    def snapshot(self) -> DashboardSnapshot:
        """The latest cached snapshot. No I/O — safe to call from a Panel callback.

        The snapshot is a frozen dataclass replaced wholesale by the poll, so a reader
        can never observe a half-updated one: the attribute either still points at the
        old object or already points at the complete new one.
        """
        return self._snapshot

    @property
    def account_id(self) -> str | None:
        """The resolved IBKR account id, or None if it has not resolved yet."""
        return self._account_id

    @property
    def base_currency(self) -> str | None:
        """The account's base-currency ISO code from `/portfolio/accounts`, if resolved."""
        return self._base_currency

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the polling task. Idempotent; recreates a finished or cancelled task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            log.info("DashboardPoller started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        """Cancel the polling task. Safe to call if polling was never started."""
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("DashboardPoller stopped")

    # ── Internal ────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll once immediately, then every `interval` seconds until cancelled.

        `CancelledError` breaks out cleanly; every other exception is logged and the loop
        continues. One bad response — or one bad *deploy* of `_poll_once` — must not take
        the dashboard down for the rest of the session, because the surface that would
        then be silently frozen is the one showing money.
        """
        try:
            await self._poll_once()
        except Exception as exc:
            log.warning("DashboardPoller initial poll error: %s", exc)
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("DashboardPoller poll error: %s", exc)

    async def _poll_once(self) -> None:
        """Run one full poll: Flex windows first, then the IBKR account data.

        Flex first, deliberately. It is a local SQLite read that does not depend on the
        gateway, so doing it first means a logged-out session still gets its realised
        week/month/YTD rather than an empty dashboard — the numbers that are available
        are shown, and the ones that are not are marked stale with a reason.
        """
        flex = self._flex_sections(await asyncio.to_thread(self._read_flex))
        try:
            ledger, positions = await asyncio.to_thread(self._read_account)
        except Exception as exc:
            log.warning("Dashboard account poll failed: %s", exc)
            self._publish_stale(flex, f"IBKR unavailable: {exc}")
            return
        positions = await asyncio.to_thread(self._read_entries, positions)
        # Deliberately NOT inside `_read_account`: orders come from `/iserver/*` and the
        # account half from `/portfolio/*`, and those fail independently (measured
        # 2026-08-04 — ledger live while orders returned "no bridge"). A raise here would
        # take a perfectly good ledger down with it, so `fetch_orders` returns None and
        # the view says "not established" instead of drawing an empty book.
        orders = await asyncio.to_thread(fetch_orders, self._client)
        self._snapshot = DashboardSnapshot(
            as_of=datetime.now(UTC),
            ledger=ledger,
            positions=positions,
            orders=orders,
            error=None,
            **flex,
        )

    def _publish_stale(self, flex: dict[str, Any], error: str) -> None:
        """Republish the previous account data, unaged, with `error` attached.

        `as_of` is carried over from the previous snapshot rather than refreshed — see
        the module docstring. The Flex sections are whatever `_flex_sections` resolved:
        this poll's if the local read succeeded, the previous poll's if it did not.
        Withholding good Flex data because an unrelated IBKR call failed would hide the
        half of the dashboard that still works.

        **All three IBKR-derived fields carry forward, orders included.** Until 2026-08-05
        `orders` was omitted here and silently defaulted to None — which on that field does
        not mean "stale", it means *the book was never established*, the opposite claim to
        the one ledger and positions were making in the same snapshot. It had no visible
        effect, because `DashboardView.refresh` blanks the whole account half via
        `without_account()` as soon as `error` is set, so both routes agreed by accident.
        Relying on a consumer to mask an inconsistency is not the same as not having one:
        the snapshot is the contract, and every field in it now describes the same instant.
        """
        previous = self._snapshot
        self._snapshot = DashboardSnapshot(
            as_of=previous.as_of,
            ledger=previous.ledger,
            positions=previous.positions,
            orders=previous.orders,
            error=error,
            **flex,
        )

    def _flex_sections(self, flex: dict[str, Any] | None) -> dict[str, Any]:
        """Fresh Flex sections from this poll, or the previous snapshot's if the read failed.

        A failed local read means *no new information*, not *the information is gone*.
        The Flex dataset changes at most once a day, so carrying the previous sections
        forward is strictly more accurate than blanking the realised windows because
        SQLite was momentarily unavailable — which is what an earlier version did, since
        it spread an empty dict into the new snapshot and let every window default to
        None.

        `None` from `_read_flex` is the failure signal; a *successful* read always
        returns all six keys, so it can never be confused with an empty result.
        """
        if flex is not None:
            return flex
        previous = self._snapshot
        return {
            "week": previous.week,
            "month": previous.month,
            "ytd": previous.ytd,
            "stats": previous.stats,
            "series": previous.series,
            "coverage": previous.coverage,
        }

    def _read_flex(self) -> dict[str, Any] | None:
        """Read every Flex-derived section, or None if the read failed.

        Blocking SQLite — runs on a worker thread. Opens its own read-only connection
        per poll and closes it. A cached connection would be an object created on one
        `asyncio.to_thread` worker and used on another, which sqlite3 rejects outright;
        and holding a handle open across polls would keep a read transaction alive
        against a file the Flex sync is writing.

        Never raises: a missing or half-built store must not stop the account half of
        the poll. Returns None so `_flex_sections` can tell "the read failed" apart from
        "the read succeeded and found nothing", which are different claims.
        """
        try:
            with closing(connect(self._db_path)) as conn:
                return build_flex_sections(conn, self._today())
        except Exception as exc:
            log.warning("Dashboard Flex read failed: %s", exc)
            return None

    def _read_account(self) -> tuple[LedgerSnapshot | None, tuple[Position, ...]]:
        """Resolve the account if needed, then read the ledger and positions.

        Blocking HTTP — runs on a worker thread. Raises on any IBKR failure so
        `_poll_once` can preserve the previous snapshot's `as_of`; swallowing the error
        here would publish empty account data stamped with the current time, i.e. an
        empty dashboard that claims to be up to date.
        """
        account_id, base_currency = self._resolve_account()
        return fetch_ledger(self._client, account_id, base_currency), fetch_positions(
            self._client, account_id
        )

    def _read_entries(self, positions: tuple[Position, ...]) -> tuple[Position, ...]:
        """Attach reconstructed economic entry prices; never raise.

        A second SQLite open per poll rather than folding this into `_read_flex`, which
        runs *first and deliberately* so a logged-out gateway still gets its realised
        windows. This one needs the positions, so it cannot run before them without
        giving up that ordering. Two opens of a local read-only file every 15 seconds is
        a cheap price for keeping the gateway-independent half gateway-independent.

        A failure here degrades one column to blank, so it is logged and swallowed:
        losing the whole account panel because a reconstruction query failed would be a
        far worse trade than showing IBKR's basis alone.
        """
        if not positions:
            return positions
        try:
            with closing(connect(self._db_path)) as conn:
                return with_economic_entries(positions, economic_entries(conn, positions))
        except Exception as exc:
            log.warning("Dashboard economic-entry reconstruction failed: %s", exc)
            return positions

    def _resolve_account(self) -> tuple[str, str | None]:
        """Return the cached `(account_id, base_currency)`, resolving once on first use.

        Both come from the same `/portfolio/accounts` entry. The base currency is taken
        from here rather than from the ledger because the ledger's `BASE` row reports
        its own `currency` as the literal string `"BASE"` — see `parse_ledger`, which
        put "57,600.71 BASE" on screen before this was measured.

        Only a successful resolution is cached, so a poll attempted against a
        logged-out gateway retries on the next tick instead of poisoning the poller for
        the life of the process.
        """
        if self._account_id:
            return self._account_id, self._base_currency
        accounts = self._client.get_accounts()
        for entry in accounts or []:
            account_id = str(entry.get("accountId") or "").strip()
            if account_id:
                self._account_id = account_id
                self._base_currency = str(entry.get("currency") or "").strip().upper() or None
                log.info(
                    "DashboardPoller resolved account %s (base currency %s)",
                    account_id, self._base_currency or "unknown",
                )
                return account_id, self._base_currency
        raise RuntimeError("No IBKR account available (is the gateway authenticated?)")
