"""Tests for ContextLoader — document loading, hashing, watchdog."""

import time
from pathlib import Path

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

    assert len(fired) >= 1
    assert any("principles.md" in f or "context.md" in f for f in fired)
