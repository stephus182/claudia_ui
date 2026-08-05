"""Tests for the stale-editable-install guard.

The guard exists because the same root cause bit three times (see
`claudia/install_check.py`). These tests therefore have to prove it *detects*, not merely
that it returns an empty list on a healthy machine — a check that can only ever pass is
the thing that let this hide for so long.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch


def test_reports_modules_present_in_source_but_missing_from_the_install(tmp_path):
    """The exact 2026-07-30 shape: `local_browser.py` exists on disk, the strict-editable
    snapshot still lists only the old `scrape_fallback.py`, and every lazy import of the
    new name fails at call time while ClaudIA starts perfectly."""
    from claudia.install_check import stale_modules

    source = tmp_path / "src" / "ibkr_core_mcp"
    snapshot = tmp_path / "build" / "__editable__.x" / "ibkr_core_mcp"
    source.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    for name in ("__init__.py", "claude_tools.py", "local_browser.py"):
        (source / name).write_text("")
    # The snapshot is what was captured at install time: no local_browser.
    for name in ("__init__.py", "claude_tools.py", "scrape_fallback.py"):
        (snapshot / name).write_text("")
    # __init__.py in the snapshot is a symlink into the source tree — that is what makes
    # the real source directory discoverable at runtime.
    (snapshot / "__init__.py").unlink()
    (snapshot / "__init__.py").symlink_to(source / "__init__.py")

    fake_pkg = types.ModuleType("ibkr_core_mcp")
    fake_pkg.__file__ = str(snapshot / "__init__.py")
    with patch.dict("sys.modules", {"ibkr_core_mcp": fake_pkg}):
        assert stale_modules() == ["local_browser.py"]


def test_reports_nothing_when_the_install_is_current(tmp_path):
    """A snapshot matching the source tree reports no stale modules."""
    from claudia.install_check import stale_modules

    source = tmp_path / "src" / "ibkr_core_mcp"
    snapshot = tmp_path / "build" / "__editable__.x" / "ibkr_core_mcp"
    source.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    for name in ("__init__.py", "claude_tools.py", "local_browser.py"):
        (source / name).write_text("")
        (snapshot / name).write_text("")
    (snapshot / "__init__.py").unlink()
    (snapshot / "__init__.py").symlink_to(source / "__init__.py")

    fake_pkg = types.ModuleType("ibkr_core_mcp")
    fake_pkg.__file__ = str(snapshot / "__init__.py")
    with patch.dict("sys.modules", {"ibkr_core_mcp": fake_pkg}):
        assert stale_modules() == []


def test_a_non_editable_install_can_never_be_stale(tmp_path):
    """A normal `pip install` from git has no snapshot, so there is nothing to drift.
    Returning [] there keeps a production deployment free of false alarms."""
    from claudia.install_check import stale_modules

    real = tmp_path / "site-packages" / "ibkr_core_mcp"
    real.mkdir(parents=True)
    (real / "__init__.py").write_text("")  # a real file, not a symlink

    fake_pkg = types.ModuleType("ibkr_core_mcp")
    fake_pkg.__file__ = str(real / "__init__.py")
    with patch.dict("sys.modules", {"ibkr_core_mcp": fake_pkg}):
        assert stale_modules() == []


def test_the_warning_names_the_modules_and_the_fix(caplog):
    """Every previous occurrence was diagnosed from a bare ModuleNotFoundError with no
    hint that reinstalling was the fix. The log line has to carry both."""
    from claudia.install_check import REINSTALL_COMMAND, warn_if_stale

    with (
        patch("claudia.install_check.stale_modules", return_value=["local_browser.py"]),
        caplog.at_level("ERROR"),
    ):
        returned = warn_if_stale()

    assert returned == ["local_browser.py"]
    assert "local_browser.py" in caplog.text
    assert REINSTALL_COMMAND in caplog.text
    assert "editable_mode=strict" in caplog.text


def test_warn_is_silent_on_a_healthy_install(caplog):
    """A healthy install logs nothing — the warning must stay rare enough to be believed."""
    from claudia.install_check import warn_if_stale

    with patch("claudia.install_check.stale_modules", return_value=[]), caplog.at_level("ERROR"):
        assert warn_if_stale() == []
    assert caplog.text == ""


def test_this_machines_install_is_actually_current():
    """Runs against the REAL install, so the ordinary `pytest` run is what catches drift.

    This is the test that would have fired on 2026-07-30 the moment `scrape_fallback.py`
    became `local_browser.py`, instead of the rename surfacing later as a mystery
    ModuleNotFoundError from inside a tool call.
    """
    from claudia.install_check import REINSTALL_COMMAND, stale_modules

    missing = stale_modules()
    assert missing == [], (
        f"ibkr_core_mcp modules exist in the library but are invisible here: {missing}. "
        f"Any tool importing one will fail at call time. Fix: {REINSTALL_COMMAND}"
    )


def test_panel_app_checks_at_startup():
    """The guard is only worth having if it actually runs. Asserts the wiring, since a
    silent unwiring would restore the exact failure mode this module exists to end."""
    source = Path("claudia/panel_app.py").read_text()
    assert "from claudia.install_check import warn_if_stale" in source
    assert "warn_if_stale()" in source
