"""UI-free builders for the opening status message (Panel entry point).

Structured as pure/thread-friendly functions so `panel_app._init_session` stays readable
and tests can feed dict fixtures directly. Uses the `toolkit._store` reach-in; for config
it uses `toolkit._config`, since `ClaudeToolkit` exposes no public config/store property.

Began as a faithful port of the Chainlit app's startup status logic and **deliberately
stopped being one on 2026-08-05**, when the live dashboard took over the account figures.
What this builds now is the half chat still owns:

* the **live order book**, which no other surface shows (the dashboard's tabs are
  Chart · Positions · P&L);
* the **session state** — which of IBKR's two independent halves is answering
  (`account_readable`), and the `brokerage_session_down` flag that drives the
  Start-Gateway button and defers the background Flex sync;
* the **trade-dataset line** and the system-prompt trade/calendar context.

Account Summary, Open Positions and Account P&L were removed with the port's last
Chainlit-shaped assumption: that chat is the only place a number can live. So was the
reconciliation that re-parsed those rendered blocks against each other —
`dashboard_data.reconcile` does that on structured figures instead.
"""

import asyncio
import logging
from typing import Any

from ibkr_core_mcp import ClaudeToolkit, Config

from claudia.flex_sync import validate_dataset

log = logging.getLogger(__name__)

OFFLINE_STATUS = (
    "*IBKR not reachable — no account data. The dashboard is blank for the same reason, "
    "and will fill in when the connection returns.*"
)

