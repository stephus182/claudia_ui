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


# ── Imports for new tests ─────────────────────────────────────────────────────
from unittest.mock import MagicMock, patch

from claudia.agent import (
    ClaudIAAgent,
    _build_version_note,
    _history_to_messages,
)


def _make_agent():
    """Build a ClaudIAAgent with all dependencies mocked."""
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    loader = MagicMock()
    with patch("claudia.agent.AsyncAnthropic"):
        return ClaudIAAgent(
            toolkit=toolkit,
            store=store,
            context_loader=loader,
            session_id="test-session",
        )


# ── _history_to_messages ──────────────────────────────────────────────────────

def test_history_to_messages_user_and_assistant():
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    result = _history_to_messages(history)
    assert result == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


def test_history_to_messages_skips_tool_rows():
    """Tool rows must be skipped — orphaned tool_result blocks cause Anthropic API 400."""
    history = [
        {"role": "user", "content": "Get positions"},
        {"role": "tool", "content": None, "tool_name": "get_positions", "tool_result": "[...]"},
        {"role": "assistant", "content": "You hold 100 AAPL."},
    ]
    result = _history_to_messages(history)
    assert len(result) == 2
    assert all(r["role"] != "tool" for r in result)


def test_history_to_messages_empty():
    assert _history_to_messages([]) == []


def test_history_to_messages_none_content_becomes_empty_string():
    history = [{"role": "user", "content": None}]
    result = _history_to_messages(history)
    assert result[0]["content"] == ""


# ── _build_version_note ───────────────────────────────────────────────────────

def test_build_version_note_no_version():
    assert _build_version_note(None, None) == ""
    assert _build_version_note("", None) == ""


def test_build_version_note_first_version_no_prev():
    store = MagicMock()
    store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"},
    ]
    result = _build_version_note("v1", store)
    assert "v1" in result
    assert "Active document version" in result
    assert "previous" not in result


def test_build_version_note_second_version_shows_prev():
    store = MagicMock()
    store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"},
        {"version": "v2", "created_at": "2026-06-10T00:00:00"},
    ]
    result = _build_version_note("v2", store)
    assert "v2" in result
    assert "previous" in result
    assert "v1" in result


# ── ClaudIAAgent._handle_local_tool ──────────────────────────────────────────

def test_handle_local_tool_list_versions_empty():
    agent = _make_agent()
    agent._store.list_doc_versions.return_value = []
    result = agent._handle_local_tool("list_doc_versions", {})
    assert "No document versions" in result


def test_handle_local_tool_list_versions_with_entries():
    agent = _make_agent()
    agent._store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"},
        {"version": "v2", "created_at": "2026-06-10T00:00:00"},
    ]
    result = agent._handle_local_tool("list_doc_versions", {})
    assert "v1" in result
    assert "v2" in result
    assert "2026-06-01" in result


def test_handle_local_tool_get_version_found():
    agent = _make_agent()
    agent._store.get_doc_version.return_value = {
        "version": "v1",
        "created_at": "2026-06-01T00:00:00",
        "context_text": "# Role\nI am ClaudIA.",
        "principles_text": "# Rules\nNo YOLO trades.",
    }
    result = agent._handle_local_tool("get_doc_version", {"version": "v1"})
    assert "# Role" in result
    assert "# Rules" in result
    assert "v1" in result


def test_handle_local_tool_get_version_not_found():
    agent = _make_agent()
    agent._store.get_doc_version.return_value = None
    agent._store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"}
    ]
    result = agent._handle_local_tool("get_doc_version", {"version": "v99"})
    assert "not found" in result.lower()
    assert "v1" in result  # available list shown


def test_handle_local_tool_unknown_name():
    agent = _make_agent()
    result = agent._handle_local_tool("nonexistent_tool", {})
    assert "Unknown" in result


# ── ClaudIAAgent._extract_decisions ──────────────────────────────────────────

