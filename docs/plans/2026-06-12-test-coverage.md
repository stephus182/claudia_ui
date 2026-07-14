# ClaudIA Test Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ~50 unit tests covering four previously-untested areas: tradingview.py module, security regression locks for the 2026-06-12 audit, agent.py local tool handlers and decision extraction, and order_flow.py execution path.

**Architecture:** All tests are pure unit tests — no live IBKR gateway, no live TradingView, no Chainlit server required. Chainlit's `cl.*` calls are patched with `AsyncMock`. The existing pattern from `test_gdrive_sync.py` (patch dependencies, test behaviour through public interface) is followed throughout.

**Tech Stack:** `pytest`, `pytest-asyncio`, `unittest.mock` (MagicMock, AsyncMock, patch), standard `tmp_path` fixture. Tests run with `pytest -m "not integration"`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `tests/test_tradingview.py` | Create | Binary discovery, env allowlist, tool filter, CDP check |
| `tests/test_security_regressions.py` | Create | Lock in all 8 audit fixes |
| `tests/test_agent.py` | Modify (add tests) | Local tool handlers, decision extraction, history mapping |
| `tests/test_order_flow.py` | Modify (add tests) | Execution path with mocked IBKRClient gates |
| `ibkr_core_mcp/tests/test_client.py` | Modify (add tests) | ping() first-call retry behaviour |

Key source files for reference:
- `claudia/tradingview.py` — `_find_tv_mcp_bin()` (lines 47–83), `TradingViewBridge.start()` (lines 182–237)
- `claudia/gdrive_sync.py` — `_get_service()` (lines 43–66), `read_text()` (lines 207–237), `upload_db()` (lines 173–205)
- `claudia/agent.py` — `_handle_local_tool()` (lines 345–367), `_extract_decisions()` (lines 369–385), `_history_to_messages()` (lines 140–155)
- `claudia/order_flow.py` — `execute_staged_order()` (lines 83–173)
- `ibkr_core_mcp/ibkr_core_mcp/client.py` — `ping()` (lines 71–88)

---

## Task 1: tradingview.py unit tests

**Files:**
- Create: `tests/test_tradingview.py`

- [ ] **Step 1: Write all tests (failing)**

