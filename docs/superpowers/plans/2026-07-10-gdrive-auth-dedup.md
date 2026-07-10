# GDrive Auth De-duplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the duplicated Google Drive credential load/refresh/persist logic
(currently reimplemented separately in `ibkr_core_mcp/cache.py`'s `GDriveCache` and
`claudia_ui/claudia/gdrive_sync.py`'s `GDriveSync`) into one shared module,
`ibkr_core_mcp/gdrive_auth.py`, with zero change in external behavior for either class.

**Architecture:** New pure-function module `ibkr_core_mcp/gdrive_auth.py` exposes
`load_or_refresh_credentials(token_file, scopes) -> Credentials | None` and
`persist_credentials(token_file, creds) -> None`. `GDriveCache._get_service()` and
`GDriveSync._get_service()` both call `load_or_refresh_credentials` first; `GDriveCache`
keeps its exclusive ownership of the interactive `InstalledAppFlow` bootstrap (used only
when the shared function returns `None`), `GDriveSync` keeps raising `RuntimeError` in
that case (unchanged — it must never pop an interactive browser mid-chat-session).

**Tech Stack:** Python 3.11+, `google-auth`, `google-auth-oauthlib`,
`google-api-python-client`, `pytest`, `unittest.mock`.

**Design spec:** `docs/superpowers/specs/2026-07-10-gdrive-auth-dedup-design.md`

**Repos involved (two separate git repos, both required for this plan):**
- `ibkr_core_mcp` at `/Users/steph/Claude_Projects/ibkr_core_mcp` — own `.venv`, own `pytest`, own git history.
- `claudia_ui` at `/Users/steph/Claude_Projects/claudia_ui` — own `.venv` (with `ibkr_core_mcp` installed editable via `pip install -e "../ibkr_core_mcp"`, so changes to `ibkr_core_mcp`'s source are picked up immediately without reinstalling), own `pytest`, own git history.

---

### Task 1: Write failing tests for `ibkr_core_mcp/gdrive_auth.py`

**Repo:** `ibkr_core_mcp`

**Files:**
- Create: `tests/test_gdrive_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for gdrive_auth — shared Drive OAuth credential load/refresh/persist logic.

Shared by ibkr_core_mcp's GDriveCache and claudia_ui's GDriveSync. See
docs/superpowers/specs/2026-07-10-gdrive-auth-dedup-design.md (claudia_ui repo) for why
this module exists.
"""
import os
import stat
from unittest.mock import MagicMock, patch

from ibkr_core_mcp.gdrive_auth import load_or_refresh_credentials, persist_credentials

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def test_load_or_refresh_returns_none_when_file_missing(tmp_path):
    token_file = tmp_path / "token.json"
    result = load_or_refresh_credentials(token_file, _SCOPES)
    assert result is None


def test_load_or_refresh_returns_valid_credentials_unchanged(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"existing": "token"}')

    mock_creds = MagicMock()
    mock_creds.valid = True

    with patch(
        "ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file",
        return_value=mock_creds,
    ), patch("ibkr_core_mcp.gdrive_auth.Request") as mock_request:
        result = load_or_refresh_credentials(token_file, _SCOPES)

    assert result is mock_creds
    mock_request.assert_not_called()
    mock_creds.refresh.assert_not_called()
    # File is untouched — valid credentials are never rewritten.
    assert token_file.read_text() == '{"existing": "token"}'


def test_load_or_refresh_refreshes_and_persists_expired_credentials(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"existing": "token"}')

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "rt"
    mock_creds.to_json.return_value = '{"refreshed": true}'

    with patch(
        "ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file",
        return_value=mock_creds,
    ), patch("ibkr_core_mcp.gdrive_auth.Request"):
        result = load_or_refresh_credentials(token_file, _SCOPES)

    assert result is mock_creds
    mock_creds.refresh.assert_called_once()
    assert token_file.read_text() == '{"refreshed": true}'


def test_load_or_refresh_returns_none_when_expired_and_unrefreshable(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"existing": "token"}')

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = None

    with patch(
        "ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file",
        return_value=mock_creds,
    ):
        result = load_or_refresh_credentials(token_file, _SCOPES)

    assert result is None
    # No refresh attempted, nothing persisted — file untouched.
    assert token_file.read_text() == '{"existing": "token"}'


def test_persist_credentials_writes_json_with_restricted_permissions(tmp_path):
    token_file = tmp_path / "token.json"
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "value"}'

    persist_credentials(token_file, mock_creds)

    assert token_file.read_text() == '{"token": "value"}'
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600


def test_persist_credentials_enforces_permissions_on_preexisting_file(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    os.chmod(token_file, 0o644)  # simulate a pre-existing file with loose permissions

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "value"}'

    persist_credentials(token_file, mock_creds)

    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600


def test_persist_credentials_creates_parent_directory(tmp_path):
    token_file = tmp_path / "nested" / "dir" / "token.json"
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "value"}'

    persist_credentials(token_file, mock_creds)

    assert token_file.read_text() == '{"token": "value"}'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_gdrive_auth.py -v`
Expected: `ModuleNotFoundError: No module named 'ibkr_core_mcp.gdrive_auth'` (collection error, all 7 tests fail/error)

- [ ] **Step 3: Commit**

```bash
git add tests/test_gdrive_auth.py
git commit -m "test: add failing tests for shared gdrive_auth module"
```

---

### Task 2: Implement `ibkr_core_mcp/gdrive_auth.py`

**Repo:** `ibkr_core_mcp`

**Files:**
- Create: `ibkr_core_mcp/gdrive_auth.py`

- [ ] **Step 1: Write the implementation**

```python
"""Shared Google Drive OAuth credential loading, refresh, and persistence.

Used by both ibkr_core_mcp.cache.GDriveCache and claudia_ui's claudia.gdrive_sync.GDriveSync
so there is exactly one implementation of "how do we load/refresh a Drive token." Before
this module existed, both classes independently reimplemented the same ~15 lines of
Credentials-loading/refresh/persist logic — see
docs/superpowers/specs/2026-07-10-gdrive-auth-dedup-design.md (claudia_ui repo) for why
that was extracted.

Source (google-auth credentials): https://google-auth.readthedocs.io/en/stable/reference/google.oauth2.credentials.html
"""
from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


def load_or_refresh_credentials(token_file: Path, scopes: list[str]) -> Credentials | None:
    """Load credentials from token_file, refreshing in place if expired but refreshable.

    Returns the credentials unchanged if still valid. If expired but refreshable (has a
    refresh_token), refreshes via google.auth.transport.requests.Request, persists the
    refreshed token via persist_credentials, and returns it. Returns None if the file
    doesn't exist, or if the credentials are expired with no refresh_token — never raises;
    callers decide what "no usable credentials" means for them (interactive bootstrap, or
    a hard error).
    """
    if not token_file.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        persist_credentials(token_file, creds)
        return creds
    return None


def persist_credentials(token_file: Path, creds: Credentials) -> None:
    """Write creds.to_json() to token_file with mode 0o600.

    Two-step chmod: os.open's O_CREAT mode only applies the permission on file creation,
    not on an existing file, so os.chmod is called unconditionally afterward to enforce
    0o600 regardless of whether the file was created or truncated.
    """
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_path = str(token_file)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(token_path, 0o600)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_gdrive_auth.py -v`
Expected: `7 passed`

- [ ] **Step 3: Commit**

```bash
git add ibkr_core_mcp/gdrive_auth.py
git commit -m "feat: add shared gdrive_auth module for Drive credential load/refresh/persist"
```

---

### Task 3: Refactor `GDriveCache._get_service()` to delegate

**Repo:** `ibkr_core_mcp`

**Files:**
- Modify: `ibkr_core_mcp/cache.py:1-22` (imports), `ibkr_core_mcp/cache.py:74-113` (`_get_service`)
- Modify: `tests/test_cache.py:87-121` (`test_token_file_created_with_restricted_permissions`)

- [ ] **Step 1: Update imports in `ibkr_core_mcp/cache.py`**

Current imports (lines 1-20):
```python
from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from ibkr_core_mcp.config import Config
from ibkr_core_mcp.exceptions import CacheMissError, CacheWriteError
```

Replace with (removes now-unused `os`, `Request`, `Credentials`; adds `gdrive_auth` import):
```python
from __future__ import annotations

import io
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from ibkr_core_mcp.config import Config
from ibkr_core_mcp.exceptions import CacheMissError, CacheWriteError
from ibkr_core_mcp.gdrive_auth import load_or_refresh_credentials, persist_credentials
```

Before removing `import os`, verify nothing else in the file uses `os.`:
Run: `grep -n '\bos\.' ibkr_core_mcp/cache.py`
Expected: no output (only the lines inside `_get_service`, which Step 2 removes)

- [ ] **Step 2: Replace `_get_service` body**

Current (lines 74-113):
```python
    def _get_service(self) -> Any:
        """Return an authenticated Drive API v3 service object.

        Token refresh: if the stored credentials are expired and have a refresh_token,
        they are silently refreshed via google.auth.transport.requests.Request.
        First-time auth: InstalledAppFlow opens a local browser flow on port 0
        (OS-assigned). The resulting token is written to GDRIVE_TOKEN_FILE with
        mode 0o600 (user-only read/write).

        Source: https://developers.google.com/drive/api/quickstart/python
        """
        if self._service:
            return self._service
        creds = None
        if self._config.gdrive_token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._config.gdrive_token_file), _SCOPES
            )
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._config.gdrive_credentials_file), _SCOPES
                )
                creds = flow.run_local_server(port=0)
            self._config.gdrive_token_file.parent.mkdir(parents=True, exist_ok=True)
            token_path = str(self._config.gdrive_token_file)
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(creds.to_json())
            os.chmod(token_path, 0o600)  # enforce on pre-existing files too
        if not self._config.gdrive_folder_id and not self._config.gdrive_cache_folder_id:
            from ibkr_core_mcp.exceptions import CacheError
            raise CacheError(
                "GOOGLE_DRIVE_FOLDER_ID (or GDRIVE_CACHE_FOLDER_ID) is required for "
                "Drive cache but is not set. Set it in .env or pass it to Config."
            )
        self._service = build("drive", "v3", credentials=creds)
        return self._service
```

Replace with:
```python
    def _get_service(self) -> Any:
        """Return an authenticated Drive API v3 service object.

        Delegates token loading/refresh to ibkr_core_mcp.gdrive_auth.load_or_refresh_credentials
        (shared with claudia_ui's GDriveSync). If no valid token exists, runs the interactive
        first-time bootstrap: InstalledAppFlow opens a local browser flow on port 0
        (OS-assigned), and the resulting token is persisted via gdrive_auth.persist_credentials
        with mode 0o600 (user-only read/write).

        Source: https://developers.google.com/drive/api/quickstart/python
        """
        if self._service:
            return self._service
        creds = load_or_refresh_credentials(self._config.gdrive_token_file, _SCOPES)
        if creds is None:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self._config.gdrive_credentials_file), _SCOPES
            )
            creds = flow.run_local_server(port=0)
            persist_credentials(self._config.gdrive_token_file, creds)
        if not self._config.gdrive_folder_id and not self._config.gdrive_cache_folder_id:
            from ibkr_core_mcp.exceptions import CacheError
            raise CacheError(
                "GOOGLE_DRIVE_FOLDER_ID (or GDRIVE_CACHE_FOLDER_ID) is required for "
                "Drive cache but is not set. Set it in .env or pass it to Config."
            )
        self._service = build("drive", "v3", credentials=creds)
        return self._service
```

- [ ] **Step 3: Update `test_token_file_created_with_restricted_permissions` in `tests/test_cache.py`**

Current (lines 87-121):
```python
def test_token_file_created_with_restricted_permissions(tmp_path):
    import os
    import stat
    from unittest.mock import MagicMock

    from ibkr_core_mcp.cache import GDriveCache

    token_file = tmp_path / "token.json"
    token_file.write_text('{"existing": "token"}')

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder123"
    cfg.gdrive_token_file = token_file
    cfg.gdrive_credentials_file = tmp_path / "creds.json"

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "refresh_token_value"
    fake_creds.to_json.return_value = '{"token": "refreshed"}'

    cache = GDriveCache.__new__(GDriveCache)
    cache._config = cfg
    cache._service = None
    cache._manifest = {}
    cache._manifest_loaded_at = 0.0

    with patch("ibkr_core_mcp.cache.Credentials.from_authorized_user_file", return_value=fake_creds), \
         patch("ibkr_core_mcp.cache.Request"), \
         patch("ibkr_core_mcp.cache.build") as mock_build:
        mock_build.return_value = MagicMock()
        cache._get_service()

    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
```

Replace with (patches `gdrive_auth.Credentials`/`gdrive_auth.Request` — the module where
`Credentials.from_authorized_user_file` and `Request` are actually called now — instead of
the removed `cache.Credentials`/`cache.Request` names):
```python
def test_token_file_created_with_restricted_permissions(tmp_path):
    import os
    import stat
    from unittest.mock import MagicMock

    from ibkr_core_mcp.cache import GDriveCache

    token_file = tmp_path / "token.json"
    token_file.write_text('{"existing": "token"}')

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder123"
    cfg.gdrive_token_file = token_file
    cfg.gdrive_credentials_file = tmp_path / "creds.json"

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "refresh_token_value"
    fake_creds.to_json.return_value = '{"token": "refreshed"}'

    cache = GDriveCache.__new__(GDriveCache)
    cache._config = cfg
    cache._service = None
    cache._manifest = {}
    cache._manifest_loaded_at = 0.0

    with patch("ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file", return_value=fake_creds), \
         patch("ibkr_core_mcp.gdrive_auth.Request"), \
         patch("ibkr_core_mcp.cache.build") as mock_build:
        mock_build.return_value = MagicMock()
        cache._get_service()

    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
```

- [ ] **Step 4: Verify `test_get_service_raises_on_empty_folder_id` still passes unmodified**

This test (lines 146-175 of `tests/test_cache.py`) patches
`ibkr_core_mcp.cache.Credentials.from_authorized_user_file` and `ibkr_core_mcp.cache.build`
with a credentials mock that has `valid = True`. Since `Credentials` is no longer imported
into `cache.py` after Step 1, this patch target will fail to resolve. Update it the same way
as Step 3 — change `patch("ibkr_core_mcp.cache.Credentials.from_authorized_user_file", ...)`
to `patch("ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file", ...)`. Leave the
`patch("ibkr_core_mcp.cache.build")` line unchanged (`build` is still imported directly into
`cache.py`).

- [ ] **Step 5: Run the full cache test file**

Run: `.venv/bin/pytest tests/test_cache.py -v`
Expected: all tests pass (same count as before this task — check with `git stash` +
`.venv/bin/pytest tests/test_cache.py --collect-only -q | tail -1` beforehand if you want an
exact baseline count)

- [ ] **Step 6: Commit**

```bash
git add ibkr_core_mcp/cache.py tests/test_cache.py
git commit -m "refactor: GDriveCache delegates credential load/refresh to gdrive_auth"
```

---

### Task 4: Refactor `GDriveSync._get_service()` to delegate

**Repo:** `claudia_ui`

**Files:**
- Modify: `claudia/gdrive_sync.py:1-35` (imports), `claudia/gdrive_sync.py:52-92` (`_get_service`)
- Modify: `tests/test_gdrive_sync.py:180-197` (`test_get_service_writes_back_refreshed_token`)

- [ ] **Step 1: Update imports in `claudia/gdrive_sync.py`**

Current imports (lines 13-30):
```python
import io
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from ibkr_core_mcp.config import Config
```

Replace with (removes now-unused `os`, `Request`, `Credentials`; adds `gdrive_auth` import):
```python
import io
import logging
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from ibkr_core_mcp.config import Config
from ibkr_core_mcp.gdrive_auth import load_or_refresh_credentials
```

Before removing `import os`, verify nothing else in the file uses `os.`:
Run: `grep -n '\bos\.' claudia/gdrive_sync.py`
Expected: no output (only the lines inside `_get_service`, which Step 2 removes)

- [ ] **Step 2: Replace `_get_service` body**

Current (lines 52-92):
```python
    def _get_service(self) -> Any:
        """Return an authenticated Drive API v3 service object (cached per instance).

        Token refresh: if the access token is expired but a refresh_token is present,
        google-auth calls the OAuth2 token endpoint automatically via creds.refresh(Request()).
        The refreshed token is written back to the token file with strict permissions (0o600)
        so subsequent processes reuse it without re-prompting.

        Two-step chmod: os.open with O_CREAT mode 0o600 only applies the permission on
        file creation, not on an existing file. os.chmod is called unconditionally after
        the write to enforce 0o600 regardless of whether the file was created or truncated.

        Source (google-auth credentials): https://google-auth.readthedocs.io/en/stable/reference/google.oauth2.credentials.html
        Source (Drive API v3 service): https://developers.google.com/drive/api/reference/rest/v3
        """
        with self._lock:
            if self._service:
                return self._service
            token_file = self._config.gdrive_token_file
            if not token_file.exists():
                raise RuntimeError(
                    f"GDrive token file not found: {token_file}. "
                    "Authenticate via GDriveCache (ibkr_core_mcp) first."
                )
            creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    token_path = str(self._config.gdrive_token_file)
                    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write(creds.to_json())
                    # chmod separately: O_CREAT mode only applies on creation, not on existing files.
                    os.chmod(token_path, 0o600)
                else:
                    raise RuntimeError(
                        "GDrive credentials are invalid and cannot be refreshed. "
                        "Re-authenticate via GDriveCache (ibkr_core_mcp)."
                    )
            self._service = build("drive", "v3", credentials=creds)
            return self._service
```

Replace with:
```python
    def _get_service(self) -> Any:
        """Return an authenticated Drive API v3 service object (cached per instance).

        Delegates token loading/refresh to ibkr_core_mcp.gdrive_auth.load_or_refresh_credentials
        (shared with ibkr_core_mcp's GDriveCache). Unlike GDriveCache, this never runs the
        interactive bootstrap flow — if no valid token exists, it raises, since popping a
        browser mid-chat-session is not acceptable here. Authenticate via GDriveCache
        (ibkr_core_mcp) first to establish the initial token.

        Source (Drive API v3 service): https://developers.google.com/drive/api/reference/rest/v3
        """
        with self._lock:
            if self._service:
                return self._service
            creds = load_or_refresh_credentials(self._config.gdrive_token_file, _SCOPES)
            if creds is None:
                raise RuntimeError(
                    f"GDrive token file not found or invalid: {self._config.gdrive_token_file}. "
                    "Authenticate via GDriveCache (ibkr_core_mcp) first."
                )
            self._service = build("drive", "v3", credentials=creds)
            return self._service
```

- [ ] **Step 3: Update `test_get_service_writes_back_refreshed_token` in `tests/test_gdrive_sync.py`**

Current (lines 180-197):
```python
def test_get_service_writes_back_refreshed_token(sync, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    sync._config.gdrive_token_file = token_file

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "rt"
    mock_creds.to_json.return_value = '{"refreshed": true}'

    with patch("claudia.gdrive_sync.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("claudia.gdrive_sync.Request"), \
         patch("claudia.gdrive_sync.build"):
        sync._get_service()

    assert token_file.read_text() == '{"refreshed": true}'
```

Replace with (patches `ibkr_core_mcp.gdrive_auth.Credentials`/`Request` — the module where
the actual loading/refresh now happens — instead of the removed `claudia.gdrive_sync.Credentials`/`Request` names):
```python
def test_get_service_writes_back_refreshed_token(sync, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    sync._config.gdrive_token_file = token_file

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "rt"
    mock_creds.to_json.return_value = '{"refreshed": true}'

    with patch("ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("ibkr_core_mcp.gdrive_auth.Request"), \
         patch("claudia.gdrive_sync.build"):
        sync._get_service()

    assert token_file.read_text() == '{"refreshed": true}'
```

- [ ] **Step 4: Check all other tests in `tests/test_gdrive_sync.py` that patch `_get_service` directly**

Every other test in this file (`test_download_db_*`, `test_upload_db_*`, `test_read_text_*`)
uses `patch.object(sync, "_get_service", ...)` — patching the method itself, not its
internals — so they are unaffected by this refactor and need no changes. Only the one test
that patches the internal `Credentials`/`Request`/`build` names (Step 3) needed updating.

- [ ] **Step 5: Run the full gdrive_sync test file**

Run: `.venv/bin/pytest tests/test_gdrive_sync.py -v`
Expected: all tests pass (same count as before this task)

- [ ] **Step 6: Commit**

```bash
git add claudia/gdrive_sync.py tests/test_gdrive_sync.py
git commit -m "refactor: GDriveSync delegates credential load/refresh to ibkr_core_mcp.gdrive_auth"
```

---

### Task 5: Full regression + live verification

**Repos:** both

- [ ] **Step 1: Run the full ibkr_core_mcp unit test suite**

Run (from `/Users/steph/Claude_Projects/ibkr_core_mcp`): `.venv/bin/pytest -m "not integration" -v`
Expected: all tests pass, no new failures vs. the pre-refactor baseline

- [ ] **Step 2: Run the full claudia_ui unit test suite**

Run (from `/Users/steph/Claude_Projects/claudia_ui`): `.venv/bin/pytest -m "not integration" -v`
Expected: all tests pass, no new failures vs. the pre-refactor baseline

- [ ] **Step 3: Live-verify `GDriveCache` against the real Drive account**

Run (from `/Users/steph/Claude_Projects/claudia_ui`, same venv — `ibkr_core_mcp` is installed
editable here):
```bash
.venv/bin/python3 -c "
from ibkr_core_mcp.config import Config
from ibkr_core_mcp.cache import GDriveCache

config = Config.from_env(dotenv_path='.env')
cache = GDriveCache(config)
print('market_data folder id:', cache._resolve_cache_folder())
print('account_data folder id:', cache._resolve_account_folder())
"
```
Expected: both folder IDs print with no exception (matches the folder IDs already verified
during the 2026-07-10 OAuth client migration — `1dP66xpsigTSGsbHuIvQrUsR6cXPgguHJ` and
`1G1vzNmv4b-si4StzxcbT08v6oLQKhHec` respectively, unless the Drive folder structure has
since changed).

- [ ] **Step 4: Live-verify `GDriveSync` against the real Drive account**

Run (from `/Users/steph/Claude_Projects/claudia_ui`):
```bash
.venv/bin/python3 -c "
from ibkr_core_mcp.config import Config
from claudia.gdrive_sync import GDriveSync

config = Config.from_env(dotenv_path='.env')
sync = GDriveSync(config)
print('ping:', sync.ping())
"
```
Expected: `ping: True`

- [ ] **Step 5: No commit for this task** (verification only, no file changes)

---

### Task 6: Documentation pass

**Repos:** both — only proceed with this task after Tasks 1-5 are complete and all tests/live
checks pass. Do not fix documentation for behavior that isn't implemented and verified yet.

**Files:**
- Modify: `claudia_ui/.env.example:38-39`
- Modify: `ibkr_core_mcp/CLAUDE.md:47-48`
- Modify: `ibkr_core_mcp/CLAUDE.md:51`
- Modify: `ibkr_core_mcp/CLAUDE.md:510-511`
- Modify: `ibkr_core_mcp/docs/windows-setup.md:76-77`
- Modify: `claudia_ui/docs/connectivity.md:105`
- Modify: `claudia_ui/CLAUDE.md` (GDrive Sync section — add architecture note)

- [ ] **Step 1: Update `claudia_ui/.env.example`**

Current (lines 38-39):
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials.json
```

Replace with:
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token_ibkr_core_mcp.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials_ibkr_core_mcp.json
```

- [ ] **Step 2: Update `ibkr_core_mcp/CLAUDE.md` example `.env` block**

Current (lines 47-48, inside the `.env` code block):
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials.json
```

Replace with:
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token_ibkr_core_mcp.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials_ibkr_core_mcp.json
```

- [ ] **Step 3: Update `ibkr_core_mcp/CLAUDE.md` line 51**

Current:
```
Never commit `.env`, `token.json`, or `credentials.json`.
```

Replace with:
```
Never commit `.env` or any GDrive OAuth credential/token file (e.g. `credentials_ibkr_core_mcp.json`, `token_ibkr_core_mcp.json`).
```

- [ ] **Step 4: Update `ibkr_core_mcp/CLAUDE.md` Claude Desktop config JSON example**

Current (lines 510-511):
```json
        "GDRIVE_TOKEN_FILE": "~/.ibkr_core/token.json",
        "GDRIVE_CREDENTIALS_FILE": "~/.ibkr_core/credentials.json"
```

Replace with:
```json
        "GDRIVE_TOKEN_FILE": "~/.ibkr_core/token_ibkr_core_mcp.json",
        "GDRIVE_CREDENTIALS_FILE": "~/.ibkr_core/credentials_ibkr_core_mcp.json"
```

- [ ] **Step 5: Update `ibkr_core_mcp/docs/windows-setup.md`**

Current (lines 76-77):
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials.json
```

Replace with:
```
GDRIVE_TOKEN_FILE=~/.ibkr_core/token_ibkr_core_mcp.json
GDRIVE_CREDENTIALS_FILE=~/.ibkr_core/credentials_ibkr_core_mcp.json
```

- [ ] **Step 6: Update `claudia_ui/docs/connectivity.md` line 105**

Read the surrounding table row first (`grep -n -B2 -A2 "regenerate" docs/connectivity.md`)
to confirm the exact current line, then change the filename reference from `token.json` to
`token_ibkr_core_mcp.json` to match the actual current file, e.g.:
```
| Token file deleted | Re-run `ibkr_core_mcp` GDriveCache OAuth flow to regenerate `token_ibkr_core_mcp.json` |
```

- [ ] **Step 7: Add architecture note to `claudia_ui/CLAUDE.md`**

In the `## GDrive Sync` section, after the "What syncs" table, add:

```markdown
**Shared credentials, independent implementations:** `GDriveSync` (this file's module) and
`ibkr_core_mcp`'s `GDriveCache` both read `GDRIVE_TOKEN_FILE`/`GDRIVE_CREDENTIALS_FILE` from
the same `Config`/env vars — one Drive OAuth client, shared because both run in the same
process (claudia_ui imports ibkr_core_mcp directly; see architecture diagram above). They are
not in a service/client relationship: each builds its own `googleapiclient.discovery.build("drive", "v3", ...)`
service object. As of 2026-07-10 both delegate the actual credential load/refresh/persist
logic to the shared `ibkr_core_mcp.gdrive_auth` module (see
`docs/superpowers/specs/2026-07-10-gdrive-auth-dedup-design.md`) — `GDriveCache` additionally
owns the interactive first-time OAuth bootstrap, which `GDriveSync` deliberately does not
have (it raises if no valid token exists, rather than popping a browser mid-chat-session).
```

- [ ] **Step 8: Commit (each repo separately)**

In `ibkr_core_mcp`:
```bash
git add CLAUDE.md docs/windows-setup.md
git commit -m "docs: update GDrive credential filenames to match ibkr_core_mcp OAuth client"
```

In `claudia_ui`:
```bash
git add .env.example docs/connectivity.md CLAUDE.md
git commit -m "docs: update GDrive credential filenames and add gdrive_auth architecture note"
```

---

## Self-Review Notes (completed during plan authoring)

- **Spec coverage:** every phase in the design spec (extract module, refactor both call
  sites, test coverage for all 4 branches of `load_or_refresh_credentials` +
  `persist_credentials`, full regression, live verification, all 6 stale doc locations) has
  a corresponding task above.
- **Placeholder scan:** no TBD/TODO; every code step shows complete before/after code, not a
  description of what to change.
- **Type consistency:** `load_or_refresh_credentials(token_file: Path, scopes: list[str]) -> Credentials | None`
  and `persist_credentials(token_file: Path, creds: Credentials) -> None` signatures are
  identical everywhere they're defined (Task 2) and called (Tasks 3, 4).
