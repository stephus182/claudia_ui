"""Tests for PanelMessageSink — the Panel-side MessageSink implementation.

send_message and tool_step have real, working (if basic) behavior; order/cancel/modify
proposal rendering delegates to claudia/panel_order_flow.py's render_*_proposal
functions, mirroring how tests/test_message_sink.py verifies ChainlitMessageSink's
equivalent delegation to claudia/order_flow.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_tool_step_success_streams_input_then_output_with_separator():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions") as step:
        step.input = '{"foo": "bar"}'
        step.output = "100 AAPL"
    sent_step = chat.send.call_args.args[0]
    assert sent_step.status == "success"
    # ChatStep.serialize() wraps the Markdown body in Python repr() (panel/chat/utils.py
    # serialize_recursively: `string = f"{label}={string!r}"`), so the two real newline
    # bytes our separator inserts show up here as the *escaped text* \n\n — hence the
    # doubled backslashes below to encode that escaped text correctly as a source literal.
    assert sent_step.serialize() == 'ChatStep(Markdown=\'Input: `{"foo": "bar"}`\\n\\nOutput: 100 AAPL\')'


@pytest.mark.asyncio
async def test_tool_step_exception_sets_failed_status_and_reraises():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    with pytest.raises(RuntimeError, match="boom"):
        async with sink.tool_step("get_positions") as step:
            step.input = "{}"
            raise RuntimeError("boom")
    sent_step = chat.send.call_args.args[0]
    assert sent_step.status == "failed"
    assert "boom" in sent_step.serialize()


@pytest.mark.asyncio
async def test_tool_step_sends_a_real_chatstep_not_a_plain_message():
    import panel as pn
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1")
    async with sink.tool_step("get_positions"):
        pass
    sent_step = chat.send.call_args.args[0]
    assert isinstance(sent_step, pn.chat.ChatStep)
    call_kwargs = chat.send.call_args.kwargs
    assert call_kwargs["user"] == "System"
    assert call_kwargs["respond"] is False


@pytest.mark.asyncio
async def test_send_order_proposal_delegates_to_panel_order_flow():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1", store=None)
    proposal = {"symbol": "AAPL", "action": "BUY", "quantity": 10}
    with patch("claudia.panel_order_flow.render_order_proposal", new=AsyncMock()) as mock_render:
        await sink.send_order_proposal(proposal)
        mock_render.assert_awaited_once_with(chat, proposal, session_id="s1", store=None)


@pytest.mark.asyncio
async def test_send_cancel_proposal_delegates_to_panel_order_flow():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1", store=None)
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.panel_order_flow.render_cancel_proposal", new=AsyncMock()) as mock_render:
        await sink.send_cancel_proposal(proposal)
        mock_render.assert_awaited_once_with(chat, proposal, session_id="s1", store=None)


@pytest.mark.asyncio
async def test_send_modify_proposal_delegates_to_panel_order_flow():
    chat = _make_chat()
    sink = PanelMessageSink(chat=chat, session_id="s1", store=None)
    proposal = {"order_id": "123", "symbol": "AAPL"}
    with patch("claudia.panel_order_flow.render_modify_proposal", new=AsyncMock()) as mock_render:
        await sink.send_modify_proposal(proposal)
        mock_render.assert_awaited_once_with(chat, proposal, session_id="s1", store=None)
