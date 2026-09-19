"""Answer two questions about the installed `ibkr_core_mcp` before either bites silently.

**Which install is this?** (`core_install_origin`, 2026-09-19) — the local checkout, a
frozen copy of it, the PyPI release, or nothing. Added when the core moved to PyPI: CI's
forward-compatibility lane installs the checkout editable *over* the release, and if that
override silently fails the lane tests the release while reporting forward compatibility.
Versions cannot tell them apart — core `main` and the published release both declared 2.0.1
on 2026-09-19 — so the question has to be about provenance.

**Has the editable snapshot drifted?** (`stale_modules`, below) — the original subject of
this module, and still the one that has cost real debugging.

**Scope, since 2026-09-19: the developer override only.** `ibkr_core_mcp` is an ordinary
PyPI dependency now (`ibkr-core-mcp>=2.0.1,<3`), and a wheel cannot go stale this way —
`stale_modules()` returns `[]` for one by construction, as it always has. What is described
below is what happens when a developer overrides that copy with the local checkout to work on
both repositories at once (CLAUDE.md § Dev Setup step 3). That override is still the normal
state on this machine, and the trap below is still live in it.

**This has cost real debugging three times.** Under the override, `ibkr_core_mcp` is installed
with `pip install -e ../ibkr_core_mcp --config-settings editable_mode=strict`. Strict mode does
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
import json
import logging
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

log = logging.getLogger(__name__)

CORE_DISTRIBUTION = "ibkr-core-mcp"

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


# ── Which install is it: the checkout, or the PyPI copy? ─────────────────────


def core_install_origin() -> str:
    """Where the installed `ibkr_core_mcp` came from, per PEP 610.

    `pip` records the provenance of anything installed from a direct reference — a path, a
    URL, a VCS — in the distribution's `direct_url.json`, and records **nothing** for an
    ordinary resolve from an index. So the file's absence is itself the answer, and
    `dir_info.editable` distinguishes `pip install -e <path>` from `pip install <path>`.
    Source: https://packaging.python.org/en/latest/specifications/direct-url/

    Returns:
        - `"editable"`  — an editable install of a local checkout: edits reach the process.
        - `"directory"` — a *copy* of a local checkout, frozen at install time.
        - `"index"`     — resolved from PyPI: the released version, whatever it declares.
        - `"unknown"`   — the distribution is not installed, which is not a claim about
          anything and must never be read as one.

    Why this exists rather than comparing versions: core `main` and the published release
    can declare the *same* version string (both were 2.0.1 on 2026-09-19, nine commits
    apart), so provenance is the only thing that tells them apart.
    """
    try:
        raw = distribution(CORE_DISTRIBUTION).read_text("direct_url.json")
    except PackageNotFoundError:
        return "unknown"

    if raw is None:
        return "index"
    try:
        info = json.loads(raw)
    except ValueError:  # pragma: no cover - malformed metadata is not a claim either
        return "unknown"
    if not isinstance(info, dict) or "dir_info" not in info:
        return "unknown"
    dir_info = info["dir_info"]
    editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
    return "editable" if editable else "directory"


def main(argv: list[str] | None = None) -> int:
    """Report the install, and optionally refuse one that is not the local checkout.

    Two callers, one command:

    - `python -m claudia.install_check` — a diagnostic. It states what is installed and
      whether an editable snapshot has drifted, and refuses nothing. (The module had no
      entry point at all before 2026-09-19: the command exited 0 in silence, which reads
      exactly like a clean report.)
    - `python -m claudia.install_check --require-editable` — the guard CI's
      forward-compatibility lane runs after installing the core editable over the PyPI
      copy. That lane exists to test *unreleased* core code; if the override silently
      failed it would test the pinned release instead and pass for the wrong reason, with
      no red tick anywhere because the job carries `continue-on-error`.
    """
    argv = list(argv if argv is not None else [])
    origin = core_install_origin()
    print(f"{CORE_DISTRIBUTION} install origin: {origin}")

    if "--require-editable" in argv and origin != "editable":
        raise SystemExit(
            f"expected an editable install of {CORE_DISTRIBUTION}, found {origin!r}. "
            "The developer override did not take, so anything run against this "
            "environment describes the released core, not the checkout. Fix: "
            f"{REINSTALL_COMMAND}"
        )

    for module in stale_modules():
        print(f"stale (in the source tree, missing from the install): {module}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main(__import__("sys").argv[1:]))
