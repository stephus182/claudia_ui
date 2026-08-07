"""
Security regression tests — 2026-06-12 (commit 3927dcd) and 2026-06-25 (commit 7a3ed0a).
Each test corresponds to one resolved finding from either audit.
These tests MUST stay green — a failure here means a security control was regressed.
"""

import sqlite3
import stat
import threading
from datetime import UTC, datetime
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
        """Capture the environment the subprocess would have been given."""
        captured_env.update(kwargs.get("env", {}))
        return MagicMock()

    class FakeCM:
        """A stand-in for the sidecar's stdio context manager."""
        async def __aenter__(self):
            """Hand back a read/write pair, as the real stdio client does."""
            return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a):
            """Nothing to tear down for the stub."""
            pass

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

    with patch("ibkr_core_mcp.gdrive_auth.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("ibkr_core_mcp.gdrive_auth.Request"), \
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
        """A downloader that yields the prepared document text in one chunk."""
        def __init__(self, buf, _req):
            """Write the encoded document into the caller's buffer."""
            buf.write(content.encode())
        def next_chunk(self):
            """Report the single chunk as complete."""
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {"size": str(small_size)}

    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == content
    svc.files.return_value.get_media.assert_called_once()


# ── Fix #6 — GDriveSync has threading.Lock ───────────────────────────────────

def test_gdrive_sync_has_lock():
    """GDriveSync must have a _lock attribute for thread safety (Fix #6)."""
    from claudia.gdrive_sync import GDriveSync
    cfg = MagicMock()
    cfg.gdrive_token_file = Path("/fake/token.json")
    sync = GDriveSync(cfg)
    assert hasattr(sync, "_lock"), "GDriveSync missing _lock — thread safety removed"
    assert hasattr(sync._lock, "__enter__") and hasattr(sync._lock, "__exit__"), \
        "_lock must be a context manager (threading.Lock)"


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
        """A lock recording every acquisition, so serialisation is asserted not assumed."""
        def acquire(self, *args, **kwargs):
            """Record the acquisition and take the real lock."""
            lock_acquired.append(True)
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            """Release the real lock."""
            return real_lock.release()

        def __enter__(self):
            """Acquire on entry, matching the real lock's context-manager contract."""
            self.acquire()
            return self

        def __exit__(self, *a):
            """Release on exit."""
            self.release()

    # Duck-typed test double (acquire/release/__enter__/__exit__ only) — not a real RLock
    # subclass, deliberately, to observe lock usage without needing threading internals.
    sync._lock = TrackingLock()  # type: ignore[assignment]

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


# ── Fix #8 — Binary path is logged at INFO on start ─────────────────────────

@pytest.mark.asyncio
async def test_start_logs_selected_binary_path(tmp_path, monkeypatch, caplog):
    """Selected binary path must be logged at INFO level on start (Fix #8)."""
    import logging
    fake_bin = tmp_path / "server.js"
    fake_bin.write_text("// fake")
    monkeypatch.setenv("TRADINGVIEW_MCP_PATH", str(fake_bin))

    class FakeCM:
        """A stand-in for the sidecar's stdio context manager."""
        async def __aenter__(self):
            """Hand back a read/write pair, as the real stdio client does."""
            return (AsyncMock(), AsyncMock())
        async def __aexit__(self, *a):
            """Nothing to tear down for the stub."""
            pass

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
    assert str(fake_bin) in logged_messages, "Binary path not logged at INFO level (Fix #8)"


# ── 2026-06-25 audit — Fix H-1: SSRF guard in fetch_web_page ─────────────────

def _make_agent():
    """Build a minimal ClaudIAAgent for testing _fetch_web_page."""
    from unittest.mock import MagicMock

    from claudia.agent import ClaudIAAgent
    toolkit = MagicMock()
    toolkit.tools = []
    store = MagicMock()
    store.list_doc_versions.return_value = []
    loader = MagicMock()
    loader.load_system_prompt.return_value = ""
    return ClaudIAAgent(
        toolkit=toolkit, store=store, context_loader=loader, session_id="test",
        sink=MagicMock(),
    )


@pytest.mark.parametrize("url,expected_fragment", [
    ("file:///etc/passwd",         "only http/https"),
    ("ftp://example.com/file",     "only http/https"),
    ("http://localhost/path",      "cannot fetch from localhost"),
    ("http://localhost:5055/tickle","cannot fetch from localhost"),
    ("http://127.0.0.1/anything",  "cannot fetch from localhost"),
    ("http://0.0.0.0/anything",    "cannot fetch from localhost"),
    ("http://169.254.0.1/meta",    "cannot fetch from localhost"),
    ("http://10.0.0.1/internal",   "cannot fetch from private"),
    ("http://192.168.1.1/router",  "cannot fetch from private"),
    ("http://172.16.0.1/service",  "cannot fetch from private"),
])
def test_fetch_web_page_ssrf_guard_blocks_internal(url, expected_fragment):
    """SSRF guard (Fix H-1 / 2026-06-25) must block localhost and private IP ranges."""
    agent = _make_agent()
    result = agent._fetch_web_page({"url": url})
    assert expected_fragment in result, (
        f"SSRF guard did not block {url!r}: got {result!r}"
    )


def test_fetch_web_page_ssrf_guard_allows_public(monkeypatch):
    """SSRF guard must pass through public https:// URLs to the actual fetch."""
    from unittest.mock import MagicMock, patch
    agent = _make_agent()

    fake_resp = MagicMock()
    fake_resp.text = "<html><body>Hello world</body></html>"
    fake_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=fake_resp) as mock_get:
        result = agent._fetch_web_page({"url": "https://example.com/"})

    mock_get.assert_called_once()
    assert "Blocked" not in result
    assert "Hello world" in result or "[Fetched" in result


