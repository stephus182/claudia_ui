"""Tests for panel_pinescript.py — ```pine detection + Copy/Inject button rendering.

extract_pine_blocks is pure (regex over text); render_pinescript_blocks is the
Panel-specific wiring (a Copy button with a real client-side clipboard js_on_click,
and an Inject button whose async handler drives the TradingViewBridge). The bridge
is injected via a getter so these tests never touch a real tradingview-mcp sidecar.
"""

from unittest.mock import AsyncMock, MagicMock

import panel as pn
import pytest
from panel.links import Link

from claudia.panel_pinescript import (
    _pine_inject_succeeded,
    extract_pine_blocks,
    render_pinescript_blocks,
)
from tests.conftest import _get_click_callback


def _make_chat():
    chat = MagicMock()
    chat.send = MagicMock()
    return chat


def _buttons(container):
    """The child buttons of a sent Row/Column, returned untyped (Any). Panel's Row
    __getitem__ / iteration is typed Viewable | list[Viewable], which has no
    .label/.disabled — laundering through this untyped helper keeps attribute access
    on the real Button objects clean under mypy."""
    return list(container)


def _first_row(chat):
    """The pn.Row (Copy + Inject buttons) from the first chat.send call."""
    return chat.send.call_args_list[0].args[0]


# ── extract_pine_blocks (pure) ────────────────────────────────────────────────


def test_extract_pine_blocks_none_when_no_pine_fence():
    assert extract_pine_blocks("Just some prose, no code at all.") == []


def test_extract_pine_blocks_ignores_non_pine_fences():
    text = "```python\nprint('hi')\n```\n```js\nconsole.log(1)\n```"
    assert extract_pine_blocks(text) == []


def test_extract_pine_blocks_single():
    text = "Here you go:\n```pine\n//@version=5\nstrategy(\"X\")\n```\nEnjoy."
    assert extract_pine_blocks(text) == ['//@version=5\nstrategy("X")']


def test_extract_pine_blocks_strips_surrounding_whitespace():
    text = "```pine\n\n   //@version=5\n   \n```"
    # Inner content is stripped on both ends.
    assert extract_pine_blocks(text) == ["//@version=5"]


def test_extract_pine_blocks_tolerates_info_string_after_pine():
    text = "```pine version=5 title=foo\nindicator(\"RSI\")\n```"
    assert extract_pine_blocks(text) == ['indicator("RSI")']


def test_extract_pine_blocks_does_not_match_longer_fence_tag():
    # ```pinescript is a different (longer) tag — \b guards against a partial match.
    text = "```pinescript\nindicator(\"RSI\")\n```"
    assert extract_pine_blocks(text) == []


def test_extract_pine_blocks_multiple():
    text = (
        "First:\n```pine\nstudy(\"A\")\n```\n"
        "Second:\n```pine v5\nstudy(\"B\")\n```\n"
    )
    assert extract_pine_blocks(text) == ['study("A")', 'study("B")']


# ── render_pinescript_blocks (Panel wiring) ───────────────────────────────────


