"""Tests for the ClaudIAAgent — order proposal parsing, decision extraction."""

import asyncio
import json
import logging
import os
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
from claudia.conversation_store import (
    COMPLETED_ORDER_ACTION_TYPES,
    RENDERED_PROPOSAL_TYPES,
    ConversationStore,
)
from tests.fixtures.failing_transcripts import (
    ANNOUNCED_THEN_STOPPED,
    DEFENDED_CLAIM_588,
    FAILED_437,
    HONEST_ACTION_TALK,
    HONEST_ACTION_TALK_REVIEW,
    HONEST_BOOK_TALK,
    HONEST_RESULT_TALK,
    HONEST_RESULT_TALK_REVIEW,
    HONEST_STAGING_TALK,
    INNOCENT,
    NARRATED_ACTION,
    NARRATED_ACTION_REVIEW,
    NARRATED_BOOK_CHECK,
    NARRATED_STAGING,
    NARRATED_TOOL_RESULT,
)


def test_build_system_prompt_contains_safety():
    """The safety block names its two load-bearing claims: no order execution, not an advisor."""
    prompt = _build_system_prompt("# Role\nI am a trader assistant.\n\n# Principles\nRisk first.")
    assert "cannot place" in prompt.lower() or "CANNOT place" in prompt
    assert "propose_order" in prompt
    assert "financial advisor" in prompt.lower()


def test_build_system_prompt_contains_context():
    """The caller's context document survives into the assembled prompt verbatim."""
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
    """Cancel and modify are named as tools, so the model cannot fall back to prose for either."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "propose_cancel" in prompt
    assert "propose_modify" in prompt


def test_safety_block_requires_order_id_provenance():
    """`order_id` must come from a named lookup tool, never from memory."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "get_live_orders" in prompt
    assert "get_order_status" in prompt
    assert "invent" in prompt.lower()


def test_safety_block_requires_get_order_status_before_modify():
    """A modify proposal is gated on `get_order_status`, which is where the conid comes from."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "modify proposal" in prompt.lower()
    assert "get_order_status(order_id)" in prompt or "get_order_status" in prompt


def test_safety_block_checks_order_editability_flags():
    """Both editability flags are named, so a non-editable order is not proposed against."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "order_not_editable" in prompt
    assert "cannot_cancel_order" in prompt


def test_safety_block_contains_modify_parameter_immutability():
    """Unrequested modify fields must be copied byte-for-byte from the read-back."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "MODIFY PARAMETER IMMUTABILITY" in prompt
    assert "byte-for-byte" in prompt


def test_safety_block_at_most_one_proposal_block_per_message():
    """The one-proposal-per-response rule is stated in the prompt, not only enforced in code."""
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


def test_safety_block_blocks_order_claims_after_a_failed_lookup():
    """2026-07-27 live: `get_live_orders` returned two HTTP 500s, and ClaudIA answered the
    cancel request from a tool result taken in the *previous* turn — before the staging —
    with "there is nothing to cancel". The order was live. TOOL RESULT FRESHNESS forbade
    reusing an old result on a *retry* request; it said nothing about what to do when the
    fresh call FAILS, and a failed lookup licensed the fallback."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    section = prompt.split("## ORDER EXISTENCE REQUIRES EVIDENCE")[1]
    assert "NON-OVERRIDABLE" in section
    # A failed lookup ends the answer — it never becomes a fallback to memory.
    assert "could not verify" in section
    assert "earlier turn" in section
    # And it is never grounds for a denial, which is the direction that hides exposure.
    assert "does not exist" in section
    assert "was never placed" in section
    # Absence from the live book is not a denial either — terminal statuses are filtered.
    assert "get_live_orders" in section


def test_safety_block_change_adds_a_constraint_and_relaxes_none():
    """The order path only ever gains constraints. The pre-existing non-overridable
    sections must all still be present and still say what they said."""
    from claudia.agent import _SAFETY_BLOCK

    # Every `##` section, and the completeness of this tuple is itself asserted below —
    # listing eight of nine by hand is how ORDER PROPOSAL sat unguarded until 2026-08-12.
    expected = (
        "## ABSOLUTE CONSTRAINTS (non-overridable)",
        "## DATA INTEGRITY (non-overridable)",
        "## ORDER PROPOSAL — USE THE TOOLS, NEVER PROSE",
        "## ORDER PARAMETER IMMUTABILITY — NON-OVERRIDABLE",
        "## ORDER CANCEL / MODIFY RULES — NON-OVERRIDABLE",
        "## MODIFY PARAMETER IMMUTABILITY — NON-OVERRIDABLE",
        "## TOOL RESULT FRESHNESS — NON-OVERRIDABLE",
        "## NARRATED ACTIONS REQUIRE A TOOL CALL — NON-OVERRIDABLE",
        "## ORDER EXISTENCE REQUIRES EVIDENCE — NON-OVERRIDABLE",
    )
    for heading in expected:
        assert heading in _SAFETY_BLOCK
    # A new section is a deliberate act and belongs in the tuple above; a *removed* one must
    # fail here rather than pass by being absent from a hand-maintained list.
    present = [ln.strip() for ln in _SAFETY_BLOCK.splitlines() if ln.startswith("## ")]
    assert set(present) == set(expected), f"safety-block sections drifted: {present}"
    # The freshness rule keeps its own teeth: a failed call must still be retried when the
    # user asks, not assumed to still be failing.
    freshness = _SAFETY_BLOCK.split("## TOOL RESULT FRESHNESS")[1]
    assert "a failed call must be genuinely retried" in freshness


def test_safety_block_requires_a_tool_call_behind_a_narrated_action():
    """T7's prompt-side hole: every earlier section governs orders or data points, and
    none governed "I performed an action". The section must state the rule, the honest
    fallback, and that a button click is not the model's own tool call — and its own
    wording must trip none of the four detectors (the block is model-visible text)."""
    from claudia.agent import (
        _SAFETY_BLOCK,
        _claims_completed_action,
        _claims_completed_proposal,
        _claims_fresh_book_check,
        _claims_verbatim_tool_result,
    )

    section = _SAFETY_BLOCK.split("## NARRATED ACTIONS REQUIRE A TOOL CALL")[1].split("## ")[0]
    assert "Announcing an action is not performing it" in section
    assert "say exactly that and stop" in section
    assert "A button the user clicks is not a tool call you made" in section
    assert _claims_completed_action(section) is None
    assert _claims_completed_proposal(section) is None
    assert _claims_fresh_book_check(section) is None
    assert _claims_verbatim_tool_result(section) is None


def _make_agent():
    """Build a ClaudIAAgent with all dependencies mocked."""
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    store.get_rendered_proposals.return_value = []
    store.get_completed_order_actions.return_value = []
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
    """User and assistant rows convert to Anthropic message dicts in order."""
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
    """An empty history yields an empty message list rather than raising."""
    assert _history_to_messages([]) == []


def test_history_to_messages_none_content_becomes_empty_string():
    """A NULL content column becomes "", never None — the API rejects a null content."""
    history = [{"role": "user", "content": None}]
    result = _history_to_messages(history)
    assert result[0]["content"] == ""


# ── _build_version_note ───────────────────────────────────────────────────────

def test_build_version_note_no_version():
    """No active version means no header line at all, not an empty-looking one."""
    assert _build_version_note(None, None) == ""
    assert _build_version_note("", None) == ""


def test_build_version_note_first_version_no_prev():
    """The first version names itself and claims no predecessor."""
    store = MagicMock()
    store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"},
    ]
    result = _build_version_note("v1", store)
    assert "v1" in result
    assert "Active document version" in result
    assert "previous" not in result


def test_build_version_note_second_version_shows_prev():
    """A later version names the one it replaced, so the model can date the rules it is under."""
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
    """An unversioned store says so plainly instead of returning an empty list."""
    agent = _make_agent()
    agent._store.list_doc_versions.return_value = []
    result = agent._handle_local_tool("list_doc_versions", {})
    assert "No document versions" in result


def test_handle_local_tool_list_versions_with_entries():
    """Every registered version is listed with its date."""
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
    """A known version returns both documents under labelled headings."""
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
    """An unknown label reports the miss and names what does exist."""
    agent = _make_agent()
    agent._store.get_doc_version.return_value = None
    agent._store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"}
    ]
    result = agent._handle_local_tool("get_doc_version", {"version": "v99"})
    assert "not found" in result.lower()
    assert "v1" in result  # available list shown


def test_handle_local_tool_unknown_name():
    """An unrouted tool name returns an explanatory string — the dispatcher never raises."""
    agent = _make_agent()
    result = agent._handle_local_tool("nonexistent_tool", {})
    assert "Unknown" in result


def test_handle_local_tool_get_live_pnl_populated():
    """A cached execution-triggered snapshot is rendered with signs and the account id."""
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
    """With no cached snapshot the ledger is pulled live — the cache starts empty each process."""
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
    """A rendered proposal writes one `trade_proposed` decision carrying its parameters."""
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
    """Plain analysis writes no decision row — only a surfaced proposal is recorded."""
    agent = _make_agent()
    agent._log_proposal("Just analysis, no trade.", None, msg_id=1)
    agent._store.add_decision.assert_not_called()


def test_log_proposal_with_cancel_proposal():
    """A cancel proposal is recorded under its own type with the order id in the summary."""
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
    """A modify proposal is recorded under its own type with the order id in the summary."""
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
    """A mid-session TradingView launch registers its tool names so calls route to the bridge."""
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
    """The tool list is the union of toolkit, TradingView extras, and the local utilities."""
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
    """Exactly one breakpoint is set, on the final entry — that caches the whole array."""
    tools = [
        {"name": "a", "input_schema": {"type": "object"}},
        {"name": "b", "input_schema": {"type": "object"}},
    ]
    marked = _with_cache_marker(tools)
    assert marked[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked[0]
    assert marked[-1]["name"] == "b"


def test_with_cache_marker_does_not_mutate_input():
    """The marker is applied to a copy: the inputs are shared module-level constants."""
    original_last = {"name": "b", "input_schema": {"type": "object"}}
    tools = [{"name": "a", "input_schema": {"type": "object"}}, original_last]
    _with_cache_marker(tools)
    # The shared dict must be untouched — _LOCAL_TOOLS / toolkit constants are module-level
    assert "cache_control" not in original_last
    assert "cache_control" not in tools[-1]


def test_with_cache_marker_empty_list():
    """An empty tool list is returned unchanged rather than indexed into."""
    assert _with_cache_marker([]) == []


def test_local_tools_constant_never_carries_cache_control():
    # Regression guard: repeated calls must not leak the marker into the module constant
    """Repeated marking never leaks a breakpoint into the `_LOCAL_TOOLS` module constant."""
    _with_cache_marker(list(_LOCAL_TOOLS))
    _with_cache_marker(list(_LOCAL_TOOLS))
    assert all("cache_control" not in t for t in _LOCAL_TOOLS)


def test_all_tools_last_entry_carries_cache_marker():
    """The assembled list carries its breakpoint on the last entry and nowhere else."""
    agent = _make_agent()
    agent._toolkit.tools = [{"name": "get_positions", "description": "", "input_schema": {}}]
    tools = agent._all_tools
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in tools[:-1])


# ── Prompt caching: _system_blocks (system breakpoint) ───────────────────────