# The middle state: account endpoints answer, the brokerage session does not. Named as
# what is missing rather than as "offline", because the balances on screen are live.
# Points at the dashboard rather than at "the data above": since 2026-08-05 there is no
# account data above — the dashboard is where it lives, and it is filling normally here.
BROKERAGE_SESSION_DOWN = (
    "*The **dashboard** is live — balances and positions come from the account endpoints "
    "and are unaffected. The **brokerage session** is not authenticated, so live orders, "
    "market data and order staging are unavailable until it is — use **Start IBKR "
    "Gateway** below.*"
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
    | up | up | live orders | False |
    | down | **up** | what is unavailable and why | True |
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

    **Live orders are all this returns (2026-08-05).** It used to open with Account
    Summary, Open Positions and Account P&L as well — the Chainlit-era opening statement,
    written when chat was the only surface there was. The live dashboard now polls all
    three every 15 seconds, so printing them here produced a second copy that was stale
    the moment it rendered, of exactly the figures the panel beside it keeps current.
    Two surfaces disagreeing about the account is the failure the middle row above exists
    to prevent; keeping a frozen duplicate was inviting it back in another form.

    Live orders stayed because nothing else shows them — the dashboard's tabs are
    Chart · Positions · P&L. That also removed the three `toolkit.execute` calls and the
    ledger fetch from the startup path, and with them the text-reparsing reconciliation
    that used to compare the rendered positions block against the rendered ledger block.
    The dashboard runs the same check on structured figures (`dashboard_data.reconcile`),
    where it cannot fail to parse and can compare currencies instead of guessing at a
    bare `$`.

    `toolkit.execute()` swallows exceptions and returns an error string rather than
    raising, which is why reachability is probed first instead of being inferred from the
    output.
    """
    try:
        gateway_up = await asyncio.to_thread(toolkit.client.ping)
        if not gateway_up and not await asyncio.to_thread(account_readable, toolkit):
            return OFFLINE_STATUS, True
        # Only ask for live orders when the brokerage session can answer. Without it the
        # call returns IBKR's "no bridge" 400, and rendering that under a **Live Orders**
        # heading reads as an account fault rather than as the session state it is.
        if not gateway_up:
            return BROKERAGE_SESSION_DOWN, True
        orders_text, _ = await asyncio.to_thread(toolkit.execute, "get_live_orders", {})
        return f"**Live Orders**\n{orders_text}", False
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        return OFFLINE_STATUS, True


def _integrity_phrases(config: Config) -> tuple[str, str]:
    """(status_line_fragment, system_prompt_sentence) reflecting the dataset verdict.

    Three outcomes, three wordings, and none of them assert more than was measured:

    * **validated** — the checks ran and passed; say so, to the user and to the model.
    * **failed** — name the failure in both places. The model must not be told the data
      is complete while the dashboard's realised windows are computed from rows that
      just failed a uniqueness or identity check.
    * **nothing to validate / could not check** — say nothing about integrity at all.
      A fresh install is not a corrupt one, and an unperformed check is not a pass.

    Never raises: a validation error must not cost the user their opening status.
    """
    try:
        validity = validate_dataset(config.sqlite_path)
    except Exception as exc:
        log.warning("Dataset validation could not run: %s", exc)
        return "", "Dataset integrity was not checked this session."
    if validity.empty:
        return "", "Dataset integrity was not checked this session."
    if validity.ok:
        return (
            ", integrity validated",
            "Dataset is complete and verified — no missing imports.",
        )
    log.error("Flex dataset validation FAILED at session start — %s", validity.summary)
    return (
        f", ⚠ integrity check FAILED: {validity.summary}",
        f"⚠ The local trade dataset FAILED validation at session start: {validity.summary}. "
        f"Do not present realised P&L or trade history from it as verified; say it is "
        f"unverified if the user asks for figures derived from it.",
    )


def build_trade_lines(toolkit: ClaudeToolkit, ibkr_offline: bool) -> tuple[str, str | None]:
    """(trade_status_line, trade_context_or_None) — the welcome status line and
    the system-prompt trade/calendar context for agent._trade_context.

    Blocking (SQLite reads) — call via asyncio.to_thread. Port of the removed app.py,
    including the subtlety that the market-calendar block appends to
    trade_context even when Flex is unconfigured.

    **The "integrity validated" phrase is earned here, not decoration (2026-08-05).**
    It — and the stronger claim made to the model, "Dataset is complete and verified —
    no missing imports" — used to rest on `get_trade_date_coverage` alone, which
    describes itself in its own docstring as an ACTIVITY REPORT: it counts trades and
    finds date gaps, and validates nothing. `flex_sync.validate_dataset` now runs the
    real checks (0.19s measured on the live 53 MB store.db) and the wording follows its
    verdict in all three directions — validated, failed, or nothing to validate.
    """
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
                integrity_note, integrity_context = _integrity_phrases(config)
                trade_status = f"Historical dataset loaded: {cov['total_trades']} trades ({cov['oldest']} → {cov['newest']}{integrity_note}) — {sync_note}"
                trade_context = (
                    f"## Trade History (local store)\n"
                    f"{cov['total_trades']} executions from {cov['oldest']} to {cov['newest']}. "
                    f"Last refreshed: {cov['newest']}. {integrity_context}\n"
                    f"Flex data lags 1 day (T+1). Newest entry being yesterday is normal, not stale. "
                    f"Do not flag the data as stale or suggest syncing unless the user explicitly asks "
                    f"or days_since_newest > 3 on a weekday.\n"
                    # SETTLED — do not re-raise this as an unbacked claim (it has been, twice).
                    # The five gaps are real inactivity in this account, audited 2026-06-30
                    # against the Drive XML archive: 2020-09-02→11-09, 2020-11-11→2021-01-07,
                    # 2023-01-10→03-02, 2024-06-13→08-05, 2024-12-06→2025-01-28. Re-confirmed
                    # 2026-08-05 — the sync reported those same five, unchanged, and no others.
                    # Integrity is proven separately and does not rest on this sentence:
                    # `flex_sync.validate_dataset` at session start, and ibkr_core_mcp's
                    # 42-check audit gate (42/42 on 2026-08-05, including 6/6 calendar years
                    # against IBKR's own annual statements). User-confirmed 2026-08-05.
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
