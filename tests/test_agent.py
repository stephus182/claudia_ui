"""Tests for the ClaudIAAgent — order proposal parsing, decision extraction."""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.agent import (
    _LOCAL_TOOL_NAMES,
    _LOCAL_TOOLS,
    ClaudIAAgent,
    _build_system_prompt,
    _build_version_note,
    _history_to_messages,
    _log_cache_usage,
    _log_thinking_usage,
    _system_blocks,
    _with_cache_marker,
    _with_history_cache_marker,
)


def test_build_system_prompt_contains_safety():
    prompt = _build_system_prompt("# Role\nI am a trader assistant.\n\n# Principles\nRisk first.")
    assert "cannot place" in prompt.lower() or "CANNOT place" in prompt
    assert "propose_order" in prompt
    assert "financial advisor" in prompt.lower()


def test_build_system_prompt_contains_context():
    context = "# Role\nI am ClaudIA.\n\n# Principles\nNo YOLO trades."
    prompt = _build_system_prompt(context)
    assert "ClaudIA" in prompt
    assert "No YOLO trades" in prompt


# ── Hard Rule 1 regression (CLAUDE.md) ───────────────────────────────────────

def test_local_tool_names_excludes_order_write_tools():
    """CLAUDE.md Hard Rule 1: the LLM must never receive a callable tool for
    place_order/modify_order/cancel_order/reply_order — order execution is a
    UI-layer action triggered by a physical button click, never an LLM tool call."""
    forbidden = {"place_order", "modify_order", "cancel_order", "reply_order"}
    assert forbidden & _LOCAL_TOOL_NAMES == set()


# ── Safety block: order cancel/modify rules ──────────────────────────────────

def test_safety_block_documents_cancel_and_modify_proposal_tools():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "propose_cancel" in prompt
    assert "propose_modify" in prompt


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
            sink=MagicMock(),
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
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Get positions"},
        {"role": "tool", "content": None, "tool_name": "get_positions", "tool_result": "[...]"},
        {"role": "assistant", "content": "You hold 100 AAPL."},
    ]
    result = _history_to_messages(history)
    assert len(result) == 2
    # Return type already excludes "tool" structurally — asserted anyway as a runtime
    # regression check that survives a future loosening of that return type.
    assert all(r["role"] != "tool" for r in result)  # type: ignore[comparison-overlap]


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
            store=MagicMock(),  # unused by these tests — no doc_version passed
            context_loader=loader,
            session_id="test-session",
            sink=MagicMock(),
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
    messages: list[dict[str, Any]] = [
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


# ── handle_message() → MessageSink (Task 1.3) ───────────────────────────────

class _FakeStream:
    """Fakes AsyncAnthropic().messages.stream()'s async-context-manager + async-iterator
    shape, replaying a canned event sequence. Mirrors the SimpleNamespace-based fake-event
    pattern already used by test_log_cache_usage_* above for the same SDK event shapes."""

    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event


def _message_delta(stop_reason: str, thinking_tokens: int | None = None):
    """A message_delta event. `usage` is always present on the real event; its
    output_tokens_details is absent unless the breakdown was reported.

    `is not None`, not truthiness: thinking_tokens is a required int on
    OutputTokensDetails, so "details present, count 0" is a real and distinct API state —
    it is the diagnostic for reaching the API configured for thinking and the model
    choosing not to think.
    """
    details = (
        SimpleNamespace(thinking_tokens=thinking_tokens) if thinking_tokens is not None else None
    )
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=1400, output_tokens_details=details),
    )


def _text_response_events(text: str, stop_reason: str = "end_turn"):
    return [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        _message_delta(stop_reason),
    ]