# ── SSRF decimal/hex IP bypass (v1.0 audit port, 2026-06-27) ─────────────────────────────────

def test_fetch_web_page_ssrf_guard_blocks_decimal_ip(monkeypatch):
    """Decimal-encoded IP (2130706433 = 127.0.0.1) must be blocked via DNS resolve-then-check.

    On Linux, socket.gethostbyname("2130706433") resolves to "127.0.0.1" because glibc
    accepts integer IP representations. The string-prefix guard (127.*) doesn't catch this
    because the host is "2130706433", not "127.0.0.1". The resolve-then-check step does.
    Fix ported from ibkr_core_mcp v1.0 pre-release security audit (finding 1, Medium).
    """
    from unittest.mock import patch

    agent = _make_agent()

    with patch("socket.gethostbyname", return_value="127.0.0.1"):
        result = agent._fetch_web_page({"url": "http://2130706433/path"})

    assert "Blocked" in result, (
        f"Decimal IP 2130706433 (=127.0.0.1) was not blocked; got: {result!r}"
    )


# ════════════════════════════════════════════════════════════════════════════════════════
# Post-Panel-migration audit — 2026-07-25 (docs/audits/security-audit-2026-07-25.md)
# ════════════════════════════════════════════════════════════════════════════════════════

# ── H-1 — Panel Markdown must not render untrusted HTML as executable markup ─────────────

_XSS_PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
_FENCE_BREAKOUT = "a\n```\n<script>alert(3)</script>\n```\nb"


def _decodes_to_markup(model_text: str) -> bool:
    """True if the browser's single html_decode would turn model_text back into live markup.

    Panel escapes the pane text for transport, then the client calls html_decode() once
    before assigning innerHTML and re-executing <script> nodes. So "&lt;script&gt;" in the
    model becomes real markup, while "&amp;lt;script&amp;gt;" stays literal text.
    """
    return "&lt;script&gt;" in model_text or "&lt;img" in model_text


@pytest.mark.parametrize("payload", [_XSS_PAYLOAD, _FENCE_BREAKOUT])
def test_safe_markdown_neutralises_html(payload):
    """safe_markdown() must keep untrusted HTML double-escaped (H-1).

    Panel's Markdown pane defaults to markdown-it `html: True` plus bokeh `run_scripts=True`,
    which together execute <script> from any rendered chat text.
    """
    from claudia.panel_markdown import safe_markdown

    text = safe_markdown(f"Output: {payload}").get_root().text
    assert not _decodes_to_markup(text), f"safe_markdown left executable markup: {text!r}"


def test_unsafe_markdown_would_be_vulnerable():
    """Guards the guard: a default pane IS vulnerable, so the test above is meaningful (H-1).

    If Panel ever changes its default, this test fails and safe_markdown can be re-evaluated
    rather than silently becoming a no-op.
    """
    import panel as pn

    text = pn.pane.Markdown(f"Output: {_XSS_PAYLOAD}").get_root().text
    assert _decodes_to_markup(text), (
        "Panel's default Markdown pane no longer renders raw HTML — re-check H-1's premise"
    )


