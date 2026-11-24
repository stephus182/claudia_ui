"""Shared test helpers, and the session-wide blocks between a unit test and the real world
(CLA-SEC-009): no live system, no real secret, and none of the operator's data.

The Panel helpers were moved here from tests/test_panel_order_flow.py
(_get_click_callback) and tests/test_panel_app.py (_find_buttons) during the Task 5.6b
quality review (2026-07-24) so both modules drive live pn.widgets.Button objects through
one verified idiom instead of drifting copies. Plain functions imported explicitly
(from tests.conftest import ...) — not fixtures — since they take the object under
inspection as an argument.

The live-I/O block, added 2026-09-14 (audit 2026-09-13, finding A-5): no unit test opens a
socket, resolves a name, or sees a real secret. What the audit measured beforehand —
four tests resolving `example.com`, four connecting to the IBKR gateway on 127.0.0.1:5055
(one of which is the session keepalive), and the operator's real `ANTHROPIC_API_KEY` in
the pytest process — is asserted against by tests/security/test_no_live_io.py.

The real-data blocks, 2026-09-29 and 2026-09-30 (gaps #81, #85, #89): the configured stores
are probes, the session reporter is redirected, and an audit hook refuses anything under
`~/.ibkr_core` — each in its section below, asserted by tests/security/test_no_real_data_io.py.
The desktop block, 2026-10-16 (gap #100): the same hook refuses, by command name, a child
process that would reach the desktop or a daemon — tests/security/test_no_desktop_reached.py.
"""

import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import dotenv.main as _dotenv_main
import panel as pn
import pytest

_REAL_LOAD_DOTENV = _dotenv_main.load_dotenv
"""python-dotenv's own `load_dotenv`, before `_keep_the_real_env_file_out` replaces it: the
test of ClaudIA's replacement (`test_a_later_load_dotenv_by_anyone_exports_no_secret`) puts
it back for its own duration, so its named break — the replacement removed — exports a
secret and goes red, instead of being masked by the no-op."""

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


def _real_data_violations(
    probes: Iterable[Path],
    before: set[str],
    after: set[str],
    refusals: Iterable[str] = (),
    desktop: Iterable[str] = (),
) -> list[str]:
    """Every way the suite touched real data, one line each — empty when it touched none.

    A probe database that exists, or any SQLite sidecar of it (a read-only open in WAL mode
    creates `-wal` and `-shm` without writing a row), means a test opened a configured store.
    A report name absent at session start means a test wrote into the real report directory.
    A refusal of the operator's data directory is listed as well: the refusal stopped the
    operation, but one raised in a thread or caught by `except BaseException` fails no test.
    """
    violations: list[str] = []
    for probe in probes:
        for suffix in _SQLITE_SIDECARS:
            path = Path(str(probe) + suffix)
            if path.exists():
                violations.append(f"a test opened a configured database: {path}")
    for name in sorted(after - before):
        violations.append(f"a test wrote into the real report directory: {_REAL_REPORT_DIR / name}")
    for refusal in refusals:
        violations.append(f"a test reached for the operator's data directory: {refusal}")
    for refusal in desktop:
        violations.append(f"a test reached for the desktop: {refusal}")
    return violations


