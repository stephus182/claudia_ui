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
    """A chat interface stub that records what was sent."""
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
    """Text with no pine fence yields no blocks."""
    assert extract_pine_blocks("Just some prose, no code at all.") == []


def test_extract_pine_blocks_ignores_non_pine_fences():
    """A python or plain fence is not mistaken for pine."""
    text = "```python\nprint('hi')\n```\n```js\nconsole.log(1)\n```"
    assert extract_pine_blocks(text) == []


def test_extract_pine_blocks_single():
    """One fenced block is extracted with its body intact."""
    text = "Here you go:\n```pine\n//@version=5\nstrategy(\"X\")\n```\nEnjoy."
    assert extract_pine_blocks(text) == ['//@version=5\nstrategy("X")']


def test_extract_pine_blocks_strips_surrounding_whitespace():
    """Leading and trailing whitespace is stripped from the extracted code."""
    text = "```pine\n\n   //@version=5\n   \n```"
    # Inner content is stripped on both ends.
    assert extract_pine_blocks(text) == ["//@version=5"]


def test_extract_pine_blocks_tolerates_info_string_after_pine():
    """An info string after the language tag still parses as a pine block."""
    text = "```pine version=5 title=foo\nindicator(\"RSI\")\n```"
    assert extract_pine_blocks(text) == ['indicator("RSI")']


def test_extract_pine_blocks_matches_the_pinescript_tag():
    """`pinescript` is Pine Script's canonical tag and MUST be detected.

    This test replaces one that asserted the opposite. The original regex used `pine\\b`
    to "avoid matching a longer fence tag", naming ```pinescript as the case to reject —
    but that tag *is* Pine Script, so the guard was excluding the commonest true match.
    Live on 2026-08-11 the model tagged a block `pinescript`, no block was detected, and
    the Copy/Inject buttons silently never rendered. It used `pine` minutes later and they
    did, which is why the defect reads as intermittent rather than broken.
    """
    text = "```pinescript\nindicator(\"RSI\")\n```"
    assert extract_pine_blocks(text) == ['indicator("RSI")']


def test_extract_pine_blocks_matches_the_hyphenated_tag():
    """`pine-script` is the third spelling in the wild."""
    text = "```pine-script\nindicator(\"RSI\")\n```"
    assert extract_pine_blocks(text) == ['indicator("RSI")']


def test_extract_pine_blocks_matches_regardless_of_tag_case():
    """Fence tags are conventionally lowercase, but a model is free to capitalise."""
    for tag in ("Pine", "PINE", "PineScript"):
        text = f"```{tag}\nindicator(\"RSI\")\n```"
        assert extract_pine_blocks(text) == ['indicator("RSI")'], tag


def test_extract_pine_blocks_does_not_match_an_unrelated_tag_beginning_with_pine():
    """The word-boundary guard still earns its place: only Pine spellings match.

    Widening to the real Pine tags must not widen to any tag that merely starts with
    "pine" — that is the one thing the original `\\b` got right.
    """
    assert extract_pine_blocks("```pineapple\nnot pine\n```") == []
    assert extract_pine_blocks("```pinecone\nnot pine\n```") == []


def test_extract_pine_blocks_multiple():
    """Several blocks in one message are all extracted, in order."""
    text = (
        "First:\n```pine\nstudy(\"A\")\n```\n"
        "Second:\n```pine v5\nstudy(\"B\")\n```\n"
    )
    assert extract_pine_blocks(text) == ['study("A")', 'study("B")']


# ── render_pinescript_blocks (Panel wiring) ───────────────────────────────────