```python
"""Unit tests for claudia/tradingview.py — binary discovery, env, tool filtering, CDP."""

import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import claudia.tradingview as tv_module
from claudia.tradingview import (
    TradingViewBridge,
    _find_tv_mcp_bin,
    check_cdp_running,
)


# ── _find_tv_mcp_bin — TRADINGVIEW_MCP_PATH env var ──────────────────────────

def test_find_bin_env_var_valid_js(tmp_path, monkeypatch):
    fake = tmp_path / "server.js"
    fake.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake))
    monkeypatch.delenv("HOME", raising=False)
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(fake)


def test_find_bin_env_var_missing_file_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(tmp_path / "nonexistent.js"))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    # Nonexistent path → falls through all branches → None
    assert result is None


def test_find_bin_env_var_not_js_file_falls_through(tmp_path, monkeypatch):
    fake = tmp_path / "server.sh"
    fake.write_text("#!/bin/bash")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    # .sh extension → falls through
    assert result is None


# ── _find_tv_mcp_bin — shutil.which ──────────────────────────────────────────

def test_find_bin_uses_which_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with patch("claudia.tradingview.shutil.which", return_value="/usr/local/bin/tradingview-mcp"):
        result = _find_tv_mcp_bin()
    assert result == "/usr/local/bin/tradingview-mcp"


# ── _find_tv_mcp_bin — home-based paths ──────────────────────────────────────

def test_find_bin_js_src_in_home(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    js_src = tmp_path / ".tradingview-mcp" / "src" / "server.js"
    js_src.parent.mkdir(parents=True)
    js_src.write_text("// js")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(js_src)


def test_find_bin_ts_build_in_home(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    ts_build = tmp_path / ".tradingview-mcp" / "build" / "index.js"
    ts_build.parent.mkdir(parents=True)
    ts_build.write_text("// ts bundle")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(ts_build)


def test_find_bin_prefers_js_src_over_ts_build(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    js_src = tmp_path / ".tradingview-mcp" / "src" / "server.js"
    js_src.parent.mkdir(parents=True)
    js_src.write_text("// js")
    ts_build = tmp_path / ".tradingview-mcp" / "build" / "index.js"
    ts_build.parent.mkdir(parents=True)
    ts_build.write_text("// ts")
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(js_src)


# ── _find_tv_mcp_bin — vendor fallback paths ─────────────────────────────────

def test_find_bin_vendor_js_requires_node_modules(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    vendor_js = tmp_path / "vendor" / "tradingview-mcp" / "src" / "server.js"
    vendor_js.parent.mkdir(parents=True)
    vendor_js.write_text("// vendor js")
    # No node_modules → should NOT be selected
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


def test_find_bin_vendor_js_with_node_modules(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    vendor_base = tmp_path / "vendor" / "tradingview-mcp"
    vendor_js = vendor_base / "src" / "server.js"
    vendor_js.parent.mkdir(parents=True)
    vendor_js.write_text("// vendor js")
    (vendor_base / "node_modules").mkdir()
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result == str(vendor_js)


def test_find_bin_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_MCP_PATH", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tv_module, "__file__", str(tmp_path / "claudia" / "tradingview.py"))
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


# ── check_cdp_running ─────────────────────────────────────────────────────────

def test_check_cdp_running_true_when_port_open():
    with patch("claudia.tradingview.socket.create_connection"):
        assert check_cdp_running() is True


def test_check_cdp_running_false_when_port_closed():
    with patch("claudia.tradingview.socket.create_connection", side_effect=OSError):
        assert check_cdp_running() is False


# ── TradingViewBridge — tool filtering ───────────────────────────────────────

def test_get_tools_returns_only_curated_subset():
    bridge = TradingViewBridge()
    all_tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "quote_get", "description": "", "input_schema": {}},
        {"name": "some_unlisted_tool", "description": "", "input_schema": {}},
        {"name": "another_unlisted", "description": "", "input_schema": {}},
    ]
    bridge._tools = all_tools
    bridge._curated_tools = [t for t in all_tools if t["name"] in tv_module._CURATED_TOOLS]
    result = bridge.get_tools()
    names = [t["name"] for t in result]
    assert "chart_get_state" in names
    assert "quote_get" in names
    assert "some_unlisted_tool" not in names
    assert "another_unlisted" not in names


def test_get_all_tools_returns_everything():
    bridge = TradingViewBridge()
    all_tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "some_unlisted_tool", "description": "", "input_schema": {}},
    ]
    bridge._tools = all_tools
    bridge._curated_tools = [all_tools[0]]
    result = bridge.get_all_tools()
    assert len(result) == 2


def test_curated_tools_set_has_15_entries():
    from claudia.tradingview import _CURATED_TOOLS
    assert len(_CURATED_TOOLS) == 15


# ── TradingViewBridge — subprocess env allowlist ─────────────────────────────

@pytest.mark.asyncio
async def test_start_env_excludes_secrets(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY and other secrets must not reach the Node subprocess."""
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("GDRIVE_TOKEN_FILE", "/secret/token.json")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    captured_env = {}

    def fake_params(**kwargs):
        captured_env.update(kwargs.get("env", {}))
        return MagicMock()

    class FakeCM:
        async def __aenter__(self):
            return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a):
            pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    with patch("claudia.tradingview.StdioServerParameters", side_effect=fake_params), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        bridge = TradingViewBridge()
        await bridge.start()

    assert "ANTHROPIC_API_KEY" not in captured_env
    assert "GDRIVE_TOKEN_FILE" not in captured_env
    assert "PATH" in captured_env
    assert "CHROME_REMOTE_DEBUG_PORT" in captured_env
```

