"""Human-initiated order staging for ClaudIA — the framework-agnostic execution core.

The LLM never calls this code directly. Flow:
  1. ClaudIA calls the `propose_order` tool (claudia/proposal_tools.py), which records a
     proposal and reaches nothing.
  2. agent.py hands the recorded dict to the MessageSink
     (send_order_proposal), which the Panel sink routes to
     panel_order_flow.render_order_proposal() — a message with a "Stage this
     order" button.
  3. User sees full order details + the button.
  4. User clicks → panel_order_flow's handler calls _execute_staged_order_core()
     (the cores in THIS module hold all the safety-critical logic; the UI layer
     only renders buttons and supplies a send_status callback).
  5. IBKRClient.place_order_and_confirm() fires:
       Gate 1 — Touch ID (human_auth.require_touch_id)
       Gate 2 — AppKit colored dialog, green/BUY or red/SELL (order_confirm)
       Any chained IBKR reply prompts are resolved in a loop, each re-running
       Gate 1 + Gate 2 with the real IBKR warning text, until a terminal response.
  6. **L2 read-back**: the dispatch response only proves the request was received, so each
     core then reads the order's real state and reports what it observed. Evidence is the
     only source of truth for orders. A placement is validated by *presence in the live
     order book* (_read_back_place) — the strongest evidence available, and absence is
     never evidence; a cancel or modify by get_order_status (_read_back), because Cancelled
     is filtered out of the live book and disappearance there proves nothing.
  7. The result is logged to ConversationStore.decisions (if store is wired), recording
     the observed state — including "not observed" when nothing could be read.

No order can be placed without steps 3–5 happening via physical user interaction.
ClaudIA must never modify any user-specified order parameter (price, qty, symbol, type, TIF).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ibkr_core_mcp.order_confirm import change_value_text

from claudia.contract_identity import contract_identity

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)

SendStatus = Callable[[str, str], Awaitable[None]]
"""(text, author) -> None — the UI status callback the *_core functions call to surface
progress and results. Injected by the caller (panel_order_flow) so the cores stay
framework-agnostic: they never import or know about any specific UI toolkit."""


_MISSING_PRICE = "⚠️ NO %s PRICE GIVEN"
"""Shown when a priced order type arrives without the price it needs.

`_PRICE` is nullable in the schema, so `{"order_type": "LMT", "limit_price": null}` is a
valid proposal. Rendering that as a bare `(LMT, DAY)` would put a limit order with no
visible limit in front of the user — the same silent omission as the STOP_LIMIT gap, and
the execution core would then send IBKR a LMT body with no `price` field at all.

