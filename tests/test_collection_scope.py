"""A bare `pytest` runs every test of this repository, and nothing else (gap #116).

`testpaths` in `pyproject.toml` is what a bare run collects from; pytest documents it as a way "to
avoid picking up undesired tests by accident"
(https://docs.pytest.org/en/stable/reference/reference.html#confval-testpaths). It was not set until
2026-11-24. pytest does not read `.gitignore`, so a run in the main checkout also loaded what the
probe folders under `docs/plans/` held: that day 32 test modules, 45 text files it takes for
doctests and four `conftest.py`. The pre-push hook's collection ended in 24 errors, with 134 tests
that are not ours collected beside ours.

The setting opens one hole, which the second test closes: a test module outside `tests/` is never
collected, and nothing says so.
"""

from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def test_a_bare_run_collects_the_tests_folder(pytestconfig: pytest.Config):
    """pytest's own effective value, not the file's text: a `pytest.ini` beside `pyproject.toml`
    would win over it, and this reads what the run was actually given."""
    assert pytestconfig.getini("testpaths") == ["tests"]


def test_no_test_module_lives_where_a_bare_run_does_not_look(pytestconfig: pytest.Config):
    """Every file git tracks, or would add, whose name pytest takes for a test module is under
    `tests/`. A git-ignored file (a probe) is not this repository's and is not judged.

    The patterns are pytest's own for this run. The count keeps an empty listing from passing.
    """
    listed = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    patterns: list[str] = pytestconfig.getini("python_files")
    modules = [
        path
        for path in listed
        if any(fnmatch(PurePosixPath(path).name, pattern) for pattern in patterns)
    ]
    outside = [path for path in modules if not path.startswith("tests/")]
    assert len(modules) - len(outside) > 40, f"git listed {len(modules)} test modules: too few"
    assert outside == [], f"never collected by a bare pytest; move under tests/: {outside}"