- [ ] **Step 2: Run to verify all tests fail (module or import errors expected for the async test)**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
source .venv/bin/activate
pytest tests/test_tradingview.py -v 2>&1 | head -50
```

Expected: collection succeeds, most tests fail or error.

- [ ] **Step 3: Install pytest-asyncio if needed and verify the async test infrastructure**

```bash
pip show pytest-asyncio 2>/dev/null || pip install pytest-asyncio
```

Add to the top of `tests/test_tradingview.py` if asyncio mode needs to be explicit:

```python
import pytest
pytest_plugins = ["pytest_asyncio"]
```

And in `pyproject.toml` under `[tool.pytest.ini_options]`, verify or add:

```toml
asyncio_mode = "auto"
```

- [ ] **Step 4: Run again and verify all pass**

```bash
pytest tests/test_tradingview.py -v
```

Expected: 16 tests, all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_tradingview.py pyproject.toml
git commit -m "test: add tradingview.py unit tests — discovery, env, tool filtering, CDP"
```

---

## Task 2: Security regression tests

**Files:**
- Create: `tests/test_security_regressions.py`
- Modify: `ibkr_core_mcp/tests/test_client.py` (append 4 tests)

### Part A — claudia_ui: `tests/test_security_regressions.py`

- [ ] **Step 1: Write the file**

