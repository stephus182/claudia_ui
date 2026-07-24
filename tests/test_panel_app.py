"""Tests for claudia/panel_app.py's per-session app factory.

Phase 5 (Task 5.1): _build_chat_app is now split — only the chat surface, callback
wiring, and welcome message are synchronous; everything else (GDrive DB download,
store, loader, agent construction) runs in a background _init_session task gated by
an asyncio.Event. Every test therefore needs a running event loop (asyncio.create_task
inside the factory), and awaiting the chat callback is the natural synchronization
point: it awaits init internally, so no sleeps or timing assertions. Every callback
await is wrapped in asyncio.wait_for so a future regression in the init gate (e.g.
_init_done.set() falling out of the finally) fails cleanly instead of hanging the
suite. Every test also awaits the callback BEFORE leaving its patch context — an init
task left merely scheduled would run after the patches are removed and construct real
singletons (empirically probed during review: one await asyncio.sleep(0) after the
with-block was enough to build a real toolkit/store).

Task 5.2: _init_session now reads context.md/principles.md from Drive (when
_gdrive_sync is set), registers the document version, writes a snapshot, warns on
hash change, and stamps the session row + agent with hash/version. Tests whose init
reaches that code use _make_mock_store/_configure_loader for the required returns and
patch claudia.panel_app._write_version_snapshot so no real files land under
docs/versions/.

Task 5.3: _init_session now sends the opening status message (account status +
trade/calendar context) via _send_opening_status before publishing the agent. The
nine tests whose init completes patch it with an AsyncMock: without the patch they
would still pass via the offline-degrade path, but only through incidental
MagicMock behavior — patching keeps them focused and deterministic. The two
failure-path tests don't patch it (their init never reaches the status code);
test_opening_status.py covers the builders themselves.

Task 5.5: the autouse backend_singletons fixture guarantees no test in this module
ever constructs or starts a real ConnectivityChecker/ExecutionListener, with the
module globals reset to None (not restored — a restore would re-install a
previously leaked object) on teardown.

Unless a test targets the GDrive branch, GOOGLE_DRIVE_FOLDER_ID is blanked via
patch.dict so that branch of _init_session is skipped — unit tests must never touch
the real Drive (the developer .env sets that var, and panel_app's load_dotenv would
otherwise activate the branch).
"""

import asyncio
import logging
import os
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import panel as pn
import pytest

from claudia.panel_app import (
    _DOCS_PATH,
    _build_chat_app,
    _connect_tradingview,
    _screenshot_file_input,
)
from tests.conftest import _find_buttons, _get_click_callback

_NO_GDRIVE = {"GOOGLE_DRIVE_FOLDER_ID": ""}
_CALLBACK_TIMEOUT = 5


def _message_texts(chat) -> list[str]:
    return [(m.object if hasattr(m, "object") else str(m)) for m in chat.objects]


def _iter_tree(node):
    """Depth-first walk over a Panel layout tree (yields the node then descendants).
    Task 10.1 nested the chat one Column deeper inside the session-root Row, so the
    composition tests find the chat by type across the whole tree rather than by a
    fixed root.objects index."""
    yield node
    for child in getattr(node, "objects", []):
        yield from _iter_tree(child)


def _find_chat(root) -> pn.chat.ChatInterface:
    return next(n for n in _iter_tree(root) if isinstance(n, pn.chat.ChatInterface))


def _make_mock_store() -> MagicMock:
    """ConversationStore mock with the versioning defaults every happy-path init
    consumes (Task 5.2): no prior session hash (first run — no WARNING expected)
    and a fixed registered version label."""
    store = MagicMock()
    store.list_doc_versions.return_value = []
    store.get_doc_version.return_value = None
    store.get_last_context_hash.return_value = None
    store.register_doc_version_if_new.return_value = "v7"
    return store


def _configure_loader(mock_loader_cls: MagicMock) -> None:
    """Give a patched ContextLoader class the happy-path returns _init_session
    consumes: a REAL 2-tuple from get_effective_texts (the code unpacks it) and a
    stable hash."""
    loader = mock_loader_cls.return_value
    loader.load_system_prompt.return_value = "# Role\nStub."
    loader.reload_count = 0
    loader.get_effective_texts.return_value = ("ctx text", "pri text")
    loader.compute_hash.return_value = "hash123"


@pytest.fixture(autouse=True)
def backend_singletons():
    """No test in this module may construct or start a real ConnectivityChecker/
    ExecutionListener (module docstring discipline — they are network-facing and
    their .start() binds an asyncio task to whatever loop is running). Globals
    are reset to None on BOTH sides — deliberately NOT restored — because a
    monkeypatch-style restore would re-install any previously leaked object."""
    import claudia.panel_app as pa

    pa._connectivity_checker = None
    pa._execution_listener = None
    with (
        patch("claudia.panel_app.ConnectivityChecker") as checker_cls,
        patch("claudia.panel_app.ExecutionListener") as listener_cls,
    ):
        yield SimpleNamespace(checker_cls=checker_cls, listener_cls=listener_cls)
    pa._connectivity_checker = None
    pa._execution_listener = None


@pytest.fixture(autouse=True)
def flex_sync(request):
    """Autouse seam for the Flex-sync decision call in _init_session. Tests
    marked @pytest.mark.real_flex_sync get the REAL function (the decision/sync
    unit tests); everything else gets an AsyncMock so init-driving tests stay
    isolated."""
    if request.node.get_closest_marker("real_flex_sync"):
        yield None
        return
    with patch("claudia.panel_app._maybe_background_flex_sync",
               new_callable=AsyncMock) as mock_seam:
        yield mock_seam


@pytest.fixture(autouse=True)
def tv_connect(request):
    """Autouse seam for the TradingView connect call in _init_session (Phase 9).
    Tests marked @pytest.mark.real_tv_connect exercise the real
    _connect_tradingview (patching _get_tv_bridge/check_cdp_running themselves);
    everything else gets an AsyncMock returning tv_offline=False so init-driving
    tests stay isolated and NO real tradingview-mcp sidecar subprocess is ever
    spawned. Mirrors the flex_sync seam."""
    if request.node.get_closest_marker("real_tv_connect"):
        yield None
        return
    with patch("claudia.panel_app._connect_tradingview",
               new=AsyncMock(return_value=False)) as mock_seam:
        yield mock_seam