def test_system_blocks_shape():
    """The system prompt is wrapped as one cache-marked text block."""
    blocks = _system_blocks("You are ClaudIA.")
    assert blocks == [
        {
            "type": "text",
            "text": "You are ClaudIA.",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_system_blocks_preserves_full_prompt():
    """The prompt text is byte-identical inside the block — any change invalidates the cache."""
    prompt = _build_system_prompt("# Role\nTrader assistant.\n\n# Principles\nRisk first.")
    blocks = _system_blocks(prompt)
    assert len(blocks) == 1
    assert blocks[0]["text"] == prompt  # byte-identical — any change invalidates the cache


# ── Prompt caching: system prompt built once per session (Task 3) ────────────

class _StubLoader:
    """Loader stub counting document reads; reload_count mimics the watchdog."""

    def __init__(self):
        """Start with no reloads and no reads recorded."""
        self.reload_count = 0
        self.calls = 0

    def load_system_prompt(self):
        """Count the read and return a fixed prompt, so callers can assert the read count."""
        self.calls += 1
        return "# Role\nStub.\n\n# Principles\nStub."


def _make_agent_with_loader(loader):
    """Build an agent around a stub loader, so prompt caching is observable by read count."""
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
    """The blocks are built once and returned by identity; the documents are read once."""
    loader = _StubLoader()
    agent = _make_agent_with_loader(loader)
    b1 = agent._get_system_blocks()
    b2 = agent._get_system_blocks()
    assert b1 is b2           # same cached object — no rebuild between messages
    assert loader.calls == 1  # documents read exactly once per session
    assert b1[0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_rebuilt_after_reload():
    """A watchdog reload bumps `reload_count` and is the only thing that rebuilds the blocks."""
    loader = _StubLoader()
    agent = _make_agent_with_loader(loader)
    agent._get_system_blocks()
    loader.reload_count += 1  # watchdog fired: a document was edited
    agent._get_system_blocks()
    assert loader.calls == 2  # rebuilt exactly once more


# ── Prompt caching: _log_cache_usage (message_start telemetry) ───────────────

def test_log_cache_usage_reports_all_three_fields(caplog):
    """Created, read, and uncached token counts all reach the log line."""
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
    """Both counters at zero means caching silently failed, and must warn rather than pass."""
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
    """A usage object without the cache fields logs zeros instead of raising."""
    usage = SimpleNamespace(input_tokens=100)
    with caplog.at_level(logging.INFO, logger="claudia.agent"):
        _log_cache_usage(usage)
    assert "created=0" in caplog.text


# ── Prompt caching: _with_history_cache_marker (messages breakpoint) ─────────


def test_history_marker_string_content_becomes_marked_block():
    """A plain string turn becomes a marked text block, leaving the caller's list untouched."""
    messages = [{"role": "user", "content": "hello"}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]
    # Source untouched — markers must not accumulate across tool-loop iterations
    assert messages[-1]["content"] == "hello"


def test_history_marker_block_content_marks_last_block_only():
    """Only the final content block is marked, so the breakpoint sits at the end of the prefix."""
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
    """An empty message list is returned unchanged."""
    assert _with_history_cache_marker([]) == []


def test_history_marker_empty_string_content_left_alone():
    # An empty text block cannot be cached (official docs) — skip marking instead
    """An empty text block cannot be cached, so it is left unmarked rather than marked."""
    messages = [{"role": "user", "content": ""}]
    marked = _with_history_cache_marker(messages)
    assert marked[-1]["content"] == ""


# ── SSRF: fetch_web_page redirect handling (finding S1) ──────────────────────

class _FakeResp:
    """The subset of `requests.Response` that `_fetch_web_page` reads."""
    def __init__(self, status_code, headers=None, text="", url=""):
        """Record the response fields `_fetch_web_page` actually reads."""
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url

    def raise_for_status(self):
        """Raise on a 4xx/5xx status, matching `requests.Response`'s contract."""
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
    """A redirect to another public URL is followed manually and its content returned."""
    agent = _make_agent()
    redirect = _FakeResp(301, headers={"location": "https://example.com/moved"})
    final = _FakeResp(200, text="<html><body>final public content</body></html>")
    with patch("requests.get", side_effect=[redirect, final]):
        result = agent._fetch_web_page({"url": "https://example.com/old"})
    assert "final public content" in result


def test_fetch_web_page_blocks_redirect_loop():
    """A redirect chain past the hop limit is refused instead of followed forever."""
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
        """Hold the event sequence this stream will replay."""
        self._events = events

    async def __aenter__(self):
        """Enter the async context, returning the stream itself as the SDK does."""
        return self

    async def __aexit__(self, *exc):
        """Leave exceptions to propagate — the real stream suppresses none."""
        return False

    async def __aiter__(self):
        """Replay the recorded events in order."""
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
    """The event sequence for a plain text reply, with no tool call."""
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
    store.get_rendered_proposals.return_value = []
    store.get_completed_order_actions.return_value = []
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
    """The assembled assistant text reaches the sink exactly once."""
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Hello there."))
    )
    await agent.handle_message("Hi")
    sink.send_message.assert_awaited_once_with("Hello there.")


@pytest.mark.asyncio
async def test_handle_message_max_tokens_calls_sink_warning():
    """A `max_tokens` stop reason surfaces the truncation warning to the user."""
    agent, sink = _make_agent_with_sink()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events("Truncated...", stop_reason="max_tokens"))
    )
    await agent.handle_message("Hi")
    sink.send_max_tokens_warning.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_message_tool_call_uses_sink_tool_step():
    """A tool call opens a sink tool step rather than writing to the feed directly."""
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
    """Thinking tokens are logged against total output — the only proof reasoning engaged."""
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
    """A usage object without the breakdown logs nothing, rather than a misleading zero."""
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
    """Acceptance is reported without claiming the button rendered — this cannot know that."""
    result = agent._handle_local_tool("propose_order", VALID_ORDER)
    assert agent._pending_proposal == ("order", VALID_ORDER)
    assert "accepted" in result.lower()
    # The handler cannot know the render succeeded. Claiming it did would recreate the
    # false-confirmation failure one layer down.
    assert "displayed" not in result.lower()
    assert "rendered as a staging button" not in result.lower()


def test_propose_cancel_records_a_cancel_proposal(agent):
    """A cancel proposal is recorded under the "cancel" kind and acknowledged."""
    result = agent._handle_local_tool("propose_cancel", VALID_CANCEL)
    assert agent._pending_proposal == ("cancel", VALID_CANCEL)
    assert "accepted" in result.lower()


def test_propose_modify_records_a_modify_proposal(agent):
    """A modify proposal is recorded under the "modify" kind and acknowledged."""
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
    """The retired text-block parser stays retired — proposals are tool calls, with no text form."""
    import claudia.agent as a
    assert not hasattr(a, "_strip_order_proposal")
    assert not hasattr(a, "_make_block_stripper")


def test_order_proposal_schema_module_is_gone():
    """The hand validator is retired — the JSON Schema plus the handler are the contract."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("claudia.order_proposal_schema")


def test_proposal_tools_are_registered_in_the_tool_list(agent):
    """All three proposal tools are actually offered to the model."""
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
    """Only one proposal may be pending per turn; the second is refused and the first is kept."""
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
    """A recorded order proposal is handed to the sink unmodified."""
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_order", VALID_ORDER)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Buy 10 AAPL at 185")
    sink.send_order_proposal.assert_awaited_once_with(VALID_ORDER)


async def test_handle_message_cancel_proposal_dispatches_to_sink():
    """A recorded cancel proposal is handed to the sink unmodified."""
    agent, sink = _make_agent_with_sink()
    _wire_tool_execution(agent, sink)
    agent._client.messages.stream = MagicMock(side_effect=[
        _FakeStream(_proposal_tool_events("propose_cancel", VALID_CANCEL)),
        _FakeStream(_text_response_events("Ready when you are.")),
    ])
    await agent.handle_message("Cancel it")
    sink.send_cancel_proposal.assert_awaited_once_with(VALID_CANCEL)


async def test_handle_message_modify_proposal_dispatches_to_sink():
    """A recorded modify proposal is handed to the sink unmodified."""
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
    """A defective proposal renders nothing and returns an honest refusal the model can act on."""
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
    """A proposal from a turn that died is cleared before the next turn can render it."""
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
    """A rendered proposal writes exactly one `trade_proposed` decision, not one per loop pass."""
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
    """All three tool names appear, so the model has no reason to invent a text format."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    for tool in ("propose_order", "propose_cancel", "propose_modify"):
        assert tool in prompt


def test_safety_block_has_no_fenced_proposal_format_left():
    """Prose about a proposal creates no button — the format sections must be gone."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "```order-proposal" not in prompt
    assert "```order-cancel-proposal" not in prompt
    assert "```order-modify-proposal" not in prompt


def test_safety_block_has_no_stale_fenced_block_instruction():
    """ABSOLUTE CONSTRAINTS must not still point the model at the retired text protocol.

    It read "output an order-proposal block (see format below)" while the section below
    now says there is no text format. A system prompt that contradicts itself on this
    exact point is what the 2026-07-17 / 2026-07-24 failures looked like from the
    model's side, so the contradiction is not cosmetic.
    """
    from claudia.agent import _SAFETY_BLOCK

    assert "order-proposal block" not in _SAFETY_BLOCK
    assert "see format below" not in _SAFETY_BLOCK


def test_absolute_constraints_still_route_trades_through_the_tools():
    """The constraint itself must survive the rewording — no execution, human clicks."""
    from claudia.agent import _SAFETY_BLOCK

    constraints = _SAFETY_BLOCK.split("## ABSOLUTE CONSTRAINTS")[1].split("## DATA INTEGRITY")[0]
    assert "CANNOT place, modify, or cancel any order" in constraints
    assert "no tools for order execution" in constraints
    assert "propose_order" in constraints
    assert "must explicitly click" in constraints


# ── L1: render-completion invariant (Task 5) ─────────────────────────────────
#
# On 2026-07-17 and 2026-07-24 ClaudIA told the user a staging button existed when none
# did. The model had emitted a valid, parsed proposal in 5 of 5 inspectable failures —
# the render path discarded it between persisting the reply text and writing the decision
# row, and nothing ever told the model. Task 3 made the four *parsing* crash vectors
# impossible; it did not make rendering reliable. These tests pin the guarantee that a
# parsed proposal implies a rendered button, or ClaudIA says so plainly.


class _RecordingSink:
    """A MessageSink stand-in that records what actually reached the user.

    `proposal_error` makes all three proposal renderers raise, standing in for a sink that
    blows up mid-render (the 2026-07-17 shape). With it unset the sink renders normally
    and records the proposal, so the same class covers the success path.
    """

    def __init__(self, proposal_error: Exception | None = None) -> None:
        """Record what was sent; `proposal_error` makes each render raise, as a broken sink."""
        self.messages: list[str] = []
        self.proposals: list[tuple[str, dict]] = []
        self._error = proposal_error

    async def send_message(self, text: str) -> None:
        """Capture the assistant text instead of rendering it."""
        self.messages.append(text)

    async def send_max_tokens_warning(self) -> None:
        """Capture the truncation warning as a marker line."""
        self.messages.append("[max tokens]")

    def tool_step(self, name: str):
        """Return a no-op async step handle, so the tool loop can run without a UI."""
        step = MagicMock()
        step.__aenter__ = AsyncMock(return_value=MagicMock(input="", output=""))
        step.__aexit__ = AsyncMock(return_value=False)
        return step

    async def _render(self, kind: str, proposal: dict) -> None:
        """Record the proposal, or raise the configured error to simulate a render that fails."""
        if self._error is not None:
            raise self._error
        self.proposals.append((kind, proposal))

    async def send_order_proposal(self, proposal: dict) -> None:
        """Route a new-order proposal through the shared recorder."""
        await self._render("order", proposal)

    async def send_cancel_proposal(self, proposal: dict) -> None:
        """Route a cancel proposal through the shared recorder."""
        await self._render("cancel", proposal)

    async def send_modify_proposal(self, proposal: dict) -> None:
        """Route a modify proposal through the shared recorder."""
        await self._render("modify", proposal)


class _FakeStore:
    """A ConversationStore stand-in that really records what it is given.

    MagicMock cannot answer `get_decisions()` with what `add_decision()` was called with,
    and the invariant under test is precisely a claim about what ends up in the transcript
    and the decisions table. `get_history` replays what was stored, so the operator-channel
    tests see a realistic message list rather than an empty one.
    """

    def __init__(self) -> None:
        """Start with no messages and no decisions."""
        self.messages: list[dict] = []
        self.decisions: list[dict] = []

    def add_message(self, session_id: str, role: str, content: str = "", **kwargs) -> int:
        """Append a message row and return its 1-based id, matching the real store's contract.

        `tool_name` is kept because the called-tool ledger is built from it; a double that
        dropped it would keep an empty ledger green forever.
        """
        self.messages.append({
            "session_id": session_id, "role": role, "content": content,
            "tool_name": kwargs.get("tool_name"),
        })
        return len(self.messages)

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        """Return this session's user/assistant rows, newest-limited, oldest first."""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.messages
            if m["session_id"] == session_id
        ][-limit:]

    def add_decision(self, **kwargs) -> int:
        """Append a decision row verbatim and return its 1-based id."""
        self.decisions.append(kwargs)
        return len(self.decisions)

    def get_decisions(self, session_id: str) -> list[dict]:
        """Return every decision recorded for one session."""
        return [d for d in self.decisions if d["session_id"] == session_id]

    def get_rendered_proposals(self, session_id: str) -> list[dict]:
        """Mirrors the real query's two filters — allowlist + message_id. The SQL itself is
        pinned in tests/test_conversation_store.py; this double must not drift from it."""
        return [
            d for d in self.decisions
            if d["session_id"] == session_id
            and d.get("decision_type") in RENDERED_PROPOSAL_TYPES
            and d.get("message_id") is not None
        ]

    def get_completed_order_actions(self, session_id: str) -> list[dict]:
        """Mirrors the real query: allowlist only, and deliberately **no** message_id
        filter — `order_flow` writes these rows without one, because a button click belongs
        to no assistant turn."""
        return [
            d for d in self.decisions
            if d["session_id"] == session_id
            and d.get("decision_type") in COMPLETED_ORDER_ACTION_TYPES
        ]

    def get_called_tool_names(self, session_id: str) -> list[str]:
        """Mirrors the real query: `role='tool'` rows only, blank names dropped, distinct,
        sorted alphabetically. The SQL itself is pinned in tests/test_conversation_store.py;
        this double must not drift from it."""
        return sorted({
            m["tool_name"] for m in self.messages
            if m["session_id"] == session_id
            and m["role"] == "tool"
            and (m.get("tool_name") or "").strip()
        })

    def list_doc_versions(self) -> list[dict]:
        """No versions are registered — these tests never exercise the version note."""
        return []


def _make_agent_recording(proposal_error: Exception | None = None, *, store: Any = None):
    """An agent wired to a recording sink and, by default, a recording store.

    `store` overrides the double — used by the tool-ledger leak test, whose claim is about
    the real SQL and the real `tool_input_json` / `tool_result_json` columns rather than
    about which fields a double happens to keep.
    """
    sink = _RecordingSink(proposal_error)
    toolkit = MagicMock()
    toolkit.tools = []
    loader = MagicMock()
    loader.reload_count = 0
    loader.load_system_prompt.return_value = "# Role\nStub.\n\n# Principles\nStub."
    with patch("claudia.agent.AsyncAnthropic"):
        agent = ClaudIAAgent(
            toolkit=toolkit, store=store if store is not None else _FakeStore(),
            context_loader=loader, session_id="test-session", sink=sink,
        )
    return agent, sink


def _proposal_turn(name: str, payload: dict, reply: str) -> list:
    """The two streams of one turn: the proposal tool call, then the reply text."""
    return [
        _FakeStream(_proposal_tool_events(name, payload)),
        _FakeStream(_text_response_events(reply)),
    ]


def _system_texts(messages: list) -> list[str]:
    """Every `role: "system"` message body, whether plain string or marked text blocks."""
    out = []
    for m in messages:
        if m["role"] != "system":
            continue
        content = m["content"]
        out.append(content if isinstance(content, str) else "".join(b["text"] for b in content))
    return out


