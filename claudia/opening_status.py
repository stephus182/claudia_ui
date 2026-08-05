"""UI-free builders for the opening status message (Panel entry point).

Faithful port of the Chainlit app's startup status logic, restructured into
pure/thread-friendly functions so panel_app._init_session stays readable and tests
can feed dict fixtures directly. Uses the toolkit._store reach-in; for config, it
substitutes toolkit._config for the old Chainlit app's module-global _config —
behaviorally equivalent, both are Config.from_env() products. ClaudeToolkit
exposes no public config/store properties.
"""

import asyncio
import logging
import re
from typing import Any

from ibkr_core_mcp import ClaudeToolkit

from claudia.execution_listener import get_live_pnl_text

log = logging.getLogger(__name__)

OFFLINE_STATUS = (
    "*IBKR not reachable — no account data. The dashboard is blank for the same reason, "
    "and will fill in when the connection returns.*"
)

# The middle state: account endpoints answer, the brokerage session does not. Named as
# what is missing rather than as "offline", because the balances on screen are live.
BROKERAGE_SESSION_DOWN = (
    "*Account data above is **live**. The **brokerage session** is not authenticated, so "
    "live orders, market data and order staging are unavailable until it is — use "
    "**Start IBKR Gateway** below. Balances and positions are served from the account "
    "endpoints and are unaffected.*"
)

_EXCHANGE_LABELS = {
    "XNYS": "NYSE", "CME": "CME Futures",
    "XLON": "LSE London", "XETR": "Xetra Frankfurt", "XEUR": "Eurex",
    "XPAR": "Euronext Paris", "XMIL": "Borsa Italiana",
    "XTKS": "TSE Tokyo", "XHKG": "HKEX Hong Kong", "XSHG": "SSE Shanghai",
    "XBOM": "BSE Mumbai", "XKRX": "KRX Seoul", "XASX": "ASX Sydney",
    "XTSE": "TSX Toronto", "BVMF": "B3 São Paulo", "XMEX": "BMV Mexico City",
    "XJSE": "JSE Johannesburg", "XSAU": "Tadawul (Sun–Thu week)",  # noqa: RUF001 — correct en-dash for a day range
    "XIDX": "IDX Jakarta", "XIST": "Borsa Istanbul",
}


# Positions and the ledger are two different IBKR endpoints. When nothing is moving they
# agree to a cent — measured live 2026-08-03, -3,638.51 summed vs -3,638.52 in the ledger.
#
# They do NOT agree while a fast leg is moving, and the gap does not self-correct quickly.
# Same session, CL (2 contracts x 1,000 bbl, so one $0.01 tick = $20.00) moved +$140: the
# ledger captured all of it, `get_positions` captured +$80 and then went stale. The two sat
# **$59.99 apart, unchanged, for 52 seconds** — not a sampling race, which would have varied.
#
# So the tolerance is a heuristic, not a rounding allowance. $250 is ~12 CL ticks, about 4x
# the largest lag observed, chosen (user's call, 2026-08-03) to stay quiet through ordinary
# futures drift while still catching the failures worth catching: a missing position, an
# inverted sign, a mis-parsed row. It is calibrated against ONE session's observation and a
# violent futures move could still trip it — that is why the warning text leads with lag
# rather than asserting a data error.
_RECONCILE_TOLERANCE = 250.00
# "**-$1,152.43**" / "$9,245.00" -> the signed amount. Bare $ deliberately: the
# positions table does not carry an ISO code (see the caveat in the warning text).
_MONEY = re.compile(r"([-+]?)\$([\d,]+\.\d{2})")
_LEDGER_UNREALIZED = re.compile(r"Unrealized P&L\s*:\s*\*{0,2}([-+]?)\$([\d,]+\.\d{2})")
# A positions row: | SYM | qty | mkt val | unrealized |  -- the LAST money cell is the P&L.
_POSITION_ROW = re.compile(
    r"^\|(?!\s*(?:Symbol|-))[^|]+\|.*\|\s*\*{0,2}([-+]?\$[\d,]+\.\d{2})", re.MULTILINE
)


