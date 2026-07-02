"""
Human-initiated order staging for ClaudIA.

The LLM never calls this code directly. Flow:
  1. ClaudIA embeds an order-proposal block in its response text.
  2. agent.py parses it and calls render_order_proposal().
  3. User sees a message with full order details + "Stage this order" button.
  4. User clicks the button → execute_staged_order() fires.
  5. IBKRClient.place_order() is called → Touch ID (Gate 1) + tkinter dialog (Gate 2).
  6. Result is logged to ConversationStore.decisions.

No order can be placed without steps 3–5 happening via physical user interaction.
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
    reason = proposal.get("reason", "")

    price_str = ""
    if otype == "LMT" and limit is not None:
        price_str = f" @ ${limit:.2f} limit"
    elif otype == "STP" and stop is not None:
        price_str = f" @ ${stop:.2f} stop"

    lines = [
        f"**{action} {qty} {symbol}** ({otype}{price_str})",
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
    Execute the staged order by calling IBKRClient.place_order().
    Touch ID (Gate 1) and tkinter dialog (Gate 2) fire inside ibkr_core_mcp.
    This function is only called from a physical button click action callback.
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

    await cl.Message(
        content=f"Initiating staging for **{action_str} {qty} {symbol}**… "
                f"Touch ID prompt will appear on your Mac.",
        author="System",
    ).send()

    try:
        from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
        from dotenv import load_dotenv
        load_dotenv(override=False)
        config = Config.from_env()
        ibkr = IBKRClient(config=config, auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")))

        # Resolve conid
        contracts = ibkr.search_contract(symbol)
        if not contracts:
            await cl.Message(
                content=f"Could not find contract for {symbol}. Order not placed.",
                author="System",
            ).send()
            return
        conid = contracts[0].get("conid")

        # Build order dict
        claudia_ref = f"CLAUDIA-{int(time.time() * 1000)}"
        order_body: dict = {
            "conid": conid,
            "orderType": otype,
            "side": action_str,
            "quantity": qty,
            "tif": "DAY",
            "cOID": claudia_ref,  # customer order ID — survives round-trip, identifies ClaudIA orders
        }
        if otype == "LMT" and limit_price is not None:
            order_body["price"] = limit_price
        elif otype == "STP" and proposal.get("stop_price") is not None:
            order_body["auxPrice"] = proposal["stop_price"]

        # Gate 1 (Touch ID) + Gate 2 (tkinter dialog) fire inside place_order()
        accounts = ibkr.get_accounts()
        account_id = accounts[0].get("accountId", accounts[0].get("id", "")) if accounts else ""
        result = ibkr.place_order(account_id, order_body)

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
        # Don't leak raw exception details to chat — show a controlled message
        if "authentication" in error_msg.lower() or "touch" in error_msg.lower() or "HumanAuth" in exc_type:
            display_error = "Touch ID authentication failed or was cancelled."
        elif "cancelled" in error_msg.lower():
            display_error = "Order was cancelled at the confirmation dialog."
        elif "403" in error_msg:
            display_error = "IBKR rejected the order (HTTP 403) — brokerage session may need re-initialisation. Try logging in to the Client Portal gateway and retrying."
        else:
            display_error = f"Order staging failed ({exc_type}: {error_msg})"
        await cl.Message(content=f"**Order not placed:** {display_error}", author="System").send()
    finally:
        await action.remove()
