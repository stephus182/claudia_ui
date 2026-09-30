"""No SQLite URI is built by hand in `claudia/` or `scripts/` (gap #83, 2026-09-30).

SQLite reads a URI's query after the first `?`, its fragment after `#`, and decodes `%HH` in
the path; an ordinary filename becomes a URI only after `?` → `%3f`, `#` → `%23` and the rest
(https://www.sqlite.org/uri.html § 3.1). A hand-formatted `f"file:{path}?mode=ro"` over a path
holding `#` or `?` therefore opened — and created — a different file beside the directory,
**read-write**, with `mode=ro` lost; one holding `%41` failed to open (measured 2026-09-30,
both forms side by side over eight directory names). Seven openers carried the form: the
dashboard's `connect`, the four `flex_sync` reads of the core's store, `replay_eval.cut_store`
on `claudia.db`, and the corpus test. Each builds the URI with `Path.as_uri()` now, which
escapes the path.

This file forbids the form structurally: no string piece in the package or its scripts may
begin with `file:`. `tests/` is not scanned — it spells hand-built URIs on purpose, as inputs
the CLA-SEC-009 guard must recognise.
"""

from __future__ import annotations

import ast

from tests.security.structural import PACKAGE_DIR, SCRIPTS_DIR, package_sources


def _hand_built_uris(source: str) -> set[str]:
    """The functions (or `<module>`) holding a string piece that begins with `file:`.

    f-string pieces count — `f"file:{path}?mode=ro"` is a `file:` piece, then the path — and
    so do bytes. Docstrings are skipped: prose may show the wrong form to warn against it.
    Keyed on the scheme, not on `?mode=`: ClaudIA has no single opener, so every site builds
    its own URI, and `Path.as_uri()` is how it must.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()

    def visit(node: ast.AST, function: str) -> None:
        """Walk `node`, naming each hit after the innermost function holding it."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Constant) and id(child) not in docstrings:
                value = child.value
                if (isinstance(value, str) and value.startswith("file:")) or (
                    isinstance(value, bytes) and value.startswith(b"file:")
                ):
                    found.add(function)
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else None
            )
            visit(child, inner or function)

    visit(tree, "<module>")
    return found


def test_no_sqlite_uri_is_built_by_hand():
    """Every file URI in the package and its scripts comes from `Path.as_uri()`."""
    sites = {
        str(path.relative_to(PACKAGE_DIR.parent)): sorted(found)
        for path, source in package_sources()
        if (found := _hand_built_uris(source))
    }
    assert sites == {}, sites


def test_the_scan_covers_the_package_and_the_scripts():
    """`replay_eval.cut_store` was one of the seven, and it lives in `scripts/`."""
    walked = {path.parent for path, _source in package_sources()}
    assert PACKAGE_DIR in walked and SCRIPTS_DIR in walked


def test_the_probe_sees_every_hand_built_spelling_and_no_docstring():
    """The checker above, against each way of writing the form — and the right way."""
    source = '''
def right(path):
    """file: URIs come from as_uri(), never from f"file:{path}?mode=ro"."""
    return f"{path.as_uri()}?mode=ro"

def f_string(path):
    return f"file:{path}?mode=ro"

def concatenated(path):
    return "file:" + str(path)

def percent(path):
    return "file:%s?mode=ro" % path

def formatted(path):
    return "file:{}?mode=ro".format(path)

async def raw_bytes(path):
    return b"file:" + bytes(path)

TOP = "file:x.db?mode=rw"
'''
    assert _hand_built_uris(source) == {
        "f_string",
        "concatenated",
        "percent",
        "formatted",
        "raw_bytes",
        "<module>",
    }