def _make_agent_with_sink(sink=None):
    """Like _make_agent(), but returns (agent, sink) — sink defaults to a fresh MagicMock
    with async methods pre-wired as AsyncMock so callers can assert on them."""
    sink = sink or MagicMock()
    sink.send_message = AsyncMock()
    sink.send_max_tokens_warning = AsyncMock()
    sink.send_order_proposal = AsyncMock()
    sink.send_cancel_proposal = AsyncMock()
    sink.send_modify_proposal = AsyncMock()
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    store.get_history.return_value = []
    loader = MagicMock()
    loader.reload_count = 0
    loader.load_system_prompt.return_value = "# Role\nStub.\n\n# Principles\nStub."
    with patch("claudia.agent.AsyncAnthropic"):
        agent = ClaudIAAgent(
            toolkit=toolkit, store=store, context_loader=loader,
            session_id="test-session", sink=sink,
        )
    return agent, sink


@pytest.mark.asyncio
async def test_handle_message_sends_final_response_via_sink():
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Hello there."))
    )
    await agent.handle_message("Hi")
    sink.send_message.assert_awaited_once_with("Hello there.")


@pytest.mark.asyncio
async def test_handle_message_max_tokens_calls_sink_warning():
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Truncated...", stop_reason="max_tokens"))
    )
    await agent.handle_message("Hi")
    sink.send_max_tokens_warning.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_message_tool_call_uses_sink_tool_step():
    agent, sink = _make_agent_with_sink()
    tool_use_events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="t1", name="get_positions"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
        ),
        _message_delta("tool_use"),
    ]
    agent._client.messages.stream = MagicMock(
        side_effect=[
            _FakeStream(tool_use_events),
            _FakeStream(_text_response_events("You hold 100 AAPL.")),
        ]
    )
    agent._toolkit.execute = MagicMock(return_value=("100 AAPL", None))
    step_cm = MagicMock()
    step_handle = MagicMock(input="", output="")
    step_cm.__aenter__ = AsyncMock(return_value=step_handle)
    step_cm.__aexit__ = AsyncMock(return_value=False)
    sink.tool_step = MagicMock(return_value=step_cm)

    await agent.handle_message("What are my positions?")

    sink.tool_step.assert_called_once_with("get_positions")
    assert step_handle.input == json.dumps({}, indent=2)
    assert step_handle.output == "100 AAPL"
    sink.send_message.assert_awaited_once_with("You hold 100 AAPL.")


# ── Adaptive thinking: request config + thinking-block round trip (G2) ───────

def _thinking_then_tool_events(thinking: str, signature: str, tool_id: str):
    """Stream events for a turn that reasons first, then calls a tool.

    Mirrors the real event order for adaptive thinking: the thinking block opens and is
    filled by thinking_delta/signature_delta, then the tool_use block follows.
    """
    return [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="thinking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking=thinking),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="signature_delta", signature=signature),
        ),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id=tool_id, name="get_positions"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
        ),
        _message_delta("tool_use", thinking_tokens=900),
    ]


def _wire_tool_execution(agent, sink):
    """Give an agent from _make_agent_with_sink() a working tool_step + toolkit.execute."""
    step_cm = MagicMock()
    step_cm.__aenter__ = AsyncMock(return_value=MagicMock(input="", output=""))
    step_cm.__aexit__ = AsyncMock(return_value=False)
    sink.tool_step = MagicMock(return_value=step_cm)
    agent._toolkit.execute = MagicMock(return_value=("100 AAPL", None))


@pytest.fixture
def agent_with_fake_client():
    """An agent that has already completed one plain-text turn.

    Sync fixture driving the async entry point through asyncio.run so the request-config
    assertions below stay plain functions: the API call is over before they run, and
    `last_stream_kwargs` is what the SDK was actually handed.
    """
    agent, _sink = _make_agent_with_sink()
    stream = MagicMock(return_value=_FakeStream(_text_response_events("Hello there.")))
    agent._client.messages.stream = stream
    asyncio.run(agent.handle_message("Hi"))
    return SimpleNamespace(agent=agent, last_stream_kwargs=stream.call_args_list[-1].kwargs)


