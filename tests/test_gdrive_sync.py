"""Tests for GDriveSync — Drive download/upload for claudia.db and text files."""

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


def test_upload_db_creates_file_when_not_on_drive(sync, tmp_path):
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


# ── _get_service ──────────────────────────────────────────────────────────────

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
        def __init__(self, filename, mimetype=None):
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
        def __init__(self, buf, _req):
            buf.write(drive_bytes)
        def next_chunk(self):
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
        def __init__(self, buf, _req):
            buf.write(drive_bytes)
        def next_chunk(self):
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
