"""Tests for ContextLoader — document loading, hashing, watchdog."""

import re
import time
from unittest.mock import MagicMock, patch

import pytest

from claudia.context_loader import ContextLoader


@pytest.fixture
def docs_dir(tmp_path):
    """A docs directory holding a minimal context.md / principles.md pair."""
    (tmp_path / "context.md").write_text("# Role\nI am ClaudIA.")
    (tmp_path / "principles.md").write_text("# Principles\n- Risk first.")
    return tmp_path


def test_load_system_prompt(docs_dir):
    """Both documents reach the prompt under their own labelled sections."""
    loader = ContextLoader(docs_dir)
    prompt = loader.load_system_prompt()
    assert "ClaudIA" in prompt
    assert "Risk first" in prompt
    assert "ROLE & CONTEXT" in prompt
    assert "TRADING PRINCIPLES" in prompt


def test_compute_hash_is_deterministic(docs_dir):
    """The same content hashes to the same 64-hex SHA-256 every call."""
    loader = ContextLoader(docs_dir)
    h1 = loader.compute_hash()
    h2 = loader.compute_hash()
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_changes_on_file_edit(docs_dir):
    """Editing either document moves the hash — that is what triggers a new version."""
    loader = ContextLoader(docs_dir)
    h1 = loader.compute_hash()
    (docs_dir / "principles.md").write_text("# Principles\n- Updated rule.")
    h2 = loader.compute_hash()
    assert h1 != h2


def test_missing_context_raises(tmp_path):
    """A missing context.md fails loudly and names the file, rather than loading a half prompt."""
    (tmp_path / "principles.md").write_text("# Principles")
    loader = ContextLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match=re.escape("context.md")):
        loader.load_system_prompt()


def test_missing_principles_raises(tmp_path):
    """A missing principles.md fails loudly and names the file."""
    (tmp_path / "context.md").write_text("# Role")
    loader = ContextLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match=re.escape("principles.md")):
        loader.load_system_prompt()


def test_watchdog_fires_callback(docs_dir):
    """Editing a watched document fires the reload callback with the filename."""
    loader = ContextLoader(docs_dir)
    fired = []

    def on_reload(filename, new_prompt):
        """Record which file the watchdog reported."""
        fired.append(filename)

    loader.start_watching(on_reload)
    try:
        # Modify a watched file
        (docs_dir / "principles.md").write_text("# Principles\n- New rule added.")
        time.sleep(1.5)  # give watchdog time to detect the change
    finally:
        loader.stop_watching()

    assert fired
    assert any("principles.md" in f or "context.md" in f for f in fired)


def test_context_text_override_used_instead_of_file(docs_dir):
    """A Drive-supplied context replaces the local file entirely, rather than appending to it."""
    loader = ContextLoader(docs_dir, context_text="# Drive Context\nDrive role.")
    prompt = loader.load_system_prompt()
    assert "Drive Context" in prompt
    assert "Drive role" in prompt
    # Local file content should NOT appear
    assert "I am ClaudIA" not in prompt


def test_principles_text_override_used_instead_of_file(docs_dir):
    """A Drive-supplied principles document replaces the local file entirely."""
    loader = ContextLoader(docs_dir, principles_text="# Drive Principles\n- Drive rule.")
    prompt = loader.load_system_prompt()
    assert "Drive Principles" in prompt
    assert "Drive rule" in prompt
    assert "Risk first" not in prompt


def test_both_overrides_no_local_files_needed(tmp_path):
    # When both overrides are provided, local files are not read at all
    """With both documents supplied, no local file is read — a Drive-only setup needs none."""
    loader = ContextLoader(tmp_path, context_text="Context text", principles_text="Principles text")
    prompt = loader.load_system_prompt()
    assert "Context text" in prompt
    assert "Principles text" in prompt


def test_compute_hash_reflects_override_text(docs_dir):
    """The hash covers the effective text, so a Drive change registers as a new version."""
    loader_local = ContextLoader(docs_dir)
    loader_drive = ContextLoader(docs_dir, context_text="# Different Drive context")
    assert loader_local.compute_hash() != loader_drive.compute_hash()


