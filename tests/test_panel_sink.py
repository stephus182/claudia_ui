"""Tests for PanelMessageSink — the Panel-side MessageSink implementation.

Phase 2 scope only: send_message and tool_step have real, working (if basic) behavior;
order/cancel/modify proposal rendering is explicitly deferred to Phase 3 and sends a
plain, honest "not yet available" message rather than raising or silently dropping the
proposal — Phase 3 replaces this with the real button-pattern port.
"""

from unittest.mock import MagicMock

import pytest

from claudia.panel_sink import PanelMessageSink


def _make_chat():
    chat = MagicMock()
    chat.send = MagicMock()
    return chat


@pytest.mark.asyncio
async def test_send_message_sends_to_chat_interface_as_claudia():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_message("Hello there.")
    chat.send.assert_called_once_with("Hello there.", user="ClaudIA", respond=False)


@pytest.mark.asyncio
async def test_send_max_tokens_warning_sends_as_system():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_max_tokens_warning()
    args, kwargs = chat.send.call_args
    assert "truncated" in args[0].lower()
    assert kwargs["user"] == "System"


@pytest.mark.asyncio
async def test_tool_step_posts_then_updates_message_object():
    chat = _make_chat()
    posted_message = MagicMock()
    posted_message.object = ""
    chat.send.return_value = posted_message

    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions") as step:
        step.input = '{"foo": "bar"}'
        step.output = "100 AAPL"

    assert "get_positions" in posted_message.object
    assert "100 AAPL" in posted_message.object


@pytest.mark.asyncio
async def test_send_order_proposal_sends_placeholder_not_available_message():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    await sink.send_order_proposal({"symbol": "AAPL", "action": "BUY", "quantity": 10})
    args, kwargs = chat.send.call_args
    assert "not available" in args[0].lower() or "not yet available" in args[0].lower()
    assert kwargs["user"] == "System"
