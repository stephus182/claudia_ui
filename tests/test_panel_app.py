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

Unless a test targets the GDrive branch, GOOGLE_DRIVE_FOLDER_ID is blanked via
patch.dict so that branch of _init_session is skipped — unit tests must never touch
the real Drive (the developer .env sets that var, and panel_app's load_dotenv would
otherwise activate the branch).
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from claudia.panel_app import _build_chat_app

_NO_GDRIVE = {"GOOGLE_DRIVE_FOLDER_ID": ""}
_CALLBACK_TIMEOUT = 5


def _message_texts(chat) -> list[str]:
    return [(m.object if hasattr(m, "object") else str(m)) for m in chat.objects]


@pytest.mark.asyncio
async def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.agent.AsyncAnthropic"),
        patch("claudia.panel_app.PanelMessageSink") as mock_sink_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

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
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch.dict(os.environ, {"GOOGLE_DRIVE_FOLDER_ID": "test-folder-id"}),
        patch("claudia.panel_app.Config"),
        patch("claudia.panel_app.GDriveSync", side_effect=RuntimeError("drive down")),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

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
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

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
