"""Every SQLite URI in `claudia/` and `scripts/` comes from the one opener (gaps #83, #89).

SQLite reads a URI's query after the first `?`, its fragment after `#`, and decodes `%HH` in
the path; an ordinary filename becomes a URI only after `?` → `%3f`, `#` → `%23` and the rest
(https://www.sqlite.org/uri.html § 3.1). A hand-formatted `f"file:{path}?mode=ro"` over a path
holding `#` or `?` therefore opened — and created — a different file beside the directory,
**read-write**, with `mode=ro` lost; one holding `%41` failed to open (measured 2026-09-30,
both forms side by side over eight directory names). Seven openers carried the form: the
dashboard's `connect`, the four `flex_sync` reads of the core's store, `replay_eval.cut_store`
on `claudia.db`, and the corpus test. Their first replacement, `Path.as_uri()` at each site,
wrote a NUL as `%00`, where SQLite ends the path (gap #89). They all open through
`claudia.sqlite_read_only.connect_read_only` now, which is tested once for both.

Two rules, structural: no string piece in the package or its scripts may begin with `file:`,
and no call there but the opener's may pass `uri=`. `tests/` is not scanned — it spells
hand-built URIs on purpose, as inputs the CLA-SEC-009 guard must recognise.
"""

from __future__ import annotations

import ast

from tests.security.structural import PACKAGE_DIR, SCRIPTS_DIR, package_sources

_OPENER = PACKAGE_DIR / "sqlite_read_only.py"


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of every module, class and function docstring node in `tree`."""
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _hand_built_uris(source: str) -> set[str]:
    """The functions (or `<module>`) holding a string piece that begins with `file:`.

    f-string pieces count — `f"file:{path}?mode=ro"` is a `file:` piece, then the path — and
    so do bytes. Docstrings are skipped: prose may show the wrong form to warn against it.
    Keyed on the scheme as well as on `uri=` (below): with `SQLITE_USE_URI` compiled in, as in
    the SQLite this runs on, a `file:` name is a URI even without `uri=True`.

    It reads literal pieces only. A scheme assembled at run time (`"file" + ":"`, a variable, a
    `join`) is not seen — the rule catches the ordinary way of writing the form, not an
    evasion of it; and a message that happens to begin `file:` is flagged, to be reworded.
    """
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
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


def _uri_opens(source: str) -> set[str]:
    """The functions (or `<module>`) making a call that passes `uri=` — a SQLite URI open.

    Any callee counts, so a new route (`sqlite3.Connection(..., uri=True)`) is seen too. Not
    seen: `uri` passed positionally (the eighth parameter of `sqlite3.connect`) or through
    `**kwargs` — the rule catches the ordinary spelling, not an evasion.
    """
    found: set[str] = set()

    def visit(node: ast.AST, function: str) -> None:
        """Walk `node`, naming each hit after the innermost function holding it."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and any(k.arg == "uri" for k in child.keywords):
                found.add(function)
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else None
            )
            visit(child, inner or function)

    visit(ast.parse(source), "<module>")
    return found


def test_no_sqlite_uri_is_built_by_hand():
    """No string piece in the package and its scripts spells a `file:` URI."""
    sites = {
        str(path.relative_to(PACKAGE_DIR.parent)): sorted(found)
        for path, source in package_sources()
        if (found := _hand_built_uris(source))
    }
    assert sites == {}, sites


def test_only_the_one_opener_opens_by_uri():
    """Every read-only open goes through `connect_read_only`, where escaping, `mode=ro` and the
    NUL refusal are tested once. The opener itself must be seen doing it, or the scan is blind."""
    sites = {
        str(path.relative_to(PACKAGE_DIR.parent)): sorted(found)
        for path, source in package_sources()
        if path != _OPENER and (found := _uri_opens(source))
    }
    assert sites == {}, sites
    assert _uri_opens(_OPENER.read_text(encoding="utf-8")) == {"connect_read_only"}


def test_the_scan_covers_the_package_and_the_scripts():
    """`replay_eval.cut_store` was one of the seven, and it lives in `scripts/`."""
    walked = {path.parent for path, _source in package_sources()}
    assert PACKAGE_DIR in walked and SCRIPTS_DIR in walked
    assert _OPENER in {path for path, _source in package_sources()}


def test_the_uri_probe_sees_each_spelling_of_a_uri_open():
    """The `uri=` checker, against each way of writing the open — and a plain one."""
    source = """
import sqlite3

def plain(path):
    return sqlite3.connect(path)

def flagged(path):
    return sqlite3.connect(path, uri=True)

def by_variable(path, flag):
    return sqlite3.connect(path, uri=flag)

def other_route(path):
    return sqlite3.Connection(path, uri=True)

async def nested(path):
    def inner():
        return sqlite3.connect(path, timeout=1, uri=True)
    return inner

TOP = sqlite3.connect("x.db", uri=False)
"""
    assert _uri_opens(source) == {"flagged", "by_variable", "other_route", "inner", "<module>"}


def test_the_probe_sees_the_literal_spellings_and_no_docstring():
    """The `file:` checker, against each literal way of writing the form — and the right way."""
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
