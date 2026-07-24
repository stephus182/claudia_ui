"""Tests for claudia/panel_app.py's per-session app factory.

Phase 5 (Task 5.1): _build_chat_app is now split — only the chat surface, callback
wiring, and welcome message are synchronous; everything else (GDrive DB download,
store, loader, agent construction) runs in a background _init_session task gated by
an asyncio.Event. Every test therefore needs a running event loop (asyncio.create_task
inside the factory), and awaiting the chat callback is the natural synchronization
point: it awaits init internally, so no sleeps or timing assertions. Every callback
await is wrapped in asyncio.wait_for so a future regression in the init gate (e.g.
_init_done.set() falling out of the finally) fails cleanly instead of hanging the
suite. Every test also awaits the callback BEFORE leaving its patch context — an init
task left merely scheduled would run after the patches are removed and construct real
singletons (empirically probed during review: one await asyncio.sleep(0) after the
with-block was enough to build a real toolkit/store).

Task 5.2: _init_session now reads context.md/principles.md from Drive (when
_gdrive_sync is set), registers the document version, writes a snapshot, warns on
hash change, and stamps the session row + agent with hash/version. Tests whose init
reaches that code use _make_mock_store/_configure_loader for the required returns and
patch claudia.panel_app._write_version_snapshot so no real files land under
docs/versions/.

Task 5.3: _init_session now sends the opening status message (account status +
trade/calendar context) via _send_opening_status before publishing the agent. The
nine tests whose init completes patch it with an AsyncMock: without the patch they
would still pass via the offline-degrade path, but only through incidental
MagicMock behavior — patching keeps them focused and deterministic. The two
failure-path tests don't patch it (their init never reaches the status code);
test_opening_status.py covers the builders themselves.

Unless a test targets the GDrive branch, GOOGLE_DRIVE_FOLDER_ID is blanked via
patch.dict so that branch of _init_session is skipped — unit tests must never touch
the real Drive (the developer .env sets that var, and panel_app's load_dotenv would
otherwise activate the branch).
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from claudia.panel_app import _DOCS_PATH, _build_chat_app

_NO_GDRIVE = {"GOOGLE_DRIVE_FOLDER_ID": ""}
_CALLBACK_TIMEOUT = 5


def _message_texts(chat) -> list[str]:
    return [(m.object if hasattr(m, "object") else str(m)) for m in chat.objects]


def _make_mock_store() -> MagicMock:
    """ConversationStore mock with the versioning defaults every happy-path init
    consumes (Task 5.2): no prior session hash (first run — no WARNING expected)
    and a fixed registered version label."""
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    store.get_last_context_hash.return_value = None
    store.register_doc_version_if_new.return_value = "v7"
    return store


def _configure_loader(mock_loader_cls: MagicMock) -> None:
    """Give a patched ContextLoader class the happy-path returns _init_session
    consumes: a REAL 2-tuple from get_effective_texts (the code unpacks it) and a
    stable hash."""
    loader = mock_loader_cls.return_value
    loader.load_system_prompt.return_value = "# Role\nStub."
    loader.reload_count = 0
    loader.get_effective_texts.return_value = ("ctx text", "pri text")
    loader.compute_hash.return_value = "hash123"


@pytest.mark.asyncio
async def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        # Drain the init task while the patches are still active (see module docstring).
        await asyncio.wait_for(chat.callback("x", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert chat.callback is not None


@pytest.mark.asyncio
async def test_build_chat_app_callback_waits_for_init_then_dispatches_to_agent():
    """The gating contract: a message sent immediately after render must wait for
    the background _init_session task to finish, then reach the real agent —
    never race it and never error out because the agent doesn't exist yet."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(
            chat.callback("hello world", "User", chat), timeout=_CALLBACK_TIMEOUT
        )

    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello world")


