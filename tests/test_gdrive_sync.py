"""Tests for GDriveSync — Drive download/upload for claudia.db and text files."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claudia.gdrive_sync import GDriveSync


@pytest.fixture
def config():
    """A stub Config carrying a Drive folder id and token path."""
    cfg = MagicMock()
    cfg.gdrive_folder_id = "test-folder-id"
    cfg.gdrive_token_file = Path("/fake/token.json")
    return cfg


@pytest.fixture
def sync(config):
    """A GDriveSync built on the stub config."""
    return GDriveSync(config)


# ── download_db ───────────────────────────────────────────────────────────────

def test_download_db_returns_false_when_not_on_drive(sync, tmp_path):
    """Nothing on Drive means nothing downloaded, reported rather than raised."""
    with patch.object(sync, "_find_file", return_value=None):
        result = sync.download_db(tmp_path / "claudia.db")
    assert result is False


def test_download_db_returns_false_on_service_error(sync, tmp_path):
    """An auth or service failure is reported, not raised into session init."""
    with patch.object(sync, "_get_service", side_effect=RuntimeError("no token")):
        result = sync.download_db(tmp_path / "claudia.db")
    assert result is False


def test_download_db_returns_false_on_integrity_fail(sync, tmp_path):
    """Bytes that are not a valid database are rejected rather than written over the local file."""
    bad_bytes = b"this is not a valid sqlite3 database"

    class FakeDownloader:
        """A downloader that yields bytes which are not a valid database."""
        def __init__(self, buf, _req):
            """Write the invalid bytes straight into the caller's buffer."""
            buf.write(bad_bytes)
        def next_chunk(self):
            """Report the single chunk as complete."""
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
    """A valid database downloads and lands at the target path."""
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    db_bytes = src.read_bytes()

    class FakeDownloader:
        """A downloader that yields the prepared database bytes in one chunk."""
        def __init__(self, buf, _req):
            """Write the database bytes straight into the caller's buffer."""
            buf.write(db_bytes)
        def next_chunk(self):
            """Report the single chunk as complete."""
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
    """A first upload creates the Drive file."""
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
    """A later upload updates the existing file rather than creating a duplicate."""
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
    """With no local database there is nothing to upload and no call is made."""
    svc = MagicMock()
    with patch.object(sync, "_get_service", return_value=svc):
        sync.upload_db(tmp_path / "nonexistent.db")  # must not raise
    svc.files.assert_not_called()


def test_upload_db_drive_error_does_not_raise(sync, tmp_path):
    """A Drive failure at session end is logged, never raised — the local database survives."""
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()
    with patch.object(sync, "_get_service", side_effect=RuntimeError("auth failed")):
        sync.upload_db(db)  # must not raise


def test_upload_db_creates_file_when_not_on_drive(sync, tmp_path):
    """The upload targets the resolved database folder when creating the file."""
    db = tmp_path / "claudia.db"
    sqlite3.connect(str(db)).close()

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value=None), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch.object(sync, "_resolve_db_folder", return_value="folder-id"), \
         patch("claudia.gdrive_sync.MediaFileUpload"):
        sync.upload_db(db)

    svc.files().create.assert_called_once()


# ── read_text ─────────────────────────────────────────────────────────────────

def test_read_text_returns_none_when_not_on_drive(sync):
    """A document absent from Drive returns None so the caller can fall back to the local file."""
    with patch.object(sync, "_find_file", return_value=None):
        result = sync.read_text("context.md")
    assert result is None


def test_read_text_returns_content(sync):
    """A document present on Drive is returned decoded."""
    content = "# Role\nI am ClaudIA."

    class FakeDownloader:
        """A downloader that yields the prepared document text in one chunk."""
        def __init__(self, buf, _req):
            """Write the encoded document into the caller's buffer."""
            buf.write(content.encode())
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == content


def test_read_text_error_returns_none(sync):
    """A Drive error returns None rather than raising into session init."""
    with patch.object(sync, "_get_service", side_effect=RuntimeError("connection error")):
        result = sync.read_text("context.md")
    assert result is None


