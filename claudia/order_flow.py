"""
Human-initiated order staging for ClaudIA.

The LLM never calls this code directly. Flow:
  1. ClaudIA embeds an order-proposal block in its response text.
  2. agent.py parses it and calls render_order_proposal().
  3. User sees a message with full order details + "Stage this order" button.
  4. User clicks the button → execute_staged_order() fires.
  5. IBKRClient.place_order_and_confirm() fires:
       Gate 1 — Touch ID (human_auth.require_touch_id)
       Gate 2 — AppKit colored dialog, green/BUY or red/SELL (order_confirm)
       Any chained IBKR reply prompts are resolved in a loop, each re-running
       Gate 1 + Gate 2 with the real IBKR warning text, until a terminal response.
  6. On success, result is logged to ConversationStore.decisions (if store is wired).

No order can be placed without steps 3–5 happening via physical user interaction.
ClaudIA must never modify any user-specified order parameter (price, qty, symbol, type, TIF).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING

import chainlit as cl

if TYPE_CHECKING:
    from claudia.conversation_store import ConversationStore

log = logging.getLogger(__name__)


def _format_order_summary(proposal: dict) -> str:
    symbol = proposal.get("symbol", "?")
    action = proposal.get("action", "?")
    qty = proposal.get("quantity", "?")
    otype = proposal.get("order_type", "MKT")
    limit = proposal.get("limit_price")
    stop = proposal.get("stop_price")
    tif = (proposal.get("tif") or proposal.get("time_in_force") or proposal.get("timeInForce") or "DAY").upper()
    sec_type = proposal.get("sec_type", "STK").upper()
    reason = proposal.get("reason", "")

    price_str = ""
    if otype == "LMT" and limit is not None:
        price_str = f" @ ${limit:.2f} limit"
    elif otype == "STP" and stop is not None:
        price_str = f" @ ${stop:.2f} stop"

    sec_label = f" [{sec_type}]" if sec_type != "STK" else ""
    lines = [
        f"**{action} {qty} {symbol}{sec_label}** ({otype}{price_str}, {tif})",
    ]
    if reason:
        lines.append(f"*Reason:* {reason}")
    lines.append(
        "\n⚠️ **Clicking 'Stage this order' will initiate IBKR confirmation "
        "(Touch ID + visual confirmation dialog). You can still cancel at that step.**"
    )
    return "\n".join(lines)


async def render_order_proposal(proposal: dict, session_id: str | None = None) -> None:
    """Render an order proposal as a Chainlit message with staging action buttons."""
    summary = _format_order_summary(proposal)
    proposal_json = json.dumps(proposal)

    actions = [
        cl.Action(
            name="stage_order",
            payload={"order": proposal_json},
            label="Stage this order",
            tooltip="Opens IBKR Touch ID + confirmation dialog",
        ),
        cl.Action(
            name="cancel_proposal",
            payload={},
            label="Cancel",
            tooltip="Dismiss this proposal",
        ),
    ]

    await cl.Message(
        content=summary,
        actions=actions,
        author="ClaudIA — Order Proposal",
    ).send()


async def execute_staged_order(
    action: cl.Action,
    session_id: str | None = None,
    store: "ConversationStore | None" = None,
) -> None:
    """
    Execute the staged order by calling IBKRClient.place_order_and_confirm(), which
    resolves any chained IBKR reply prompts before returning.

    Gate 1 — Touch ID (require_touch_id in ibkr_core_mcp.human_auth)
    Gate 2 — AppKit colored dialog: green for BUY, red for SELL (ibkr_core_mcp.order_confirm).
              Falls back to osascript plain dialog if the AppKit subprocess fails.

    This function is only called from a physical button click action callback.
    ClaudIA's ORDER PARAMETER IMMUTABILITY rule prohibits changing any user-specified
    field (price, quantity, symbol, order type, TIF) without explicit user approval.
    """
    try:
        proposal = json.loads(action.payload["order"])
    except (json.JSONDecodeError, TypeError, KeyError):
        await cl.Message(content="Invalid order proposal data.", author="System").send()
        await action.remove()
        return

    symbol = proposal.get("symbol", "?")
    action_str = proposal.get("action", "?")
    qty = proposal.get("quantity", 0)
    otype = proposal.get("order_type", "MKT")
    limit_price = proposal.get("limit_price")
    sec_type = proposal.get("sec_type", "STK").upper()

    await cl.Message(
        content=(
            f"Initiating staging for **{action_str} {qty} {symbol}** ({sec_type})…\n\n"
            f"**Gate 1 — Touch ID:** A macOS authentication prompt will appear. "
            f"Use Touch ID or your system password if prompted.\n\n"
            f"**Gate 2 — Confirmation dialog:** A separate window will appear on your desktop "
            f"with full order details and a **SEND TO IBKR** button. "
            f"You have 60 seconds to confirm or it auto-cancels."
        ),
        author="System",
    ).send()

    try:
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
        from dotenv import load_dotenv
        load_dotenv(override=False)
        config = Config.from_env()
        ibkr = IBKRClient(config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")))

        # Resolve conid — routing depends on sec_type and optional conid override.
        # /iserver/secdef/search only documents STK, IND, BOND — NOT FUT, FOP, or CASH.
        # FOP requires expiry+strike+right — cannot infer from symbol alone; caller must
        # pre-resolve via get_option_strikes and embed conid in the proposal.
        # Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#sec-search
        multiplier: float | None = None
        override_conid = proposal.get("conid")
        if override_conid is not None:
            # Pre-resolved conid (required for FOP; valid for any instrument).
            # ClaudIA resolves options chain in conversation and embeds the conid.
            conid = int(override_conid)
            company_name = proposal.get("_companyName", "")
        elif sec_type == "FOP":
            # FOP conid resolution requires expiry + strike + put/call — cannot derive
            # from symbol alone. ClaudIA must call get_option_strikes first and re-issue
            # the order proposal with the conid field set.
            await cl.Message(
                content=(
                    f"Futures Options (FOP) orders require a pre-resolved contract ID. "
                    f"Ask ClaudIA to look up the specific contract "
                    f"(expiry, strike, call/put) for **{symbol}** via `get_option_strikes`, "
                    f"then re-issue the order proposal with the `conid` field set."
                ),
                author="System",
            ).send()
            return
        elif sec_type == "FUT":
            futures = ibkr.get_futures([symbol])
            if not futures:
                await cl.Message(
                    content=f"Could not find futures contracts for {symbol}. Order not placed.",
                    author="System",
                ).send()
                return
            try:
                contract = min(futures, key=lambda f: int(f.get("expirationDate") or 0))
            except (ValueError, TypeError):
                contract = futures[0]
            conid = int(contract.get("conid"))
            company_name = contract.get("contractDesc", contract.get("description", ""))
            raw_mult = contract.get("multiplier")
            try:
                multiplier = float(raw_mult) if raw_mult is not None else None
            except (ValueError, TypeError):
                multiplier = None
        else:
            contracts = ibkr.search_contract(symbol)
            if not contracts:
                await cl.Message(
                    content=f"Could not find contract for {symbol}. Order not placed.",
                    author="System",
                ).send()
                return
            conid = int(contracts[0].get("conid"))  # int — required by IBKR API
            company_name = contracts[0].get("companyName", "")

        claudia_ref = f"CLAUDIA-{int(time.time() * 1000)}"
        tif = (proposal.get("tif") or proposal.get("time_in_force") or proposal.get("timeInForce") or "DAY").upper()

        # ----------------------------------------------------------------
        # Order body — field spec from IBKR CP API docs (2026-07-02)
        # Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#place-order
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
        # extOperator    str      FUT/FOP*   CME Rule 536-B compliance (required since May 1 2025)
        # Source (536-B): https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/
        # ----------------------------------------------------------------
        order_body: dict = {
            "conid":     conid,                       # int
            "orderType": otype,                       # str
            "side":      action_str,                  # str: BUY | SELL
            "tif":       tif,                         # str: DAY | GTC | OPG | IOC
            "quantity":  int(qty),                    # int (docs say float, example uses int)
            "ticker":    symbol,                      # str — display + valid IBKR field
            "acctId":    "",                          # filled below after account lookup
            "cOID":      claudia_ref,                 # str — max 64 chars
            "_companyName": company_name,             # display only — underscore prefix → stripped
        }
        if sec_type in ("FUT", "FOP"):
            # Required for US Futures and Futures Options — CME Group Rule 536-B
            # manualIndicator=True: order submitted through a manual UI (not automated)
            # extOperator: identifies the submitting user/system
            order_body["manualIndicator"] = True
            order_body["extOperator"] = "ClaudIA"
            if multiplier is not None:
                order_body["_multiplier"] = multiplier   # display only — stripped by client.py
        if otype == "LMT" and limit_price is not None:
            order_body["price"] = float(limit_price)          # float
        elif otype == "STP" and proposal.get("stop_price") is not None:
            order_body["price"] = float(proposal["stop_price"])   # float (STP uses price field)
        elif otype == "STOP_LIMIT":
            if limit_price is not None:
                order_body["price"] = float(limit_price)
            if proposal.get("stop_price") is not None:
                order_body["auxPrice"] = float(proposal["stop_price"])

        # Gate 1 (Touch ID) + Gate 2 (AppKit colored dialog) fire inside place_order()
        accounts = ibkr.get_accounts()
        account_id = accounts[0].get("accountId", accounts[0].get("acctId", accounts[0].get("id", ""))) if accounts else ""
        order_body["acctId"] = account_id
        log.info("Placing order: %s", {k: v for k, v in order_body.items() if not k.startswith("_")})
        result = ibkr.place_order_and_confirm(account_id, order_body)

        success_text = (
            f"**Order staged successfully:** {action_str} {qty} {symbol} ({otype})\n"
            f"IBKR response: {json.dumps(result, indent=2)}"
        )
        await cl.Message(content=success_text, author="ClaudIA").send()

        if store and session_id:
            # Extract IBKR orderId from response for future cross-referencing
            ibkr_order_id = None
            if isinstance(result, list) and result:
                ibkr_order_id = result[0].get("orderId")
            elif isinstance(result, dict):
                ibkr_order_id = result.get("orderId")
            store.add_decision(
                session_id=session_id,
                decision_type="trade_staged",
                summary_text=f"STAGED: {action_str} {qty} {symbol} ({otype})",
                symbol=symbol,
                metadata={
                    "proposal": proposal,
                    "ibkr_response": result,
                    "ibkr_order_id": ibkr_order_id,
                    "claudia_ref": claudia_ref,
                },
            )

    except Exception as exc:
        log.exception("Order staging failed for %s", symbol)
        error_msg = str(exc)
        exc_type = type(exc).__name__
        # Check most-specific patterns first so dialog-cancel doesn't show as Touch ID failure
        if "cancelled by user" in error_msg.lower():
            display_error = "Order was cancelled at the confirmation dialog."
        elif "declined ibkr order reply" in error_msg.lower():
            display_error = "Order was placed but declined at a follow-up IBKR confirmation prompt — check IBKR for its current status."
        elif "timed out" in error_msg.lower() and "touch" not in error_msg.lower():
            display_error = "Confirmation dialog timed out (60 seconds) — order not placed."
        elif "authentication" in error_msg.lower() or "touch" in error_msg.lower() or "HumanAuth" in exc_type:
            display_error = "Touch ID authentication failed or was cancelled."
        elif "403" in error_msg:
            display_error = "IBKR rejected the order (HTTP 403) — brokerage session may need re-initialisation. Try logging in to the Client Portal gateway and retrying."
        else:
            display_error = f"Order staging failed ({exc_type}: {error_msg})"
        await cl.Message(content=f"**Order not placed:** {display_error}", author="System").send()
    finally:
        await action.remove()