async def test_render_failure_produces_an_honest_notice():
    """The user must be told, in the same feed carrying the model's claim."""
    agent, sink = _make_agent_recording(RuntimeError("sink blew up mid-render"))
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_order", VALID_ORDER, FAILED_437)
    )
    await agent.handle_message("stage it")

    assert sink.messages[0] == FAILED_437  # the claim still stands in the transcript
    notice = sink.messages[-1]
    assert "no staging button" in notice.lower()
    assert "nothing has been staged" in notice.lower()


async def test_render_failure_writes_the_new_decision_type():
    """Deliberately NOT trade_proposed — that type must keep meaning 'a button was shown'."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_order", VALID_ORDER, FAILED_437)
    )
    await agent.handle_message("stage it")

    decisions = agent._store.get_decisions("test-session")
    types = [d["decision_type"] for d in decisions]
    assert "proposal_render_failed" in types
    assert "trade_proposed" not in types
    row = next(d for d in decisions if d["decision_type"] == "proposal_render_failed")
    assert row["metadata"] == {"kind": "order"}
    assert "nothing staged" in row["summary_text"]


async def test_render_failure_notice_is_persisted_not_only_displayed():
    """The transcript is what the FTS tool, the session report and the next turn read."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_order", VALID_ORDER, FAILED_437)
    )
    await agent.handle_message("stage it")

    stored = [m["content"] for m in agent._store.messages if m["role"] == "assistant"]
    assert any("No staging button was created" in text for text in stored)


async def test_silent_skip_also_trips_the_invariant():
    """Vector 4: no exception, nothing rendered. A try/except alone cannot catch this.

    The original defect was a *falsy parsed value* that skipped the render with no
    exception at all. Its post-Task-3 equivalent is a recorded proposal whose kind routes
    to no renderer: every dispatch branch is skipped and nothing raises. An implementation
    that sets its `rendered` flag after the branch chain rather than inside the branch that
    awaited would call that a success — which is exactly the bug.
    """
    import claudia.agent as agent_mod

    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_order", VALID_ORDER, FAILED_437)
    )
    with patch.dict(agent_mod._PROPOSAL_KINDS, {"propose_order": "no_such_kind"}):
        await agent.handle_message("stage it")

    assert sink.proposals == []  # no renderer ran — and none raised either
    assert "no staging button" in sink.messages[-1].lower()
    types = [d["decision_type"] for d in agent._store.get_decisions("test-session")]
    assert types == ["proposal_render_failed"]


async def test_notice_uses_the_operator_channel():
    """A model-authored look-alike would be indistinguishable; role:"system" cannot be forged."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_order", VALID_ORDER, FAILED_437),
        _FakeStream(_text_response_events("Nothing is staged.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("stage it")          # render fails, note queued
    await agent.handle_message("is it staged?")     # note delivered to the model

    messages = stream.call_args_list[-1].kwargs["messages"]
    notes = _system_texts(messages)
    assert len(notes) == 1
    assert "failed to render" in notes[0]
    assert "Do not tell the user it was staged" in notes[0]


async def test_operator_note_placement_satisfies_the_api_rule():
    """A system message may not be messages[0]; it must follow a user turn."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_order", VALID_ORDER, FAILED_437),
        _FakeStream(_text_response_events("Nothing is staged.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("stage it")
    await agent.handle_message("is it staged?")

    messages = stream.call_args_list[-1].kwargs["messages"]
    idx = next(i for i, m in enumerate(messages) if m["role"] == "system")
    assert idx > 0
    assert messages[idx - 1]["role"] == "user"


async def test_operator_note_is_sent_once_then_cleared():
    """Otherwise every later turn would keep re-announcing a failure already handled."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_order", VALID_ORDER, FAILED_437),
        _FakeStream(_text_response_events("Nothing is staged.")),
        _FakeStream(_text_response_events("Understood.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("stage it")
    await agent.handle_message("is it staged?")
    await agent.handle_message("ok, moving on")

    assert _system_texts(stream.call_args_list[-1].kwargs["messages"]) == []
    assert agent._pending_operator_notes == []


async def test_successful_render_still_logs_trade_proposed():
    """No regression: a rendered button logs exactly what it always logged."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_order", VALID_ORDER, "Ready when you are.")
    )
    await agent.handle_message("Buy 10 AAPL at 185")

    assert sink.proposals == [("order", VALID_ORDER)]
    types = [d["decision_type"] for d in agent._store.get_decisions("test-session")]
    assert types == ["trade_proposed"]
    assert not any("no staging button" in m.lower() for m in sink.messages)
    assert agent._pending_operator_notes == []


@pytest.mark.parametrize("kind,tool,payload", [
    ("cancel", "propose_cancel", VALID_CANCEL),
    ("modify", "propose_modify", VALID_MODIFY),
])
async def test_cancel_and_modify_render_failures_trip_the_invariant(kind, tool, payload):
    """A render failure on any kind contradicts the claim, records it, and names the kind."""
    agent, sink = _make_agent_recording(RuntimeError("boom"))
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn(tool, payload, DEFENDED_CLAIM_588)
    )
    await agent.handle_message("do it")

    assert "no staging button" in sink.messages[-1].lower()
    row = agent._store.get_decisions("test-session")[0]
    assert row["decision_type"] == "proposal_render_failed"
    assert row["metadata"] == {"kind": kind}


@pytest.mark.parametrize("text", INNOCENT)
async def test_innocent_messages_never_trip_the_guardrail(text):
    """Staging vocabulary with no proposal must stay untouched — the guardrail keys on
    the recorded proposal, never on prose (the dropped prose detector was 81% false
    positives)."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(text))
    )
    await agent.handle_message("what's the status?")

    assert sink.messages == [text]
    assert agent._store.get_decisions("test-session") == []
    assert agent._pending_operator_notes == []


async def test_rejected_proposal_does_not_raise_the_render_guardrail():
    """A refusal is not a render failure, and must not be dressed as one.

    `_proposal_defect` refuses before the model writes its user-facing text, so the model
    can still say what went wrong. The guardrail notice would claim the proposal "was
    accepted but could not be rendered" — false here — and `proposal_render_failed` would
    stop meaning "accepted but not rendered". See `_record_proposal`'s docstring.
    """
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn(
            "propose_order", {**VALID_ORDER, "quantity": 0}, "That quantity is not valid."
        )
    )
    await agent.handle_message("Buy 0 AAPL")

    assert sink.proposals == []
    assert not any("no staging button" in m.lower() for m in sink.messages)
    assert agent._store.get_decisions("test-session") == []
    assert agent._pending_operator_notes == []


def test_operator_note_extends_the_prefix_instead_of_editing_it():
    """A note inserted *into* the replayed history would invalidate the messages cache
    breakpoint on every turn thereafter. It is appended after it, so the replayed prefix
    stays byte-identical and the cache is extended rather than rebuilt."""
    agent, _sink = _make_agent_recording()
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "stage it"},
    ]
    baseline = _history_to_messages(history)
    with_note = _history_to_messages(history)
    agent._pending_operator_notes.append("note")
    agent._append_operator_message(with_note)

    assert with_note[: len(baseline)] == baseline
    assert with_note[-1] == {"role": "system", "content": "note"}


def test_cache_breakpoint_lands_on_the_operator_note_when_it_is_last():
    """Documents the coupling the live check covers.

    On a turn's first request the note IS the last message, so the messages-level
    breakpoint is placed on the note's own text block. That marked-system-message shape
    therefore has to be accepted by the API — see
    test_live_api_accepts_mid_conversation_system_message, shape 2.
    """
    marked = _with_history_cache_marker([
        {"role": "user", "content": "stage it"},
        {"role": "system", "content": "note"},
    ])
    assert marked[-1]["role"] == "system"
    assert marked[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_operator_note_is_dropped_rather_than_misplaced(caplog):
    """Defensive branch: a system message that does not follow a user turn is a 400 that
    kills the whole turn, and the notice has already reached the user and the DB."""
    agent, _sink = _make_agent_recording()
    agent._pending_operator_notes.append("note")
    messages = [{"role": "assistant", "content": "hi"}]
    with caplog.at_level(logging.ERROR, logger="claudia.agent"):
        agent._append_operator_message(messages)

    assert messages == [{"role": "assistant", "content": "hi"}]
    assert agent._pending_operator_notes == []
    assert "Operator note dropped" in caplog.text


def test_guardrail_notice_never_claims_something_was_staged():
    """The notice contradicts a claim; wording that hedges would leave it standing."""
    from claudia.agent import _GUARDRAIL_NOTICE

    lowered = _GUARDRAIL_NOTICE.lower()
    assert "no staging button was created" in lowered
    assert "nothing has been staged and no order exists" in lowered
    assert "may have" not in lowered
    assert "might" not in lowered


# ── L3: emission records in replayed history ─────────────────────────────────
#
# `_history_to_messages` drops tool rows, so after the tool migration the model still had
# no cross-turn evidence it had ever proposed anything: on turn N+1 its only trace of turn
# N's propose_order was its own prose. That is what let it defend "the modify proposal is
# staged above" and, eventually, quote an order id it had invented. The emission records
# put the evidence back — as `role: "system"`, which the model cannot forge, and as
# identity only, so nothing in them can be copied into a later proposal.

# Synthetic throughout (tests/fixtures/failing_transcripts.py sets the precedent): no real
# order ids, and deliberately distinctive numbers so "did a price leak into the record?"
# is decidable by substring.
SYNTHETIC_ORDER = {
    "symbol": "ZZZ", "action": "BUY", "quantity": 7, "order_type": "LMT",
    "limit_price": 333.25, "stop_price": None, "tif": "DAY", "sec_type": "STK",
    "conid": None, "reason": "synthetic breakout",
}
SYNTHETIC_CANCEL = {
    "order_id": "9990001111", "symbol": "YYY", "action": "SELL", "quantity": 8,
    "order_type": "LMT", "limit_price": 444.5, "stop_price": None, "tif": "GTC",
    "reason": "synthetic cleanup",
}
SYNTHETIC_MODIFY = {
    "order_id": "8880002222", "conid": 111222, "symbol": "XXX", "action": "BUY",
    "quantity": 9, "order_type": "LMT", "limit_price": 555.75, "stop_price": None,
    "tif": "GTC", "sec_type": "STK", "reason": "synthetic bump",
    "changes": [{"field": "limit_price", "previous_value": 550.0}],
}


def _seed_rendered(agent, decision_type: str, payload: dict) -> None:
    """Record a proposal exactly as a *successful* render does — via _log_proposal's shape."""
    agent._store.add_decision(
        session_id="test-session", decision_type=decision_type, summary_text="seeded",
        symbol=payload.get("symbol"), message_id=1, metadata={"order": payload},
    )


def _operator_message(agent) -> str:
    """The single operator-channel system message the agent would append this turn."""
    messages = [{"role": "user", "content": "and now?"}]
    agent._append_operator_message(messages)
    texts = _system_texts(messages)
    return texts[0] if texts else ""


def test_emission_record_names_the_tool_and_the_order_id():
    """The order id is the whole point: it is the one fact the model most needs returned
    and the one it invented when it was not."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_cancel_proposed", SYNTHETIC_CANCEL)
    body = _operator_message(agent)

    assert "propose_cancel" in body
    assert "9990001111" in body
    assert "YYY" in body


def test_emission_record_covers_all_three_proposal_kinds():
    """Each kind renders its own identity line, in emission order."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_rendered(agent, "trade_cancel_proposed", SYNTHETIC_CANCEL)
    _seed_rendered(agent, "trade_modify_proposed", SYNTHETIC_MODIFY)
    body = _operator_message(agent)

    assert "propose_order for ZZZ" in body
    assert "propose_cancel for order 9990001111 (YYY)" in body
    assert "propose_modify for order 8880002222 (XXX)" in body
    # Emission order is the order they were emitted in.
    assert body.index("propose_order") < body.index("propose_cancel") < body.index("propose_modify")


