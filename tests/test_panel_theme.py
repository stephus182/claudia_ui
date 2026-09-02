"""claudia.panel_theme — session theme, user display name, ClaudIA's avatar.

Phase 1 of the UI customisation track (docs/panel/ui-customisation-reference.md).
"""

from __future__ import annotations

import base64
import logging

import panel as pn
import pytest

from claudia import panel_theme

# A valid 1x1 PNG — small enough to embed in every message, which is what Panel does
# with a local avatar path (panel/chat/utils.py build_avatar_pane → pn.pane.Image).
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def restore_default_avatars():
    """Panel documents `default_avatars` as modify-in-place, never replace — so the
    restore must also be in place."""
    saved = dict(pn.chat.ChatMessage.default_avatars)
    yield
    pn.chat.ChatMessage.default_avatars.clear()
    pn.chat.ChatMessage.default_avatars.update(saved)


@pytest.fixture
def restore_panel_theme():
    """Put pn.config.theme back — the applier writes the global slot when no Document is current."""
    old = pn.config.theme
    yield
    pn.config.theme = old


# ── resolve_theme ─────────────────────────────────────────────────────────────


def test_resolve_theme_defaults_to_light_when_nothing_is_set():
    """No env var, no URL argument → Panel's light theme."""
    assert panel_theme.resolve_theme(None, {}) == "default"


def test_resolve_theme_env_var_sets_the_default():
    """CLAUDIA_THEME alone decides."""
    assert panel_theme.resolve_theme("dark", {}) == "dark"


def test_resolve_theme_url_argument_alone_is_honoured():
    """?theme= alone decides; values arrive as Tornado byte lists."""
    assert panel_theme.resolve_theme(None, {"theme": [b"dark"]}) == "dark"


def test_resolve_theme_url_argument_overrides_env_var():
    """The per-tab URL argument wins over the process-wide default."""
    assert panel_theme.resolve_theme("dark", {"theme": [b"default"]}) == "default"


def test_resolve_theme_tolerates_missing_session_args():
    """Outside a served session `pn.state.session_args` is not a populated dict."""
    # Outside a served session pn.state.session_args is not a populated dict.
    assert panel_theme.resolve_theme("dark", None) == "dark"


def test_resolve_theme_is_case_and_whitespace_insensitive():
    """`Dark ` in .env is a human typing, not an error."""
    assert panel_theme.resolve_theme(" Dark ", {}) == "dark"


def test_resolve_theme_blank_env_var_means_unset():
    """An empty CLAUDIA_THEME= line behaves like no line at all."""
    assert panel_theme.resolve_theme("   ", {}) == "default"


def test_resolve_theme_invalid_env_var_warns_and_falls_back(caplog):
    """A bad value is named once in the log and light is used."""
    with caplog.at_level(logging.WARNING, logger="claudia.panel_theme"):
        assert panel_theme.resolve_theme("bogus", {}) == "default"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bogus" in warnings[0].getMessage()


def test_resolve_theme_invalid_url_argument_falls_back_to_env_default(caplog):
    """A typo in the URL lands on the configured default, not on light."""
    with caplog.at_level(logging.WARNING, logger="claudia.panel_theme"):
        assert panel_theme.resolve_theme("dark", {"theme": [b"bogus"]}) == "dark"
    assert any("bogus" in r.getMessage() for r in caplog.records)


# ── apply_session_theme ───────────────────────────────────────────────────────


def test_apply_session_theme_sets_panel_config(monkeypatch, restore_panel_theme):
    """The resolved theme is written to pn.config and returned."""
    monkeypatch.setenv("CLAUDIA_THEME", "dark")
    assert panel_theme.apply_session_theme(session_args={}) == "dark"
    assert pn.config.theme == "dark"


def test_apply_session_theme_url_wins_over_env(monkeypatch, restore_panel_theme):
    """Precedence holds through the applier, not just the resolver."""
    monkeypatch.setenv("CLAUDIA_THEME", "dark")
    assert panel_theme.apply_session_theme(session_args={"theme": [b"default"]}) == "default"
    assert pn.config.theme == "default"


# ── user_display_name ─────────────────────────────────────────────────────────


def test_user_display_name_defaults_to_user(monkeypatch):
    """Unset → Panel's own default label."""
    monkeypatch.delenv("CLAUDIA_USER_NAME", raising=False)
    assert panel_theme.user_display_name() == "User"


def test_user_display_name_is_stripped(monkeypatch):
    """Surrounding whitespace never reaches the author label."""
    monkeypatch.setenv("CLAUDIA_USER_NAME", "  Steph ")
    assert panel_theme.user_display_name() == "Steph"


def test_user_display_name_blank_means_unset(monkeypatch):
    """Whitespace-only is the same as unset."""
    monkeypatch.setenv("CLAUDIA_USER_NAME", "   ")
    assert panel_theme.user_display_name() == "User"


