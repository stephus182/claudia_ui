"""Detect a stale editable install of `ibkr_core_mcp` before it silently breaks a tool.

**This has cost real debugging three times.** `ibkr_core_mcp` is installed here with
`pip install -e ../ibkr_core_mcp --config-settings editable_mode=strict`. Strict mode does
not put the source directory on `sys.path`; it builds a **snapshot** of symlinks under
`build/__editable__…/ibkr_core_mcp/`, one per module *that existed at install time*. Add,
rename or remove a module in the library and this project keeps resolving the old set until
the install is re-run.

Three incidents, all the same root cause and none of them loud:

  1. 2026-07-28 — a newly added `ibkr_core_mcp` module raised `ModuleNotFoundError` inside
     ClaudIA while existing perfectly on disk.
  2. 2026-07-28 — the `[scraper]` extra had never been installed here, so the browser rung
     was dark: detection worked, recovery dead-ended, and nothing said so.
  3. 2026-07-30 — `scrape_fallback.py` was renamed to `local_browser.py`; the snapshot
     still advertised the old name and knew nothing of the new one.

**Why it is never caught at startup on its own:** every scraper import in `claude_tools.py`
is deliberately lazy, so the optional `[scraper]` extra is not required to import the
package. ClaudIA therefore boots perfectly and fails only when someone actually calls a web
tool — the worst possible time and the least obvious cause.

Strict mode cannot simply be dropped: re-tested 2026-07-30 against mypy 2.3.0, a default
(lazy) editable install produces 14 `Cannot find implementation or library stub for module
named "ibkr_core_mcp"` errors, because mypy's static resolution cannot see the meta-path
finder that mode installs. So the trap is inherent to a setup we need, and the fix is to
make it *loud and early* rather than to remove it.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

log = logging.getLogger(__name__)

REINSTALL_COMMAND = 'pip install -e "../ibkr_core_mcp" --config-settings editable_mode=strict'


def stale_modules() -> list[str]:
    """Return the `ibkr_core_mcp` modules that exist in source but not in the install.

    Works by resolving the installed package's `__init__.py`: under a strict editable
    install it is a **symlink** into the real source tree, so `Path.resolve()` yields the
    directory the developer actually edits. Comparing the two directories' `*.py` sets is
    exact — no version numbers, no timestamps, no heuristics.

    Returns:
        Sorted module filenames present in the source tree but missing from the installed
        snapshot — the ones that will raise `ModuleNotFoundError` at call time. Empty when
        the install is current, and **also** empty for any non-editable install (a normal
        `pip install` from git has no snapshot and cannot go stale this way), so a
        production deployment never sees a false alarm.
    """
    try:
        import ibkr_core_mcp
    except ImportError:  # pragma: no cover - the package is a hard dependency
        return []

    installed_init = Path(inspect.getfile(ibkr_core_mcp))
    if not installed_init.is_symlink():
        # Not a strict-editable snapshot (regular install, or a wheel). Nothing to drift.
        return []

    snapshot_dir = installed_init.parent
    source_dir = installed_init.resolve().parent
    if snapshot_dir == source_dir:
        return []

    installed = {p.name for p in snapshot_dir.glob("*.py")}
    on_disk = {p.name for p in source_dir.glob("*.py")}
    return sorted(on_disk - installed)


def warn_if_stale() -> list[str]:
    """Log a loud, actionable error when the editable install has drifted, and return
    the offending module names.

    Called once at ClaudIA startup. Deliberately does **not** raise: a stale install
    breaks the web tools, not the trading tools, and refusing to start would be a worse
    failure than the one being reported. The message names the modules and the exact
    command, because every previous occurrence was diagnosed from a bare
    `ModuleNotFoundError` with no hint that reinstalling was the fix.
    """
    missing = stale_modules()
    if missing:
        log.error(
            "STALE ibkr_core_mcp INSTALL — %d module(s) exist in the library but are "
            "invisible here: %s. Any tool that imports one will fail with "
            "ModuleNotFoundError when called, even though ClaudIA started cleanly. Fix: %s",
            len(missing),
            ", ".join(missing),
            REINSTALL_COMMAND,
        )
    return missing
