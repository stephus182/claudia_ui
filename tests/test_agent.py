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


# ── _make_block_stripper / cancel & modify proposal stripping ───────────────

from claudia.agent import (
    _make_block_stripper,
    _strip_order_cancel_proposal,
    _strip_order_modify_proposal,
    _LOCAL_TOOL_NAMES,
)


def test_make_block_stripper_builds_working_stripper_for_arbitrary_tag():
    """The factory isn't hardcoded to order tags — it works for any fenced tag."""
    strip = _make_block_stripper("test-block")
    text = 'Before.\n```test-block\n{"a": 1}\n```\nAfter.'
    clean, parsed = strip(text)
    assert parsed == {"a": 1}
    assert "test-block" not in clean
    assert "Before." in clean and "After." in clean


def test_strip_order_cancel_proposal_found():
    proposal = {"order_id": "242538143", "symbol": "AAPL", "action": "BUY", "quantity": 1}
    text = f"Sure, let's cancel it.\n```order-cancel-proposal\n{json.dumps(proposal)}\n```"
    clean, parsed = _strip_order_cancel_proposal(text)
    assert "order-cancel-proposal" not in clean
    assert parsed["order_id"] == "242538143"
    assert "Sure, let's cancel it" in clean


def test_strip_order_cancel_proposal_not_found():
    text = "No cancellation here."
    clean, parsed = _strip_order_cancel_proposal(text)
    assert clean == text
    assert parsed is None


def test_strip_order_cancel_proposal_malformed_json():
    text = "Text.\n```order-cancel-proposal\n{not json}\n```\nEnd."
    clean, parsed = _strip_order_cancel_proposal(text)
    assert parsed is None


def test_strip_order_modify_proposal_found():
    proposal = {
        "order_id": "242538143", "conid": 265598, "symbol": "AAPL",
        "action": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 105.0,
        "tif": "GTC", "_changed_fields": ["limit_price"], "_previous_values": {"limit_price": 100.0},
    }
    text = f"Here's the modification.\n```order-modify-proposal\n{json.dumps(proposal)}\n```"
    clean, parsed = _strip_order_modify_proposal(text)
    assert "order-modify-proposal" not in clean
    assert parsed["conid"] == 265598
    assert parsed["_changed_fields"] == ["limit_price"]


def test_strip_order_modify_proposal_not_found():
    text = "No modification here."
    clean, parsed = _strip_order_modify_proposal(text)
    assert clean == text
    assert parsed is None


def test_strip_order_modify_proposal_malformed_json():
    text = "Text.\n```order-modify-proposal\n{not json}\n```\nEnd."
    clean, parsed = _strip_order_modify_proposal(text)
    assert parsed is None


def test_strip_order_proposal_unaffected_by_cancel_modify_tags():
    """The three strippers only match their own exact tag — no cross-matching."""
    cancel_text = '```order-cancel-proposal\n{"order_id": "1"}\n```'
    assert _strip_order_proposal(cancel_text) == (cancel_text, None)
    modify_text = '```order-modify-proposal\n{"order_id": "1", "conid": 1}\n```'
    assert _strip_order_proposal(modify_text) == (modify_text, None)


# ── Hard Rule 1 regression (CLAUDE.md) ───────────────────────────────────────

def test_local_tool_names_excludes_order_write_tools():
    """CLAUDE.md Hard Rule 1: the LLM must never receive a callable tool for
    place_order/modify_order/cancel_order/reply_order — order execution is a
    UI-layer action triggered by a physical button click, never an LLM tool call."""
    forbidden = {"place_order", "modify_order", "cancel_order", "reply_order"}
    assert forbidden & _LOCAL_TOOL_NAMES == set()


# ── Safety block: order cancel/modify rules ──────────────────────────────────

def test_safety_block_documents_cancel_and_modify_proposal_formats():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "order-cancel-proposal" in prompt
    assert "order-modify-proposal" in prompt


def test_safety_block_requires_order_id_provenance():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "get_live_orders" in prompt
    assert "get_order_status" in prompt
    assert "invent" in prompt.lower()


def test_safety_block_requires_get_order_status_before_modify():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "modify proposal" in prompt.lower()
    assert "get_order_status(order_id)" in prompt or "get_order_status" in prompt