@pytest.fixture
def agent_with_thinking_then_tool():
    """An agent that has completed a reason → call-tool → answer turn.

    `messages_sent` is the message list of the *final* request, so `[-2]` is the
    reconstructed assistant turn the tool_result replies to.
    """
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    stream = MagicMock(side_effect=[
        _FakeStream(_thinking_then_tool_events("Check positions first.", "sig-abc", "t1")),
        _FakeStream(_text_response_events("You hold 100 AAPL.")),
    ])
    agent._client.messages.stream = stream
    asyncio.run(agent.handle_message("What are my positions?"))
    return SimpleNamespace(agent=agent, messages_sent=stream.call_args_list[-1].kwargs["messages"])


def test_stream_call_enables_adaptive_thinking(agent_with_fake_client):
    """Opus 4.8 runs WITHOUT thinking when the param is omitted — it must be explicit."""
    kwargs = agent_with_fake_client.last_stream_kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}


def test_max_tokens_leaves_room_for_thinking(agent_with_fake_client):
    """max_tokens caps thinking + text together; 4096 truncates once thinking engages."""
    assert agent_with_fake_client.last_stream_kwargs["max_tokens"] >= 16000


def test_thinking_blocks_are_echoed_back_in_the_tool_loop(agent_with_thinking_then_tool):
    """Docs: pass thinking blocks back unmodified, particularly during tool use."""
    assistant_turn = agent_with_thinking_then_tool.messages_sent[-2]
    kinds = [b["type"] for b in assistant_turn["content"]]
    assert kinds[0] == "thinking"
    assert "tool_use" in kinds


def test_echoed_thinking_block_carries_text_and_signature_unmodified(
    agent_with_thinking_then_tool,
):
    """"Unmodified" means the accumulated deltas, signature included — the signature is
    what the API verifies the reasoning against, so an empty one is worse than useless."""
    block = agent_with_thinking_then_tool.messages_sent[-2]["content"][0]
    assert block == {
        "type": "thinking",
        "thinking": "Check positions first.",
        "signature": "sig-abc",
    }


def test_signature_only_thinking_block_is_echoed_with_empty_text():
    """The actual production shape on claude-opus-4-8, where `display` defaults to omitted.

    "On models where display defaults to omitted, including claude-opus-4-8, the
    `thinking` field otherwise comes back as an empty string with only the `signature`
    populated. Either way, echo the content array back unchanged." No thinking_delta is
    streamed at all in that mode — the block opens, one signature_delta closes it — so
    this, not the populated-text case above, is the path every live turn takes.
    Source: https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows
    """
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_start", content_block=SimpleNamespace(type="thinking")
        ),
        # no thinking_delta: the server skips streaming thinking tokens entirely
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="signature_delta", signature="sig-abc"),
        ),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="t1", name="get_positions"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
        ),
        _message_delta("tool_use", thinking_tokens=900),
    ]
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(events),
        _FakeStream(_text_response_events("You hold 100 AAPL.")),
    ])
    asyncio.run(agent.handle_message("What are my positions?"))

    content = agent._client.messages.stream.call_args_list[-1].kwargs["messages"][-2]["content"]
    assert content[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def test_thinking_blocks_reset_between_tool_loop_iterations():
    """Each assistant turn echoes only its own reasoning.

    A missed per-iteration reset would replay turn 1's thinking inside turn 2's message,
    silently attributing the wrong reasoning to the second tool call.
    """
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    stream = MagicMock(side_effect=[
        _FakeStream(_thinking_then_tool_events("First.", "sig-1", "t1")),
        _FakeStream(_thinking_then_tool_events("Second.", "sig-2", "t2")),
        _FakeStream(_text_response_events("Done.")),
    ])
    agent._client.messages.stream = stream
    asyncio.run(agent.handle_message("What are my positions?"))

    # final request: [user, assistant(turn 1), tool_results, assistant(turn 2), tool_results]
    turn_two = stream.call_args_list[-1].kwargs["messages"][-2]
    thinking = [b["thinking"] for b in turn_two["content"] if b["type"] == "thinking"]
    assert thinking == ["Second."]


def test_redacted_thinking_blocks_survive_the_echo():
    """Dropping redacted_thinking while echoing its siblings is a documented 400.

    "`thinking` or `redacted_thinking` blocks in the latest assistant message cannot be
    modified" — raised precisely when code "filters content blocks by type and drops
    redacted_thinking blocks", which is what rebuilding the assistant turn does unless
    the block is carried through. Order matters too: it must stay where the API put it.
    Source: https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
    """
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    events = _thinking_then_tool_events("Check positions first.", "sig-abc", "t1")
    events.insert(4, SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="redacted_thinking", data="EncRypTed=="),
    ))
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(events),
        _FakeStream(_text_response_events("You hold 100 AAPL.")),
    ])
    asyncio.run(agent.handle_message("What are my positions?"))

    content = agent._client.messages.stream.call_args_list[-1].kwargs["messages"][-2]["content"]
    assert content[:2] == [
        {"type": "thinking", "thinking": "Check positions first.", "signature": "sig-abc"},
        {"type": "redacted_thinking", "data": "EncRypTed=="},
    ]


