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
                token_path = str(self._config.gdrive_token_file)
                fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(creds.to_json())
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