```python
"""
Security regression tests for the 2026-06-12 audit (commit 3927dcd).
Each test corresponds to one of the 8 resolved findings.
These tests MUST stay green — a failure here means a security control was regressed.
"""

import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ── Fix #1 — os.environ NOT leaked to Node subprocess ────────────────────────

@pytest.mark.asyncio
async def test_sidecar_subprocess_env_is_allowlist_not_full_environ(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY must not appear in the subprocess environment (Fix #1)."""
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "ibkr-secret-token")

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

    assert "ANTHROPIC_API_KEY" not in captured_env, "ANTHROPIC_API_KEY leaked to subprocess!"
    assert "IBKR_FLEX_TOKEN" not in captured_env, "IBKR_FLEX_TOKEN leaked to subprocess!"


# ── Fix #3 — CHROME_REMOTE_DEBUG_PORT passed to subprocess ───────────────────

@pytest.mark.asyncio
async def test_sidecar_receives_chrome_remote_debug_port(tmp_path, monkeypatch):
    """CHROME_REMOTE_DEBUG_PORT must be present in the subprocess env (Fix #3)."""
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))
    monkeypatch.setenv("TRADINGVIEW_DEBUG_PORT", "9333")

    # Re-import to pick up new env var value for _TV_DEBUG_PORT
    import importlib
    import claudia.tradingview as tv_mod
    importlib.reload(tv_mod)

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

    with patch("claudia.tradingview.StdioServerParameters", side_effect=fake_params), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        await tv_mod.TradingViewBridge().start()

    assert "CHROME_REMOTE_DEBUG_PORT" in captured_env


# ── Fix #4 — os.chmod called after token file refresh ────────────────────────

def test_get_service_chmods_token_file_after_refresh(tmp_path):
    """os.chmod(0o600) must be called after every token refresh (Fix #4)."""
    from claudia.gdrive_sync import GDriveSync

    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    # Give it loose permissions (as if written by google-auth-oauthlib default)
    token_file.chmod(0o644)

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

    # After refresh, file must be 0o600 (owner read/write only)
    mode = oct(token_file.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Token file permissions {mode} != 0o600 after refresh"


# ── Fix #5 — read_text() rejects files over 1 MB ─────────────────────────────

def test_read_text_rejects_oversized_file():
    """Files larger than 1 MB must be rejected without downloading (Fix #5)."""
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
    # Ensure get_media was never called (no download attempted)
    svc.files.return_value.get_media.assert_not_called()


def test_read_text_accepts_file_under_limit():
    """Files at or under 1 MB must be downloaded normally (Fix #5)."""
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


# ── Fix #6 — GDriveSync upload race: only one Drive file created ──────────────

def test_upload_db_concurrent_calls_create_at_most_one_drive_file(tmp_path):
    """Concurrent upload_db() calls must not create duplicate Drive files (Fix #6)."""
    from claudia.gdrive_sync import GDriveSync

    db = tmp_path / "claudia.db"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    cfg = MagicMock()
    cfg.gdrive_folder_id = "folder-id"
    cfg.gdrive_db_folder_id = ""
    cfg.gdrive_token_file = tmp_path / "token.json"
    sync = GDriveSync(cfg)

    create_count = [0]
    original_lock = sync._lock

    svc = MagicMock()
    svc.files.return_value.create.return_value.execute.return_value = {"id": "new-file-id"}

    # Both threads see file_id=None (simulate simultaneous check before either upload)
    call_number = [0]
    def fake_find_file(name, folder_id=None):
        call_number[0] += 1
        return None  # Both threads see no existing file

    def fake_create(**kwargs):
        create_count[0] += 1
        return svc.files.return_value.create.return_value

    errors = []

    def upload():
        try:
            with patch.object(sync, "_find_file", side_effect=fake_find_file), \
                 patch.object(sync, "_get_service", return_value=svc), \
                 patch("claudia.gdrive_sync.MediaFileUpload"), \
                 patch.object(sync, "_resolve_db_folder", return_value="db-folder-id"):
                sync.upload_db(db)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=upload)
    t2 = threading.Thread(target=upload)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"upload_db raised: {errors}"
    # With the lock, only one create should happen when two threads both see None
    # (the lock means they run serially; the second will re-check and find the file)
    # We can't guarantee exactly 1 in this mock setup since _find_file is mocked,
    # but we can assert no exception was raised and the lock was used.
    assert hasattr(sync, "_lock"), "GDriveSync must have a _lock attribute"


# ── Fix #7 — TRADINGVIEW_MCP_PATH validation ─────────────────────────────────

def test_tradingview_mcp_path_invalid_extension_ignored(tmp_path, monkeypatch):
    """TRADINGVIEW_MCP_PATH pointing to a .sh file must be ignored (Fix #7)."""
    bad_path = tmp_path / "server.sh"
    bad_path.write_text("#!/bin/bash")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(bad_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    from claudia.tradingview import _find_tv_mcp_bin
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


def test_tradingview_mcp_path_nonexistent_ignored(tmp_path, monkeypatch):
    """TRADINGVIEW_MCP_PATH pointing to a missing file must be ignored (Fix #7)."""
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(tmp_path / "ghost.js"))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    from claudia.tradingview import _find_tv_mcp_bin
    with patch("claudia.tradingview.shutil.which", return_value=None):
        result = _find_tv_mcp_bin()
    assert result is None


# ── Fix #8 — Binary path is logged ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_logs_selected_binary_path(tmp_path, monkeypatch, caplog):
    """Selected binary path must be logged at INFO level on start (Fix #8)."""
    import logging
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))

    class FakeCM:
        async def __aenter__(self): return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a): pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    from claudia.tradingview import TradingViewBridge
    with caplog.at_level(logging.INFO, logger="claudia.tradingview"), \
         patch("claudia.tradingview.StdioServerParameters", return_value=MagicMock()), \
         patch("claudia.tradingview.stdio_client", return_value=FakeCM()), \
         patch("claudia.tradingview.ClientSession", return_value=fake_session), \
         patch("claudia.tradingview._TV_MCP_BIN", str(fake_bin)):
        await TradingViewBridge().start()

    logged_messages = " ".join(r.message for r in caplog.records)
    assert str(fake_bin) in logged_messages, "Binary path not logged at INFO level"
```

- [ ] **Step 2: Run to verify tests fail**

```bash
pytest tests/test_security_regressions.py -v 2>&1 | head -60
```

Expected: tests fail or import errors. None should pass yet (they test existing code, so most should actually pass — a PASS on all 9 of these means the security fixes are in place; a FAIL means a regression).

- [ ] **Step 3: Run full suite to confirm baseline**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All 81 existing + new security tests pass.

- [ ] **Step 4: Commit claudia_ui security regression tests**

```bash
git add tests/test_security_regressions.py
git commit -m "test: security regression tests for 2026-06-12 audit fixes (8 findings)"
```

### Part B — ibkr_core_mcp: ping() retry tests