@pytest.mark.parametrize("payload", [_XSS_PAYLOAD, _FENCE_BREAKOUT])
def test_escape_markup_neutralises_chatstep_stream(payload):
    """Tool input/output streamed into a ChatStep must be escaped (H-1).

    ChatStep has no `renderers` parameter and builds its own Markdown panes, so the
    ChatInterface-level safe_markdown hook cannot reach this path. Raw tool results are the
    most exposed sink in the UI: a page fetched by fetch_web_page reaches here verbatim.
    """
    import panel as pn

    from claudia.panel_markdown import escape_markup

    step = pn.chat.ChatStep()
    step.stream(f"Output: {escape_markup(payload)}")
    text = step.objects[-1].get_root().text
    assert not _decodes_to_markup(text), f"ChatStep stream left executable markup: {text!r}"


def test_no_unguarded_markdown_panes_in_package():
    """No module may construct pn.pane.Markdown directly — safe_markdown is the only route (H-1).

    A structural check: a new direct pane would silently reopen the injection, and that is
    exactly how this class of bug returns.
    """
    import re

    package = Path(__file__).resolve().parent.parent / "claudia"
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name == "panel_markdown.py":  # the one sanctioned construction site
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"pn\.pane\.(Markdown|HTML|Str)\(", line):
                offenders.append(f"{path.name}:{num}: {line.strip()}")
    assert not offenders, "Unguarded Markdown/HTML panes found:\n" + "\n".join(offenders)


# ── H-3 — the Panel server must bind loopback only ───────────────────────────────────────

def test_pn_serve_binds_loopback_only():
    """main() must pass address="127.0.0.1" to pn.serve (H-3).

    Without it Panel passes address=None to bokeh, tornado binds every interface, and the
    UI — live account data, P&L, positions, and every widget — is reachable from the LAN.
    websocket_origin is NOT a substitute: it gates only the websocket upgrade, and tornado
    skips the origin check entirely when no Origin header is sent.
    """
    from claudia import panel_app

    # `_port_is_free` is patched only so this test does not depend on whether 8001 happens
    # to be free on the machine running it — main() now returns early on a taken port,
    # which would skip pn.serve entirely and make this security assertion vacuous.
    with patch("claudia.panel_app.pn.serve") as mock_serve, \
         patch("claudia.panel_app._port_is_free", return_value=True), \
         patch("claudia.panel_app.signal.signal"), \
         patch.object(panel_app, "_gdrive_sync", None):
        panel_app.main()

    assert mock_serve.called, "pn.serve was never called"
    address = mock_serve.call_args.kwargs.get("address")
    assert address in ("127.0.0.1", "localhost"), (
        f"pn.serve address={address!r} — must be loopback, not all-interfaces"
    )


# ── H-2 — private documents must never be tracked by git ─────────────────────────────────

# Every class of content the project has decided must never enter git. Standing rule
# (user, 2026-07-25): "keep private data and all plans out of git."
_MUST_NEVER_BE_TRACKED = [
    ".env",                     # ANTHROPIC_API_KEY, IBKR_FLEX_TOKEN
    "docs/context.md",          # ClaudIA persona
    "docs/principles.md",       # personal trading rules
    "docs/versions",            # verbatim snapshots of both of the above (H-2)
    "docs/plans",               # personal working documents — local + Drive only
    "docs/panel/screenshots",   # UI smokes carry live account data, balances, order IDs
    "data",                     # claudia.db, Flex archive, session reports
    # Browser page dumps. `.playwright-mcp/` was already ignored, but a snapshot written
    # to the repo ROOT under any name was not, and `git add -A` swept one into a commit
    # on 2026-08-07 — 289 lines carrying net liquidation, cash, positions and P&L. Caught
    # before the push and removed from the commit, so it never reached the public remote.
    # These dumps are the screenshots class in text form and belong in the same list.
    "page.yml",
    "snapshot.yaml",
]


@pytest.mark.parametrize("path", _MUST_NEVER_BE_TRACKED)
def test_private_content_is_not_git_tracked(path):
    """No private content may be tracked, in any class (H-2).

    docs/versions/v1/{context,principles}.md were tracked and PUBLIC on GitHub for ~6 weeks:
    the 2026-07-10 filter-repo scrub was path-scoped to docs/context.md and
    docs/principles.md and never covered the version snapshots. .gitignore does not untrack
    files that are already tracked, so only a structural check catches this.

    Parametrised per path so a failure names the class that leaked rather than one blob.
    """
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["git", "ls-files", "--", path],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    tracked = [line for line in out.stdout.splitlines() if line.strip()]
    assert not tracked, f"{path!r} is git-tracked and must not be: {tracked}"


