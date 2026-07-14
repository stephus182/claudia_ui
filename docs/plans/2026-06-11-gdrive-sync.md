# GDrive Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync `claudia.db` (conversation history) to Google Drive — download at session start, upload at stop — and optionally load `context.md` / `principles.md` from Drive for portability across machines.

**Architecture:** A new `GDriveSync` class wraps the Drive API (same credentials as `GDriveCache`). It is a module-level singleton in `app.py`, enabled automatically when `GOOGLE_DRIVE_FOLDER_ID` is set. `ContextLoader` gains optional Drive-text overrides that clear themselves on local file change. All Drive operations are non-fatal — any failure logs a warning and falls back to local state.

**Tech Stack:** `google-api-python-client`, `google-auth`, `google-auth-oauthlib` (already required by `ibkr_core_mcp`); `ibkr_core_mcp.config.Config`; Python `sqlite3`, `shutil`, `tempfile`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `claudia/gdrive_sync.py` | **Create** | `GDriveSync` class: `download_db`, `upload_db`, `read_text` |
| `claudia/context_loader.py` | **Modify** | Accept `context_text`/`principles_text` overrides; clear on file change |
| `claudia/conversation_store.py` | **Modify** | Add `get_last_context_hash()` for hash-change alert |
| `claudia/app.py` | **Modify** | Wire download at start, upload at stop, hash-change alert |
| `tests/test_gdrive_sync.py` | **Create** | Unit tests for GDriveSync (mocked Drive service) |
| `tests/test_context_loader.py` | **Modify** | Tests for Drive text overrides and clear-on-change |
| `tests/test_conversation_store.py` | **Modify** | Test `get_last_context_hash()` |
| `CLAUDE.md` | **Modify** | Document Drive sync, folder layout, how to upload context/principles |
| `SECURITY.md` | **Modify** | Drive sync threat model section |
| `.env.example` | **Modify** | Note on `GOOGLE_DRIVE_FOLDER_ID` enabling DB sync |

---

## Task 1: `claudia/gdrive_sync.py` — GDriveSync class

**Files:**
- Create: `claudia/gdrive_sync.py`
- Create: `tests/test_gdrive_sync.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gdrive_sync.py`:

```python
"""Tests for GDriveSync — Drive download/upload for claudia.db and text files."""

import io
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claudia.gdrive_sync import GDriveSync


@pytest.fixture
def config():
    cfg = MagicMock()
    cfg.gdrive_folder_id = "test-folder-id"
    cfg.gdrive_token_file = Path("/fake/token.json")
    return cfg


@pytest.fixture
def sync(config):
    return GDriveSync(config)


# ── download_db ───────────────────────────────────────────────────────────────

def test_download_db_returns_false_when_not_on_drive(sync, tmp_path):
    with patch.object(sync, "_find_file", return_value=None):
        result = sync.download_db(tmp_path / "claudia.db")
    assert result is False


def test_download_db_returns_false_on_service_error(sync, tmp_path):
    with patch.object(sync, "_get_service", side_effect=RuntimeError("no token")):
        result = sync.download_db(tmp_path / "claudia.db")
    assert result is False


def test_download_db_returns_false_on_integrity_fail(sync, tmp_path):
    bad_bytes = b"this is not a valid sqlite3 database"

    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(bad_bytes)
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    target = tmp_path / "claudia.db"
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.download_db(target)

    assert result is False
    assert not target.exists()  # temp file cleaned up, target not created


def test_download_db_success(sync, tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    db_bytes = src.read_bytes()

    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(db_bytes)
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    target = tmp_path / "claudia.db"
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.download_db(target)

    assert result is True
    assert target.exists()


# ── upload_db ─────────────────────────────────────────────────────────────────

def test_upload_db_calls_create_when_not_on_drive(sync, tmp_path):
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value=None), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaFileUpload"):
        sync.upload_db(db)

    svc.files.return_value.create.assert_called_once()


def test_upload_db_calls_update_when_exists_on_drive(sync, tmp_path):
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value="existing-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaFileUpload"):
        sync.upload_db(db)

    svc.files.return_value.update.assert_called_once()


def test_upload_db_missing_local_file_does_nothing(sync, tmp_path):
    svc = MagicMock()
    with patch.object(sync, "_get_service", return_value=svc):
        sync.upload_db(tmp_path / "nonexistent.db")  # must not raise
    svc.files.assert_not_called()


def test_upload_db_drive_error_does_not_raise(sync, tmp_path):
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()
    with patch.object(sync, "_get_service", side_effect=RuntimeError("auth failed")):
        sync.upload_db(db)  # must not raise


# ── read_text ─────────────────────────────────────────────────────────────────

def test_read_text_returns_none_when_not_on_drive(sync):
    with patch.object(sync, "_find_file", return_value=None):
        result = sync.read_text("context.md")
    assert result is None


def test_read_text_returns_content(sync):
    content = "# Role\nI am ClaudIA."

    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(content.encode())
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == content


def test_read_text_error_returns_none(sync):
    with patch.object(sync, "_get_service", side_effect=RuntimeError("connection error")):
        result = sync.read_text("context.md")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/steph/Claude_Projects/claudia_ui && source .venv/bin/activate
pytest tests/test_gdrive_sync.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'claudia.gdrive_sync'`