def test_emission_record_carries_no_pricing():
    """Identity only. Anything copyable into a later proposal is a fabrication surface —
    the record answers "what did you do", never "what were the values"."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_rendered(agent, "trade_cancel_proposed", SYNTHETIC_CANCEL)
    _seed_rendered(agent, "trade_modify_proposed", SYNTHETIC_MODIFY)
    body = _operator_message(agent)

    assert "$" not in body
    assert "limit" not in body.lower()
    assert "quantity" not in body.lower()
    # Order ids are the one number allowed through; strip them, then no digits may remain.
    stripped = body.replace("9990001111", "").replace("8880002222", "")
    assert not any(ch.isdigit() for ch in stripped)
    for value in ("7", "333.25", "8", "444.5", "9", "555.75", "550.0", "111222"):
        assert value not in stripped
    # And no free-text reason, which the model writes and could seed anything into.
    assert "synthetic" not in body.lower()


def test_no_proposals_produces_no_system_message():
    """An empty record block would be a message asserting nothing, on a channel whose
    value is that everything on it is load-bearing."""
    agent, _sink = _make_agent_recording()
    messages = [{"role": "user", "content": "hello"}]
    agent._append_operator_message(messages)

    assert messages == [{"role": "user", "content": "hello"}]
    assert agent._emission_records() == ""


async def test_render_failure_is_never_replayed_as_an_emission_record():
    """The critical negative. A `proposal_render_failed` row means no button exists;
    replaying it as an emission record would re-state, on the non-spoofable channel, the
    precise false claim this whole guardrail was built to delete."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_order", VALID_ORDER, FAILED_437),
        _FakeStream(_text_response_events("Nothing is staged.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("stage it")
    await agent.handle_message("is it staged?")

    types = [d["decision_type"] for d in agent._store.get_decisions("test-session")]
    assert types == ["proposal_render_failed"]
    body = "".join(_system_texts(stream.call_args_list[-1].kwargs["messages"]))
    assert "failed to render" in body            # the Task 5 note is there
    assert "propose_order" not in body           # but no emission record is
    assert "already emitted" not in body


async def test_rendered_proposal_reappears_as_an_emission_record_next_turn():
    """The behaviour the task exists for: turn N's proposal is visible on turn N+1."""
    agent, _sink = _make_agent_recording()
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_cancel", VALID_CANCEL, "Button's up."),
        _FakeStream(_text_response_events("Still waiting on you.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("cancel it")
    # Turn 1 must NOT carry its own record: it is built from history before the proposal
    # is made, and claiming a button before rendering one is the failure being closed.
    assert _system_texts(stream.call_args_list[0].kwargs["messages"]) == []

    await agent.handle_message("is the button there?")
    body = "".join(_system_texts(stream.call_args_list[-1].kwargs["messages"]))
    assert f"propose_cancel for order {VALID_CANCEL['order_id']}" in body


def test_emission_records_are_byte_stable_across_calls():
    """Rebuilt from persisted rows every turn, so any instability would rewrite the
    request body turn after turn and thrash the prompt cache."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_rendered(agent, "trade_modify_proposed", SYNTHETIC_MODIFY)

    first = agent._emission_records()
    assert first != ""
    assert [agent._emission_records() for _ in range(5)] == [first] * 5
    assert _operator_message(agent) == _operator_message(agent)


def test_emission_record_message_follows_a_user_turn_and_is_not_first():
    """The API's placement rule: a system message may not be messages[0] and must follow a
    user turn. The plan's original design (one record after each assistant turn) is a 400."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    messages = _history_to_messages([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and now?"},
    ])
    agent._append_operator_message(messages)

    idx = next(i for i, m in enumerate(messages) if m["role"] == "system")
    assert idx > 0
    assert messages[idx - 1]["role"] == "user"
    assert idx == len(messages) - 1


def test_emission_records_extend_the_prefix_instead_of_editing_it():
    """Records are appended after the current user turn, never woven into the replayed
    history — so the messages-level cache breakpoint cannot be invalidated by them."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and now?"},
    ]
    baseline = _history_to_messages(history)
    extended = _history_to_messages(history)
    agent._append_operator_message(extended)

    assert extended[: len(baseline)] == baseline
    assert len(extended) == len(baseline) + 1


async def test_a_note_and_records_share_one_system_message():
    """Two consecutive system messages would put the second after a system turn rather
    than a user turn — outside the probed placement rule. One message, both payloads."""
    agent, sink = _make_agent_recording()
    stream = MagicMock(side_effect=[
        *_proposal_turn("propose_order", VALID_ORDER, "Button's up."),      # renders
        *_proposal_turn("propose_cancel", VALID_CANCEL, DEFENDED_CLAIM_588),  # fails
        _FakeStream(_text_response_events("Understood.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("buy it")
    sink._error = RuntimeError("boom")
    await agent.handle_message("cancel it")
    await agent.handle_message("where do we stand?")

    texts = _system_texts(stream.call_args_list[-1].kwargs["messages"])
    assert len(texts) == 1
    assert "propose_order for AAPL" in texts[0]   # the rendered one
    assert "failed to render" in texts[0]         # the Task 5 note
    assert "propose_cancel" not in texts[0]       # the failed one is not an emission


# ── completed order actions in the operator channel ──────────────────────────
#
# 2026-07-27, live: an ES order was staged through both gates and read back as Submitted.
# Minutes later get_live_orders returned two HTTP 500s, and ClaudIA — with no fresh
# evidence — fell back on a tool result from *before* the staging and told the user "there
# is nothing to cancel; the ES order was only ever a staged button". The order was live.
# Staging is a button click: no tool call, no assistant message, nothing in the replayed
# transcript. These records are the missing evidence, on the channel the model cannot forge.

SYNTHETIC_STAGED_ID = "1230000456"


def _seed_completed(agent, decision_type: str, *, confirmed: bool, state: str | None,
                    order_id: str | None = SYNTHETIC_STAGED_ID, symbol: str = "ZZZ") -> None:
    """Record a completed action exactly as `order_flow` does after both gates pass.

    Same metadata shape as `_execute_staged_order_core`'s decision row, including the
    `proposal` key — whose prices and quantities must never reach the record.
    """
    agent._store.add_decision(
        session_id="test-session", decision_type=decision_type, summary_text="seeded",
        symbol=symbol,
        metadata={
            "proposal": SYNTHETIC_ORDER,
            "ibkr_order_id": order_id,
            "readback_confirmed": confirmed,
            "readback_order_status": state,
        },
    )


def test_completed_staging_is_replayed_with_its_ibkr_order_id():
    """The incident test. The staging left no trace in the transcript; this record is the
    only thing that can stop "there is nothing to cancel"."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    body = _operator_message(agent)

    assert SYNTHETIC_STAGED_ID in body
    assert "ZZZ" in body
    assert "Submitted" in body
    assert "IBKR" in body


def test_confirmed_staging_reads_as_an_order_that_exists():
    """A proposal record says "you offered a button". A staging record has to say something
    strictly stronger — the order is at IBKR — or it cannot contradict a denial."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    body = _operator_message(agent)

    assert "CONFIRMED" in body
    assert "NOT confirmed" not in body


def test_unconfirmed_staging_is_neither_a_confirmation_nor_a_denial():
    """readback_confirmed=False still means the dispatch reached IBKR — the order may be
    working. Reporting it as confirmed invents an order; reporting it as nothing re-creates
    the exact denial this change exists to stop. It has to read as "it reached IBKR, its
    state is unverified, go and check"."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=False, state="PendingSubmit")
    body = _operator_message(agent)

    assert SYNTHETIC_STAGED_ID in body
    assert "reached IBKR" in body
    assert "did NOT confirm" in body
    assert "may be live" in body
    assert "PendingSubmit" in body


def test_staging_with_nothing_observed_still_says_it_reached_ibkr():
    """The 500-storm shape: dispatch accepted, read-back saw nothing at all."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=False, state=None)
    body = _operator_message(agent)

    assert "reached IBKR" in body
    assert "nothing was observed" in body


def test_staging_without_an_order_id_is_still_recorded():
    """IBKR returned no order id — the least verifiable outcome there is, and the one where
    silence would be most dangerous."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=False, state=None, order_id=None)
    body = _operator_message(agent)

    assert "no order id" in body
    assert "reached IBKR" in body


def test_completed_records_cover_cancel_and_modify_too():
    """A cancel and a modify are button clicks as well, equally invisible in the transcript,
    and each carries its own read-back verdict."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    _seed_completed(agent, "trade_cancelled", confirmed=True, state="Cancelled")
    _seed_completed(agent, "trade_modified", confirmed=False, state="Submitted")
    body = _operator_message(agent)

    assert "PLACED order" in body
    assert "CANCEL SENT for order" in body
    assert "MODIFY SENT for order" in body
    assert body.index("PLACED") < body.index("CANCEL SENT") < body.index("MODIFY SENT")


def test_completed_record_carries_no_pricing():
    """Identity and observed state only — same rule as the proposal records. The decision
    row's metadata holds the whole proposal; none of it may leak into the record."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    body = _operator_message(agent)

    assert "$" not in body
    assert "limit" not in body.lower()
    assert "quantity" not in body.lower()
    stripped = body.replace(SYNTHETIC_STAGED_ID, "")
    assert not any(ch.isdigit() for ch in stripped)
    for value in ("7", "333.25", "BUY", "LMT", "DAY"):
        assert value not in stripped
    assert "synthetic" not in body.lower()


def test_no_completed_actions_produces_no_section():
    """An empty section would devalue a channel whose worth is that every line on it is
    load-bearing."""
    agent, _sink = _make_agent_recording()
    assert agent._completed_order_records() == ""
    messages = [{"role": "user", "content": "hello"}]
    agent._append_operator_message(messages)
    assert messages == [{"role": "user", "content": "hello"}]


def test_completed_records_are_byte_stable_across_calls():
    """Repeated builds return identical text — an unstable block would rewrite every turn."""
    agent, _sink = _make_agent_recording()
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    _seed_completed(agent, "trade_cancelled", confirmed=False, state="PendingCancel")

    first = agent._completed_order_records()
    assert first != ""
    assert [agent._completed_order_records() for _ in range(5)] == [first] * 5


def test_proposals_and_completed_actions_share_one_system_message():
    """Two consecutive system messages are outside the probed placement rule. Two labelled
    sections, one message — and the sections must stay distinguishable, because "a button
    was drawn" and "an order exists at IBKR" are different facts."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    messages = [{"role": "user", "content": "and now?"}]
    agent._append_operator_message(messages)

    texts = _system_texts(messages)
    assert len(texts) == 1
    assert "propose_order for ZZZ" in texts[0]
    assert f"PLACED order {SYNTHETIC_STAGED_ID}" in texts[0]
    # Proposals first, completed actions after: escalating strength, closest to where
    # generation resumes.
    assert texts[0].index("propose_order for") < texts[0].index("PLACED order")


def test_a_proposal_that_was_never_clicked_produces_no_completed_record():
    """The critical negative, mirroring the render-failure one: a rendered button that the
    user never clicked must never appear as an order that reached IBKR."""
    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)

    assert agent._completed_order_records() == ""


def test_completed_action_verbs_cover_exactly_the_store_allowlist():
    """Drift guard: a fourth post-click type added to the store must not be silently
    dropped from the records, and one removed there must not linger here."""
    from claudia.agent import _COMPLETED_ACTION_VERBS

    assert set(_COMPLETED_ACTION_VERBS) == set(COMPLETED_ORDER_ACTION_TYPES)


def test_unmapped_completed_type_is_dropped_not_guessed(caplog):
    """Defence in depth. Guessing a verb would put a fabricated action in front of the
    model — the failure class itself."""
    agent, _sink = _make_agent_recording()
    agent._store.add_decision(
        session_id="test-session", decision_type="trade_teleported",
        summary_text="seeded", metadata={"ibkr_order_id": SYNTHETIC_STAGED_ID},
    )
    with caplog.at_level(logging.WARNING, logger="claudia.agent"):
        # Reach the mapping directly: the store's allowlist would filter this row out.
        agent._store.get_completed_order_actions = lambda _sid: [  # type: ignore[method-assign]
            {"decision_type": "trade_teleported", "symbol": "ZZZ",
             "metadata": {"ibkr_order_id": SYNTHETIC_STAGED_ID}},
        ]
        assert agent._completed_order_records() == ""
    assert "Unmapped completed order action" in caplog.text


def test_completed_record_header_forbids_stating_current_state():
    """The records are evidence that an action happened, not a live order book. Reading
    them as current state would be the same defect pointed the other way."""
    from claudia.agent import _COMPLETED_ORDER_HEADER

    lowered = _COMPLETED_ORDER_HEADER.lower()
    assert "current state" in lowered
    assert "does not exist" in lowered      # the denial it must forbid
    assert "call a tool" in lowered
    assert "button click" in lowered


# ── the called-tool ledger in the operator channel ───────────────────────────
#
# `_history_to_messages` drops tool rows, so from turn N+1 the model holds no tool payload
# at all — only its own earlier prose about them. Its docstring assumed that prose was
# enough; 2026-08-11 measured that it is not. Asked for chart settings its own earlier
# message had recorded only by study *name*, it produced colours, line widths, precision
# and "30/70 bands" — terms with zero occurrences anywhere in the database, across three
# turns, each with no tool row and a single API round-trip. The same question in a fresh
# session (empty history) produced five tool calls and ten of ten fields exact against the
# live chart. Only variable: prose versus no prose.
#
# The ledger does not restore the payloads — it says they are gone. Names only: a tool
# input can carry an account number, an order id or a position, and this block goes into
# the model's context and the request body.


def _seed_tool_call(agent, name: str) -> None:
    """Record a tool call exactly as the tool loop does — a `role: "tool"` message row."""
    agent._store.add_message("test-session", "tool", tool_name=name)


def test_no_tool_calls_produces_no_ledger():
    """The empty-header rule, same as its two siblings: a message asserting nothing
    devalues a channel whose worth is that everything on it is load-bearing."""
    agent, _sink = _make_agent_recording()
    assert agent._called_tool_records() == ""
    messages = [{"role": "user", "content": "hello"}]
    agent._append_operator_message(messages)
    assert messages == [{"role": "user", "content": "hello"}]


def test_tool_ledger_names_each_tool_once_sorted():
    """Identity only, one line per distinct tool, alphabetical. Not chronological and not
    counted: both would move the block when a tool is merely called again."""
    agent, _sink = _make_agent_recording()
    _seed_tool_call(agent, "get_market_snapshot")
    _seed_tool_call(agent, "chart_get_studies")
    _seed_tool_call(agent, "get_market_snapshot")
    body = agent._called_tool_records()

    from claudia.agent import _TOOL_LEDGER_HEADER

    assert body == "\n".join([
        _TOOL_LEDGER_HEADER, "  - chart_get_studies", "  - get_market_snapshot",
    ])


def test_tool_ledger_comes_before_the_emission_records():
    """Ordering rule of this channel: least urgent furthest from where generation resumes.
    The ledger is standing background context; an emission record is about this session's
    own output, and a note contradicts the turn just gone."""
    agent, _sink = _make_agent_recording()
    _seed_tool_call(agent, "get_live_orders")
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    agent._pending_operator_notes.append("a note")
    body = _operator_message(agent)

    assert body.index("get_live_orders") < body.index("propose_order for")
    assert body.index("propose_order for") < body.index("PLACED order")
    assert body.index("PLACED order") < body.index("a note")


def test_tool_ledger_is_one_system_message_after_the_user_turn():
    """A ledger on its own still obeys the probed placement rule: one `role: "system"`
    message, never messages[0], immediately after the user turn."""
    agent, _sink = _make_agent_recording()
    _seed_tool_call(agent, "get_live_orders")
    messages = _history_to_messages([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and now?"},
    ])
    agent._append_operator_message(messages)

    assert len(_system_texts(messages)) == 1
    idx = next(i for i, m in enumerate(messages) if m["role"] == "system")
    assert idx > 0
    assert messages[idx - 1]["role"] == "user"
    assert idx == len(messages) - 1


def test_tool_ledger_is_byte_stable_when_a_tool_is_called_again():
    """The block lands after the cached prefix and is rebuilt every turn. A second call to
    a tool already named must not change a byte, or the request body is rewritten for
    nothing turn after turn."""
    agent, _sink = _make_agent_recording()
    _seed_tool_call(agent, "get_market_snapshot")
    _seed_tool_call(agent, "chart_get_studies")
    first = agent._called_tool_records()
    assert first != ""

    _seed_tool_call(agent, "get_market_snapshot")
    _seed_tool_call(agent, "chart_get_studies")
    assert agent._called_tool_records() == first
    assert [agent._called_tool_records() for _ in range(5)] == [first] * 5


def test_tool_ledger_carries_no_tool_input_or_result(tmp_path):
    """The safety claim, tested against the real store and the real columns rather than a
    double's fields. A tool input can carry an account number, an order id or a position;
    a result can carry the whole book. A name is safe, nothing else on this channel is."""
    store = ConversationStore(tmp_path / "ledger.db")
    store.create_session("test-session")
    agent, _sink = _make_agent_recording(store=store)
    store.add_message(
        "test-session", "tool", tool_name="get_market_snapshot",
        tool_input={"conid": "SENTINEL-INPUT-4242"},
        tool_result={"last": "SENTINEL-RESULT-9999"},
    )
    body = _operator_message(agent)

    assert "get_market_snapshot" in body
    assert "SENTINEL-INPUT-4242" not in body
    assert "SENTINEL-RESULT-9999" not in body
    assert "conid" not in body
    assert not any(ch.isdigit() for ch in body)


def test_tool_ledger_never_names_a_proposal_tool():
    """The one exclusion. The header's remedy is "call the tool again in this turn", which
    for `propose_order` means emitting a second staging button — an order-flow action, not
    a lookup. A proposal call is also already covered, more carefully, by the emission
    records, whose worth rests on excluding a render that failed."""
    agent, _sink = _make_agent_recording()
    _seed_tool_call(agent, "propose_order")
    _seed_tool_call(agent, "propose_cancel")
    _seed_tool_call(agent, "propose_modify")
    assert agent._called_tool_records() == ""

    _seed_tool_call(agent, "get_live_orders")
    body = agent._called_tool_records()
    assert "  - get_live_orders" in body
    assert "propose_" not in body


def test_tool_ledger_exclusion_covers_exactly_the_proposal_tools():
    """Drift guard: a fourth proposal tool must not land in the ledger by default, and one
    removed from `proposal_tools` must not stay excluded here by a hardcoded name."""
    from claudia.agent import _PROPOSAL_KINDS, PROPOSAL_TOOL_NAMES

    assert frozenset(_PROPOSAL_KINDS) == PROPOSAL_TOOL_NAMES


def test_tool_ledger_header_says_the_results_are_gone():
    """The whole point of the block. It must not read as "here is what you learned" — it
    says the payloads are absent and that a current value needs a fresh call."""
    from claudia.agent import _TOOL_LEDGER_HEADER

    lowered = _TOOL_LEDGER_HEADER.lower()
    assert "not in your context" in lowered
    assert "recollection, not evidence" in lowered
    assert "read it again" in lowered
    # The remedy must be a READ. It used to say "call the tool again in this turn", and the
    # ledger names writers too — create_price_alert, pine_set_source, chart_set_symbol — so
    # that phrasing invited re-running a side effect the user never asked for. It is the same
    # argument the proposal-tool exclusion below rests on, which had not been generalised.
    assert "call the tool again" not in lowered
    # Turn-relative: _append_operator_message runs once before the tool loop, so mid-turn the
    # model reads this beside a live tool_result for a tool on the list. Say which turn.
    assert "before this turn" in lowered


def test_emission_record_tools_cover_exactly_the_store_allowlist():
    """Drift guard: a fourth rendered type added to the store must not be silently dropped
    from the records, and a type removed there must not linger here."""
    from claudia.agent import _PROPOSAL_DECISION_TOOLS, _PROPOSAL_KINDS

    assert set(_PROPOSAL_DECISION_TOOLS) == set(RENDERED_PROPOSAL_TYPES)
    assert set(_PROPOSAL_DECISION_TOOLS.values()) == set(_PROPOSAL_KINDS)


def test_emission_record_header_never_claims_a_pending_or_staged_order():
    """The record says a button was drawn. It must not drift into implying the user acted
    on it, or it becomes the next generation of the same false claim."""
    from claudia.agent import _EMISSION_RECORD_HEADER

    lowered = _EMISSION_RECORD_HEADER.lower()
    assert "staging button" in lowered
    assert "staged" not in lowered.replace("staging", "")
    assert "order exists" not in lowered
    assert "pending" not in lowered


# ── L4: a narrated staging action nothing backs (gap #3) ─────────────────────
#
# L1 asserts parsed ⇒ rendered. This asserts claimed ⇒ called. The two are disjoint: L1
# needs a recorded proposal to have anything to assert, so a turn that never called a
# proposal tool is structurally invisible to it. Every test below runs with
# `_pending_proposal is None`, which is the whole trigger condition.


@pytest.mark.parametrize("text", NARRATED_STAGING)
def test_claim_detector_fires_on_a_completed_action_claim(text):
    """Each corpus sentence that really asserts a completed staging is detected."""
    from claudia.agent import _claims_completed_proposal

    assert _claims_completed_proposal(text) is not None


@pytest.mark.parametrize("text", HONEST_STAGING_TALK + INNOCENT)
def test_claim_detector_is_silent_on_honest_staging_talk(text):
    """The trap, and the reason the 2026-07-27 prose detector was dropped at 81% false
    positives. Two of these are ClaudIA owning this very failure; firing on them would
    train the user to ignore the guardrail, which is how the original defect stayed
    invisible for two sessions."""
    from claudia.agent import _claims_completed_proposal

    assert _claims_completed_proposal(text) is None


def test_the_guardrails_own_texts_never_trip_the_detector():
    """Recursion guard. Every string this agent writes about staging is persisted as an
    assistant row or replayed on the operator channel; any of them matching would make the
    guardrail fire on its own output."""
    from claudia.agent import (
        _COMPLETED_ORDER_HEADER,
        _EMISSION_RECORD_HEADER,
        _GUARDRAIL_NOTICE,
        _OPERATOR_NOTE,
        _STALE_BOOK_CLAIM_NOTICE,
        _STALE_BOOK_CLAIM_OPERATOR_NOTE,
        _TOOL_LEDGER_HEADER,
        _UNBACKED_ACTION_NOTICE,
        _UNBACKED_ACTION_OPERATOR_NOTE,
        _UNBACKED_CLAIM_NOTICE,
        _UNBACKED_CLAIM_OPERATOR_NOTE,
        _UNBACKED_RESULT_NOTICE,
        _UNBACKED_RESULT_OPERATOR_NOTE,
        _claims_completed_action,
        _claims_completed_proposal,
        _claims_fresh_book_check,
        _claims_verbatim_tool_result,
    )

    for text in (
        _GUARDRAIL_NOTICE,
        _OPERATOR_NOTE.format(kind="cancel"),
        _EMISSION_RECORD_HEADER,
        _COMPLETED_ORDER_HEADER,
        _TOOL_LEDGER_HEADER,
        _UNBACKED_CLAIM_NOTICE,
        _UNBACKED_CLAIM_OPERATOR_NOTE,
        _STALE_BOOK_CLAIM_NOTICE,
        _STALE_BOOK_CLAIM_OPERATOR_NOTE,
        _UNBACKED_ACTION_NOTICE,
        _UNBACKED_ACTION_OPERATOR_NOTE,
        _UNBACKED_RESULT_NOTICE,
        _UNBACKED_RESULT_OPERATOR_NOTE,
    ):
        assert _claims_completed_proposal(text) is None, text[:60]
        assert _claims_fresh_book_check(text) is None, text[:60]
        assert _claims_completed_action(text) is None, text[:60]
        assert _claims_verbatim_tool_result(text) is None, text[:60]


def test_replayed_record_lines_never_trip_the_detector():
    """The operator channel's rendered lines, not just their headers."""
    from claudia.agent import (
        _claims_completed_action,
        _claims_completed_proposal,
        _claims_fresh_book_check,
    )

    agent, _sink = _make_agent_recording()
    for decision_type in RENDERED_PROPOSAL_TYPES:
        agent._store.add_decision(
            session_id="test-session", decision_type=decision_type, symbol="ZZZ",
            message_id=1, metadata={"order": {"order_id": "9000001"}},
        )
    for decision_type in COMPLETED_ORDER_ACTION_TYPES:
        agent._store.add_decision(
            session_id="test-session", decision_type=decision_type, symbol="ZZZ",
            metadata={"ibkr_order_id": "9000001", "readback_confirmed": True,
                      "readback_order_status": "Submitted"},
        )
    # The ledger names order-book tools by name, which is the closest a line here gets to
    # the book detector's own vocabulary.
    for name in ("get_live_orders", "get_order_status", "get_market_snapshot"):
        _seed_tool_call(agent, name)
    assert _claims_completed_proposal(agent._emission_records()) is None
    assert _claims_completed_proposal(agent._completed_order_records()) is None
    assert _claims_completed_proposal(agent._called_tool_records()) is None
    assert _claims_fresh_book_check(agent._emission_records()) is None
    assert _claims_fresh_book_check(agent._completed_order_records()) is None
    assert _claims_fresh_book_check(agent._called_tool_records()) is None
    assert _claims_completed_action(agent._emission_records()) is None
    assert _claims_completed_action(agent._completed_order_records()) is None
    assert _claims_completed_action(agent._called_tool_records()) is None


async def test_narrated_staging_produces_an_honest_notice():
    """The user is told, in the same feed that carries the claim, that it did not happen."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_STAGING[0]))
    )
    await agent.handle_message("cancel that one")

    assert sink.messages[0] == NARRATED_STAGING[0]  # the claim still stands, uncensored
    notice = sink.messages[-1].lower()
    assert "no staging button" in notice
    assert "nothing has been staged" in notice
    assert sink.proposals == []


async def test_narrated_staging_writes_its_own_decision_type():
    """Deliberately not `proposal_render_failed`: nothing was accepted, so nothing could
    fail to render. Conflating them would make that type stop meaning what every historical
    row means."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_STAGING[0]))
    )
    await agent.handle_message("cancel that one")

    rows = agent._store.get_decisions("test-session")
    assert [r["decision_type"] for r in rows] == ["proposal_claim_unbacked"]
    # Anchored to the assistant row carrying the claim (user turn is 1), so the session
    # report and the FTS index can find the message the notice is about.
    assert rows[0]["message_id"] == 2
    assert "no proposal tool was called" in rows[0]["summary_text"].lower()
    # The offending sentence is live conversation text: logged, never stored.
    assert NARRATED_STAGING[0][:20] not in json.dumps(rows[0], default=str)


