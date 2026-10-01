"""Tests for GDriveSync — Drive download/upload for claudia.db and text files."""

import contextlib
import logging
import os
import sqlite3
from datetime import UTC, datetime
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
    with (
        patch.object(sync, "_find_file", return_value=None),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaFileUpload"),
    ):
        sync.upload_db(db)

    svc.files.return_value.create.assert_called_once()


def test_upload_db_calls_update_when_exists_on_drive(sync, tmp_path):
    """A later upload updates the existing file rather than creating a duplicate."""
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    svc = MagicMock()
    with (
        patch.object(sync, "_find_file", return_value="existing-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaFileUpload"),
    ):
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


# ── gap #5: upload_db must report its outcome, not swallow it ────────────────
#
# `download_db` has always returned bool. `upload_db` returned None and caught every
# exception internally, so the caller's `except` clause was unreachable and a session
# could render "claudia.db → Drive ✅" when nothing had been uploaded. These three pin
# the reporting contract; the panel_app test pins that the caller actually reads it.


def test_upload_db_returns_true_when_the_upload_succeeds(sync, tmp_path):
    """A completed upload says so, so the caller can tell the user the truth."""
    db = tmp_path / "claudia.db"
    sqlite3.connect(str(db)).close()

    svc = MagicMock()
    with (
        patch.object(sync, "_find_file", return_value=None),
        patch.object(sync, "_get_service", return_value=svc),
        patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
        patch("claudia.gdrive_sync.MediaFileUpload"),
    ):
        result = sync.upload_db(db)

    assert result is True


def test_upload_db_returns_false_when_local_file_missing(sync, tmp_path):
    """Nothing to upload is not a successful upload."""
    svc = MagicMock()
    with patch.object(sync, "_get_service", return_value=svc):
        result = sync.upload_db(tmp_path / "nonexistent.db")

    assert result is False


def test_upload_db_returns_false_on_drive_error(sync, tmp_path):
    """A Drive failure is still not raised — but it must no longer look like success."""
    db = tmp_path / "claudia.db"
    sqlite3.connect(str(db)).close()

    with patch.object(sync, "_get_service", side_effect=RuntimeError("auth failed")):
        result = sync.upload_db(db)

    assert result is False


def test_upload_db_creates_file_when_not_on_drive(sync, tmp_path):
    """The upload targets the resolved database folder when creating the file."""
    db = tmp_path / "claudia.db"
    sqlite3.connect(str(db)).close()

    svc = MagicMock()
    with (
        patch.object(sync, "_find_file", return_value=None),
        patch.object(sync, "_get_service", return_value=svc),
        patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
        patch("claudia.gdrive_sync.MediaFileUpload"),
    ):
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
        "size": "13",
        "modifiedTime": "2020-01-01T00:00:00.000Z",
    }
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
    ):
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
        "size": "13",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
    }
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
    ):
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
        "size": "20",
        "modifiedTime": "2099-01-01T00:00:00.000Z",
    }
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
        "size": "13",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
    }
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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

    with (
        patch(
            "ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ),
        patch("ibkr_core_mcp.gdrive_auth.Request"),
        patch("claudia.gdrive_sync.build"),
    ):
        sync._get_service()

    assert token_file.read_text() == '{"refreshed": true}'


def test_every_request_gets_its_own_http(sync):
    """Two requests from the one cached service never share an `httplib2.Http` (gap #61).

    Google's documented rule: "each thread that you are making requests from must have its
    own instance of `httplib2.Http()`"
    (https://googleapis.github.io/google-api-python-client/docs/thread_safety.html). A
    shared one is what aborted the process on 2026-09-23. Uses the real `build()` (static
    discovery, offline) and a real credential, so this is Google's request path, not a mock.
    """
    import httplib2
    from google.oauth2.credentials import Credentials

    creds = Credentials(token="test-token")
    with patch("claudia.gdrive_sync.load_or_refresh_credentials", return_value=creds):
        svc = sync._get_service()

    first = svc.files().list(pageSize=1, fields="files(id)")
    second = svc.files().list(pageSize=1, fields="files(id)")

    assert isinstance(first.http.http, httplib2.Http)
    assert first.http is not second.http
    assert first.http.http is not second.http.http
    assert first.http.credentials is creds


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
        with (
            patch.object(sync, "_find_file", return_value="existing-id"),
            patch.object(sync, "_get_service", return_value=svc),
            patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
            patch("claudia.gdrive_sync.MediaFileUpload", FakeUpload),
        ):
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
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


# ── A WAL sidecar with no frame is not a local edit (2026-09-29) ──────────────

_OLD = datetime(2020, 1, 1, tzinfo=UTC).timestamp()


@contextlib.contextmanager
def _drive_serving(sync, drive_bytes, modified_time):
    """Drive holds one file stamped `modified_time` that downloads as `drive_bytes`."""

    class FakeDownloader:
        """A downloader that yields the prepared Drive bytes in one chunk."""

        def __init__(self, buf, _req):
            """Write the prepared Drive bytes into the caller's buffer."""
            buf.write(drive_bytes)

        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {"modifiedTime": modified_time}
    with (
        patch.object(sync, "_find_file", return_value="file-id"),
        patch.object(sync, "_get_service", return_value=svc),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader),
    ):
        yield


