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


# ── The Docker build context: the fourth time this root cause bit ───────────


def test_the_gateway_build_context_is_readable_by_docker():
    """`GatewayManager.build_image()` must point Docker at a context it can read.

    Found 2026-08-06 by running a real cold start, after the image was deleted to force
    the rebuild that the in-container tickler removal needed. The build failed instantly:

        ERROR: failed to read dockerfile: open Dockerfile: no such file or directory

    `_DOCKER_DIR` was `Path(__file__).parent`. Imported from claudia_ui — which installs
    ibkr_core_mcp with `editable_mode=strict`, as CLAUDE.md requires for mypy — `__file__`
    is inside `build/__editable__…/ibkr_core_mcp/gateway/`, a farm in which every entry,
    `Dockerfile` included, is a symlink to an absolute path outside the directory.
    **Docker does not follow a symlink that leaves the build context.**

    ## Why this test lives in claudia_ui and not in ibkr_core_mcp

    It was written there first and **could not fail**: pytest run from that repo imports
    the package from its own source tree, so `__file__` is already the real file and the
    broken expression produced the right answer. The bug is only reachable through the
    editable install, which is to say only from here. A test that cannot fail is worse
    than no test — it reports safety it never checked.

    ## Why `is_symlink`, not `exists`

    A symlink to a real file exists perfectly well from Python's point of view, so an
    existence check passes against the broken version too. Docker needs a **real file
    inside the context**, and that is the property asserted.
    """
    from ibkr_core_mcp.gateway.manager import _DOCKER_DIR

    assert "__editable__" not in str(_DOCKER_DIR), (
        f"The gateway Docker build context is the editable-install symlink farm "
        f"({_DOCKER_DIR}). `docker build` cannot read a Dockerfile from there. "
        "_DOCKER_DIR must resolve through symlinks."
    )
    assert _DOCKER_DIR.is_dir(), f"{_DOCKER_DIR} is not a directory"

    for name in ("Dockerfile", "conf.yaml", "run_gateway.sh", "healthcheck.sh"):
        entry = _DOCKER_DIR / name
        assert entry.is_file(), f"{name} is missing from the build context {_DOCKER_DIR}"
        assert not entry.is_symlink(), (
            f"{name} is a SYMLINK inside the Docker build context ({_DOCKER_DIR}); the "
            "build will fail with 'failed to read dockerfile'."
        )


# ── Which install is it: the checkout, or the PyPI copy? ────────────────────────


def _fake_distribution(direct_url: str | None):
    """A `Distribution` double whose only job is to answer `read_text("direct_url.json")`."""

    class _Dist:
        """Stands in for `importlib.metadata.Distribution`, metadata reads only."""

        def read_text(self, name: str) -> str | None:
            """Return the canned `direct_url.json`, and refuse any other metadata file.

            The assertion is the point: if `core_install_origin` ever starts reading
            something else, this double must not answer it silently.
            """
            assert name == "direct_url.json", f"unexpected metadata read: {name}"
            return direct_url

    return _Dist()


def test_an_editable_install_is_recognised_as_the_checkout():
    """PEP 610: an editable install records `dir_info.editable` true in direct_url.json.

    This is the shape the forward-compatibility CI lane must see. It is what the developer
    override produces, measured 2026-09-19 against a real install.
    """
    from claudia.install_check import core_install_origin

    payload = '{"dir_info": {"editable": true}, "url": "file:///Users/x/ibkr_core_mcp"}'
    with patch("claudia.install_check.distribution", return_value=_fake_distribution(payload)):
        assert core_install_origin() == "editable"


def test_a_plain_index_install_is_recognised_as_the_release():
    """No direct_url.json at all is PEP 610's way of saying "this came from an index".

    Measured on a real fresh venv 2026-09-19: `pip install -e ".[dev]"` pulling
    ibkr-core-mcp from PyPI leaves no direct_url.json.
    """
    from claudia.install_check import core_install_origin

    with patch("claudia.install_check.distribution", return_value=_fake_distribution(None)):
        assert core_install_origin() == "index"


def test_a_non_editable_directory_install_is_not_mistaken_for_a_checkout():
    """`pip install ../ibkr_core_mcp` (no -e) also writes direct_url.json, without `editable`.

    It is a snapshot of the checkout, not the checkout: edits to the source do not reach it.
    Calling that "editable" would make the forward-compatibility lane pass while testing a
    frozen copy — the same false-assurance shape the assertion exists to prevent.
    """
    from claudia.install_check import core_install_origin

    payload = '{"dir_info": {}, "url": "file:///Users/x/ibkr_core_mcp"}'
    with patch("claudia.install_check.distribution", return_value=_fake_distribution(payload)):
        assert core_install_origin() == "directory"


def test_a_missing_distribution_is_unknown_rather_than_a_claim():
    """An absent core is not evidence of anything; it must not read as "index"."""
    from importlib.metadata import PackageNotFoundError

    from claudia.install_check import core_install_origin

    with patch("claudia.install_check.distribution", side_effect=PackageNotFoundError()):
        assert core_install_origin() == "unknown"


def test_require_editable_fails_when_the_override_did_not_take():
    """THE POINT OF THE WHOLE CHECK.

    The forward-compatibility lane installs the core editable over the PyPI copy. If that
    install silently fails, every seam assertion in that lane runs against the *pinned
    release* and passes for the wrong reason — and because the job carries
    `continue-on-error`, no tick anywhere turns red. Until 2026-09-19 the lane merely
    *printed* the import path, which is a claim nobody reads, not a check.

    Version strings cannot close this gap: core `main` declared 2.0.1 on 2026-09-19, the
    same string as the published release, so where the distribution came from is the only
    thing that distinguishes them.
    """
    import pytest

    from claudia.install_check import main

    with (
        patch("claudia.install_check.core_install_origin", return_value="index"),
        pytest.raises(SystemExit) as caught,
    ):
        main(["--require-editable"])

    assert caught.value.code not in (0, None), "a failed override must fail the step"
    assert "index" in str(caught.value.code), "the message must name what was found instead"


def test_require_editable_passes_on_the_override():
    """The same command is a no-op when the lane is set up correctly."""
    from claudia.install_check import main

    with patch("claudia.install_check.core_install_origin", return_value="editable"):
        assert main(["--require-editable"]) == 0


def test_reporting_without_the_flag_never_fails():
    """`python -m claudia.install_check` is a diagnostic; it must not refuse anything.

    It did nothing at all until 2026-09-19 — the module had no entry point, so the command
    exited 0 in silence, which reads exactly like a clean report.
    """
    from claudia.install_check import main

    with (
        patch("claudia.install_check.core_install_origin", return_value="index"),
        patch("claudia.install_check.stale_modules", return_value=[]),
    ):
        assert main([]) == 0