@pytest.mark.asyncio
async def test_build_chat_app_returns_a_chat_interface_with_callback_wired():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        # Drain the init task while the patches are still active (see module docstring).
        await asyncio.wait_for(chat.callback("x", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert chat.callback is not None


@pytest.mark.asyncio
async def test_build_chat_app_callback_waits_for_init_then_dispatches_to_agent():
    """The gating contract: a message sent immediately after render must wait for
    the background _init_session task to finish, then reach the real agent —
    never race it and never error out because the agent doesn't exist yet."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(
            chat.callback("hello world", "User", chat), timeout=_CALLBACK_TIMEOUT
        )

    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello world")


@pytest.mark.asyncio
async def test_build_chat_app_constructs_sink_with_the_real_store():
    """Code-quality review of Task 3.3 flagged this as untested: PanelMessageSink now
    needs store= wired through so staged/cancelled/modified orders actually get logged
    to ConversationStore.decisions — forgetting it silently defaults to None (no error,
    no test failure), the same class of silent audit-trail gap this project treats as
    non-negotiable elsewhere."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.agent.AsyncAnthropic"),
        patch("claudia.panel_app.PanelMessageSink") as mock_sink_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        chat = _build_chat_app()
        # Sink construction happens in the background init task — awaiting the
        # callback is the synchronization point that guarantees init has finished.
        await asyncio.wait_for(chat.callback("ping", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_sink_cls.assert_called_once()
    assert mock_sink_cls.call_args.kwargs["store"] is mock_store


@pytest.mark.asyncio
async def test_build_chat_app_wires_tv_bridge_getter_reading_live_module_global(monkeypatch):
    """Task 9.2: the sink's tv_bridge_getter must read panel_app's live _tv_bridge
    module global at click time — so a ```pine Inject button reflects a TradingView
    launched AFTER the message rendered, not a snapshot captured at init."""
    import claudia.panel_app as panel_app

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.agent.AsyncAnthropic"),
        patch("claudia.panel_app.PanelMessageSink") as mock_sink_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("ping", "User", chat), timeout=_CALLBACK_TIMEOUT)

    getter = mock_sink_cls.call_args.kwargs["tv_bridge_getter"]
    assert callable(getter)
    # Reads the live global: mutate _tv_bridge after init, the getter reflects it.
    sentinel = object()
    monkeypatch.setattr(panel_app, "_tv_bridge", sentinel)
    assert getter() is sentinel


@pytest.mark.asyncio
async def test_init_downloads_drive_db_before_first_store_open(monkeypatch):
    """Design D1 ordering: the GDrive DB download must COMPLETE before
    ConversationStore first opens the DB file — otherwise the store's sqlite
    connection would hold the old inode the download atomically replaces and its
    writes would be silently lost."""
    # Reset the module singletons this test exercises (order-independence;
    # monkeypatch restores the originals afterwards).
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", None)
    monkeypatch.setattr("claudia.panel_app._conv_store", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    # Shared manager records cross-mock call order.
    manager = Mock()
    mock_sync_cls = MagicMock()
    manager.attach_mock(mock_sync_cls.return_value.download_db, "download_db")
    mock_get_store = MagicMock(return_value=mock_store)
    manager.attach_mock(mock_get_store, "get_store")

    with (
        patch.dict(os.environ, {"GOOGLE_DRIVE_FOLDER_ID": "test-folder-id"}),
        patch("claudia.panel_app.Config"),
        patch("claudia.panel_app.GDriveSync", mock_sync_cls),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", mock_get_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    call_names = [name for name, _args, _kwargs in manager.mock_calls]
    assert "download_db" in call_names
    assert "get_store" in call_names
    assert call_names.index("download_db") < call_names.index("get_store")
    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_init_continues_without_drive_when_gdrive_sync_fails(monkeypatch):
    """Drive failure is non-fatal: a GDriveSync that blows up at construction must be
    logged and skipped — init still completes and the agent is usable."""
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", None)
    monkeypatch.setattr("claudia.panel_app._conv_store", None)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, {"GOOGLE_DRIVE_FOLDER_ID": "test-folder-id"}),
        patch("claudia.panel_app.Config"),
        patch("claudia.panel_app.GDriveSync", side_effect=RuntimeError("drive down")),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")
    assert not any("Session init failed" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_failure_missing_docs_sends_setup_required_and_callback_answers_honestly():
    """Missing context.md/principles.md must surface as a visible 'Setup required'
    message, and a subsequent user message must get an honest 'Setup required' reply —
    not a re-prefixed 'Session init failed' double label, and never reach an agent
    that was never built."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.side_effect = FileNotFoundError(
            "docs/context.md not found"
        )
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    # Two 'Setup required' messages: the init-time one AND the callback's honest reply.
    assert sum("Setup required" in t for t in texts) >= 2
    # The callback reply must not re-prefix 'Session init failed:' (double label).
    assert not any("Session init failed" in t for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_init_unexpected_failure_reports_error_not_crash():
    """Any unexpected init failure (here: toolkit construction blowing up) must be
    reported in-chat, never crash the session or leave the input gate deadlocked."""
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", side_effect=RuntimeError("boom")),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.return_value = "# Role\nStub."
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("Session init failed" in t for t in texts)
    mock_agent_cls.return_value.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_init_registers_doc_version_and_creates_session_with_metadata():
    """Parity with app.py:302-322 (design D3): every session registers the document
    version (idempotent), writes the human-readable snapshot, and stamps both the
    session row and the agent with the current hash + version label."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot") as mock_snapshot,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_store.register_doc_version_if_new.assert_called_once_with(
        "hash123", "ctx text", "pri text"
    )
    mock_snapshot.assert_called_once_with("v7", "ctx text", "pri text")
    create_kwargs = mock_store.create_session.call_args.kwargs
    assert create_kwargs["context_hash"] == "hash123"
    assert create_kwargs["doc_version"] == "v7"
    assert mock_agent_cls.call_args.kwargs["doc_version"] == "v7"


@pytest.mark.asyncio
async def test_init_hash_change_sends_warning():
    """When the current doc hash differs from the last session's, init must send the
    security WARNING naming both the previous and current version labels
    (app.py:309-320 parity)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    mock_store.get_last_context_hash.return_value = "oldhash"
    mock_store.get_version_label.return_value = "v6"

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("WARNING" in t and "v6" in t and "v7" in t for t in texts)
    # Ordering invariant (same technique as the D1 download-before-store test):
    # get_last_context_hash must run BEFORE this session's create_session — the
    # query reads the newest session row, so inserting ours first would make it
    # see its own hash and the warning would never fire again.
    call_names = [name for name, _args, _kwargs in mock_store.mock_calls]
    assert call_names.index("get_last_context_hash") < call_names.index("create_session")


@pytest.mark.asyncio
async def test_init_no_warning_when_hash_unchanged_or_first_run():
    """First run (no prior session row) must NOT produce the hash-change WARNING —
    get_last_context_hash returning None means there is nothing to compare against."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()  # get_last_context_hash already returns None

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert not any("WARNING" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_reads_context_docs_from_drive_when_sync_available(monkeypatch):
    """User-confirmed requirement: context.md/principles.md live in Google Drive —
    EVERY session must read them via GDriveSync.read_text (which itself falls back
    to the local file when Drive is unreachable or the file is absent), not from
    local disk alone (app.py:256-265 parity)."""
    mock_sync = MagicMock()

    def _drive_read_text(filename, local_path=None):
        return "drive ctx" if filename == "context.md" else "drive pri"

    mock_sync.read_text.side_effect = _drive_read_text
    # With _gdrive_sync already set, the download branch is skipped entirely (its
    # condition checks `_gdrive_sync is None`) — no GOOGLE_DRIVE_FOLDER_ID needed;
    # the _NO_GDRIVE env guard stays anyway for isolation. monkeypatch restores the
    # module global afterwards (same hygiene as the Task 5.1 GDrive tests).
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", mock_sync)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    assert mock_loader_cls.call_args.kwargs["context_text"] == "drive ctx"
    assert mock_loader_cls.call_args.kwargs["principles_text"] == "drive pri"
    # Local-fallback wiring: read_text must receive the local path so its internal
    # freshness guard / fallback can compare against (and fall back to) the file.
    mock_sync.read_text.assert_any_call("context.md", local_path=_DOCS_PATH / "context.md")
    mock_sync.read_text.assert_any_call("principles.md", local_path=_DOCS_PATH / "principles.md")


@pytest.mark.asyncio
async def test_init_sends_opening_status_and_stamps_trade_context():
    """Task 5.3: after the agent is built, init must send the status message
    (status block + trade status line) and stamp agent._trade_context BEFORE the
    input gate opens (app.py:399-514 parity) — an agent published without its
    trade context would silently answer without trade-history grounding."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch(
            "claudia.panel_app.gather_status_block",
            new=AsyncMock(return_value=("STATUS BLOCK", False)),
        ),
        patch(
            "claudia.panel_app.build_trade_lines",
            return_value=("trade status line", "TRADE CTX"),
        ) as mock_build,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    texts = _message_texts(chat)
    assert any("STATUS BLOCK" in t and "trade status line" in t for t in texts)
    assert mock_agent_cls.return_value._trade_context == "TRADE CTX"
    mock_build.assert_called_once_with(mock_toolkit, False)
    mock_agent_cls.return_value.handle_message.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_init_offline_flag_flows_from_gather_to_trade_lines():
    """Pins the ibkr_offline plumbing: gather_status_block's offline result must
    flow into build_trade_lines as its second argument — a hardcoded False or an
    argument swap must fail this test."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch(
            "claudia.panel_app.gather_status_block",
            new=AsyncMock(return_value=("OFFLINE BLOCK", True)),
        ),
        patch(
            "claudia.panel_app.build_trade_lines",
            return_value=("trade status line", "TRADE CTX"),
        ) as mock_build,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    mock_build.assert_called_once_with(mock_toolkit, True)
    assert any("OFFLINE BLOCK" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_init_starts_doc_watcher_with_alert_callback():
    """Task 5.4: init must register a hot-reload callback on the loader
    (app.py:275-294 parity)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    loader = mock_loader_cls.return_value
    loader.start_watching.assert_called_once()
    assert callable(loader.start_watching.call_args.args[0])


@pytest.mark.asyncio
async def test_doc_change_callback_delivers_alert_from_a_plain_thread():
    """The D4-verified loop bridge: the watchdog callback fires in a plain OS
    thread with no asyncio/Bokeh context; the alert must still land in the chat
    via loop.call_soon_threadsafe. The test invokes the REAL registered callback
    from a real thread — if the bridge is replaced with a naive chat.send-only
    callback this still passes (direct sends work too, per the D4 probe), but if
    the callback raises on a foreign thread or the partial wiring breaks, the
    alert never renders and this fails."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        on_reload = mock_loader_cls.return_value.start_watching.call_args.args[0]
        t = threading.Thread(target=on_reload, args=("context.md", "new prompt text"))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        # call_soon_threadsafe scheduled the send onto THIS loop — yield to run it.
        await asyncio.sleep(0.05)

    texts = _message_texts(chat)
    assert any("Document updated" in t_ and "context.md" in t_ for t_ in texts)


@pytest.mark.asyncio
async def test_init_starts_connectivity_and_execution_singletons(monkeypatch, backend_singletons):
    """Task 5.5 (design D6): first session constructs + starts both process
    singletons. The checker's 60s /tickle poll is the IBKR session KEEPALIVE —
    a live-session-protection requirement, not cosmetics (app.py:348-377
    parity). Since Task 6.2 each session also subscribes its alert closure."""
    sentinel = MagicMock()
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", sentinel)

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    checker_kwargs = backend_singletons.checker_cls.call_args.kwargs
    assert checker_kwargs["gateway_url"] is mock_toolkit._config.gateway_url
    assert checker_kwargs["gdrive_token_file"] is mock_toolkit._config.gdrive_token_file
    assert checker_kwargs["tv_bridge"] is None
    assert checker_kwargs["gdrive_sync"] is sentinel
    backend_singletons.checker_cls.return_value.start.assert_called_once()
    # Task 6.2 flipped this from assert_not_called(): the Phase 5 placeholder
    # pinned the ABSENCE of per-session subscription; Phase 6 delivers it.
    backend_singletons.checker_cls.return_value.subscribe.assert_called_once()
    backend_singletons.listener_cls.assert_called_once_with(
        mock_toolkit._config.gateway_url, mock_toolkit._store
    )
    backend_singletons.listener_cls.return_value.start.assert_called_once()


@pytest.mark.asyncio
async def test_second_session_reuses_singletons_but_restarts_them(backend_singletons):
    """app.py:360-361 parity: construction happens once, but .start() is called
    unconditionally every session (idempotent — restarts a cancelled task)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat1 = _build_chat_app()
        await asyncio.wait_for(chat1.callback("a", "User", chat1), timeout=_CALLBACK_TIMEOUT)
        chat2 = _build_chat_app()
        await asyncio.wait_for(chat2.callback("b", "User", chat2), timeout=_CALLBACK_TIMEOUT)

    backend_singletons.checker_cls.assert_called_once()
    backend_singletons.listener_cls.assert_called_once()
    assert backend_singletons.checker_cls.return_value.start.call_count == 2
    assert backend_singletons.listener_cls.return_value.start.call_count == 2


@pytest.mark.asyncio
async def test_singletons_not_started_when_docs_missing(backend_singletons):
    """Setup-required parity with app.py control flow: the missing-docs guard
    returns before app.py's singleton block runs — Panel matches (keepalive only
    for sessions that got past doc validation)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
    ):
        mock_loader_cls.return_value.load_system_prompt.side_effect = FileNotFoundError(
            "docs/context.md not found"
        )
        mock_loader_cls.return_value.reload_count = 0
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    backend_singletons.checker_cls.assert_not_called()
    backend_singletons.listener_cls.assert_not_called()


@pytest.mark.asyncio
async def test_run_session_cleanup_closes_reports_uploads(monkeypatch):
    """app.py:670-700 parity: stop watching, close session with model metadata,
    generate report (threaded), count messages, upload DB to Drive."""
    from claudia.panel_app import _run_session_cleanup

    mock_sync = MagicMock()
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", mock_sync)
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", None)
    store = MagicMock()
    store.get_session.return_value = {"doc_version": "v7"}
    store.count_messages.return_value = 42
    loader = MagicMock()

    with patch("claudia.panel_app.generate_session_report") as mock_report:
        status = await _run_session_cleanup("sid-1", store, loader)

    loader.stop_watching.assert_called_once()
    store.close_session.assert_called_once_with("sid-1", metadata={"model": ANY})
    mock_report.assert_called_once_with("sid-1", store, {}, "v7")
    mock_sync.upload_db.assert_called_once()
    assert status == "42 messages saved · claudia.db → Drive ✅"


@pytest.mark.asyncio
async def test_run_session_cleanup_drive_failure_is_nonfatal(monkeypatch):
    from claudia.panel_app import _run_session_cleanup

    mock_sync = MagicMock()
    mock_sync.upload_db.side_effect = RuntimeError("drive down")
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", mock_sync)
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", None)
    store = MagicMock()
    store.get_session.return_value = {}
    store.count_messages.return_value = 5
    with patch("claudia.panel_app.generate_session_report"):
        status = await _run_session_cleanup("sid-1", store, MagicMock())
    assert "Drive upload failed ⚠️" in status


@pytest.mark.asyncio
async def test_session_destroy_hook_registered_and_runs_cleanup_once():
    """V4 contract: a sync per-session destroy hook is registered at build time;
    invoking it schedules cleanup exactly once (the closed flag suppresses the
    second invocation — End Session button parity, app.py session_closed)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch.object(pn.state, "on_session_destroyed") as mock_register,
        patch("claudia.panel_app._run_session_cleanup",
              new=AsyncMock(return_value="ok")) as mock_cleanup,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        mock_register.assert_called_once()
        hook = mock_register.call_args.args[0]
        hook(MagicMock())          # first destroy → schedules cleanup
        hook(MagicMock())          # second → suppressed by closed flag
        await asyncio.sleep(0.05)  # let the created task run

    # ANY = the session's uuid; store/loader pin the holder wiring (the hook
    # must pass THIS session's store and loader, not fresh or global objects).
    mock_cleanup.assert_awaited_once_with(ANY, mock_store, mock_loader_cls.return_value)


@pytest.mark.asyncio
async def test_end_session_button_always_present_and_runs_cleanup():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._run_session_cleanup",
              new=AsyncMock(return_value="7 messages saved")) as mock_cleanup,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        buttons = _find_buttons(chat)
        end_btns = [b for b in buttons if b.name == "End Session"]
        assert len(end_btns) == 1
        assert not [b for b in buttons if "Gateway" in b.name]  # online → no gateway btn

        # Simulate a real click via the Phase 3 idiom (test_panel_order_flow.py).
        await _get_click_callback(end_btns[0])(None)
    mock_cleanup.assert_awaited_once()
    texts = _message_texts(chat)
    assert any("Session ended." in t and "7 messages saved" in t for t in texts)


@pytest.mark.asyncio
async def test_start_gateway_button_present_only_when_ibkr_offline():
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, True))),   # offline
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    gateway_btns = [b for b in _find_buttons(chat) if b.name == "Start IBKR Gateway"]
    assert len(gateway_btns) == 1


@pytest.mark.asyncio
async def test_destroy_hook_cleanup_failure_is_logged_not_raised(caplog):
    """5.6b quality review (I2): the destroy-path task's outcome must be
    consumed by the done callback — a failing cleanup logs 'Destroy-path
    cleanup failed' (log.exception) instead of dying as an unretrieved task
    exception on the shared loop."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
        patch.object(pn.state, "on_session_destroyed") as mock_register,
        patch("claudia.panel_app._run_session_cleanup",
              new=AsyncMock(side_effect=RuntimeError("cleanup blew up"))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        hook = mock_register.call_args.args[0]
        hook(MagicMock())
        await asyncio.sleep(0.05)  # let the task fail and the done callback run

    assert "Destroy-path cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_start_gateway_click_success_path_streams_and_refreshes_status(monkeypatch):
    """5.6b quality review (I3a): clicking the REAL gateway button drives the
    full app.py:838-874 core — ensure_docker_running → start → wait_for_gateway
    → open_login_page — renders the success message, and awaits the immediate
    connectivity re-check (app.py:863-864 parity)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, True))),   # offline → button present
        patch("claudia.panel_app.GatewayManager") as mock_gm_cls,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        gm = mock_gm_cls.return_value
        gm.wait_for_gateway.return_value = True
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        # Stronger of the two review options: a checker mock with an AsyncMock
        # _run_checks, asserted awaited — pins the immediate re-check trigger,
        # not just its absence of crash.
        checker = MagicMock()
        checker._run_checks = AsyncMock()
        monkeypatch.setattr("claudia.panel_app._connectivity_checker", checker)

        gw_btn = next(b for b in _find_buttons(chat) if b.name == "Start IBKR Gateway")
        await _get_click_callback(gw_btn)(None)

    gm.ensure_docker_running.assert_called_once()
    gm.start.assert_called_once()
    gm.wait_for_gateway.assert_called_once()
    gm.open_login_page.assert_called_once()
    checker._run_checks.assert_awaited_once()
    assert gw_btn.disabled is True
    texts = _message_texts(chat)
    assert any("✅ IBKR Gateway is reachable" in t for t in texts)


@pytest.mark.asyncio
async def test_start_gateway_click_timeout_reports_and_skips_login_page():
    """5.6b quality review (I3b): wait_for_gateway returning False renders the
    timeout message and never opens the login page."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, True))),   # offline → button present
        patch("claudia.panel_app.GatewayManager") as mock_gm_cls,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        gm = mock_gm_cls.return_value
        gm.wait_for_gateway.return_value = False
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        gw_btn = next(b for b in _find_buttons(chat) if b.name == "Start IBKR Gateway")
        await _get_click_callback(gw_btn)(None)

    gm.wait_for_gateway.assert_called_once()
    gm.open_login_page.assert_not_called()
    texts = _message_texts(chat)
    assert any("✕ Gateway did not start within timeout" in t for t in texts)


def test_main_serves_with_locked_kwargs_and_uploads_on_exit(monkeypatch):
    """5.6b-pre review deferral (M2) + V5 shutdown contract: pn.serve kwargs are
    behavior-bearing (websocket_origin: 403 without 127.0.0.1 — probe-verified),
    and the final Drive upload must run in the finally even if serve raises.
    Task 6.2 quality review (I1): the serve TARGET is behavior-bearing too —
    reverting main() to serve _build_chat_app would silently drop the status
    indicators while every other test stays green."""
    from claudia.panel_app import _build_session_root, main

    mock_sync = MagicMock()
    monkeypatch.setattr("claudia.panel_app._gdrive_sync", mock_sync)
    with (
        patch("claudia.panel_app.pn.serve", side_effect=KeyboardInterrupt) as mock_serve,
        patch("claudia.panel_app.signal.signal") as mock_signal,
        pytest.raises(KeyboardInterrupt),
    ):
        main()
    assert mock_serve.call_args.args[0] is _build_session_root
    kwargs = mock_serve.call_args.kwargs
    assert kwargs["show"] is False
    assert any("127.0.0.1" in o for o in kwargs["websocket_origin"])
    assert any(o.startswith("localhost") for o in kwargs["websocket_origin"])
    mock_signal.assert_called_once_with(signal.SIGTERM, ANY)
    mock_sync.upload_db.assert_called_once()


# ── Task 5.7: background Flex sync decision + sync + store.db backup ──────────


def _flex_toolkit(stale: bool, attempts: list[dict] | None = None) -> MagicMock:
    toolkit = MagicMock()
    toolkit._config.flex_token = "tok"
    toolkit._config.flex_query_id = "qid"
    toolkit._config.sqlite_path = "/tmp/store.db"
    toolkit._store.get_trade_date_coverage.return_value = {
        "stale": stale, "newest": "2026-07-22", "last_trading_day": "2026-07-23",
    }
    toolkit._store.get_log.return_value = attempts or []
    toolkit.execute.return_value = ("synced 3 trades", None)
    return toolkit


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_skips_when_data_current(caplog):
    from claudia.panel_app import _maybe_background_flex_sync
    toolkit = _flex_toolkit(stale=False)
    chat = MagicMock()
    with caplog.at_level(logging.INFO):
        await _maybe_background_flex_sync(chat, toolkit, ibkr_offline=False)
    toolkit.execute.assert_not_called()
    assert any("Flex sync skipped" in r.message for r in caplog.records)


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_skips_on_recent_attempt():
    from datetime import UTC, datetime

    from claudia.panel_app import _maybe_background_flex_sync
    toolkit = _flex_toolkit(
        stale=True,
        attempts=[{"ts": datetime.now(UTC).isoformat()}],
    )
    await _maybe_background_flex_sync(MagicMock(), toolkit, ibkr_offline=False)
    toolkit.execute.assert_not_called()


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_runs_and_backs_up_when_stale_and_never_attempted():
    from claudia.panel_app import _maybe_background_flex_sync
    toolkit = _flex_toolkit(stale=True, attempts=[])
    chat = MagicMock()
    await _maybe_background_flex_sync(chat, toolkit, ibkr_offline=False)
    for _ in range(10):          # drain the spawned sync task
        await asyncio.sleep(0)
    toolkit.execute.assert_called_once_with("sync_flex_trades", {})
    toolkit._cache.upload_account_file.assert_called_once_with(
        toolkit._config.sqlite_path, "store.db"
    )
    sent = [c.args[0] for c in chat.send.call_args_list]
    assert any(str(s).startswith("✅") for s in sent)


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_failure_sends_coverage_fallback():
    from claudia.panel_app import _maybe_background_flex_sync
    toolkit = _flex_toolkit(stale=True, attempts=[])
    toolkit.execute.side_effect = [
        RuntimeError("flex api down"),
        ("coverage: 1129 trades", None),
    ]
    chat = MagicMock()
    await _maybe_background_flex_sync(chat, toolkit, ibkr_offline=False)
    for _ in range(10):
        await asyncio.sleep(0)
    assert toolkit.execute.call_args_list[1].args[0] == "check_flex_coverage"
    sent = [str(c.args[0]) for c in chat.send.call_args_list]
    assert any(s.startswith("⚠ Sync failed") and "coverage: 1129 trades" in s for s in sent)


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_task_death_is_logged_by_done_callback(caplog):
    """I1 (Task 5.7 quality review): if the background sync coroutine itself
    dies — sync fails AND the fallback chat.send raises too — the done callback
    must retrieve and log the exception ('Background Flex sync task died',
    exc_info attached) instead of leaving an unretrieved task exception on the
    shared loop."""
    import claudia.panel_app as pa
    from claudia.panel_app import _maybe_background_flex_sync

    toolkit = _flex_toolkit(stale=True, attempts=[])
    toolkit.execute.side_effect = RuntimeError("flex api down")  # both execute calls fail
    chat = MagicMock()
    chat.send.side_effect = RuntimeError("session gone")
    with caplog.at_level(logging.WARNING):
        await _maybe_background_flex_sync(chat, toolkit, ibkr_offline=False)
        # Sturdier drain than the sleep(0) loop: this assertion needs the task
        # fully completed AND its done callback run before we inspect caplog.
        await asyncio.wait_for(
            asyncio.gather(*pa._background_tasks, return_exceptions=True), timeout=2
        )
        await asyncio.sleep(0)  # done callback is scheduled via call_soon — run it
    died = [r for r in caplog.records if "Background Flex sync task died" in r.message]
    assert len(died) == 1
    # exc_info present == the exception WAS retrieved (no unraisable/never-retrieved).
    assert died[0].exc_info is not None
    assert pa._background_tasks == set()  # discard still ran


@pytest.mark.real_flex_sync
@pytest.mark.asyncio
async def test_flex_sync_noop_when_offline_or_unconfigured():
    from claudia.panel_app import _maybe_background_flex_sync
    toolkit = _flex_toolkit(stale=True)
    await _maybe_background_flex_sync(MagicMock(), toolkit, ibkr_offline=True)
    toolkit._store.get_trade_date_coverage.assert_not_called()
    toolkit2 = _flex_toolkit(stale=True)
    toolkit2._config.flex_token = ""
    await _maybe_background_flex_sync(MagicMock(), toolkit2, ibkr_offline=False)
    toolkit2._store.get_trade_date_coverage.assert_not_called()


# ── Task 6.2: per-session status indicators + alert delivery (Phase 6) ────────


def test_apply_status_maps_enum_to_value_and_color():
    from claudia.panel_app import _apply_status, _make_status_indicators
    from claudia.status import ServiceStatus

    ind = _make_status_indicators()
    assert set(ind) == {"ibkr", "gdrive", "tv"}
    _apply_status(ind, {
        "ibkr": ServiceStatus.OK,
        "gdrive": ServiceStatus.ERROR,
        "tv": ServiceStatus.UNKNOWN,
    })
    assert (ind["ibkr"].value, ind["ibkr"].color) == (True, "success")
    assert (ind["gdrive"].value, ind["gdrive"].color) == (True, "danger")
    assert (ind["tv"].value, ind["tv"].color) == (False, "dark")


def test_indicator_keys_match_real_checker_status_keys():
    """The dots are keyed like ConnectivityChecker.get_status() — pinned against
    the REAL checker (constructor only, no network), not a hand-written dict:
    the spec-review found impl+test sharing the same wrong 'tradingview' key
    while the checker uses 'tv'."""
    from pathlib import Path

    from claudia.panel_app import _INDICATOR_LABELS
    from claudia.status import ConnectivityChecker

    checker = ConnectivityChecker(gateway_url="http://x", gdrive_token_file=Path("/nonexistent"))
    assert set(_INDICATOR_LABELS) == set(checker.get_status())


@pytest.mark.asyncio
async def test_build_session_root_composes_indicators_above_chat():
    from claudia.panel_app import _build_session_root

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
        # Composition is this test's subject, not the timer — patching the
        # periodic registration keeps the module's no-real-background-objects
        # discipline (an unpatched start=False cb would still be onload-started
        # into a real _async_repeat task on this test's loop).
        patch.object(pn.state, "add_periodic_callback"),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        root = _build_session_root()
        chat = _find_chat(root)
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)
    # Plan sketch said `from panel.indicators import BooleanStatus` — that module
    # path doesn't exist (pn.indicators is panel/__init__.py's re-export of
    # panel.widgets.indicators, verified in the venv); the accessor form is the
    # importable one.
    from panel.widgets.indicators import BooleanStatus

    # Task 10.1 nested the chat + indicators inside a left Column of a top-level Row;
    # the indicator Row is now root.objects[0].objects[0].
    left_column = root.objects[0]
    indicator_row = left_column.objects[0]
    assert len([o for o in indicator_row.objects if isinstance(o, BooleanStatus)]) == 3
    assert chat in left_column.objects  # chat sits beside the indicators in the left column
    # The independent candlestick chart pane is composed into the root beside the chat.
    assert any(isinstance(n, pn.pane.Bokeh) for n in _iter_tree(root))


@pytest.mark.asyncio
async def test_build_session_root_registers_5s_periodic_refresh():
    from claudia.panel_app import _build_session_root

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
        patch.object(pn.state, "add_periodic_callback") as mock_cb,
        patch.object(pn.state, "onload") as mock_onload,
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        root = _build_session_root()
        chat = _find_chat(root)
        await asyncio.wait_for(chat.callback("x", "User", chat), timeout=_CALLBACK_TIMEOUT)
    assert mock_cb.call_args.kwargs["period"] == 5000
    # start=False + onload(cb.start) pin the held-event double-delivery fix:
    # starting at build time double-registers the callback on the ServerSession
    # (bokeh ValueError per session), while dropping the onload start would
    # freeze the dots gray forever (refresh never starts) with no test failing.
    assert mock_cb.call_args.kwargs.get("start") is False
    mock_onload.assert_called_once_with(mock_cb.return_value.start)


@pytest.mark.asyncio
async def test_init_subscribes_alert_callback_and_it_sends_to_chat(backend_singletons):
    """The singleton block must subscribe an async alert callback; invoking the
    captured callback sends the pre-formatted alert text into the chat."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        unsub = MagicMock()
        backend_singletons.checker_cls.return_value.subscribe.return_value = unsub
        chat = _build_chat_app()   # subscription is in _init_session — plain factory is fine
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)
        backend_singletons.checker_cls.return_value.subscribe.assert_called_once()
        alert_cb = backend_singletons.checker_cls.return_value.subscribe.call_args.args[0]
        await alert_cb("⚠️ GDrive disconnected.")
    assert any("GDrive disconnected" in t for t in _message_texts(chat))


@pytest.mark.asyncio
async def test_destroy_hook_unsubscribes_before_cleanup(backend_singletons):
    """The hook must unsubscribe BEFORE invoking cleanup (plan design point) —
    ordering pinned via the shared-parent-Mock technique the D1 download-order
    test established (6.2 quality review M4: the earlier body asserted only
    that unsubscribe happened, not the ordering the name claims)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    unsub_mock = MagicMock()
    mock_cleanup = AsyncMock(return_value="ok")
    manager = Mock()
    manager.attach_mock(unsub_mock, "unsub")
    manager.attach_mock(mock_cleanup, "cleanup")

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch.object(pn.state, "on_session_destroyed") as mock_register,
        patch("claudia.panel_app._run_session_cleanup", mock_cleanup),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        backend_singletons.checker_cls.return_value.subscribe.return_value = unsub_mock
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        hook = mock_register.call_args.args[0]
        hook(MagicMock())
        await asyncio.sleep(0.05)
    unsub_mock.assert_called_once()
    call_names = [name for name, _args, _kwargs in manager.mock_calls]
    assert "unsub" in call_names
    assert "cleanup" in call_names
    assert call_names.index("unsub") < call_names.index("cleanup")


@pytest.mark.asyncio
async def test_destroy_during_init_undoes_fresh_subscription(backend_singletons):
    """Init-race hardening: the destroy hook can fire while _init_session is
    still running (init blocked 15-32s), i.e. BEFORE the subscription exists —
    the hook's own unsubscribe then sees None and does nothing. Init must
    therefore re-check the closed flag right after subscribing and undo its
    fresh subscription, or a process-lifetime subscriber closure over a dead
    chat leaks. Deterministic shape: _build_chat_app only schedules the init
    task (it cannot have run yet — no await between create_task and return),
    so invoking the destroy hook before the first callback await guarantees
    closed=True is seen by init's guard, not by the hook's unsub path."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    unsub_mock = MagicMock()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch.object(pn.state, "on_session_destroyed") as mock_register,
        patch("claudia.panel_app._run_session_cleanup",
              new=AsyncMock(return_value="ok")),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        backend_singletons.checker_cls.return_value.subscribe.return_value = unsub_mock
        chat = _build_chat_app()

        # Destroy the session BEFORE init has run: unsubscribe is still None,
        # so the hook itself cannot be the caller of unsub_mock below.
        hook = mock_register.call_args.args[0]
        hook(MagicMock())
        unsub_mock.assert_not_called()

        # Drain init: it subscribes, sees closed=True, and must undo it.
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)
    backend_singletons.checker_cls.return_value.subscribe.assert_called_once()
    unsub_mock.assert_called_once()


@pytest.mark.asyncio
async def test_end_session_button_unsubscribes_too(backend_singletons):
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    unsub_mock = MagicMock()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._run_session_cleanup",
              new=AsyncMock(return_value="7 messages saved")),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        backend_singletons.checker_cls.return_value.subscribe.return_value = unsub_mock
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

        end_btn = next(b for b in _find_buttons(chat) if b.name == "End Session")
        await _get_click_callback(end_btn)(None)
    unsub_mock.assert_called_once()


@pytest.mark.asyncio
async def test_init_awaits_flex_sync_seam_with_gather_offline_flag(flex_sync):
    """Wiring: _init_session must await the decision function with the
    gather-derived ibkr_offline (True here)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, True))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hi", "User", chat), timeout=_CALLBACK_TIMEOUT)

    flex_sync.assert_awaited_once()
    a = flex_sync.await_args
    offline = a.args[2] if len(a.args) > 2 else a.kwargs.get("ibkr_offline")
    assert offline is True


