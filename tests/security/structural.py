"""AST helpers for ClaudIA's structural security tests.

These answer "which modules reference an order-write method?" or "which functions call an
execution core?" from source rather than from behaviour. A behavioural test needs an input
that reaches the code; a structural one sees the code whether or not anything calls it yet —
which is the point when the thing guarded against is a *new* call site written by someone
who did not read the rule (audit 2026-09-13, §6).

Every checker takes source text and returns plain data, so each test file can feed it a
deliberately-violating snippet and prove the checker still fires. A structural test that
cannot fail is worse than no test: it reads as coverage.

Deliberately not shared with `ibkr_core_mcp/tests/security/structural.py`. The two packages
ask different questions of different trees, and a shared helper across two repos would make
one repo's CI depend on the other's test layout — the coupling this audit is trying to make
explicit, not deepen.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = _ROOT / "claudia"
SCRIPTS_DIR = _ROOT / "scripts"

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def package_sources() -> Iterator[tuple[Path, str]]:
    """Every `.py` in the package and in `scripts/`, as (path, source).

    `scripts/` is included because a dev harness runs the same agent against the same store:
    `replay_eval.py` builds a real toolkit, and "it is only a script" is exactly the argument
    that would put an execution core in one.
    """
    for directory in (PACKAGE_DIR, SCRIPTS_DIR):
        for path in sorted(directory.rglob("*.py")):
            yield path, path.read_text(encoding="utf-8")


@lru_cache(maxsize=128)
def _tree(source: str) -> ast.Module:
    """Parse once per distinct source text — these tests re-read `agent.py` many times."""
    return ast.parse(source)


def _functions(source: str) -> Iterator[FunctionNode]:
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def functions_named(source: str, name: str) -> list[FunctionNode]:
    """Every function or method called `name`, in source order.

    Plural because a `property` and its setter share one name: `_PanelToolStepHandle.input`
    is a getter that returns a field and a setter that writes it to the UI, and
    `function_named` returns the first — which is the one that does nothing.

    Raises:
        KeyError: no such function, for the same reason `function_named` raises.
    """
    found = [fn for fn in _functions(source) if fn.name == name]
    if not found:
        raise KeyError(name)
    return found


def function_named(source: str, name: str) -> FunctionNode:
    """The first function or method called `name`.

    Raises:
        KeyError: no such function, which means the test is pinning something that has been
            renamed — a failure, never a silent skip.
    """
    for fn in _functions(source):
        if fn.name == name:
            return fn
    raise KeyError(name)


def callee_name(call: ast.Call) -> str | None:
    """The name a call invokes — bare (`f(...)`) or the last attribute (`a.b.f(...)`)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def referenced_names(source: str) -> set[str]:
    """Every bare name and every attribute name appearing anywhere in `source`.

    Docstrings and comments are not included — they are not nodes of this kind — so a module
    may *describe* `place_order` without tripping a rule about reaching it. That distinction
    is why these tests are AST and not grep: `order_flow.py` documents `require_touch_id` in
    its module docstring and must keep being able to.
    """
    tree = _tree(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def imported_modules(source: str) -> set[str]:
    """Every module named by an `import x` or `from x import ...`, at any nesting depth.

    Function-local imports count: `panel_sink` imports the render module inside its methods
    to break a cycle, and a future `from claudia.order_flow import _execute_staged_order_core`
    inside a handler would be exactly as reachable.
    """
    modules: set[str] = set()
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def keyword_arguments_used(source: str) -> set[str]:
    """Every keyword-argument name passed at any call site."""
    return {
        kw.arg
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg is not None
    }


def functions_calling(source: str, callees: Iterable[str]) -> dict[str, set[str]]:
    """Map each enclosing function's name to which of `callees` its body calls.

    The enclosing function is the innermost one, so a handler nested inside a renderer is
    reported under the handler's own name — which is what the click-boundary rule is about.
    """
    wanted = set(callees)
    found: dict[str, set[str]] = {}
    for fn in _functions(source):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = callee_name(node)
                if name in wanted and _innermost_function(fn, node) is fn:
                    found.setdefault(fn.name, set()).add(name)
    return found


def _innermost_function(fn: FunctionNode, target: ast.AST) -> FunctionNode | None:
    """The innermost function inside `fn` (possibly `fn`) that contains `target`."""
    best: FunctionNode | None = fn
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node is not fn:
            for inner in ast.walk(node):
                if inner is target:
                    deeper = _innermost_function(node, target)
                    best = deeper if deeper is not None else node
    return best


def handlers_bound_to(source: str, binder: str) -> set[str]:
    """Names of functions passed as the first positional argument to any `.<binder>(...)`.

    `stage_btn.on_click(_on_stage)` yields `{"_on_stage"}`. This is how "reachable only from
    a click" is expressed structurally: the handler must be a function this call registers.
    """
    bound: set[str] = set()
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and callee_name(node) == binder
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            bound.add(node.args[0].id)
    return bound


def assignment_targets(source: str) -> set[str]:
    """Every attribute name written to: `a.b = x`, `a.b += x`, `a.b: T = x`.

    Used to forbid writing `.clicks`, which is how a Panel button's handler can be fired
    without a human (the parameter the `on_click` watcher is registered on).
    """
    targets: set[str] = set()
    for node in ast.walk(_tree(source)):
        stores: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            stores = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            stores = [node.target]
        for target in stores:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute):
                    targets.add(sub.attr)
    return targets