- [ ] **Step 3: Implement `claudia/gdrive_sync.py`**

Create `claudia/gdrive_sync.py`:

```python
"""
GDrive sync for claudia.db, context.md, and principles.md.

Downloads claudia.db from Drive at session start; uploads at stop.
Reads context.md / principles.md from Drive if present (fallback: local files).

Enabled automatically when GOOGLE_DRIVE_FOLDER_ID is set.
Does NOT run an interactive OAuth flow — requires an existing valid token file.
Authenticate first via GDriveCache (ibkr_core_mcp / market data sync).
"""
from __future__ import annotations

import io
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from ibkr_core_mcp.config import Config

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_DB_FILENAME = "claudia.db"


class GDriveSync:
    """Sync claudia.db (and optionally context/principles) to Google Drive."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._service: Any = None

    def _get_service(self) -> Any:
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
            else:
                raise RuntimeError(
                    "GDrive credentials are invalid and cannot be refreshed. "
                    "Re-authenticate via GDriveCache (ibkr_core_mcp)."
                )
        self._service = build("drive", "v3", credentials=creds)
        return self._service

    def _find_file(self, name: str) -> str | None:
        """Return Drive file ID for name in the configured folder, or None."""
        svc = self._get_service()
        folder_id = self._config.gdrive_folder_id
        results = (
            svc.files()
            .list(
                q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
                fields="files(id)",
            )
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None

    def download_db(self, local_path: Path) -> bool:
        """
        Download claudia.db from Drive to local_path.

        Returns True if found and downloaded; False if not on Drive (first run).
        On error: logs warning, returns False — caller continues with local/empty DB.
        """
        try:
            svc = self._get_service()
            file_id = self._find_file(_DB_FILENAME)
            if file_id is None:
                log.info("claudia.db not found on Drive (first run or not yet uploaded)")
                return False

            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd = tempfile.NamedTemporaryFile(
                dir=local_path.parent, suffix=".db.tmp", delete=False
            )
            tmp_path = Path(tmp_fd.name)
            try:
                downloader = MediaIoBaseDownload(
                    tmp_fd, svc.files().get_media(fileId=file_id)
                )
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                tmp_fd.flush()
                tmp_fd.close()

                conn = sqlite3.connect(str(tmp_path))
                try:
                    row = conn.execute("PRAGMA integrity_check").fetchone()
                    if row[0] != "ok":
                        log.warning(
                            "claudia.db from Drive failed integrity_check (%s) — ignoring",
                            row[0],
                        )
                        return False
                finally:
                    conn.close()

                shutil.move(str(tmp_path), local_path)
                log.info("Downloaded claudia.db from Drive to %s", local_path)
                return True
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        except Exception as exc:
            log.warning("GDriveSync.download_db failed: %s — continuing with local DB", exc)
            return False

    def upload_db(self, local_path: Path) -> None:
        """
        Upload local_path as claudia.db to Drive (create or update in-place).
        On error: logs warning — local copy is preserved, data not lost.
        """
        if not local_path.exists():
            log.warning("GDriveSync.upload_db: %s not found — nothing to upload", local_path)
            return
        try:
            # Checkpoint WAL so the main DB file contains all committed data
            conn = sqlite3.connect(str(local_path))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

            svc = self._get_service()
            folder_id = self._config.gdrive_folder_id
            media = MediaFileUpload(str(local_path), mimetype="application/x-sqlite3")
            file_id = self._find_file(_DB_FILENAME)
            if file_id:
                svc.files().update(fileId=file_id, media_body=media).execute()
            else:
                metadata = {"name": _DB_FILENAME, "parents": [folder_id]}
                svc.files().create(body=metadata, media_body=media, fields="id").execute()
            log.info("Uploaded claudia.db to Drive")
        except Exception as exc:
            log.warning("GDriveSync.upload_db failed: %s — local copy preserved", exc)

    def read_text(self, filename: str) -> str | None:
        """
        Download a text file (e.g. "context.md") from Drive.
        Returns content string, or None if not found or on any error.
        """
        try:
            svc = self._get_service()
            file_id = self._find_file(filename)
            if file_id is None:
                return None
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning(
                "GDriveSync.read_text(%r) failed: %s — using local fallback", filename, exc
            )
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_gdrive_sync.py -v
```

