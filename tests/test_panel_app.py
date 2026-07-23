"""Tests for claudia/panel_app.py's per-session app factory.

Phase 5 (Task 5.1): _build_chat_app is now split — only the chat surface, callback
wiring, and welcome message are synchronous; everything else (GDrive DB download,
store, loader, agent construction) runs in a background _init_session task gated by
an asyncio.Event. Every test therefore needs a running event loop (asyncio.create_task
inside the factory), and `await chat.callback(...)` is the natural synchronization
point: the callback awaits init internally, so no sleeps or timing assertions.

All tests blank GOOGLE_DRIVE_FOLDER_ID via patch.dict so the GDrive branch of
_init_session is skipped — unit tests must never touch the real Drive (the developer
.env sets that var, and panel_app's load_dotenv would otherwise activate the branch).
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.panel_app import _build_chat_app

_NO_GDRIVE = {"GOOGLE_DRIVE_FOLDER_ID": ""}


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
        patch("claudia.agent.AsyncAnthropic"),
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
        chat = _build_chat_app()

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
        await chat.callback("hello world", "User", chat)

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
        await chat.callback("ping", "User", chat)

    mock_sink_cls.assert_called_once()
    assert mock_sink_cls.call_args.kwargs["store"] is mock_store


@pytest.mark.asyncio
async def test_init_failure_missing_docs_sends_setup_required_and_callback_answers_honestly():
    """Missing context.md/principles.md must surface as a visible 'Setup required'
    message, and a subsequent user message must get an honest 'Session init failed'
    answer instead of reaching an agent that was never built."""
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
        await chat.callback("hello", "User", chat)

    texts = _message_texts(chat)
    assert any("Setup required" in t for t in texts)
    assert any("Session init failed" in t for t in texts)
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
        await chat.callback("hello", "User", chat)

    texts = _message_texts(chat)
    assert any("Session init failed" in t for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()
