"""
Security regression tests for the 2026-06-12 audit (commit 3927dcd).
Each test corresponds to one of the 8 resolved findings.
These tests MUST stay green — a failure here means a security control was regressed.
"""

import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fix #1 + #3 — env allowlist: secrets not in subprocess, CDP port present ──

@pytest.mark.asyncio
async def test_sidecar_subprocess_env_excludes_secrets(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY and IBKR_FLEX_TOKEN must not appear in the sidecar env (Fix #1)."""
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "ibkr-secret-token")
    monkeypatch.setenv("GDRIVE_TOKEN_FILE", "/secret/gdrive-token.json")

    captured_env = {}

    def fake_params(**kwargs):
        captured_env.update(kwargs.get("env", {}))
        return MagicMock()

    class FakeCM:
        async def __aenter__(self): return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a): pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    from claudia.tradingview import TradingViewBridge
    with patch("claudia.tradingview.StdioServerParameters", side_effect=fake_params), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        await TradingViewBridge().start()

    assert captured_env, "StdioServerParameters was never called — env not captured"
    assert "ANTHROPIC_API_KEY" not in captured_env, "ANTHROPIC_API_KEY leaked to subprocess!"
    assert "IBKR_FLEX_TOKEN" not in captured_env, "IBKR_FLEX_TOKEN leaked to subprocess!"
    assert "GDRIVE_TOKEN_FILE" not in captured_env, "GDRIVE_TOKEN_FILE leaked to subprocess!"
    assert "CHROME_REMOTE_DEBUG_PORT" in captured_env, "CHROME_REMOTE_DEBUG_PORT missing from env (Fix #3)!"


# ── Fix #4 — os.chmod called after token file refresh ────────────────────────

def test_gdrive_token_file_chmod_after_refresh(tmp_path):
    """Token file must be chmod 0o600 after every credential refresh (Fix #4)."""
    from claudia.gdrive_sync import GDriveSync

    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    token_file.chmod(0o644)  # simulate loose permissions (google-auth-oauthlib default)

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder-id"
    cfg.gdrive_token_file = token_file

    sync = GDriveSync(cfg)

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "rt"
    mock_creds.to_json.return_value = '{"access_token": "new"}'

    with patch("claudia.gdrive_sync.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("claudia.gdrive_sync.Request"), \
         patch("claudia.gdrive_sync.build"):
        sync._get_service()

    mode = oct(token_file.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Token file permissions {mode} != 0o600 after refresh"


# ── Fix #5 — read_text() size guard ──────────────────────────────────────────

def test_read_text_rejects_oversized_file():
    """Files > 1 MB must be rejected without downloading (Fix #5)."""
    from claudia.gdrive_sync import GDriveSync

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder-id"
    cfg.gdrive_token_file = Path("/fake/token.json")
    sync = GDriveSync(cfg)

    large_size = 2 * 1024 * 1024  # 2 MB
    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {"size": str(large_size)}

    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc):
        result = sync.read_text("context.md")

    assert result is None
    svc.files.return_value.get_media.assert_not_called()


def test_read_text_accepts_file_under_limit():
    """Files <= 1 MB must be downloaded normally (Fix #5)."""
    from claudia.gdrive_sync import GDriveSync

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder-id"
    cfg.gdrive_token_file = Path("/fake/token.json")
    sync = GDriveSync(cfg)

    content = "# Role\nI am ClaudIA."
    small_size = len(content.encode())

    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(content.encode())
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {"size": str(small_size)}

    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == content


# ── Fix #6 — GDriveSync has threading.Lock ───────────────────────────────────

def test_gdrive_sync_has_lock():
    """GDriveSync must have a _lock attribute for thread safety (Fix #6)."""
    from claudia.gdrive_sync import GDriveSync
    cfg = MagicMock()
    cfg.gdrive_token_file = Path("/fake/token.json")
    sync = GDriveSync(cfg)
    assert hasattr(sync, "_lock"), "GDriveSync missing _lock — thread safety removed"
    assert isinstance(sync._lock, type(threading.Lock())), "_lock must be a threading.Lock"


def test_upload_db_is_protected_by_lock(tmp_path):
    """upload_db() must acquire _lock during the find+create/update block (Fix #6)."""
    from claudia.gdrive_sync import GDriveSync

    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder-id"
    cfg.gdrive_db_folder_id = "db-folder-id"
    cfg.gdrive_token_file = tmp_path / "token.json"
    sync = GDriveSync(cfg)

    # Replace _lock with a tracking wrapper — _thread.lock.acquire is read-only
    # in CPython 3.14+, so we substitute a whole MagicMock that delegates to a
    # real lock so the context-manager protocol still works correctly.
    real_lock = threading.Lock()
    lock_acquired = []

    class TrackingLock:
        def acquire(self, *args, **kwargs):
            lock_acquired.append(True)
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *a):
            self.release()

    sync._lock = TrackingLock()

    svc = MagicMock()
    svc.files.return_value.update.return_value.execute.return_value = {}

    with patch.object(sync, "_find_file", return_value="existing-file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaFileUpload"), \
         patch.object(sync, "_resolve_db_folder", return_value="db-folder-id"):
        sync.upload_db(db)

    assert lock_acquired, "upload_db() never acquired _lock — race condition possible"


# ── Fix #7 — TRADINGVIEW_MCP_PATH validation ─────────────────────────────────

def test_tradingview_mcp_path_non_js_ignored(tmp_path, monkeypatch):
    """TRADINGVIEW_MCP_PATH with a .sh extension must be rejected (Fix #7)."""
    bad_path = tmp_path / "server.sh"
    bad_path.write_text("#!/bin/bash")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(bad_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    import claudia.tradingview as tv_module
    from claudia.tradingview import _find_tv_mcp_bin
    with patch("claudia.tradingview.shutil.which", return_value=None), \
         patch.object(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py")):
        result = _find_tv_mcp_bin()
    assert result is None, ".sh path must be rejected — only .js paths are valid"


def test_tradingview_mcp_path_nonexistent_ignored(tmp_path, monkeypatch):
    """TRADINGVIEW_MCP_PATH pointing to a missing file must be rejected (Fix #7)."""
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(tmp_path / "ghost.js"))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    import claudia.tradingview as tv_module
    from claudia.tradingview import _find_tv_mcp_bin
    with patch("claudia.tradingview.shutil.which", return_value=None), \
         patch.object(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py")):
        result = _find_tv_mcp_bin()
    assert result is None, "Nonexistent path must be rejected"
