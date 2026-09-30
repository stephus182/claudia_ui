"""Shared test helpers, and the session-wide live-I/O block (CLA-SEC-009).

Two jobs. The Panel helpers were moved here from tests/test_panel_order_flow.py
(_get_click_callback) and tests/test_panel_app.py (_find_buttons) during the Task 5.6b
quality review (2026-07-24) so both modules drive live pn.widgets.Button objects through
one verified idiom instead of drifting copies. Plain functions imported explicitly
(from tests.conftest import ...) — not fixtures — since they take the object under
inspection as an argument.

The second job, added 2026-09-14 (audit 2026-09-13, finding A-5): no unit test opens a
socket, resolves a name, or sees a real secret. What the audit measured beforehand —
four tests resolving `example.com`, four connecting to the IBKR gateway on 127.0.0.1:5055
(one of which is the session keepalive), and the operator's real `ANTHROPIC_API_KEY` in
the pytest process — is asserted against by tests/security/test_no_live_io.py.
"""

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import panel as pn
import pytest

# Every entry here resolves a real name through `agent._validate_public_url`'s
# `gethostbyname`, which is the SSRF guard's second layer. Blocking DNS does not make these
# tests fail loudly — `SocketBlockedError` is not `socket.gaierror`, so it escapes the
# guard's narrow except and returns "Invalid URL: ...", and an assertion on "Blocked" would
# then pass for entirely the wrong reason. The matching tests for a *private* address are
# deliberately NOT listed: localhost and a literal 127.x need no DNS to be recognised, so
# they keep their meaning under the block.
_REAL_DNS_EXEMPT_TESTS = {
    "test_fetch_web_page_blocks_redirect_to_private_address",
    "test_fetch_web_page_follows_public_redirect",
    "test_fetch_web_page_blocks_redirect_loop",
    "test_fetch_web_page_ssrf_guard_allows_public",
}

# Tests that bind a real loopback socket because the thing under test IS socket behaviour.
# Kept apart from the DNS list on purpose: this one reaches no name and no remote host, it
# asks the kernel for a free port on 127.0.0.1 and hands it back. Mocking it would leave
# `_port_is_free` asserted against a mock of the only thing it does.
_REAL_LOOPBACK_BIND_TESTS = {
    "test_port_is_free_detects_a_bound_port",
}

# Scrubbed by prefix, never by a list of names: in ibkr_core_mcp the first hand-kept list
# had 14 names, the test that checked it had 7, and neither carried the one that mattered.
# `CLAUDIA_` is deliberately absent — it carries configuration, and `CLAUDIA_LIVE_SCHEMA_CHECK`
# is how the operator opts into the live_api tests.
_SECRET_ENV_PREFIXES = (
    "ANTHROPIC_",
    "IBKR_",
    "GDRIVE_",
    "GOOGLE_",
    "FIRECRAWL_",
    "CRAWL4AI_",
    "TRADINGVIEW_",
)

# ── No unit test writes into the operator's data (CLA-SEC-009, second half) ─────────────
#
# Found 2026-09-29: 369 session reports stamped with a MagicMock document version in the real
# `data/test-sessions/`, one per pytest run since 2026-09-10 (the `main()` tests fall into
# `pn.serve`'s `finally`, which finalises every session earlier tests left in
# `_open_sessions`, with the report path built relative to the working directory), and a
# false "Drive is older than local" warning caused by the corpus test's read-only open of the
# real `data/claudia.db` creating an empty `-wal` sidecar. The rule is structural: both
# configured database paths point at PROBE files that must never exist, the reporter is
# redirected for every test, leftover sessions are cleared, and the run fails at session end
# if a probe or a new report appears. `tests/security/test_no_real_data_io.py` asserts each.
# One legitimate trip (seen 2026-09-29 12:44 in the pre-push hook): a RUNNING ClaudIA
# finalising a browser session during the suite writes a genuine report into the real
# directory, and the check cannot tell that writer from a test. The failure names the
# file; a report with no MagicMock text is the app's — re-run the suite. Panel finalises
# a closed browser session only after its websocket timeout, so the report can land a
# minute after the tab closed (twice on 2026-09-29): wait for it before a push.
_REAL_REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "test-sessions"
_PROBE_DIR = Path(tempfile.mkdtemp(prefix="claudia-pytest-probe-"))
_PROBE_ENV: dict[str, str] = {
    # `panel_app._DB_PATH` reads this at import; the default is the real `data/claudia.db`.
    "CLAUDIA_DB_PATH": str(_PROBE_DIR / "claudia.db"),
    # The core's `Config.from_env()` reads this at call time; the default is the operator's
    # trade store at `~/.ibkr_core/store.db` — absolute, so no working directory protects it.
    "IBKR_SQLITE_PATH": str(_PROBE_DIR / "store.db"),
}
_SQLITE_SIDECARS = ("", "-wal", "-shm", "-journal")
_reports_at_start: set[str] = set()


