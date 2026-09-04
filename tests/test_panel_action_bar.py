"""Tests for claudia.panel_action_bar — reconnect buttons whose colour is the light."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import panel as pn
import pytest

from claudia.panel_action_bar import SERVICE_LABELS, ActionBar
from claudia.status import ConnectivityChecker, ServiceStatus
from tests.conftest import _get_click_callback


def _bar(**overrides):
    """An ActionBar with AsyncMock reconnects, a MagicMock error sink and an empty status."""
    kwargs = {
        "reconnect": {k: AsyncMock() for k in SERVICE_LABELS},
        "end_session": AsyncMock(),
        "on_error": MagicMock(),
        "status": lambda: {},
    }
    kwargs.update(overrides)
    return ActionBar(**kwargs), kwargs


def test_service_keys_match_the_real_checker_status_keys():
    """The buttons are keyed like ConnectivityChecker.get_status() — pinned against the
    REAL checker (constructor only, no network), not a hand-written dict."""
    checker = ConnectivityChecker(gateway_url="http://x", gdrive_token_file=Path("/nonexistent"))
    assert set(SERVICE_LABELS) == set(checker.get_status())


def test_row_holds_the_three_service_buttons_then_end_session_in_order():
    """IBKR, TradingView, Drive, End Session — always present, in that order."""
    bar, _ = _bar()
    assert isinstance(bar.row, pn.Row)
    assert [b.label for b in bar.row.objects] == ["IBKR", "TradingView", "Drive", "End Session"]
    assert all(isinstance(b, pn.widgets.Button) for b in bar.row.objects)


def test_repaint_maps_status_to_colour():
    """OK → success (green), ERROR → danger (red), UNKNOWN → light (grey)."""
    bar, _ = _bar()
    bar.repaint({
        "ibkr": ServiceStatus.OK,
        "tv": ServiceStatus.ERROR,
        "gdrive": ServiceStatus.UNKNOWN,
    })
    assert bar.buttons["ibkr"].color == "success"
    assert bar.buttons["tv"].color == "danger"
    assert bar.buttons["gdrive"].color == "light"


def test_repaint_ignores_unknown_keys_and_leaves_missing_ones_alone():
    """A status dict with extra or missing keys neither raises nor repaints the others."""
    bar, _ = _bar()
    bar.repaint({"ibkr": ServiceStatus.OK})
    bar.repaint({"nope": ServiceStatus.ERROR})
    assert bar.buttons["ibkr"].color == "success"
    assert bar.buttons["tv"].color == "light"


def test_drive_unknown_is_disabled_because_nothing_is_configured():
    """UNKNOWN on Drive means no credentials — the button cannot reconnect and says why."""
    bar, _ = _bar()
    bar.repaint({"gdrive": ServiceStatus.UNKNOWN})
    assert bar.buttons["gdrive"].disabled is True
    assert "GOOGLE_DRIVE_FOLDER_ID" in bar.buttons["gdrive"].description
    bar.repaint({"gdrive": ServiceStatus.ERROR})
    assert bar.buttons["gdrive"].disabled is False


def test_ibkr_unknown_stays_clickable():
    """UNKNOWN on IBKR is 'not checked yet', not 'not configured' — a click may reconnect."""
    bar, _ = _bar()
    bar.repaint({"ibkr": ServiceStatus.UNKNOWN})
    assert bar.buttons["ibkr"].disabled is False


@pytest.mark.asyncio
async def test_click_awaits_the_injected_reconnect_once_and_repaints_after():
    """A click runs that service's reconnect exactly once, then repaints from `status`."""
    status = MagicMock(return_value={"tv": ServiceStatus.OK})
    bar, kw = _bar(status=status)
    assert bar.buttons["tv"].color == "light"
    await _get_click_callback(bar.buttons["tv"])(None)
    status.assert_called_once()
    kw["reconnect"]["tv"].assert_awaited_once()
    for other in ("ibkr", "gdrive"):
        kw["reconnect"][other].assert_not_awaited()
    assert bar.buttons["tv"].color == "success"


@pytest.mark.asyncio
async def test_button_is_disabled_and_loading_while_its_reconnect_runs():
    """Disable-first: a second click cannot start a second gateway launch."""
    gate = asyncio.Event()
    seen: dict[str, tuple[bool, bool]] = {}

    async def slow() -> None:
        """Snapshot the button state mid-flight, then wait to be released."""
        seen["mid"] = (bar.buttons["ibkr"].disabled, bar.buttons["ibkr"].loading)
        await gate.wait()

    bar, _ = _bar(reconnect={"ibkr": slow, "tv": AsyncMock(), "gdrive": AsyncMock()})
    task = asyncio.create_task(_get_click_callback(bar.buttons["ibkr"])(None))
    await asyncio.sleep(0)
    assert seen["mid"] == (True, True)
    gate.set()
    await task
    assert (bar.buttons["ibkr"].disabled, bar.buttons["ibkr"].loading) == (False, False)


@pytest.mark.asyncio
async def test_a_raising_reconnect_reports_the_error_and_re_enables_the_button():
    """Never a stuck button, never a crash: the error goes to `on_error`, the button comes back."""
    bar, kw = _bar(reconnect={
        "ibkr": AsyncMock(side_effect=RuntimeError("docker gone")),
        "tv": AsyncMock(), "gdrive": AsyncMock(),
    })
    await _get_click_callback(bar.buttons["ibkr"])(None)
    kw["on_error"].assert_called_once()
    assert "docker gone" in kw["on_error"].call_args.args[0]
    assert "IBKR" in kw["on_error"].call_args.args[0]
    assert bar.buttons["ibkr"].disabled is False
    assert bar.buttons["ibkr"].loading is False


@pytest.mark.asyncio
async def test_end_session_awaits_its_callable_and_disables_every_button():
    """End Session runs the injected cleanup once and leaves the whole bar inert."""
    bar, kw = _bar()
    await _get_click_callback(bar.end_button)(None)
    kw["end_session"].assert_awaited_once()
    assert all(b.disabled for b in bar.row.objects)


@pytest.mark.asyncio
async def test_repaint_does_not_re_enable_a_button_mid_reconnect():
    """The 5-second repaint must not undo disable-first while a reconnect is in flight."""
    gate = asyncio.Event()

    async def slow() -> None:
        """Hold the reconnect open until the test releases it."""
        await gate.wait()

    bar, _ = _bar(reconnect={"ibkr": AsyncMock(), "tv": AsyncMock(), "gdrive": slow})
    task = asyncio.create_task(_get_click_callback(bar.buttons["gdrive"])(None))
    await asyncio.sleep(0)
    bar.repaint({"gdrive": ServiceStatus.ERROR})
    assert bar.buttons["gdrive"].disabled is True
    gate.set()
    await task
    assert bar.buttons["gdrive"].disabled is False
