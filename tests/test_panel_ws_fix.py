"""Tests for claudia/panel_ws_fix.py — the bokeh-fastapi 0.1.8 disconnect bridge fix.
Without it, Starlette's raw WebSocket.receive() RETURNS the websocket.disconnect
message (never raises WebSocketDisconnect), bokeh_fastapi's _receive_loop drops it,
client_lost() never runs, and Panel session-destroy hooks never fire (probe-verified;
migration plan D7 notes)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudia.panel_ws_fix import _receive_loop_fixed, apply_ws_disconnect_fix


def _make_handler(messages):
    """Fake WSHandler self: _socket.receive() pops from `messages`."""
    handler = MagicMock()
    handler._socket.receive = AsyncMock(side_effect=list(messages))
    handler._receive = AsyncMock(return_value=None)
    return handler


@pytest.mark.asyncio
async def test_disconnect_message_calls_client_lost_and_exits():
    handler = _make_handler([{"type": "websocket.disconnect", "code": 1001}])
    await _receive_loop_fixed(handler)  # must terminate — a hang fails via timeout
    handler.application.client_lost.assert_called_once_with(handler.connection)


@pytest.mark.asyncio
async def test_text_frames_still_processed_before_disconnect():
    handler = _make_handler(
        [{"text": "frame1"}, {"type": "websocket.disconnect", "code": 1000}]
    )
    await _receive_loop_fixed(handler)
    handler._receive.assert_awaited_once_with("frame1")
    handler.application.client_lost.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_exception_also_breaks():
    from starlette.websockets import WebSocketDisconnect

    handler = MagicMock()
    handler._socket.receive = AsyncMock(side_effect=WebSocketDisconnect(1006))
    await _receive_loop_fixed(handler)
    handler.application.client_lost.assert_called_once_with(handler.connection)


def test_apply_patches_known_broken_version():
    with (
        patch("claudia.panel_ws_fix._KNOWN_BROKEN", frozenset({"9.9.9"})),
        patch("claudia.panel_ws_fix._installed_version", return_value="9.9.9"),
        patch("claudia.panel_ws_fix.WSHandler") as mock_handler_cls,
    ):
        applied = apply_ws_disconnect_fix()
    assert applied is True
    assert mock_handler_cls._receive_loop is _receive_loop_fixed


def test_apply_skips_and_warns_on_unknown_version(caplog):
    with (
        patch("claudia.panel_ws_fix._installed_version", return_value="0.2.0"),
        patch("claudia.panel_ws_fix.WSHandler") as mock_handler_cls,
    ):
        original = mock_handler_cls._receive_loop
        applied = apply_ws_disconnect_fix()
    assert applied is False
    assert mock_handler_cls._receive_loop is original
    assert any("re-verify" in r.message for r in caplog.records)
