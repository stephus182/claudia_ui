"""Tests for claudia.panel_system_log — the collapsed System log card."""

from __future__ import annotations

from unittest.mock import MagicMock

import panel as pn

from claudia.panel_system_log import SystemLog


def test_card_starts_collapsed_with_a_bare_title():
    """A fresh log is a collapsible, collapsed Card titled 'System log' with no count."""
    log = SystemLog()
    assert isinstance(log.card, pn.Card)
    assert log.card.collapsed is True
    assert log.card.collapsible is True
    assert log.card.title == "System log"


def test_feed_is_read_only_and_chrome_free():
    """The feed is a ChatFeed (no input row) with reaction and copy icons off."""
    log = SystemLog()
    assert type(log.feed) is pn.chat.ChatFeed
    assert log.feed.message_params["show_reaction_icons"] is False
    assert log.feed.message_params["show_copy_icon"] is False
    assert log.feed in log.card.objects


def test_say_appends_a_system_message_and_counts_in_the_title():
    """Each say() adds one 'System' message and bumps the count in the card title."""
    log = SystemLog()
    log.say("first")
    log.say("second", "warning")
    assert [m.object for m in log.feed.objects] == ["first", "second"]
    assert all(m.user == "System" for m in log.feed.objects)
    assert log.card.title == "System log (2)"


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
    """pn.state.notifications is None outside pn.serve — the log entry is still written."""
    monkeypatch.setattr(type(pn.state), "notifications", None, raising=False)
    log = SystemLog()
    log.say("x", "error")
    assert len(log.feed.objects) == 1


def test_feed_installs_the_safe_markdown_renderer():
    """Security (2026-07-25 audit H-1, re-found by review 2026-09-04): the log feed shows
    tool results, gateway details and exception text verbatim, so it needs the same
    HTML-off renderer as the chat — Panel's default Markdown pane renders raw HTML."""
    from claudia.panel_markdown import safe_markdown

    log = SystemLog()
    assert safe_markdown in log.feed.renderers
    log.say("<img src=x onerror=alert(1)>")
    pane = log.feed.objects[0]._object_panel
    assert type(pane).__name__ == "Markdown"
    assert pane.renderer_options == {"html": False}


def test_every_chat_feed_in_the_package_installs_the_safe_renderer():
    """Structural guard: any ChatFeed/ChatInterface constructed in claudia/*.py must pass
    renderers=[safe_markdown] — the chat had it, the log did not, and only a per-widget
    test caught the gap. Control the class, not the instance."""
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