@pytest.mark.parametrize("wal_bytes", [0, 32], ids=["no-bytes", "header-only"])
def test_download_db_ignores_a_wal_sidecar_that_carries_no_frame(sync, tmp_path, caplog, wal_bytes):
    """A `-wal` file with no frame is not a local edit, so a newer Drive copy still lands.

    A read-only open of a WAL-mode database creates `-wal` and `-shm` when they do not exist
    (SQLite >= 3.22.0, https://www.sqlite.org/wal.html, "Read-Only Databases"). Measured
    2026-09-29 on the real `data/claudia.db`: a 0-byte `-wal` stamped by a read-only query
    made the start-up comparison report Drive as "older than local" although nothing had been
    written — and had Drive really been newer, that copy would have been skipped and then
    overwritten at session end. The WAL header is 32 bytes and every frame adds a 24-byte
    header plus a page (https://www.sqlite.org/fileformat2.html#walformat), so a file of at
    most 32 bytes holds no frame and nothing to lose.
    """
    target = tmp_path / "claudia.db"
    target.write_bytes(_valid_db_bytes(tmp_path, "older-local"))
    os.utime(target, (_OLD, _OLD))
    wal = tmp_path / "claudia.db-wal"
    wal.write_bytes(b"\0" * wal_bytes)  # mtime now: newer than Drive, as the real one was
    drive_bytes = _valid_db_bytes(tmp_path, "newer-drive")

    with (
        _drive_serving(sync, drive_bytes, "2026-01-01T00:00:00.000Z"),
        caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"),
    ):
        result = sync.download_db(target)

    assert result is True
    rows = sqlite3.connect(str(target)).execute("SELECT v FROM m").fetchall()
    assert rows == [("newer-drive",)]
    assert not wal.exists(), "the frameless sidecar must be gone with the replaced file"
    assert "older than local" not in caplog.text


def test_download_db_keeps_local_when_the_wal_holds_a_committed_frame(sync, tmp_path, caplog):
    """The other half of the rule, so the fix can never become "ignore every WAL".

    In WAL mode a write moves the `-wal` file's mtime and not the main file's, so a Drive copy
    newer than the main file but older than a WAL holding a committed frame must not replace
    the local database: the frame IS the newest local data.
    """
    target = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE m (v TEXT)")
    conn.execute("INSERT INTO m VALUES ('committed-in-the-wal')")
    conn.commit()  # conn stays open: no close-time checkpoint, the row lives in the WAL
    wal = tmp_path / "claudia.db-wal"
    assert wal.stat().st_size > 32, "the setup must leave a frame in the WAL"
    os.utime(target, (_OLD, _OLD))  # the main file looks old; the edit is in the sidecar
    drive_bytes = _valid_db_bytes(tmp_path, "newer-than-main-older-than-wal")

    with (
        _drive_serving(sync, drive_bytes, "2026-01-01T00:00:00.000Z"),
        caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"),
    ):
        result = sync.download_db(target)

    assert result is False
    assert "older than local" in caplog.text
    assert conn.execute("SELECT v FROM m").fetchall() == [("committed-in-the-wal",)]
    conn.close()


# ── Copies an earlier process left behind are swept (gap #90, 2026-10-01) ─────
#
# Both transfers write a temporary copy of claudia.db beside it and remove it on the way out
# (`finally` on upload, `except` on download). A process that dies mid-transfer runs neither,
# so the copy stays — six were found in data/, 2026-08-10 → 09-25. SQLite opens both copies,
# so a death can strand a `-journal`, `-wal` or `-shm` beside one as well.

