"""CLA-SEC-009, second half — a unit test writes nothing into the operator's data, and opens
no database in the core's data directory.

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
run fails at session end if a probe or a new report appears.

The probes keep the *configured* core store out of reach; they cannot see a test that names
the real one — `~/.ibkr_core/store.db` written out, or a `Config` rebuilt after
`IBKR_SQLITE_PATH` was removed, whose default is that absolute path. So since 2026-09-30
`sqlite3.connect` refuses any database under `~/.ibkr_core` for the length of every test,
whatever spells it. No test named that directory when the guard was written (checked the
same day); it is there before one does.

The mechanisms are in `tests/conftest.py`; this file is what fails when they stop working.
"""

from __future__ import annotations

import ast
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests import conftest
from tests.conftest import _PROBE_ENV, _REAL_REPORT_DIR, _real_data_violations
from tests.security.structural import PACKAGE_DIR, SCRIPTS_DIR

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


# ── The core's data directory, whatever names it (2026-09-30) ─────────────────────────────
#
# Every test below first points the guard at a directory under `tmp_path`, so a broken
# guard — which is how each one was proven able to fail — opens a scratch file, never the
# operator's store.


@pytest.fixture
def operator_dir(tmp_path, monkeypatch):
    """A stand-in for `~/.ibkr_core`, installed as the directory the guard protects."""
    fake = (tmp_path / ".ibkr_core").resolve()
    fake.mkdir()
    monkeypatch.setattr(conftest, "_OPERATOR_DATA_DIR", fake)
    return fake


def test_the_guarded_directory_is_where_the_default_store_lives(monkeypatch):
    """Without the probe variable, `Config.from_env()` falls back to the operator's store —
    an absolute path, so no working directory protects it — and the guard recognises it."""
    from ibkr_core_mcp.config import Config

    assert Path.home().resolve() / ".ibkr_core" == conftest._OPERATOR_DATA_DIR
    monkeypatch.delenv("IBKR_SQLITE_PATH")
    default = Config.from_env().sqlite_path
    assert default == Path("~/.ibkr_core/store.db").expanduser()
    assert conftest._operator_database(default) is not None


@pytest.mark.parametrize(
    "spell",
    [
        lambda p: str(p),
        lambda p: p,
        lambda p: str(p).encode(),
        lambda p: f"file:{p}?mode=ro",
        lambda p: f"{p.as_uri()}?mode=ro",
        lambda p: f"{p.as_uri()}?mode=rw#fragment",
        lambda p: str(p) + "-wal",
        lambda p: str(p.parent / "nested" / "other.db"),
    ],
    ids=[
        "str",
        "path",
        "bytes",
        "file-uri",
        "as-uri",
        "uri-with-fragment",
        "wal-sidecar",
        "nested",
    ],
)
def test_every_spelling_of_a_database_there_is_recognised(operator_dir, spell):
    """Each form `sqlite3.connect` accepts for a file in the directory is judged the same way."""
    assert conftest._operator_database(spell(operator_dir / "store.db")) is not None


def test_a_percent_escape_is_decoded_before_the_path_is_judged(operator_dir):
    """SQLite decodes `%HH` in a URI's path, so the guard must judge the decoded path."""
    spaced = operator_dir / "my store.db"
    assert "%20" in spaced.as_uri()
    assert conftest._operator_database(f"{spaced.as_uri()}?mode=ro") == spaced


@pytest.mark.parametrize(
    "database",
    [":memory:", "", "file::memory:?cache=shared", "file:?mode=memory", 42, None],
    ids=["memory", "empty", "shared-memory-uri", "pathless-uri", "not-a-path", "none"],
)
def test_a_database_that_names_no_file_there_is_not_refused(operator_dir, database):
    """In-memory and pathless databases, and values that are no path at all, pass through."""
    assert conftest._operator_database(database) is None


def test_a_tilde_spelling_is_refused_though_sqlite_would_not_expand_it(operator_dir, monkeypatch):
    """SQLite does not expand `~`: it would look for `~/.ibkr_core/store.db` under a directory
    literally named `~` in the working directory. A test that spells the store that way meant
    the operator's, so the judge expands it and refuses it as if it had reached it."""
    monkeypatch.setenv("HOME", str(operator_dir.parent))
    assert conftest._operator_database("~/.ibkr_core/store.db") == operator_dir / "store.db"


def test_a_path_through_a_symlink_is_judged_where_it_lands(operator_dir, tmp_path):
    """The operator's directory may be reached through a link; the judge resolves the path."""
    link = tmp_path / "link-to-the-directory"
    link.symlink_to(operator_dir, target_is_directory=True)
    assert conftest._operator_database(link / "store.db") == operator_dir / "store.db"