This only makes the gap visible. Refusing the proposal outright belongs in
`agent._proposal_defect`, alongside the other four guarantees `strict` cannot express;
until that lands, the approval screen must at least not hide it.
"""


def _price_suffix(order_type: str, limit: float | None, stop: float | None) -> str:
    """The price clause of an approval line, e.g. `" @ 6,000.00 limit / 5,950.00 stop"`.

    Shared by `_format_order_summary` and `_format_cancel_summary` so the two cannot drift;
    each used to carry its own copy, and only one of them showed a stop price.

    **No currency symbol.** A bare `$` is shared by USD/MXN/CAD/AUD/HKD/SGD, so on a
    wrong-currency contract it reads as an ordinary price — and this account trades
    EUR-denominated equities. The proposal carries no currency field, so the honest render
    is the number alone: exactly what `panel_dashboard.fmt_money` does when the currency is
    unknown ("a bare `0.00` is honest where `0.00 USD` on a EUR account would not be").
    Thousands separator matches that formatter too.

    **STOP_LIMIT shows both prices.** It is in the `order_type` enum and
    `_execute_staged_order_core` sends `price` (limit) *and* `auxPrice` (stop) for it, but
    neither formatter rendered either one — so the last screen before Touch ID showed a
    stop-limit order with no price at all.

    Args:
        order_type: MKT, LMT, STP or STOP_LIMIT.
        limit: Limit price, or None.
        stop: Stop price, or None.

    Returns:
        A leading-space clause ready to append, or `""` when the type carries no price (MKT)
        or the price it needs is absent.
    """
    parts = []
    if order_type in ("LMT", "STOP_LIMIT"):
        parts.append(f"{limit:,.2f} limit" if limit is not None else _MISSING_PRICE % "LIMIT")
    if order_type in ("STP", "STOP_LIMIT"):
        parts.append(f"{stop:,.2f} stop" if stop is not None else _MISSING_PRICE % "STOP")
    return f" @ {' / '.join(parts)}" if parts else ""


def _futures_contract_facts(ibkr: Any, conid: int) -> tuple[float | None, str | None, str]:
    """(multiplier, currency, label) for a futures contract, from `/iserver/contract/{conid}/info`.

    Measured 2026-09-04 on ES conid 649180671: `multiplier` `'50'`, `currency` `'USD'`,
    `local_symbol` `'ESU6'`, `maturity_date` `'20260918'`. `/trsrv/futures` — the chain the
    resolver uses — carries no multiplier at all, so this is the only source. Read-only, and
    total by construction: any failure or unparseable field yields `None`/`""`, and the
    caller then tells Gate 2 the multiplier is unknown rather than printing a wrong number.

    The label, e.g. `ESU6 · expires 2026-09-18`, is what the dialog shows as the
    symbol line — the contract month, which neither the proposal text nor the dialog
    showed before 2026-09-04.
    """
    try:
        info = ibkr.get_contract_info(conid) or {}
    except Exception as exc:
        log.warning("Contract info unavailable for conid %s: %s", conid, exc)
        return None, None, ""
    try:
        multiplier: float | None = float(info["multiplier"])
    except (KeyError, TypeError, ValueError):
        multiplier = None
    raw_ccy = info.get("currency")
    currency = (
        str(raw_ccy).strip().upper() if isinstance(raw_ccy, str) and raw_ccy.strip() else None
    )
    parts: list[str] = []
    local_symbol = info.get("local_symbol")
    if isinstance(local_symbol, str) and local_symbol.strip():
        parts.append(local_symbol.strip())
    maturity = str(info.get("maturity_date") or "")
    if len(maturity) == 8 and maturity.isdigit():
        parts.append(f"expires {maturity[:4]}-{maturity[4:6]}-{maturity[6:]}")
    # The multiplier is contract SIZE, not identity: it used to ride here as `· x50`, so the
    # Symbol line read as if the size were part of the name (gap #45, user 2026-09-10). It
    # now goes to the dialog's Quantity row (`1 (×50 per contract)`) through `_multiplier`.
    return multiplier, currency, " · ".join(parts)


def proposal_contract_label(proposal: dict[str, Any]) -> str | None:
    """The resolved contract's label (`ESU6 · SEP26 · expires 2026-09-18`) for a futures
    proposal's approval text, or None.

    Read-only and fail-soft by design: a stock returns None before any client is built, and
    any failure returns None so the proposal still renders — Gate 2 carries the same label
    independently. The render site calls this in a thread.
    """
    if str(proposal.get("sec_type", "STK")).upper() not in ("FUT", "FOP"):
        return None
    conid = proposal.get("conid")
    if conid in (None, ""):
        return None
    try:
        from dotenv import load_dotenv
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient

        load_dotenv(override=False)
        ibkr = IBKRClient(
            config=Config.from_env(),
            auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")),
        )
        identity = contract_identity(ibkr, int(conid))
    except Exception as exc:
        log.warning("Contract label unavailable for conid %s: %s", conid, exc)
        return None
    return identity.label if identity is not None else None


def cancel_proposal_contract_label(proposal: dict[str, Any]) -> str | None:
    """The resolved contract's label for a CANCEL proposal's approval text, or None.

    A cancel proposal cannot use `proposal_contract_label`: `propose_cancel`'s schema is the
    order id plus display context, with **no `conid` and no `sec_type`**, so there is nothing
    to resolve from. The contract has to come from the live order itself — which is exactly
    where Gate 2 already reads it (`_cancel_display_details`). Until this existed the cancel
    card was the one order surface that could not name the contract it was about to remove,
    rendering a bare `BUY 1 ES` while the place card, both dialogs and the Orders tab all
    said `ESU6` (gap #37, found live 2026-09-10).

    Read-only and fail-soft, like the place path: any failure returns None and the card
    renders without the line rather than blocking a cancel.

    Args:
        proposal: Schema-checked cancel-proposal dict; `order_id` names the live order.

    Returns:
        The contract label, or None for a non-future or any failed read.
    """
    order_id = str(proposal.get("order_id", "") or "")
    if not order_id:
        return None
    try:
        from dotenv import load_dotenv
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient

        load_dotenv(override=False)
        ibkr = IBKRClient(
            config=Config.from_env(),
            auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")),
        )
        status = ibkr.get_order_status(order_id)
        if str(status.get("sec_type", "")).upper() not in ("FUT", "FOP"):
            return None
        conid = status.get("conid")
        if conid in (None, ""):
            return None
        identity = contract_identity(ibkr, int(conid))
    except Exception as exc:
        log.warning("Contract label unavailable for cancel of order %s: %s", order_id, exc)
        return None
    return identity.label if identity is not None else None


def _number_or_none(value: Any) -> float | None:
    """IBKR's status endpoint reports prices as strings — '7900.00', or '' when absent."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _current_order_description(ibkr: Any, order_id: str) -> str | None:
    """IBKR's own one-line description of a working order (`order_description_with_contract`,
    e.g. "Buy 1 ES Sep18'26 Stop 7900.00, GTC"), for the modify dialog's `Currently at IBKR`
    row. A failed read returns None — the dialog then shows the request alone; it never
    blocks."""
    try:
        status = ibkr.get_order_status(order_id)
    except Exception as exc:
        log.warning("Order %s: status read for the dialog failed: %s", order_id, exc)
        return None
    if not isinstance(status, dict):
        return None
    text = status.get("order_description_with_contract")
    return str(text) if text else None


def _apply_futures_display_facts(ibkr: Any, order: dict[str, Any], conid: int) -> None:
    """Attach the futures label, multiplier and currency as display-only keys (gap #34 on the
    place path since 2026-09-04; modify and cancel since 2026-09-10)."""
    multiplier, currency, contract_label = _futures_contract_facts(ibkr, conid)
    if contract_label:
        order["_companyName"] = contract_label
    if multiplier is not None:
        order["_multiplier"] = multiplier
    else:
        order["_multiplier_unknown"] = True
    if currency:
        order["_currency"] = currency


_STOCK_FACTS: dict[int, tuple[str | None, str | None]] = {}


def clear_stock_facts_cache() -> None:
    """Tests only."""
    _STOCK_FACTS.clear()


def _stock_contract_facts(ibkr: Any, conid: int) -> tuple[str | None, str | None]:
    """(company_name, currency) for a stock, from `/iserver/contract/{conid}/info`.

    The same read `_futures_contract_facts` makes for a future. Live 2026-09-11 (gap #49):
    `SELL 1 GLD LMT 600` reached Gate 2 as `Symbol: GLD`, `Price: 600.00` — no name, no
    currency — while IBKR's own status for the order said `USD`. A future's price is index
    points, not money, so no currency there is right; a stock's price IS money, and the rule
    is an ISO code or nothing, never a guess (IGV is USD on BATS and MXN on MEXI). Cached per
    conid; a failed or empty read is (None, None) and is not cached, so Gate 2 prints nothing
    rather than a guess and the next attempt may succeed.
    """
    if conid in _STOCK_FACTS:
        return _STOCK_FACTS[conid]
    try:
        info = ibkr.get_contract_info(conid) or {}
    except Exception as exc:
        log.warning("Contract info unavailable for conid %s: %s", conid, exc)
        return (None, None)
    name = str(info.get("company_name") or "").strip() or None
    raw = info.get("currency")
    currency = str(raw).strip().upper() if isinstance(raw, str) and raw.strip() else None
    if name is None and currency is None:
        return (None, None)
    _STOCK_FACTS[conid] = (name, currency)
    return _STOCK_FACTS[conid]


def _apply_contract_display_facts(
    ibkr: Any, order: dict[str, Any], conid: int, sec_type: str
) -> None:
    """Display-only keys for Gate 2, by instrument (#49, 2026-09-11).

    Futures get the contract label, multiplier and currency (`_apply_futures_display_facts`);
    everything else gets its listing's name and currency. A name already on the order — the
    proposal's own `_companyName` — is kept.
    """
    if sec_type.upper() in ("FUT", "FOP"):
        _apply_futures_display_facts(ibkr, order, conid)
        return
    name, currency = _stock_contract_facts(ibkr, conid)
    if name and not order.get("_companyName"):
        order["_companyName"] = name
    if currency:
        order["_currency"] = currency


# IBKR's order status spells a limit `LIMIT` and a stop `STP`; the proposals — and so the
# place and modify dialogs — say `LMT` / `STP` / `STOP_LIMIT` / `MKT`. Read live 2026-09-11:
# the cancel dialog said `LIMIT` for the order the place dialog had called `LMT`. One
# vocabulary on every dialog; the `Currently at IBKR` row keeps IBKR's own sentence.
_IBKR_ORDER_TYPE_WORDS = {"LIMIT": "LMT", "MARKET": "MKT", "STOP": "STP"}


def _cancel_display_details(ibkr: Any, proposal: dict[str, Any]) -> dict[str, Any]:
    """The IBKR-shaped display dict for the cancel dialog (gap #40).

    Read from the live order status when it can be read — side, size, type, prices, TIF,
    outside-RTH and IBKR's own description — so the dialog shows the order as IBKR holds
    it; on a failed read, from the proposal's own fields, so a cancel is never blocked by a
    read. Futures get the contract label, multiplier and currency like the place path.
    """
    order_id = str(proposal.get("order_id", ""))
    status: dict[str, Any] | None = None
    try:
        raw = ibkr.get_order_status(order_id)
        status = raw if isinstance(raw, dict) else None
    except Exception as exc:
        log.warning("Order %s: status read for the cancel dialog failed: %s", order_id, exc)
    if status:
        side_raw = str(status.get("side", "")).upper()
        side = {"B": "BUY", "S": "SELL"}.get(side_raw, side_raw or str(proposal.get("action", "?")))
        raw_type = str(status.get("order_type") or proposal.get("order_type") or "?")
        order_type = _IBKR_ORDER_TYPE_WORDS.get(raw_type.upper(), raw_type)
        details: dict[str, Any] = {
            "ticker": status.get("symbol") or proposal.get("symbol", "?"),
            "side": side,
            "quantity": status.get("size") or proposal.get("quantity", "?"),
            "orderType": order_type,
            "tif": status.get("tif") or proposal.get("tif", "?"),
        }
        limit_price = _number_or_none(status.get("limit_price"))
        stop_price = _number_or_none(status.get("stop_price"))
        if order_type == "STOP_LIMIT":
            if limit_price is not None:
                details["price"] = limit_price
            if stop_price is not None:
                details["auxPrice"] = stop_price
        elif order_type == "STP" and stop_price is not None:
            details["price"] = stop_price
        elif limit_price is not None:
            details["price"] = limit_price
        if isinstance(status.get("outside_rth"), bool):
            details["outsideRTH"] = status["outside_rth"]
        text = status.get("order_description_with_contract")
        if text:
            details["_current_description"] = str(text)
        conid = status.get("conid")
        if conid is not None:
            _apply_contract_display_facts(
                ibkr, details, int(conid), str(status.get("sec_type", ""))
            )
        return details
    details = {
        "ticker": proposal.get("symbol", "?"),
        "side": proposal.get("action", "?"),
        "quantity": proposal.get("quantity", "?"),
        "orderType": proposal.get("order_type", "?"),
        "tif": proposal.get("tif", "?"),
    }
    proposal_type = proposal.get("order_type")
    if proposal_type == "STOP_LIMIT":
        if proposal.get("limit_price") is not None:
            details["price"] = proposal["limit_price"]
        if proposal.get("stop_price") is not None:
            details["auxPrice"] = proposal["stop_price"]
    elif proposal_type == "STP" and proposal.get("stop_price") is not None:
        details["price"] = proposal["stop_price"]
    elif proposal.get("limit_price") is not None:
        details["price"] = proposal["limit_price"]
    return details


def _apply_outside_rth(order_body: dict[str, Any], proposal: dict[str, Any]) -> None:
    """Copy the proposal's `outside_rth` into the body as IBKR's `outsideRTH` — only when stated.

    `None` (the user did not say) sends nothing, which is IBKR's default; `True`/`False` go
    verbatim, and nothing else is coerced — `_proposal_defect` rejects a non-boolean before
    it reaches here, and this guard is the belt to that brace (order-parameter immutability:
    no fabricated value in either direction). Shared by the place and modify paths so a modify — which resends the
    whole order — cannot drop the attribute the original carried. Why it exists: IBKR
    simulates stops on US futures and triggers them only in RTH unless this is set
    (docs/order-api-reference.md § Stop orders on US futures, 2026-09-04).
    """
    value = proposal.get("outside_rth")
    if isinstance(value, bool):  # never bool(): "false" would become True (review #4)
        order_body["outsideRTH"] = value


def _outside_rth_line(proposal: dict[str, Any]) -> str | None:
    """The approval line for the outside-RTH attribute, or None when nothing needs saying.

    A stop or stop-limit on a future ALWAYS gets the line: "no" is the dangerous default
    (the stop is inert outside regular trading hours) and belongs on the last screen before
    Touch ID as much as "yes" does. Any other order mentions it only when the user set it.
    """
    otype = str(proposal.get("order_type", "MKT")).upper()
    sec_type = str(proposal.get("sec_type", "STK")).upper()
    value = proposal.get("outside_rth")
    futures_stop = sec_type in ("FUT", "FOP") and otype in ("STP", "STOP_LIMIT")
    # A stated value reads differently from "not set": the bodies differ (False is sent,
    # None is not), so the approval text must too (review #5).
    if value is True:
        return "⏰ Outside RTH: **yes** — active through the whole electronic session."
    if value is False:
        return "⏰ Outside RTH: **no** (stated) — regular trading hours only."
    if futures_stop:
        return (
            "⏰ Outside RTH: **not set** — IBKR triggers futures stops during regular "
            "trading hours only; say so if you want it active through the electronic session."
        )
    return None


def _format_order_summary(proposal: dict[str, Any], contract_label: str | None = None) -> str:
    """Build the human-approval text for a new order.

    Safety surface: this string, plus the Gate 2 dialog, is everything the user sees before
    authorising a live order. It deliberately ends with the Touch-ID/confirmation warning so
    the consequence of the click is never implicit.

    Recognised keys: `symbol`, `action`, `quantity`, `order_type` (default MKT),
    `limit_price` / `stop_price` (rendered by `_price_suffix`, which covers LMT, STP and
    STOP_LIMIT), `sec_type` (default STK; labelled inline only when it is not STK),
    `outside_rth` (rendered by `_outside_rth_line` — always for a futures stop, otherwise
    only when set), and `reason`. TIF is read from `tif`, then `time_in_force`, then `timeInForce`, defaulting
    to DAY — the identical expression `_execute_staged_order_core` uses, so display and
    execution cannot diverge. Do not change one without the other.

    `contract_label` (design 2026-09-10, gap #37) is the resolved futures contract in IBKR's
    own terms — `ESU6 · SEP26 · expires 2026-09-18` from `proposal_contract_label` — shown as a
    `Contract:` line on a future only; a bare root in the proposal means the front month.

    Args:
        proposal: Schema-checked order-proposal dict.
        contract_label: The resolved contract's label for a future, or None for no line.

    Returns:
        Markdown for the proposal message. Rendered via `safe_markdown` — `reason` is
        free-form LLM text.
    """
    symbol = proposal.get("symbol", "?")
    action = proposal.get("action", "?")
    qty = proposal.get("quantity", "?")
    otype = proposal.get("order_type", "MKT")
    limit = proposal.get("limit_price")
    stop = proposal.get("stop_price")
    tif = (
        proposal.get("tif") or proposal.get("time_in_force") or proposal.get("timeInForce") or "DAY"
    ).upper()
    sec_type = proposal.get("sec_type", "STK").upper()
    reason = proposal.get("reason", "")

    price_str = _price_suffix(otype, limit, stop)

    sec_label = f" [{sec_type}]" if sec_type != "STK" else ""
    lines = [
        f"**{action} {qty} {symbol}{sec_label}** ({otype}{price_str}, {tif})",
    ]
    if sec_type in ("FUT", "FOP") and contract_label:
        # The resolved contract in IBKR's own terms (design 2026-09-10): a bare root means
        # the front month, and this line says which contract that is before Touch ID.
        lines.append(f"**Contract:** {contract_label}")
    rth_line = _outside_rth_line(proposal)
    if rth_line:
        lines.append(rth_line)
    if reason:
        lines.append(f"*Reason:* {reason}")
    lines.append(
        "⚠️ **Clicking 'Stage this order' will initiate IBKR confirmation "
        "(Touch ID + visual confirmation dialog). You can still cancel at that step.**"
    )
    # Blank line between entries, not a bare newline: Markdown renders a single newline
    # as a space, which fused every field of this text into one paragraph (gap #44).
    return "\n\n".join(lines)


def _post_dispatch_failure_text(exc: Exception, noun: str) -> str:
    """The failure message for an exception raised *after* the IBKR write returned.

    Once the dispatch call has returned, the write reached IBKR. Anything that fails
    afterwards — surfacing the result, reading the state back, writing the decision row —
    is a reporting failure, not a placement failure. Telling the user "Order not placed"
    there would be the same assert-without-evidence defect this module's read-back exists
    to close, only pointed the other way.
    """
    return (
        f"⚠️ **The {noun} WAS dispatched to IBKR** — this failure happened afterwards, "
        f"while reporting or recording it: {_classify_execution_error(exc)}. "
        f"Its live state is unknown from here — check IBKR directly."
    )


def _reply_first_line(text: str, limit: int = 120) -> str:
    """The first non-empty line of an IBKR reply's cleaned text, cut to `limit` characters.

    For a status line only — the decision row keeps the full raw and cleaned texts.
    """
    for raw_line in text.replace("\xa0", " ").splitlines():
        line = " ".join(raw_line.split())
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def _format_reply_log(reply_log: list[dict[str, Any]]) -> str:
    """The IBKR precautions the human confirmed before the write was accepted (gap #38).

    Rendered from the client's reply records so the chat says what the human clicked
    through — IBKR's own words, first line each — instead of a bare "accepted". "" when
    no reply was confirmed.
    """
    confirmed = [r for r in reply_log if r.get("confirmed")]
    if not confirmed:
        return ""
    noun = "confirmation" if len(confirmed) == 1 else "confirmations"
    lines = [f"**IBKR asked {len(confirmed)} {noun} before accepting:**"]
    for i, record in enumerate(confirmed, 1):
        text = str(record.get("message_text") or record.get("message") or "")
        lines.append(f"{i}. {_reply_first_line(text)}")
    return "\n".join(lines)


def _declined_reply_text(reply_log: list[dict[str, Any]]) -> str:
    """Name the IBKR prompt the human declined — the log's last unconfirmed record, or ""."""
    declined = [r for r in reply_log if not r.get("confirmed")]
    if not declined:
        return ""
    text = str(declined[-1].get("message_text") or declined[-1].get("message") or "")
    first = _reply_first_line(text)
    return f" Declined prompt: {first}" if first else ""


# One table behind the user-facing sentence and the recorded stage of a refusal (#50,
# 2026-09-11): (predicate on (message, exception type name), stage, sentence). Order
# matters and is the classifier's original order — a dialog cancel is matched before the
# Touch ID patterns so it is never misreported as a Touch ID failure, and a Touch ID timeout
# stays a Touch ID failure (the `"touch" not in` guard on the dialog-timeout row).
_FAILURE_PATTERNS: tuple[tuple[Callable[[str, str], bool], str, str], ...] = (
    (
        lambda m, _t: "cancelled by user" in m.lower(),
        "gate2",
        "Order was cancelled at the confirmation dialog.",
    ),
    (
        lambda m, _t: "declined ibkr order reply" in m.lower(),
        "reply",
        "Declined at a follow-up IBKR confirmation prompt after Gate 2 was approved — "
        "check IBKR for the order's current status.",
    ),
    (
        lambda m, _t: "timed out" in m.lower() and "touch" not in m.lower(),
        "timeout",
        "Confirmation dialog timed out (60 seconds) — no action was taken.",
    ),
    (
        lambda m, t: "authentication" in m.lower() or "touch" in m.lower() or "HumanAuth" in t,
        "touch_id",
        "Touch ID authentication failed or was cancelled.",
    ),
    (
        lambda m, _t: "403" in m,
        "rejected",
        "IBKR rejected the order (HTTP 403) — brokerage session may need "
        "re-initialisation. Try logging in to the Client Portal gateway and retrying.",
    ),
)


def _classify_execution_error(exc: Exception) -> str:
    """Map an exception from a Gate 1/2-guarded IBKR call to a user-facing message.

    Shared by the three _execute_*_order_core functions — all
    route through the same Touch ID (Gate 1) + AppKit dialog (Gate 2) gates in ibkr_core_mcp,
    so the same failure modes (dialog cancel, Touch ID failure, reply-chain decline, timeout,
    403) can occur regardless of which order action triggered them. Check most-specific
    patterns first so a dialog cancel is never misreported as a Touch ID failure.
    """
    error_msg = str(exc)
    exc_type = type(exc).__name__
    for matches, _stage, sentence in _FAILURE_PATTERNS:
        if matches(error_msg, exc_type):
            return sentence
    return f"{exc_type}: {error_msg}"


def _refusal_stage(exc: Exception) -> str:
    """Which gate refused, for the decision row a refusal writes (#50, 2026-09-11).

    `gate2` (DO NOT SEND / KEEP ORDER / LEAVE UNCHANGED), `reply` (a declined precaution),
    `timeout` (the dialog auto-cancelled), `touch_id` (denied, unavailable, or timed out at
    the sensor), `rejected` (IBKR 403), or `other`. Read off the same table as
    `_classify_execution_error`, so the stage and the sentence cannot disagree.
    """
    error_msg = str(exc)
    exc_type = type(exc).__name__
    return next(
        (stage for matches, stage, _ in _FAILURE_PATTERNS if matches(error_msg, exc_type)), "other"
    )


def _refusal_subject(noun: str, proposal: dict[str, Any]) -> str:
    """The one line a refusal row names its order by — the success rows' own wording."""
    if noun == "trade":
        return (
            f"{proposal.get('action', '?')} {proposal.get('quantity', '?')} "
            f"{proposal.get('symbol', '?')} ({proposal.get('order_type', 'MKT')})"
        )
    return f"order {proposal.get('order_id', '?')} ({proposal.get('symbol', '?')})"


def _record_outcome(
    store: Any,
    session_id: str | None,
    *,
    decision_type: str,
    summary: str,
    symbol: str | None,
    metadata: dict[str, Any],
) -> None:
    """Write one decision row for an outcome after the button; never let it mask the report."""
    if store is None or not session_id:
        return
    try:
        store.add_decision(
            session_id=session_id,
            decision_type=decision_type,
            summary_text=summary,
            symbol=symbol,
            metadata=metadata,
        )
    except Exception:  # the record must never mask the report the user sees
        log.exception("Could not record %s", decision_type)


def _record_refusal(
    store: Any,
    session_id: str | None,
    *,
    noun: str,
    proposal: dict[str, Any],
    exc: Exception,
    reply_log: list[dict[str, Any]],
    dispatched: bool,
    symbol: str | None,
) -> None:
    """Write the decision a refusal or failure after the button leaves behind (#50).

    Until 2026-09-11 every refusal — Touch ID denied, DO NOT SEND, a declined precaution, a
    timeout — reached the chat as a status line and the store as nothing: `add_decision` ran
    only on the success path, so a reader of the store could not tell a proposal refused at
    Gate 2 from one never clicked, and a declined precaution lost the very `reply_log` entry
    (`confirmed: false`) built to prove it. Measured in the coverage matrix, cells 5 and 6.

    `noun` is "trade", "modify" or "cancel"; the row is `<noun>_refused`, or
    `<noun>_dispatched_unverified` when the write had already reached IBKR and what failed
    was the reporting — a different fact, named differently. No store or no session: the
    render sites allow both, and a refusal must still report and return.
    """
    kind = "dispatched_unverified" if dispatched else "refused"
    reason = _classify_execution_error(exc)
    _record_outcome(
        store,
        session_id,
        decision_type=f"{noun}_{kind}",
        summary=f"{kind.upper().replace('_', ' ')}: {_refusal_subject(noun, proposal)} — {reason}",
        symbol=symbol,
        metadata={
            "proposal": proposal,
            "stage": "dispatch" if dispatched else _refusal_stage(exc),
            "reason": reason,
            "ibkr_replies": reply_log,
            "dispatched": dispatched,
        },
    )


def _record_rejection(
    store: Any,
    session_id: str | None,
    *,
    noun: str,
    proposal: dict[str, Any],
    result: Any,
    symbol: str | None,
) -> None:
    """IBKR said no to a write the human had approved: `<noun>_rejected`, with IBKR's payload (#50)."""
    _record_outcome(
        store,
        session_id,
        decision_type=f"{noun}_rejected",
        summary=f"REJECTED BY IBKR: {_refusal_subject(noun, proposal)}",
        symbol=symbol,
        metadata={"proposal": proposal, "stage": "ibkr", "ibkr_response": result},
    )


def _log_refusal(exc: Exception, dispatched: bool, action: str, subject: Any) -> None:
    """A human's no is not an error: `<action> refused at <stage> for <subject>: …` at INFO,
    without a traceback; `<action> failed for <subject>` with one for a genuine failure
    (2026-09-11 — DO NOT SEND had logged as ERROR, then as "failed — refused")."""
    stage = _refusal_stage(exc)
    if dispatched or stage == "other":
        log.exception("%s failed for %s", action, subject)
    else:
        log.info("%s refused at %s for %s: %s", action, stage, subject, exc)


_CONID_LOOKUP = {
    # sec_type -> (tool that resolves it, why a bare symbol is not enough for THIS type)
    "OPT": ("get_option_chain", "expiry, strike and call/put are all needed"),
    "FOP": ("get_option_chain", "expiry, strike and call/put are all needed"),
    "CASH": ("get_market_snapshot", "an FX pair must be given as BASE.QUOTE, e.g. EUR.USD"),
}
"""sec_type -> (resolving tool, the reason specific to that type).

The reasons are per-type on purpose. A single shared justification read plausibly but was
false for CASH — an FX pair is not a company with rival listings, it is a pair that has to
be named as one — and a *mostly* true explanation is the failure mode this whole guard
exists to remove.

`get_currency_pairs` is deliberately not named for CASH even though resolution goes through
that endpoint: it is an `IBKRClient` method, **not** a declared tool, so naming it would
send the model after something it cannot call. Checked against `TOOL_DEFINITIONS` rather
than assumed — naming a nonexistent tool is the same class of error as resolving a
nonexistent listing.
"""

_CONID_LOOKUP_DEFAULT = (
    "get_market_snapshot",
    "IBKR assigns a distinct conid per listing and currency, and one ticker can belong to "
    "more than one company",
)
"""STK/IND/BOND. `get_market_snapshot` routes through ibkr_core_mcp's
`_resolve_snapshot_conid`, the authoritative symbol->conid implementation.

The reason is stated universally, with no ticker named. The evidence behind it — IGV
resolving to both a US ETF and an Italian company, VOD to Vodafone Group and Vodacom — is
in `_needs_conid_text`'s docstring, which is where it belongs: a message that cites one
instrument reads as a rule about that instrument.
"""


def _needs_conid_text(sec_type: str, symbol: str) -> str:
    """Refuse a placement whose contract identity was never established. Names the fix.

    ## Why this is a refusal and not a lookup

    Until 2026-08-05 this branch called `IBKRClient.search_contract(symbol)` and took
    `contracts[0]["conid"]`. That is `/iserver/secdef/search`, and ibkr_core_mcp documents
    the exact pattern as a defect it already removed from every read path
    (`ClaudeToolkit._resolve_stock_conid`): the endpoint returns neither `isUS` nor a
    currency, **its result order is not documented as meaningful**, and `contracts[0]` for
    IGV is the *Mexican* listing — "Right by luck, wrong by luck". IBKR assigns a distinct
    conid per product *and currency*, and the same ticker can be a different company
    outright (IGV is also I GRANDI VIAGGI SPA; VOD is Vodafone Group and Vodacom Group).

    A wrong listing does not look wrong: it is a plausible price for the wrong instrument,
    in the wrong currency, bought with real money. So the money path must not guess.

    ## Why refusing costs nothing

    The authoritative resolver is already in the loop one turn earlier — `get_market_snapshot`
    and `preview_order` both route through `_resolve_snapshot_conid`, and that is where the
    model gets the conid it puts in the proposal. Measured over this account's whole order
    history on 2026-08-05, across the 18 `trade_proposed`/`trade_staged` rows: 16 carried a
    conid, and the 2 that did not are a single order proposed and staged on 2026-07-06.
    **On and after 2026-07-10 it is 16 of 16 — no placement has gone without one.** This
    guard therefore makes mandatory what production already does, rather than adding a step.
    Re-implementing the resolution rule here was rejected for the opposite reason: it would
    put a second, drifting definition of symbol resolution next to the authoritative one.

    FUT is the one type still resolved here, by front month via `get_futures` — unambiguous
    by construction, and unchanged.

    Args:
        sec_type: The proposal's security type, already upper-cased.
        symbol: The proposal's symbol, echoed back so the model can act without re-deriving it.

    Returns:
        Markdown naming the type, the reason, and the exact tool to call — the model has to
        be able to fix this in one turn, or the guard just becomes a dead end.
    """
    tool, reason = _CONID_LOOKUP.get(sec_type, _CONID_LOOKUP_DEFAULT)
    return (
        f"**{sec_type} orders require a pre-resolved contract ID.** A symbol alone does not "
        f"identify a contract here — {reason} — so resolving **{symbol}** at this point "
        f"would risk trading the wrong instrument.\n\n"
        f"Ask ClaudIA to look up **{symbol}** with `{tool}`, then re-issue the proposal with "
        f"the `conid` field set. **Order not placed.**"
    )


def _resolve_account_id(accounts: list[dict[str, Any]]) -> str:
    """Extract an account ID from IBKRClient.get_accounts()'s response.

    IBKR's account objects have used different key names (accountId/acctId/id)
    across endpoints/API versions — try each in turn. Empty string if no accounts.
    """
    if not accounts:
        return ""
    account = accounts[0]
    return str(account.get("accountId", account.get("acctId", account.get("id", ""))))


def _is_ibkr_rejection(result: object) -> bool:
    """True when an order-endpoint response is an IBKR rejection payload.

    ## Role after the L2 read-back (2026-07-27)

    This classifier no longer authorises a success claim — only `_read_back` does that,
    and only from an observed order state. What it still does, and nothing else can, is
    detect a *dispatch that never became an order*. Its evidence is the POST body's own
    error text ("Can not contain field # 8089"), and that text is unrecoverable
    afterwards: a rejected order has no order id, so there is nothing to read back. Drop
    this and a rejection would degrade into "dispatch accepted, could not verify" — which
    would be strictly worse, implying acceptance where IBKR explicitly refused and
    discarding the reason. The two are complementary evidence sources, not duplicates:
    a rejection payload is positive evidence of failure; a non-rejection payload is only
    the absence of evidence of failure, which is no longer sufficient to claim anything.

    IBKR returns order rejections as an HTTP 200 payload — no exception raised —
    proven live 2026-07-23 on a FUT order (see
    docs/plans/2026-07-23-futures-order-field-8089-bug.md). The rejection entry carries
    ``"action": "order_submit_issue"``, an ``"error"`` string, and
    ``order_id: "0"`` inside ``cqe.post_payload``. Historically, without this
    classification the callers labelled such a rejection "staged successfully" — that
    wording is gone from every path, but the rejection still has to be named as one.

    Accepts both response shapes: place_order_and_confirm() returns a list of
    dicts; modify_order_and_confirm() and cancel_order() return a single dict.

    Rejection markers (any one ⇒ rejected):
      - an entry with ``action == "order_submit_issue"``
      - an entry with a non-empty ``error`` value
      - no entry carries ``order_status`` AND the last-seen order id across
        entries is "0"/0/missing (last-write-wins — the reply-chain terminal
        entry is last, so it is the authoritative one)
    Success shapes (live-verified): a non-zero ``order_id``/``orderId`` (both key
    spellings occur across IBKR responses), or an ``order_status`` (e.g.
    ``"Submitted"``) on a terminal reply-chain entry.
    """
    entries = result if isinstance(result, list) else [result]
    has_order_status = False
    order_id: object = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("action") == "order_submit_issue":
            return True
        if entry.get("error"):
            return True
        if entry.get("order_status") is not None:
            has_order_status = True
        for key in ("order_id", "orderId"):
            if entry.get(key) is not None:
                order_id = entry[key]
    return not has_order_status and order_id in ("0", 0, None)


# ── L2 — post-dispatch read-back ─────────────────────────────────────────────
#
# The rule this section exists to enforce: evidence is the only source of truth for
# orders, no assumptions. A dispatch response proves the request was received and
# nothing more. IBKR says so itself for cancels — the {"msg": "Request was submitted"}
# body "indicates our request to cancel order 987654 was received, but not that the
# order ticket itself has been canceled".
#
# Sources (verified 2026-07-27):
#   https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md
#   https://ibkrcampus.com/docs/web-api/web-api-v-1-0-documentation/endpoints/order-monitoring/order-status-value.md
#   https://ibkrcampus.com/docs/web-api/web-api-v-1-0-documentation/endpoints/order-monitoring/order-status.md

_READBACK_DELAY_S = 2.0
"""Single fixed delay before the confirming read. Above client.py's 1 s subscription
warmup, short enough not to stall the human after a live money action. A latency choice,
not a correctness one — whatever is observed is reported honestly, including a pending
state. Deliberately NOT a poll loop: a retry state machine is complexity that can itself
fail (user direction 2026-07-27).

Still 2.0 s after the placement path started leading with get_live_orders (2026-07-28).
That endpoint's documented two-call warmup — `?force=true`, `time.sleep(1)`, then the data
call — happens *inside* `IBKRClient.get_live_orders`, so it consumes none of this budget
and instead adds ~1 s after it: the live book is read ≈3 s post-dispatch, strictly later
than the 2 s the status read had. Raising this constant would only stall the human further
for no extra evidence."""

_CONFIRMED = {
    # "accepted and is working at the destination" (Submitted), "accepted by the system
    # ... yet to be elected" (PreSubmitted), "completely filled" (Filled). PendingSubmit
    # is excluded by its own definition: "transmitted your order, but have not yet
    # received confirmation that it has been accepted by the order destination".
    "place": frozenset({"Submitted", "PreSubmitted", "Filled"}),
    "modify": frozenset({"Submitted", "PreSubmitted", "Filled"}),
    # "Indicates that the balance of your order has been confirmed canceled by the
    # system" — the only documented value that is evidence of a cancellation.
    # "ApiCancelled" is deliberately absent: client.py lists it in _TERMINAL_STATUSES for
    # filtering the *live-orders* feed, but it is not a documented value of this
    # endpoint's order_status field, and an undocumented state must never be treated as
    # proof (never invent an order state).
    "cancel": frozenset({"Cancelled"}),
}

_PENDING_CANCEL = ("PendingCancel", "PreCancelled")
"""PendingCancel: "sent a request to cancel the order but have not yet received cancel
confirmation from the order destination". PreCancelled: "a cancellation request has been
accepted by the system but ... the request is not being recognized". Neither is a
cancellation, and IBKR warns an execution can still arrive while one is pending."""


def _extract_order_id(result: object) -> str | None:
    """The order id from a dispatch response, or None when there is not a usable one.

    Accepts both response shapes (place returns list[dict]; modify/cancel return a bare
    dict) and both key spellings, which vary across IBKR responses. Last-write-wins
    across entries, matching `_is_ibkr_rejection`'s reasoning: the reply-chain terminal
    entry is last, so it is the authoritative one.

    Returns a `str` because `IBKRClient.get_order_status` validates its argument against
    a numeric-string pattern. `"0"` is IBKR's not-an-order placeholder (it is what a
    rejection carries), so it is treated as no id — never as something to read back.
    """
    entries = result if isinstance(result, list) else [result]
    found: str | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("order_id", "orderId"):
            value = entry.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and text != "0":
                found = text
    return found


async def _read_back(
    ibkr: Any, order_id: str, action: str
) -> tuple[bool, str, dict[str, Any] | None]:
    """Wait, then observe the order's state via get_order_status. See `_read_order_status`.

    The evidence rule for **cancel** and **modify**, and the fall-through for **place**
    (which leads with the live book — see `_read_back_place`).

    Absence from get_live_orders is NOT evidence: _TERMINAL_STATUSES filters Cancelled
    out, so a cancelled order and one that never existed look identical there. That is why
    a cancellation is only ever confirmed by this endpoint reporting `Cancelled`, and why
    disappearance from the live book proves nothing about one.

    Runs on the event loop that the dispatch already blocked (Known Gap #15); this adds
    one ~2 s call, not a loop.
    """
    await asyncio.sleep(_READBACK_DELAY_S)
    return await _read_order_status(ibkr, order_id, action)


async def _read_order_status(
    ibkr: Any, order_id: str, action: str
) -> tuple[bool, str, dict[str, Any] | None]:
    """Read the per-order status endpoint. Returns (confirmed, human line, status dict).

    No wait of its own — callers own the settle delay, so a path that has already waited
    (the place read-back, which leads with the live book) does not wait twice.

    The third element is the raw observed status dict (None when nothing was observed),
    so callers can record what was seen and — for modify — compare the returned fields
    against what was requested, without spending a second live call.

    Every failure path returns confirmed=False. A read that fails is an absence of
    evidence and can never be upgraded into a confirmation — notably the documented 503,
    which IBKR returns by design for orders cancelled or filled before the active
    session, and for FA/linked accounts without an account switch.

    The read goes through asyncio.to_thread because IBKRClient is synchronous.
    """
    try:
        status = await asyncio.to_thread(ibkr.get_order_status, order_id)
    except Exception as exc:
        log.warning("Read-back failed for order %s: %s", order_id, exc)
        return (
            False,
            (
                f"⚠️ Dispatch accepted, but the live state of order {order_id} **could not be "
                f"verified** ({exc}). Do not assume this order is working — check IBKR directly."
            ),
            None,
        )

    if not isinstance(status, dict):
        log.warning("Read-back for order %s returned %r", order_id, type(status).__name__)
        return (
            False,
            (
                f"⚠️ The read-back for order {order_id} returned an unexpected shape "
                f"({type(status).__name__}) — **not confirmed**. Check IBKR directly."
            ),
            None,
        )

    state = status.get("order_status") or "unknown"
    desc = status.get("order_status_description", "")
    # .get, not [], so an unknown action can never confirm — the fail-safe direction.
    if state in _CONFIRMED.get(action, frozenset()):
        return True, f"✅ Verified via get_order_status: **{state}** ({desc})", status
    if state in _PENDING_CANCEL:
        return (
            False,
            (
                f"⚠️ Order {order_id} is **{state}** — the cancel is **not confirmed**. "
                "IBKR documents that you may still receive an execution while a cancellation "
                "request is pending."
            ),
            status,
        )
    return False, f"⚠️ Order {order_id} reads **{state}** ({desc}) — not confirmed.", status


async def _live_book_presence(ibkr: Any, order_id: str) -> tuple[str, dict[str, Any] | None, str]:
    """Look for `order_id` in the live order book. Returns (verdict, row, detail).

    verdict is one of:
      "present"     — the order is in the book; `row` is its entry. Positive evidence.
      "absent"      — the book was read and does not contain it. **Not evidence.**
      "unavailable" — the book could not be read at all; `detail` says why. Not evidence
                      either, and specifically not an absence: the call that failed on
                      2026-07-27 (two HTTP 500s) is this one, and rendering that failure
                      as "not in the book" is the defect this module exists to prevent.

    Matched on the order id alone, in both spellings IBKR uses across order responses
    (see `_extract_order_id`) and compared as trimmed strings because ids arrive as both
    ints and strings. "Some order is in the book" is not evidence about this one.

    `get_live_orders` performs its own documented two-call subscription warmup
    (?force=true, a 1 s sleep, then the data call), so this costs ~1 s plus two round
    trips on top of the caller's settle delay. Synchronous client, hence to_thread.

    Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/live-orders.md
    """
    try:
        orders = await asyncio.to_thread(ibkr.get_live_orders)
    except Exception as exc:
        log.warning("Live-order check failed for order %s: %s", order_id, exc)
        return "unavailable", None, str(exc)
    if not isinstance(orders, list):
        log.warning("Live-order check returned %r", type(orders).__name__)
        return "unavailable", None, f"unexpected shape ({type(orders).__name__})"
    wanted = str(order_id).strip()
    for entry in orders:
        if not isinstance(entry, dict):
            continue
        for key in ("orderId", "order_id"):
            value = entry.get(key)
            if value is not None and str(value).strip() == wanted:
                return "present", entry, ""
    return "absent", None, ""


_LIVE_BOOK_ABSENCE_NOTE = (
    "🔎 Order {order_id} is **not in the live order book**. That is **NOT evidence** "
    "either way: get_live_orders filters out Filled, Cancelled, ApiCancelled and Expired "
    "orders, so a fully filled order is absent from it too. Checking the per-order status "
    "endpoint…"
)
"""Absence is the one outcome that proves nothing — it must never read as a failure."""

_LIVE_BOOK_UNAVAILABLE_NOTE = (
    "⚠️ The live order book could not be read ({detail}) — **no evidence either way**, and "
    "specifically not an absence. Checking the per-order status endpoint…"
)
"""The 2026-07-27 shape: get_live_orders 500s. A failed lookup is not a missing order."""


async def _read_back_place(ibkr: Any, order_id: str) -> tuple[bool, str, dict[str, Any] | None]:
    """Validate a placement by presence in the live order book. Returns `_read_back`'s tuple.

    User rule, 2026-07-27: "each action must be validated by evidence: when placing an
    order, check live orders to validate its presence."

    The asymmetry that shapes this, from `IBKRClient.get_live_orders` filtering
    `_TERMINAL_STATUSES` (Filled, Cancelled, ApiCancelled, Expired):

      | resting order      | present | ✅ the strongest positive evidence available |
      | immediately filled | absent  | absence is EXPECTED here, not a failure      |
      | cancelled          | absent  | indistinguishable from never-existed         |

    **Presence is proof; absence never is.** So the book is checked first and a match
    confirms outright; anything else falls through to the per-order status endpoint, which
    is what distinguishes a fill from an order that never made it. Absence alone is
    reported as neither success nor failure — that is the whole point of the fall-through.

    Presence proves the order *exists*. Whether it is *working* is a separate question,
    answered by the row's own status against `_CONFIRMED["place"]` — a `PendingSubmit` row
    is in the book but is not a working order, and both facts are stated, because either
    one alone misleads.

    Cost: one settle delay, then get_live_orders (~1 s of internal warmup + two round
    trips), then at most one status read. No poll loop, no retry state machine (user
    direction 2026-07-27). It runs on the event loop the dispatch already blocked
    (Known Gap #15).

    Returns:
        (confirmed, human-readable line, observed dict). On presence the observed dict is
        `{"order_status": ..., "readback_source": "get_live_orders", "live_order": <row>}`
        — the status copied under the key every caller already records, with its provenance
        and the untouched row alongside it.
    """
    await asyncio.sleep(_READBACK_DELAY_S)
    verdict, row, detail = await _live_book_presence(ibkr, order_id)

    if verdict == "present" and row is not None:
        state = str(row.get("status") or "").strip() or "unknown"
        observed = {
            "order_status": state,
            "readback_source": "get_live_orders",
            "live_order": row,
        }
        if state in _CONFIRMED["place"]:
            return (
                True,
                (
                    f"✅ Verified via get_live_orders: order {order_id} is **present in the "
                    f"live order book** — **{state}**. The order exists at IBKR and is working."
                ),
                observed,
            )
        return (
            False,
            (
                f"⚠️ Order {order_id} is **present in the live order book** — so it exists at "
                f"IBKR — but reads **{state}**, which is not a working order: **not "
                f"confirmed**. Check IBKR directly."
            ),
            observed,
        )

    note = (
        _LIVE_BOOK_ABSENCE_NOTE.format(order_id=order_id)
        if verdict == "absent"
        else _LIVE_BOOK_UNAVAILABLE_NOTE.format(detail=detail)
    )
    confirmed, line, status = await _read_order_status(ibkr, order_id, "place")
    return confirmed, f"{note}\n{line}", status


_MODIFY_READBACK_FIELDS = (
    # (key in the dispatched order body, field in the read-back, human label)
    # `total_size` is "Total quantity of the order"; `size` is only "Remainder of order
    # to be filled", so a partial fill would make `size` disagree with a correctly
    # applied modify. Compare against total_size.
    ("quantity", "total_size", "quantity"),
    ("orderType", "order_type", "order type"),
    ("tif", "tif", "time in force"),
    ("side", "side", "side"),
    # Measured 2026-09-04: the status of a STOCK order carries `outside_rth` (snake_case,
    # bool); the status of a FUTURES order carries no such key at all. Present → compared;
    # absent → skipped here and caveated below, never treated as agreement.
    ("outsideRTH", "outside_rth", "outside RTH"),
)
"""The fields IBKR's order-status response exposes discretely AND that a modify sets.

Prices are not in this tuple because which status field holds them depends on the order
type — see `_price_readback_fields`. Until 2026-09-04 they were "absent on purpose": the
*documented* response has no limit/stop price field (`average_price` is "the average price
of execution"). The live response does — `limit_price` on a limit order, `stop_price` on a
stop, measured on three resting orders — so a price is now compared when the field is
present and caveated only when it is not. `outsideRTH` is likewise only sometimes observable
(see the tuple's comment); an absent field never counts as agreement."""


def _price_readback_fields(order_body: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """The (body key, status field, label) price pairs a modify of this order type sets.

    Measured 2026-09-04 on the live account: the order-status response carries
    `limit_price` for a limit order (`'150.00'`, `'7660.00'`) and `stop_price` for a stop
    (`'7732.00'`, with `limit_price` `''`) — neither is in IBKR's documented response, like
    `outside_rth`. A STOP_LIMIT sets both (`price` = limit, `auxPrice` = stop).
    """
    otype = str(order_body.get("orderType", "")).upper()
    if otype == "LMT":
        return (("price", "limit_price", "limit price"),)
    if otype == "STP":
        return (("price", "stop_price", "stop price"),)
    if otype == "STOP_LIMIT":
        return (("price", "limit_price", "limit price"), ("auxPrice", "stop_price", "stop price"))
    return ()


# IBKR answers with a different vocabulary than it accepts, and not even one of its own:
# for the SAME order on 2026-08-05, `get_order_status` returned side "B" / orderType
# "LIMIT" while `get_live_orders` returned "BUY" / "Limit", and IBKR's OpenAPI spec
# (https://api.ibkr.com/gw/api/v3/api-docs) documents `orderStatus.side` as
# enum ['BUY','SELL'] — which the live response does not honour.
#
# Only measured equivalences live here. Anything absent still fails loud, which is the
# safe direction: a false alarm costs a second look, a silently-accepted difference costs
# a wrong order state. Add a pair when it has been *observed*, not when it seems obvious —
# stop and market types are deliberately not guessed at.
_FIELD_SYNONYMS: dict[str, str] = {
    "b": "buy",  # measured: get_order_status returns "B" for a BUY
    "s": "sell",  # the symmetric counterpart of the above, not separately measured
    "limit": "lmt",  # measured: "LIMIT" read back for a request of "LMT"
}


def _canonical(value: object) -> str:
    """A field value reduced to its canonical spelling for comparison."""
    text = str(value).strip().casefold()
    return _FIELD_SYNONYMS.get(text, text)


def _values_match(requested: object, observed: object) -> bool:
    """Compare a requested order field against IBKR's read-back of it.

    Numeric first (IBKR may return "3.0" where the request had 3), then a comparison that
    folds case *and* IBKR's own synonyms. Both directions of failure matter: a comparison
    that cries mismatch on a vocabulary difference trains the user to ignore the warning,
    and one that shrugs off a real difference defeats the read-back.

    The first of those is not hypothetical — it shipped. A live modify on 2026-08-05
    (order 314390101, limit 50 → 100) applied perfectly and was reported to the user as
    **"the modification is not confirmed"**, on the strength of `LMT` vs `LIMIT` and
    `BUY` vs `B`. A guardrail that fires on every successful modify is training data for
    ignoring it.
    """
    try:
        return float(requested) == float(observed)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _canonical(requested) == _canonical(observed)


def _compare_modify_readback(
    order_body: dict[str, Any], status: dict[str, Any]
) -> tuple[bool, str]:
    """Check the read-back's fields against the modify that was requested.

    A modify that silently did not apply still reads "Submitted" — the status proves the
    order exists, not that the change landed. Returns (fields_agree, line).

    Three outcomes, deliberately distinct:
      - any comparable field disagrees → not confirmed, naming the field and both values
      - all comparable fields agree, at least one was comparable → confirmed
      - nothing was comparable → not confirmed ("no comparable fields"), because
        agreement was never observed. Absent fields alone never *cause* a mismatch, so a
        sparse response cannot manufacture a false alarm.

    A price in the request is compared against the status's `limit_price` / `stop_price`
    (`_price_readback_fields`); only when the response lacks that field does the line carry
    the explicit "could not be verified" caveat, with IBKR's own `order_description` quoted
    so the human can read the resting price themselves.
    """
    matched: list[str] = []
    mismatched: list[str] = []
    unverified_prices: list[str] = []
    price_fields = _price_readback_fields(order_body)
    for req_key, obs_key, label in (*_MODIFY_READBACK_FIELDS, *price_fields):
        if req_key not in order_body:
            continue
        observed_value = status.get(obs_key)
        if (req_key, obs_key, label) in price_fields and (
            observed_value is None or not str(observed_value).strip()
        ):
            unverified_prices.append(label)
            continue
        # A missing or blank read-back field is an absence of information, not a
        # disagreement — reporting it as a mismatch would be a false alarm, and false
        # alarms are what teach a user to ignore the real ones. (An explicit 0 is a
        # value, not a blank, so it is still compared.)
        if observed_value is None or not str(observed_value).strip():
            continue
        if req_key == "outsideRTH" and not isinstance(observed_value, bool):
            continue  # only a real boolean is a claim about the attribute
        if _values_match(order_body[req_key], observed_value):
            matched.append(f"{label} {order_body[req_key]}")
        else:
            mismatched.append(
                f"{label}: requested {order_body[req_key]!r}, IBKR reports {observed_value!r}"
            )

    if mismatched:
        line = (
            "⚠️ The read-back **does NOT match the request** — the modification is "
            "**not confirmed**: " + "; ".join(mismatched) + "."
        )
        agree = False
    elif matched:
        line = "✅ Read-back matches the request: " + ", ".join(matched) + "."
        agree = True
    else:
        line = (
            "⚠️ The read-back carried **no comparable fields**, so the modification is "
            "**not confirmed** — nothing was observed to compare against the request."
        )
        agree = False

    if "outsideRTH" in order_body and not isinstance(status.get("outside_rth"), bool):
        line += (
            "\n⚠️ **Outside RTH could not be verified**: IBKR's order-status response "
            "does not report the attribute for this order (measured 2026-09-04: present "
            "once set, absent on an order that never had it). The request carried "
            f"outsideRTH={order_body['outsideRTH']!r}; read it in IBKR's own order ticket."
        )

    if unverified_prices:
        description = status.get("order_description")
        evidence = (
            f"IBKR's own description of the order now resting: `{description}` — read the "
            "price there."
            if description
            else "and the response carried no `order_description` to read it from."
        )
        line += (
            f"\n⚠️ The **{' and '.join(unverified_prices)} could not be verified**: IBKR's "
            "order-status response did not carry the field for this order (it usually does "
            "— `limit_price` / `stop_price`, measured 2026-09-04 — but the documented shape "
            f"has neither). {evidence}"
        )
    return agree, line


async def _execute_staged_order_core(
    proposal: dict[str, Any],
    send_status: SendStatus,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Framework-agnostic core of the staged-order flow — the full Gate 1/Gate 2 spec is
    in this module's docstring. Called with an already-parsed proposal dict and a
    send_status callback supplied by the UI layer."""
    symbol = proposal.get("symbol", "?")
    action_str = proposal.get("action", "?")
    qty = proposal.get("quantity", 0)
    otype = proposal.get("order_type", "MKT")
    limit_price = proposal.get("limit_price")
    sec_type = proposal.get("sec_type", "STK").upper()
    dispatched = False  # flips once the IBKR write has returned — see the except block

    await send_status(
        (
            f"Initiating staging for **{action_str} {qty} {symbol}** ({sec_type})…\n\n"
            f"**Gate 1 — Touch ID:** A macOS authentication prompt will appear. "
            f"Use Touch ID or your system password if prompted.\n\n"
            f"**Gate 2 — Confirmation dialog:** A separate window will appear on your desktop "
            f"with full order details and a **SEND TO IBKR** button. "
            f"You have 60 seconds to confirm or it auto-cancels."
        ),
        "System",
    )

    reply_log: list[dict[str, Any]] = []
    try:
        from dotenv import load_dotenv
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient

        load_dotenv(override=False)
        config = Config.from_env()
        ibkr = IBKRClient(
            config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome"))
        )

        # Resolve conid — routing depends on sec_type and optional conid override.
        # Only two routes reach an order body: a conid the caller pre-resolved, or the
        # front-month lookup for FUT. Everything else is refused — see `_needs_conid_text`.
        multiplier: float | None = None
        override_conid = proposal.get("conid")
        if override_conid is not None:
            # Pre-resolved conid (required for FOP; valid for any instrument).
            # ClaudIA resolves options chain in conversation and embeds the conid.
            conid = int(override_conid)
            company_name = proposal.get("_companyName", "")
        elif sec_type == "FUT":
            futures = ibkr.get_futures([symbol])
            if not futures:
                await send_status(
                    f"Could not find futures contracts for {symbol}. Order not placed.",
                    "System",
                )
                return
            try:
                contract = min(futures, key=lambda f: int(f.get("expirationDate") or 0))
            except (ValueError, TypeError):
                contract = futures[0]
            # conid is IBKR's mandatory contract identifier — always present on a successful
            # get_futures() lookup (the `if not futures` guard above already handles the
            # no-match case). Not user/LLM-supplied, so order-parameter-immutability doesn't
            # apply here — this is IBKR's own response data.
            conid = int(contract.get("conid"))  # type: ignore[arg-type]
            company_name = contract.get("contractDesc", contract.get("description", ""))
            # No multiplier read here: /trsrv/futures rows carry none (measured 2026-09-04;
            # the read that used to sit here had never fired live). It comes from contract
            # info below, on every futures path.
        else:
            await send_status(_needs_conid_text(sec_type, symbol), "System")
            return

        currency: str | None = None
        if sec_type in ("FUT", "FOP"):
            # Every futures path, conid-supplied or resolved: the multiplier is what turns
            # a price into a notional on the Gate 2 screen, and the label carries the
            # contract month the proposal text cannot show. Live 2026-09-04 (conid path):
            # the dialog printed 7,735.00 for one ES contract worth 386,750 USD.
            multiplier, currency, contract_label = _futures_contract_facts(ibkr, conid)
            if contract_label:
                company_name = contract_label
        else:
            # A stock's price IS money: its listing's name and currency reach Gate 2 too
            # (gap #49, 2026-09-11). A name the proposal already carries is kept.
            name, currency = (
                _stock_contract_facts(ibkr, int(conid)) if conid is not None else (None, None)
            )
            if name and not company_name:
                company_name = name

        claudia_ref = f"CLAUDIA-{int(time.time() * 1000)}"
        tif = (
            proposal.get("tif")
            or proposal.get("time_in_force")
            or proposal.get("timeInForce")
            or "DAY"
        ).upper()

        # ----------------------------------------------------------------
        # Order body — field spec from IBKR CP API docs (2026-07-02)
        # Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/place-order.md
        #
        # Field          Type     Req?       Notes
        # -------------- -------- ---------- ---------------------------------
        # conid          int      yes*       *or conidex; SMART-routes when set
        # orderType      str      yes        LMT | MKT | STP | STOP_LIMIT | MIDPRICE | TRAIL | TRAILLMT
        # side           str      yes        "BUY" | "SELL"
        # tif            str      yes        DAY | GTC | OPG | IOC | PAX(crypto)
        # quantity       float*   yes*       *docs say float; example uses int; whole shares only
        # price          float    LMT/STOP_LIMIT  limit price
        # auxPrice       float    STOP_LIMIT/TRAILLMT  stop price
        # acctId         str      no         defaults to first account if omitted
        # ticker         str      no         underlying symbol — valid IBKR field (not stripped)
        # cOID           str      no         customer order ID; max 64 chars; unique per 24h
        # listingExchange str     no         default: SMART routing
        # outsideRTH     bool     no         allow execution outside regular trading hours
        # manualIndicator bool    FUT/FOP*   CME Rule 536-B compliance (required since May 1 2025)
        # extOperator    str      FUT/FOP*   NOT sent — docs mark it "Required*" for 536-B, but
        #                                    IBKR rejects any non-empty value as undocumented
        #                                    field 8089 on this account class (the "Required*"
        #                                    evidently scopes to institutional/multi-operator
        #                                    setups). Proven via whatif isolation 2026-07-23:
        #                                    docs/plans/2026-07-23-futures-order-field-8089-bug.md
        # Source (536-B): https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/
        # ----------------------------------------------------------------
        order_body: dict[str, Any] = {
            "conid": conid,  # int
            "orderType": otype,  # str
            "side": action_str,  # str: BUY | SELL
            "tif": tif,  # str: DAY | GTC | OPG | IOC
            "quantity": int(qty),  # int (docs say float, example uses int)
            "ticker": symbol,  # str — display + valid IBKR field
            "acctId": "",  # filled below after account lookup
            "cOID": claudia_ref,  # str — max 64 chars
            "_companyName": company_name,  # display only — underscore prefix → stripped
        }
        if sec_type in ("FUT", "FOP"):
            # CME Group Rule 536-B — manualIndicator=True: order submitted through a
            # manual UI (not automated). extOperator is deliberately NOT sent: IBKR
            # rejects it with any non-empty value as undocumented field 8089 on
            # this account class — proven via whatif isolation 2026-07-23
            # (manualIndicator alone is accepted); see
            # docs/plans/2026-07-23-futures-order-field-8089-bug.md
            order_body["manualIndicator"] = True
            if multiplier is not None:
                order_body["_multiplier"] = multiplier  # display only — stripped by client.py
            else:
                # Gate 2 must not fall back to price x qty on a future: that is not an
                # estimate, it is the notional divided by the multiplier.
                order_body["_multiplier_unknown"] = True
            if currency:
                order_body["_currency"] = currency  # display only — ISO code, never "$"
        elif currency:
            order_body["_currency"] = currency  # display only — a stock's price is money (#49)
        if otype == "LMT" and limit_price is not None:
            order_body["price"] = float(limit_price)  # float
        elif otype == "STP" and proposal.get("stop_price") is not None:
            order_body["price"] = float(proposal["stop_price"])  # float (STP uses price field)
        elif otype == "STOP_LIMIT":
            if limit_price is not None:
                order_body["price"] = float(limit_price)
            if proposal.get("stop_price") is not None:
                order_body["auxPrice"] = float(proposal["stop_price"])
        _apply_outside_rth(order_body, proposal)

        # Gate 1 (Touch ID) + Gate 2 (AppKit colored dialog) fire inside place_order()
        accounts = ibkr.get_accounts()
        account_id = _resolve_account_id(accounts)
        order_body["acctId"] = account_id
        log.info(
            "Placing order: %s", {k: v for k, v in order_body.items() if not k.startswith("_")}
        )
        result = ibkr.place_order_and_confirm(account_id, order_body, reply_log=reply_log)
        dispatched = True
        reply_line = _format_reply_log(reply_log)
        if reply_line:
            await send_status(reply_line, "System")

        # IBKR returns rejections as HTTP 200 payloads — no exception — so the
        # result must be classified before claiming success (proven live 2026-07-23;
        # see _is_ibkr_rejection and docs/plans/2026-07-23-futures-order-field-8089-bug.md).
        if _is_ibkr_rejection(result):
            log.warning("IBKR rejected order for %s: %s", symbol, result)
            _record_rejection(
                store, session_id, noun="trade", proposal=proposal, result=result, symbol=symbol
            )
            await send_status(
                (
                    f"**Order REJECTED by IBKR (not placed):** {action_str} {qty} {symbol} ({otype})\n"
                    f"IBKR response: {json.dumps(result, indent=2)}"
                ),
                "System",
            )
            # No decision logged — matches the other failure paths in this module.
            return

        # Everything below reports only what is known or observed. The dispatch being
        # accepted is known; that the order is working is not, until it is read back.
        ibkr_order_id = _extract_order_id(result)
        response_json = json.dumps(result, indent=2)

        if ibkr_order_id is None:
            # A placement that yields no order id is unverifiable. Say so — never
            # silence, and never a claim of success.
            confirmed, observed = False, None
            await send_status(
                (
                    f"**Dispatch accepted by IBKR** — {action_str} {qty} {symbol} ({otype}) — "
                    f"but the response carried no order id, so the live state of this order "
                    f"**could not be verified**. Do not assume it is working — check IBKR "
                    f"directly.\nIBKR response: {response_json}"
                ),
                "System",
            )
        else:
            await send_status(
                (
                    f"**Dispatch accepted by IBKR — order {ibkr_order_id}** "
                    f"({action_str} {qty} {symbol}, {otype}). Verifying live state…\n"
                    f"IBKR response: {response_json}"
                ),
                "System",
            )
            confirmed, readback_line, observed = await _read_back_place(ibkr, ibkr_order_id)
            await send_status(readback_line, "System")

        if store and session_id:
            # The dispatch happened either way, so the decision row is always written —
            # it records the state that was observed, not the one that was hoped for.
            observed_state = (observed or {}).get("order_status")
            store.add_decision(
                session_id=session_id,
                decision_type="trade_staged",
                summary_text=(
                    f"{'STAGED' if confirmed else 'DISPATCHED (UNVERIFIED)'}: "
                    f"{action_str} {qty} {symbol} ({otype}) — "
                    f"observed state: {observed_state or 'not observed'}"
                ),
                symbol=symbol,
                metadata={
                    "proposal": proposal,
                    "ibkr_response": result,
                    "ibkr_replies": reply_log,
                    "ibkr_order_id": ibkr_order_id,
                    "claudia_ref": claudia_ref,
                    "readback_confirmed": confirmed,
                    "readback_order_status": observed_state,
                    "readback_response": observed,
                },
            )

    except Exception as exc:
        _log_refusal(exc, dispatched, "Order staging", symbol)
        _record_refusal(
            store,
            session_id,
            noun="trade",
            proposal=proposal,
            exc=exc,
            reply_log=reply_log,
            dispatched=dispatched,
            symbol=symbol,
        )
        if dispatched:
            await send_status(_post_dispatch_failure_text(exc, "order"), "System")
        else:
            await send_status(
                f"**Order not placed:** {_classify_execution_error(exc)}"
                f"{_declined_reply_text(reply_log)}",
                "System",
            )


# ── Order cancellation ───────────────────────────────────────────────────────


def _format_cancel_summary(proposal: dict[str, Any], contract_label: str | None = None) -> str:
    """Build the human-approval text for cancelling a live order.

    Same safety-surface role as `_format_order_summary`. Keys: `order_id` (the order being
    cancelled) plus display-only context — `symbol`, `action`, `quantity`, `order_type`,
    `limit_price` / `stop_price`, `tif` (this path reads only the `tif` spelling), `reason`.

    Args:
        proposal: Schema-checked cancel-proposal dict.
        contract_label: The resolved contract's label from
            `cancel_proposal_contract_label`, or None for no line. A cancel proposal
            carries no `sec_type`, so the resolver decides whether there is a contract
            to name; this function only renders what it is given.

    Returns:
        Markdown for the proposal message.
    """
    order_id = proposal.get("order_id", "?")
    symbol = proposal.get("symbol", "?")
    action = proposal.get("action", "?")
    qty = proposal.get("quantity", "?")
    otype = proposal.get("order_type", "MKT")
    limit = proposal.get("limit_price")
    stop = proposal.get("stop_price")
    tif = (proposal.get("tif") or "DAY").upper()
    reason = proposal.get("reason", "")

    price_str = _price_suffix(otype, limit, stop)

    lines = [
        f"**CANCEL order {order_id}: {action} {qty} {symbol}** ({otype}{price_str}, {tif})",
    ]
    if contract_label:
        lines.append(f"**Contract:** {contract_label}")
    if reason:
        lines.append(f"*Reason:* {reason}")
    lines.append(
        "⚠️ **Clicking 'Cancel this order' will initiate IBKR confirmation "
        "(Touch ID + visual confirmation dialog). You can still keep the order at that step.**"
    )
    return "\n\n".join(lines)


async def _execute_cancel_order_core(
    proposal: dict[str, Any],
    send_status: SendStatus,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Framework-agnostic core of the cancel-order flow — the full Gate 1/Gate 2 spec is
    in this module's docstring. Called with an already-parsed proposal dict and a
    send_status callback supplied by the UI layer."""
    order_id = proposal.get("order_id")
    symbol = proposal.get("symbol", "?")
    dispatched = False  # flips once the IBKR write has returned — see the except block

    if not order_id:
        await send_status(
            "Cancel proposal is missing order_id — order not cancelled.",
            "System",
        )
        return

    await send_status(
        (
            f"Initiating cancellation for order **{order_id}** ({symbol})…\n\n"
            f"**Gate 1 — Touch ID:** A macOS authentication prompt will appear. "
            f"Use Touch ID or your system password if prompted.\n\n"
            f"**Gate 2 — Confirmation dialog:** A separate window will appear on your desktop "
            f"with full order details and a **SEND TO IBKR** button. "
            f"You have 60 seconds to confirm or it auto-cancels."
        ),
        "System",
    )

    try:
        from dotenv import load_dotenv
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient

        load_dotenv(override=False)
        config = Config.from_env()
        ibkr = IBKRClient(
            config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome"))
        )

        accounts = ibkr.get_accounts()
        account_id = _resolve_account_id(accounts)

        log.info("Cancelling order %s (%s)", order_id, symbol)
        # One read-only status GET before Touch ID: the dialog shows the order as IBKR holds
        # it, not the proposal's copy (gap #40). A failed read falls back to the proposal.
        order_details = _cancel_display_details(ibkr, proposal)
        result = ibkr.cancel_order(account_id, order_id, order_details=order_details)
        dispatched = True

        # Same 200-with-rejection classification as the place path — cancel_order
        # returns the parsed JSON unconditionally, no exception on a rejection.
        if _is_ibkr_rejection(result):
            log.warning("IBKR rejected cancel for order %s: %s", order_id, result)
            _record_rejection(
                store, session_id, noun="cancel", proposal=proposal, result=result, symbol=symbol
            )
            await send_status(
                (
                    f"**Cancel FAILED (order may still be working):** order {order_id} ({symbol})\n"
                    f"IBKR response: {json.dumps(result, indent=2)}"
                ),
                "System",
            )
            # No decision logged — matches the other failure paths in this module.
            return

        # IBKR is explicit that this response body "indicates our request to cancel
        # order 987654 was received, but not that the order ticket itself has been
        # canceled" — so it is reported as exactly that, and nothing more, until the
        # read-back observes the order's real state.
        # Source: https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md
        await send_status(
            (
                f"**Cancel request accepted by IBKR — order {order_id}** ({symbol}). "
                f"IBKR documents that this confirms the request was received, "
                f"not that the order ticket has been cancelled. Verifying live state…\n"
                f"IBKR response: {json.dumps(result, indent=2)}"
            ),
            "System",
        )
        confirmed, readback_line, observed = await _read_back(ibkr, str(order_id), "cancel")
        await send_status(readback_line, "System")

        if store and session_id:
            observed_state = (observed or {}).get("order_status")
            store.add_decision(
                session_id=session_id,
                decision_type="trade_cancelled",
                summary_text=(
                    f"{'CANCELLED' if confirmed else 'CANCEL DISPATCHED (UNVERIFIED)'}: "
                    f"order {order_id} ({symbol}) — "
                    f"observed state: {observed_state or 'not observed'}"
                ),
                symbol=symbol,
                metadata={
                    "proposal": proposal,
                    "ibkr_response": result,
                    "ibkr_order_id": order_id,
                    "readback_confirmed": confirmed,
                    "readback_order_status": observed_state,
                    "readback_response": observed,
                },
            )

    except Exception as exc:
        _log_refusal(exc, dispatched, "Order cancellation", f"order {order_id}")
        _record_refusal(
            store,
            session_id,
            noun="cancel",
            proposal=proposal,
            exc=exc,
            reply_log=[],
            dispatched=dispatched,
            symbol=symbol,
        )
        if dispatched:
            await send_status(_post_dispatch_failure_text(exc, "cancel request"), "System")
        else:
            await send_status(
                f"**Order not cancelled:** {_classify_execution_error(exc)}", "System"
            )


# ── Order modification ───────────────────────────────────────────────────────


def _format_modify_summary(proposal: dict[str, Any], contract_label: str | None = None) -> str:
    """Build the human-approval text for modifying a live order, as a field-by-field diff.

    The only consumer of `changes`, the array `propose_modify` carries alongside the
    replacement order (claudia/proposal_tools.py). Each entry is
    ``{"field": <name>, "previous_value": <prior>}`` and renders as
    ``field: <previous_value> → <proposal[field]>``.

    `changes` is LLM-authored, so the "before" column is a claim, not a verified read of
    the resting order — Gate 2 re-renders the actual order, and that is the authoritative
    view. Falls back to "(no changed fields listed)" when `changes` is absent or empty.

    **Total by construction**: it renders a malformed entry rather than raising. A render
    that dies is precisely how a proposal disappeared while the model went on to describe
    a button that was never created (finding-llm-proposal-block-emission), so this last
    step before the human sees anything must not be the thing that fails.

    Args:
        proposal: Schema-checked modify-proposal dict — the full replacement order.
        contract_label: The resolved futures contract's label (`ESU6 · SEP26 · expires
            2026-09-18`) for the `Contract:` line, or None for no line (design 2026-09-10).

    Returns:
        Markdown for the proposal message.
    """
    order_id = proposal.get("order_id", "?")
    symbol = proposal.get("symbol", "?")
    changes = proposal.get("changes") or []
    reason = proposal.get("reason", "")

    lines = [f"**MODIFY order {order_id}: {symbol}**"]
    if str(proposal.get("sec_type", "STK")).upper() in ("FUT", "FOP") and contract_label:
        # Same line as the place text (design 2026-09-10): the contract, in IBKR's terms.
        lines.append(f"**Contract:** {contract_label}")
    rth_line = _outside_rth_line(proposal)
    if rth_line and proposal.get("outside_rth") is None:
        # A modify resends the whole order: null here DROPS the attribute the original
        # may have carried, and IBKR does not report it for futures, so nothing downstream
        # can catch it (review #1, measured 2026-09-04).
        rth_line += (
            " This modify will resend the order WITHOUT the attribute — state it if the "
            "original had it."
        )
    if rth_line:
        lines.append(rth_line)
    if changes:
        # One block, so the list items stay newline-separated while the blocks around
        # them get a blank line — a blank line between items would make it a loose
        # list (gap #44).
        change_lines: list[str] = []
        for change in changes:
            if not isinstance(change, dict):
                change_lines.append(f"- (malformed change entry: {change!r})")
                continue
            field = change.get("field")
            if not isinstance(field, str):
                change_lines.append(f"- (malformed change entry: {change!r})")
                continue
            # One formatter for both sides of the arrow, shared with Gate 2 — on
            # 2026-09-10 this line read `7900.0 → 7950` while the dialog read
            # `7900.0 → 7950.0` for the same change.
            change_lines.append(
                f"- {field}: {change_value_text(field, change.get('previous_value'))}"
                f" → {change_value_text(field, proposal.get(field))}"
            )
        lines.append("\n".join(change_lines))
    else:
        lines.append("(no changed fields listed)")
    if reason:
        lines.append(f"*Reason:* {reason}")
    lines.append(
        "⚠️ **Clicking 'Modify this order' will initiate IBKR confirmation "
        "(Touch ID + visual confirmation dialog). You can still discard at that step.**"
    )
    return "\n\n".join(lines)


async def _execute_modify_order_core(
    proposal: dict[str, Any],
    send_status: SendStatus,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Framework-agnostic core of the modify-order flow — the full Gate 1/Gate 2 spec is
    in this module's docstring. Called with an already-parsed proposal dict and a
    send_status callback supplied by the UI layer."""
    order_id = proposal.get("order_id")
    conid = proposal.get("conid")
    symbol = proposal.get("symbol", "?")
    dispatched = False  # flips once the IBKR write has returned — see the except block

    if not order_id:
        await send_status(
            "Modify proposal is missing order_id — order not modified.",
            "System",
        )
        return

    if conid is None:
        await send_status(
            (
                "Modify proposal is missing conid. Ask ClaudIA to call `get_order_status` "
                f"for order {order_id} first, then re-issue the modify proposal with the "
                "conid field set. Order not modified."
            ),
            "System",
        )
        return

    await send_status(
        (
            f"Initiating modification for order **{order_id}** ({symbol})…\n\n"
            f"**Gate 1 — Touch ID:** A macOS authentication prompt will appear. "
            f"Use Touch ID or your system password if prompted.\n\n"
            f"**Gate 2 — Confirmation dialog:** A separate window will appear on your desktop "
            f"with full order details and a **SEND TO IBKR** button. "
            f"You have 60 seconds to confirm or it auto-cancels."
        ),
        "System",
    )

    reply_log: list[dict[str, Any]] = []
    try:
        from dotenv import load_dotenv
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient

        load_dotenv(override=False)
        config = Config.from_env()
        ibkr = IBKRClient(
            config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome"))
        )

        action_str = proposal.get("action", "?")
        qty = proposal.get("quantity", 0)
        otype = proposal.get("order_type", "MKT")
        tif = (proposal.get("tif") or "DAY").upper()
        sec_type = proposal.get("sec_type", "STK").upper()
        limit_price = proposal.get("limit_price")
        stop_price = proposal.get("stop_price")

        # Fresh order body — field spec mirrors place_order's (CLAUDE.md Order Staging Flow).
        # modify_order() does no _-prefix stripping, so only genuine IBKR fields go in here.
        order_body: dict[str, Any] = {
            "conid": int(conid),
            "orderType": otype,
            "side": action_str,
            "tif": tif,
            "quantity": int(qty),
            "ticker": symbol,
        }
        if sec_type in ("FUT", "FOP"):
            # CME Rule 536-B — manualIndicator only, same as the place path above.
            # extOperator deliberately NOT sent (IBKR rejects it as undocumented
            # field 8089 on this account class — proven 2026-07-23; see
            # docs/plans/2026-07-23-futures-order-field-8089-bug.md).
            order_body["manualIndicator"] = True
        if otype == "LMT" and limit_price is not None:
            order_body["price"] = float(limit_price)
        elif otype == "STP" and stop_price is not None:
            order_body["price"] = float(stop_price)
        elif otype == "STOP_LIMIT":
            if limit_price is not None:
                order_body["price"] = float(limit_price)
            if stop_price is not None:
                order_body["auxPrice"] = float(stop_price)
        _apply_outside_rth(order_body, proposal)
        # Same facts as the place path (gap #34): a future's label carries the contract
        # month and its multiplier turns the price into a notional; a stock gets its name
        # and currency (gap #49). Always an ISO code, never a guess.
        _apply_contract_display_facts(ibkr, order_body, int(conid), sec_type)
        order_body["_changes"] = list(proposal.get("changes") or [])
        current = _current_order_description(ibkr, str(order_id))
        if current:
            order_body["_current_description"] = current

        accounts = ibkr.get_accounts()
        account_id = _resolve_account_id(accounts)

        log.info("Modifying order %s: %s", order_id, order_body)
        result = ibkr.modify_order_and_confirm(
            account_id, order_id, order_body, reply_log=reply_log
        )
        dispatched = True
        reply_line = _format_reply_log(reply_log)
        if reply_line:
            await send_status(reply_line, "System")

        # Same 200-with-rejection classification as the place path — modify hits the
        # same order-submission machinery and can return the same rejection shape.
        if _is_ibkr_rejection(result):
            log.warning("IBKR rejected modify for order %s: %s", order_id, result)
            _record_rejection(
                store, session_id, noun="modify", proposal=proposal, result=result, symbol=symbol
            )
            await send_status(
                (
                    f"**Modify REJECTED by IBKR (not applied):** order {order_id} ({symbol})\n"
                    f"IBKR response: {json.dumps(result, indent=2)}"
                ),
                "System",
            )
            # No decision logged — matches the other failure paths in this module.
            return

        await send_status(
            (
                f"**Modify request accepted by IBKR — order {order_id}** ({symbol}). "
                f"Verifying live state…\n"
                f"IBKR response: {json.dumps(result, indent=2)}"
            ),
            "System",
        )
        confirmed, readback_line, observed = await _read_back(ibkr, str(order_id), "modify")
        await send_status(readback_line, "System")

        # A working status only proves the order exists. Whether the *modification*
        # landed is a separate question, answered by comparing the read-back's fields
        # against the body that was dispatched.
        fields_agree = False
        if observed is not None:
            fields_agree, comparison_line = _compare_modify_readback(order_body, observed)
            await send_status(comparison_line, "System")
        confirmed = confirmed and fields_agree

        if store and session_id:
            observed_state = (observed or {}).get("order_status")
            store.add_decision(
                session_id=session_id,
                decision_type="trade_modified",
                summary_text=(
                    f"{'MODIFIED' if confirmed else 'MODIFY DISPATCHED (UNVERIFIED)'}: "
                    f"order {order_id} ({symbol}) — "
                    f"observed state: {observed_state or 'not observed'}"
                ),
                symbol=symbol,
                metadata={
                    "proposal": proposal,
                    "ibkr_response": result,
                    "ibkr_replies": reply_log,
                    "ibkr_order_id": order_id,
                    "readback_confirmed": confirmed,
                    "readback_order_status": observed_state,
                    "readback_fields_match": fields_agree,
                    "readback_response": observed,
                },
            )

    except Exception as exc:
        _log_refusal(exc, dispatched, "Order modification", f"order {order_id}")
        _record_refusal(
            store,
            session_id,
            noun="modify",
            proposal=proposal,
            exc=exc,
            reply_log=reply_log,
            dispatched=dispatched,
            symbol=symbol,
        )
        if dispatched:
            await send_status(_post_dispatch_failure_text(exc, "modify request"), "System")
        else:
            await send_status(
                f"**Order not modified:** {_classify_execution_error(exc)}"
                f"{_declined_reply_text(reply_log)}",
                "System",
            )
