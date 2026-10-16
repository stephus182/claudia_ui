"""CLA-SEC-010, spawn-site half (gap #100) — processes start only in `tradingview.py` and
`gateway_launch.py`, list-form, never through a shell.

Until 2026-10-16 nothing failed if a third module spawned or `shell=True` appeared; the #93
read could have been placed in `gateway_preflight` with nothing going red. The environment
half is `test_sidecar_child_environment.py`; the no-secret-anywhere rule is gap #103. The
probe sees every way to start a process, not only `subprocess`: `os.system`/`popen`/`exec*`/
`spawn*`/`posix_spawn*`/`fork*`, `asyncio.create_subprocess_*`, `pty.spawn`, `webbrowser`,
`multiprocessing`, and the MCP `stdio_client`.
"""

from __future__ import annotations

from tests.security.structural import PACKAGE_DIR, package_sources, shell_true_keywords, spawn_sites

# Relative to `claudia/`. Each entry says what it spawns and why.
ALLOWED_SPAWN_MODULES = {
    "tradingview.py",  # pgrep / osascript quit / pkill / open -a for TradingView Desktop; git rev-parse; the sidecar through stdio_client
    "gateway_launch.py",  # the docker CLI that reads the gateway's log (gap #93), with _docker_env
}


def test_only_the_two_named_modules_start_processes():
    """Break: `import subprocess` plus a call in `gateway_preflight.py` — red."""
    spawning = {
        str(path.relative_to(PACKAGE_DIR)): sorted(sites)
        for path, source in package_sources()
        if path.is_relative_to(PACKAGE_DIR) and (sites := spawn_sites(source))
    }
    assert set(spawning) == ALLOWED_SPAWN_MODULES, spawning


def test_no_script_starts_a_process():
    """`scripts/` is a dev harness against the same store; "it is only a script" is the
    argument that would put a spawn in one."""
    spawning = {
        str(path): sorted(sites)
        for path, source in package_sources()
        if not path.is_relative_to(PACKAGE_DIR) and (sites := spawn_sites(source))
    }
    assert not spawning, spawning


def test_nothing_in_the_package_or_scripts_passes_shell_true():
    """Break: `shell=True` on the pgrep call — red."""
    offenders = {
        str(path): lines
        for path, source in package_sources()
        if (lines := shell_true_keywords(source))
    }
    assert not offenders, offenders


def test_the_spawn_probe_sees_each_form():
    snippet = (
        "import subprocess\n"
        "import os, asyncio, pty, webbrowser\n"
        "from multiprocessing import Process\n"
        "from mcp.client.stdio import stdio_client\n"
        "from os import system\n"
        "from os import execv as run_it\n"
        "os.system('x')\n"
        "os.popen('x')\n"
        "os.posix_spawn('x', [], {})\n"
        "asyncio.create_subprocess_exec('x')\n"
        "pty.spawn('x')\n"
        "webbrowser.open('x')\n"
    )
    assert spawn_sites(snippet) == {
        "import subprocess",
        "import pty",
        "import webbrowser",
        "from multiprocessing import …",
        "from mcp.client.stdio import …",
        "from os import system",
        "from os import execv",
        "os.system",
        "os.popen",
        "os.posix_spawn",
        "asyncio.create_subprocess_exec",
        "pty.spawn",
        "webbrowser.open",
    }


def test_the_spawn_probe_ignores_an_innocent_os_import():
    assert spawn_sites("from os import path, environ\nprint(path, environ)\n") == set()


def test_the_shell_probe_sees_shell_true():
    assert shell_true_keywords("import subprocess\nsubprocess.run('ls', shell=True)\n") == [2]