# ── register_claudia_avatar ───────────────────────────────────────────────────


def test_register_claudia_avatar_missing_file_warns_and_leaves_panel_default(
    tmp_path, caplog, restore_default_avatars
):
    """No file → False, one warning naming the path, and Panel's letter fallback untouched."""
    # Start clean: importing claudia.panel_app elsewhere in the run registers the real
    # asset, and a missing file must not *add* a key — it is not asked to remove one.
    pn.chat.ChatMessage.default_avatars.pop("claudia", None)
    missing = tmp_path / "nope.png"
    with caplog.at_level(logging.WARNING, logger="claudia.panel_theme"):
        assert panel_theme.register_claudia_avatar(missing) is False
    assert any(str(missing) in r.getMessage() for r in caplog.records)
    assert "claudia" not in pn.chat.ChatMessage.default_avatars
    # Panel's own fallback: first letter of the author name, resolved at render time.
    assert pn.chat.ChatMessage("hi", user="ClaudIA")._render_avatar().object == "C"


def test_register_claudia_avatar_makes_claudia_messages_carry_the_image(
    tmp_path, restore_default_avatars
):
    """The one registration reaches every `user=\"ClaudIA\"` send with no `avatar=` argument."""
    avatar = tmp_path / "claudia-avatar.png"
    avatar.write_bytes(_TINY_PNG)
    assert panel_theme.register_claudia_avatar(avatar) is True
    # Sent exactly the way panel_sink / panel_app send: user name, no avatar argument.
    msg = pn.chat.ChatMessage("hi", user="ClaudIA")
    assert msg.avatar == str(avatar)
    assert isinstance(msg._render_avatar(), pn.pane.Image)


def test_register_claudia_avatar_does_not_touch_other_authors(tmp_path, restore_default_avatars):
    """System keeps its gear and a human keeps the letter fallback."""
    avatar = tmp_path / "claudia-avatar.png"
    avatar.write_bytes(_TINY_PNG)
    panel_theme.register_claudia_avatar(avatar)
    assert pn.chat.ChatMessage("hi", user="System").avatar == "⚙️"
    assert pn.chat.ChatMessage("hi", user="Steph")._render_avatar().object == "S"


def test_register_claudia_avatar_warns_when_the_file_is_large(
    tmp_path, caplog, restore_default_avatars
):
    """The path is base64-embedded in every message model — a big file is paid per message."""
    avatar = tmp_path / "claudia-avatar.png"
    avatar.write_bytes(_TINY_PNG + b"\0" * (panel_theme.AVATAR_SIZE_WARN_BYTES + 1))
    with caplog.at_level(logging.WARNING, logger="claudia.panel_theme"):
        assert panel_theme.register_claudia_avatar(avatar) is True
    assert any("KB" in r.getMessage() for r in caplog.records)


def test_default_avatar_path_is_inside_the_package_assets_dir():
    """The expected asset location is fixed and next to claudia-logo.png."""
    assert panel_theme.CLAUDIA_AVATAR_PATH.parent.name == "assets"
    assert panel_theme.CLAUDIA_AVATAR_PATH.name == "claudia-avatar.png"


# ── intro_card ────────────────────────────────────────────────────────────────


def test_intro_card_is_the_portrait_over_the_text_when_the_asset_exists(tmp_path):
    """The opening bubble carries the standing portrait above the ready text."""
    portrait = tmp_path / "claudia-standing.jpg"
    portrait.write_bytes(_TINY_PNG)
    card = panel_theme.intro_card("**ClaudIA is ready**", path=portrait)
    assert isinstance(card, pn.Column)
    image, text = card.objects
    assert isinstance(image, pn.pane.Image)
    assert image.object == str(portrait)
    assert image.height == panel_theme.INTRO_HEIGHT
    assert isinstance(text, pn.pane.Markdown)
    assert text.object == "**ClaudIA is ready**"


def test_intro_card_falls_back_to_plain_text_without_the_asset(tmp_path, caplog):
    """No portrait → the plain string, so the feed's own renderer handles it; one warning."""
    with caplog.at_level(logging.WARNING, logger="claudia.panel_theme"):
        card = panel_theme.intro_card("**ClaudIA is ready**", path=tmp_path / "nope.png")
    assert card == "**ClaudIA is ready**"
    assert any("nope.png" in r.getMessage() for r in caplog.records)


def test_intro_asset_path_is_inside_the_package_assets_dir():
    """Same home as the avatar and the logo."""
    assert panel_theme.CLAUDIA_INTRO_PATH.parent.name == "assets"
    assert panel_theme.CLAUDIA_INTRO_PATH.name == "claudia-standing.jpg"