Expected: all 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claudia/gdrive_sync.py tests/test_gdrive_sync.py
git commit -m "feat: add GDriveSync — download/upload claudia.db + read text files from Drive"
```

---

## Task 2: `claudia/context_loader.py` — Drive text overrides

**Files:**
- Modify: `claudia/context_loader.py:47-66`
- Modify: `tests/test_context_loader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context_loader.py`:

```python
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
    # When both overrides are provided, local files are not read
    loader = ContextLoader(tmp_path, context_text="Context text", principles_text="Principles text")
    prompt = loader.load_system_prompt()
    assert "Context text" in prompt
    assert "Principles text" in prompt


def test_compute_hash_reflects_override_text(docs_dir):
    loader_local = ContextLoader(docs_dir)
    loader_drive = ContextLoader(docs_dir, context_text="# Different Drive context")
    assert loader_local.compute_hash() != loader_drive.compute_hash()


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

    assert len(fired_prompts) >= 1
    assert "Local Context" in fired_prompts[-1]
    assert "Drive Context" not in fired_prompts[-1]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_context_loader.py -v -k "override or hash_reflects or clears_context"
```

Expected: `TypeError: ContextLoader.__init__() got an unexpected keyword argument 'context_text'`

- [ ] **Step 3: Implement the changes in `claudia/context_loader.py`**

Replace the `ContextLoader` class (lines 39–109) with:

```python
class ContextLoader:
    """
    Manages the two user-written documents that form ClaudIA's system prompt.

    Documents are loaded from CLAUDIA_DOCS_PATH (default: docs/).
    Optional context_text / principles_text override local file reads (used
    when Drive versions are fetched at session start). A local file-change
    event clears the overrides so hot-reload reverts to the local files.
    """

    def __init__(
        self,
        docs_path: str | Path = "docs",
        context_text: str | None = None,
        principles_text: str | None = None,
    ) -> None:
        self.docs_path = Path(docs_path)
        self._context_path = self.docs_path / "context.md"
        self._principles_path = self.docs_path / "principles.md"
        self._context_override: str | None = context_text
        self._principles_override: str | None = principles_text
        self._watch: ObservedWatch | None = None
        self._reload_callback: Callable[[str, str], None] | None = None

    def _get_text(self, override: str | None, path: Path, name: str) -> str:
        if override is not None:
            return override
        return self._read_required(path, name)

    def load_system_prompt(self) -> str:
        """Return concatenated context + principles as a single system prompt string."""
        context = self._get_text(self._context_override, self._context_path, "context.md")
        principles = self._get_text(
            self._principles_override, self._principles_path, "principles.md"
        )
        return _CONTEXT_HEADER + context + _PRINCIPLES_HEADER + principles

    def compute_hash(self) -> str:
        """SHA-256 of context + principles content, for integrity tracking."""
        context = self._get_text(self._context_override, self._context_path, "context.md")
        principles = self._get_text(
            self._principles_override, self._principles_path, "principles.md"
        )
        combined = context + principles
        return hashlib.sha256(combined.encode()).hexdigest()

    def start_watching(self, on_reload: Callable[[str, str], None]) -> None:
        """
        Register a watchdog handler on the shared module-level Observer.
        Unschedules any previous watch for this instance first.
        """
        self.stop_watching()
        self._reload_callback = on_reload
        handler = _DocChangeHandler(
            watched={self._context_path, self._principles_path},
            on_change=self._handle_change,
        )
        obs = _get_shared_observer()
        self._watch = obs.schedule(handler, str(self.docs_path), recursive=False)
        log.info("Watching %s for document changes", self.docs_path)

    def stop_watching(self) -> None:
        if self._watch is not None:
            try:
                _get_shared_observer().unschedule(self._watch)
            except Exception:
                pass
            self._watch = None
        self._reload_callback = None

    def _handle_change(self, changed_file: str) -> None:
        # File change clears Drive overrides — local files become source of truth
        self._context_override = None
        self._principles_override = None
        if self._reload_callback:
            try:
                new_prompt = self.load_system_prompt()
                self._reload_callback(changed_file, new_prompt)
            except Exception as exc:
                log.error("Failed to reload documents after change: %s", exc)

    @staticmethod
    def _read_required(path: Path, name: str) -> str:
        if not path.exists():
            raise FileNotFoundError(
                f"Required document not found: {path}\n"
                f"Create docs/{name} to configure ClaudIA's {name.replace('.md', '')}."
            )
        return path.read_text(encoding="utf-8", errors="replace").strip()