_LEFT = (
    "tmpab12cd34.upload.tmp",
    "tmpab12cd34.upload.tmp-journal",
    "tmpzz99yy88.db.tmp",
    "tmpzz99yy88.db.tmp-wal",
    "tmpzz99yy88.db.tmp-shm",
)


def _plant(directory, names, mtime=_OLD):
    """Write a small file for each name, stamped `mtime` (default: long before this process)."""
    for name in names:
        path = directory / name
        path.write_bytes(b"private copy")
        os.utime(path, (mtime, mtime))


def _empty_db(path):
    """A real, empty SQLite database at `path`."""
    sqlite3.connect(str(path)).close()


@contextlib.contextmanager
def _drive_accepting(sync):
    """Drive answers an upload as if it succeeded."""
    with (
        patch.object(sync, "_find_file", return_value="existing-id"),
        patch.object(sync, "_get_service", return_value=MagicMock()),
        patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
        patch("claudia.gdrive_sync.MediaFileUpload"),
    ):
        yield


def test_upload_db_removes_the_copies_an_earlier_process_left(sync, tmp_path, caplog):
    """Every stranded copy and sidecar goes, the database stays, and the log says so."""
    db = tmp_path / "claudia.db"
    _empty_db(db)
    _plant(tmp_path, _LEFT)

    with _drive_accepting(sync), caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"):
        assert sync.upload_db(db) is True

    assert sorted(p.name for p in tmp_path.iterdir()) == ["claudia.db"]
    assert "Removed 5 leftover temporary file(s) of claudia.db transfers" in caplog.text


def test_download_db_removes_them_even_when_drive_has_nothing(sync, tmp_path):
    """The sweep runs before any early return: a first run with no Drive copy still cleans up."""
    _plant(tmp_path, _LEFT)

    with (
        patch.object(sync, "_get_service", return_value=MagicMock()),
        patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
        patch.object(sync, "_find_file", return_value=None),
    ):
        assert sync.download_db(tmp_path / "claudia.db") is False

    assert list(tmp_path.iterdir()) == []


def test_upload_db_sweeps_even_when_there_is_no_database_to_upload(sync, tmp_path):
    """A missing claudia.db is no reason to keep its stranded copies."""
    _plant(tmp_path, _LEFT)

    assert sync.upload_db(tmp_path / "claudia.db") is False

    assert list(tmp_path.iterdir()) == []


def test_a_copy_newer_than_this_process_is_left_alone(sync, tmp_path):
    """A copy written since this process started may be another session's transfer in flight:
    two sessions can close at once, and the lock covers only the Drive call."""
    db = tmp_path / "claudia.db"
    _empty_db(db)
    in_flight = "tmpinflight.upload.tmp"
    _plant(tmp_path, [in_flight], mtime=datetime.now(UTC).timestamp())

    with _drive_accepting(sync):
        assert sync.upload_db(db) is True

    assert (tmp_path / in_flight).exists()


def test_only_the_copies_this_module_writes_are_swept(sync, tmp_path, caplog):
    """Old files that are not its temporaries are never touched — above all the database's own
    live WAL, which holds committed rows — nor other `.tmp` names, a directory, or anything in a
    subdirectory."""
    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('only-in-the-wal')")
    conn.commit()  # conn stays open: the row lives in claudia.db-wal
    for own in ("claudia.db", "claudia.db-wal", "claudia.db-shm"):
        os.utime(tmp_path / own, (_OLD, _OLD))
    others = [
        "notes.tmp",
        "tmp.upload.tmp",
        "tmpab12cd34.upload.tmp.bak",
        "copy.db.tmp",
        "tmpab12cd34.upload.tmp-other",
    ]
    _plant(tmp_path, others)
    (tmp_path / "tmpdirectory.upload.tmp").mkdir()
    os.utime(
        tmp_path / "tmpdirectory.upload.tmp", (_OLD, _OLD)
    )  # old, so age alone cannot spare it
    (tmp_path / "sub").mkdir()
    _plant(tmp_path / "sub", ["tmpab12cd34.upload.tmp"])

    try:
        with _drive_accepting(sync), caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"):
            assert sync.upload_db(db) is True
        assert "Could not remove" not in caplog.text, "a directory is not a copy to remove"
        left = {p.name for p in tmp_path.iterdir()}
        assert left == {
            "claudia.db",
            "claudia.db-wal",
            "claudia.db-shm",
            *others,
            "tmpdirectory.upload.tmp",
            "sub",
        }
        assert (tmp_path / "sub" / "tmpab12cd34.upload.tmp").exists()
        assert conn.execute("SELECT v FROM t").fetchall() == [("only-in-the-wal",)]
    finally:
        conn.close()