async def test_narrated_staging_notice_is_persisted_not_only_displayed():
    """session_reporter, the FTS index and the next turn's replayed history all read the
    transcript, not the sink."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_STAGING[1]))
    )
    await agent.handle_message("cancel it")

    stored = [m["content"] for m in agent._store.messages if m["role"] == "assistant"]
    assert any("no staging button was created" in text.lower() for text in stored)


async def test_narrated_staging_uses_the_operator_channel():
    """The model must not carry the claim into the next turn and defend it — the 2026-07-24
    failure. A model-authored correction would be forgeable; `role: "system"` is not."""
    agent, _sink = _make_agent_recording()
    stream = MagicMock(side_effect=[
        _FakeStream(_text_response_events(NARRATED_STAGING[0])),
        _FakeStream(_text_response_events("Understood — nothing was staged.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("cancel that one")
    await agent.handle_message("is it staged?")

    notes = _system_texts(stream.call_args_list[-1].kwargs["messages"])
    assert len(notes) == 1
    assert "no proposal tool" in notes[0].lower()
    assert "propose_cancel" in notes[0]


async def test_evidence_beats_the_text_when_a_proposal_really_was_recorded():
    """The verdict is the tool call, never the prose. The same sentence that fires above is
    correct here, and must pass silently."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_cancel", VALID_CANCEL, NARRATED_STAGING[0])
    )
    await agent.handle_message("cancel it")

    assert sink.proposals == [("cancel", VALID_CANCEL)]
    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "trade_cancel_proposed"
    ]
    assert not any("no staging button" in m.lower() for m in sink.messages)
    assert agent._pending_operator_notes == []


async def test_a_render_failure_raises_exactly_one_notice():
    """A recorded-but-unrendered proposal is L1's case. The claim detector must stay out of
    it, or the user gets two contradictory corrections for one failure."""
    agent, _sink = _make_agent_recording(RuntimeError("boom"))
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn("propose_cancel", VALID_CANCEL, NARRATED_STAGING[0])
    )
    await agent.handle_message("cancel it")

    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "proposal_render_failed"
    ]
    assert len(agent._pending_operator_notes) == 1


