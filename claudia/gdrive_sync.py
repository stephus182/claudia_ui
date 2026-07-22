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
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from ibkr_core_mcp.config import Config
from ibkr_core_mcp.gdrive_auth import load_or_refresh_credentials

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_DB_FILENAME = "claudia.db"


class GDriveSync:
    """Sync claudia.db (and optionally context/principles) to Google Drive."""

    def __init__(self, config: Config) -> None:
        """Initialise sync state. Call download_db() / upload_db() to trigger actual I/O.

        _resolved_db_folder is an empty string until the first call that needs the folder
        (lazy resolution via _resolve_db_folder). RLock is reentrant because upload_db()
        calls _find_file() which calls _get_service() — all three acquire the same lock.
        """
        self._config = config
        self._service: Any = None
        self._resolved_db_folder: str = ""
        self._lock = threading.RLock()

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

    def _resolve_db_folder(self) -> str:
        """Return the Drive folder ID for claudia.db, auto-creating 'db/' if needed.

        Uses files().list() to search by name within the root folder (not a recursive
        search — parent constraint scopes it). On first run the 'db/' subfolder does not
        exist, so files().create() with mimeType=application/vnd.google-apps.folder creates
        it. Result is cached in _resolved_db_folder for the process lifetime.

        Source: https://developers.google.com/drive/api/reference/rest/v3/files/list
        Source: https://developers.google.com/drive/api/reference/rest/v3/files/create
        """
        if self._config.gdrive_db_folder_id:
            return self._config.gdrive_db_folder_id
        if self._resolved_db_folder:
            return self._resolved_db_folder
        svc = self._get_service()
        parent = self._config.gdrive_folder_id
        results = (
            svc.files()
            .list(
                q=(
                    f"name='db' and '{parent}' in parents "
                    "and mimeType='application/vnd.google-apps.folder' and trashed=false"
                ),
                fields="files(id)",
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            self._resolved_db_folder = files[0]["id"]
        else:
            meta = {
                "name": "db",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent],
            }
            f = svc.files().create(body=meta, fields="id").execute()
            self._resolved_db_folder = f["id"]
            log.info("Created 'db/' subfolder in Drive for claudia.db")
        return self._resolved_db_folder

    @staticmethod
    def _download_chunked(downloader: MediaIoBaseDownload) -> None:
        done = False
        while not done:
            _, done = downloader.next_chunk()

    def _find_file(self, name: str, folder_id: str | None = None) -> str | None:
        """Return Drive file ID for name in folder_id (default: root folder), or None.

        The `trashed=false` clause is required — Drive search includes trashed files by
        default, and a recently-deleted claudia.db would be returned without it.

        Source: https://developers.google.com/drive/api/reference/rest/v3/files/list
        """
        svc = self._get_service()
        fid = folder_id if folder_id is not None else self._config.gdrive_folder_id
        # name is always a hardcoded constant ("claudia.db", "context.md", "principles.md")
        # fid is a Drive folder ID from config or a previous files().create() response.
        # Neither is user-controlled — do not add a parameter that accepts external input here
        # without sanitizing the value first (single quote in name breaks the query).
        results = (
            svc.files()
            .list(
                q=f"name='{name}' and '{fid}' in parents and trashed=false",
                fields="files(id)",
            )
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None

    def ping(self) -> bool:
        """Return True if Drive API is reachable and credentials are valid.

        Source: https://developers.google.com/drive/api/reference/rest/v3/files/list
        """
        try:
            svc = self._get_service()
            svc.files().list(pageSize=1, fields="files(id)").execute()
            return True
        except Exception:
            return False

    def download_db(self, local_path: Path) -> bool:
        """Download claudia.db from Drive to local_path.

        Returns True if found and downloaded; False if not on Drive (first run).
        On error: logs warning, returns False — caller continues with local/empty DB.

        Download uses MediaIoBaseDownload with next_chunk() for streaming — same chunked
        approach as the Google Drive Python client docs recommend for binary files.
        A temp file is used so a failed download never overwrites a good local copy.
        PRAGMA integrity_check validates the downloaded file before it replaces local.

        Source (files.get_media): https://developers.google.com/drive/api/reference/rest/v3/files/get
        Source (MediaIoBaseDownload): https://developers.google.com/drive/api/guides/manage-downloads
        """
        try:
            svc = self._get_service()
            db_folder = self._resolve_db_folder()
            file_id = self._find_file(_DB_FILENAME, db_folder)
            if file_id is None:
                log.info("claudia.db not found on Drive (first run or not yet uploaded)")
                return False

            # Freshness guard: if the previous end-session upload failed and the
            # process restarted, the Drive copy is OLDER than the local DB —
            # replacing it would silently lose the last session. Compare Drive
            # modifiedTime (RFC 3339) against the local mtime (including the -wal
            # sidecar: in WAL mode the main file's mtime does not advance on writes).
            # Source: https://developers.google.com/drive/api/reference/rest/v3/files
            if local_path.exists():
                meta = svc.files().get(fileId=file_id, fields="modifiedTime").execute()
                drive_mtime = datetime.fromisoformat(meta["modifiedTime"])
                local_ts = local_path.stat().st_mtime
                wal = Path(str(local_path) + "-wal")
                if wal.exists():
                    local_ts = max(local_ts, wal.stat().st_mtime)
                local_mtime = datetime.fromtimestamp(local_ts, tz=UTC)
                if local_mtime > drive_mtime:
                    log.warning(
                        "claudia.db on Drive (%s) is older than local (%s) — keeping "
                        "local; it will sync to Drive at session end",
                        drive_mtime.isoformat(), local_mtime.isoformat(),
                    )
                    return False

            local_path.parent.mkdir(parents=True, exist_ok=True)
            # delete=False + explicit close() below is deliberate: the path is reused after
            # closing (sqlite3.connect, then shutil.move) — a `with` block would keep the fd
            # open across that. try/except below unlinks it on any failure.
            tmp_fd = tempfile.NamedTemporaryFile(  # noqa: SIM115
                dir=local_path.parent, suffix=".db.tmp", delete=False
            )
            tmp_path = Path(tmp_fd.name)
            try:
                downloader = MediaIoBaseDownload(
                    tmp_fd, svc.files().get_media(fileId=file_id)
                )
                self._download_chunked(downloader)
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
                        tmp_path.unlink(missing_ok=True)
                        return False
                except sqlite3.DatabaseError as exc:
                    log.warning("claudia.db from Drive is corrupt (%s) — ignoring", exc)
                    tmp_path.unlink(missing_ok=True)
                    return False
                finally:
                    conn.close()

                # Remove stale WAL/SHM sidecars from a crashed prior run BEFORE the
                # new file lands — SQLite would otherwise replay old WAL frames into
                # the freshly downloaded database on first open.
                Path(str(local_path) + "-wal").unlink(missing_ok=True)
                Path(str(local_path) + "-shm").unlink(missing_ok=True)
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
        """Upload local_path as claudia.db to Drive (create or update in-place).

        On error: logs warning — local copy is preserved, data not lost.

        files().update() patches an existing file's content without changing metadata or
        sharing settings. files().create() is only called when no file exists yet (first
        upload). The lock prevents a duplicate-file race when two sessions close concurrently.

        Source (files.update): https://developers.google.com/drive/api/reference/rest/v3/files/update
        Source (files.create): https://developers.google.com/drive/api/reference/rest/v3/files/create
        Source (MediaFileUpload): https://developers.google.com/drive/api/guides/manage-uploads
        """
        if not local_path.exists():
            log.warning("GDriveSync.upload_db: %s not found — nothing to upload", local_path)
            return
        snapshot: Path | None = None
        try:
            svc = self._get_service()
            db_folder = self._resolve_db_folder()
            # The DB runs in WAL mode: recent commits live in claudia.db-wal, not the
            # main file, and uploading the raw file while another connection checkpoints
            # risks a torn read. sqlite3's online backup API copies a consistent
            # snapshot (main file + committed WAL pages) even while other connections
            # are active — upload that snapshot, never the live file.
            # Source: https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup
            # delete=False + explicit close() below is deliberate — same reused-path pattern
            # as download_db(); the outer finally unlinks the snapshot unconditionally.
            tmp_fd = tempfile.NamedTemporaryFile(  # noqa: SIM115
                dir=local_path.parent, suffix=".upload.tmp", delete=False
            )
            tmp_fd.close()
            snapshot = Path(tmp_fd.name)
            src = sqlite3.connect(str(local_path))
            try:
                dst = sqlite3.connect(str(snapshot))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            media = MediaFileUpload(str(snapshot), mimetype="application/x-sqlite3")
            # Lock around find+create/update to prevent duplicate-file race when two
            # sessions close concurrently and both observe file_id=None simultaneously.
            with self._lock:
                file_id = self._find_file(_DB_FILENAME, db_folder)
                if file_id:
                    svc.files().update(fileId=file_id, media_body=media).execute()
                else:
                    metadata = {"name": _DB_FILENAME, "parents": [db_folder]}
                    svc.files().create(body=metadata, media_body=media, fields="id").execute()
            log.info("Uploaded claudia.db to Drive")
        except Exception as exc:
            log.warning("GDriveSync.upload_db failed: %s — local copy preserved", exc)
        finally:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)

    _MAX_TEXT_BYTES = 1 * 1024 * 1024  # 1 MB — generous for context/principles docs

    def read_text(self, filename: str, local_path: Path | None = None) -> str | None:
        """Download a text file (e.g. "context.md") from Drive.

        Returns content string, or None if not found, if Drive's copy is not newer than
        the local file (freshness guard), or on any error.

        Freshness guard: if local_path is given and exists, Drive's copy is skipped when
        its modifiedTime is not newer than the local file's mtime — mirrors download_db's
        identical guard for claudia.db. Closes a gap found live 2026-07-10: with no guard,
        a stale Drive copy could silently overwrite a newer local context.md/principles.md
        edit that was never re-uploaded, reverting ClaudIA's persona without warning.

        files().get(fields="size,modifiedTime") fetches only file metadata — avoids
        downloading the content twice. The 1 MB guard prevents a runaway context.md from
        bloating the system prompt.

        Source (files.get): https://developers.google.com/drive/api/reference/rest/v3/files/get
        Source (files.get_media): https://developers.google.com/drive/api/reference/rest/v3/files/get
        """
        try:
            svc = self._get_service()
            file_id = self._find_file(filename)
            if file_id is None:
                return None
            meta = svc.files().get(fileId=file_id, fields="size,modifiedTime").execute()
            if local_path is not None and local_path.exists():
                drive_mtime = datetime.fromisoformat(meta["modifiedTime"])
                local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=UTC)
                # >= (tie-inclusive), unlike download_db's strict >: context.md/principles.md
                # are hand-edited at human cadence, so an exact mtime tie means "no local
                # edit since last sync" far more often than a real race — skip is the safer
                # default here. download_db stays strict because DB writes are frequent and
                # machine-generated, where a tie is genuinely ambiguous. Do not "unify" these.
                if local_mtime >= drive_mtime:
                    log.warning(
                        "GDriveSync.read_text(%r): Drive copy (%s) is not newer than local "
                        "(%s) — keeping local",
                        filename, drive_mtime.isoformat(), local_mtime.isoformat(),
                    )
                    return None
            size = int(meta.get("size", 0))
            if size > self._MAX_TEXT_BYTES:
                log.warning(
                    "GDriveSync.read_text(%r): file is %d bytes (limit %d) — skipping",
                    filename, size, self._MAX_TEXT_BYTES,
                )
                return None
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
            self._download_chunked(downloader)
            return buf.getvalue().decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning(
                "GDriveSync.read_text(%r) failed: %s — using local fallback", filename, exc
            )
            return None
