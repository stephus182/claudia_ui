"""Tests for session_reporter — Markdown report generation."""

import pytest

from claudia.conversation_store import ConversationStore
from claudia.session_reporter import _TOOL_LABELS, generate_session_report


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "test.db")


@pytest.fixture
def session_with_tools(store):
    """Session with two tool calls, one error, one decision."""
    session_id = "test-session-reporter"
    store.create_session(session_id, context_hash="abc", doc_version="v1")
    store.add_message(session_id, role="user", content="What positions do I have?")
    store.add_message(session_id, role="assistant", content="Let me check.")
    store.add_message(
        session_id,
        role="tool",
        content="",
        tool_name="get_positions",
        tool_result='{"positions": []}',
    )
    store.add_message(
        session_id,
        role="tool",
        content="",
        tool_name="get_pnl",
        tool_result='{"pnl": 0}',
    )
    return session_id


@pytest.fixture
def session_with_error(store):
    """Session where one tool result contains an error keyword."""
    session_id = "test-session-error"
    store.create_session(session_id)
    store.add_message(
        session_id,
        role="tool",
        content="",
        tool_name="fetch_market_data",
        tool_result="Error: HMDS warmup — symbol subscription initializing",
    )
    return session_id


# ---------------------------------------------------------------------------
# Report file creation
# ---------------------------------------------------------------------------

def test_report_is_written_to_disk(store, session_with_tools, tmp_path, monkeypatch):
    """generate_session_report writes a .md file and returns its path."""
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store)
    assert path is not None
    assert path.exists()
    assert path.suffix == ".md"


def test_report_filename_is_timestamp(store, session_with_tools, tmp_path, monkeypatch):
    """Report filename matches YYYY-MM-DD-HHmm pattern."""
    import re
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store)
    assert path is not None
    assert re.match(r"\d{4}-\d{2}-\d{2}-\d{4}\.md", path.name)


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------

def test_report_contains_session_id(store, session_with_tools, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store)
    assert path is not None
    content = path.read_text()
    assert session_with_tools in content


def test_report_labels_known_tools(store, session_with_tools, tmp_path, monkeypatch):
    """Known tool names are converted to readable labels in the report."""
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store)
    assert path is not None
    content = path.read_text()
    assert "IBKR: positions" in content
    assert "IBKR: P&L" in content


def test_report_uses_raw_name_for_unknown_tool(store, tmp_path, monkeypatch):
    """Unknown tool names appear verbatim (no label entry needed for every tool)."""
    session_id = "sess-unknown-tool"
    store.create_session(session_id)
    store.add_message(session_id, role="tool", content="", tool_name="some_future_tool",
                      tool_result="ok")
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_id, store)
    assert path is not None
    assert "some_future_tool" in path.read_text()


def test_report_shows_tool_count_when_called_multiple_times(store, tmp_path, monkeypatch):
    """A tool called more than once shows ×N suffix."""
    session_id = "sess-multi-tool"
    store.create_session(session_id)
    for _ in range(3):
        store.add_message(session_id, role="tool", content="", tool_name="get_positions",
                          tool_result="[]")
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_id, store)
    assert path is not None
    assert "×3" in path.read_text()  # noqa: RUF001 — matches _tool_section's output format


def test_report_no_tools_shows_placeholder(store, tmp_path, monkeypatch):
    """Session with no tool calls shows the '(no tool calls)' line."""
    session_id = "sess-no-tools"
    store.create_session(session_id)
    store.add_message(session_id, role="user", content="Hello")
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_id, store)
    assert path is not None
    assert "no tool calls" in path.read_text()


def test_report_captures_error_results(store, session_with_error, tmp_path, monkeypatch):
    """Tool results containing 'Error' are included in the Errors section."""
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_error, store)
    assert path is not None
    content = path.read_text()
    assert "Errors / Anomalies" in content
    assert "fetch_market_data" in content
    assert "HMDS warmup" in content


def test_report_no_errors_shows_none_detected(store, session_with_tools, tmp_path, monkeypatch):
    """Clean session reports 'None detected' in the errors section."""
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store)
    assert path is not None
    assert "None detected" in path.read_text()


def test_report_includes_connectivity(store, session_with_tools, tmp_path, monkeypatch):
    """Connectivity state is included when provided."""
    monkeypatch.chdir(tmp_path)
    conn = {"ibkr": "OK", "gdrive": "OK", "tradingview": "ERROR"}
    path = generate_session_report(session_with_tools, store, connectivity=conn)
    assert path is not None
    content = path.read_text()
    assert "IBKR=OK" in content
    assert "GDrive=OK" in content
    assert "TradingView=ERROR" in content


def test_report_includes_doc_version(store, session_with_tools, tmp_path, monkeypatch):
    """Doc version label appears in the report header."""
    monkeypatch.chdir(tmp_path)
    path = generate_session_report(session_with_tools, store, doc_version="v2")
    assert path is not None
    assert "v2" in path.read_text()


def test_report_returns_none_on_bad_session(store, tmp_path, monkeypatch):
    """Non-existent session does not raise — returns a report with empty sections."""
    monkeypatch.chdir(tmp_path)
    # Non-existent session_id — store returns empty lists, report still written
    path = generate_session_report("nonexistent-session", store)
    assert path is not None  # still writes, just empty


# ---------------------------------------------------------------------------
# _TOOL_LABELS completeness
# ---------------------------------------------------------------------------

def test_all_local_tools_have_labels():
    """Every local tool defined in agent.py must have a label entry."""
    local_tools = {"list_doc_versions", "get_doc_version", "search_past_conversations", "fetch_web_page"}
    missing = local_tools - _TOOL_LABELS.keys()
    assert missing == set(), f"Missing labels for local tools: {missing}"


def test_key_ibkr_tools_have_labels():
    """High-frequency IBKR tools must have readable labels."""
    required = {
        "get_positions", "get_live_orders", "get_pnl", "get_account_summary",
        "get_trades", "sync_flex_trades", "create_price_alert", "modify_price_alert",
        "fetch_market_data",
    }
    missing = required - _TOOL_LABELS.keys()
    assert missing == set(), f"Missing labels for IBKR tools: {missing}"


def test_key_tv_tools_have_labels():
    """High-frequency TradingView tools must have readable labels."""
    required = {
        "chart_get_state", "quote_get", "pine_set_source", "pine_smart_compile",
        "capture_screenshot",
    }
    missing = required - _TOOL_LABELS.keys()
    assert missing == set(), f"Missing labels for TradingView tools: {missing}"