def test_read_text_skips_when_local_not_older_than_drive(sync, tmp_path):
    """A stale Drive copy must not silently override a local context.md/principles.md
    edit that was never re-uploaded — the same gap download_db already guards against
    for claudia.db. Found live 2026-07-10 (v3 local vs v1 stale Drive copy)."""
    local_file = tmp_path / "context.md"
    local_file.write_text("local content")  # mtime = now

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "13", "modifiedTime": "2020-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc):
        result = sync.read_text("context.md", local_path=local_file)

    assert result is None


def test_read_text_skips_on_exact_mtime_tie(sync, tmp_path):
    """The one case that actually distinguishes read_text's >= from download_db's strict
    > : an exact tie between local mtime and Drive's modifiedTime must skip (see the
    comment at the comparison in read_text for why the two guards differ)."""
    local_file = tmp_path / "context.md"
    local_file.write_text("local content")

    tie_dt = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    os.utime(local_file, (tie_dt.timestamp(), tie_dt.timestamp()))

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "13", "modifiedTime": "2026-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc):
        result = sync.read_text("context.md", local_path=local_file)

    assert result is None


def test_read_text_proceeds_when_drive_newer(sync, tmp_path):
    """The guard must not block legitimate updates uploaded from another machine."""
    local_file = tmp_path / "context.md"
    local_file.write_text("stale local content")

    class FakeDownloader:
        """A downloader standing in for a fresh Drive copy of the document."""
        def __init__(self, buf, _req):
            """Write the fresh Drive content into the caller's buffer."""
            buf.write(b"fresh drive content")
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "20", "modifiedTime": "2099-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md", local_path=local_file)

    assert result == "fresh drive content"


def test_read_text_without_local_path_downloads_unconditionally(sync):
    """Backward compatibility: a caller that doesn't pass local_path gets the old
    unconditional-download behavior (no local file to compare against)."""
    class FakeDownloader:
        """A downloader standing in for the Drive copy of the document."""
        def __init__(self, buf, _req):
            """Write the Drive content into the caller's buffer."""
            buf.write(b"drive content")
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "13", "modifiedTime": "2026-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == "drive content"


# ── _get_service ──────────────────────────────────────────────────────────────

def test_get_service_writes_back_refreshed_token(sync, tmp_path):
    """A refreshed credential is written back, so the next start does not re-refresh."""
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


# ── G1: upload_db must upload a WAL-consistent snapshot ──────────────────────

def test_upload_db_uploads_wal_consistent_snapshot(sync, tmp_path):
    """A row committed to the WAL (not yet checkpointed into the main file)
    must be present in the uploaded bytes — review finding G1."""
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('wal-resident-row')")
    conn.commit()
    # Keep conn open: prevents the close-time auto-checkpoint, so the row
    # lives only in claudia.db-wal — exactly the state at session stop when
    # another connection is still active.
    assert (tmp_path / "claudia.db-wal").exists()

    uploaded = {}

    class FakeUpload:
        """An upload stub that captures the bytes actually handed to Drive."""
        def __init__(self, filename, mimetype=None):
            """Capture the bytes of the file Drive was asked to upload."""
            uploaded["bytes"] = Path(filename).read_bytes()

    svc = MagicMock()
    try:
        with patch.object(sync, "_find_file", return_value="existing-id"), \
             patch.object(sync, "_get_service", return_value=svc), \
             patch.object(sync, "_resolve_db_folder", return_value="folder-id"), \
             patch("claudia.gdrive_sync.MediaFileUpload", FakeUpload):
            sync.upload_db(db)
    finally:
        conn.close()

    snap = tmp_path / "uploaded_snapshot.db"
    snap.write_bytes(uploaded["bytes"])
    rows = sqlite3.connect(str(snap)).execute("SELECT v FROM t").fetchall()
    assert rows == [("wal-resident-row",)]
    # No leftover snapshot temp files next to the DB
    assert not list(tmp_path.glob("*.upload.tmp"))


# ── G2: download_db freshness guard ──────────────────────────────────────────

