"""Tests for ContextLoader — document loading, hashing, watchdog."""

import time

import pytest

from claudia.context_loader import ContextLoader


@pytest.fixture
def docs_dir(tmp_path):
    (tmp_path / "context.md").write_text("# Role\nI am ClaudIA.")
    (tmp_path / "principles.md").write_text("# Principles\n- Risk first.")
    return tmp_path


def test_load_system_prompt(docs_dir):
    loader = ContextLoader(docs_dir)
    prompt = loader.load_system_prompt()
    assert "ClaudIA" in prompt
    assert "Risk first" in prompt
    assert "ROLE & CONTEXT" in prompt
    assert "TRADING PRINCIPLES" in prompt


def test_compute_hash_is_deterministic(docs_dir):
    loader = ContextLoader(docs_dir)
    h1 = loader.compute_hash()
    h2 = loader.compute_hash()
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_changes_on_file_edit(docs_dir):
    loader = ContextLoader(docs_dir)
    h1 = loader.compute_hash()
    (docs_dir / "principles.md").write_text("# Principles\n- Updated rule.")
    h2 = loader.compute_hash()
    assert h1 != h2


def test_missing_context_raises(tmp_path):
    (tmp_path / "principles.md").write_text("# Principles")
    loader = ContextLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match="context.md"):
        loader.load_system_prompt()


def test_missing_principles_raises(tmp_path):
    (tmp_path / "context.md").write_text("# Role")
    loader = ContextLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match="principles.md"):
        loader.load_system_prompt()


def test_watchdog_fires_callback(docs_dir):
    loader = ContextLoader(docs_dir)
    fired = []

    def on_reload(filename, new_prompt):
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
    loader = ContextLoader(docs_dir, context_text="# Drive Context\nDrive role.")
    prompt = loader.load_system_prompt()
    assert "Drive Context" in prompt
    assert "Drive role" in prompt
    # Local file content should NOT appear
    assert "I am ClaudIA" not in prompt


def test_principles_text_override_used_instead_of_file(docs_dir):
    loader = ContextLoader(docs_dir, principles_text="# Drive Principles\n- Drive rule.")
    prompt = loader.load_system_prompt()
    assert "Drive Principles" in prompt
    assert "Drive rule" in prompt
    assert "Risk first" not in prompt


def test_both_overrides_no_local_files_needed(tmp_path):
    # When both overrides are provided, local files are not read at all
    loader = ContextLoader(tmp_path, context_text="Context text", principles_text="Principles text")
    prompt = loader.load_system_prompt()
    assert "Context text" in prompt
    assert "Principles text" in prompt


def test_compute_hash_reflects_override_text(docs_dir):
    loader_local = ContextLoader(docs_dir)
    loader_drive = ContextLoader(docs_dir, context_text="# Different Drive context")
    assert loader_local.compute_hash() != loader_drive.compute_hash()


def test_get_effective_texts_returns_file_content(docs_dir):
    loader = ContextLoader(docs_dir)
    ctx, pri = loader.get_effective_texts()
    assert "ClaudIA" in ctx
    assert "Risk first" in pri


def test_get_effective_texts_returns_overrides(docs_dir):
    loader = ContextLoader(docs_dir, context_text="override ctx", principles_text="override pri")
    ctx, pri = loader.get_effective_texts()
    assert ctx == "override ctx"
    assert pri == "override pri"


def test_compute_hash_stable_across_drive_and_local_sources(docs_dir):
    # Drive content with surrounding whitespace must hash the same as the
    # equivalent local file (which _read_required always strips). Prevents
    # spurious security alerts when switching between Drive and local sources.
    local_content = (docs_dir / "context.md").read_text()
    loader_local = ContextLoader(docs_dir)
    loader_drive = ContextLoader(docs_dir, context_text=f"\n{local_content}\n")
    assert loader_local.compute_hash() == loader_drive.compute_hash()


def test_file_change_clears_context_override(docs_dir):
    loader = ContextLoader(docs_dir, context_text="# Drive Context\nDrive role.")
    fired_prompts = []

    def on_reload(filename, new_prompt):
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