async def test_a_rejected_proposal_still_reaches_the_claim_detector():
    """`_record_proposal` refuses a defective proposal and leaves `_pending_proposal` unset,
    so no button exists. If the model then narrates one anyway, that is exactly this
    failure and the notice must fire — the refusal string reaches the user only through the
    tool-step pane."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_proposal_turn(
            "propose_cancel", {**VALID_CANCEL, "order_id": "  "}, NARRATED_STAGING[0]
        )
    )
    await agent.handle_message("cancel it")

    assert sink.proposals == []
    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "proposal_claim_unbacked"
    ]


# ── L5: a narrated order-book lookup nothing backs (gap #3, freshness half) ──
#
# L4 asserts *claimed a proposal ⇒ called a proposal tool*. This asserts *claimed a lookup ⇒
# called a lookup tool*. The 2026-07-28 message did both in one turn, and neither check can
# see the other's half — so unlike L1/L4 these two are not mutually exclusive.


def _tool_then_text(tool_name: str, reply: str, tool_result: str = "{}") -> list:
    """The two streams of one turn: a real tool call, then the reply text."""
    return [
        _FakeStream([
            SimpleNamespace(
                type="message_start", message=SimpleNamespace(usage=SimpleNamespace())
            ),
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="t1", name=tool_name),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
            ),
            _message_delta("tool_use"),
        ]),
        _FakeStream(_text_response_events(reply)),
    ]


@pytest.mark.parametrize("text", NARRATED_BOOK_CHECK)
def test_book_check_detector_fires_on_a_claimed_lookup(text):
    """Each corpus sentence that really claims a just-run order-book check is detected."""
    from claudia.agent import _claims_fresh_book_check

    assert _claims_fresh_book_check(text) is not None


@pytest.mark.parametrize("text", HONEST_BOOK_TALK + HONEST_STAGING_TALK + INNOCENT)
def test_book_check_detector_is_silent_on_everything_else(text):
    """The same trap that retired the 2026-07-27 detector, aimed at this shape. A
    verification verb and a book noun are both present somewhere in 76 of the live store's
    169 assistant messages; firing on co-presence rather than on a claim is what measured
    81% false positives."""
    from claudia.agent import _claims_fresh_book_check

    assert _claims_fresh_book_check(text) is None


def test_book_check_detector_ignores_the_adjectival_reading():
    """The one false-positive class the live corpus does not contain, so it is excluded by
    construction instead of by measurement: an adjective takes a determiner, and neither
    alternative of the shape can follow one."""
    from claudia.agent import _claims_fresh_book_check

    assert _claims_fresh_book_check("A confirmed live order needs a click.") is None
    assert _claims_fresh_book_check("Confirmed the live orders.") is not None


def test_book_reading_tools_match_the_toolkits_order_surface():
    """Drift guard. A new order-reading tool in ibkr_core_mcp that is not added here would
    silently make this guardrail fire on honest messages — the failure mode a guardrail can
    least afford. `preview_order` is excluded deliberately: it prices a hypothetical order
    and reads no existing one."""
    from ibkr_core_mcp.claude_tools import TOOL_DEFINITIONS

    from claudia.agent import _BOOK_READING_TOOLS

    order_tools = {t["name"] for t in TOOL_DEFINITIONS if "order" in t["name"]}
    assert order_tools == _BOOK_READING_TOOLS | {"preview_order"}


async def test_narrated_book_check_produces_an_honest_notice():
    """The claim is left visible and a correction appended saying the lookup never ran."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_BOOK_CHECK[0]))
    )
    await agent.handle_message("what's working?")

    assert sink.messages[0] == NARRATED_BOOK_CHECK[0]  # the claim still stands, uncensored
    notice = sink.messages[-1].lower()
    assert "never ran" in notice
    assert "came from memory" in notice

    rows = agent._store.get_decisions("test-session")
    assert [r["decision_type"] for r in rows] == ["book_claim_unverified"]
    assert rows[0]["message_id"] == 2
    # The offending sentence is live conversation text: logged, never stored.
    assert NARRATED_BOOK_CHECK[0][:20] not in json.dumps(rows[0], default=str)


@pytest.mark.parametrize("tool_name", ["get_live_orders", "get_order_status", "diagnose_orders"])
async def test_a_real_lookup_clears_the_claim(tool_name):
    """The verdict is the tool call, never the prose. Each of the three book readers must
    clear the same sentence that fires without one."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_tool_then_text(tool_name, NARRATED_BOOK_CHECK[0])
    )
    agent._toolkit.execute = MagicMock(return_value=("[]", None))

    await agent.handle_message("what's working?")

    assert not any("never ran" in m.lower() for m in sink.messages)
    assert agent._store.get_decisions("test-session") == []
    assert agent._pending_operator_notes == []


async def test_an_unrelated_tool_does_not_clear_the_claim():
    """`get_positions` is a real IBKR read and still says nothing about the order book —
    the distinction the evidence set exists to make."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_tool_then_text("get_positions", NARRATED_BOOK_CHECK[0])
    )
    agent._toolkit.execute = MagicMock(return_value=("[]", None))

    await agent.handle_message("what's working?")

    assert any("never ran" in m.lower() for m in sink.messages)


async def test_a_lookup_in_an_earlier_round_of_the_same_turn_still_counts():
    """`tool_calls` is cleared on every pass of the loop, so a book read followed by two
    more tool rounds must still count as evidence. Accumulating per-turn is the whole point
    of `called_tools`."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(side_effect=[
        _tool_then_text("get_live_orders", "")[0],
        _tool_then_text("get_positions", "")[0],
        _FakeStream(_text_response_events(NARRATED_BOOK_CHECK[0])),
    ])
    agent._toolkit.execute = MagicMock(return_value=("[]", None))

    await agent.handle_message("what's working?")

    assert not any("never ran" in m.lower() for m in sink.messages)


async def test_narrated_book_check_uses_the_operator_channel():
    """The model must not carry an unverified order state into the next turn."""
    agent, _sink = _make_agent_recording()
    stream = MagicMock(side_effect=[
        _FakeStream(_text_response_events(NARRATED_BOOK_CHECK[0])),
        _FakeStream(_text_response_events("Understood — I will check for real.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("what's working?")
    await agent.handle_message("and now?")

    notes = _system_texts(stream.call_args_list[-1].kwargs["messages"])
    assert len(notes) == 1
    assert "no order-book tool ran" in notes[0].lower()
    assert "get_live_orders" in notes[0]


async def test_one_message_can_earn_both_corrections():
    """The 2026-07-28 message itself: it narrated a lookup *and* a staging, and neither was
    real. Unlike L1 and L4 these two are not mutually exclusive, so the user is owed both."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(
            f"{NARRATED_BOOK_CHECK[0]}\n\n{NARRATED_STAGING[0]}"
        ))
    )
    await agent.handle_message("check the book and cancel it")

    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "proposal_claim_unbacked",
        "book_claim_unverified",
    ]
    assert len(agent._pending_operator_notes) == 2


def test_stale_book_notice_never_implies_the_state_was_verified():
    """Wording guard: this text exists to contradict a claim the transcript already
    carries, so a hedge would leave that claim standing."""
    from claudia.agent import _STALE_BOOK_CLAIM_NOTICE

    lowered = _STALE_BOOK_CLAIM_NOTICE.lower()
    assert "never ran" in lowered
    assert "came from memory" in lowered
    assert "may " not in lowered and "might" not in lowered


def test_unbacked_claim_notice_never_implies_something_was_staged():
    """Wording guard, same as the render-failure notice: this text exists to contradict a
    claim the transcript already carries, so a hedge would leave the claim standing."""
    from claudia.agent import _UNBACKED_CLAIM_NOTICE

    lowered = _UNBACKED_CLAIM_NOTICE.lower()
    assert "nothing has been staged" in lowered
    assert "no staging button" in lowered
    assert "may " not in lowered and "might" not in lowered


@pytest.mark.live_api
@pytest.mark.skipif(
    os.environ.get("CLAUDIA_LIVE_SCHEMA_CHECK") != "1",
    reason="live API check is opt-in: set CLAUDIA_LIVE_SCHEMA_CHECK=1",
)
def test_live_api_accepts_mid_conversation_system_message():
    """Prove the operator channel is accepted by the real API, in its production shape.

    Local validation cannot prove API acceptance — two schema defects earlier in this plan
    reached a fully green suite because of exactly that gap, both registration-time 400s
    that would have failed every request in production. The message-role shape carries the
    same risk: `role: "system"` inside `messages` is model-gated, and an unsupported model
    returns `role 'system' is not supported on this model`.

    Three shapes are probed, all of which the agent really produces:
      1. the note last in `messages`, straight after the user turn (first request of a turn)
      2. the same note carrying the messages-level prompt-cache breakpoint, which is what
         `_with_history_cache_marker` builds when the note is the final message
      3. the note mid-list, followed by the assistant/tool_result round trip of a proposal
         tool call (every later pass of the tool loop)

    All three probed ACCEPTED on claude-opus-4-8, 2026-07-27, with no beta header. Two
    neighbouring rejections were probed in the same run and are what shape the placement
    rule and shape 3's tail:

      - a system message at `messages[0]` → 400, "use the top-level 'system' parameter for
        the initial system prompt". `_append_operator_message`' user-turn guard makes this
        unreachable.
      - a list *ending* on an assistant turn → 400, "This model does not support assistant
        message prefill". Unrelated to the system role, but it is why shape 3 carries the
        tool_result turn rather than stopping at the assistant message.

    Opt-in — it costs real API calls:

        CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest tests/test_agent.py -m live_api -v
    """
    import anthropic
    from dotenv import load_dotenv

    from claudia.agent import _GUARDRAIL_NOTICE, _with_cache_marker, _with_history_cache_marker
    from claudia.proposal_tools import PROPOSAL_TOOLS

    load_dotenv(override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("CLAUDIA_LIVE_SCHEMA_CHECK=1 but no ANTHROPIC_API_KEY resolved (checked .env)")

    model = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
    note = {"role": "system", "content": _GUARDRAIL_NOTICE}
    two_turns = [
        {"role": "user", "content": "Buy 1 AAPL at 250."},
        {"role": "assistant", "content": FAILED_437},
        {"role": "user", "content": "Is it staged?"},
    ]
    tool_round_trip = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_probe1", "name": "propose_order", "input": VALID_ORDER},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_probe1", "content": "Proposal accepted."},
        ]},
    ]
    shapes = {
        "note last": [*two_turns, note],
        "note last, cache-marked": _with_history_cache_marker([*two_turns, note]),
        "note before a tool round trip": [*two_turns, note, *tool_round_trip],
    }
    client = anthropic.Anthropic()
    for label, messages in shapes.items():
        try:
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=messages,  # type: ignore[arg-type]
                tools=_with_cache_marker(PROPOSAL_TOOLS),  # type: ignore[arg-type]
            )
        except anthropic.BadRequestError as exc:  # pragma: no cover - only on API change
            pytest.fail(f"live API rejected the operator channel ({label}) on {model}: {exc}")