@pytest.mark.parametrize(
    "database",
    ["store\x00.db", "file:store%00.db?mode=ro", "store\ud800.db"],
    ids=["nul", "nul-in-uri", "lone-surrogate"],
)
def test_a_path_the_judge_cannot_resolve_is_left_to_sqlite(operator_dir, database):
    """`Path.resolve()` raises `ValueError` for a NUL or an unencodable character. The judge
    must answer None and let `sqlite3.connect` refuse the path in its own words — a guard
    that raised would change what the code under test sees."""
    assert conftest._operator_database(database) is None


def test_a_database_elsewhere_is_not_refused(operator_dir, tmp_path):
    """Only the directory itself is guarded — not a sibling whose name merely starts like it."""
    elsewhere = tmp_path / "elsewhere" / "store.db"
    assert conftest._operator_database(elsewhere) is None
    assert conftest._operator_database(f"{elsewhere.as_uri()}?mode=ro") is None
    assert conftest._operator_database(tmp_path / ".ibkr_core_backup" / "store.db") is None


def test_a_test_cannot_open_a_database_in_the_operators_directory(operator_dir):
    """Read-write, read-only and URI opens are all refused, and nothing is created there."""
    assert getattr(sqlite3.connect, "guards_operator_data", False), "the guard is not installed"
    target = operator_dir / "store.db"
    with pytest.raises(pytest.fail.Exception, match="operator's data"):
        sqlite3.connect(target)
    with pytest.raises(pytest.fail.Exception, match="operator's data"):
        sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
    with pytest.raises(pytest.fail.Exception, match="operator's data"):
        sqlite3.connect(f"file:{target}?mode=rwc", uri=True)
    assert list(operator_dir.iterdir()) == [], "the refusal came after the open"


def test_the_installed_cores_own_store_is_refused_there_too(operator_dir):
    """The route a ClaudIA test would take: the core's `SQLiteStore`, whose `_connect` calls
    `sqlite3.connect` by attribute — the name the guard replaces."""
    from dataclasses import replace

    from ibkr_core_mcp import SQLiteStore
    from ibkr_core_mcp.config import Config

    config = replace(Config.from_env(), sqlite_path=operator_dir / "store.db")
    with pytest.raises(pytest.fail.Exception, match="operator's data"):
        SQLiteStore(config).initialize()
    assert not (operator_dir / "store.db").exists()


def test_the_refusal_cannot_be_swallowed_by_code_that_catches_exception(operator_dir):
    """`flex_sync`'s never-raise functions wrap their opens in `except sqlite3.Error`; a
    refusal they could catch would read as "dataset unreadable" instead of failing the test."""
    assert not issubclass(pytest.fail.Exception, Exception)
    with pytest.raises(pytest.fail.Exception):
        try:
            sqlite3.connect(operator_dir / "store.db")
        except Exception:  # what a never-raise reader does
            pytest.fail("the refusal was caught by `except Exception`")


def test_a_temporary_database_still_opens(tmp_path):
    """The guard is a refusal for one directory, not a block on SQLite."""
    conn = sqlite3.connect(tmp_path / "scratch.db")
    try:
        conn.execute("CREATE TABLE t (x)")
    finally:
        conn.close()
    assert (tmp_path / "scratch.db").exists()


# `sqlite3.dbapi2.connect` and `_sqlite3.connect` are the very function object the guard
# replaces on `sqlite3` (measured 2026-09-30), so reaching either is an unguarded open.
_UNGUARDED_MODULES = frozenset({"sqlite3.dbapi2", "_sqlite3"})


def _unguarded_opens(source: str) -> list[int]:
    """Line numbers of imports that bind SQLite's `connect` somewhere the guard is not.

    `from sqlite3 import …` binds its names at import time, before any fixture runs, and the
    two other modules hold the same function under names the guard does not replace.
    """
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            unguarded = node.module in {"sqlite3", *_UNGUARDED_MODULES}
        elif isinstance(node, ast.Import):
            unguarded = any(alias.name in _UNGUARDED_MODULES for alias in node.names)
        else:
            continue
        if unguarded:
            lines.append(node.lineno)
    return sorted(lines)


def test_nothing_opens_sqlite_around_the_guard():
    """The package, its scripts and the tests reach SQLite only through `sqlite3.connect`.

    The tests are scanned too: they are what the guard is for, and a test module that did
    `from sqlite3 import connect` would hold the original from collection time.
    """
    tests_dir = Path(__file__).resolve().parents[1]
    offenders = {
        str(path.relative_to(PACKAGE_DIR.parent)): lines
        for directory in (PACKAGE_DIR, SCRIPTS_DIR, tests_dir)
        for path in sorted(directory.rglob("*.py"))
        if (lines := _unguarded_opens(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


def test_the_import_probe_sees_every_unguarded_route():
    """The checker above, against each spelling it exists to catch — and one it must not."""
    source = (
        "import sqlite3\n"
        "from sqlite3 import connect\n"
        "from sqlite3.dbapi2 import connect as c\n"
        "import sqlite3.dbapi2\n"
        "import _sqlite3\n"
        "def f():\n"
        "    from _sqlite3 import connect\n"
    )
    assert _unguarded_opens(source) == [2, 3, 4, 5, 7]
