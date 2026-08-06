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

_CLAUDIA = Path(__file__).resolve().parent.parent / "claudia"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# The module that is allowed to do these things. The whole point of the plan.
_OWNER = "gateway_session.py"

# Endpoints that create, destroy or re-establish a session. A call to any of these from
# outside the owner is a second authority over the session's lifecycle.
_SESSION_WRITE_ENDPOINTS = re.compile(r"ssodh/init|/logout|reauthenticate")

# Allowed session-write sites, with the stage that removes each.
#
# **EMPTY since 2026-08-06, and that is the finished state, not an oversight.** The last
# entry was `gateway_preflight.py`, for the `POST /logout` behind `--release`; the review
# on that date moved the function itself into `gateway_session` and left the CLI flag
# calling into it. Nothing in the tree now writes to a session endpoint outside the owner.
#
# Do not add an entry to make a new call pass. The rule this file exists to defend has no
# exceptions left, and an allowlist that starts growing again is the rule being abandoned
# one entry at a time — which is exactly what the module docstring above warns about.
_SESSION_WRITE_ALLOWED: dict[str, str] = {}


def _python_sources() -> list[Path]:
    """Every shipped module, excluding caches."""
    return sorted(p for p in _CLAUDIA.rglob("*.py") if "__pycache__" not in p.parts)


_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "request"})


def _offenders(pattern: re.Pattern[str], allowed: dict[str, str]) -> list[str]:
    """Files that **call** a session-write endpoint, outside the owner.

    ## Why this reads the syntax tree instead of the lines

    A line-based scan counted prose. Every user-facing message this repo writes about the
    borrowed session names the endpoints — `verdict`'s guidance explains that
    `ssodh/init` cannot help, and the `--release` CLI prints "POST /logout" before doing
    it — so a text match flagged two strings that send nothing. That is not a harmless
    false positive: the response to it, taken on 2026-08-05, was to allowlist the whole
    of `gateway_preflight.py`, which then hid any *real* write in the same file. A guard
    noisy enough to need an allowlist ends up weaker than one that is precise.

    So a hit requires an actual call:

    * an HTTP verb (`requests.post(...)`, `self._session.post(...)`) whose source mentions
      one of the endpoints — this catches f-strings, which is how every real call is
      written here; or
    * a method whose *name* is one of them, which is how `IBKRClient.reauthenticate()`
      would appear.

    **Known limit, stated rather than papered over:** a write whose URL is assembled out
    of sight — `path = "/logout"` on one line and `requests.post(base + path)` on another
    — is not caught. Nothing in either repo is written that way, and covering it would
    mean re-admitting the prose matches this exists to drop. If that pattern ever appears,
    extend this rather than widen `_SESSION_WRITE_ALLOWED`.
    """
    hits = []
    for path in _python_sources():
        if path.name == _OWNER or path.name in allowed:
            continue
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            segment = ast.get_source_segment(source, node) or ""
            called_endpoint = pattern.search(node.func.attr) is not None
            sent_to_endpoint = (
                node.func.attr in _HTTP_VERBS and pattern.search(segment) is not None
            )
            if called_endpoint or sent_to_endpoint:
                hits.append(f"{path.name}:{node.lineno}: {segment.splitlines()[0][:90]}")
    return hits


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for a name/attribute chain, else "". Used to track a bound manager."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _is_manager_construction(node: ast.AST) -> bool:
    """Whether `node` constructs a `GatewayManager`, however the class was imported."""
    return (
        isinstance(node, ast.Call)
        and _dotted(node.func).split(".")[-1] == "GatewayManager"
    )