@pytest.mark.live_api
@pytest.mark.skipif(
    os.environ.get("CLAUDIA_LIVE_SCHEMA_CHECK") != "1",
    reason="live API check is opt-in: set CLAUDIA_LIVE_SCHEMA_CHECK=1",
)
def test_live_api_rejects_the_operator_channel_on_an_excluded_model():
    """Prove an excluded model really does reject the operator channel, and how.

    The negative half of the probe above, and the evidence behind
    `warn_if_model_lacks_operator_channel`. That warning tells the user their session will
    "fail with an API 400" — a claim taken from Anthropic's reference, not from execution.
    The official prompt-caching page states the exclusion (*"not available on Claude
    Sonnet 5; use the top-level `system` field instead"*, read 2026-08-05) but gives no
    error, and CLAUDE.md's rule is explicit that a message-role placement must be probed
    rather than inferred — a doc is a claim, execution is evidence.

    Asserts only what the warning depends on: that it is a `BadRequestError`. The message
    text is recorded in the failure output rather than matched, since wording is
    Anthropic's to change and pinning it would make this test fail on a rename rather than
    on a behaviour change.

    A model that *stops* rejecting this is not a failure of the system — it is a signal to
    add it to `_OPERATOR_CHANNEL_MODELS`, and the assertion message says so.

    Opt-in — it costs real API calls:

        CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest tests/test_agent.py -m live_api -v
    """
    import anthropic
    from dotenv import load_dotenv

    from claudia.agent import _GUARDRAIL_NOTICE, _OPERATOR_CHANNEL_MODELS

    load_dotenv(override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("CLAUDIA_LIVE_SCHEMA_CHECK=1 but no ANTHROPIC_API_KEY resolved (checked .env)")

    excluded = "claude-sonnet-4-6"
    assert excluded not in _OPERATOR_CHANNEL_MODELS, "probe model must be one we exclude"

    messages = [
        {"role": "user", "content": "Buy 1 AAPL at 250."},
        {"role": "assistant", "content": FAILED_437},
        {"role": "user", "content": "Is it staged?"},
        {"role": "system", "content": _GUARDRAIL_NOTICE},
    ]
    client = anthropic.Anthropic()
    try:
        client.messages.create(
            model=excluded,
            max_tokens=1,
            thinking={"type": "adaptive"},
            messages=messages,  # type: ignore[arg-type]
        )
    except anthropic.BadRequestError as exc:
        print(f"\n{excluded} rejected the operator channel as expected: {exc}")
        return
    pytest.fail(
        f"{excluded} ACCEPTED a mid-conversation system message. The exclusion in "
        f"agent._OPERATOR_CHANNEL_MODELS is now wrong — re-read the prompt-caching docs "
        f"and add it, rather than deleting this test."
    )


@pytest.mark.live_api
@pytest.mark.skipif(
    os.environ.get("CLAUDIA_LIVE_SCHEMA_CHECK") != "1",
    reason="live API check is opt-in: set CLAUDIA_LIVE_SCHEMA_CHECK=1",
)
def test_live_api_accepts_the_emission_record_channel():
    """Prove the *combined* operator message is accepted by the real API.

    This check is not optional diligence. The plan's own design for this task —
    `role: "system"` inserted directly after each assistant turn that produced a proposal —
    was probed against the live API on 2026-07-27 and rejected in every shape tried:

        400  user, assistant, system(record), user        <- the plan's exact replay shape
        400  two records across two turns
        400  record last, no following user turn
             -> messages.2: role 'system' must follow a 'user' message or an
                'assistant' message ending in a server tool result

    That is why records are consolidated into one message appended after the *current* user
    turn instead. Local tests cannot distinguish the two — both are well-formed dicts — and
    two schema defects earlier in this plan reached a fully green suite for exactly that
    reason.

    The body probed is the real one: built by `_emission_records()` and
    `_completed_order_records()` from seeded decision rows and joined with a genuine
    `_OPERATOR_NOTE`, i.e. the production string, not a hand-written stand-in — so the
    completed-action section added on 2026-07-28 is covered by the same probe as the
    section it sits beside. Three shapes, all of which the agent really produces:
      1. the combined message last, straight after the user turn (first request of a turn)
      2. the same carrying the messages-level prompt-cache breakpoint
      3. the same mid-list, followed by the assistant/tool_result round trip of a proposal
         tool call (every later pass of the tool loop)

    Opt-in — it costs real API calls:

        CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest tests/test_agent.py -m live_api -v
    """
    import anthropic
    from dotenv import load_dotenv

    from claudia.agent import _OPERATOR_NOTE, _with_cache_marker, _with_history_cache_marker
    from claudia.proposal_tools import PROPOSAL_TOOLS

    load_dotenv(override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("CLAUDIA_LIVE_SCHEMA_CHECK=1 but no ANTHROPIC_API_KEY resolved (checked .env)")

    agent, _sink = _make_agent_recording()
    _seed_rendered(agent, "trade_proposed", SYNTHETIC_ORDER)
    _seed_rendered(agent, "trade_modify_proposed", SYNTHETIC_MODIFY)
    _seed_completed(agent, "trade_staged", confirmed=True, state="Submitted")
    _seed_completed(agent, "trade_cancelled", confirmed=False, state=None)
    agent._pending_operator_notes.append(_OPERATOR_NOTE.format(kind="cancel"))
    combined = {"role": "system", "content": _operator_message(agent)}
    assert "propose_order" in combined["content"] and "failed to render" in combined["content"]
    assert f"PLACED order {SYNTHETIC_STAGED_ID}" in combined["content"]

    model = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
    three_turns = [
        {"role": "user", "content": "Buy 1 ZZZ at 250."},
        {"role": "assistant", "content": "Proposal is up — the staging button is below."},
        {"role": "user", "content": "Now cancel order 8880002222."},
        {"role": "assistant", "content": DEFENDED_CLAIM_588},
        {"role": "user", "content": "So what have you actually proposed so far?"},
    ]
    tool_round_trip = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_probe2", "name": "propose_order",
             "input": SYNTHETIC_ORDER},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_probe2", "content": "Proposal accepted."},
        ]},
    ]
    shapes = {
        "records+note last": [*three_turns, combined],
        "records+note last, cache-marked": _with_history_cache_marker([*three_turns, combined]),
        "records+note before a tool round trip": [*three_turns, combined, *tool_round_trip],
    }
    client = anthropic.Anthropic()
    for label, messages in shapes.items():
        try:
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=messages,  # type: ignore[arg-type]
                tools=_with_cache_marker(PROPOSAL_TOOLS),  # type: ignore[arg-type]
            )
        except anthropic.BadRequestError as exc:  # pragma: no cover - only on API change
            pytest.fail(f"live API rejected the emission-record channel ({label}) on {model}: {exc}")


# ── Safety block: derived figures (2026-08-03 live finding) ───────────────────


def test_safety_block_requires_percentages_to_name_their_base():
    """Live 2026-08-03: ClaudIA computed a drawdown correctly and named the wrong base.

    It reported "a ~25% drawdown on a $9,245 position". 24.6% is correct against the
    12,254.91 cost basis; $9,245 is the current market value, which gives 32.6%. The
    calculation was the right one -- the prose named the other quantity. No existing
    guardrail catches this: L4/L5 assert a claim was BACKED by a tool call, not that a
    ratio names its denominator, so the turn passed both and still failed verification
    for anyone who checked it.
    """
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "DERIVED FIGURES MUST NAME THEIR BASE" in prompt
    assert "cost basis" in prompt
    assert "Do not switch bases silently." in prompt


def test_safety_block_derived_figures_rule_sits_inside_data_integrity():
    """It is a provenance rule, not a formatting preference -- placement matters.

    DATA INTEGRITY is marked non-overridable; a base that cannot be traced to a tool
    result is the same defect as a price that cannot.
    """
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    di = prompt.index("## DATA INTEGRITY")
    rule = prompt.index("DERIVED FIGURES MUST NAME THEIR BASE")
    nxt = prompt.index("## ORDER PROPOSAL")
    assert di < rule < nxt


# ── Operator-channel model support ───────────────────────────────────────────


@pytest.mark.parametrize(
    "model", ["claude-opus-4-8", "claude-opus-5", "claude-fable-5", "claude-mythos-5"]
)
def test_a_supported_model_produces_no_operator_channel_warning(model, caplog):
    """The known-good set passes silently — the warning has to stay rare to be believed."""
    from claudia.agent import warn_if_model_lacks_operator_channel

    with caplog.at_level(logging.ERROR, logger="claudia.agent"):
        assert warn_if_model_lacks_operator_channel(model) is None
    assert caplog.text == ""


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-sonnet-5", "claude-haiku-4-5"])
def test_a_model_without_the_operator_channel_is_reported_with_the_symptom(model, caplog):
    """An unsupported model is named, and the log says what breaks and when.

    `claude-sonnet-4-6` was recommended by `docs/env-vars-reference.md` until 2026-08-05:
    it meets the adaptive-thinking requirement, which is the only one that had been
    checked. Mid-conversation `role: "system"` messages are Opus-4.8/Opus-5/Fable/Mythos
    only (verified against the official prompt-caching page), so the Sonnet line 400s once
    the operator channel carries anything — which since 2026-08-11 is the turn after ANY
    tool call, not just from the first rendered proposal. The message must name the
    EARLIEST trigger: a reader told only about proposals would rule the channel out for a
    session that never touched orders, which is now the common case (measured over this
    repo's history: 84% of sessions with a user message make a ledger-eligible call, against
    16% that record a proposal).
    """
    from claudia.agent import warn_if_model_lacks_operator_channel

    with caplog.at_level(logging.ERROR, logger="claudia.agent"):
        assert warn_if_model_lacks_operator_channel(model) == model
    assert model in caplog.text
    assert "400" in caplog.text           # names the failure
    assert "tool call" in caplog.text     # names the EARLIEST trigger
    assert "proposal" in caplog.text      # and still names the later ones


def test_the_operator_channel_allowlist_excludes_the_sonnet_line():
    """Sonnet is excluded by construction, not by a test that could drift from the set.

    The docs are explicit — "not available on Claude Sonnet 5; use the top-level `system`
    field instead" — and Sonnet 4.6 is absent from the supported list too.
    """
    from claudia.agent import _OPERATOR_CHANNEL_MODELS

    assert not any("sonnet" in m for m in _OPERATOR_CHANNEL_MODELS)
    assert not any("haiku" in m for m in _OPERATOR_CHANNEL_MODELS)


def test_the_model_panel_app_will_actually_use_can_carry_the_operator_channel():
    """The model `panel_app` really resolved must satisfy the requirement it depends on.

    Reads `panel_app._MODEL` rather than re-deriving the default literal: a test that
    repeats the same string would keep passing after someone changed the real default,
    which is the failure mode where a double is easier to satisfy than the thing it
    stands for. With no `CLAUDIA_MODEL` in the test environment this is the shipped
    default; with one set it checks that too, which is the more useful assertion anyway.
    """
    from claudia.agent import _OPERATOR_CHANNEL_MODELS
    from claudia.panel_app import _MODEL

    assert _MODEL in _OPERATOR_CHANNEL_MODELS, (
        f"panel_app resolved CLAUDIA_MODEL={_MODEL!r}, which cannot carry the "
        f"mid-conversation system message the operator channel depends on"
    )


# ── search_past_conversations never raises ───────────────────────────────────


def test_a_failing_search_returns_an_honest_string_instead_of_raising():
    """`_handle_local_tool` promises never to raise; a store failure must not break it.

    The tool result feeds straight back to the model, so the string also has to stop it
    concluding anything from the failure — a search that did not run is not evidence that
    a topic was never discussed. Same rule as a failed order lookup.
    """
    agent = _make_agent()
    agent._store.search_messages.side_effect = RuntimeError("fts5 index is corrupt")

    result = agent._handle_local_tool("search_past_conversations", {"query": "NVDA"})

    assert "failed" in result.lower()
    assert "not evidence" in result.lower()


def test_an_empty_result_is_reported_as_no_match_not_as_a_failure():
    """Nothing found and search-broke are different claims and must read differently."""
    agent = _make_agent()
    agent._store.search_messages.return_value = []

    result = agent._handle_local_tool("search_past_conversations", {"query": "NVDA"})

    assert "No past conversations found" in result
    assert "failed" not in result.lower()


# ── S5: IBKR tools are silent while the session is being established ─────────


def _suspended_state():
    """A session mid-login: total suspension, per plan §8.1."""
    from claudia.gateway_session import SessionPhase, declare
    return declare(SessionPhase.AUTHENTICATING)


def _live_state():
    """A confirmed session — every tool may run."""
    from datetime import UTC, datetime

    from claudia.gateway_session import SessionPhase, SessionState
    return SessionState(phase=SessionPhase.LIVE, as_of=datetime.now(UTC), detail="ok")


def test_ibkr_tools_are_blocked_while_a_login_is_in_progress():
    """Any request renews the session, so the model's tools are not exempt.

    An exception for "just the agent" would reintroduce the traffic that made
    `POST /logout` unable to clear a borrowed session on 2026-08-05.
    """
    from claudia.agent import _ibkr_unavailable

    owner = MagicMock()
    owner.state.return_value = _suspended_state()
    with patch("claudia.gateway_session.get_session", return_value=owner):
        blocked = _ibkr_unavailable()

    assert blocked is not None
    assert "No request was sent" in blocked


def test_the_block_tells_the_model_not_to_guess():
    """A refusal the model narrates around is worse than no refusal.

    The proposal-block finding already showed the model will describe a tool call it never
    made, so the text has to say explicitly that nothing was read and nothing changed.
    """
    from claudia.agent import _ibkr_unavailable

    owner = MagicMock()
    owner.state.return_value = _suspended_state()
    with patch("claudia.gateway_session.get_session", return_value=owner):
        blocked = _ibkr_unavailable()

    assert "Do not guess" in blocked
    assert "nothing was changed" in blocked


def test_a_live_session_blocks_nothing():
    """The gate must open again, or the assistant is permanently mute."""
    from claudia.agent import _ibkr_unavailable

    owner = MagicMock()
    owner.state.return_value = _live_state()
    with patch("claudia.gateway_session.get_session", return_value=owner):
        assert _ibkr_unavailable() is None


def test_a_free_or_down_gateway_is_not_blocked_here():
    """A 401 or connection error is MORE informative to the model than a refusal.

    Blocking those would replace a precise error ("not authenticated") with a vague one,
    and would stop the model reporting a genuinely down gateway.
    """
    from datetime import UTC, datetime

    from claudia.agent import _ibkr_unavailable
    from claudia.gateway_session import SessionPhase, SessionState

    for phase in (SessionPhase.FREE, SessionPhase.DOWN, SessionPhase.DEGRADED):
        owner = MagicMock()
        owner.state.return_value = SessionState(
            phase=phase, as_of=datetime.now(UTC), detail="x"
        )
        with patch("claudia.gateway_session.get_session", return_value=owner):
            assert _ibkr_unavailable() is None, f"{phase} must not be blocked"


# ── The unbacked-action detector: a narrated action nothing backs ─────────────
#
# T7's shape (2026-08-11), and the oldest failure in the store (2026-06-24): a lead-in
# to act, then a completion report, in a turn with zero tool calls. Measured 2026-08-12
# against all 225 assistant messages: 22 instances across nine sessions,
# including a held position the user rebutted in his next message, an order-status table
# with an invented order id, and both TV fabrications. The trigger is textual, the
# verdict is evidence: `called_tools` empty is what makes the same sentence a lie here
# and honest in the turn that really ran its tools (msg 746's text is byte-similar to a
# firing one and its tools ran — it must stay silent).


@pytest.mark.parametrize("text", NARRATED_ACTION)
def test_action_detector_fires_on_an_intent_then_report(text):
    """Each measured fabrication grammar is detected at the pure-text level."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(text) is not None


@pytest.mark.parametrize("text", ANNOUNCED_THEN_STOPPED)
def test_action_detector_is_silent_on_an_announcement_with_no_report(text):
    """Intent without a completion report is honest — announce-then-wait is ClaudIA's
    normal speech, and 24 of the corpus's 33 zero-tool preamble matches are this shape."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(text) is None