def _money(sign: str, digits: str) -> float:
    """A ``(sign, digits)`` regex pair as a signed float, e.g. ``("-", "1,152.43")``."""
    return float(digits.replace(",", "")) * (-1.0 if sign == "-" else 1.0)


def reconcile_positions_against_ledger(
    positions_text: str, ledger_text: str, tolerance: float = _RECONCILE_TOLERANCE
) -> str | None:
    """Warn if the displayed positions do not sum to the displayed ledger P&L.

    Two different IBKR endpoints produce these, and they must agree to rounding —
    measured live 2026-08-03 they reconciled to **one cent** (-3,638.51 summed vs
    -3,638.52 in the ledger) while a *third* source, `get_pnl`'s real-time endpoint,
    was $128.51 away. That third one moving when the market is shut is expected and
    is not what this checks; this checks the invariant that should hold regardless.

    Parses the rendered text rather than re-fetching, deliberately: the property worth
    guaranteeing is that the two numbers **the user is shown** agree with each other.

    Fails **safe**. These strings are formatted in another repo; if either cannot be
    parsed the check returns None rather than raising an alarm it cannot substantiate.

    ⚠ It cannot verify currency. The positions table renders a bare `$` with no ISO
    code, so a position denominated in something other than the ledger's currency
    would make the sum invalid and trip this — IGV once priced a US ETF in MXN, so
    that is a real path, not a hypothetical. The warning says so rather than asserting
    a data error it cannot distinguish from a currency mix.

    Returns the warning line, or None when the numbers agree or cannot be read.
    """
    ledger_hit = _LEDGER_UNREALIZED.search(ledger_text or "")
    if not ledger_hit:
        return None
    rows = _POSITION_ROW.findall(positions_text or "")
    if not rows:
        return None
    total = 0.0
    for cell in rows:
        hit = _MONEY.search(cell)
        if not hit:
            return None  # a row we cannot read makes the whole sum untrustworthy
        total += _money(*hit.groups())

    ledger = _money(*ledger_hit.groups())
    # Round to cents BEFORE comparing. These are cent-denominated figures that do not
    # survive binary float exactly: summing the real 2026-08-03 positions yields
    # -3638.5099999999998, so a delta that is exactly five cents measures as
    # 0.0500000000001819 and would trip a "tolerance" of 0.05 by 1.8e-13 — a spurious
    # integrity alarm on money that reconciles perfectly.
    delta = round(abs(total - ledger), 2)
    if delta <= tolerance:
        return None
    return (
        f"⚠ Position P&L does not reconcile with the account ledger: positions sum to "
        f"{total:,.2f} USD, ledger reports {ledger:,.2f} USD — a difference of "
        f"{delta:,.2f} USD. Most often this means `get_positions` has gone stale while a "
        f"fast-moving leg (futures) kept ticking in the ledger; it can also mean a position "
        f"is denominated in a currency other than the ledger's, which cannot be checked "
        f"here because the positions table shows a bare $ with no ISO code. A gap this "
        f"large is past ordinary drift, so verify against IBKR before trading on either "
        f"figure."
    )


def account_readable(toolkit: ClaudeToolkit) -> bool:
    """Whether IBKR's **account** endpoints answer, independent of `ping()`.

    Blocking — call via `asyncio.to_thread`.

    `/portfolio/*` and `/iserver/*` are not one switch, and treating them as one is what
    put a contradiction on screen: the chat said "IBKR gateway not connected" beside a
    dashboard showing live, ticking account figures. Measured 2026-08-04, all three at
    the same moment:

        client.ping()                    -> False
        /portfolio/{id}/ledger           -> live (netliq 59,118.00, unrealised -10,101.02)
        /iserver/account/orders          -> HTTP 400 {"error": "Bad Request: no bridge"}

    "no bridge" is IBKR naming the thing that is actually missing: the brokerage session.
    Account data is served from the SSO session and keeps working without it. So `ping()`
    is the right question for *orders* and the wrong one for *balances*, and this asks the
    second question separately rather than inferring it from the first.

    `get_accounts` is the probe because it is the same call the dashboard poller already
    makes to resolve the account, so a pass here means the poller will populate too — the
    two panels cannot disagree about whether the account is reachable.
    """
    try:
        return bool(toolkit.client.get_accounts())
    except Exception as exc:
        log.warning("IBKR account endpoints unreachable: %s", exc)
        return False


