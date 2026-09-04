"""System log — the collapsed card that holds a session's own events, one line each.

What happens *to* the session (connectivity changes, Flex sync, document reloads, gateway
and TradingView progress, session save/end) lands here instead of in the conversation.
What happens *in* the conversation — tool steps, a truncated reply, an upload rejected —
stays in the chat, because it answers something the user just did. The rule and the
inventory behind it: `docs/panel/ui-customisation-reference.md` § System log and action bar.

The body is terminal-style (user request 2026-09-04, "more like a terminal than a chat"):
a scrolling `pn.Column` of one monospace `pn.pane.Str` per event, `HH:MM:SS  [LEVEL]  text`,
newest at the bottom and scrolled into view. It replaced a `ChatFeed` of bubbles the same
day. Each line is `panel_markdown.safe_text` — a `Str` pane, which renders its object as a
raw string, every character escaped, no Markdown, no HTML — so tool results and exception
text are safe here by construction (the ChatFeed before it needed `renderers=[safe_markdown]`
for the same guarantee), and the one sanctioned construction site for markup panes stays one.

Panel-only: no IBKR, no SQL, no `panel_app` import (same rule as the dashboard modules).
"""

from __future__ import annotations

import re
import time
from typing import Literal

import panel as pn

from claudia.panel_markdown import safe_text

Level = Literal["info", "warning", "error"]

# Warnings stay 8s; errors stay until dismissed — a failed sync or a lost gateway is the
# kind of thing a trader should have to acknowledge, an info line is not.
_TOAST_DURATION_MS: dict[str, int] = {"warning": 8000, "error": 0}

# Fixed so the lines scroll inside the card instead of growing the page: the left
# column's pinned sizing (08d5654) needs everything below the chat to be fixed-height.
_FEED_HEIGHT = 240

# Wrap long lines at the card's width and drop the <pre>'s default outer margin.
_LINE_CSS = "pre { white-space: pre-wrap; margin: 0; }"

# The level column, blank for info so ordinary lines stay short.
_LEVEL_TAG: dict[str, str] = {"info": "", "warning": "WARN  ", "error": "ERR   "}

_MARKDOWN_MARKS = re.compile(r"\*\*|`{1,3}")
_WHITESPACE = re.compile(r"\s+")


def format_line(text: str, level: Level, clock: str) -> str:
    """One terminal line: `HH:MM:SS  [WARN  |ERR   ]text`, Markdown marks dropped, newlines
    collapsed. The messages are authored for the chat's Markdown renderer (`**bold**`,
    fenced commands); in a monospace log the marks are noise, so they go and the words stay.
    """
    clean = _WHITESPACE.sub(" ", _MARKDOWN_MARKS.sub("", text)).strip()
    return f"{clock}  {_LEVEL_TAG[level]}{clean}"


class SystemLog:
    """A collapsed `pn.Card` whose body is a scrolling column of monospace lines.

    `say()` is the single route for session-level messages. It appends a line, keeps the
    raw text in `entries` (what tests and readers assert on), bumps the count shown in the
    card title (so a collapsed card still says how much is inside), and raises a toast for
    `warning`/`error` so an event is noticed while the user is reading the chat. The toast
    is a courtesy; the line is the record.
    """

    def __init__(self, title: str = "System log") -> None:
        """Build the card collapsed and empty."""
        self._title = title
        self.entries: list[str] = []
        # Captured now, inside the session factory where `pn.state.curdoc` is this session's
        # document, and used by `say()` from then on. Resolving it at say-time would be wrong
        # for a message delivered by a process-wide task (the execution listener, the
        # connectivity checker): that task keeps the document of whichever session started
        # it, so after a tab reload its toasts would target a dead page (review 2026-09-04).
        self._notifications = pn.state.notifications
        # view_latest scrolls to the newest object on every append — the chat feed's
        # auto-scroll, for a plain column.
        self.lines = pn.Column(
            scroll=True,
            view_latest=True,
            height=_FEED_HEIGHT,
            sizing_mode="stretch_width",
        )
        self.card = pn.Card(
            self.lines,
            title=title,
            collapsed=True,
            collapsible=True,
            sizing_mode="stretch_width",
        )

    def say(self, text: str, level: Level = "info") -> None:
        """Record one event. Never raises: a missing notifications object is skipped.

        The notifications area was captured at construction (see `__init__`); it is None
        outside a served session (and in every test), so the toast is guarded exactly as
        `panel_dashboard` guards its stale-data toast.
        """
        self.entries.append(text)
        line = format_line(text, level, time.strftime("%H:%M:%S"))
        # `Str` renders a <pre>, which does not wrap: a long gateway detail would be
        # clipped at the card's edge. The rule must reach the <pre> itself — `styles=`
        # lands on the pane's wrapper and the browser's `pre { white-space: pre }` wins —
        # so it goes through the component's own `stylesheets` (scoped to its shadow DOM).
        self.lines.append(safe_text(line, margin=(0, 6), stylesheets=[_LINE_CSS]))
        self.card.title = f"{self._title} ({len(self.entries)})"
        if level == "info":
            return
        notifications = self._notifications
        if notifications is None:
            return
        duration = _TOAST_DURATION_MS[level]
        if level == "warning":
            notifications.warning(text, duration=duration)
        else:
            notifications.error(text, duration=duration)
