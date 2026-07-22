"""Tests for ChainlitMessageSink — preserves exact current Chainlit UI behavior
behind the MessageSink protocol that ClaudIAAgent depends on."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.message_sink import ChainlitMessageSink


@pytest.mark.asyncio
async def test_send_message_calls_cl_message_send():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl", new=MagicMock()) as mock_cl:
        mock_cl.Message.return_value.send = AsyncMock()
        await sink.send_message("hello")
        mock_cl.Message.assert_called_once_with(content="hello")
        mock_cl.Message.return_value.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_max_tokens_warning_uses_system_author():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl", new=MagicMock()) as mock_cl:
        mock_cl.Message.return_value.send = AsyncMock()
        await sink.send_max_tokens_warning()
        _, kwargs = mock_cl.Message.call_args
        assert kwargs["author"] == "System"
        assert "truncated" in kwargs["content"].lower()


def test_tool_step_returns_cl_step_with_name_and_type_tool():
    sink = ChainlitMessageSink(session_id="s1")
    with patch("claudia.message_sink.cl", new=MagicMock()) as mock_cl:
        sink.tool_step("get_positions")
        mock_cl.Step.assert_called_once_with(name="get_positions", type="tool")


@pytest.mark.asyncio
async def test_send_order_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10}
    with patch("claudia.order_flow.render_order_proposal", new=AsyncMock()) as mock_render:
        await sink.send_order_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")


@pytest.mark.asyncio
async def test_send_cancel_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.order_flow.render_cancel_proposal", new=AsyncMock()) as mock_render:
        await sink.send_cancel_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")


@pytest.mark.asyncio
async def test_send_modify_proposal_delegates_to_order_flow_with_session_id():
    sink = ChainlitMessageSink(session_id="sess-42")
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.order_flow.render_modify_proposal", new=AsyncMock()) as mock_render:
        await sink.send_modify_proposal(proposal)
        mock_render.assert_awaited_once_with(proposal, session_id="sess-42")