async def gather_status_block(toolkit: ClaudeToolkit) -> tuple[str, bool]:
    """(status_block_markdown, brokerage_session_down).

    Three states, because IBKR has three — see `account_readable` for the measurement
    that forced the middle one to exist:

    | `ping()` | account reads | block | flag |
    |---|---|---|---|
    | up | up | full status, orders included | False |
    | down | **up** | account status + what is unavailable and why | True |
    | down | down | `OFFLINE_STATUS` | True |

    The middle row is the one this function used to get wrong. It short-circuited on
    `ping()` and declared the gateway disconnected, while the dashboard beside it drew
    live balances from the account endpoints that were answering perfectly. Whichever
    panel a user believed, the other one was telling them something false.

    The returned flag stays "the brokerage session is down", which is what its consumers
    actually want: it drives the Start-Gateway button and defers the background Flex
    sync. It is deliberately True in the middle row — order actions really are
    unavailable there — and the block says so in words instead of implying the account
    is unreadable.

    `toolkit.execute()` swallows exceptions and returns an error string rather than
    raising, which is why reachability is probed first instead of being inferred from the
    output. The gather over `to_thread` matches the removed Chainlit app.py's
    `cl.make_async` concurrency exactly (same thread-pool parallelism against
    `IBKRClient` — no new hazard).
    """
    orders_task: asyncio.Task[tuple[str, Any]] | None = None
    try:
        gateway_up = await asyncio.to_thread(toolkit.client.ping)
        if not gateway_up and not await asyncio.to_thread(account_readable, toolkit):
            return OFFLINE_STATUS, True
        # Only ask for live orders when the brokerage session can answer. Without it the
        # call returns IBKR's "no bridge" 400, and rendering that under a **Live Orders**
        # heading reads as an account fault rather than as the session state it is.
        # Started first so it still overlaps the other three, as it always has.
        if gateway_up:
            orders_task = asyncio.create_task(
                asyncio.to_thread(toolkit.execute, "get_live_orders", {})
            )
        (opening_text, _), (positions_text, _), pnl_text = await asyncio.gather(
            asyncio.to_thread(toolkit.execute, "get_account_summary", {}),
            asyncio.to_thread(toolkit.execute, "get_positions", {}),
            asyncio.to_thread(get_live_pnl_text, toolkit),
        )
        # Two different IBKR endpoints produced the two blocks above. They must agree
        # to rounding, and a session that opens on figures that do not is something the
        # user needs told before they act on them, not something to discover later.
        mismatch = reconcile_positions_against_ledger(positions_text, pnl_text)
        if mismatch:
            log.warning("Opening status reconciliation failed: %s", mismatch)
        block = (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Account P&L**\n{pnl_text}\n\n"
            + (f"{mismatch}\n\n" if mismatch else "")
        )
        if orders_task is None:
            return block + BROKERAGE_SESSION_DOWN, True
        orders_text, _ = await orders_task
        return block + f"**Live Orders**\n{orders_text}", False
    except Exception as exc:
        if orders_task is not None and not orders_task.done():
            orders_task.cancel()  # else the failure surfaces as a stray unretrieved task
        log.warning("Could not load IBKR opening status: %s", exc)
        return OFFLINE_STATUS, True