# ── Task 8.1: screenshot upload via standalone FileInput watcher ──────────────
# (ChatInterface's native file tab unpacks its upload wrapper before the chat
# callback sees it — mime/filename lost, bare BytesIO delivered. See the
# "⚠ CORRECTION 2026-07-24" block in the migration plan's Task 8.1 section.)


async def _wait_until(predicate, timeout=_CALLBACK_TIMEOUT):
    """Drain the loop until an async-watcher-driven condition holds. param
    schedules async watchers as tasks rather than running them inline
    (probe-verified, param 2.4.1), so tests poll instead of awaiting a handle."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("watcher condition not met within timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_image_upload_reaches_agent_as_vision_block():
    """Driving the FileInput's public params (mime_type/filename first, value
    last — the watcher keys on value) must deliver the fixed caption plus an
    Anthropic base64 vision block whose data round-trips to the raw bytes, echo
    the image into the feed, and reset the widget for the next upload."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        handle = mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)
        fi = _screenshot_file_input(chat)
        fi.mime_type = "image/png"
        fi.filename = "shot.png"
        fi.value = b"\x89PNG fake"
        # call 1 = "hello"; call 2 = the upload; value None = post-upload reset ran.
        await _wait_until(lambda: handle.call_count >= 2 and fi.value is None)

    call = handle.call_args_list[-1]
    assert "shot.png" in call.args[0]
    img = call.kwargs["images"][0]
    assert img["type"] == "image"
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    import base64 as b64
    assert b64.b64decode(img["source"]["data"]) == b"\x89PNG fake"
    # The screenshot is echoed into the feed (the standalone widget renders no
    # message of its own), and the server-side reset also cleared the metadata
    # so re-uploading the same file re-fires the watcher.
    assert any(isinstance(m.object, pn.pane.Image) for m in chat.objects)
    assert fi.mime_type is None and fi.filename is None


