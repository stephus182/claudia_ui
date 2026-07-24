"""UI-free builders for the opening status message (Panel entry point).

Faithful port of the Chainlit startup status logic (app.py:399-514) restructured
into pure/thread-friendly functions so panel_app._init_session stays readable and
tests can feed dict fixtures directly. app.py is deliberately left untouched — it
is deleted wholesale at Phase 11 (cutover). Uses the toolkit._store reach-in —
the same one app.py itself uses (app.py:433,466); for config, the port
substitutes toolkit._config for app.py's module-global _config (app.py:427) —
behaviorally equivalent, both are Config.from_env() products. ClaudeToolkit
exposes no public config/store properties.
"""

import asyncio
import logging
from typing import Any

from ibkr_core_mcp import ClaudeToolkit

from claudia.execution_listener import get_live_pnl_text

log = logging.getLogger(__name__)

OFFLINE_STATUS = "*IBKR gateway not connected — data will load when gateway is online.*"

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


async def gather_status_block(toolkit: ClaudeToolkit) -> tuple[str, bool]:
    """(status_block_markdown, ibkr_offline).

    toolkit.execute() swallows all exceptions and returns an error string instead
    of raising, so we pre-check reachability and skip the calls when the gateway
    is unreachable. ping() verifies authentication (not just reachability); it
    retries once internally for the IBKR first-call quirk where
    authenticated=false on a fresh session. The 4-way gather over to_thread
    matches app.py's cl.make_async concurrency exactly (same thread-pool
    parallelism against IBKRClient — no new hazard)."""
    try:
        gateway_up = await asyncio.to_thread(toolkit.client.ping)
        if not gateway_up:
            raise ConnectionError("IBKR gateway not reachable")
        (opening_text, _), (orders_text, _), (positions_text, _), pnl_text = (
            await asyncio.gather(
                asyncio.to_thread(toolkit.execute, "get_account_summary", {}),
                asyncio.to_thread(toolkit.execute, "get_live_orders", {}),
                asyncio.to_thread(toolkit.execute, "get_positions", {}),
                asyncio.to_thread(get_live_pnl_text, toolkit),
            )
        )
        return (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Account P&L**\n{pnl_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        ), False
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        return OFFLINE_STATUS, True


def build_trade_lines(toolkit: ClaudeToolkit, ibkr_offline: bool) -> tuple[str, str | None]:
    """(trade_status_line, trade_context_or_None) — the welcome status line and
    the system-prompt trade/calendar context for agent._trade_context.

    Blocking (SQLite reads) — call via asyncio.to_thread. Port of app.py:426-513,
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
    # schedule). app.py:511 parity: appends even when trade_context is None.
    try:
        mkt = toolkit._store.get_market_calendar_context()
        if mkt:
            trade_context = (trade_context or "") + _format_market_calendar(mkt)
    except Exception as exc:
        log.warning("Could not build market calendar context: %s", exc)
    return trade_status, trade_context


def _format_market_calendar(mkt: dict[str, Any]) -> str:
    """Pure formatting of get_market_calendar_context's dict → the '## Market
    Calendar' system-prompt block (verbatim app.py:468-510 port)."""
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