@pytest.mark.asyncio
async def test_render_sends_one_row_of_two_buttons_per_block():
    """Each block gets its own copy and inject buttons."""
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
    """Copy is a real client-side clipboard write, so the code is passed to the JS callback."""
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
    """Guards each COPY button's arg binding only. NOTE: Copy is closure-IMMUNE —
    js_on_click serializes `code` eagerly at registration, so these args are
    correct even if the loop-closure bug is present. The inject-path guard (the
    thing _render_pine_block actually protects) is the separate test below."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```\n```pine\nstudy(\"B\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    copy_a = chat.send.call_args_list[0].args[0][0]
    copy_b = chat.send.call_args_list[1].args[0][0]
    assert Link.registry[copy_a][0].args == {"code": 'study("A")'}
    assert Link.registry[copy_b][0].args == {"code": 'study("B")'}


@pytest.mark.asyncio
async def test_render_multiple_blocks_each_inject_button_sends_its_own_source():
    """Real teeth for the loop-closure fix _render_pine_block exists to provide:
    firing BOTH inject handlers must call execute with each block's OWN source. An
    inlined loop would bind every handler to the last block ('study("B")' twice).
    The copy-args test above can't catch this — only the async inject handler
    closes over the per-block variable."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```\n```pine\nstudy(\"B\")\n```"
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: bridge)

    inject_a = chat.send.call_args_list[0].args[0][1]
    inject_b = chat.send.call_args_list[1].args[0][1]
    await _get_click_callback(inject_a)(None)
    await _get_click_callback(inject_b)(None)

    sources = [c.args[1]["source"] for c in bridge.execute.await_args_list]
    assert sources == ['study("A")', 'study("B")']


@pytest.mark.asyncio
async def test_inject_when_bridge_none_sends_not_connected_and_never_calls_execute():
    """With no TradingView bridge, inject reports not-connected and calls nothing."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=lambda: None)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    last = chat.send.call_args
    assert "not connected" in last.args[0].lower()
    assert last.kwargs["user"] == "System"
    # Recoverable (TV not launched yet) → button re-enabled for retry (idempotent
    # inject, unlike order flow's one-shot).
    assert inject_btn.disabled is False


@pytest.mark.asyncio
async def test_inject_when_getter_itself_none_sends_not_connected():
    # The sink default is tv_bridge_getter=None (not a getter returning None).
    """With no bridge getter at all, inject reports not-connected."""
    chat = _make_chat()
    text = "```pine\nstudy(\"A\")\n```"
    await render_pinescript_blocks(chat, text, tv_bridge_getter=None)

    inject_btn = _first_row(chat)[1]
    await _get_click_callback(inject_btn)(None)

    assert "not connected" in chat.send.call_args.args[0].lower()


def test_pine_inject_succeeded_classifies_result_shapes():
    # Explicit success signal — the only truthy case.
    """Each observed sidecar result shape is classified as success or failure."""
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
    """A successful inject calls the sidecar's set-source tool and confirms it."""
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
    assert "CDP connection failed after 5 attempts" in last.args[0]  # error surfaced
    assert '{"success"' not in last.args[0]  # M2: clean error text, not raw JSON braces
    assert last.kwargs["user"] == "System"
    assert inject_btn.disabled is False  # recoverable (e.g. CDP dropped) → retryable


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
    """A failed inject reports honestly and never raises — injection can be retried."""
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
    assert inject_btn.disabled is False  # recoverable → retryable


def test_extract_pine_blocks_drops_an_empty_fence():
    """An empty fence must not produce a button row.

    It used to yield [""], and Inject on that calls pine_set_source("") — which CLEARS the
    user's Pine editor (`pine.js:266-277`, `m.editor.setValue("")`). Pre-existing, but
    widening the tag set from one spelling to three widened the ways to reach it.
    """
    f = "```"
    assert extract_pine_blocks(f + "pine\n" + f) == []
    assert extract_pine_blocks(f + "pinescript\n   \n" + f) == []
    # A real block alongside an empty one still comes through.
    text = f + "pine\n\n" + f + "\n" + f + "pine\nindicator(\"X\")\n" + f
    assert extract_pine_blocks(text) == ['indicator("X")']