def test_log_proposal_with_order_proposal():
    agent = _make_agent()
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 50,
        "order_type": "LMT",
        "reason": "Support bounce",
    }
    agent._log_proposal("Some text", proposal, msg_id=42)
    agent._store.add_decision.assert_called_once()
    kwargs = agent._store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_proposed"
    assert "AAPL" in kwargs["summary_text"]
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["message_id"] == 42
    assert kwargs["session_id"] == "test-session"


def test_log_proposal_without_proposal():
    agent = _make_agent()
    agent._log_proposal("Just analysis, no trade.", None, msg_id=1)
    agent._store.add_decision.assert_not_called()


# ── ClaudIAAgent.set_tv_bridge ────────────────────────────────────────────────

def test_set_tv_bridge_updates_tool_names():
    agent = _make_agent()
    assert agent._tv_tool_names == set()

    bridge = MagicMock()
    tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "quote_get", "description": "", "input_schema": {}},
    ]
    agent.set_tv_bridge(bridge, tools)

    assert agent._tv_bridge is bridge
    assert "chart_get_state" in agent._tv_tool_names
    assert "quote_get" in agent._tv_tool_names
    assert len(agent._tv_tool_names) == 2


# ── ClaudIAAgent._all_tools property ─────────────────────────────────────────

def test_all_tools_includes_toolkit_extra_and_local():
    agent = _make_agent()
    agent._toolkit.tools = [{"name": "get_positions", "description": "", "input_schema": {}}]
    agent._extra_tools = [{"name": "chart_get_state", "description": "", "input_schema": {}}]

    names = {t["name"] for t in agent._all_tools}
    assert "get_positions" in names       # from toolkit
    assert "chart_get_state" in names     # from extra_tools (TV)
    assert "list_doc_versions" in names   # local
    assert "get_doc_version" in names     # local


# ── Prompt caching: _with_cache_marker (tools breakpoint) ────────────────────

from claudia.agent import _with_cache_marker, _LOCAL_TOOLS


def test_with_cache_marker_marks_only_last_tool():
    tools = [
        {"name": "a", "input_schema": {"type": "object"}},
        {"name": "b", "input_schema": {"type": "object"}},
    ]
    marked = _with_cache_marker(tools)
    assert marked[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked[0]
    assert marked[-1]["name"] == "b"


def test_with_cache_marker_does_not_mutate_input():
    original_last = {"name": "b", "input_schema": {"type": "object"}}
    tools = [{"name": "a", "input_schema": {"type": "object"}}, original_last]
    _with_cache_marker(tools)
    # The shared dict must be untouched — _LOCAL_TOOLS / toolkit constants are module-level
    assert "cache_control" not in original_last
    assert "cache_control" not in tools[-1]


def test_with_cache_marker_empty_list():
    assert _with_cache_marker([]) == []


def test_local_tools_constant_never_carries_cache_control():
    # Regression guard: repeated calls must not leak the marker into the module constant
    _with_cache_marker(list(_LOCAL_TOOLS))
    _with_cache_marker(list(_LOCAL_TOOLS))
    assert all("cache_control" not in t for t in _LOCAL_TOOLS)


def test_all_tools_last_entry_carries_cache_marker():
    agent = _make_agent()
    agent._toolkit.tools = [{"name": "get_positions", "description": "", "input_schema": {}}]
    tools = agent._all_tools
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in tools[:-1])


# ── Prompt caching: _system_blocks (system breakpoint) ───────────────────────

from claudia.agent import _system_blocks


def test_system_blocks_shape():
    blocks = _system_blocks("You are ClaudIA.")
    assert blocks == [
        {
            "type": "text",
            "text": "You are ClaudIA.",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_system_blocks_preserves_full_prompt():
    prompt = _build_system_prompt("# Role\nTrader assistant.\n\n# Principles\nRisk first.")
    blocks = _system_blocks(prompt)
    assert len(blocks) == 1
    assert blocks[0]["text"] == prompt  # byte-identical — any change invalidates the cache