def test_a_copy_that_cannot_be_removed_never_fails_the_transfer(sync, tmp_path, caplog):
    """The sweep is housekeeping: an unlink that raises is logged and the upload goes on."""
    db = tmp_path / "claudia.db"
    _empty_db(db)
    _plant(tmp_path, ["tmpab12cd34.upload.tmp"])
    real_unlink = Path.unlink

    def refusing_unlink(self, missing_ok=False):
        """Refuse to remove the planted copy, as a permission error would; allow the rest."""
        if self.name == "tmpab12cd34.upload.tmp":
            raise PermissionError("not permitted")
        real_unlink(self, missing_ok=missing_ok)

    with (
        _drive_accepting(sync),
        patch.object(Path, "unlink", refusing_unlink),
        caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"),
    ):
        assert sync.upload_db(db) is True

    assert "tmpab12cd34.upload.tmp" in caplog.text and "not permitted" in caplog.text


def test_a_directory_that_cannot_be_listed_never_fails_the_transfer(sync, tmp_path, caplog):
    """The other way the sweep can meet the filesystem's refusal: the listing itself."""
    db = tmp_path / "claudia.db"
    _empty_db(db)

    def refusing_iterdir(self):
        """Refuse to list, as an unreadable directory would."""
        raise PermissionError("not permitted")

    with (
        _drive_accepting(sync),
        patch.object(Path, "iterdir", refusing_iterdir),
        caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"),
    ):
        assert sync.upload_db(db) is True

    assert "Could not list" in caplog.text and "not permitted" in caplog.text


def test_a_directory_that_does_not_exist_yet_is_not_a_warning(sync, tmp_path, caplog):
    """A first run has no `data/` yet: nothing to sweep, and nothing to warn about."""
    with caplog.at_level(logging.WARNING, logger="claudia.gdrive_sync"):
        assert sync.upload_db(tmp_path / "data" / "claudia.db") is False

    assert "Could not list" not in caplog.text


def test_the_sweep_recognises_the_names_the_transfers_actually_write(sync, tmp_path):
    """One definition for writing and sweeping: the names a real upload and a real download
    give their copies, stranded, are what the sweep removes."""
    db = tmp_path / "claudia.db"
    _empty_db(db)
    written = []

    class RecordingUpload:
        """Record the snapshot's path, as Drive would be handed it."""

        def __init__(self, filename, mimetype=None):
            """Keep the path."""
            written.append(Path(filename).name)

    class RecordingDownloader:
        """Record the download's temporary path and yield a valid database."""

        def __init__(self, buf, _req):
            """Keep the buffer's path and fill it."""
            written.append(Path(buf.name).name)
            buf.write(_valid_db_bytes(tmp_path / "src", "x"))

        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    (tmp_path / "src").mkdir()
    with _drive_accepting(sync), patch("claudia.gdrive_sync.MediaFileUpload", RecordingUpload):
        assert sync.upload_db(db) is True
    with (
        _drive_serving(sync, b"", "2099-01-01T00:00:00.000Z"),
        patch("claudia.gdrive_sync.MediaIoBaseDownload", RecordingDownloader),
    ):
        assert sync.download_db(tmp_path / "fresh" / "claudia.db") is True
    assert len(written) == 2

    _plant(tmp_path, [written[0], written[0] + "-journal"])
    _plant(tmp_path / "fresh", [written[1], written[1] + "-wal"])
    with _drive_accepting(sync):
        assert sync.upload_db(db) is True
    with (
        patch.object(sync, "_find_file", return_value=None),
        patch.object(sync, "_get_service", return_value=MagicMock()),
        patch.object(sync, "_resolve_db_folder", return_value="folder-id"),
    ):
        sync.download_db(tmp_path / "fresh" / "claudia.db")

    assert (
        not (tmp_path / written[0]).exists() and not (tmp_path / (written[0] + "-journal")).exists()
    )
    assert not (tmp_path / "fresh" / written[1]).exists()
    assert not (tmp_path / "fresh" / (written[1] + "-wal")).exists()
