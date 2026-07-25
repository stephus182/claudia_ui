"""Docstring coverage gate for `claudia/`.

The 2026-07-25 audit found 67 undocumented definitions, including the three handlers that
fire live IBKR orders, the SSRF boundary (`_fetch_web_page`), and the 157-line
`_init_session` that owns the `_init_lock` data-loss hazard. Nothing in CI could see them.

ruff's pydocstyle rules are enabled (`"D"` in pyproject) but only reach **public** symbols —
D103 is "undocumented-*public*-function". Roughly half of this package is `_`-prefixed, and
that half contains nearly every safety-critical function, so ruff alone would leave the
important cases unguarded. This AST sweep covers all of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parent.parent / "claudia"

# Dunder methods whose contract is fully defined by the language. Anything with a
# non-obvious contract — __aenter__/__aexit__ govern exception suppression — is NOT here.
_EXEMPT_DUNDERS = frozenset({"__repr__", "__str__", "__eq__", "__hash__"})


def _undocumented(path: Path) -> list[str]:
    """Return "line:name" for every definition in `path` lacking a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[str] = []
    if ast.get_docstring(tree) is None:
        missing.append("1:<module>")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name in _EXEMPT_DUNDERS:
            continue
        if ast.get_docstring(node) is None:
            missing.append(f"{node.lineno}:{node.name}")
    return missing


@pytest.mark.parametrize(
    "module", sorted(_PACKAGE.glob("*.py")), ids=lambda p: p.name
)
def test_every_definition_has_a_docstring(module: Path):
    """Every module, class, function, and method in claudia/ must be documented.

    Includes private (`_`-prefixed) and nested definitions, which ruff's D1xx rules skip —
    that is the whole reason this test exists alongside them.
    """
    missing = _undocumented(module)
    assert not missing, (
        f"{module.name} has {len(missing)} undocumented definition(s):\n  "
        + "\n  ".join(missing)
    )


def test_no_stale_chainlit_references_presented_as_current():
    """Chainlit/app.py may only be referenced in the past tense (2026-07-24 cutover).

    Historical provenance ("the removed Chainlit app.py", "Panel's equivalent of Chainlit's
    ChatStep") is intentional and stays. What must never return is a docstring describing
    `claudia/app.py`, port 8000, or `/api/status` as *current* behaviour, or a dangling
    `app.py:NNN` line cite pointing into a file that no longer exists.
    """
    import re

    offenders: list[str] = []
    for path in sorted(_PACKAGE.glob("*.py")):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"app\.py:\d", line):
                offenders.append(f"{path.name}:{num}: dangling app.py line cite")
            if re.search(r"panel_ws_fix|/api/status|localhost:8000", line):
                offenders.append(f"{path.name}:{num}: stale Chainlit-era reference")
    assert not offenders, "Stale references found:\n  " + "\n  ".join(offenders)
