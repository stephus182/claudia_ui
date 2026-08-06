"""Repo-level guards: only the session owner may write to the gateway or the container.

Invariant §3.1 of `docs/plans/2026-08-06-gateway-session-lifecycle-owner.md` — *one owner
writes* — is not something a unit test of any single module can defend. It is a property
of the **whole tree**: it is violated by a new call appearing anywhere, which is exactly
how the original defect arrived. So it is asserted by scanning the source.

This is the same shape as the existing Hard Rule 1 assertion in
`tests/test_panel_dashboard.py`, which checks *every* `Tabulator` rather than a fixed one:
a guard that names the things it permits will fail on the thing nobody thought of, and a
guard that names the things it forbids will not.

## The allowlists are a work-list

Each entry records **which stage removes it**. When a stage lands, its entry is deleted
and this file tightens automatically. An allowlist that only ever grows is a rule being
abandoned one exception at a time; these are meant to shrink to nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_CLAUDIA = Path(__file__).resolve().parent.parent / "claudia"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# The module that is allowed to do these things. The whole point of the plan.
_OWNER = "gateway_session.py"

# Endpoints that create, destroy or re-establish a session. A call to any of these from
# outside the owner is a second authority over the session's lifecycle.
_SESSION_WRITE_ENDPOINTS = re.compile(r"ssodh/init|/logout|reauthenticate")

# Allowed session-write sites, with the stage that removes each.
_SESSION_WRITE_ALLOWED = {
    # `--release` is a deliberate manual escape hatch, never called automatically. Its
    # scope relative to IBKR Mobile is undocumented (see the docstring), which is why
    # `recover()` does not use it.
    "gateway_preflight.py": "manual --release only; nothing automatic calls it",
}


def _python_sources() -> list[Path]:
    """Every shipped module, excluding caches."""
    return sorted(p for p in _CLAUDIA.rglob("*.py") if "__pycache__" not in p.parts)


def _offenders(pattern: re.Pattern[str], allowed: dict[str, str]) -> list[str]:
    """Files matching `pattern` in real code — comments and docstrings excluded.

    Docstrings are excluded deliberately: this module's own design notes quote
    `POST /logout` repeatedly to explain why recovery does *not* use it, and a guard that
    counted prose would force those explanations out of the code, which is the opposite
    of what it is for.
    """
    hits = []
    for path in _python_sources():
        if path.name == _OWNER or path.name in allowed:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        docstrings = {
            ast.get_docstring(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef
                          | ast.AsyncFunctionDef)
        }
        for lineno, line in enumerate(source.splitlines(), 1):
            code = line.split("#", 1)[0]
            if not pattern.search(code):
                continue
            if any(d and line.strip() in d for d in docstrings):
                continue
            hits.append(f"{path.name}:{lineno}: {line.strip()}")
    return hits


def test_only_the_owner_writes_to_a_session_endpoint():
    """`ssodh/init`, `/logout` and `reauthenticate` create or destroy sessions.

    Scattered across modules they become several authorities over one session, which is
    the condition that made every earlier fix uncover another gap.
    """
    offenders = _offenders(_SESSION_WRITE_ENDPOINTS, _SESSION_WRITE_ALLOWED)
    assert not offenders, (
        "Session-affecting write outside claudia/gateway_session.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nMove it into the owner, or add it to _SESSION_WRITE_ALLOWED with the "
          "stage that removes it."
    )


def test_only_the_owner_mutates_the_container():
    """`docker stop|rm|restart|run` destroys whatever session the container holds.

    The original defect was exactly this: `GatewayManager.start()` removing a container
    before the pre-flight that was supposed to inspect it.
    """
    pattern = re.compile(r"""docker["'\s,\]]+\s*["']?(stop|rm|restart|run)\b""")
    offenders = _offenders(pattern, {})
    assert not offenders, (
        "Container mutation outside claudia/gateway_session.py:\n  " + "\n  ".join(offenders)
    )


def test_the_login_page_is_opened_from_exactly_one_place():
    """Every route to a login page must pass the owner's pre-flight.

    `CLAUDE.md` states the rule as "from a script, a button, or by hand". On 2026-08-06
    the button honoured it in the wrong order and the launcher skipped it entirely, so
    stating it in prose has already been shown to be insufficient.
    """
    offenders = _offenders(re.compile(r"open_login_page|webbrowser\.open"), {})
    assert not offenders, (
        "The login page is opened outside the session owner:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name,stage", sorted(_SESSION_WRITE_ALLOWED.items()))
def test_every_allowlist_entry_still_exists(name, stage):
    """A stale exemption silently widens the rule.

    If the file is gone, or no longer contains a session write, the entry has outlived
    its purpose and must be deleted — otherwise the allowlist records permission nobody
    is using and the guard reads as weaker than it is.
    """
    path = _CLAUDIA / name
    assert path.exists(), f"{name} is allowlisted ({stage}) but no longer exists"
    assert _SESSION_WRITE_ENDPOINTS.search(path.read_text()), (
        f"{name} is allowlisted ({stage}) but no longer performs a session write — "
        "delete the entry."
    )


def test_the_keepalive_script_honours_the_suspend_lock():
    """The launchd tickler must be silent while a login or recovery is in progress.

    It runs always (`RunAtLoad` + `KeepAlive`), outside any Python process, so the file
    lock is the only mechanism that can reach it. Without this check the one renewer the
    owner cannot see in-process would keep renewing the session being established —
    the exact behaviour that defeated `POST /logout` on 2026-08-05.
    """
    script = (_SCRIPTS / "ibkr-keepalive.sh").read_text()
    assert "session.suspend" in script, (
        "scripts/ibkr-keepalive.sh does not check the suspend lock, so it will tickle "
        "the gateway during a login. See GatewaySession.SuspendLock."
    )
