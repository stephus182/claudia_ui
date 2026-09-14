"""CLA-SEC-006 — untrusted content cannot execute in the UI, structurally.

The canaries in `test_security_regressions.py` prove the four helpers in `panel_markdown`
neutralise a payload on the surfaces that carry untrusted text *today*. They cannot see a
fifth surface written next week. The audit recorded that as § B-8: no structural allowlist
of construction sites, so a new `pn.pane.HTML(tool_result)` in a new module would be
correctly typed, correctly linted, fully green and an execution sink.

A line-regex version of this rule has existed since 2026-07-25
(`test_no_unguarded_markdown_panes_in_package`). It matches the literal text
``pn.pane.(Markdown|HTML|Str)(`` in `claudia/*.py` and is blind to every other spelling of
the same construction — `from panel.pane import HTML`, an aliased import, a call split over
two lines, `pn.pane.DataFrame`, `pn.pane.Alert`, `scripts/`, a subpackage. This file is the
AST version of the same rule, and the regex one stays as the cheap first line.

**The banned class is computed from Panel, not typed here.** Every subclass of
`panel.pane.markup.HTMLBasePane` whose `object` is *text it turns into markup* — the
`panel.pane.markup` module plus `Alert` (a `Markdown` subclass) and `SVG` (markup in a file's
clothing). The image and plot panes take bytes, a path or a figure, and banning them would
be a rule someone deletes rather than obeys. A pane Panel adds to `panel.pane.markup`
tomorrow is covered the day it ships.

**What this rule does not cover, and what does.** Three markup paths in this UI are not a
named pane constructor: `pn.chat.ChatStep` builds Markdown panes of its own (covered by
`escape_markup` and the ChatStep canary), `ChatInterface.send` renders strings through the
feed-level `renderers=[safe_markdown]` hook (covered by that hook and its canary), and
`pn.state.notifications` assigns with `innerHTML` (covered by `safe_toast` and the rule
below that nothing calls a notification level directly). They are named here so the next
reader knows the allowlist is not claiming to be the whole of CLA-SEC-006.
"""

from __future__ import annotations

import inspect

import pytest

from tests.security.structural import (
    constructor_call_sites,
    notification_call_sites,
    package_sources,
)

# Construction sites outside `panel_markdown.py` that a human has read and accepted. Each
# entry is (module filename, pane class, why it is safe). Empty today, and that is the
# honest state: every markup pane in this package is built by one of the four helpers.
# An entry here is a decision someone makes in review, not a diff nobody reads.
REVIEWED_EXCEPTIONS: dict[tuple[str, str], str] = {}

# The one module allowed to construct any of them: it is where the escaping lives.
_SANCTIONED_MODULE = "panel_markdown.py"


def markup_pane_names() -> set[str]:
    """Every Panel pane that turns *text* into markup, read from the installed Panel."""
    import panel.pane as pane_module
    from panel.pane.markup import HTMLBasePane

    names = {
        name
        for name in dir(pane_module)
        if inspect.isclass(cls := getattr(pane_module, name))
        and issubclass(cls, HTMLBasePane)
        and cls.__module__ in ("panel.pane.markup", "panel.pane.alert")
    }
    # SVG lives in `panel.pane.image` because it is loaded like an image, but its payload is
    # markup and it is the one image pane that can carry a `<script>`.
    return names | {"SVG"}


def test_the_banned_class_is_not_empty_and_contains_the_panes_that_bit():
    """Guards the guard: if Panel's class layout moves, the rule must not quietly empty."""
    names = markup_pane_names()
    assert {"Markdown", "HTML", "Str", "DataFrame", "Alert", "SVG"} <= names, (
        f"the markup-pane class no longer covers the known sinks: {sorted(names)}"
    )


def test_markup_panes_are_constructed_only_through_the_sanctioned_helpers():
    """No module builds a text-to-markup pane except `panel_markdown` and reviewed sites."""
    banned = markup_pane_names()
    offenders = []
    for path, source in package_sources():
        if path.name == _SANCTIONED_MODULE:
            continue
        for line, pane in constructor_call_sites(source, banned):
            if (path.name, pane) in REVIEWED_EXCEPTIONS:
                continue
            offenders.append(f"{path.name}:{line}: {pane}(...)")
    assert not offenders, (
        "markup panes built outside panel_markdown.py — route them through safe_markdown / "
        "safe_text, or add a reviewed exception with a reason:\n" + "\n".join(offenders)
    )