def test_log_thinking_usage_reports_thinking_share_of_output(caplog):
    # thinking_tokens is the only proof reasoning actually engaged — without it the
    # effect of enabling adaptive thinking cannot be measured against the baseline.
    usage = SimpleNamespace(
        output_tokens=1400,
        output_tokens_details=SimpleNamespace(thinking_tokens=900),
    )
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_thinking_usage(usage)
    assert "900" in caplog.text
    assert "1400" in caplog.text


def test_thinking_token_spend_is_logged_from_the_stream(caplog):
    """The measurement hook has to actually run inside the loop, not merely exist."""
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_thinking_then_tool_events("Check positions first.", "sig-abc", "t1")),
        _FakeStream(_text_response_events("You hold 100 AAPL.")),
    ])
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        asyncio.run(agent.handle_message("What are my positions?"))
    assert "thinking tokens: 900 of 1400" in caplog.text


def test_log_thinking_usage_silent_when_details_absent(caplog):
    # Non-final message_delta events and non-thinking models omit the field — must
    # neither raise nor claim zero thinking happened.
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_thinking_usage(SimpleNamespace(output_tokens=1400))
    assert caplog.text == ""


# ── Order proposals as strict tool calls (Task 3) ────────────────────────────
#
# The fenced ```order-proposal block is gone. A proposal is now a tool call whose
# input the API has already validated against a strict schema; agent.py only records
# it. These tests cover the wiring, the four guarantees the schema cannot express,
# and the per-turn lifecycle of the recorded proposal.

VALID_ORDER = {
    "symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "LMT",
    "limit_price": 185.0, "stop_price": None, "tif": "DAY", "sec_type": "STK",
    "conid": None, "reason": "Breakout above resistance",
}

VALID_CANCEL = {
    "order_id": "242538143", "symbol": "AAPL", "action": "BUY", "quantity": 1,
    "order_type": "LMT", "limit_price": 100.0, "stop_price": None, "tif": "GTC",
    "reason": "Closing the test order",
}

VALID_MODIFY = {
    "order_id": "242538143", "conid": 265598, "symbol": "AAPL", "action": "BUY",
    "quantity": 1, "order_type": "LMT", "limit_price": 105.0, "stop_price": None,
    "tif": "GTC", "sec_type": "STK", "reason": "Bumping the limit",
    "changes": [{"field": "limit_price", "previous_value": 100.0}],
}


@pytest.fixture
def agent():
    """A ClaudIAAgent with every dependency mocked — the same build as _make_agent()."""
    return _make_agent()


def test_propose_order_records_and_reports_acceptance(agent):
    result = agent._handle_local_tool("propose_order", VALID_ORDER)
    assert agent._pending_proposal == ("order", VALID_ORDER)
    assert "accepted" in result.lower()
    # The handler cannot know the render succeeded. Claiming it did would recreate the
    # false-confirmation failure one layer down.
    assert "displayed" not in result.lower()
    assert "rendered as a staging button" not in result.lower()


def test_propose_cancel_records_a_cancel_proposal(agent):
    result = agent._handle_local_tool("propose_cancel", VALID_CANCEL)
    assert agent._pending_proposal == ("cancel", VALID_CANCEL)
    assert "accepted" in result.lower()