def _real_report_names() -> set[str]:
    """The files in the real report directory right now (empty set when it does not exist)."""
    return set(os.listdir(_REAL_REPORT_DIR)) if _REAL_REPORT_DIR.exists() else set()


def _real_data_violations(probes: Iterable[Path], before: set[str], after: set[str]) -> list[str]:
    """Every way the suite touched real data, one line each — empty when it touched none.

    A probe database that exists, or any SQLite sidecar of it (a read-only open in WAL mode
    creates `-wal` and `-shm` without writing a row), means a test opened a configured store.
    A report name absent at session start means a test wrote into the real report directory.
    """
    violations: list[str] = []
    for probe in probes:
        for suffix in _SQLITE_SIDECARS:
            path = Path(str(probe) + suffix)
            if path.exists():
                violations.append(f"a test opened a configured database: {path}")
    for name in sorted(after - before):
        violations.append(f"a test wrote into the real report directory: {_REAL_REPORT_DIR / name}")
    return violations


# ── …and opens no database in the core's data directory (CLA-SEC-009, 2026-09-30) ─────────
#
# The probes above keep the CONFIGURED core store out of reach. They cannot see a test that
# names the real one: `~/.ibkr_core/store.db` written out, or a `Config` rebuilt after
# `IBKR_SQLITE_PATH` was removed — whose default is that absolute path, so no working
# directory and no tmp_path convention protects it. No test did either when this was written
# (checked 2026-09-30). So `sqlite3.connect` refuses any database under that directory for
# the length of every test, read-only opens included: a `mode=ro` open of a WAL database can
# still create `-shm`/`-wal` beside it (https://www.sqlite.org/wal.html § 5, "Read-Only
# Databases"). `tests/security/test_no_real_data_io.py` is what fails when this stops working.
_OPERATOR_DATA_DIR = Path.home().resolve() / ".ibkr_core"


def _operator_database(database: object) -> Path | None:
    """The file under the operator's data directory that `database` names, or None.

    Accepts every spelling `sqlite3.connect` does: `str`, `bytes`, a path object, and the
    `file:` URI form, whose path is percent-decoded and cut at `?`/`#` as SQLite reads it
    (https://www.sqlite.org/uri.html § 3). `~` is expanded, which SQLite itself would not do:
    a test that spells the store that way meant the operator's, and is refused as if it had
    reached it. Symlinks are resolved, so `/var/...` and `/private/var/...` are one place.
    Anything that cannot name a file there — `:memory:`, an empty name, a path elsewhere, a
    value that is no path — is None.
    """
    if isinstance(database, bytes):
        database = database.decode("utf-8", "surrogateescape")
    if not isinstance(database, str | os.PathLike):
        return None
    text = os.fspath(database)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "surrogateescape")
    if text.startswith("file:"):
        text = unquote(urlsplit(text).path)
    if not text or text == ":memory:":
        return None
    try:
        resolved = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):  # ValueError: a NUL or unencodable character
        return None  # not judgeable here; `connect` refuses such a path in its own words
    return resolved if resolved.is_relative_to(_OPERATOR_DATA_DIR) else None