def test_safety_block_checks_order_editability_flags():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "order_not_editable" in prompt
    assert "cannot_cancel_order" in prompt


def test_safety_block_contains_modify_parameter_immutability():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "MODIFY PARAMETER IMMUTABILITY" in prompt
    assert "byte-for-byte" in prompt


def test_safety_block_at_most_one_proposal_block_per_message():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "at most one" in prompt.lower() or "ONE proposal block" in prompt


def test_safety_block_requires_fresh_tool_call_on_retry():
    """2026-07-10 live finding: 'retry'-phrased requests sometimes skipped the actual
    tool call and fabricated a plausible result instead (confirmed 3x independently:
    a fake TSLA quote, a fake Pine Script injection disproven by a live screenshot, a
    fake alert-creation retry). This rule closes that gap explicitly."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "TOOL RESULT FRESHNESS" in prompt
    assert "fresh tool call" in prompt.lower()
    assert "retry" in prompt.lower()


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


def test_handle_local_tool_get_live_pnl_populated():
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = {
        "account": "DU1234567.Core", "dpl": 12.5, "nl": 10000.0,
        "upl": 3.0, "uel": 9000.0, "mv": 5000.0,
    }
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "DU1234567.Core" in result
    assert "+12.50" in result
    assert "10000.00" in result


def test_handle_local_tool_get_live_pnl_none_falls_back_to_ledger():
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = None
    agent._toolkit.execute.return_value = ("Account Ledger (USD):\n  Realized P&L : +461.56", None)
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "Realized P&L" in result
    agent._toolkit.execute.assert_called_once_with("get_ledger", {})


def test_handle_local_tool_get_live_pnl_partial_fields_format_as_na():
    """A snapshot with some None numeric fields (early/partial tick) must format
    those fields as 'n/a' rather than raising a format-spec TypeError."""
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = {
        "account": "DU1234567.Core", "dpl": None, "nl": 10000.0,
        "upl": None, "uel": None, "mv": None,
    }
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "n/a" in result
    assert "10000.00" in result  # the one populated field still formats normally


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


def test_log_proposal_with_cancel_proposal():
    agent = _make_agent()
    cancel_proposal = {"order_id": "242538143", "symbol": "AAPL", "reason": "Closing test order"}
    agent._log_proposal("Some text", None, msg_id=7, cancel_proposal=cancel_proposal)
    agent._store.add_decision.assert_called_once()
    kwargs = agent._store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_cancel_proposed"
    assert "242538143" in kwargs["summary_text"]
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["message_id"] == 7


def test_log_proposal_with_modify_proposal():
    agent = _make_agent()
    modify_proposal = {"order_id": "242538143", "conid": 265598, "symbol": "AAPL", "reason": "Bumping limit"}
    agent._log_proposal("Some text", None, msg_id=8, modify_proposal=modify_proposal)
    agent._store.add_decision.assert_called_once()
    kwargs = agent._store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_modify_proposed"
    assert "242538143" in kwargs["summary_text"]
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["message_id"] == 8


def test_log_proposal_order_proposal_takes_priority_over_others():
    """If somehow more than one proposal type is passed, order_proposal wins (matches
    handle_message's elif-chain rendering priority)."""
    agent = _make_agent()
    order_proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 1, "reason": "x"}
    cancel_proposal = {"order_id": "1", "symbol": "MSFT", "reason": "y"}
    agent._log_proposal("text", order_proposal, msg_id=9, cancel_proposal=cancel_proposal)
    agent._store.add_decision.assert_called_once()
    kwargs = agent._store.add_decision.call_args.kwargs
    assert kwargs["decision_type"] == "trade_proposed"


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


# ── Prompt caching: system prompt built once per session (Task 3) ────────────

class _StubLoader:
    """Loader stub counting document reads; reload_count mimics the watchdog."""

    def __init__(self):
        self.reload_count = 0
        self.calls = 0

    def load_system_prompt(self):
        self.calls += 1
        return "# Role\nStub.\n\n# Principles\nStub."


def _make_agent_with_loader(loader):
    toolkit = MagicMock()
    toolkit.tools = []
    with patch("claudia.agent.AsyncAnthropic"):
        return ClaudIAAgent(
            toolkit=toolkit,
            store=None,
            context_loader=loader,
            session_id="test-session",
        )