def test_every_private_path_is_gitignored():
    """Each private class must also be covered by .gitignore, not merely absent (H-2).

    Absence from the index is the symptom; the ignore rule is the control. Without it a
    stray `git add -A` re-adds the content and the check above only notices afterwards.
    """
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    unignored = []
    for path in _MUST_NEVER_BE_TRACKED:
        probe = path if Path(repo / path).is_file() else f"{path}/probe.md"
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", probe],
            cwd=repo, capture_output=True, check=False,
        )
        if result.returncode != 0:
            unignored.append(path)
    assert not unignored, f"Private paths missing from .gitignore: {unignored}"


def test_no_tracked_but_ignored_files():
    """.gitignore and the index must agree (H-2, L-3)."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["git", "ls-files", "-i", "-c", "--exclude-standard"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    offenders = [line for line in out.stdout.splitlines() if line.strip()]
    assert not offenders, f"Tracked files matched by .gitignore: {offenders}"


# ── M-2 — version snapshots of the private docs must be chmod 0600 ───────────────────────

def test_version_snapshot_is_chmod_600(tmp_path, monkeypatch):
    """_write_version_snapshot must chmod both files to 0600 (M-2).

    Path.write_text() honours the umask (0644 by default), and these snapshots hold the
    verbatim persona and trading rules. Mirrors the token-file reasoning in SECURITY.md §7:
    creation flags only set the mode on files that did not already exist.
    """
    from claudia import panel_app

    monkeypatch.setattr(panel_app, "_VERSIONS_PATH", tmp_path)
    panel_app._write_version_snapshot("v9", "persona text", "trading rules")

    for name in ("context.md", "principles.md"):
        path = tmp_path / "v9" / name
        assert path.exists(), f"{name} was not written"
        mode = oct(path.stat().st_mode & 0o777)
        assert mode == oct(0o600), f"{name} permissions {mode} != 0o600"


# ── M-1 — order proposals must be validated before rendering or staging ──────────────────
#
# The invariant is unchanged since the 2026-07-25 audit: a proposal the model was not
# permitted to emit must never reach the staging button. What moved is the enforcement.
# `order_proposal_schema.py` (a hand validator over a fenced JSON block) is retired; the
# contract is now the strict tool schema in claudia/proposal_tools.py — which the API
# enforces before agent.py ever sees the input — plus `_proposal_defect` for the four
# guarantees that schema provably cannot express (probed 2026-07-27).
#
# Cases that were rejected by the old validator and are now structurally impossible are
# asserted against the schema in tests/test_proposal_tools.py, not here.

_M1_ORDER = {
    "symbol": "AAPL", "action": "BUY", "quantity": 10, "order_type": "LMT",
    "limit_price": 185.0, "stop_price": None, "tif": "DAY", "sec_type": "STK",
    "conid": None, "reason": "M-1 regression fixture",
}
_M1_MODIFY = {
    "order_id": "242538143", "conid": 265598, "symbol": "AAPL", "action": "BUY",
    "quantity": 1, "order_type": "LMT", "limit_price": 105.0, "stop_price": None,
    "tif": "GTC", "sec_type": "STK", "reason": "M-1 regression fixture",
    "changes": [{"field": "limit_price", "previous_value": 100.0}],
}


def _m1_agent():
    """A ClaudIAAgent with every dependency mocked — proposal handling touches none of them."""
    from unittest.mock import MagicMock, patch

    from claudia.agent import ClaudIAAgent

    toolkit = MagicMock()
    toolkit.tools = []
    with patch("claudia.agent.AsyncAnthropic"):
        return ClaudIAAgent(
            toolkit=toolkit, store=MagicMock(), context_loader=MagicMock(),
            session_id="m1", sink=MagicMock(),
        )


@pytest.mark.parametrize("tool,payload,reason", [
    ("propose_order", {**_M1_ORDER, "quantity": -5}, "negative quantity"),
    ("propose_order", {**_M1_ORDER, "quantity": 0}, "zero quantity"),
    ("propose_order", {**_M1_ORDER, "quantity": True}, "bool quantity"),
    ("propose_order", {**_M1_ORDER, "quantity": "10"}, "string quantity"),
    ("propose_order", {**_M1_ORDER, "symbol": "   "}, "blank symbol"),
    ("propose_order", {k: v for k, v in _M1_ORDER.items() if k != "symbol"}, "missing symbol"),
    ("propose_modify", {**_M1_MODIFY, "order_id": "  "}, "blank order_id"),
    ("propose_modify", {**_M1_MODIFY, "changes": [
        {"field": "limit_price", "previous_value": 100.0},
        {"field": "limit_price", "previous_value": 99.0},
    ]}, "duplicate changes entries"),
])
def test_malformed_order_proposal_is_rejected(tool, payload, reason):
    """A proposal the model was not permitted to emit must never reach the staging button (M-1)."""
    agent = _m1_agent()
    result = agent._handle_local_tool(tool, payload)
    assert agent._pending_proposal is None, f"{reason} was recorded for rendering"
    assert "REJECTED" in result
    assert "no staging button" in result.lower()


@pytest.mark.parametrize("payload", [
    _M1_ORDER,
    # The live-proven ES order (716373691) that cleared the full gate chain on 2026-07-24.
    {"symbol": "ES", "action": "BUY", "quantity": 1, "order_type": "LMT",
     "limit_price": 6100.0, "stop_price": None, "sec_type": "FUT", "conid": 730283085,
     "tif": "GTC", "reason": "Live-proven shape"},
])
def test_valid_order_proposal_is_accepted(payload):
    """Real proposal shapes must still pass — validation rejects, it must not over-reject (M-1)."""
    agent = _m1_agent()
    result = agent._handle_local_tool("propose_order", payload)
    assert agent._pending_proposal == ("order", payload)
    assert "accepted" in result.lower()


def test_validator_never_mutates_the_proposal():
    """Order parameters are immutable — validation must reject, never repair (M-1).

    A handler that silently normalised action/quantity/price would violate the rule
    enforced in _SAFETY_BLOCK (feedback-order-parameter-immutability).
    """
    import copy

    agent = _m1_agent()
    payload = {**_M1_ORDER, "quantity": 0}
    before = copy.deepcopy(payload)
    agent._handle_local_tool("propose_order", payload)
    assert payload == before, "handler mutated the proposal"


def test_accepted_proposal_is_recorded_by_reference_not_reshaped():
    """The dict handed to the render path must be exactly what the model emitted — any
    reshaping in the handler would be a mutation of an order proposal en route to Gate 2."""
    agent = _m1_agent()
    payload = dict(_M1_MODIFY)
    agent._handle_local_tool("propose_modify", payload)
    assert agent._pending_proposal is not None
    assert agent._pending_proposal[1] is payload


# ── 2026-08-05 audit — L-1/L-2: app-written account files must be chmod 0600 ─────────────
#
# SECURITY.md §12 requires every app-written file holding private-document or account
# content to be 0600 immediately after the write. Two writers had never honoured it, so
# the reports and the Flex verdict sat at the umask default (0644, confirmed on disk over
# 45 live session reports and the real store.db record). `Path.write_text` honours the
# umask AND leaves an existing file's mode alone, which is why both chmods are
# unconditional rather than create-only.


def test_session_report_is_chmod_600(tmp_path, monkeypatch):
    """A session report carries order proposals and prices — it must not be world-readable."""
    from claudia import session_reporter

    store = MagicMock()
    store.get_history.return_value = []
    store.get_decisions.return_value = []

    # The report directory is `data/test-sessions` relative to the process cwd.
    monkeypatch.chdir(tmp_path)
    path = session_reporter.generate_session_report("s1", store)

    assert path is not None, "report was not written"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (
        f"session report is {oct(stat.S_IMODE(path.stat().st_mode))}, expected 0o600"
    )


def test_flex_validation_record_is_chmod_600(tmp_path):
    """The cached Flex verdict summarises the trade dataset — same 0600 rule."""
    from claudia import flex_sync

    path = tmp_path / "store.db.validation.json"
    validity = flex_sync.DatasetValidity(checks=(), empty=False)
    flex_sync._write_record(path, validity, (1, 2, "2026-08-05"), datetime(2026, 8, 5, tzinfo=UTC))

    assert path.exists(), "verdict was not written"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (
        f"validation record is {oct(stat.S_IMODE(path.stat().st_mode))}, expected 0o600"
    )


def test_flex_validation_record_rewrite_stays_600(tmp_path):
    """The chmod must be unconditional: write_text does not touch an existing file's mode,
    so a record created before this fix (0644) must be corrected on the next write."""
    from claudia import flex_sync

    path = tmp_path / "store.db.validation.json"
    path.write_text("{}")
    path.chmod(0o644)

    validity = flex_sync.DatasetValidity(checks=(), empty=False)
    flex_sync._write_record(path, validity, (1, 2, None), datetime(2026, 8, 5, tzinfo=UTC))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "pre-existing 0644 record was not corrected"