@pytest.mark.asyncio
async def test_non_image_upload_gets_honest_refusal_no_agent_call():
    """FileInput.accept is a client-side hint only — a non-image can still
    arrive. It must get an honest System refusal and never reach the agent —
    no silent drop, no pretend-processing."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)
        fi = _screenshot_file_input(chat)
        fi.mime_type = "application/pdf"
        fi.filename = "doc.pdf"
        fi.value = b"%PDF"
        # _message_texts includes non-str objects (action-button Row, Image
        # panes) — the predicate filters to strings.
        await _wait_until(lambda: any(
            isinstance(t, str) and "image" in t.lower() for t in _message_texts(chat)
        ))

    # handle_message called only for the earlier text message, not the PDF
    assert mock_agent_cls.return_value.handle_message.call_count == 1


@pytest.mark.asyncio
async def test_file_widget_configured_and_composed_in_session_root():
    """The upload widget must be an image/*-restricted FileInput, reachable via
    the chat handoff attribute AND actually composed into the session root's
    top row — a built-but-unplaced widget would pass the other two tests."""
    from claudia.panel_app import _build_session_root

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=AsyncMock(return_value=(None, False))),
        # Same discipline as the Task 6.2 composition test: the timer is not
        # this test's subject.
        patch.object(pn.state, "add_periodic_callback"),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        root = _build_session_root()
        chat = _find_chat(root)
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    from panel.widgets import FileInput
    fi = _screenshot_file_input(chat)
    assert isinstance(fi, FileInput)
    assert fi.accept == "image/*"
    # Task 10.1 nesting: the indicator/upload Row is now the left Column's first child.
    assert fi in root.objects[0].objects[0].objects


@pytest.mark.asyncio
async def test_upload_metadata_snapshotted_before_init_gate():
    """Data-integrity guard: a second upload arriving during the init window
    must not cross-wire the first upload's bytes with the second's mime/filename
    (a mislabeled media_type to the vision API). The watcher snapshots all three
    params at entry, before awaiting _init_done — proven here by firing an upload
    while init is still blocked, overwriting the widget metadata, then releasing
    init and asserting the first upload kept ITS snapshot."""
    import base64 as b64

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    release_init = asyncio.Event()

    async def _blocked_status(chat, toolkit, tv_offline):
        await release_init.wait()
        return (None, False)

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status", new=_blocked_status),
    ):
        _configure_loader(mock_loader_cls)
        handle = mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        fi = _screenshot_file_input(chat)
        # Upload #1 fires the watcher while init is still blocked → it suspends
        # at `await _init_done.wait()` after snapshotting its metadata.
        fi.mime_type = "image/png"
        fi.filename = "first.png"
        fi.value = b"FIRSTBYTES"
        await asyncio.sleep(0.05)  # let the watcher reach the init await
        # Upload #2's metadata lands on the shared widget mid-suspension.
        fi.mime_type = "image/jpeg"
        fi.filename = "second.jpg"
        # Release init; watcher #1 resumes and must use its own snapshot.
        release_init.set()
        await _wait_until(lambda: handle.call_count >= 1)

    call = handle.call_args_list[-1]
    assert "first.png" in call.args[0] and "second.jpg" not in call.args[0]
    img = call.kwargs["images"][0]
    assert img["source"]["media_type"] == "image/png"  # NOT the overwritten jpeg
    assert b64.b64decode(img["source"]["data"]) == b"FIRSTBYTES"


# ── Phase 9 (Task 9.1): TradingView launch button + sidecar tool merge ─────────


@pytest.mark.asyncio
async def test_connect_tradingview_success_wires_agent_and_checker(monkeypatch):
    """_connect_tradingview happy path: sidecar up AND CDP open → merge the TV
    tools into the agent, hand the bridge to the ConnectivityChecker, and report
    tv_offline=False (online)."""
    agent = MagicMock()
    bridge = MagicMock()
    tv_tools = [{"name": "tv_get_chart_snapshot"}]
    bridge.get_tools.return_value = tv_tools
    checker = MagicMock()
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", checker)

    with (
        patch("claudia.panel_app._get_tv_bridge", new=AsyncMock(return_value=bridge)),
        patch("claudia.panel_app.check_cdp_running", return_value=True),
    ):
        tv_offline = await _connect_tradingview(agent)

    assert tv_offline is False
    agent.set_tv_bridge.assert_called_once_with(bridge, tv_tools)
    checker.set_tv_bridge.assert_called_once_with(bridge)


@pytest.mark.asyncio
async def test_connect_tradingview_offline_paths_return_true_without_wiring(monkeypatch):
    """Every non-online outcome returns tv_offline=True and NEVER merges tools:
    (a) sidecar up but CDP down; (b) _get_tv_bridge raises (sidecar unavailable)."""
    checker = MagicMock()
    monkeypatch.setattr("claudia.panel_app._connectivity_checker", checker)

    # (a) sidecar up, tools present, but Desktop/CDP down.
    agent_a = MagicMock()
    bridge = MagicMock()
    bridge.get_tools.return_value = [{"name": "tv_get_chart_snapshot"}]
    with (
        patch("claudia.panel_app._get_tv_bridge", new=AsyncMock(return_value=bridge)),
        patch("claudia.panel_app.check_cdp_running", return_value=False),
    ):
        assert await _connect_tradingview(agent_a) is True
    agent_a.set_tv_bridge.assert_not_called()
    checker.set_tv_bridge.assert_not_called()

    # (b) sidecar unavailable — _get_tv_bridge raises. Must be swallowed.
    agent_b = MagicMock()
    with (
        patch("claudia.panel_app._get_tv_bridge",
              new=AsyncMock(side_effect=RuntimeError("sidecar down"))),
        patch("claudia.panel_app.check_cdp_running", return_value=True),
    ):
        assert await _connect_tradingview(agent_b) is True
    agent_b.set_tv_bridge.assert_not_called()
    checker.set_tv_bridge.assert_not_called()

    # (c) sidecar up + CDP up but it exposes NO tools → still offline, no wiring.
    agent_c = MagicMock()
    bridge_c = MagicMock()
    bridge_c.get_tools.return_value = []
    with (
        patch("claudia.panel_app._get_tv_bridge", new=AsyncMock(return_value=bridge_c)),
        patch("claudia.panel_app.check_cdp_running", return_value=True),
    ):
        assert await _connect_tradingview(agent_c) is True
    agent_c.set_tv_bridge.assert_not_called()
    checker.set_tv_bridge.assert_not_called()


@pytest.mark.real_tv_connect
@pytest.mark.asyncio
async def test_launch_tv_button_present_only_when_tv_offline():
    """The "Launch TradingView" button renders only when _connect_tradingview
    reports offline (here: sidecar up, CDP down)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()
    bridge = MagicMock()
    bridge.get_tools.return_value = [{"name": "tv_get_chart_snapshot"}]

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),   # IBKR online
        patch("claudia.panel_app._get_tv_bridge", new=AsyncMock(return_value=bridge)),
        patch("claudia.panel_app.check_cdp_running", return_value=False),  # → tv_offline
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    tv_btns = [b for b in _find_buttons(chat) if b.name == "Launch TradingView"]
    assert len(tv_btns) == 1


@pytest.mark.real_tv_connect
@pytest.mark.asyncio
async def test_tv_failure_does_not_block_init_and_chat_still_works():
    """A TradingView failure at init is non-fatal: the agent is still built, the
    chat callback reaches it, and the Launch button is offered (tv_offline)."""
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._get_tv_bridge",
              new=AsyncMock(side_effect=RuntimeError("sidecar down"))),
    ):
        _configure_loader(mock_loader_cls)
        handle = mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        # Callback awaits init internally — if a TV failure blocked/aborted init,
        # this would time out or the agent would be absent.
        await asyncio.wait_for(chat.callback("hello", "User", chat), timeout=_CALLBACK_TIMEOUT)

    handle.assert_awaited_once()  # agent built + reachable despite TV failure
    tv_btns = [b for b in _find_buttons(chat) if b.name == "Launch TradingView"]
    assert len(tv_btns) == 1


@pytest.mark.real_tv_connect
@pytest.mark.asyncio
async def test_launch_tv_button_click_success_wires_agent_in_order(monkeypatch):
    """Clicking Launch drives launch_tradingview → _get_tv_bridge → agent.set_tv_bridge
    in that order and renders the success message."""
    monkeypatch.setattr("claudia.panel_app._tv_bridge", None)
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    init_bridge = MagicMock()
    init_bridge.get_tools.return_value = [{"name": "tv_a"}]
    launched_bridge = MagicMock()
    launched_bridge.get_tools.return_value = [{"name": "tv_a"}, {"name": "tv_b"}]

    order = MagicMock()
    launch_mock = AsyncMock(return_value=True)
    get_bridge_mock = AsyncMock()
    order.attach_mock(launch_mock, "launch")
    order.attach_mock(get_bridge_mock, "get_bridge")

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._get_tv_bridge", new=get_bridge_mock),
        patch("claudia.panel_app.check_cdp_running", return_value=False),  # init → offline
        patch("claudia.panel_app.launch_tradingview", new=launch_mock),
    ):
        _configure_loader(mock_loader_cls)
        agent = mock_agent_cls.return_value
        agent.handle_message = AsyncMock()
        # Init calls _get_tv_bridge once (bridge with cdp down → button, no wiring).
        get_bridge_mock.return_value = init_bridge
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hi", "User", chat), timeout=_CALLBACK_TIMEOUT)

        tv_btn = next(b for b in _find_buttons(chat) if b.name == "Launch TradingView")
        # After init: the click rebuilds a fresh bridge. Reset order tracking so the
        # asserted sequence is the click's, not init's.
        order.reset_mock()
        agent.set_tv_bridge.reset_mock()
        get_bridge_mock.return_value = launched_bridge
        await _get_click_callback(tv_btn)(None)

    launch_mock.assert_awaited_once()
    agent.set_tv_bridge.assert_called_once_with(launched_bridge, [{"name": "tv_a"}, {"name": "tv_b"}])
    call_order = [name for name, _, _ in order.mock_calls if name in ("launch", "get_bridge")]
    assert call_order == ["launch", "get_bridge"]
    assert tv_btn.disabled is True
    texts = _message_texts(chat)
    assert any("✅ TradingView connected (2 tools available)." in t for t in texts)


@pytest.mark.real_tv_connect
@pytest.mark.asyncio
async def test_launch_tv_click_stops_stale_bridge_before_rebuild(monkeypatch):
    """The rebuild must tear down the stale (CDP-less) bridge FIRST: on a Launch
    click, a resident _tv_bridge.stop() is awaited BEFORE _get_tv_bridge rebuilds
    (removing the stop() line would leak the old sidecar connection)."""
    old_bridge = MagicMock()
    old_bridge.stop = AsyncMock()
    launched_bridge = MagicMock()
    launched_bridge.get_tools.return_value = [{"name": "tv_a"}]

    order = MagicMock()
    launch_mock = AsyncMock(return_value=True)
    get_bridge_mock = AsyncMock(return_value=launched_bridge)
    order.attach_mock(old_bridge.stop, "stop")
    order.attach_mock(get_bridge_mock, "get_bridge")

    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._get_tv_bridge", new=get_bridge_mock),
        patch("claudia.panel_app.check_cdp_running", return_value=False),  # init → offline
        patch("claudia.panel_app.launch_tradingview", new=launch_mock),
    ):
        _configure_loader(mock_loader_cls)
        mock_agent_cls.return_value.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hi", "User", chat), timeout=_CALLBACK_TIMEOUT)
        tv_btn = next(b for b in _find_buttons(chat) if b.name == "Launch TradingView")
        # A stale bridge is resident in the module global (init's patched
        # _get_tv_bridge never set the real global); the click must stop it first.
        monkeypatch.setattr("claudia.panel_app._tv_bridge", old_bridge)
        order.reset_mock()
        await _get_click_callback(tv_btn)(None)

    old_bridge.stop.assert_awaited_once()
    call_order = [name for name, _, _ in order.mock_calls if name in ("stop", "get_bridge")]
    assert call_order == ["stop", "get_bridge"]


@pytest.mark.real_tv_connect
@pytest.mark.asyncio
async def test_launch_tv_button_click_failure_shows_manual_launch(monkeypatch):
    """launch_tradingview returning False renders the manual-launch instructions
    and NEVER rebuilds the bridge or re-wires the agent."""
    monkeypatch.setattr("claudia.panel_app._tv_bridge", None)
    mock_toolkit = MagicMock()
    mock_toolkit.tools = []
    mock_store = _make_mock_store()

    init_bridge = MagicMock()
    init_bridge.get_tools.return_value = [{"name": "tv_a"}]
    get_bridge_mock = AsyncMock(return_value=init_bridge)

    with (
        patch.dict(os.environ, _NO_GDRIVE),
        patch("claudia.panel_app._get_toolkit", return_value=mock_toolkit),
        patch("claudia.panel_app._get_store", return_value=mock_store),
        patch("claudia.panel_app.ContextLoader") as mock_loader_cls,
        patch("claudia.panel_app._write_version_snapshot"),
        patch("claudia.panel_app.ClaudIAAgent") as mock_agent_cls,
        patch("claudia.panel_app._send_opening_status",
              new=AsyncMock(return_value=(None, False))),
        patch("claudia.panel_app._get_tv_bridge", new=get_bridge_mock),
        patch("claudia.panel_app.check_cdp_running", return_value=False),  # init → offline
        patch("claudia.panel_app.launch_tradingview", new=AsyncMock(return_value=False)),
    ):
        _configure_loader(mock_loader_cls)
        agent = mock_agent_cls.return_value
        agent.handle_message = AsyncMock()
        chat = _build_chat_app()
        await asyncio.wait_for(chat.callback("hi", "User", chat), timeout=_CALLBACK_TIMEOUT)

        tv_btn = next(b for b in _find_buttons(chat) if b.name == "Launch TradingView")
        get_bridge_mock.reset_mock()          # ignore the init-time bridge fetch
        agent.set_tv_bridge.reset_mock()
        await _get_click_callback(tv_btn)(None)

    get_bridge_mock.assert_not_awaited()      # no rebuild on failed launch
    agent.set_tv_bridge.assert_not_called()
    texts = _message_texts(chat)
    # Points at the one-command quit+relaunch helper — the debug port can only be
    # set at launch, so an already-running TV must be relaunched, not patched.
    assert any("./scripts/launch-tradingview-debug.sh" in t for t in texts)