def test_propose_modify_records_a_modify_proposal(agent):
    result = agent._handle_local_tool("propose_modify", VALID_MODIFY)
    assert agent._pending_proposal == ("modify", VALID_MODIFY)
    assert "accepted" in result.lower()


def test_proposal_handlers_cannot_reach_execution(agent):
    """CLAUDE.md Hard Rule 1 — the proposal tools declare, they never execute."""
    from pathlib import Path

    import claudia.proposal_tools as pt
    src = Path(pt.__file__).read_text()
    for forbidden in ("IBKRClient", "ClaudeToolkit", "place_order", "cancel_order"):
        assert forbidden not in src


def test_block_stripper_is_gone():
    import claudia.agent as a
    assert not hasattr(a, "_strip_order_proposal")
    assert not hasattr(a, "_make_block_stripper")


def test_order_proposal_schema_module_is_gone():
    """The hand validator is retired — the JSON Schema plus the handler are the contract."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("claudia.order_proposal_schema")


def test_proposal_tools_are_registered_in_the_tool_list(agent):
    names = {t["name"] for t in agent._all_tools}
    assert {"propose_order", "propose_cancel", "propose_modify"} <= names


def test_locally_handled_tools_exclude_order_write_tools():
    """Hard Rule 1 again, over the full set the agent dispatches locally — the proposal
    tools widen that set, so the guard must widen with it."""
    from claudia.agent import _LOCALLY_HANDLED
    assert {"place_order", "modify_order", "cancel_order", "reply_order"} & _LOCALLY_HANDLED == set()


# ── The four guarantees strict mode cannot express (proposal_tools.py) ───────

@pytest.mark.parametrize("quantity", [0, -5])
def test_non_positive_quantity_is_rejected(agent, quantity):
    """exclusiveMinimum is a hard 400 on the tools endpoint, so the bound lives here."""
    result = agent._handle_local_tool("propose_order", {**VALID_ORDER, "quantity": quantity})
    assert agent._pending_proposal is None
    assert "rejected" in result.lower()
    assert "no staging button" in result.lower()


@pytest.mark.parametrize("symbol", ["", "   "])
def test_blank_symbol_is_rejected(agent, symbol):
    """minLength is deliberately not used, and `"   "` would satisfy it anyway."""
    result = agent._handle_local_tool("propose_order", {**VALID_ORDER, "symbol": symbol})
    assert agent._pending_proposal is None
    assert "rejected" in result.lower()


@pytest.mark.parametrize("tool,payload", [
    ("propose_cancel", VALID_CANCEL),
    ("propose_modify", VALID_MODIFY),
])
@pytest.mark.parametrize("order_id", ["", "   "])
def test_blank_order_id_is_rejected(agent, tool, payload, order_id):
    """Acting on the wrong (or no) order is the failure mode for cancel and modify."""
    result = agent._handle_local_tool(tool, {**payload, "order_id": order_id})
    assert agent._pending_proposal is None
    assert "rejected" in result.lower()


def test_duplicate_changes_entries_are_rejected(agent):
    """uniqueItems is unsupported, so two entries for one field are schema-valid."""
    result = agent._handle_local_tool("propose_modify", {**VALID_MODIFY, "changes": [
        {"field": "limit_price", "previous_value": 100.0},
        {"field": "limit_price", "previous_value": 99.0},
    ]})
    assert agent._pending_proposal is None
    assert "rejected" in result.lower()
    assert "limit_price" in result


def test_rejection_never_repairs_the_proposal(agent):
    """Order parameters are immutable: a bad proposal is rejected whole, never normalised."""
    import copy

    payload = {**VALID_ORDER, "quantity": 0}
    before = copy.deepcopy(payload)
    agent._handle_local_tool("propose_order", payload)
    assert payload == before


# ── One proposal per turn ────────────────────────────────────────────────────

def test_second_proposal_in_one_turn_is_refused_and_the_first_survives(agent):
    agent._handle_local_tool("propose_order", VALID_ORDER)
    result = agent._handle_local_tool("propose_cancel", VALID_CANCEL)
    assert agent._pending_proposal == ("order", VALID_ORDER)
    assert "already" in result.lower()


# ── Per-turn lifecycle ───────────────────────────────────────────────────────

def _proposal_tool_events(name: str, payload: dict, tool_id: str = "p1"):
    """Stream events for a turn whose only content block is a proposal tool call."""
    return [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace())),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id=tool_id, name=name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json=json.dumps(payload)),
        ),
        _message_delta("tool_use"),
    ]


async def test_handle_message_order_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", VALID_ORDER)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Buy 10 AAPL at 185")
    sink.send_order_proposal.assert_awaited_once_with(VALID_ORDER)


async def test_handle_message_cancel_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_cancel", VALID_CANCEL)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Cancel it")
    sink.send_cancel_proposal.assert_awaited_once_with(VALID_CANCEL)


async def test_handle_message_modify_proposal_dispatches_to_sink():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_modify", VALID_MODIFY)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Move the limit to 105")
    sink.send_modify_proposal.assert_awaited_once_with(VALID_MODIFY)


async def test_proposal_tool_result_is_fed_back_to_the_model():
    """The model must see a tool_result for its own act — that feedback gap is what let
    it defend a claim that a button existed when none did."""
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", VALID_ORDER)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    agent._client.messages.stream = stream
    await agent.handle_message("Buy 10 AAPL at 185")
    tool_results = stream.call_args_list[-1].kwargs["messages"][-1]["content"]
    assert tool_results[0]["type"] == "tool_result"
    assert "accepted" in tool_results[0]["content"].lower()


async def test_rejected_proposal_renders_no_button_and_tells_the_model_why():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", {**VALID_ORDER, "quantity": 0})),
        _FakeStream(_text_response_events("That quantity is not valid.")),
    ])
    agent._client.messages.stream = stream
    await agent.handle_message("Buy 0 AAPL")
    sink.send_order_proposal.assert_not_awaited()
    tool_results = stream.call_args_list[-1].kwargs["messages"][-1]["content"]
    assert "rejected" in tool_results[0]["content"].lower()


async def test_pending_proposal_is_cleared_at_the_start_of_each_turn():
    """Reset at the TOP of handle_message: a turn that raised must not leak a stale
    proposal into the next one, where it would render a button nobody asked for."""
    agent, sink = _make_agent_with_sink()
    agent._pending_proposal = ("order", VALID_ORDER)
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Nothing to propose."))
    )
    await agent.handle_message("Just chatting")
    sink.send_order_proposal.assert_not_awaited()
    assert agent._pending_proposal is None


async def test_a_turn_that_raises_does_not_leak_its_proposal_into_the_next_turn():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", VALID_ORDER)),
        RuntimeError("stream blew up after the proposal was recorded"),
        _FakeStream(_text_response_events("Hello again.")),
    ])
    with pytest.raises(RuntimeError):
        await agent.handle_message("Buy 10 AAPL at 185")
    assert agent._pending_proposal == ("order", VALID_ORDER)  # leaked from the failed turn

    await agent.handle_message("Never mind, how are you?")
    sink.send_order_proposal.assert_not_awaited()


async def test_proposal_is_logged_as_a_decision():
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", VALID_ORDER)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Buy 10 AAPL at 185")
    kinds = [c.kwargs["decision_type"] for c in agent._store.add_decision.call_args_list]
    assert kinds == ["trade_proposed"]


# ── The system prompt points at the tools, not a text format ─────────────────

def test_safety_block_names_the_proposal_tools():
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    for tool in ("propose_order", "propose_cancel", "propose_modify"):
        assert tool in prompt


def test_safety_block_has_no_fenced_proposal_format_left():
    """Prose about a proposal creates no button — the format sections must be gone."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "```order-proposal" not in prompt
    assert "```order-cancel-proposal" not in prompt
    assert "```order-modify-proposal" not in prompt