def _neutralise_dotenv() -> None:
    """Make `load_dotenv` a no-op at every import site, before any claudia module loads.

    This cannot be a fixture. `claudia/panel_app.py` calls `load_dotenv(override=False)` at
    module scope (line 86), and collection imports that module long before the first fixture
    runs — measured 2026-09-13: a probe inside a running test found a real `sk-ant-` key of
    length 108 and `IBKR_FLEX_TOKEN` in `os.environ`. `pytest_configure` runs before
    collection, so patching the `dotenv` module here is what the module-level call picks up.
    """
    noop = lambda *args, **kwargs: False  # noqa: E731
    import dotenv
    import dotenv.main

    dotenv.load_dotenv = noop
    dotenv.main.load_dotenv = noop


def live_api_opted_in() -> bool:
    """True when the operator asked for the live-API checks on this run.

    `CLAUDIA_LIVE_SCHEMA_CHECK=1` is the documented opt-in (CLAUDE.md § Testing). Those four
    tests exist because local validation cannot prove what the tools endpoint accepts — three
    schema defects once passed a docs read and a green suite and would each have 400'd every
    request — so they need the real key and the network, and a block that quietly retires
    them is worse than no block. The first version of this file scrubbed `ANTHROPIC_*` and
    no-op'd `load_dotenv` unconditionally, which made `pytest -m live_api` unpassable even
    with the key exported, and failed pointing at `.env` (review 2026-09-14).
    """
    return os.environ.get("CLAUDIA_LIVE_SCHEMA_CHECK") == "1"


def pytest_configure(config):
    """Arm the block for the whole session, import and collection included.

    From the first test onward pytest-socket's own per-test hooks take over (see
    `pytest_collection_modifyitems`); its teardown re-enables sockets after every test,
    which is why this call alone is not the per-test guarantee.

    On an opted-in live-API run the secret scrub and the dotenv no-op are skipped: the
    sockets stay blocked for every other test through the per-test markers, so the
    protection that matters is unchanged, while the run the operator asked for can happen.
    """
    from pytest_socket import disable_socket

    if not live_api_opted_in():
        _neutralise_dotenv()
        for name in [k for k in os.environ if k.startswith(_SECRET_ENV_PREFIXES)]:
            del os.environ[name]
    # After the scrub, never before: `IBKR_SQLITE_PATH` carries the scrubbed prefix. Set here,
    # before collection, because `panel_app._DB_PATH` is read at import.
    os.environ.update(_PROBE_ENV)
    _reports_at_start.clear()
    _reports_at_start.update(_real_report_names())
    disable_socket(allow_unix_socket=True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if any test touched real data — a probe created or a report leaked.

    A hook rather than a test because no test can run last by construction; the check has
    to see the whole session. `session.exitstatus` is what `pytest.main` returns after this
    hook, so setting it is what turns the run red.
    """
    violations = _real_data_violations(
        (Path(v) for v in _PROBE_ENV.values()), _reports_at_start, _real_report_names()
    )
    if not violations:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = [
        "",
        "REAL DATA TOUCHED BY THE TEST SUITE (tests/conftest.py, CLA-SEC-009):",
        *violations,
    ]
    for line in lines:
        if reporter is not None:
            reporter.write_line(line, red=True, bold=True)
        else:  # pragma: no cover - no terminal plugin (e.g. --collect-only under -p no:terminal)
            print(line)
    session.exitstatus = 1


def pytest_unconfigure(config):
    """Re-enable sockets, and remove the probe directory.

    Only sockets: the dotenv no-op and the scrubbed variables are left as they are, because
    the process is ending and restoring them would put the operator's key back into an
    interpreter that no longer needs it. Said explicitly because an earlier version of this
    docstring claimed to hand the process back as it was found, which was not true
    (review 2026-09-14).
    """
    from pytest_socket import enable_socket

    enable_socket()
    # The probes were reported by `pytest_sessionfinish` if they existed; nothing to keep.
    shutil.rmtree(_PROBE_DIR, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    """Give every test one of pytest-socket's own markers.

    The plugin applies them in its `pytest_runtest_setup`, which runs before any fixture,
    module-scoped ones included — the ordering ibkr_core_mcp's review of 2026-09-13 found
    a session block plus a function-scoped fixture could not give. `live_api` and
    `integration` tests are opt-in and genuinely need the network.
    """
    for item in items:
        # `originalname` rather than `name`: a parametrised test is `test_x[case]`, which
        # would never match an exemption written as `test_x` — the exemption would look
        # present and do nothing (review 2026-09-14). `originalname` is unset for a plain
        # test, so fall back to `name`.
        base = getattr(item, "originalname", None) or item.name
        needs_network = (
            item.get_closest_marker("integration")
            or item.get_closest_marker("live_api")
            or base in _REAL_DNS_EXEMPT_TESTS
            or base in _REAL_LOOPBACK_BIND_TESTS
        )
        item.add_marker(pytest.mark.enable_socket if needs_network else pytest.mark.disable_socket)


@pytest.fixture(autouse=True)
def isolate_real_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test: reports go under its tmp_path, and no session is inherited or bequeathed.

    `panel_app._open_sessions` is process-wide; a session a test registers and never
    finalises is exactly what the `main()` tests' shutdown finaliser then reports on. Cleared
    on both sides, not restored — restoring would re-install a leaked entry.
    """
    from claudia import panel_app, session_reporter

    panel_app._open_sessions.clear()
    monkeypatch.setattr(session_reporter, "REPORT_DIR", tmp_path / "session-reports")
    yield
    panel_app._open_sessions.clear()


@pytest.fixture(autouse=True)
def refuse_operator_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test: `sqlite3.connect` refuses a database in the operator's data directory.

    The refusal is `pytest.fail`, whose exception derives from `BaseException`: code under
    test that catches an `Exception` subclass around an open — `flex_sync`'s never-raise
    readers catch `_UNOPENABLE` — cannot swallow it and turn the touch into a quiet
    "unreadable". Nothing is opened first: the path is judged before the real `connect` is
    called. No marker is exempt: the probes above apply to every test as well, and no test
    here carries `integration` (CLAUDE.md § Testing).
    """
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):  # forwards whatever `connect` returns
        """`sqlite3.connect`, after refusing any database under the operator's directory."""
        target = _operator_database(database)
        if target is not None:
            pytest.fail(
                f"a unit test tried to open the operator's data ({target.name} under "
                "~/.ibkr_core) — build the database under tmp_path",
                pytrace=True,
            )
        return real_connect(database, *args, **kwargs)

    guarded_connect.guards_operator_data = True  # type: ignore[attr-defined]
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield


def _find_buttons(chat):
    """All pn.widgets.Button objects across chat messages (Phase 3 pattern:
    buttons live in a pn.Column/Row inside a message)."""
    found = []
    for m in chat.objects:
        obj = getattr(m, "object", None)
        if obj is None:
            continue
        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, pn.widgets.Button):
                found.append(node)
            stack.extend(getattr(node, "objects", []))
    return found


