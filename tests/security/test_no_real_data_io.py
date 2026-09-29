"""CLA-SEC-009, second half — a unit test writes nothing into the operator's data.

Found 2026-09-29 while explaining a false "claudia.db on Drive is older than local" warning.
Measured before this file existed: **369 session reports** stamped with a MagicMock document
version sat in the real `data/test-sessions/`, one per pytest run since 2026-09-10 — the
three tests that call `main()` patch `pn.serve` but not the shutdown finaliser, which then
ran the real report writer over every session earlier tests had left in `_open_sessions`,
into a path built relative to the working directory. The same run's corpus test opened the
real `data/claudia.db` read-only, which in WAL mode creates an empty `-wal` sidecar whose
mtime the Drive comparison then read as a local edit.

Both configured database paths are pointed at files that must never exist (the probes), the
reporter is redirected for every test, leftover sessions are cleared between tests, and the
run fails at session end if a probe or a new report appears. The mechanism is in
`tests/conftest.py`; this file is what fails when it stops working.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.conftest import _PROBE_ENV, _REAL_REPORT_DIR, _real_data_violations

_REPO_DEFAULT_REPORT_DIR = Path("data/test-sessions")


def test_the_configured_conversation_db_is_a_probe_that_does_not_exist():
    """`panel_app._DB_PATH` is read from the environment at import; the probe is what it sees."""
    from claudia.panel_app import _DB_PATH

    assert str(_DB_PATH) == os.environ["CLAUDIA_DB_PATH"] == _PROBE_ENV["CLAUDIA_DB_PATH"]
    assert not _DB_PATH.exists(), "a test opened the configured conversation DB"


def test_the_configured_core_store_is_a_probe_that_does_not_exist():
    """The core's default is `~/.ibkr_core/store.db`, absolute — the operator's trade data.

    `Config.from_env()` reads `IBKR_SQLITE_PATH` at call time, so a test that builds a real
    `SQLiteStore` from it must land on the probe and nowhere else.
    """
    from ibkr_core_mcp.config import Config

    path = Config.from_env().sqlite_path
    assert str(path) == _PROBE_ENV["IBKR_SQLITE_PATH"]
    assert not path.exists(), "a test opened the configured core store"


def test_the_reporter_is_redirected_and_no_session_is_left_over():
    """Every test starts with the reporter pointed away from the repo and an empty session dict."""
    from claudia import panel_app, session_reporter

    assert session_reporter.REPORT_DIR != _REPO_DEFAULT_REPORT_DIR
    assert not session_reporter.REPORT_DIR.is_relative_to(Path.cwd()) or "pytest" in str(
        session_reporter.REPORT_DIR
    )
    assert panel_app._open_sessions == {}


def test_a_leftover_session_finalised_at_shutdown_writes_no_report_into_the_real_directory():
    """The defect, replayed: a mock store left registered, then the shutdown finaliser.

    This is the exact path the three `main()` tests took. The report must land under the
    redirected directory and the real one must be untouched.
    """
    from claudia import panel_app, session_reporter

    before = set(os.listdir(_REAL_REPORT_DIR)) if _REAL_REPORT_DIR.exists() else set()
    store = MagicMock()
    store.get_history.return_value = []
    store.get_decisions.return_value = []
    panel_app._open_sessions["left-over-by-an-earlier-test"] = store

    panel_app._finalize_open_sessions_at_shutdown()

    after = set(os.listdir(_REAL_REPORT_DIR)) if _REAL_REPORT_DIR.exists() else set()
    assert after == before, f"a report reached the real directory: {sorted(after - before)}"
    assert list(session_reporter.REPORT_DIR.glob("*.md")), "no report was written at all"
    assert panel_app._open_sessions == {}


@pytest.mark.parametrize(
    ("probe_exists", "sidecar", "new_reports", "expected_count"),
    [
        (False, None, set(), 0),
        (True, None, set(), 1),
        (False, "-wal", set(), 1),
        (False, "-shm", set(), 1),
        (False, None, {"2026-09-29-0913.md"}, 1),
        (True, "-wal", {"x.md"}, 3),
    ],
)
def test_the_session_end_check_names_every_violation(
    tmp_path, probe_exists, sidecar, new_reports, expected_count
):
    """The decision behind `pytest_sessionfinish`, as a pure function over paths and names.

    A probe that exists, any of its SQLite sidecars, and any report name that was not there
    at session start are each one violation; the message names the path.
    """
    probe = tmp_path / "claudia.db"
    if probe_exists:
        probe.write_bytes(b"")
    if sidecar:
        Path(str(probe) + sidecar).write_bytes(b"")

    violations = _real_data_violations([probe], before=set(), after=new_reports)

    assert len(violations) == expected_count
    for v in violations:
        assert str(tmp_path) in v or any(r in v for r in new_reports)


# An ordered pair, deliberately: pytest runs a file's tests in definition order, and the
# clearing fixture is only shown to work by one test leaving a session and the next not
# seeing it. The pair is the discriminating check for the fixture's teardown half.


def test_1_of_2_a_test_that_leaves_a_session_registered():
    """Registers a session and does not finalise it — exactly what an init-driving test does."""
    from claudia import panel_app

    panel_app._open_sessions["left-behind-on-purpose"] = MagicMock()
    assert panel_app._open_sessions


def test_2_of_2_the_next_test_starts_with_no_session():
    """The session the previous test left must not be here to be reported on."""
    from claudia import panel_app

    assert panel_app._open_sessions == {}
