"""Tests for claudia.panel_system_log — the collapsed System log card, terminal-style."""

from __future__ import annotations

from unittest.mock import MagicMock

import panel as pn

from claudia.panel_system_log import SystemLog, format_line


def test_card_starts_collapsed_with_a_bare_title():
    """A fresh log is a collapsible, collapsed Card titled 'System log' with no count."""
    log = SystemLog()
    assert isinstance(log.card, pn.Card)
    assert log.card.collapsed is True
    assert log.card.collapsible is True
    assert log.card.title == "System log"


def test_body_is_a_scrolling_column_of_monospace_lines():
    """Terminal style (2026-09-04): a bounded, scrolling Column that follows the newest line;
    each event is one `pn.pane.Str` — a raw string, no Markdown, no HTML."""
    log = SystemLog()
    assert isinstance(log.lines, pn.Column)
    assert log.lines.scroll is True
    assert log.lines.view_latest is True
    assert log.lines.height == 240
    assert log.lines in log.card.objects
    log.say("hello")
    assert len(log.lines.objects) == 1
    assert type(log.lines.objects[0]) is pn.pane.Str
    # A <pre> does not wrap; a long gateway detail must wrap at the card's edge, not clip.
    # The rule has to reach the <pre> element itself, hence a component stylesheet.
    assert any("pre-wrap" in css for css in log.lines.objects[0].stylesheets)


def test_say_keeps_the_raw_text_and_counts_in_the_title():
    """Each say() keeps the raw text in `entries` and bumps the count in the card title."""
    log = SystemLog()
    log.say("first")
    log.say("second", "warning")
    assert log.entries == ["first", "second"]
    assert log.card.title == "System log (2)"


def test_format_line_is_timestamp_level_text_with_markdown_dropped():
    """`HH:MM:SS  [WARN  |ERR   ]text`; `**`, backticks and fences go, newlines collapse."""
    assert format_line("**Session ended.** 3 saved\n\nSafe to close.", "info", "10:02:08") == (
        "10:02:08  Session ended. 3 saved Safe to close."
    )
    assert format_line("run `x`", "warning", "10:02:08") == "10:02:08  WARN  run x"
    assert format_line("boom", "error", "10:02:08") == "10:02:08  ERR   boom"


def test_lines_are_rendered_as_raw_strings_not_html():
    """Security (2026-07-25 audit H-1): tool results and exception text land here verbatim.
    `Str` escapes every character of its object, so a payload renders as text."""
    from bokeh.document import Document

    log = SystemLog()
    log.say("<img src=x onerror=alert(1)>")
    model = log.lines.objects[0].get_root(Document())
    assert "&lt;img" in model.text
    assert "<img" not in model.text


def test_every_chat_feed_in_the_package_installs_the_safe_renderer():
    """Structural guard: any ChatFeed/ChatInterface constructed in claudia/*.py must pass
    renderers=[safe_markdown] — the chat had it, the (former) log feed did not, and only a
    per-widget test caught the gap. Control the class, not the instance."""
    import re
    from pathlib import Path

    misses = []
    for path in sorted(Path("claudia").glob("*.py")):
        src = path.read_text()
        for m in re.finditer(r"pn\.chat\.(ChatFeed|ChatInterface)\(", src):
            window = src[m.end():m.end() + 800]
            if "renderers=[safe_markdown]" not in window:
                misses.append(f"{path}:{src[:m.start()].count(chr(10)) + 1}")
    assert not misses, f"ChatFeed/ChatInterface without renderers=[safe_markdown]: {misses}"


def test_warning_and_error_toast_when_notifications_exist(monkeypatch):
    """warning/error levels raise a toast of the matching kind; info never toasts."""
    notifications = MagicMock()
    # `pn.state.notifications` is a read-only property, so the patch goes on the class
    # (same idiom as tests/test_panel_dashboard.py).
    monkeypatch.setattr(type(pn.state), "notifications", notifications, raising=False)
    log = SystemLog()
    log.say("plain")
    log.say("careful", "warning")
    log.say("broken", "error")
    notifications.warning.assert_called_once()
    assert notifications.warning.call_args.args[0] == "careful"
    notifications.error.assert_called_once()
    assert notifications.error.call_args.args[0] == "broken"
    notifications.info.assert_not_called()
    notifications.success.assert_not_called()


def test_say_never_raises_without_a_served_session(monkeypatch):
    """pn.state.notifications is None outside pn.serve — the line is still written."""
    monkeypatch.setattr(type(pn.state), "notifications", None, raising=False)
    log = SystemLog()
    log.say("x", "error")
    assert log.entries == ["x"]


def test_toast_area_is_captured_at_construction_for_the_session_that_built_it(monkeypatch):
    """Review #5: the listener's task keeps the FIRST session's document for the life of the
    process, so resolving `pn.state.notifications` at say-time would toast a dead tab after a
    reload. The area is captured when the log is built — inside the session factory — and
    used from then on."""
    at_build = MagicMock()
    monkeypatch.setattr(type(pn.state), "notifications", at_build, raising=False)
    log = SystemLog()
    later = MagicMock()
    monkeypatch.setattr(type(pn.state), "notifications", later, raising=False)
    log.say("lost the feed", "warning")
    at_build.warning.assert_called_once()
    later.warning.assert_not_called()