def _get_click_callback(button):
    """Extract the real on_click callback from a live pn.widgets.Button, for direct
    invocation in a unit test (no browser, no running Panel server).

    Verified live, 2026-07-22, against the installed panel==1.9.3: Button.on_click(cb)
    is implemented as `self.param.watch(cb, 'clicks', onlychanged=False)` (confirmed via
    `inspect.getsource(pn.widgets.Button.on_click)`) — there is no `_on_click` attribute
    on the button itself. The registered callback lives in
    `button.param.watchers['clicks']['value']`, a list of param Watcher namedtuples;
    Panel's own internal sync watchers (name/label/value mirroring etc.) are always
    registered with `onlychanged=True`, while on_click's own watcher is always
    `onlychanged=False` — confirmed by direct inspection of that list — so filtering on
    that flag reliably isolates the one watcher the production render/send functions
    registered, regardless of how many internal watchers Panel itself adds. Calling
    `.fn` directly and awaiting it (async callbacks are supported natively, confirmed via
    `param.parameterized`'s `iscoroutinefunction(watcher.fn)` branch) exercises the exact
    function a real click would invoke, without needing Panel's async_executor/event-loop
    plumbing that a bare pytest run doesn't have.
    """
    watchers = button.param.watchers["clicks"]["value"]
    matches = [w.fn for w in watchers if not w.onlychanged]
    assert len(matches) == 1, f"expected exactly 1 on_click watcher, found {len(matches)}"
    return matches[0]