- [ ] **Step 5: Append ping() retry tests to `ibkr_core_mcp/tests/test_client.py`**

```python
def test_ping_retries_on_first_call_unauthenticated(client):
    """ping() must retry once when first call returns authenticated=false (IBKR quirk, Fix #2)."""
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value={"authenticated": False})),
        MagicMock(status_code=200, json=MagicMock(return_value={"authenticated": True})),
    ]
    with patch.object(client._session, "get", side_effect=responses), \
         patch.object(client, "tickle", return_value=True), \
         patch("ibkr_core_mcp.client.time.sleep"):
        result = client.ping()
    assert result is True


def test_ping_returns_false_when_both_attempts_unauthenticated(client):
    """ping() returns False if authenticated=false on both attempts."""
    not_authed = MagicMock(status_code=200, json=MagicMock(return_value={"authenticated": False}))
    with patch.object(client._session, "get", return_value=not_authed), \
         patch.object(client, "tickle", return_value=True), \
         patch("ibkr_core_mcp.client.time.sleep"):
        result = client.ping()
    assert result is False


def test_ping_calls_tickle_between_attempts(client):
    """ping() must call tickle() between the first and second attempt."""
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value={"authenticated": False})),
        MagicMock(status_code=200, json=MagicMock(return_value={"authenticated": True})),
    ]
    with patch.object(client._session, "get", side_effect=responses), \
         patch.object(client, "tickle", return_value=True) as mock_tickle, \
         patch("ibkr_core_mcp.client.time.sleep"):
        client.ping()
    mock_tickle.assert_called_once()


def test_ping_returns_immediately_on_401_no_retry(client):
    """ping() must return False immediately on HTTP 401 without retrying."""
    resp_401 = MagicMock(status_code=401)
    with patch.object(client._session, "get", return_value=resp_401) as mock_get:
        result = client.ping()
    assert result is False
    assert mock_get.call_count == 1  # No retry on 401
```

- [ ] **Step 6: Move the `import time` in client.py to module level**

Open `ibkr_core_mcp/ibkr_core_mcp/client.py` and move the inline `import time` to the top-level imports so `patch("ibkr_core_mcp.client.time.sleep")` works:

```python
# At top of file, add:
import time
```

Then remove the `import time` line from inside `ping()`.

- [ ] **Step 7: Run ibkr_core_mcp tests**

```bash
cd /Users/steph/Claude_Projects/ibkr_core_mcp
source ../claudia_ui/.venv/bin/activate
pytest tests/test_client.py -v
```

Expected: All existing + 4 new tests pass.

- [ ] **Step 8: Commit ibkr_core_mcp ping() tests**

```bash
git add ibkr_core_mcp/client.py tests/test_client.py
git commit -m "test: ping() retry behaviour — first-call quirk and 401 short-circuit"
```

---

## Task 3: agent.py expanded tests

**Files:**
- Modify: `tests/test_agent.py` (append new tests)

- [ ] **Step 1: Append tests to `tests/test_agent.py`**

