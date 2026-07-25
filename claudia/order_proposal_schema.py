"""Schema for the order-proposal blocks the LLM emits — validation only, no execution.

The model is told exactly which fields and values it may emit in the "ORDER PROPOSAL
FORMAT" section of `agent.py`'s `_SAFETY_BLOCK`. Nothing enforced that contract, so a
proposal carrying an unrecognised action, a negative quantity, or a made-up order type
would be rendered into the approval summary and handed to the execution core unchecked
(security-audit-2026-07-25.md, M-1).

This module is the enforcement half of that contract. It is deliberately dependency-free
and execution-free: `agent.py` keeps its runtime decoupling from the order-execution layer,
and nothing here can reach IBKR.

Defence in depth, not the primary barrier — Gate 1 (Touch ID) and Gate 2 (the AppKit
confirmation dialog) still stand between any proposal and a live order. What this adds is
that the human never sees, and can never click through, a summary built from values the
model was not permitted to emit.
"""

from __future__ import annotations

# Enum values as declared to the model in the _SAFETY_BLOCK "ORDER PROPOSAL FORMAT"
# section. Kept in sync by hand — these are the *only* values the model is told to emit, so
# anything else is a malformed proposal, not a new feature.
_VALID_ACTIONS = frozenset({"BUY", "SELL"})
_VALID_ORDER_TYPES = frozenset({"MKT", "LMT", "STP", "STOP_LIMIT"})
_VALID_TIFS = frozenset({"DAY", "GTC", "IOC", "OPG"})
_VALID_SEC_TYPES = frozenset({"STK", "FUT", "OPT", "FOP", "CASH"})


class ProposalValidationError(ValueError):
    """A proposal block from the LLM does not conform to the declared schema.

    Raised before anything is rendered or executed, so a malformed proposal can never reach
    the staging button (security-audit-2026-07-25.md, M-1).
    """


def _require_enum(proposal: dict, key: str, valid: frozenset[str], default: str) -> None:
    """Reject `proposal[key]` unless it is one of `valid` (case-insensitively).

    Absent or None is accepted: the summary formatters and execution cores both apply
    `default`, so an omitted key is well-defined rather than malformed.

    Raises:
        ProposalValidationError: if present and not a string in `valid`.
    """
    raw = proposal.get(key)
    if raw is None:
        return
    if not isinstance(raw, str) or raw.upper() not in valid:
        raise ProposalValidationError(
            f"{key}={raw!r} is not one of {sorted(valid)} (default when omitted: {default})"
        )


def validate_order_proposal(proposal: dict) -> None:
    """Check an ```order-proposal``` block against the schema before it is shown or staged.

    Defence in depth, not the primary barrier — Gate 1 (Touch ID) and Gate 2 (the AppKit
    confirmation dialog) still stand between any proposal and IBKR. What this adds is that
    the human never sees, and can never click through, a summary built from values the model
    was not permitted to emit.

    **Validates only; never repairs.** Order parameters are immutable — a validator that
    silently normalised `action`/`quantity`/price would violate the rule enforced in
    _SAFETY_BLOCK and recorded in memory (feedback-order-parameter-immutability). A bad
    proposal is rejected whole and reported to the user.

    Args:
        proposal: Parsed JSON from the fenced block emitted by the model.

    Raises:
        ProposalValidationError: on a missing/blank symbol, an action outside
            {BUY, SELL}, a non-positive or non-numeric quantity, or an order_type / tif /
            sec_type outside the declared enums.
    """
    symbol = proposal.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ProposalValidationError(f"symbol={symbol!r} is missing or blank")

    action = proposal.get("action")
    if not isinstance(action, str) or action.upper() not in _VALID_ACTIONS:
        raise ProposalValidationError(f"action={action!r} is not one of {sorted(_VALID_ACTIONS)}")

    qty = proposal.get("quantity")
    # bool is an int subclass — True would otherwise pass as quantity 1.
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        raise ProposalValidationError(f"quantity={qty!r} is not a number")
    if not qty > 0 or qty != qty or qty in (float("inf"), float("-inf")):
        raise ProposalValidationError(f"quantity={qty!r} must be a finite positive number")

    _require_enum(proposal, "order_type", _VALID_ORDER_TYPES, "MKT")
    _require_enum(proposal, "tif", _VALID_TIFS, "DAY")
    _require_enum(proposal, "sec_type", _VALID_SEC_TYPES, "STK")


def validate_amend_proposal(proposal: dict, kind: str) -> None:
    """Check an ```order-cancel-proposal``` / ```order-modify-proposal``` block.

    Both address an order that already exists, so the identity field is what matters: acting
    on the wrong `order_id` is the failure mode. Instrument and pricing fields are not
    re-checked here — for modify they are validated by the same IBKR round-trip that a fresh
    order takes, and for cancel they are display-only.

    Args:
        proposal: Parsed JSON from the fenced block emitted by the model.
        kind: "cancel" or "modify", used only in the error message.

    Raises:
        ProposalValidationError: if order_id is missing, blank, or not a string/int.
    """
    order_id = proposal.get("order_id")
    if isinstance(order_id, bool) or not isinstance(order_id, (str, int)):
        raise ProposalValidationError(f"{kind}: order_id={order_id!r} is missing or not an id")
    if isinstance(order_id, str) and not order_id.strip():
        raise ProposalValidationError(f"{kind}: order_id is blank")