def test_system_prompt_built_once_per_session():
    loader = _StubLoader()
    agent = _make_agent_with_loader(loader)
    b1 = agent._get_system_blocks()
    b2 = agent._get_system_blocks()
    assert b1 is b2           # same cached object — no rebuild between messages
    assert loader.calls == 1  # documents read exactly once per session
    assert b1[0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_rebuilt_after_reload():
    loader = _StubLoader()
    agent = _make_agent_with_loader(loader)
    agent._get_system_blocks()
    loader.reload_count += 1  # watchdog fired: a document was edited
    agent._get_system_blocks()
    assert loader.calls == 2  # rebuilt exactly once more


# ── Prompt caching: _log_cache_usage (message_start telemetry) ───────────────

import logging
from types import SimpleNamespace

from claudia.agent import _log_cache_usage


def test_log_cache_usage_reports_all_three_fields(caplog):
    usage = SimpleNamespace(
        cache_creation_input_tokens=12000,
        cache_read_input_tokens=0,
        input_tokens=450,
    )
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert "created=12000" in caplog.text
    assert "read=0" in caplog.text
    assert "uncached=450" in caplog.text


def test_log_cache_usage_warns_when_cache_inactive(caplog):
    # Both cache fields zero = caching silently failed (note: "Verification — do not skip")
    usage = SimpleNamespace(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_tokens=30000,
    )
    with caplog.at_level(logging.WARNING, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_log_cache_usage_handles_missing_fields(caplog):
    # SDK may omit the fields on models/paths without caching — must not raise
    usage = SimpleNamespace(input_tokens=100)
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert "created=0" in caplog.text


# ── Prompt caching: _with_history_cache_marker (messages breakpoint) ─────────

from claudia.agent import _with_history_cache_marker


def test_history_marker_string_content_becomes_marked_block():
    messages = [{"role": "user", "content": "hello"}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]
    # Source untouched — markers must not accumulate across tool-loop iterations
    assert messages[-1]["content"] == "hello"


def test_history_marker_block_content_marks_last_block_only():
    tool_results = [
        {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
    ]
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [{"type": "text", "text": "calling tools"}]},
        {"role": "user", "content": tool_results},
    ]
    marked = _with_history_cache_marker(messages)
    blocks = marked[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier messages and the source blocks are untouched
    assert "cache_control" not in messages[-1]["content"][-1]
    assert marked[0]["content"] == "question"


def test_history_marker_empty_messages():
    assert _with_history_cache_marker([]) == []


def test_history_marker_empty_string_content_left_alone():
    # An empty text block cannot be cached (official docs) — skip marking instead
    messages = [{"role": "user", "content": ""}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == ""


# ── SSRF: fetch_web_page redirect handling (finding S1) ──────────────────────

class _FakeResp:
    def __init__(self, status_code, headers=None, text="", url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_fetch_web_page_blocks_redirect_to_private_address():
    """A public URL that 302s to localhost must be blocked — the H-1 SSRF
    attack one hop removed (review finding S1)."""
    agent = _make_agent()
    redirect = _FakeResp(302, headers={"location": "http://localhost:5055/v1/api/portfolio/accounts"})
    secret = _FakeResp(200, text="ACCOUNT DATA")
    with patch("requests.get", side_effect=[redirect, secret]) as mock_get:
        result = agent._fetch_web_page({"url": "https://example.com/page"})
    assert "Blocked" in result
    assert "ACCOUNT DATA" not in result
    # The private target must never have been fetched
    assert mock_get.call_count == 1


def test_fetch_web_page_follows_public_redirect():
    agent = _make_agent()
    redirect = _FakeResp(301, headers={"location": "https://example.com/moved"})
    final = _FakeResp(200, text="<html><body>final public content</body></html>")
    with patch("requests.get", side_effect=[redirect, final]):
        result = agent._fetch_web_page({"url": "https://example.com/old"})
    assert "final public content" in result


def test_fetch_web_page_blocks_redirect_loop():
    agent = _make_agent()
    hop = _FakeResp(302, headers={"location": "https://example.com/again"})
    with patch("requests.get", side_effect=[hop] * 10):
        result = agent._fetch_web_page({"url": "https://example.com/loop"})
    assert "Blocked" in result and "redirect" in result.lower()