@pytest.mark.asyncio
async def test_build_chat_app_constructs_sink_with_the_real_store():
    """Code-quality review of Task 3.3 flagged this as untested: PanelMessageSink now
    needs store= wired through so staged/cancelled/modified orders actually get logged
    to ConversationStore.decisions — forgetting it silently defaults to None (no error,
    no test failure), the same class of silent audit-trail gap this project treats as
    non-negotiable elsewhere."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.agent.AsyncAnthropic"),
        patch("claudia.panel_app.PanelMessageSink") as mock_sink_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        chat = _build_chat_app()
        # Sink construction happens in the background init task — awaiting the
        # callback is the synchronization point that guarantees init has finished.
        await asyncio.wait_for(chat.callback("ping", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_sink_cls.assert_called_once()
    assert mock_sink_cls.call_args.kwargs["store"] is mock_store


@pytest.mark.asyncio
async def test_init_downloads_drive_db_before_first_store_open(monkeypatch):
    """Design D1 ordering: the GDrive DB download must COMPLETE before
    ConversationStore first opens the DB file — otherwise the store's sqlite
    connection would hold the old inode the download atomically replaces and its
    writes would be silently lost."""
    # Reset the module singletons this test exercises (order-independence;
    # monkeypatch restores the originals afterwards).
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", None)
    monkeypatch.setattr("claudia.panel_app._conv_store", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    # Shared manager records cross-mock call order.
    manager = Mock()
    mock_sync_cls = MagicMock()
    manager.attach_mock(mock_sync_cls.return_value.download_db, "download_db")
    mock_get_store = MagicMock(return_value=mock_store)
    manager.attach_mock(mock_get_store, "get_store")

    with (
        patch.dict(os.environ, {"GOOGLE_DRIVE_FOLDER_ID": "test-folder-id"}),
        patch("claudia.panel_app.Config"),
        patch("claudia.panel_app.GDriveSync", mock_sync_cls),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", mock_get_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    call_names = [name for name, _args, _kwargs in manager.mock_calls]
    assert "download_db" in call_names
    assert "get_store" in call_names
    assert call_names.index("download_db") < call_names.index("get_store")
    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_init_continues_without_drive_when_gdrive_sync_fails(monkeypatch):
    """Drive failure is non-fatal: a GDriveSync that blows up at construction must be
    logged and skipped — init still completes and the agent is usable."""
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", None)
    monkeypatch.setattr("claudia.panel_app._conv_store", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, {"GOOGLE_DRIVE_FOLDER_ID": "test-folder-id"}),
        patch("claudia.panel_app.Config"),
        patch("claudia.panel_app.GDriveSync", side_effect=RuntimeError("drive down")),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")
    assert not any("Session init failed" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_failure_missing_docs_sends_setup_required_and_callback_answers_honestly():
    """Missing context.md/principles.md must surface as a visible 'Setup required'
    message, and a subsequent user message must get an honest 'Setup required' reply —
    not a re-prefixed 'Session init failed' double label, and never reach an agent
    that was never built."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.side_effect = FileNotFoundError(
            "docs/context.md not found"
        )
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    # Two 'Setup required' messages: the init-time one AND the callback's honest reply.
    assert sum("Setup required" in t for t in texts) >= 2
    # The callback reply must not re-prefix 'Session init failed:' (double label).
    assert not any("Session init failed" in t for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_init_unexpected_failure_reports_error_not_crash():
    """Any unexpected init failure (here: toolkit construction blowing up) must be
    reported in-chat, never crash the session or leave the input gate deadlocked."""
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", side_effect=RuntimeError("boom")),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("Session init failed" in t for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_init_registers_doc_version_and_creates_session_with_metadata():
    """Parity with app.py:302-322 (design D3): every session registers the document
    version (idempotent), writes the human-readable snapshot, and stamps both the
    session row and the agent with the current hash + version label."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot") as mock_snapshot,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_store.register_doc_version_if_new.assert_called_once_with(
        "hash123", "ctx text", "pri text"
    )
    mock_snapshot.assert_called_once_with("v7", "ctx text", "pri text")
    create_kwargs = mock_store.create_session.call_args.kwargs
    assert create_kwargs["context_hash"] == "hash123"
    assert create_kwargs["doc_version"] == "v7"
    assert mock_agent_cls.call_args.kwargs["doc_version"] == "v7"


@pytest.mark.asyncio
async def test_init_hash_change_sends_warning():
    """When the current doc hash differs from the last session's, init must send the
    security WARNING naming both the previous and current version labels
    (app.py:309-320 parity)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    mock_store.get_last_context_hash.return_value = "oldhash"
    mock_store.get_version_label.return_value = "v6"

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("WARNING" in t and "v6" in t and "v7" in t for t in texts)
    # Ordering invariant (same technique as the D1 download-before-store test):
    # get_last_context_hash must run BEFORE this session's create_session — the
    # query reads the newest session row, so inserting ours first would make it
    # see its own hash and the warning would never fire again.
    call_names = [name for name, _args, _kwargs in mock_store.mock_calls]
    assert call_names.index("get_last_context_hash") < call_names.index("create_session")


@pytest.mark.asyncio
async def test_init_no_warning_when_hash_unchanged_or_first_run():
    """First run (no prior session row) must NOT produce the hash-change WARNING —
    get_last_context_hash returning None means there is nothing to compare against."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()  # get_last_context_hash already returns None

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert not any("WARNING" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_reads_context_docs_from_drive_when_sync_available(monkeypatch):
    """User-confirmed requirement: context.md/principles.md live in Google Drive —
    EVERY session must read them via GDriveSync.read_text (which itself falls back
    to the local file when Drive is unreachable or the file is absent), not from
    local disk alone (app.py:256-265 parity)."""
    mock_sync = MagicMock()

    def _drive_read_text(filename, local_path=None):
        return "drive ctx" if filename == "context.md" else "drive pri"

    mock_sync.read_text.side_effect = _drive_read_text
    # With _gdrive_sync already set, the download branch is skipped entirely (its
    # condition checks `_gdrive_sync is None`) — no GOOGLE_DRIVE_FOLDER_ID needed;
    # the _NO_GDRIVE env guard stays anyway for isolation. monkeypatch restores the
    # module global afterwards (same hygiene as the Task 5.1 GDrive tests).
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", mock_sync)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new_callable=AsyncMock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert mock_loader_cls.call_args.kwargs["context_text"] == "drive ctx"
    assert mock_loader_cls.call_args.kwargs["principles_text"] == "drive pri"
    # Local-fallback wiring: read_text must receive the local path so its internal
    # freshness guard / fallback can compare against (and fall back to) the file.
    mock_sync.read_text.assert_any_call("context.md", local_path=_DOCS_PATH / "context.md")
    mock_sync.read_text.assert_any_call("principles.md", local_path=_DOCS_PATH / "principles.md")


@pytest.mark.asyncio
async def test_init_sends_opening_status_and_stamps_trade_context():
    """Task 5.3: after the agent is built, init must send the status message
    (status block + trade status line) and stamp agent._trade_context BEFORE the
    input gate opens (app.py:399-514 parity) — an agent published without its
    trade context would silently answer without trade-history grounding."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch(
            "claudia.panel_app.gather_status_block",
            new=AsyncMock(return_value=("STATUS BLOCK", False)),
        ),
        patch(
            "claudia.panel_app.build_trade_lines",
            return_value=("trade status line", "TRADE CTX"),
        ) as mock_build,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("STATUS BLOCK" in t and "trade status line" in t for t in texts)
    assert mock_agent_cls.return_value._trade_context == "TRADE CTX"
    mock_build.assert_called_once_with(mock_toolkit, False)
    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_init_offline_flag_flows_from_gather_to_trade_lines():
    """Pins the ibkr_offline plumbing: gather_status_block's offline result must
    flow into build_trade_lines as its second argument — a hardcoded False or an
    argument swap must fail this test."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch(
            "claudia.panel_app.gather_status_block",
            new=AsyncMock(return_value=("OFFLINE BLOCK", True)),
        ),
        patch(
            "claudia.panel_app.build_trade_lines",
            return_value=("trade status line", "TRADE CTX"),
        ) as mock_build,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_build.assert_called_once_with(mock_toolkit, True)
    assert any("OFFLINE BLOCK" in t for t in _message_texts(chat))
