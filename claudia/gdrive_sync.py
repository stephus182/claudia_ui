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
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from ibkr_core_mcp.config import Config

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_DB_FILENAME = "claudia.db"


class GDriveSync:
    """Sync claudia.db (and optionally context/principles) to Google Drive."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._service: Any = None
        self._resolved_db_folder: str = ""
        self._lock = threading.RLock()

    def _get_service(self) -> Any:
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

    def _resolve_db_folder(self) -> str:
        """Return the Drive folder ID for claudia.db, auto-creating 'db/' if needed."""
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
    def _download_chunked(downloader: "MediaIoBaseDownload") -> None:
        done = False
        while not done:
            _, done = downloader.next_chunk()

    def _find_file(self, name: str, folder_id: str | None = None) -> str | None:
        """Return Drive file ID for name in folder_id (default: root folder), or None."""
        svc = self._get_service()
        fid = folder_id if folder_id is not None else self._config.gdrive_folder_id
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

    def download_db(self, local_path: Path) -> bool:
        """
        Download claudia.db from Drive to local_path.

        Returns True if found and downloaded; False if not on Drive (first run).
        On error: logs warning, returns False — caller continues with local/empty DB.
        """
        try:
            svc = self._get_service()
            db_folder = self._resolve_db_folder()
            file_id = self._find_file(_DB_FILENAME, db_folder)
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
            svc = self._get_service()
            db_folder = self._resolve_db_folder()
            media = MediaFileUpload(str(local_path), mimetype="application/x-sqlite3")
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

    _MAX_TEXT_BYTES = 1 * 1024 * 1024  # 1 MB — generous for context/principles docs

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
            meta = svc.files().get(fileId=file_id, fields="size").execute()
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