def _valid_db_bytes(tmp_path, marker):
    """Bytes of a real one-row SQLite database, tagged with `marker`."""
    src = tmp_path / f"_src_{marker}.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE m (v TEXT)")
    conn.execute("INSERT INTO m VALUES (?)", (marker,))
    conn.commit()
    conn.close()
    return src.read_bytes()


def test_download_db_skips_when_local_newer_than_drive(sync, tmp_path):
    """A failed end-session upload followed by a process restart must not let an
    older Drive copy overwrite the newer local DB — review finding G2."""
    target = tmp_path / "claudia.db"
    local_bytes = _valid_db_bytes(tmp_path, "newer-local")
    target.write_bytes(local_bytes)  # mtime = now; Drive copy is from 2020

    drive_bytes = _valid_db_bytes(tmp_path, "older-drive")

    class FakeDownloader:
        """A downloader that yields the prepared Drive bytes in one chunk."""
        def __init__(self, buf, _req):
            """Write the prepared Drive bytes into the caller's buffer."""
            buf.write(drive_bytes)
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "modifiedTime": "2020-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.download_db(target)

    assert result is False
    assert target.read_bytes() == local_bytes  # local preserved


def test_download_db_proceeds_when_drive_newer(sync, tmp_path):
    """The guard must not block legitimate syncs from another machine."""
    target = tmp_path / "claudia.db"
    target.write_bytes(_valid_db_bytes(tmp_path, "older-local"))

    drive_bytes = _valid_db_bytes(tmp_path, "newer-drive")

    class FakeDownloader:
        """A downloader that yields the prepared Drive bytes in one chunk."""
        def __init__(self, buf, _req):
            """Write the prepared Drive bytes into the caller's buffer."""
            buf.write(drive_bytes)
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "modifiedTime": "2099-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.download_db(target)

    assert result is True
    rows = sqlite3.connect(str(target)).execute("SELECT v FROM m").fetchall()
    assert rows == [("newer-drive",)]


# ── G3: stale WAL/SHM sidecars removed when download replaces the DB ─────────

def test_download_db_removes_stale_wal_shm_sidecars(sync, tmp_path):
    """Sidecars from a crashed prior run must not be replayed into a freshly
    downloaded DB — review finding G3."""
    target = tmp_path / "claudia.db"
    (tmp_path / "claudia.db-wal").write_bytes(b"stale wal from crashed run")
    (tmp_path / "claudia.db-shm").write_bytes(b"stale shm from crashed run")

    drive_bytes = _valid_db_bytes(tmp_path, "fresh-from-drive")

    class FakeDownloader:
        """A downloader that yields the prepared Drive bytes in one chunk."""
        def __init__(self, buf, _req):
            """Write the prepared Drive bytes into the caller's buffer."""
            buf.write(drive_bytes)
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.download_db(target)  # target absent -> guard not involved

    assert result is True
    assert not (tmp_path / "claudia.db-wal").exists()
    assert not (tmp_path / "claudia.db-shm").exists()


# ── reconnect (2026-09-03, the Drive action button) ───────────────────────────

def test_reconnect_drops_the_cached_service_and_authenticates_again(sync):
    """reconnect() must not reuse the cached service: a stale token is the whole reason
    to click. It rebuilds the service and returns ping()'s verdict."""
    stale = MagicMock(name="stale-service")
    sync._service = stale
    fresh = MagicMock(name="fresh-service")
    with (
        patch("claudia.gdrive_sync.load_or_refresh_credentials", return_value=MagicMock()) as creds,
        patch("claudia.gdrive_sync.build", return_value=fresh),
    ):
        assert sync.reconnect() is True
    creds.assert_called_once()
    assert sync._service is fresh
    fresh.files.return_value.list.return_value.execute.assert_called_once()
    stale.files.assert_not_called()


def test_reconnect_reports_false_when_the_token_is_gone(sync):
    """No valid token → False, no raise — the button reports it, the app keeps running."""
    sync._service = MagicMock()
    with patch("claudia.gdrive_sync.load_or_refresh_credentials", return_value=None):
        assert sync.reconnect() is False
    assert sync._service is None