# ── …and touches nothing in the core's data directory (CLA-SEC-009; gaps #85, #89) ────────
#
# The probes above keep the CONFIGURED stores out of reach. They cannot see a test that names
# the real directory, `~/.ibkr_core`: the trade store written out; a `Config` rebuilt after the
# scrub, whose defaults — the store, the Drive token, the Drive credentials — all lie there; or
# the core's `SQLiteStore`, which creates and restricts that directory before it connects. So
# an audit hook (https://docs.python.org/3.11/library/sys.html#sys.addaudithook), armed in
# `pytest_configure` before anything is collected, refuses any SQLite open of a database there
# and any file opened, created, changed or removed there, however the path names it —
# read-only opens included, since a `mode=ro` open of a WAL database can still create `-shm`
# and `-wal` beside it (https://www.sqlite.org/wal.html § 5, "Read-Only Databases"). SQLite's
# own event fires inside the connection, so every route to it is covered: `sqlite3.connect`, a
# reference taken earlier, `sqlite3.Connection`, `sqlite3.dbapi2`. The refusal is
# `pytest.fail`, a `BaseException`, raised before the operation happens.
#
# It replaced (2026-09-30, gap #89) a `sqlite3.connect` swapped by an autouse fixture, which a
# review measured: not armed at import or in a module-scoped fixture; `Connection()` and
# `sqlite3.dbapi2.connect` went around it; it read URIs with `urllib`, so a `%00` tail or a raw
# newline reached the store; another letter case (APFS ignores case) and the
# `/System/Volumes/Data` firmlink passed its comparison by name; and nothing covered files.
#
# What it cannot see, stated rather than implied: SQL-level `ATTACH` and `VACUUM INTO` (SQLite
# opens those files itself, with no Python event), a path relative to a `dir_fd`, and anything
# that disables it on purpose — audit hooks are "not suitable for implementing a sandbox"
# (Python's own documentation). It is there to catch accidents.
_OPERATOR_DATA_DIR = (Path.home() / ".ibkr_core").resolve()
_REAL_OPERATOR_DATA_DIR = _OPERATOR_DATA_DIR
# The operator's `.env` is the other real-data file a test can reach: python-dotenv's default
# search walks up from any test file to the repository root and finds it. Seen 2026-10-16 —
# a mutation of the loader's replacement let a bare `load_dotenv()` in a test export the real
# file into the pytest process, and the assertion's introspection printed two real values.
# The same hook refuses any file event naming it; None on an opted-in `live_api` run, which
# reads the real file by design; a test of the guard points it at a stand-in.
_REAL_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_GUARDED_ENV_FILE: Path | None = _REAL_ENV_FILE
_OPERATOR_GUARD = {"armed": False}  # a hook cannot be removed, only disarmed
_operator_refusals: list[str] = []  # of the real directory; read by `pytest_sessionfinish`

# Each audited file event, with the positions of its arguments that name a place opened,
# created, changed or removed. Argument order: https://docs.python.org/3.11/library/audit_events.html
# (checked 2026-09-30). `open` is `open()`, `io.open`, `os.open` and `io.open_code` alike. A
# symlink's own target is not a touch — opening through it is, and is judged where it lands.
_FILE_EVENTS: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.link": (0, 1),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "tempfile.mkdtemp": (0,),
    "tempfile.mkstemp": (0,),
}
# Where each event carries a `dir_fd` (None when not given). A relative path with one set is
# relative to that descriptor, not to the working directory, so it names no whole path to
# judge and is not judged (§ 9 of the security architecture): pytest's own cleanup of old
# temp directories unlinks by bare name this way, and a bare `.env` resolved against the
# repository root read as the operator's file (2026-10-16). Positions probed on this Python.
_DIR_FD_POSITIONS: dict[str, tuple[int, ...]] = {
    "os.chmod": (2,),
    "os.chown": (3,),
    "os.link": (2, 3),
    "os.mkdir": (2,),
    "os.remove": (1,),
    "os.rename": (2, 3),
    "os.rmdir": (1,),
    "os.symlink": (2,),
    "os.utime": (3,),
}
_HEX_OCTET = re.compile(rb"[0-9A-Fa-f]{2}")