def self_collaborators(node: ast.AST) -> set[str]:
    """Every `self.<name>` the body touches, whether it is called, read, or passed on.

    Not "called on": `agent._stream_turn` reaches the toolkit as
    `asyncio.to_thread(self._toolkit.execute, …)`, where `self._toolkit.execute` is an
    *argument*, so a call-site-only probe reported four collaborators and missed the one
    that reaches IBKR (measured 2026-09-14 while writing this test — the probe was wrong in
    exactly the direction that would have let a new sink through).

    Returns the attribute names, so `self._toolkit.execute(...)` and `self._toolkit` both
    give `"_toolkit"`, and a plain `self.method()` gives `"method"`.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
        ):
            names.add(child.attr)
    return names


def loops_over(fn: FunctionNode, iterable_name: str) -> list[ast.For]:
    """Every `for … in <iterable_name>:` loop inside `fn`.

    All of them, not the first: `_stream_turn` iterates `tool_calls` twice — once to rebuild
    the assistant turn for the message list, once to execute — and a helper that stopped at
    the first pinned the wrong one while reading as if it pinned the dispatcher (found
    2026-09-14 by the sink test asserting an empty set).

    The `ast.For` node is returned as-is rather than wrapped in a synthetic function: the
    checkers below take any subtree, and a hand-built `FunctionDef` needs fields that differ
    between 3.11 and 3.12.

    Raises:
        KeyError: no such loop, i.e. the dispatcher was restructured. A test pinning it must
            be re-read by a human rather than quietly passing over nothing.
    """
    loops = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == iterable_name
    ]
    if not loops:
        raise KeyError(f"no `for … in {iterable_name}` loop in {fn.name}")
    return loops


def dict_mutations(node: ast.AST, names: Iterable[str]) -> set[str]:
    """Every way `fn` writes to one of `names`: `n[k] = v`, `del n[k]`, `n.update(...)`,
    `n.setdefault(...)`, `n.pop(...)`, `n.clear()`, `n.popitem()`.

    The order-parameter rule is "reject whole, never repair", so a defect check that
    normalised a value in passing would be a fabricated order parameter with a green test
    beside it. Reading `n.get(...)` is not a mutation and is what these functions do all day.
    """
    wanted = set(names)
    mutators = {"update", "setdefault", "pop", "clear", "popitem"}
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in wanted
                ):
                    found.add(f"{target.value.id}[…] =")
        elif isinstance(child, ast.Delete):
            for target in child.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in wanted
                ):
                    found.add(f"del {target.value.id}[…]")
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in mutators
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id in wanted
        ):
            found.add(f"{child.func.value.id}.{child.func.attr}()")
    return found


def called_names(node: ast.AST) -> set[str]:
    """Every function or method name called anywhere inside `fn`."""
    return {
        name
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and (name := callee_name(child)) is not None
    }


def call_line_numbers(node: ast.AST, names: Iterable[str]) -> list[int]:
    """Line numbers of every call to one of `names` inside `node`, ascending."""
    wanted = set(names)
    return sorted(
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and callee_name(child) in wanted
    )


def first_call_line(node: ast.AST) -> int | None:
    """Line number of the first call of any kind inside `node`, or None if it makes none.

    Used to assert that a guard runs *before* everything else in a handler, which is what
    "claimed synchronously, before the first await" has to mean in a test.
    """
    lines = [child.lineno for child in ast.walk(node) if isinstance(child, ast.Call)]
    return min(lines) if lines else None


def dynamic_attribute_sites(node: ast.AST, forbidden: Iterable[str]) -> set[str]:
    """Every `getattr`/`setattr` call that a name probe cannot see through.

    A name probe reads `x.place_order` and `place_order`; it reads nothing at all from
    `getattr(x, "place_order")` or `getattr(x, name)` (verified 2026-09-14). Two shapes are
    reported, and only two:

    * the attribute name is not a string literal — the probe cannot know what it resolves to;
    * the attribute name is a literal in `forbidden` — the probe was evaded in plain sight.

    A literal read of an ordinary field is neither. `agent.py` does
    `getattr(usage, "cache_read_input_tokens", None)` half a dozen times because the
    Anthropic SDK's usage object gains and loses fields between versions, and a rule that
    called that a violation would be deleted rather than obeyed.
    """
    banned = set(forbidden)
    sites: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or callee_name(child) not in ("getattr", "setattr"):
            continue
        if len(child.args) < 2:
            continue
        name_arg = child.args[1]
        if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
            sites.add(f"line {child.lineno}: attribute name is not a literal")
        elif name_arg.value in banned:
            sites.add(f"line {child.lineno}: resolves {name_arg.value!r}")
    return sites


def _panel_pane_aliases(source: str) -> dict[str, str]:
    """Local name → Panel pane class, for every `from panel[.pane] import X [as Y]`.

    `from panel.pane import HTML as Raw` gives `{"Raw": "HTML"}`, so a checker that knows
    the class names Panel defines still recognises `Raw(...)` as one of them. Only imports
    rooted at `panel` are recorded: `from pandas import DataFrame` must not make every
    `DataFrame(...)` in the dashboard look like a markup pane.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "panel":
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _attribute_path(node: ast.expr) -> list[str]:
    """The dotted path of an attribute chain, outermost last: `pn.pane.HTML` → [pn, pane, HTML]."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def constructor_call_sites(source: str, class_names: Iterable[str]) -> list[tuple[int, str]]:
    """Every call in `source` constructing one of Panel's `class_names`, as (line, name).

    Recognises the spellings one construction can take, which is the whole reason this is
    AST and not a line regex: `pn.pane.HTML(x)`, `panel.pane.HTML(x)`, `pane.HTML(x)`,
    `HTML(x)` after `from panel.pane import HTML`, `Raw(x)` after an aliased import, and any
    of them split over several lines. The 2026-07-25 regex version saw only the first,
    spelled exactly.

    The pane's *origin* is resolved, not just its name. An attribute chain qualifies only
    when the segment before the class is `pane`, and a bare name only when it was imported
    from `panel`. Without that, `pd.DataFrame(...)` — which the dashboard builds four times —
    reads as a markup pane, and a rule with four standing exceptions on its first run is one
    nobody trusts.
    """
    wanted = set(class_names)
    aliases = _panel_pane_aliases(source)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            path = _attribute_path(node.func)
            if len(path) >= 2 and path[-1] in wanted and path[-2] == "pane":
                sites.append((node.lineno, path[-1]))
        elif isinstance(node.func, ast.Name):
            resolved = aliases.get(node.func.id, "")
            if resolved in wanted:
                sites.append((node.lineno, resolved))
    return sorted(sites)


_NOTIFICATION_LEVELS = frozenset({"info", "warning", "error", "success"})


def notification_call_sites(source: str) -> list[tuple[int, str]]:
    """Every `<something-notifications>.<level>(...)` call, as (line, level).

    Panel hands a notification's `message` to notyf unchanged and notyf assigns it with
    `innerHTML`, with no `html_decode` step — so a direct call is an execution sink for any
    IBKR, tool or exception text it interpolates. `safe_toast` is the one route, and it
    reaches the level through `getattr`, which this checker deliberately does not see.

    Matched on the receiver's *name* containing "notification" rather than on an import,
    because the object has none: it is reached as `pn.state.notifications` — an attribute
    chain — and usually bound to a local first. Both spellings are recognised.
    """
    sites: list[tuple[int, str]] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _NOTIFICATION_LEVELS:
            continue
        receiver = node.func.value
        name = (
            receiver.id
            if isinstance(receiver, ast.Name)
            else receiver.attr
            if isinstance(receiver, ast.Attribute)
            else ""
        )
        if "notification" in name.lower():
            sites.append((node.lineno, node.func.attr))
    return sorted(sites)
