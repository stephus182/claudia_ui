"""Tests for the ClaudIAAgent — order proposal parsing, decision extraction."""

import json


from claudia.agent import _strip_order_proposal, _build_system_prompt


def test_strip_order_proposal_found():
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 50,
        "order_type": "LMT",
        "limit_price": 185.0,
        "reason": "Breakout above resistance",
    }
    text = (
        "Here is my analysis.\n\n"
        "```order-proposal\n"
        f"{json.dumps(proposal, indent=2)}\n"
        "```\n\n"
        "Let me know if you want to proceed."
    )
    clean, parsed = _strip_order_proposal(text)
    assert "order-proposal" not in clean
    assert "```" not in clean
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
    assert parsed["quantity"] == 50
    assert "Here is my analysis" in clean
    assert "Let me know" in clean


def test_strip_order_proposal_not_found():
    text = "Here is a regular response with no order proposal."
    clean, parsed = _strip_order_proposal(text)
    assert clean == text
    assert parsed is None


def test_strip_order_proposal_malformed_json():
    text = "Some text.\n```order-proposal\n{not valid json}\n```\nEnd."
    clean, parsed = _strip_order_proposal(text)
    # Malformed JSON: block not stripped, proposal is None
    assert parsed is None


def test_build_system_prompt_contains_safety():
    prompt = _build_system_prompt("# Role\nI am a trader assistant.\n\n# Principles\nRisk first.")
    assert "cannot place" in prompt.lower() or "CANNOT place" in prompt
    assert "order-proposal" in prompt
    assert "financial advisor" in prompt.lower()


def test_build_system_prompt_contains_context():
    context = "# Role\nI am ClaudIA.\n\n# Principles\nNo YOLO trades."
    prompt = _build_system_prompt(context)
    assert "ClaudIA" in prompt
    assert "No YOLO trades" in prompt


def test_order_proposal_all_order_types():
    for otype in ["MKT", "LMT", "STP"]:
        proposal = {
            "symbol": "TSLA",
            "action": "SELL",
            "quantity": 10,
            "order_type": otype,
            "limit_price": None,
            "stop_price": None,
            "reason": "Test",
        }
        text = f"Analysis.\n```order-proposal\n{json.dumps(proposal)}\n```"
        clean, parsed = _strip_order_proposal(text)
        assert parsed["order_type"] == otype
        assert "order-proposal" not in clean
