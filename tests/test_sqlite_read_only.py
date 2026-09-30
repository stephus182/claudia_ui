"""The one read-only SQLite opener, `claudia.sqlite_read_only.connect_read_only` (gaps #83, #89).

Two ways a path's characters used to change which file a read-only open reached:

- a hand-formatted `file:{path}?mode=ro` lost `mode=ro` over a path holding `?` or `#`, and
  opened — and created — a different file beside the directory, read-write (gap #83;
  https://www.sqlite.org/uri.html § 3.1);
- `Path.as_uri()`, which replaced it, writes a NUL as `%00`, and SQLite ends a URI's path there
  and opens what precedes it (`sqlite3ParseUri` in SQLite's `src/main.c`; its documentation
  pages do not say so — measured 2026-09-30 on 3.53.4, gap #89).

Every URI open in the package and its scripts goes through this one function
(`tests/test_sqlite_uris.py` holds that), so each property is tested once, here.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from claudia.sqlite_read_only import connect_read_only


def _database(path: Path, marker: str) -> Path:
    """A database at `path` whose one row names it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker (v)")
    conn.execute("INSERT INTO marker VALUES (?)", (marker,))
    conn.commit()
    conn.close()
    return path


def _marker(conn: sqlite3.Connection) -> str:
    """The row naming the database `conn` opened; closes `conn`."""
    try:
        return str(conn.execute("SELECT v FROM marker").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("directory", ["plain", "odd dir #1 ?x %41", "per%25cent &a=b"])
def test_it_opens_the_file_the_path_names_and_creates_nothing(directory, tmp_path):
    """`?`, `#`, `&`, `=` and `%HH` stay in the filename, and nothing appears beside the
    directory — where the hand-formatted form created a new, writable database."""
    path = _database(tmp_path / directory / "store.db", directory)
    before = sorted(p.name for p in tmp_path.iterdir())

    assert _marker(connect_read_only(path)) == directory
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("directory", ["plain", "odd dir #1 ?x %41"])
def test_the_connection_cannot_write(directory, tmp_path):
    """`mode=ro` survives the escaping: the file it opened cannot be written through it."""
    conn = connect_read_only(_database(tmp_path / directory / "store.db", directory))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE written (x)")
    finally:
        conn.close()


def test_a_missing_file_is_an_error_and_is_never_created(tmp_path):
    """A read-only open creates nothing: no empty database is left where the store should be."""
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        connect_read_only(missing)
    assert not missing.exists()


def test_a_nul_is_refused_even_when_its_prefix_is_a_database(tmp_path):
    """A NUL names no file on any file system. Written as `%00` into the URI it would end the
    path, and the database at the prefix — a real one here — would open in place of an error."""
    _database(tmp_path / "store", "the prefix")
    with pytest.raises(ValueError, match="NUL"):
        connect_read_only(tmp_path / "store\x00.db")


def test_the_premise_sqlite_ends_a_uri_path_at_percent_00(tmp_path):
    """Why the NUL is refused: SQLite opens the prefix of a URI path cut at `%00`. Should a
    build ever reject `%00` instead (`SQLITE_ENABLE_URI_00_ERROR`), this fails, and the reason
    in `claudia/sqlite_read_only.py` must be re-read."""
    prefix = _database(tmp_path / "store", "the prefix")
    conn = sqlite3.connect(f"{prefix.as_uri()}%00.db?mode=ro", uri=True)
    assert _marker(conn) == "the prefix"


def test_a_relative_path_opens_under_the_working_directory(tmp_path, monkeypatch):
    """`absolute()` names what a plain `sqlite3.connect(path)` names: SQLite resolves a relative
    path against the working directory."""
    _database(tmp_path / "store.db", "relative")
    monkeypatch.chdir(tmp_path)
    assert _marker(connect_read_only("store.db")) == "relative"


def test_a_tilde_is_a_directory_name_not_home(tmp_path, monkeypatch):
    """SQLite never expands `~`, and ClaudIA opens `CLAUDIA_DB_PATH` literally, so neither does
    this: `~/x.db` is a directory named `~` under the working directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _database(tmp_path / "home" / "x.db", "home")
    _database(tmp_path / "~" / "x.db", "literal")
    monkeypatch.chdir(tmp_path)
    assert _marker(connect_read_only("~/x.db")) == "literal"


@pytest.mark.parametrize(
    ("value", "error"),
    [(object(), TypeError), ("store\ud800.db", ValueError)],
    ids=["not-a-path", "lone-surrogate"],
)
def test_a_value_that_names_no_file_raises_before_anything_opens(
    value, error, tmp_path, monkeypatch
):
    """A test double is no path; a lone surrogate cannot be encoded for the file system."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(error):
        connect_read_only(value)
    assert list(tmp_path.iterdir()) == []


def test_a_relative_path_with_no_working_directory_raises_oserror(tmp_path):
    """`absolute()` asks for the working directory; once it is deleted, that is an `OSError`."""
    gone = tmp_path / "gone"
    gone.mkdir()
    home = os.getcwd()
    os.chdir(gone)
    try:
        gone.rmdir()
        with pytest.raises(OSError):
            connect_read_only("store.db")
    finally:
        os.chdir(home)
