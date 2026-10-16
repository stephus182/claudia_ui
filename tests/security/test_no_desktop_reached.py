"""No unit test can start a process that reaches the operator's desktop or daemons
(gap #100, 2026-10-16) — the core's register F32/F44 guard, for this suite.

`tradingview.py` quits TradingView with `osascript` and `pkill`, launches it with `open -a`,
and `gateway_launch.py` runs the docker CLI; the core's Gate 2 is a child running
`_order_dialog.py` or `osascript`, and its login window is Chrome. A test — or a mutant of
one — that reached any of those would act on the operator's machine, and the socket guard
does not stop a process start. Inventoried 2026-10-16 with the hook reporting: the suite's
45 real starts are `git`, `python` and a shimmed `bash`, none of these names, so the guard
refuses by name and exempts nothing. Sized to that inventory (the operator's choice over
refusing every start with an exemption list).

**Every test here is safe on a broken guard:** each would run something harmless —
`osascript -e "return 1"` shows nothing, `pkill -x` of a name no process has kills nothing,
`open` with no argument prints its usage, `docker --version` prints and exits — and asserts
the refusal came first. The names are the ones the two repositories actually spawn, no more.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

import pytest

from tests import conftest


@contextlib.contextmanager
def refused(what: str):
    before = len(conftest._desktop_refusals)
    with pytest.raises(pytest.fail.Exception, match="reach the desktop"):
        yield
    recorded = conftest._desktop_refusals[before:]
    del conftest._desktop_refusals[before:]
    assert len(recorded) == 1 and what in recorded[0], recorded


@pytest.mark.parametrize(
    ("argv", "what"),
    [
        (["osascript", "-e", "return 1"], "osascript"),
        (["pkill", "-x", "no-such-process-name-for-this-test"], "pkill"),
        (["open"], "open"),
        (["docker", "--version"], "docker"),
        (["/usr/bin/python3", "/tmp/_order_dialog.py"], "_order_dialog.py"),
        (
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
            "Google Chrome",
        ),
    ],
    ids=["osascript", "pkill", "open", "docker", "dialog-script", "chrome"],
)
def test_a_desktop_command_is_refused_before_it_starts(argv, what):
    """Break: the name removed from `_DESKTOP_COMMANDS` (or the launcher list) — that case red."""
    assert conftest._DESKTOP_GUARD["armed"]  # before anything real
    with refused(what):
        subprocess.run(argv, capture_output=True, check=False)


def test_an_app_window_flag_is_refused():
    with refused("--app="):
        subprocess.run(["true", "--app=https://localhost:5055"], capture_output=True, check=False)


def test_a_shell_command_string_is_read_word_by_word():
    """`os.system` audits one string; the launcher is found as a word of it."""
    with refused("osascript"):
        os.system("osascript -e 'return 1'")  # noqa: S605 - refused before it runs


def test_git_python_and_bash_are_not_refused():
    """The suite's real starts stay allowed: the guard is by name, not blanket."""
    done = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    assert done.returncode == 0


def test_a_refusal_swallowed_by_except_exception_still_fails_the_run():
    """`pytest.fail` is a `BaseException`; the record is what `pytest_sessionfinish` reads."""
    before = len(conftest._desktop_refusals)
    with contextlib.suppress(BaseException):  # the swallow this test is about
        subprocess.run(["osascript", "-e", "return 1"], capture_output=True, check=False)
    recorded = conftest._desktop_refusals[before:]
    del conftest._desktop_refusals[before:]
    assert recorded and "osascript" in recorded[0]
    assert conftest._real_data_violations((), set(), set(), (), recorded) == [
        f"a test reached for the desktop: {recorded[0]}"
    ]