@pytest.mark.asyncio
async def test_render_sends_one_row_of_two_buttons_per_block():
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```\n```pine\nstudy(\"B\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    assert chat.send.call_count == 2  # one Row per block
    for call in chat.send.call_args_list:
        row = call.args[0]
        assert isinstance(row, pn.Row)
        copy_btn, inject_btn = _buttons(row)
        assert copy_btn.label == "Copy PineScript"
        assert inject_btn.label == "Inject into TradingView"
        assert call.kwargs["user"] == "ClaudIA — PineScript"
        assert call.kwargs["respond"] is False


@pytest.mark.asyncio
async def test_render_copy_button_carries_code_in_js_on_click_args():
    chat = _make_chat()
    code = 'strategy("Q")\n// "quotes" and `backticks` and \\n'
    text = f"```pine\n{code}\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    copy_btn = _first_row(chat)[0]
    # js_on_click registers a panel.links.Callback in Link.registry keyed by the button.
    callbacks = Link.registry[copy_btn]
    assert len(callbacks) == 1
    cb = callbacks[0]
    # Code is passed as a serialized arg (injection-safe), NOT interpolated into the JS.
    assert cb.args == {"code": code}
    assert cb.code["event:button_click"] == "navigator.clipboard.writeText(code)"


@pytest.mark.asyncio
async def test_render_multiple_blocks_each_copy_button_has_its_own_code():
    """Guards the loop-closure correctness: each Inject/Copy pair must bind its own
    block, not the last block's source."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```\n```pine\nstudy(\"B\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    copy_a = chat.send.call_args_list[0].args[0][0]
    copy_b = chat.send.call_args_list[1].args[0][0]
    assert Link.registry[copy_a][0].args == {"code": 'study("A")'}
    assert Link.registry[copy_b][0].args == {"code": 'study("B")'}


@pytest.mark.asyncio
async def test_inject_when_bridge_none_sends_not_connected_and_never_calls_execute():
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    last = chat.send.call_args
    assert "not connected" in last.args[0].lower()
    assert last.kwargs["user"] == "System"
    assert inject_btn.disabled is True


@pytest.mark.asyncio
async def test_inject_when_getter_itself_none_sends_not_connected():
    # The sink default is tv_bridge_getter=None (not a getter returning None).
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=None)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    assert "not connected" in chat.send.call_args.args[0].lower()


def test_pine_inject_succeeded_classifies_result_shapes():
    # Explicit success signal — the only truthy case.
    assert _pine_inject_succeeded('{"success": true}') is True
    assert _pine_inject_succeeded('{"success": true, "source": "internal_api"}') is True
    # Explicit failure from the sidecar.
    assert _pine_inject_succeeded('{"success": false, "error": "CDP connection failed"}') is False
    # Fail-safe: non-JSON status strings from execute()'s no-session / exception paths.
    assert _pine_inject_succeeded("TradingView is not connected.") is False
    assert _pine_inject_succeeded("TradingView tool 'pine_set_source' failed.") is False
    # Fail-safe: malformed / non-dict JSON never reads as success.
    assert _pine_inject_succeeded("{not valid json") is False
    assert _pine_inject_succeeded("true") is False  # valid JSON, but not a {"success": true} dict
    assert _pine_inject_succeeded("") is False


@pytest.mark.asyncio
async def test_inject_success_calls_pine_set_source_and_confirms():
    chat = _make_chat()
    code = 'study("A")'
    text = f"```pine\n{code}\n```"
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: bridge)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    bridge.execute.assert_awaited_once_with("pine_set_source", {"source": code})
    last = chat.send.call_args
    assert last.args[0] == "✅ Injected into the TradingView Pine Editor."
    assert last.kwargs["user"] == "ClaudIA"
    assert inject_btn.disabled is True


@pytest.mark.asyncio
async def test_inject_sidecar_reports_failure_is_not_reported_as_success():
    """The false-success fix: a {"success": false} sidecar result must NOT be prefixed
    'Injected'. It surfaces the honest failure + the raw error, as a System message."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    bridge = MagicMock()
    bridge.execute = AsyncMock(
        return_value='{"success": false, "error": "CDP connection failed after 5 attempts"}'
    )
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: bridge)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    last = chat.send.call_args
    assert "✅" not in last.args[0]
    assert "did not complete" in last.args[0]
    assert "CDP connection failed after 5 attempts" in last.args[0]  # raw error surfaced
    assert last.kwargs["user"] == "System"
    assert inject_btn.disabled is True


@pytest.mark.asyncio
async def test_inject_non_json_status_string_is_treated_as_failure():
    """execute()'s no-session path returns the literal 'TradingView is not connected.'
    string (not JSON) — fail-safe classifies it as a failure, never 'Injected'."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value="TradingView is not connected.")
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: bridge)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    last = chat.send.call_args
    assert "✅" not in last.args[0]
    assert "did not complete" in last.args[0]
    assert last.kwargs["user"] == "System"


@pytest.mark.asyncio
async def test_inject_failure_sends_honest_message_and_does_not_raise():
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    bridge = MagicMock()
    bridge.execute = AsyncMock(side_effect=RuntimeError("boom"))
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: bridge)

    inject_btn = _first_row(chat)[1]
    # No raise despite execute blowing up.
    await _get_click_callback(inject_btn)(None)

    last = chat.send.call_args
    assert "Injection failed" in last.args[0]
    assert last.kwargs["user"] == "System"
    assert inject_btn.disabled is True