def test_no_reviewed_exception_has_gone_stale():
    """An exception for a site that no longer exists reads as coverage. Remove it."""
    banned = markup_pane_names()
    live = {
        (path.name, pane)
        for path, source in package_sources()
        if path.name != _SANCTIONED_MODULE
        for _line, pane in constructor_call_sites(source, banned)
    }
    stale = sorted(set(REVIEWED_EXCEPTIONS) - live)
    assert not stale, f"REVIEWED_EXCEPTIONS names sites that are gone: {stale}"


def test_no_module_overrides_the_renderer_options_that_close_the_injection():
    """`renderer_options={"html": False}` is the control; passing it again re-opens it.

    `safe_markdown`'s docstring asks callers not to override it. This is the rule behind
    the request: the keyword may be written in `panel_markdown.py` and nowhere else.
    """
    from tests.security.structural import keyword_arguments_used

    offenders = [
        path.name
        for path, source in package_sources()
        if path.name != _SANCTIONED_MODULE and "renderer_options" in keyword_arguments_used(source)
    ]
    assert not offenders, f"renderer_options set outside panel_markdown.py: {offenders}"


def test_notifications_are_raised_only_through_safe_toast():
    """notyf assigns the body with `innerHTML` and never decodes — one escape is the control.

    The toast path has no pane and therefore no constructor to ban, so the rule is on the
    call: a module may reach `pn.state.notifications` to test it for None, but the level
    methods belong to `safe_toast`, which escapes exactly once. Two escapes would show the
    user entities; none is an execution sink.
    """
    offenders = [
        f"{path.name}:{line}: notifications.{level}(...)"
        for path, source in package_sources()
        if path.name != _SANCTIONED_MODULE
        for line, level in notification_call_sites(source)
    ]
    assert not offenders, (
        "a toast was raised without safe_toast — its body reaches innerHTML unescaped:\n"
        + "\n".join(offenders)
    )


# ── Guard on the guard: the checker must fire on every spelling of the construction ──────


@pytest.mark.parametrize(
    "snippet",
    [
        "import panel as pn\npn.pane.HTML(tool_result)",
        "import panel as pn\npn.pane.Markdown(page_text)",
        "import panel as pn\npn.pane.Str(fill_line)",
        "import panel as pn\npn.pane.Alert(exception_text)",
        "import panel as pn\npn.pane.DataFrame(frame)",
        "from panel.pane import HTML\nHTML(tool_result)",
        "from panel.pane import HTML as Raw\nRaw(tool_result)",
        "from panel import pane\npane.Markdown(page_text)",
        "import panel as pn\npn.pane.HTML(\n    tool_result,\n    sizing_mode='stretch_width',\n)",
    ],
)
def test_the_checker_fires_on_each_way_of_writing_an_unguarded_pane(snippet):
    """A structural test that cannot fail reads as coverage — this is where that is proved."""
    assert constructor_call_sites(snippet, markup_pane_names()), (
        f"the checker missed an unguarded construction:\n{snippet}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "import panel as pn\npn.pane.Image(str(path))",
        "import panel as pn\npn.pane.HoloViews(layout)",
        "import panel as pn\npn.widgets.Button(label='Stage this order')",
        "import panel as pn\npn.indicators.Number(value=1.0)",
        "import panel as pn\npn.widgets.Tabulator(frame, disabled=True)",
        "import panel as pn\npn.Column(a, b)",
        "html = '<b>not a call</b>'",
        # The dashboard builds four of these. A rule that flags pandas is a rule with four
        # standing exceptions on its first run, which is a rule nobody trusts.
        "import pandas as pd\npd.DataFrame(rows)",
        "from pandas import DataFrame\nDataFrame(rows)",
    ],
)
def test_the_checker_leaves_ordinary_panel_objects_alone(snippet):
    """A rule that banned every widget would be deleted rather than obeyed."""
    assert not constructor_call_sites(snippet, markup_pane_names()), (
        f"the checker flagged a safe construction:\n{snippet}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "notifications.error(exception_text, duration=0)",
        "pn.state.notifications.success('done', duration=1)",
        "my_notifications.warning(ibkr_detail, duration=4000)",
    ],
)
def test_the_toast_checker_fires_on_a_direct_notification_call(snippet):
    """The same guard-on-the-guard as the panes: a rule that cannot fire is not a rule."""
    assert notification_call_sites(snippet), f"the checker missed a direct toast:\n{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "notifications = pn.state.notifications",
        "if notifications is None:\n    return",
        "log.error('something went wrong')",
        "getattr(notifications, level)(escape_markup(text), duration=duration)",
    ],
)
def test_the_toast_checker_leaves_the_sanctioned_route_and_plain_logging_alone(snippet):
    """`safe_toast` reaches the level through getattr, and a logger is not a toast."""
    assert not notification_call_sites(snippet), f"the checker over-fired:\n{snippet}"
