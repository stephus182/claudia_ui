"""Tests for ConversationStore — SQLite schema, FTS5 search, CRUD."""


import pytest

from claudia.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "test.db")


def test_create_and_get_session(store):
    store.create_session("sess-1", context_hash="abc123")
    session = store.get_session("sess-1")
    assert session is not None
    assert session["id"] == "sess-1"
    assert session["context_hash"] == "abc123"
    assert session["ended_at"] is None


def test_close_session(store):
    store.create_session("sess-2")
    store.close_session("sess-2", metadata={"model": "test"})
    session = store.get_session("sess-2")
    assert session["ended_at"] is not None


def test_add_and_get_messages(store):
    store.create_session("sess-3")
    store.add_message("sess-3", "user", "Hello ClaudIA")
    store.add_message("sess-3", "assistant", "Hello! How can I help?")
    history = store.get_history("sess-3")
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"


def test_add_tool_message(store):
    store.create_session("sess-4")
    mid = store.add_message(
        "sess-4",
        "tool",
        tool_name="get_positions",
        tool_input={"account": "U123"},
        tool_result="AAPL: 100 shares",
    )
    assert mid > 0
    history = store.get_history("sess-4")
    assert history[0]["tool_name"] == "get_positions"


def test_fts5_search(store):
    store.create_session("sess-5")
    store.add_message("sess-5", "user", "What is my NVDA position?")
    store.add_message("sess-5", "assistant", "You hold 50 shares of NVDA at $850 average.")
    results = store.search_messages("NVDA position")
    assert any("NVDA" in (r.get("content") or "") for r in results)


def test_add_and_search_decisions(store):
    store.create_session("sess-6")
    store.add_decision(
        session_id="sess-6",
        decision_type="trade_proposed",
        summary_text="BUY 50 AAPL at limit 185 — momentum breakout",
        symbol="AAPL",
    )
    results = store.search_decisions("AAPL momentum")
    assert len(results) >= 1
    assert results[0]["symbol"] == "AAPL"


def test_get_decisions_for_symbol(store):
    store.create_session("sess-7")
    store.add_decision("sess-7", "trade_staged", "STAGED BUY 10 MSFT", symbol="MSFT")
    store.add_decision("sess-7", "backtest_run", "Backtest 20/50 SMA on MSFT", symbol="MSFT")
    decisions = store.get_decisions_for_symbol("MSFT")
    assert len(decisions) == 2


def test_relationships(store):
    store.create_session("sess-8")
    store.add_relationship("TSLA", "pattern", "Often gaps up on earnings", session_id="sess-8")
    store.add_relationship("TSLA", "risk", "High IV before earnings", session_id="sess-8")
    rels = store.get_relationships("TSLA")
    assert len(rels) == 2


def test_list_sessions(store):
    for i in range(3):
        store.create_session(f"list-sess-{i}")
    sessions = store.list_sessions()
    assert len(sessions) >= 3


def test_history_limit(store):
    store.create_session("sess-limit")
    for i in range(60):
        store.add_message("sess-limit", "user", f"message {i}")
    history = store.get_history("sess-limit", limit=10)
    assert len(history) == 10


def test_get_last_context_hash_no_sessions(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    assert store.get_last_context_hash() is None


def test_get_last_context_hash_open_session_returned(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-open", context_hash="abc123")
    # Open session is still returned — we track all sessions, not just closed ones
    assert store.get_last_context_hash() == "abc123"


def test_get_last_context_hash_returns_most_recent_started(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-1", context_hash="hash-old")
    store.close_session("sess-1")
    store.create_session("sess-2", context_hash="hash-new")
    store.close_session("sess-2")
    assert store.get_last_context_hash() == "hash-new"


def test_get_last_context_hash_includes_open_session(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-closed", context_hash="hash-old")
    store.close_session("sess-closed")
    store.create_session("sess-open", context_hash="hash-new")
    # Open session is most recently started — it should be returned
    assert store.get_last_context_hash() == "hash-new"


# ── Doc versions ──────────────────────────────────────────────────────────────

def test_register_doc_version_first_time_is_v1(store):
    label = store.register_doc_version_if_new("hash-a", "context text", "principles text")
    assert label == "v1"


def test_register_doc_version_same_hash_returns_same_label(store):
    store.register_doc_version_if_new("hash-a", "context", "principles")
    label2 = store.register_doc_version_if_new("hash-a", "context", "principles")
    assert label2 == "v1"


def test_register_doc_version_new_hash_increments(store):
    store.register_doc_version_if_new("hash-a", "context v1", "principles v1")
    label2 = store.register_doc_version_if_new("hash-b", "context v2", "principles v2")
    assert label2 == "v2"


def test_get_version_label_known_hash(store):
    store.register_doc_version_if_new("hash-x", "ctx", "pri")
    assert store.get_version_label("hash-x") == "v1"


def test_get_version_label_unknown_hash_returns_none(store):
    assert store.get_version_label("nonexistent-hash") is None


def test_get_doc_version_returns_content(store):
    store.register_doc_version_if_new("hash-v1", "my context", "my principles")
    data = store.get_doc_version("v1")
    assert data is not None
    assert data["context_text"] == "my context"
    assert data["principles_text"] == "my principles"
    assert data["version"] == "v1"


def test_get_doc_version_unknown_returns_none(store):
    assert store.get_doc_version("v99") is None


def test_list_doc_versions_empty(store):
    assert store.list_doc_versions() == []


def test_list_doc_versions_ordered_oldest_first(store):
    store.register_doc_version_if_new("hash-1", "ctx1", "pri1")
    store.register_doc_version_if_new("hash-2", "ctx2", "pri2")
    versions = store.list_doc_versions()
    assert [v["version"] for v in versions] == ["v1", "v2"]


def test_create_session_stores_doc_version(store):
    store.create_session("sess-v1", context_hash="hash-a", doc_version="v1")
    session = store.get_session("sess-v1")
    assert session["doc_version"] == "v1"


def test_search_decisions_includes_doc_version(store):
    store.register_doc_version_if_new("hash-v1", "ctx", "pri")
    store.create_session("sess-dec", context_hash="hash-v1", doc_version="v1")
    store.add_decision("sess-dec", "trade_proposed", "BUY 100 AAPL: strong breakout", symbol="AAPL")
    results = store.search_decisions("AAPL breakout")
    assert results
    assert results[0]["doc_version"] == "v1"
