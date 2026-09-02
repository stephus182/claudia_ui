"""UI customisation knobs that are not part of any one widget: the session theme, the
human's display name, and ClaudIA's avatar.

Phase 1 of the UI customisation track (2026-09-02). Everything here is a documented Panel
mechanism, no CSS — see ``docs/panel/ui-customisation-reference.md`` for the research each
choice rests on and for how to change it.

Why the theme is set **per session** rather than on ``pn.extension(theme=...)``: Panel's
``config.theme`` getter reads the session-scoped value first, then the global one, and only
then the ``?theme=`` URL argument (``panel/config.py``, ``_config.theme``, panel 1.9.3). A
global default would therefore silence the URL override. Setting ``pn.config.theme`` while a
session Document is current writes the session-scoped slot (``theme`` is not in
``_config._globals``), which is exactly what lets ``CLAUDIA_THEME`` be the default and
``?theme=`` win.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import panel as pn

log = logging.getLogger(__name__)

THEMES: tuple[str, ...] = ("default", "dark")
"""Panel's two built-in themes — the accepted values of ``CLAUDIA_THEME`` and ``?theme=``."""

CLAUDIA_AVATAR_PATH = Path(__file__).parent / "assets" / "claudia-avatar.png"
"""Where ClaudIA's chat avatar is expected. Absent → Panel's letter fallback, with a warning."""

AVATAR_SIZE_WARN_BYTES = 200 * 1024
"""A local avatar path is base64-embedded in *every* message model (panel/chat/utils.py
``build_avatar_pane`` → ``pn.pane.Image``), so a large file is paid on each message."""


def _normalise(value: str | None) -> str | None:
    """Lower-case and strip; blank means unset."""
    if value is None:
        return None
    value = value.strip().lower()
    return value or None


def resolve_theme(env_value: str | None, session_args: Mapping[str, Any] | None) -> str:
    """Pick the session theme. Precedence: ``?theme=`` URL argument → ``CLAUDIA_THEME`` →
    ``"default"``.

    Args:
        env_value: The raw ``CLAUDIA_THEME`` value, or ``None``.
        session_args: ``pn.state.session_args`` — Tornado's shape, each value a list of
            ``bytes`` (``panel/config.py`` decodes ``[0]`` the same way). ``None`` or empty
            outside a served session.

    An unrecognised value at either level is logged once and skipped, so a typo in the URL
    still lands on the configured default rather than on light.
    """
    url_raw = None
    if session_args:
        values = session_args.get("theme")
        if values:
            first = values[0]
            url_raw = first.decode("utf-8") if isinstance(first, bytes) else str(first)
    for source, raw in (("?theme", url_raw), ("CLAUDIA_THEME", env_value)):
        candidate = _normalise(raw)
        if candidate is None:
            continue
        if candidate in THEMES:
            return candidate
        log.warning(
            "Ignoring %s=%r — not one of %s", source, raw, "/".join(THEMES)
        )
    return "default"


def apply_session_theme(session_args: Mapping[str, Any] | None = None) -> str:
    """Resolve the theme for the current session and set it on ``pn.config``.

    Call from the per-session factory, while the session Document is current, so the value
    lands in Panel's session-scoped config (see the module docstring). Returns the theme.

    Args:
        session_args: Override for ``pn.state.session_args`` (tests); default reads Panel's.
    """
    if session_args is None:
        session_args = pn.state.session_args
    theme = resolve_theme(os.environ.get("CLAUDIA_THEME"), session_args)
    # `theme` is a read-only property in Panel's typing, but `_config.__setattr__`
    # (panel/config.py:436-462) intercepts the assignment and stores it in the session
    # slot when a Document is current — asserted at runtime by tests/test_panel_theme.py.
    pn.config.theme = theme  # type: ignore[misc]
    return theme


def user_display_name() -> str:
    """The author label on the human's messages: ``CLAUDIA_USER_NAME``, else ``"User"``."""
    name = os.environ.get("CLAUDIA_USER_NAME", "").strip()
    return name or "User"


def register_claudia_avatar(path: Path = CLAUDIA_AVATAR_PATH) -> bool:
    """Make every message authored ``"ClaudIA"`` carry the image at ``path``.

    Uses the documented in-place update of ``ChatMessage.default_avatars`` (keys are matched
    alphanumerically and case-insensitively, so ``"claudia"`` covers ``user="ClaudIA"``). This
    reaches every send site — the sink, the opening messages, proposals — without any of them
    passing ``avatar=``. Returns ``False`` and leaves Panel's letter fallback in place when the
    file is missing.
    """
    if not path.is_file():
        log.warning("ClaudIA avatar not found at %s — using Panel's letter fallback", path)
        return False
    size = path.stat().st_size
    if size > AVATAR_SIZE_WARN_BYTES:
        log.warning(
            "ClaudIA avatar %s is %d KB; it is embedded in every message — resize it "
            "(e.g. `sips -Z 128 <src> --out %s`)",
            path, size // 1024, path,
        )
    pn.chat.ChatMessage.default_avatars["claudia"] = str(path)
    return True