def _manager_driver_offenders() -> list[str]:
    """Every site outside the owner that calls a **method** on a `GatewayManager`.

    Constructing one and passing it on stays legal, and deliberately: that is exactly how
    `panel_app` and `gateway_launch` are supposed to reach the gateway — they hand the
    manager to `GatewaySession.establish`, which reads the session before anything
    touches the container. The owner receives it as a `GatewayManagerLike` parameter, so
    its own calls are on a parameter rather than on a construction and are not matched
    here either way; the owner's file is skipped regardless.

    What is forbidden is *driving* one: `start`, `restart` and `stop` each destroy
    whatever session the container holds, and `startup` additionally opens a login page
    with no pre-flight at all. Matching on the receiver rather than on a list of method
    names is what makes this fail on a method nobody thought of, which is the same
    reasoning as the Hard Rule 1 assertion over every `Tabulator`.
    """
    hits = []
    for path in _python_sources():
        if path.name == _OWNER:
            continue
        tree = ast.parse(path.read_text())
        bound = {
            _dotted(target)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and _is_manager_construction(node.value)
            for target in node.targets
            if _dotted(target)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            receiver = node.func.value
            if _is_manager_construction(receiver) or _dotted(receiver) in bound:
                hits.append(
                    f"{path.name}:{node.lineno}: .{node.func.attr}() on a GatewayManager"
                )
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

    ## Why this asserts two different things

    The literal-`docker` scan alone was **measured to be vacuous on 2026-08-06**: a probe
    module added to `claudia/` containing `GatewayManager().start()`, `.restart()` and
    `.stop()` — the 2026-08-06 defect, verbatim — passed all five tests in this file, as
    did one containing `GatewayManager().startup()`. The string `docker` never appears in
    claudia_ui: every container command is spelled `subprocess.run(["docker", ...])`
    inside `ibkr_core_mcp/gateway/manager.py`, which this file does not scan and should
    not. So the regex defended a shape that has never existed here, while the shape that
    actually cost a day went straight through.

    Both checks are kept. The regex still catches someone shelling out directly; the AST
    walk catches the real route, which is a method call on a manager object.
    """
    pattern = re.compile(r"""docker["'\s,\]]+\s*["']?(stop|rm|restart|run)\b""")
    offenders = _offenders(pattern, {}) + _manager_driver_offenders()
    assert not offenders, (
        "Container mutation outside claudia/gateway_session.py:\n  " + "\n  ".join(offenders)
        + "\n\nConstruct the manager and hand it to GatewaySession.establish/recover "
          "instead — the owner reads the session before anything touches the container."
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


def test_every_allowlist_entry_still_exists():
    """A stale exemption silently widens the rule.

    If the file is gone, or no longer contains a session write, the entry has outlived
    its purpose and must be deleted — otherwise the allowlist records permission nobody
    is using and the guard reads as weaker than it is.

    Written as a loop rather than a `parametrize` because the allowlist is now **empty**:
    `pytest.mark.parametrize` over an empty sequence reports an empty parameter set, so
    the file's best possible state would have shown up as a warning. A loop over nothing
    is a pass, which is the correct reading.
    """
    for name, stage in sorted(_SESSION_WRITE_ALLOWED.items()):
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


# ── The shell layer, where two of the six 2026-08-06 gaps actually lived ─────

_LAUNCHER = Path(__file__).resolve().parent.parent / "start-claudia.sh"


def _shell_code_lines(path: Path) -> list[tuple[int, str]]:
    """Numbered lines of a shell script with whole-line comments dropped.

    Only lines whose first non-blank character is `#` are removed. Stripping from an
    inline `#` would also cut it out of quoted strings and URLs, and these scripts carry
    both — the false negative that would create is worse than the few comment lines this
    leaves in, because every pattern below is a call shape that does not appear in prose.
    """
    return [
        (n, line)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _shell_sources() -> list[Path]:
    """`start-claudia.sh` plus every script in `scripts/`.

    The launcher lives at the repo root rather than in `scripts/`, and it is where gap 1
    was — so a guard that scanned only `scripts/` would have missed the file the rule was
    written about.
    """
    return [_LAUNCHER, *sorted(_SCRIPTS.glob("*.sh"))]


def test_no_shell_script_drives_the_gateway_directly():
    """Gap 1: `start-claudia.sh` ran no pre-flight at all, because it drove the manager.

    Until 2026-08-06 the launcher called `GatewayManager.startup()`, which removes any
    existing container and opens a login page with nothing having read the session first.
    The fix was to route it through `python -m claudia.gateway_launch`; nothing stopped
    the old line coming back, and the Python-only guards above cannot see a shell file.

    `scripts/gateway-reset.sh` reads `GatewayManager.CONTAINER_NAME` and must keep
    passing: deriving a fact from its authority is the behaviour this file wants more of.
    What is forbidden is *constructing* a manager or driving one — hence the parenthesis.
    """
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in _shell_sources()
        for n, line in _shell_code_lines(path)
        if re.search(r"GatewayManager\(|\.startup\(|open_login_page", line)
    ]
    assert not offenders, (
        "A shell script drives the gateway instead of going through "
        "claudia.gateway_launch:\n  " + "\n  ".join(offenders)
    )


def test_the_launcher_blocks_until_the_session_is_resolved():
    """Gap 3: the launcher opened the login page and started ClaudIA immediately.

    Three pollers then hammered a gateway that was still mid-authentication — captured in
    the gateway's own log at 13:12:25-13:12:30 on 2026-08-06. The fix is that
    `gateway_launch` blocks, which only helps if the launcher actually waits for it: a
    single `&` restores the gap in full, silently, and no other test in this repo would
    notice.

    `|| true` is expected and must keep passing — a gateway problem deliberately does not
    stop the UI coming up, because the UI is where the recovery button and the status dot
    live. `&&` is a conditional, not a background operator, so it is excluded too.
    """
    invocations = [
        (n, line)
        for n, line in _shell_code_lines(_LAUNCHER)
        if "claudia.gateway_launch" in line
    ]
    assert invocations, (
        "start-claudia.sh no longer invokes claudia.gateway_launch. If the gateway is "
        "started some other way now, that way must still pre-flight and still block."
    )
    backgrounded = [
        f"{n}: {line.strip()}"
        for n, line in invocations
        if re.search(r"(?<!&)&\s*$", line)
    ]
    assert not backgrounded, (
        "start-claudia.sh backgrounds the gateway launcher, so ClaudIA starts polling a "
        "gateway that is still mid-login:\n  " + "\n  ".join(backgrounded)
    )