```

- [ ] **Step 4: Run all context_loader tests**

```bash
pytest tests/test_context_loader.py -v
```

Expected: all tests PASS (including existing ones, which still work since `context_text=None` is default)

- [ ] **Step 5: Commit**

```bash
git add claudia/context_loader.py tests/test_context_loader.py
git commit -m "feat: context_loader accepts Drive text overrides, clears them on local file change"
```

---

## Task 3: `claudia/conversation_store.py` — `get_last_context_hash()`

**Files:**
- Modify: `claudia/conversation_store.py:130` (after `close_session`)
- Modify: `tests/test_conversation_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_conversation_store.py`:

```python
def test_get_last_context_hash_no_sessions(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    assert store.get_last_context_hash() is None


def test_get_last_context_hash_no_completed_sessions(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-open", context_hash="abc123")
    # Session not closed — ended_at is NULL
    assert store.get_last_context_hash() is None


def test_get_last_context_hash_returns_most_recent_completed(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-1", context_hash="hash-old")
    store.close_session("sess-1")
    store.create_session("sess-2", context_hash="hash-new")
    store.close_session("sess-2")
    assert store.get_last_context_hash() == "hash-new"


def test_get_last_context_hash_ignores_open_session(tmp_path):
    store = ConversationStore(tmp_path / "claudia.db")
    store.create_session("sess-closed", context_hash="hash-closed")
    store.close_session("sess-closed")
    store.create_session("sess-open", context_hash="hash-open")
    # Open session is ignored; most recent CLOSED one is returned
    assert store.get_last_context_hash() == "hash-closed"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_conversation_store.py -v -k "last_context_hash"
```

Expected: `AttributeError: 'ConversationStore' object has no attribute 'get_last_context_hash'`

- [ ] **Step 3: Add `get_last_context_hash()` to `claudia/conversation_store.py`**

Insert after `close_session` (after line 135):

```python
    def get_last_context_hash(self) -> str | None:
        """Return context_hash from the most recently completed session, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT context_hash FROM sessions "
                "WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC LIMIT 1"
            ).fetchone()
        return row["context_hash"] if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_conversation_store.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add claudia/conversation_store.py tests/test_conversation_store.py
git commit -m "feat: add ConversationStore.get_last_context_hash() for Drive sync security alert"
```

---

## Task 4: `claudia/app.py` — Wire GDriveSync into session lifecycle

**Files:**
- Modify: `claudia/app.py`

This task has no unit tests (app.py wiring is integration-level). Manual verification is in Step 5.

- [ ] **Step 1: Add `_gdrive_sync` singleton and import**

In `claudia/app.py`, after the existing imports (around line 196), add the import:

```python
from claudia.gdrive_sync import GDriveSync
```

After the `_connectivity_checker: ConnectivityChecker | None = None` line (around line 214), add:

```python
_gdrive_sync: GDriveSync | None = None
```

- [ ] **Step 2: Add GDriveSync setup and `download_db` to `on_chat_start`**

In `on_chat_start`, before the `loader = ContextLoader(_DOCS_PATH)` line, insert:

```python
    # GDrive sync — download DB on first session start (before store is opened)
    global _gdrive_sync, _config
    if _gdrive_sync is None and os.environ.get("GOOGLE_DRIVE_FOLDER_ID"):
        cfg = _config or Config.from_env()
        _config = cfg
        try:
            _gdrive_sync = GDriveSync(cfg)
            if _conv_store is None:
                _gdrive_sync.download_db(_DB_PATH)
        except Exception as exc:
            log.warning("GDriveSync setup failed: %s — continuing without Drive sync", exc)

    # Read context/principles from Drive (None if not configured or not on Drive)
    drive_context: str | None = None
    drive_principles: str | None = None
    if _gdrive_sync is not None:
        drive_context = _gdrive_sync.read_text("context.md")
        drive_principles = _gdrive_sync.read_text("principles.md")
```

Change the `ContextLoader` instantiation to pass Drive texts:

```python
    loader = ContextLoader(_DOCS_PATH, context_text=drive_context, principles_text=drive_principles)
```

- [ ] **Step 3: Add hash-change security alert to `on_chat_start`**

Find the existing block:
```python
    store = _get_store()
    store.create_session(session_id, context_hash=loader.compute_hash())
```

Replace with:
```python
    store = _get_store()

    # Hash-change security alert: warn if context/principles changed since last session
    prev_hash = store.get_last_context_hash()
    current_hash = loader.compute_hash()
    if prev_hash is not None and prev_hash != current_hash:
        await cl.Message(
            content=(
                "⚠️ **context.md / principles.md changed since your last session.**\n"
                "Please verify the content before continuing."
            ),
            author="System",
        ).send()

    store.create_session(session_id, context_hash=current_hash)
```

- [ ] **Step 4: Add `upload_db` to `on_stop`**

Find the existing `on_stop` function:
```python
@cl.on_stop
async def on_stop():
    session_id = cl.user_session.get("session_id")
    store: ConversationStore = cl.user_session.get("store")
    loader: ContextLoader = cl.user_session.get("loader")

    if loader:
        loader.stop_watching()

    if store and session_id:
        store.close_session(session_id, metadata={"model": _MODEL})
```

Add Drive upload after `close_session`:
```python
@cl.on_stop
async def on_stop():
    session_id = cl.user_session.get("session_id")
    store: ConversationStore = cl.user_session.get("store")
    loader: ContextLoader = cl.user_session.get("loader")

    if loader:
        loader.stop_watching()

    if store and session_id:
        store.close_session(session_id, metadata={"model": _MODEL})

    if _gdrive_sync is not None:
        await cl.make_async(_gdrive_sync.upload_db)(_DB_PATH)
```

- [ ] **Step 5: Manual smoke test**

Without `GOOGLE_DRIVE_FOLDER_ID` set:
```bash
cd /Users/steph/Claude_Projects/claudia_ui && source .venv/bin/activate
chainlit run claudia/app.py --no-cache 2>&1 | head -40
```
Expected: starts normally with no Drive-related warnings, no errors.

With `GOOGLE_DRIVE_FOLDER_ID` set (if Drive is configured):
- Start ClaudIA → check logs for "Downloaded claudia.db from Drive" or "claudia.db not found on Drive (first run)"
- Send a message, close session → check logs for "Uploaded claudia.db to Drive"

- [ ] **Step 6: Run unit tests to verify no regressions**

```bash
pytest -m "not integration" -v
```

Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add claudia/app.py
git commit -m "feat: wire GDriveSync into app.py — download DB at start, upload at stop, hash-change alert"
```

---

## Task 5: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `SECURITY.md`
- Modify: `.env.example`

- [ ] **Step 1: Update `.env.example`**

Find the `GOOGLE_DRIVE_FOLDER_ID` line and add a comment:

```bash
# Google Drive folder for market data cache AND claudia.db sync.
# When set, claudia.db is downloaded from Drive at session start and
# uploaded at stop. context.md / principles.md are also read from Drive
# if present (upload them via the Drive web UI to enable portability).
GOOGLE_DRIVE_FOLDER_ID=your_folder_id_here
```

- [ ] **Step 2: Update `CLAUDE.md` Architecture section**

Add to the Architecture diagram's comment block a note about GDriveSync:

After the `claudia/conversation_store.py` line in the architecture section, add:
```
claudia/gdrive_sync.py      — GDriveSync: download claudia.db at start / upload at stop
                              also reads context.md / principles.md from Drive if present
```

Add a new section **GDrive Sync** after the existing architecture section, before Dev Setup:

```markdown
## GDrive Sync

`claudia/gdrive_sync.py` — `GDriveSync` class, auto-enabled when `GOOGLE_DRIVE_FOLDER_ID` is set.

### What syncs

| File | Direction | When |
|---|---|---|
| `claudia.db` | Drive → local | Session start (first session in process, before DB opens) |
| `claudia.db` | local → Drive | Session stop (after `close_session`, with WAL checkpoint) |
| `context.md` | Drive → memory | Session start (overrides local file if present on Drive) |
| `principles.md` | Drive → memory | Session start (overrides local file if present on Drive) |

### Drive folder layout

All files share `GOOGLE_DRIVE_FOLDER_ID`:
```
<Drive folder>/
  manifest.json                    ← market data index (GDriveCache, ibkr_core_mcp)
  AAPL_1D_1Y_2026-01-01.parquet    ← market data (GDriveCache)
  claudia.db                       ← conversation history (GDriveSync)
  context.md                       ← ClaudIA persona (optional, upload manually)
  principles.md                    ← trading rules (optional, upload manually)
```

### First-time setup (new machine)

1. `GOOGLE_DRIVE_FOLDER_ID` is already set from your existing `.env` (market data uses it)
2. Start ClaudIA — it downloads `claudia.db` if it exists on Drive (first session skips download)
3. To enable Drive context/principles: upload `docs/context.md` and `docs/principles.md` to the Drive folder via the web UI

### Hot-reload after Drive fetch

Drive texts are fetched once at session start. The watchdog still watches local files — if you edit `docs/context.md` while a session is running, the local file takes over (Drive override is cleared).

### Error handling

All Drive operations are non-fatal. On any failure (no token, network error, bad file):
- `download_db`: logs warning, continues with existing local `claudia.db`
- `upload_db`: logs warning, local copy preserved; sync retries next session
- `read_text` for context/principles: logs warning, falls back to local `docs/` files
```

- [ ] **Step 3: Update `SECURITY.md`**

Add a **GDrive Sync Threat Model** section after the existing security sections:

```markdown
## GDrive Sync Threat Model

Drive sync introduces three attack surfaces. All are mitigated without relaxing the existing order execution barriers.

### 1. Poisoned `context.md` / `principles.md` (HIGH — prompt injection)

**Threat:** An attacker with Drive access modifies `principles.md` to weaken trading rules or inject adversarial instructions into the system prompt.

**Mitigations:**
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by anything from Drive.
  Order execution still requires physical button click + Touch ID + tkinter dialog regardless
  of what is in `principles.md`.
- On each session start with Drive content, `SHA-256(context + principles)` is compared against
  the hash stored in the previous session's `sessions.context_hash`. A mismatch triggers a
  visible `⚠️` warning in chat.

### 2. Poisoned `claudia.db` (MEDIUM — conversation history injection)

**Threat:** A malicious actor replaces `claudia.db` on Drive with a crafted file containing
fake conversation history designed to influence responses.

**Mitigations:**
- After downloading, `PRAGMA integrity_check` runs on the SQLite file. A structurally
  tampered file fails this check; ClaudIA falls back to the existing local DB.
- Conversation history cannot initiate an order — the physical button + biometric path
  is the only execution route.

### 3. Drive OAuth token theft (LOW — scoped blast radius)

**Threat:** Stolen `GDRIVE_TOKEN_FILE` grants Drive access.

**Mitigations:**
- The `drive.file` OAuth scope limits the token to files this app created — cannot access
  the rest of the user's Drive.
- `GDRIVE_TOKEN_FILE` is `chmod 600`.
- The token covers only `claudia.db`, `context.md`, `principles.md`, and market data
  parquets — no IBKR credentials, no `ANTHROPIC_API_KEY`.

### Hard guarantees unchanged

These two properties hold regardless of what is on Drive:
- The hardcoded `_SAFETY_BLOCK` in `agent.py` cannot be overridden by Drive content.
- No order can be placed without physical button click + Touch ID + tkinter dialog.
```

- [ ] **Step 4: Run tests to confirm docs didn't break anything**

```bash
pytest -m "not integration" -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md SECURITY.md .env.example
git commit -m "docs: document GDrive sync feature — folder layout, threat model, first-time setup"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered in |
|---|---|
| `GDriveSync(config)` with `download_db`, `upload_db`, `read_text` | Task 1 |
| Download at session start (before store opens) | Task 4 Step 2 |
| Upload at session stop (after close_session) | Task 4 Step 4 |
| `PRAGMA integrity_check` on downloaded DB | Task 1 (gdrive_sync.py) |
| Drive text overrides for context/principles | Task 2 |
| Clear overrides on local file change | Task 2 |
| Hash-change security alert in chat | Task 3 + Task 4 Step 3 |
| Auto-enable when `GOOGLE_DRIVE_FOLDER_ID` set | Task 4 Step 2 |
| Non-fatal: all failures fall back gracefully | Task 1 (all exception handlers) |
| WAL checkpoint before upload | Task 1 (upload_db) |
| No new env vars | All tasks (uses existing `GOOGLE_DRIVE_FOLDER_ID`) |
| No interactive OAuth flow | Task 1 (_get_service raises on missing/invalid token) |
| `drive.file` scope (limited blast radius) | Task 1 (`_SCOPES`) |
| CLAUDE.md docs | Task 5 |
| SECURITY.md threat model | Task 5 |
| `.env.example` updated | Task 5 |

**Placeholder scan:** None found — all steps have complete code.

**Type consistency check:**
- `GDriveSync.__init__(config: Config)` — `Config` is `ibkr_core_mcp.config.Config` throughout
- `download_db(local_path: Path) -> bool` — consistent in test and impl
- `upload_db(local_path: Path) -> None` — consistent
- `read_text(filename: str) -> str | None` — consistent
- `ContextLoader(docs_path, context_text=None, principles_text=None)` — consistent in test and impl
- `store.get_last_context_hash() -> str | None` — consistent
- `_gdrive_sync.upload_db(_DB_PATH)` — `_DB_PATH` is `Path`, matches signature ✓