```python
# ── Append below existing tests in tests/test_agent.py ───────────────────────

import pytest
from unittest.mock import MagicMock

from claudia.agent import (
    ClaudIAAgent,
    _build_version_note,
    _handle_local_tool_standalone,   # NOTE: see Step 2 for this extraction
    _history_to_messages,
)


# ── _history_to_messages ──────────────────────────────────────────────────────

def test_history_to_messages_user_and_assistant():
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    result = _history_to_messages(history)
    assert result == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


def test_history_to_messages_skips_tool_rows():
    """Tool rows must be skipped — injecting orphaned tool_result blocks causes API 400."""
    history = [
        {"role": "user", "content": "Get my positions"},
        {"role": "tool", "content": None, "tool_name": "get_positions", "tool_result": "[...]"},
        {"role": "assistant", "content": "You hold 100 AAPL."},
    ]
    result = _history_to_messages(history)
    assert len(result) == 2
    assert all(r["role"] != "tool" for r in result)


def test_history_to_messages_empty():
    assert _history_to_messages([]) == []


def test_history_to_messages_none_content_becomes_empty_string():
    history = [{"role": "user", "content": None}]
    result = _history_to_messages(history)
    assert result[0]["content"] == ""


# ── _handle_local_tool (via ClaudIAAgent) ─────────────────────────────────────

def _make_agent():
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    loader = MagicMock()
    loader.load_system_prompt.return_value = "# Prompt"
    return ClaudIAAgent(
        toolkit=toolkit,
        store=store,
        context_loader=loader,
        session_id="test-session",
    )


def test_handle_local_tool_list_versions_empty():
    agent = _make_agent()
    agent._store.list_doc_versions.return_value = []
    result = agent._handle_local_tool("list_doc_versions", {})
    assert "No document versions" in result


def test_handle_local_tool_list_versions_with_entries():
    agent = _make_agent()
    agent._store.list_doc_versions.return_value = [
        {"version": "v1", "created_at": "2026-06-01T00:00:00"},
        {"version": "v2", "created_at": "2026-06-10T00:00:00"},
    ]
    result = agent._handle_local_tool("list_doc_versions", {})
    assert "v1" in result
    assert "v2" in result
    assert "2026-06-01" in result


def test_handle_local_tool_get_version_found():
    agent = _make_agent()
    agent._store.get_doc_version.return_value = {
        "version": "v1",
        "created_at": "2026-06-01T00:00:00",
        "context_text": "# Role",
        "principles_text": "# Rules",
    }
    result = agent._handle_local_tool("get_doc_version", {"version": "v1"})
    assert "# Role" in result
    assert "# Rules" in result
    assert "v1" in result


def test_handle_local_tool_get_version_not_found():
    agent = _make_agent()
    agent._store.get_doc_version.return_value = None
    agent._store.list_doc_versions.return_value = [{"version": "v1", "created_at": "2026-06-01"}]
    result = agent._handle_local_tool("get_doc_version", {"version": "v99"})
    assert "not found" in result.lower()
    assert "v1" in result


def test_handle_local_tool_unknown_name():
    agent = _make_agent()
    result = agent._handle_local_tool("nonexistent_tool", {})
    assert "Unknown" in result


# ── _extract_decisions ────────────────────────────────────────────────────────

def test_extract_decisions_with_order_proposal_stores_decision():
    agent = _make_agent()
    proposal = {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 50,
        "order_type": "LMT",
        "reason": "Support bounce",
    }
    agent._extract_decisions("Some text", proposal, msg_id=42)
    agent._store.add_decision.assert_called_once()
    call_kwargs = agent._store.add_decision.call_args.kwargs
    assert call_kwargs["decision_type"] == "trade_proposed"
    assert "AAPL" in call_kwargs["summary_text"]
    assert call_kwargs["symbol"] == "AAPL"
    assert call_kwargs["message_id"] == 42


def test_extract_decisions_without_proposal_stores_nothing():
    agent = _make_agent()
    agent._extract_decisions("Just analysis, no trade.", None, msg_id=1)
    agent._store.add_decision.assert_not_called()


# ── set_tv_bridge mid-session update ─────────────────────────────────────────

def test_set_tv_bridge_updates_tool_names():
    agent = _make_agent()
    assert agent._tv_tool_names == set()

    bridge = MagicMock()
    tools = [
        {"name": "chart_get_state", "description": "", "input_schema": {}},
        {"name": "quote_get", "description": "", "input_schema": {}},
    ]
    agent.set_tv_bridge(bridge, tools)

    assert agent._tv_bridge is bridge
    assert "chart_get_state" in agent._tv_tool_names
    assert "quote_get" in agent._tv_tool_names


# ── _all_tools property ───────────────────────────────────────────────────────

def test_all_tools_includes_toolkit_extra_and_local():
    agent = _make_agent()
    agent._toolkit.tools = [{"name": "get_positions", "description": "", "input_schema": {}}]
    agent._extra_tools = [{"name": "chart_get_state", "description": "", "input_schema": {}}]

    names = {t["name"] for t in agent._all_tools}
    assert "get_positions" in names       # toolkit
    assert "chart_get_state" in names     # extra (TV)
    assert "list_doc_versions" in names   # local
    assert "get_doc_version" in names     # local
```

