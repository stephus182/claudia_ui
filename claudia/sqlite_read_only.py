"""The one way ClaudIA opens a SQLite database read-only (gaps #83 and #89, 2026-09-30).

A read-only open is a URI open (`mode=ro`), and the path's characters must not change which
file that URI names. Two spellings did:

- `f"file:{path}?mode=ro"`, formatted by hand. SQLite reads a URI's query after the first `?`,
  its fragment after `#`, and decodes `%HH` in the path (https://www.sqlite.org/uri.html
  § 3.1), so over a path holding `?` or `#` it opened — and created — a different file beside
  the directory, read-write (gap #83). Seven openers carried it.
- `Path.as_uri()` at each of those sites, which escapes all of that but writes a NUL as `%00`.
  SQLite ends a URI's path at `%00` and opens what precedes it — `sqlite3ParseUri` in its
  `src/main.c` skips the rest of the path unless built with `SQLITE_ENABLE_URI_00_ERROR`, and
  its documentation pages do not say so (measured on 3.53.4, gap #89). A NUL names no file on
  any file system, so it is refused here, as `sqlite3.connect` refuses one in a plain path.

Every URI open in the package and its scripts goes through `connect_read_only`
(`tests/test_sqlite_uris.py` holds that), so each property is tested once, in
`tests/test_sqlite_read_only.py`.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def connect_read_only(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open the database at `path` read-only — that file, and no other.

    `absolute()` is the least a URI needs: SQLite resolves a relative path against the working
    directory, so this names the file a plain `sqlite3.connect(path)` would. Never
    `expanduser()`: SQLite does not expand `~`, and ClaudIA opens `CLAUDIA_DB_PATH` literally,
    so an expansion would read a different file than the app writes. A missing file is an
    error, never created.

    Raises:
        TypeError: `path` is not a path (a test double, None).
        OSError: `path` is relative and the working directory no longer exists.
        ValueError: `path` holds a NUL, or a character the file system cannot encode.
        sqlite3.Error: the file cannot be opened.
    """
    absolute = Path(path).absolute()
    if "\x00" in str(absolute):
        raise ValueError(f"a path holding a NUL names no file: {str(absolute)!r}")
    return sqlite3.connect(f"{absolute.as_uri()}?mode=ro", uri=True)
