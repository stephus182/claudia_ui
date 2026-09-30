"""Tests for scripts/replay_eval.py — the store cut, which must read the file ClaudIA wrote.

`cut_store` copies `claudia.db` through a read-only connection and deletes everything after
the chosen turn. Its source used to be opened as `file:{src}?mode=ro`, formatted by hand:
SQLite reads a URI's query after the first `?`, its fragment after `#`, and decodes `%HH`
(https://www.sqlite.org/uri.html § 3.1), so a path holding `#` or `?` opened — and created —
a different, empty file beside the directory, read-write, and the cut failed on "no such
table: messages" (reproduced 2026-09-30, gap #83). The script is loaded from its path: it is
a script, not a module of the package.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from claudia.conversation_store import ConversationStore

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_eval.py"


@pytest.fixture(scope="module")
def replay_eval() -> ModuleType:
    """`scripts/replay_eval.py`, executed as a module (it has no `main()` side effects)."""
    spec = importlib.util.spec_from_file_location("replay_eval", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conversation(path: Path) -> tuple[str, int]:
    """A session of three turns written by ClaudIA's own store; returns (session, first turn)."""
    store = ConversationStore(path)
    store.create_session("s-1", doc_version="v7")
    first = store.add_message("s-1", "user", "first question")
    store.add_message("s-1", "assistant", "an answer")
    store.add_message("s-1", "user", "second question")
    return "s-1", first


@pytest.mark.parametrize(
    "spelling",
    ["odd dir #1 ?x %41/claudia.db", "relative/claudia.db", "~/claudia.db"],
    ids=["metacharacters", "relative", "tilde"],
)
def test_the_cut_reads_the_file_the_conversation_store_wrote(
    replay_eval, spelling, tmp_path, monkeypatch
):
    """For one path value, `cut_store` must read what `ConversationStore` wrote — the app
    opens `CLAUDIA_DB_PATH` literally, so the replay must too. A relative value and a `~`
    value both resolve against the working directory (SQLite never expands `~`); HOME points
    at a scratch directory so that an expansion would look there, not in the operator's."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    work = tmp_path / "work"
    work.mkdir()
    session_id, first = _conversation(Path(spelling))
    before = sorted(os.listdir(tmp_path))

    cut, cut_session, doc_version = replay_eval.cut_store(Path(spelling), first, work)

    assert (cut_session, doc_version) == (session_id, "v7")
    with sqlite3.connect(cut) as copy:
        kept = [r[0] for r in copy.execute("SELECT content FROM messages ORDER BY id")]
    copy.close()
    assert kept == ["first question"]
    assert sorted(os.listdir(tmp_path)) == before, "the open created a file beside the store"


def test_the_source_is_never_written(replay_eval, tmp_path):
    """The cut edits its copy, never the store it was taken from."""
    source = tmp_path / "claudia.db"
    _session, first = _conversation(source)
    work = tmp_path / "work"
    work.mkdir()

    replay_eval.cut_store(source, first, work)

    with sqlite3.connect(source) as original:
        count = original.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    original.close()
    assert count == 3