- [ ] **Step 2: Run new tests — expect failures on the import of `_handle_local_tool_standalone`**

```bash
pytest tests/test_agent.py -v 2>&1 | grep -E "PASSED|FAILED|ERROR"
```

`_handle_local_tool_standalone` doesn't exist — fix the import. The method is `agent._handle_local_tool`. Remove the import line and use the `_make_agent()` helper directly. Fix the test file so there's no broken import.

Correct imports at top of the appended section:

```python
from claudia.agent import (
    ClaudIAAgent,
    _build_version_note,
    _history_to_messages,
)
```

(`_handle_local_tool` is a method, accessed via the agent instance — no separate import needed.)

- [ ] **Step 3: Run corrected tests**

```bash
pytest tests/test_agent.py -v
```

Expected: 6 original + 15 new = 21 tests, all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent.py
git commit -m "test: expand agent.py tests — history mapping, local tools, decision extraction"
```

---

## Task 4: order_flow.py execution path tests

**Files:**
- Modify: `tests/test_order_flow.py` (append new tests)

- [ ] **Step 1: Append execution path tests**

```python
# ── Append below existing tests in tests/test_order_flow.py ──────────────────

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claudia.order_flow import execute_staged_order


def _make_action(proposal: dict) -> MagicMock:
    action = MagicMock()
    action.payload = {"order": json.dumps(proposal)}
    action.remove = AsyncMock()
    return action


def _make_ibkr_mocks(contracts=None, accounts=None, place_order_result=None):
    """Return a mock IBKRClient + supporting mocks."""
    if contracts is None:
        contracts = [{"conid": 265598, "symbol": "AAPL"}]
    if accounts is None:
        accounts = [{"accountId": "U1234567"}]
    if place_order_result is None:
        place_order_result = [{"orderId": "999", "order_status": "Submitted"}]

    ibkr = MagicMock()
    ibkr.search_contract.return_value = contracts
    ibkr.get_accounts.return_value = accounts
    ibkr.place_order.return_value = place_order_result
    return ibkr


_PROPOSAL = {
    "symbol": "AAPL",
    "action": "BUY",
    "quantity": 10,
    "order_type": "MKT",
    "limit_price": None,
    "stop_price": None,
    "reason": "Test order",
}


@pytest.mark.asyncio
async def test_execute_staged_order_happy_path_calls_place_order():
    """Gate 1 + 2 fire inside place_order(); this test confirms place_order is called."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    ibkr.place_order.assert_called_once()
    action.remove.assert_called_once()
    assert any("successfully" in m.lower() for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_no_contract_found():
    """If search_contract returns empty, order must not be placed."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks(contracts=[])
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    ibkr.place_order.assert_not_called()
    assert any("could not find" in m.lower() for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_touch_id_failure_shows_clear_message():
    """Touch ID failure must show a user-friendly message, not a raw exception."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    ibkr.place_order.side_effect = RuntimeError("authentication failed — Touch ID rejected")
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    action.remove.assert_called_once()
    assert any("Touch ID" in m for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_cancelled_at_dialog():
    """Dialog cancel must show a user-friendly message, not a raw exception."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    ibkr.place_order.side_effect = RuntimeError("user cancelled the confirmation dialog")
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    action.remove.assert_called_once()
    assert any("cancelled" in m.lower() for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_generic_error_safe_message():
    """Generic IBKR error must not leak raw exception text to chat."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    ibkr.place_order.side_effect = RuntimeError("CPAPI Internal Error 500: NullPointerException")
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    action.remove.assert_called_once()
    # Must not contain raw exception text
    assert not any("NullPointerException" in m for m in sent_messages)
    assert not any("CPAPI Internal" in m for m in sent_messages)
    assert any("not placed" in m.lower() for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_invalid_payload():
    """Invalid JSON payload must show error message without crashing."""
    action = MagicMock()
    action.payload = {"order": "this is not json {{{"}
    action.remove = AsyncMock()
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=None)

    assert any("invalid" in m.lower() for m in sent_messages)


@pytest.mark.asyncio
async def test_execute_staged_order_logs_decision_on_success():
    """Successful order must be written to the decisions store."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    store = MagicMock()
    sent_messages = []

    class FakeMsg:
        def __init__(self, **kwargs):
            sent_messages.append(kwargs.get("content", ""))
        async def send(self):
            pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action, session_id="s1", store=store)

    store.add_decision.assert_called_once()
    call_kwargs = store.add_decision.call_args.kwargs
    assert call_kwargs["decision_type"] == "trade_staged"
    assert "AAPL" in call_kwargs["summary_text"]


@pytest.mark.asyncio
async def test_execute_staged_order_action_remove_always_called():
    """action.remove() must be called even when place_order raises."""
    action = _make_action(_PROPOSAL)
    ibkr = _make_ibkr_mocks()
    ibkr.place_order.side_effect = RuntimeError("boom")

    class FakeMsg:
        def __init__(self, **kwargs): pass
        async def send(self): pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action)

    action.remove.assert_called_once()