def _sqlite_uri_path(uri: str) -> str | None:
    """The file SQLite opens for a `file:` URI — its own parse, not `urllib`'s — or None.

    `sqlite3ParseUri` in SQLite's `src/main.c`, as built here (no `SQLITE_ALLOW_URI_AUTHORITY`):
    an authority after `//` runs to the next `/` and must be empty or `localhost` — anything
    else is an error and nothing opens (None); the path ends at the first `?` or `#`; `%HH` is
    decoded; and at a `%00` the rest of the path is ignored. `urllib` differs exactly there: it
    decodes `%00` into the path, deletes raw tabs and newlines, and raises on a `[` in the
    authority — three ways the store was reached, or the guard itself raised, before this.
    """
    rest = uri[len("file:") :]
    if rest.startswith("//"):
        authority, slash, path = rest[2:].partition("/")
        if authority not in ("", "localhost"):
            return None
        rest = slash + path
    for end in ("?", "#"):
        rest = rest.partition(end)[0]
    raw = rest.encode("utf-8", "surrogateescape")
    decoded = bytearray()
    at = 0
    while at < len(raw):
        escape = raw[at + 1 : at + 3]
        if raw[at] == ord("%") and _HEX_OCTET.fullmatch(escape):
            if escape == b"00":
                break
            decoded.append(int(escape, 16))
            at += 3
        else:
            decoded.append(raw[at])
            at += 1
    return os.fsdecode(bytes(decoded))


def _inside_operator_directory(path: Path) -> bool:
    """True when `path` is the operator's directory or lies under it — by name, or by identity.

    By name after `resolve()`, which follows symlinks. By identity for the spellings
    `resolve()` leaves apart — another letter case on a case-insensitive volume, a firmlink
    such as `/System/Volumes/Data/Users/...`: `os.path.samestat` of the directory against
    `path` and each of its existing ancestors. Where the directory does not exist (CI) there
    is nothing to reach, and the comparison by name alone stands.
    """
    if path.is_relative_to(_OPERATOR_DATA_DIR):
        return True
    try:
        operator = _OPERATOR_DATA_DIR.stat()
    except OSError:
        return False
    for place in (path, *path.parents):
        try:
            if os.path.samestat(place.stat(), operator):
                return True
        except OSError:
            continue
    return False


def _operator_path(value: object, *, database: bool, fd_relative: bool = False) -> Path | None:
    """The place under the operator's data directory that `value` names, or None.

    `value` is what the audited call was given: `str`, `bytes` or a path object — and, for a
    database, the `file:` URI form, read the way SQLite reads it. `~` is expanded, which SQLite
    would not do: a test that spells the store that way meant the operator's, and is refused
    as if it had reached it. Anything that names no place there — `:memory:`, an empty name, a
    file descriptor, a path elsewhere — is None, and so is a name no file system can hold (a
    NUL): the call refuses that one itself, in its own words.
    """
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str | os.PathLike):
        return None
    text = os.fspath(value)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if database and text.startswith("file:"):
        uri_path = _sqlite_uri_path(text)
        if uri_path is None:
            return None
        text = uri_path
    if not text or (database and text == ":memory:"):
        return None
    if fd_relative and not os.path.isabs(os.path.expanduser(text)):
        return (
            None  # relative to a descriptor, not to the working directory: no whole path to judge
        )
    try:
        resolved = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):  # a NUL, an unencodable name, an unknown ~user
        return None
    if _GUARDED_ENV_FILE is not None and resolved == _GUARDED_ENV_FILE:
        return resolved
    return resolved if _inside_operator_directory(resolved) else None


def _operator_data_hook(event: str, args: tuple[object, ...]) -> None:
    """The audit hook: refuse a SQLite open or a file operation under the operator's directory."""
    if not _OPERATOR_GUARD["armed"]:
        return
    if event == "sqlite3.connect":
        targets = [_operator_path(args[0], database=True)]
    elif (positions := _FILE_EVENTS.get(event)) is not None:
        fd_relative = any(
            args[i] is not None for i in _DIR_FD_POSITIONS.get(event, ()) if i < len(args)
        )
        targets = [
            _operator_path(args[p], database=False, fd_relative=fd_relative)
            for p in positions
            if p < len(args)
        ]
    else:
        return
    for target in targets:
        if target is None:
            continue
        if target == _REAL_ENV_FILE or (
            _OPERATOR_DATA_DIR == _REAL_OPERATOR_DATA_DIR and target != _GUARDED_ENV_FILE
        ):
            _operator_refusals.append(f"{event} {target}")
        where = (
            "the repository's .env"
            if target == _GUARDED_ENV_FILE
            else f"{target.name} under ~/.ibkr_core"
        )
        pytest.fail(
            f"a unit test tried to reach the operator's data ({event}: {where}) — build it under tmp_path",
            pytrace=True,
        )


