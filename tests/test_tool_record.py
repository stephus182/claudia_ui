"""The out-of-loop tool seam — gap #21.

Tools run outside `handle_message` write no `tool` row and never reach the called-tool
ledger. The Pine Inject button was fixed in 2026-08-12 as one instance; this is the class.
Why it matters for fabrication specifically: the measured "Cache miss — … It worked" family
is about exactly the bar cache an unrecorded chart-pane fetch mutates, so the model can
later narrate a fetch it never made with neither the ledger nor the DB able to show what
actually ran.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claudia.tool_record import STARTUP_ORIGIN, UI_BUTTON_ORIGIN, record_and_execute


@pytest.fixture
def toolkit():
    """A toolkit stand-in whose execute() returns the (summary, payload) pair."""
    tk = MagicMock()
    tk.execute.return_value = ("42 trades synced", {"rows": 42})
    return tk


@pytest.mark.asyncio
async def test_it_returns_what_the_tool_returned(toolkit):
    """The seam is transparent: callers keep the result shape they had before."""
    result = await record_and_execute(
        toolkit, "sync_flex_trades", {}, store=None, session_id=None, origin=STARTUP_ORIGIN
    )
    assert result == ("42 trades synced", {"rows": 42})
    toolkit.execute.assert_called_once_with("sync_flex_trades", {})


@pytest.mark.asyncio
async def test_it_writes_a_tool_row_stamped_with_its_origin(toolkit):
    """The record is the point: name, input, raw result and WHO caused it."""
    store = MagicMock()
    await record_and_execute(
        toolkit, "sync_flex_trades", {"a": 1}, store=store, session_id="s1", origin=STARTUP_ORIGIN
    )
    kwargs = store.add_message.call_args.kwargs
    args = store.add_message.call_args.args
    assert args[:2] == ("s1", "tool")
    assert kwargs["content"] == STARTUP_ORIGIN
    assert kwargs["tool_name"] == "sync_flex_trades"
    assert kwargs["tool_input"] == {"a": 1}
    assert kwargs["tool_result"] == ("42 trades synced", {"rows": 42})


@pytest.mark.asyncio
async def test_a_failing_store_does_not_cost_the_caller_its_result(toolkit):
    """A failing sink must not cost the action — the pine seam's rule, kept."""
    store = MagicMock()
    store.add_message.side_effect = RuntimeError("database is locked")
    result = await record_and_execute(
        toolkit, "check_flex_coverage", {}, store=store, session_id="s1", origin=STARTUP_ORIGIN
    )
    assert result == ("42 trades synced", {"rows": 42})


@pytest.mark.asyncio
async def test_no_session_means_no_row_but_the_tool_still_runs(toolkit):
    """Startup paths may have no session yet; the tool must not be blocked on bookkeeping."""
    store = MagicMock()
    await record_and_execute(
        toolkit, "check_flex_coverage", {}, store=store, session_id=None, origin=STARTUP_ORIGIN
    )
    store.add_message.assert_not_called()
    toolkit.execute.assert_called_once()


@pytest.mark.asyncio
async def test_a_raising_tool_is_recorded_and_then_re_raised(toolkit):
    """An attempted call is a fact. Recording only successes would hide the interesting half."""
    toolkit.execute.side_effect = RuntimeError("Flex 1025")
    store = MagicMock()
    with pytest.raises(RuntimeError, match="Flex 1025"):
        await record_and_execute(
            toolkit, "sync_flex_trades", {}, store=store, session_id="s1", origin=STARTUP_ORIGIN
        )
    kwargs = store.add_message.call_args.kwargs
    assert kwargs["tool_name"] == "sync_flex_trades"
    assert "Flex 1025" in str(kwargs["tool_result"])


def test_the_two_origins_are_distinct_and_stable():
    """The stamp separates a user's click from the model's own call; they must not collide."""
    assert UI_BUTTON_ORIGIN == "ui_button"
    assert STARTUP_ORIGIN == "startup"
    assert UI_BUTTON_ORIGIN != STARTUP_ORIGIN
