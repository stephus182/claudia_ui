"""Tests for claudia/panel_app.py's per-session app factory."""

from unittest.mock import AsyncMock, MagicMock, patch

from claudia.panel_app import _build_chat_app


def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.agent.AsyncAnthropic"),
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
        chat = _build_chat_app()

    assert chat.callback is not None


async def test_build_chat_app_callback_dispatches_to_agent_handle_message():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = MagicMock()
    mock_store.list_doc_versions.return_value = []
    mock_store.get_doc_version.return_value = None

    with (
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