# ── …and starts nothing that reaches the operator's desktop or daemons (gap #100) ────────
#
# `tradingview.py` quits TradingView (`osascript`, `pkill`), launches it (`open -a`), and
# `gateway_launch.py` runs the docker CLI; the core's Gate 2 is a child running
# `_order_dialog.py` or `osascript`, its login window is Chrome. The socket guard stops none
# of these, and a test that patched one call and not another — or a mutant that moved the
# call — would act on the operator's machine (the core's register F32: a real Gate 2 dialog
# opened on the operator's screen from a mutation run, 2026-10-01). The same audit hook
# refuses a child process by the name of what would run — its last path component, read
# whole ("Google Chrome" holds a space) — or by an `--app=` argument, whichever call asked.
# Inventoried 2026-10-16 with the hook reporting: the suite's 45 real starts are `git`,
# `python` and a shimmed `bash`, so nothing is exempt. Its own name lists, not the code's.
# What it cannot see: a command named inside one shell string is read word by word, and
# anything that disarms it on purpose. It catches accidents.
_DESKTOP_GUARD = {"armed": False}
_desktop_refusals: list[str] = []  # read by `pytest_sessionfinish`
_DESKTOP_COMMANDS = ("osascript", "pkill", "open", "docker", "_order_dialog.py")
_BROWSER_LAUNCHERS = (
    "Google Chrome",
    "chrome.exe",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
# Process creation, with the positions of the arguments that say what would run. Argument
# order: https://docs.python.org/3.11/library/audit_events.html (checked 2026-10-16).
_PROCESS_EVENTS: dict[str, tuple[int, ...]] = {
    "subprocess.Popen": (0, 1),  # executable, args
    "os.system": (0,),  # command
    "os.posix_spawn": (0, 1),  # path, argv
    "os.exec": (0, 1),  # path, args
    "os.spawn": (1, 2),  # mode, path, args
}


def _names_a_desktop_command(value: object) -> str | None:
    """What `value` would run that reaches the desktop, or None: a path, a command line, a
    list of either, read by each word's last path component, or an `--app=` argument."""
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str):
        whole = value.replace("\\", "/").rsplit("/", 1)[-1]
        if whole in _BROWSER_LAUNCHERS:
            return whole
        if value.startswith("--app="):
            return "--app="
        for word in value.split():
            name = word.strip("'\"").rsplit("/", 1)[-1]
            if name in _DESKTOP_COMMANDS:
                return name
        return None
    if isinstance(value, list | tuple):
        for item in value:
            if (found := _names_a_desktop_command(item)) is not None:
                return found
    return None


def _desktop_hook(event: str, args: tuple[object, ...]) -> None:
    """The audit hook: refuse a child process that would reach the desktop or a daemon."""
    if not _DESKTOP_GUARD["armed"]:
        return
    positions = _PROCESS_EVENTS.get(event)
    if positions is None:
        return
    for position in positions:
        if position < len(args) and (found := _names_a_desktop_command(args[position])) is not None:
            _desktop_refusals.append(f"{event} {found}")
            pytest.fail(
                f"a unit test tried to reach the desktop ({event} {found}) — patch the call "
                "(subprocess.run / Popen) in the test; a mutant of the code under test must "
                "not be able to act on the operator's machine",
                pytrace=True,
            )