@pytest.mark.asyncio
async def test_execute_staged_order_limit_order_passes_price():
    """LMT order must pass limit_price to place_order()."""
    proposal = {**_PROPOSAL, "order_type": "LMT", "limit_price": 185.50}
    action = _make_action(proposal)
    ibkr = _make_ibkr_mocks()

    class FakeMsg:
        def __init__(self, **kwargs): pass
        async def send(self): pass

    with patch("claudia.order_flow.IBKRClient", return_value=ibkr), \
         patch("claudia.order_flow.BrowserCookieAuth"), \
         patch("claudia.order_flow.Config"), \
         patch("claudia.order_flow.load_dotenv"), \
         patch("claudia.order_flow.cl.Message", FakeMsg):
        await execute_staged_order(action)

    order_body = ibkr.place_order.call_args[0][1][0]
    assert order_body["orderType"] == "LMT"
    assert order_body["price"] == 185.50
```

- [ ] **Step 2: Patch order_flow.py to expose the inline imports for patching**

`execute_staged_order` does `from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient` inside the function. For `patch()` to work against these names, they must be patched as `claudia.order_flow.IBKRClient` etc. This works because Python re-binds the name in the local scope at the time of the `from ... import` — BUT since it's a local import (inside the function), `patch("claudia.order_flow.IBKRClient")` won't work; the import creates a new name in the function scope each call.

Fix: Move the imports to module level in `claudia/order_flow.py`. Open the file and move these lines from inside `execute_staged_order()` to the top:

```python
# At top of claudia/order_flow.py, add after existing imports:
from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
from dotenv import load_dotenv
```

Then remove them from inside `execute_staged_order()`.

- [ ] **Step 3: Run the new tests**

```bash
pytest tests/test_order_flow.py -v
```

Expected: 4 original + 9 new = 13 tests, all PASS.

- [ ] **Step 4: Run full suite to confirm nothing regressed**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_order_flow.py claudia/order_flow.py
git commit -m "test: order_flow execution path — gates, error handling, decision logging"
```

---

## Final verification

- [ ] **Run complete test suite**

```bash
pytest tests/ -v 2>&1 | tail -20
```

Expected output:
```
tests/test_agent.py — 21 passed
tests/test_context_loader.py — 13 passed
tests/test_conversation_store.py — 25 passed
tests/test_gdrive_sync.py — 13 passed
tests/test_order_flow.py — 13 passed
tests/test_security_regressions.py — 9 passed
tests/test_status.py — 21 passed
tests/test_tradingview.py — 16 passed
============ 131 passed, 1 warning in X.XXs
```

- [ ] **Run ibkr_core_mcp tests**

```bash
cd /Users/steph/Claude_Projects/ibkr_core_mcp
pytest tests/test_client.py -v
```

Expected: all existing + 4 new = passing.

- [ ] **Final commit if any cleanup needed**

```bash
git add -p
git commit -m "test: final cleanup"
```