def build_trade_lines(toolkit: ClaudeToolkit, ibkr_offline: bool) -> tuple[str, str | None]:
    """(trade_status_line, trade_context_or_None) — the welcome status line and
    the system-prompt trade/calendar context for agent._trade_context.

    Blocking (SQLite reads) — call via asyncio.to_thread. Port of the removed app.py,
    including the subtlety that the market-calendar block appends to
    trade_context even when Flex is unconfigured."""
    config = toolkit._config
    flex_configured = bool(config and config.flex_token and config.flex_query_id)
    trade_context: str | None = None
    if flex_configured:
        try:
            cov = toolkit._store.get_trade_date_coverage()
            if cov["oldest"]:
                if ibkr_offline:
                    days = cov["days_since_newest"]
                    sync_note = f"last refreshed {cov['newest']} ({days}d ago) — connect IBKR to refresh"
                else:
                    sync_note = f"last refreshed {cov['newest']}"
                trade_status = f"Historical dataset loaded: {cov['total_trades']} trades ({cov['oldest']} → {cov['newest']}, integrity validated) — {sync_note}"
                trade_context = (
                    f"## Trade History (local store — integrity validated)\n"
                    f"{cov['total_trades']} executions from {cov['oldest']} to {cov['newest']}. "
                    f"Last refreshed: {cov['newest']}. Dataset is complete and verified — no missing imports.\n"
                    f"Flex data lags 1 day (T+1). Newest entry being yesterday is normal, not stale. "
                    f"Do not flag the data as stale or suggest syncing unless the user explicitly asks "
                    f"or days_since_newest > 3 on a weekday.\n"
                    f"Date gaps in the dataset are verified inactivity periods (no trading). "
                    f"Do not mention gaps or suggest XML backfill unless the user specifically asks about data integrity.\n"
                    f"Use `get_trades` (default: source='store') for any analysis beyond 6 days. "
                    f"Today's intraday trades: use `get_trades source='live'`."
                )
            else:
                trade_status = "Trade history: no data yet — syncing…"
                trade_context = (
                    "## Trade History (local store)\n"
                    "No trade data yet in the local store. Run `sync_flex_trades` to import recent data, "
                    "or `sync_flex_archive` to import historical XMLs from Drive."
                )
        except Exception as exc:
            log.warning("Could not read trade date coverage: %s", exc)
            trade_status = "Trade history: syncing…"
    else:
        trade_status = "Trade history: Flex not configured (set IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID)"

    # Append market calendar context (holidays, last/next trading day, futures
    # schedule). Parity with the removed app.py: appends even when trade_context is None.
    try:
        mkt = toolkit._store.get_market_calendar_context()
        if mkt:
            trade_context = (trade_context or "") + _format_market_calendar(mkt)
    except Exception as exc:
        log.warning("Could not build market calendar context: %s", exc)
    return trade_status, trade_context


def _format_market_calendar(mkt: dict[str, Any]) -> str:
    """Pure formatting of get_market_calendar_context's dict → the '## Market
    Calendar' system-prompt block (verbatim port from the removed app.py)."""
    holiday_lines = []
    for xcode, holidays in mkt.get("holidays_by_exchange", {}).items():
        name = _EXCHANGE_LABELS.get(xcode, xcode)
        holiday_lines.append(
            f"{name}: {', '.join(holidays)}" if holidays else f"{name}: no holidays this year/next"
        )

    fut = mkt.get("futures", {})
    cme_extra = fut.get("cme_open_nyse_closed", [])
    group_lines = []
    for gname, g in fut.get("product_groups", {}).items():
        syms = ", ".join(g["products"][:4]) + ("…" if len(g["products"]) > 4 else "")
        group_lines.append(
            f"  {gname.replace('_', ' ').title()} ({g['exchange']}): "
            f"{g['globex_hours_ct']} [{syms}]"
            + (f" — {g['note']}" if "note" in g else "")
        )

    return (
        f"\n\n## Market Calendar\n"
        f"Today: {mkt['today']} ({'trading day' if mkt['is_trading_day'] else 'non-trading day'} on NYSE).\n"
        f"Last trading day (NYSE): {mkt['last_trading_day']}. "
        f"Next trading day (NYSE): {mkt['next_trading_day']}.\n\n"
        f"### Exchange Holidays (current + next year)\n" +
        "\n".join(f"  - {line}" for line in holiday_lines) + "\n\n"
        f"### Futures vs Securities — Key Distinction\n"
        f"{fut.get('note', '')}\n"
        f"Maintenance break: {fut.get('maintenance_break_ct', 'N/A')}\n"
        f"CME open when NYSE is closed: {', '.join(cme_extra) if cme_extra else 'none this period'}\n\n"
        f"### CME Globex Product Schedule (all times CT)\n" +
        "\n".join(group_lines) + "\n"
    )