_PLACEHOLDER_ENV_FILE = _PROBE_DIR / ".env"
"""The suite's own `.env`: a placeholder key and nothing else, mode 0600, under the probe
directory (removed at unconfigure). Holding a placeholder rather than nothing lets every
agent test build its client through the real read path — `ClaudIAAgent` refuses to build
without a key — while no test can read the operator's file."""


def _keep_the_real_env_file_out() -> None:
    """No test reads the operator's `.env` (CLA-SEC-009), before any claudia module loads.

    `claudia/panel_app.py` calls `env_file.load()` at module scope, and collection imports
    that module long before the first fixture runs — measured 2026-09-13, when the call was
    `load_dotenv`: a probe inside a running test found a real `sk-ant-` key of length 108 and
    `IBKR_FLEX_TOKEN` in `os.environ`. So, from `pytest_configure`: the loader's file is the
    suite's own stand-in above; the core's default search (`find_dotenv`, walking up from its
    `config.py` — the core repository's own `.env` under the developer override) finds
    nothing; and `load_dotenv` is a no-op for any third party (`crawl4ai` calls it at import).
    A test of the loader points `env_file.ENV_FILE` at a file under its `tmp_path`.
    """
    import dotenv
    import dotenv.main
    import ibkr_core_mcp.config as core_config

    from claudia import env_file

    noop = lambda *args, **kwargs: False  # noqa: E731
    dotenv.load_dotenv = noop
    dotenv.main.load_dotenv = noop
    # `setattr`: the core imports `find_dotenv` without re-exporting it, so an attribute
    # assignment is refused by mypy; the patch is the same.
    setattr(core_config, "find_dotenv", lambda *args, **kwargs: "")  # noqa: B010
    dotenv.find_dotenv = lambda *args, **kwargs: (
        ""
    )  # the genuine `load_dotenv`, if a test restores it,
    dotenv.main.find_dotenv = lambda *args, **kwargs: (
        ""
    )  # searches upward from the test file otherwise
    _PLACEHOLDER_ENV_FILE.write_text(
        "ANTHROPIC_API_KEY=sk-ant-placeholder-for-unit-tests\n", encoding="utf-8"
    )
    _PLACEHOLDER_ENV_FILE.chmod(0o600)
    env_file.ENV_FILE = _PLACEHOLDER_ENV_FILE


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

    The operator-data hook is armed here too, for the same reason as the sockets: from here
    on, test modules are imported and fixtures of every scope run, and a function-scoped
    fixture would arm it only for the test body.
    """
    from pytest_socket import disable_socket

    global _GUARDED_ENV_FILE
    if live_api_opted_in():
        _GUARDED_ENV_FILE = None
    else:
        _keep_the_real_env_file_out()
        for name in [k for k in os.environ if k.startswith(_SECRET_ENV_PREFIXES)]:
            del os.environ[name]
    # After the scrub, never before: `IBKR_SQLITE_PATH` carries the scrubbed prefix. Set here,
    # before collection, because `panel_app._DB_PATH` is read at import.
    os.environ.update(_PROBE_ENV)
    _reports_at_start.clear()
    _reports_at_start.update(_real_report_names())
    disable_socket(allow_unix_socket=True)
    sys.addaudithook(_operator_data_hook)
    _OPERATOR_GUARD["armed"] = True
    sys.addaudithook(_desktop_hook)
    _DESKTOP_GUARD["armed"] = True


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if any test touched real data — a probe created or a report leaked.

    A hook rather than a test because no test can run last by construction; the check has
    to see the whole session. `session.exitstatus` is what `pytest.main` returns after this
    hook, so setting it is what turns the run red.
    """
    violations = _real_data_violations(
        (Path(v) for v in _PROBE_ENV.values()),
        _reports_at_start,
        _real_report_names(),
        _operator_refusals,
        _desktop_refusals,
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

    _OPERATOR_GUARD["armed"] = False
    _DESKTOP_GUARD["armed"] = False
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
