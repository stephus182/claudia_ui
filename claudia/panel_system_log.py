"""System log — the collapsed card that holds a session's own events.

What happens *to* the session (connectivity changes, Flex sync, document reloads, gateway
and TradingView progress, session save/end) lands here instead of in the conversation.
What happens *in* the conversation — tool steps, a truncated reply, an upload rejected —
stays in the chat, because it answers something the user just did. The rule and the
inventory behind it: `docs/panel/ui-customisation-reference.md` § System log and action bar.

Panel-only: no IBKR, no SQL, no `panel_app` import (same rule as the dashboard modules).
"""

from __future__ import annotations

from typing import Literal

import panel as pn

Level = Literal["info", "warning", "error"]

# Warnings stay 8s; errors stay until dismissed — a failed sync or a lost gateway is the
# kind of thing a trader should have to acknowledge, an info line is not.
_TOAST_DURATION_MS: dict[str, int] = {"warning": 8000, "error": 0}

# Fixed so the feed scrolls inside the card instead of growing the page: the left
# column's pinned sizing (08d5654) needs everything below the chat to be fixed-height.
_FEED_HEIGHT = 240


class SystemLog:
    """A collapsed `pn.Card` whose body is a read-only, timestamped `ChatFeed`.

    `say()` is the single route for session-level messages. It appends to the feed, bumps
    the count shown in the card title (so a collapsed card still says how much is inside),
    and raises a toast for `warning`/`error` so an event is noticed while the user is
    reading the chat. The toast is a courtesy; the feed entry is the record.
    """

    def __init__(self, title: str = "System log") -> None:
        """Build the card collapsed and empty.

        `ChatFeed`, not `ChatInterface`: no input row, nothing to type into. Reaction and
        copy icons are off for the same reason they are off in the chat (phase 1).
        """
        self._title = title
        self._count = 0
        self.feed = pn.chat.ChatFeed(
            message_params={"show_reaction_icons": False, "show_copy_icon": False},
            show_activity_dot=False,
            height=_FEED_HEIGHT,
            sizing_mode="stretch_width",
        )
        self.card = pn.Card(
            self.feed,
            title=title,
            collapsed=True,
            collapsible=True,
            sizing_mode="stretch_width",
        )

    def say(self, text: str, level: Level = "info") -> None:
        """Record one event. Never raises: a missing notifications object is skipped.

        `pn.state.notifications` is None outside a served session (and in every test), so
        the toast is guarded exactly as `panel_dashboard` guards its stale-data toast.
        """
        self.feed.send(text, user="System", respond=False)
        self._count += 1
        self.card.title = f"{self._title} ({self._count})"
        if level == "info":
            return
        notifications = pn.state.notifications
        if notifications is None:
            return
        duration = _TOAST_DURATION_MS[level]
        if level == "warning":
            notifications.warning(text, duration=duration)
        else:
            notifications.error(text, duration=duration)