@pytest.mark.parametrize(
    "text", HONEST_ACTION_TALK + HONEST_BOOK_TALK + HONEST_STAGING_TALK + INNOCENT
)
def test_action_detector_is_silent_on_everything_else(text):
    """The verb allowlist, the result-noun gate, the user-source veto and the past-turn
    veto, each earned by a real innocent look-alike in the corpus."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(text) is None


def test_action_detector_ignores_the_models_own_composition():
    """'Here's the proposal exactly as specified:' presents the model's own work, not a
    tool result — the one false positive the corpus actually contains (msg 347)."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(
        "I'll check the level first. You flagged this as a test, so here's the "
        "proposal exactly as specified:"
    ) is None


def test_action_detector_requires_the_report_to_follow_the_intent():
    """Report before intent is a recap plus a new announcement, not a claim."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(
        "Here's the chart from our last session. Let me capture a fresh screenshot "
        "once you confirm the symbol."
    ) is None


def test_a_report_of_no_errors_is_still_a_report():
    """'Loaded and compiled — no errors' was T6's exact wording. A sentence-wide negation
    veto on the report half would swallow it; the veto list must stay clear of bare
    negation."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(
        "I'll load it into the Pine editor and compile it.Loaded and compiled — no "
        "errors. Clean compile, no warnings."
    ) is not None


@pytest.mark.parametrize("text", NARRATED_TOOL_RESULT)
def test_result_detector_fires_on_a_shown_payload(text):
    """A fenced block presented as a raw tool result, in a turn nothing ran — the
    fabricated-audit-trail shape (msg 380: an invented `_source: quote_get` payload
    produced on demand to authenticate an earlier invented quote)."""
    from claudia.agent import _claims_verbatim_tool_result

    assert _claims_verbatim_tool_result(text) is not None


@pytest.mark.parametrize("text", HONEST_RESULT_TALK)
def test_result_detector_is_silent_on_honest_fence_talk(text):
    """Format explanations, hypotheticals, the model's own code, and the phrase without
    a fence must all stay silent."""
    from claudia.agent import _claims_verbatim_tool_result

    assert _claims_verbatim_tool_result(text) is None


async def test_narrated_action_produces_an_honest_notice():
    """The user must be told, in the same feed carrying the false report."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_ACTION[0]))
    )
    await agent.handle_message("switch to ZZZ and show me a screenshot")

    assert sink.messages[0] == NARRATED_ACTION[0]
    notice = sink.messages[-1]
    assert "never ran" in notice.lower()
    assert "no tool was called" in notice.lower()


async def test_narrated_action_writes_its_own_decision_type():
    """`action_claim_unbacked`, distinct from both siblings, so the decision log keeps
    the three failure modes separable."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_ACTION[0]))
    )
    await agent.handle_message("switch and screenshot")

    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "action_claim_unbacked"
    ]


async def test_narrated_action_uses_the_operator_channel():
    """The correction must reach the model on the channel it cannot forge, before the
    next turn — an uncorrected false claim becomes in-context precedent."""
    agent, _sink = _make_agent_recording()
    stream = MagicMock(side_effect=[
        _FakeStream(_text_response_events(NARRATED_ACTION[0])),
        _FakeStream(_text_response_events("Understood — I will call the tool for real.")),
    ])
    agent._client.messages.stream = stream

    await agent.handle_message("switch to ZZZ and screenshot it")
    await agent.handle_message("and now?")

    notes = _system_texts(stream.call_args_list[-1].kwargs["messages"])
    assert len(notes) == 1
    assert "no tool ran" in notes[0].lower()
    assert "announcing an action is not performing it" in notes[0].lower()


@pytest.mark.parametrize(
    "tool_name", ["get_market_snapshot", "chart_set_symbol", "get_doc_version"]
)
async def test_any_tool_call_clears_the_action_claim(tool_name):
    """The verdict is the turn's tool set: an IBKR read, a TV action or a local tool all
    clear it, because with any real call the turn's report may be grounded. The give-up —
    a turn where SOME tool ran and a DIFFERENT claimed action did not — is documented in
    the detector's docstring and deliberate."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        side_effect=_tool_then_text(tool_name, NARRATED_ACTION[0])
    )
    agent._toolkit.execute = MagicMock(return_value=("{}", None))
    agent._handle_local_tool = MagicMock(return_value="{}")

    await agent.handle_message("switch and screenshot")

    assert not any("never ran" in m.lower() for m in sink.messages)
    assert agent._store.get_decisions("test-session") == []


async def test_an_uploaded_image_clears_the_action_claim():
    """A screenshot the user dragged in is a guaranteed source under DATA INTEGRITY —
    describing it needs no tool, so the evidence veto is the image itself."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(
            "Let me read the chart image.Here's the chart — ZZZ 1H, RSI at 62, price "
            "pressing the upper band."
        ))
    )
    await agent.handle_message(
        "analyse this chart",
        images=[{"type": "image", "source": {"type": "base64", "data": "zzz"}}],
    )

    assert not any("never ran" in m.lower() for m in sink.messages)
    assert agent._store.get_decisions("test-session") == []


async def test_a_sibling_correction_suppresses_the_general_one():
    """msg 562's single sentence is both a book claim and an action report. Two
    corrections for two distinct lies is owed; a third that restates one of them is
    noise — the general detector stands down when a sibling has already corrected the
    turn."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(
            "I'll check the live book first, then stage the cancel.Confirmed against "
            "the live book — both ZZZ orders are working:"
        ))
    )
    await agent.handle_message("check the book and cancel one")

    decisions = [d["decision_type"] for d in agent._store.get_decisions("test-session")]
    assert "book_claim_unverified" in decisions
    assert "action_claim_unbacked" not in decisions


async def test_shown_payload_produces_its_own_notice():
    """The fabricated-payload shape gets its specific correction, not the generic one."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_TOOL_RESULT[0]))
    )
    await agent.handle_message("show me the raw tool result")

    notice = sink.messages[-1]
    assert "no tool ran" in notice.lower()
    assert "constructed" in notice.lower()
    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "result_claim_unbacked"
    ]


def test_unbacked_action_notice_never_claims_an_order_state():
    """The notice corrects an action claim; it must not read as a statement about
    orders, staging or the book — those corrections belong to the siblings."""
    from claudia.agent import _UNBACKED_ACTION_NOTICE

    lowered = _UNBACKED_ACTION_NOTICE.lower()
    for term in ("staged", "staging", "order", "book", "button"):
        assert term not in lowered, term


def test_action_verbs_and_report_participles_stay_in_step():
    """Drift guard: every verb in the intent allowlist must have its participle in the
    report set, so adding a verb to one list and not the other cannot pass silently —
    the intent would then match and its own completion report would not."""
    from claudia.agent import _REPORTED_COMPLETE, _TOOL_ACTION_VERB

    irregular = {"run": "ran", "read": "read"}
    doubling = {"grab", "scan"}
    for verb in _TOOL_ACTION_VERB.strip("(?:)").split("|"):
        if verb in irregular:
            participle = irregular[verb]
        elif verb in doubling:
            participle = verb + verb[-1] + "ed"
        elif verb.endswith("e"):
            participle = verb + "d"
        elif verb.endswith("y"):
            participle = verb[:-1] + "ied"
        else:
            participle = verb + "ed"
        # The dash context satisfies the predicate lookahead, as in "Compiled — clean".
        assert _REPORTED_COMPLETE.search(f"{participle.capitalize()} — done.") is not None, verb


# ── 2026-08-12 review: the false-positive surface, each an executed repro ─────
#
# Every case below fired against the shipped detectors before its veto existed.
# They are regression guards for the review's correctness findings, and they are
# what the corpus measurement cannot see: the corpus is one user's history, so a
# shape that never happened to occur in it is unconstrained by it.


@pytest.mark.parametrize("text", HONEST_ACTION_TALK_REVIEW)
def test_action_detector_is_silent_on_the_review_false_positives(text):
    """Own compositions, conditionals/futures, gerund idioms, and user-supplied
    content named in the report segment — all honest, all previously firing."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(text) is None


@pytest.mark.parametrize("text", HONEST_RESULT_TALK_REVIEW)
def test_result_detector_is_silent_on_the_review_false_positives(text):
    """Honest refusal, past-turn recap, and the model explaining its own correction."""
    from claudia.agent import _claims_verbatim_tool_result

    assert _claims_verbatim_tool_result(text) is None


@pytest.mark.parametrize("text", NARRATED_ACTION_REVIEW)
def test_action_detector_catches_the_review_false_negatives(text):
    """'already cached' is one adverb from a measured fabrication, and a second
    distinct lie in a two-lie message must still be caught."""
    from claudia.agent import _claims_completed_action

    assert _claims_completed_action(text) is not None


def test_contraction_negation_actually_vetoes():
    """`\\bn't\\b` could never match inside "can't" — no word boundary before the n —
    so the contraction branch of the oldest veto was dead from the day it shipped.
    Pinned directly, because its failure is silent everywhere it is used."""
    from claudia.agent import _GOVERNING_OPERATOR

    for text in ("can't", "didn't", "couldn\u2019t", "wasn't"):
        assert _GOVERNING_OPERATOR.search(text) is not None, text


async def test_two_distinct_lies_in_one_message_earn_two_corrections():
    """The turn-wide stand-down let the second lie stand. Suppression is now per
    overlapping *sentence*: a book claim in sentence A does not excuse a fabricated
    screenshot report in sentence C."""
    agent, _sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_ACTION_REVIEW[1]))
    )
    await agent.handle_message("check the book, then screenshot the chart")

    types = [d["decision_type"] for d in agent._store.get_decisions("test-session")]
    assert "book_claim_unverified" in types
    assert "action_claim_unbacked" in types


async def test_an_image_does_not_clear_a_fabricated_payload():
    """The image veto is calibrated to *describing* an upload. No uploaded image can
    ground the provenance of a fenced 'raw tool result', so the payload detector is
    deliberately not exempted."""
    agent, sink = _make_agent_recording()
    agent._client.messages.stream = MagicMock(
        return_value=_FakeStream(_text_response_events(NARRATED_TOOL_RESULT[0]))
    )
    await agent.handle_message(
        "show me the raw payload behind that number",
        images=[{"type": "image", "source": {"type": "base64", "data": "zzz"}}],
    )

    assert [d["decision_type"] for d in agent._store.get_decisions("test-session")] == [
        "result_claim_unbacked"
    ]
    assert any("constructed" in m.lower() for m in sink.messages)


async def test_every_correction_persists_before_it_displays():
    """The one safety property of the shared emitter: the record must exist before the
    sink is touched, so a failing chat feed cannot cost a correction. Asserted by
    breaking the sink and checking the row survives — for every correction shape."""
    from claudia.agent import (
        _STALE_BOOK_CLAIM_NOTICE,
        _UNBACKED_ACTION_NOTICE,
        _UNBACKED_CLAIM_NOTICE,
        _UNBACKED_RESULT_NOTICE,
    )

    shapes = [
        ("_emit_unbacked_claim_notice", _UNBACKED_CLAIM_NOTICE, "proposal_claim_unbacked"),
        ("_emit_stale_book_claim_notice", _STALE_BOOK_CLAIM_NOTICE, "book_claim_unverified"),
        ("_emit_unbacked_action_notice", _UNBACKED_ACTION_NOTICE, "action_claim_unbacked"),
        ("_emit_unbacked_result_notice", _UNBACKED_RESULT_NOTICE, "result_claim_unbacked"),
    ]
    for method, notice, decision_type in shapes:
        agent, sink = _make_agent_recording()
        sink.send_message = AsyncMock(side_effect=RuntimeError("chat feed down"))
        with pytest.raises(RuntimeError):
            await getattr(agent, method)(1, "some claimed sentence")
        decisions = agent._store.get_decisions("test-session")
        assert [d["decision_type"] for d in decisions] == [decision_type], method
        # The user-facing notice is persisted as an assistant row before the sink runs,
        # so the correction survives in the transcript even though the display failed.
        assert notice in agent._store.get_history("test-session")[-1]["content"], method
        assert agent._pending_operator_notes, method
        # The offending sentence is logged only — never persisted anywhere.
        assert not any("some claimed sentence" in (d.get("summary_text") or "")
                       for d in decisions), method


async def test_the_render_failure_notice_persists_before_it_displays():
    """The fifth shape, and the one that was left out.

    `_emit_guardrail_notice` hand-rolled the same persist→record→queue→display sequence
    until 2026-08-12 and was the only correction the ordering test above did not cover —
    found by an independent audit of the reference doc, not by the suite. It now routes
    through `_emit_correction`; this pins that, including the `metadata` the other four
    do not carry."""
    from claudia.agent import _GUARDRAIL_NOTICE

    agent, sink = _make_agent_recording()
    sink.send_message = AsyncMock(side_effect=RuntimeError("chat feed down"))
    with pytest.raises(RuntimeError):
        await agent._emit_guardrail_notice("cancel", 1)
    decisions = agent._store.get_decisions("test-session")
    assert [d["decision_type"] for d in decisions] == ["proposal_render_failed"]
    assert decisions[0]["metadata"] == {"kind": "cancel"}
    assert _GUARDRAIL_NOTICE in agent._store.get_history("test-session")[-1]["content"]
    assert agent._pending_operator_notes


def test_gerund_stems_stay_within_the_verb_allowlist():
    """Drift guard for the list the participle test cannot see: every gerund stem must
    correspond to a verb in the allowlist, so the two lists cannot diverge silently in
    the direction that widens the trigger."""
    from claudia.agent import _TOOL_ACTION_GERUND, _TOOL_ACTION_VERB

    verbs = set(_TOOL_ACTION_VERB.strip("(?:)").split("|"))
    stems = _TOOL_ACTION_GERUND.replace("ing", "").strip("(?:)").split("|")
    for stem in stems:
        # Stems are the verb, optionally minus a final 'e' or plus a doubled consonant.
        candidates = {stem, stem + "e", stem[:-1]}
        assert candidates & verbs, f"gerund stem {stem!r} has no verb in the allowlist"