def test_get_effective_texts_returns_file_content(docs_dir):
    """Without overrides the effective texts are the files' contents."""
    loader = ContextLoader(docs_dir)
    ctx, pri = loader.get_effective_texts()
    assert "ClaudIA" in ctx
    assert "Risk first" in pri


def test_get_effective_texts_returns_overrides(docs_dir):
    """With overrides the effective texts are the supplied strings, verbatim."""
    loader = ContextLoader(docs_dir, context_text="override ctx", principles_text="override pri")
    ctx, pri = loader.get_effective_texts()
    assert ctx == "override ctx"
    assert pri == "override pri"


def test_compute_hash_stable_across_drive_and_local_sources(docs_dir):
    # Drive content with surrounding whitespace must hash the same as the
    # equivalent local file (which _read_required always strips). Prevents
    # spurious security alerts when switching between Drive and local sources.
    """Whitespace-only differences hash identically, so switching Drive/local raises no alert."""
    local_content = (docs_dir / "context.md").read_text()
    loader_local = ContextLoader(docs_dir)
    loader_drive = ContextLoader(docs_dir, context_text=f"\n{local_content}\n")
    assert loader_local.compute_hash() == loader_drive.compute_hash()


def test_file_change_clears_context_override(docs_dir):
    """A local edit drops the Drive override — the file just edited is what takes effect."""
    loader = ContextLoader(docs_dir, context_text="# Drive Context\nDrive role.")
    fired_prompts = []

    def on_reload(filename, new_prompt):
        """Record the rebuilt prompt so the test can assert the override was dropped."""
        fired_prompts.append(new_prompt)

    loader.start_watching(on_reload)
    try:
        (docs_dir / "context.md").write_text("# Local Context\nNew local role.")
        time.sleep(1.5)
    finally:
        loader.stop_watching()

    assert fired_prompts
    assert "Local Context" in fired_prompts[-1]
    assert "Drive Context" not in fired_prompts[-1]


def test_stop_watching_removes_only_this_loaders_handler(tmp_path):
    """watchdog's unschedule() deletes ALL handlers for the (path, recursive)
    key — one session's teardown would kill every other session's hot-reload
    (probe-confirmed on watchdog 6.0.0, see migration plan D7 notes).
    stop_watching must use remove_handler_for_watch instead."""
    (tmp_path / "context.md").write_text("c")
    (tmp_path / "principles.md").write_text("p")
    loader = ContextLoader(tmp_path)
    mock_obs = MagicMock()
    with patch("claudia.context_loader._get_shared_observer", return_value=mock_obs):
        loader.start_watching(lambda f, p: None)
        watch = mock_obs.schedule.return_value
        handler = mock_obs.schedule.call_args.args[0]
        loader.stop_watching()
    mock_obs.remove_handler_for_watch.assert_called_once_with(handler, watch)
    mock_obs.unschedule.assert_not_called()


def test_stop_watching_twice_is_safe(tmp_path):
    """A second stop is a no-op: the handler is removed exactly once."""
    (tmp_path / "context.md").write_text("c")
    (tmp_path / "principles.md").write_text("p")
    loader = ContextLoader(tmp_path)
    mock_obs = MagicMock()
    with patch("claudia.context_loader._get_shared_observer", return_value=mock_obs):
        loader.start_watching(lambda f, p: None)
        loader.stop_watching()
        loader.stop_watching()  # second call: _watch/_handler already None — no-op
    assert mock_obs.remove_handler_for_watch.call_count == 1


def test_reload_count_increments_on_change(tmp_path):
    """Each handled change bumps `reload_count`, which invalidates the agent's cached prompt."""
    (tmp_path / "context.md").write_text("# Role\nX")
    (tmp_path / "principles.md").write_text("# P\nY")
    loader = ContextLoader(tmp_path)
    assert loader.reload_count == 0
    loader._handle_change("context.md")
    assert loader.reload_count == 1
    loader._handle_change("principles.md")
    assert loader.reload_count == 2