# ── The button-driven call leaves a record ────────────────────────────────────
#
# panel_pinescript calls pine_set_source OUTSIDE the agent's tool loop, so until
# 2026-08-12 a button inject wrote no tool row: invisible to the called-tool ledger,
# invisible to the store's forensics. That gap is what made the 2026-07-10 mixed
# message — a REAL button inject and a FABRICATED compile in one breath —
# undiagnosable from the database alone, and it fed the fabrication itself: asked to
# "compile it", the model had no record the inject ever happened.


@pytest.mark.asyncio
async def test_inject_persists_a_tool_row_on_success():
    """The click is recorded exactly as the agent loop records a model call: a `tool`
    row carrying the raw result, written before any chat.send."""
    chat = _make_chat()
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    store = MagicMock()
    await render_pinescript_blocks(
        chat, "```pine\nstudy(\"A\")\n```", tv_bridge_getter=lambda: bridge,
        store=store, session_id="s-1",
    )
    await _get_click_callback(_first_row(chat)[1])(None)

    from claudia.panel_pinescript import UI_BUTTON_ORIGIN

    store.add_message.assert_called_once_with(
        "s-1", "tool",
        content=UI_BUTTON_ORIGIN,
        tool_name="pine_set_source",
        tool_input={"source": 'study("A")'},
        tool_result='{"success": true}',
    )


@pytest.mark.asyncio
async def test_inject_persists_the_failure_result_too():
    """A run that failed still ran — the raw result is the record, same polarity as the
    agent loop, which persists error results rather than sniffing them out."""
    chat = _make_chat()
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": false, "error": "no editor"}')
    store = MagicMock()
    await render_pinescript_blocks(
        chat, "```pine\nstudy(\"A\")\n```", tv_bridge_getter=lambda: bridge,
        store=store, session_id="s-1",
    )
    await _get_click_callback(_first_row(chat)[1])(None)

    assert store.add_message.call_args.kwargs["tool_result"] == (
        '{"success": false, "error": "no editor"}'
    )


@pytest.mark.asyncio
async def test_inject_without_a_store_still_injects():
    """The record is additive: no store wired (the pre-2026-08-12 construction) must
    not break the button."""
    chat = _make_chat()
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    await render_pinescript_blocks(
        chat, "```pine\nstudy(\"A\")\n```", tv_bridge_getter=lambda: bridge,
    )
    await _get_click_callback(_first_row(chat)[1])(None)

    bridge.execute.assert_awaited_once()
    assert "✅" in chat.send.call_args.args[0]


@pytest.mark.asyncio
async def test_inject_still_reports_success_when_the_record_fails():
    """Persist-first must not turn a store hiccup into a false failure report: the
    inject really happened, so the user is still told it happened."""
    chat = _make_chat()
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    store = MagicMock()
    store.add_message = MagicMock(side_effect=RuntimeError("db locked"))
    await render_pinescript_blocks(
        chat, "```pine\nstudy(\"A\")\n```", tv_bridge_getter=lambda: bridge,
        store=store, session_id="s-1",
    )
    await _get_click_callback(_first_row(chat)[1])(None)

    assert "✅" in chat.send.call_args.args[0]


@pytest.mark.asyncio
async def test_inject_row_is_stamped_as_a_button_click():
    """A click writes a `tool` row the model never called. Unstamped, it is
    byte-identical to a model call — and the forensic rule "a tool row between a user
    row and an assistant row is the model's evidence" would then be false, letting a
    future audit clear a genuine fabrication with a user's click."""
    from claudia.panel_pinescript import UI_BUTTON_ORIGIN

    chat = _make_chat()
    bridge = MagicMock()
    bridge.execute = AsyncMock(return_value='{"success": true}')
    store = MagicMock()
    await render_pinescript_blocks(
        chat, "```pine\nstudy(\"A\")\n```", tv_bridge_getter=lambda: bridge,
        store=store, session_id="s-1",
    )
    await _get_click_callback(_first_row(chat)[1])(None)

    assert store.add_message.call_args.kwargs["content"] == UI_BUTTON_ORIGIN
