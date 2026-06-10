"""
Chainlit entry point for ClaudIA.

Wires together: ibkr_core_mcp toolkit, conversation store, context loader,
tradingview-mcp sidecar, and the core agent loop.

Run with:  chainlit run claudia/app.py
"""

import base64
import logging
import os
from pathlib import Path

# ── Python 3.14 + anyio/sniffio compatibility fix ────────────────────────────
# anyio.to_thread.run_sync fails in every Chainlit static-file route (/assets/,
# /logo, /favicon) because sniffio cannot detect the asyncio backend — uvicorn
# does not set sniffio's ContextVar.  The error surfaces in two places inside
# FileResponse: the os.stat() call in __call__ AND the anyio.open_file() call
# in _handle_simple that actually streams the file bytes.
#
# Root fix: patch anyio.to_thread.run_sync to fall back to asyncio.to_thread
# on NoEventLoopError.  This covers all internal anyio I/O at once.
# The FileResponse.__init__ pre-stat patch is kept as a cheap optimisation
# (avoids dispatching to a thread for a stat call that would succeed anyway).
import asyncio as _asyncio
import anyio as _anyio
import anyio.to_thread as _anyio_to_thread
from starlette.responses import FileResponse as _FileResponse

_orig_anyio_run_sync = _anyio_to_thread.run_sync

async def _anyio_run_sync_compat(
    func, *args, abandon_on_cancel: bool = False, cancellable=None, limiter=None
):
    try:
        return await _orig_anyio_run_sync(
            func, *args,
            abandon_on_cancel=abandon_on_cancel,
            cancellable=cancellable,
            limiter=limiter,
        )
    except _anyio.NoEventLoopError:
        return await _asyncio.to_thread(func, *args)

_anyio_to_thread.run_sync = _anyio_run_sync_compat

_orig_fr_init = _FileResponse.__init__

def _fr_init_with_stat(self, path, *args, stat_result=None, **kwargs):
    if stat_result is None:
        try:
            stat_result = os.stat(path)
        except OSError:
            pass  # missing file — FileResponse will raise a clearer error later
    _orig_fr_init(self, path, *args, stat_result=stat_result, **kwargs)

_FileResponse.__init__ = _fr_init_with_stat

# ── Python 3.14 + engineio compatibility fix ─────────────────────────────────
# asyncio.wait_for() uses asyncio.timeout() internally. Python 3.14 added a
# strict check: asyncio.timeout().__aenter__ raises RuntimeError if
# asyncio.current_task() is None.  In uvicorn's ASGI context current_task()
# can return None, which crashes engineio's _service_task and drops the
# WebSocket connection ("Could not reach the server").
#
# Fix: patch asyncio.wait_for to fall back to asyncio.wait() (which does NOT
# use asyncio.timeout) when current_task() is None.  The fallback fully
# preserves TimeoutError semantics.
_orig_asyncio_wait_for = _asyncio.wait_for

async def _asyncio_wait_for_compat(fut, timeout=None, **kwargs):
    if timeout is None or _asyncio.current_task() is not None:
        return await _orig_asyncio_wait_for(fut, timeout=timeout, **kwargs)
    # No current task: asyncio.timeout() would fail.  Use asyncio.wait()
    # which cancels via loop.call_later() and doesn't need a current task.
    if _asyncio.iscoroutine(fut):
        fut = _asyncio.ensure_future(fut)
    done, pending = await _asyncio.wait({fut}, timeout=timeout)
    if pending:
        for p in pending:
            p.cancel()
            try:
                await p
            except (_asyncio.CancelledError, Exception):
                pass
        raise _asyncio.TimeoutError()
    return next(iter(done)).result()

_asyncio.wait_for = _asyncio_wait_for_compat
# ─────────────────────────────────────────────────────────────────────────────

import chainlit as cl
from chainlit.server import app as _server_app
from dotenv import load_dotenv
from starlette.responses import JSONResponse, Response

from ibkr_core_mcp import (
    BrowserCookieAuth,
    ClaudeToolkit,
    Config,
    GDriveCache,
    IBKRClient,
    SQLiteStore,
)

from claudia.agent import ClaudIAAgent
from claudia.alert_manager import AlertManager
from claudia.context_loader import ContextLoader
from claudia.conversation_store import ConversationStore
from claudia.status import ConnectivityChecker
from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv(override=False)

_MODEL = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
_DOCS_PATH = Path(os.environ.get("CLAUDIA_DOCS_PATH", "docs"))
_DB_PATH = Path(os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db"))

# Shared singletons (initialized once at module load, safe to share across sessions)
_config: Config | None = None
_toolkit: ClaudeToolkit | None = None
_conv_store: ConversationStore | None = None
_tv_bridge: TradingViewBridge | None = None
_alert_manager: AlertManager | None = None
_connectivity_checker: ConnectivityChecker | None = None


@_server_app.get("/api/status")
async def api_status():
    """Returns cached connectivity status — instant, non-blocking."""
    if _connectivity_checker:
        return JSONResponse({k: v.value for k, v in _connectivity_checker.get_status().items()})
    return JSONResponse({"ibkr": "unknown", "gdrive": "unknown", "tv": "unknown"})


# Chainlit's /public/{filename} handler uses anyio.to_thread which fails on
# Python 3.14. Serve all custom assets via plain Response so FileResponse
# is never called. No files live in public/ — nothing for Chainlit to serve.
_ASSETS = Path(__file__).parent / "assets"


@_server_app.get("/cl/custom.css")
async def serve_css():
    return Response((_ASSETS / "custom.css").read_bytes(), media_type="text/css")


@_server_app.get("/cl/custom.js")
async def serve_js():
    return Response((_ASSETS / "custom.js").read_bytes(), media_type="application/javascript")


@_server_app.get("/cl/claudia-logo.png")
async def serve_logo():
    return Response((_ASSETS / "claudia-logo.png").read_bytes(), media_type="image/png")


def _get_toolkit() -> ClaudeToolkit:
    global _config, _toolkit
    if _toolkit is None:
        _config = Config.from_env()
        ibkr = IBKRClient(
            config=_config,
            auth=BrowserCookieAuth(_config.gateway_url),
        )
        cache = GDriveCache(_config)
        store = SQLiteStore(_config.sqlite_path)
        _toolkit = ClaudeToolkit(client=ibkr, cache=cache, store=store, config=_config)
    return _toolkit


def _get_store() -> ConversationStore:
    global _conv_store
    if _conv_store is None:
        _conv_store = ConversationStore(_DB_PATH)
    return _conv_store


async def _get_tv_bridge() -> TradingViewBridge:
    global _tv_bridge
    if _tv_bridge is None:
        _tv_bridge = TradingViewBridge()
        await _tv_bridge.start()
    return _tv_bridge


# ── Session start ─────────────────────────────────────────────────────────────

@cl.on_chat_start
async def on_chat_start():
    session_id = cl.context.session.id

    # Load documents
    loader = ContextLoader(_DOCS_PATH)
    try:
        loader.load_system_prompt()  # validate docs exist before proceeding
    except FileNotFoundError as exc:
        await cl.Message(
            content=f"**Setup required:** {exc}\n\nCreate the missing file and restart.",
            author="System",
        ).send()
        return

    # Start file watcher for hot-reload
    def _on_doc_change(filename: str, new_prompt: str) -> None:
        cl.run_sync(
            cl.Message(
                content=f"**Document updated:** `{filename}` reloaded. "
                        f"Principles apply from your next message.",
                author="System",
            ).send
        )()

    loader.start_watching(_on_doc_change)

    # Init toolkit (shared singleton, IBKRClient is stateless per request)
    toolkit = _get_toolkit()

    # Init conversation store, open session
    store = _get_store()
    store.create_session(session_id, context_hash=loader.compute_hash())

    # Connect tradingview-mcp sidecar
    try:
        tv = await _get_tv_bridge()
        tv_tools = tv.get_tools()
        tv_status = f"TradingView: connected ({len(tv_tools)} tools)" if tv_tools else "TradingView: unavailable"
    except Exception as exc:
        log.warning("tradingview-mcp sidecar not available: %s", exc)
        tv_tools = []
        tv_status = "TradingView: unavailable (screenshot mode active)"

    # Start connectivity monitor (singleton — persists across sessions)
    global _connectivity_checker, _config
    if _connectivity_checker is None:
        cfg = _config or Config.from_env()
        _config = cfg  # cache so subsequent sessions don't call Config.from_env() again
        _connectivity_checker = ConnectivityChecker(
            gateway_url=cfg.gateway_url,
            gdrive_token_file=cfg.gdrive_token_file,
            tv_bridge=_tv_bridge,
        )
    # Call unconditionally — start() is idempotent and restarts a cancelled task
    _connectivity_checker.start()
    # Update bridge if TradingView became available after the checker was constructed
    if _tv_bridge is not None:
        _connectivity_checker.set_tv_bridge(_tv_bridge)

    # Build agent for this session
    agent = ClaudIAAgent(
        toolkit=toolkit,
        store=store,
        context_loader=loader,
        session_id=session_id,
        model=_MODEL,
        extra_tools=tv_tools,
    )

    cl.user_session.set("agent", agent)
    cl.user_session.set("loader", loader)
    cl.user_session.set("store", store)
    cl.user_session.set("session_id", session_id)

    # Emit opening status
    try:
        opening_text, _ = await cl.make_async(toolkit.execute)("get_account_summary", {})
        orders_text, _ = await cl.make_async(toolkit.execute)("get_live_orders", {})
        positions_text, _ = await cl.make_async(toolkit.execute)("get_positions", {})
        status_block = (
            f"**Account Summary**\n```\n{opening_text}\n```\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        )
    except Exception as exc:
        log.warning("Could not load IBKR opening status: %s", exc)
        status_block = "*IBKR gateway not connected — data will load when gateway is online.*"

    await cl.Message(
        content=(
            f"**ClaudIA is ready.** {tv_status}\n\n"
            f"{status_block}\n\n"
            "_Ask me anything about your portfolio, markets, or strategy._"
        ),
        author="ClaudIA",
    ).send()


# ── Message handler ────────────────────────────────────────────────────────────

@cl.on_message
async def on_message(message: cl.Message):
    agent: ClaudIAAgent = cl.user_session.get("agent")
    if not agent:
        await cl.Message(content="Session not initialized. Please refresh.", author="System").send()
        return

    # Check for image attachments (TradingView screenshots)
    images = []
    if message.elements:
        for el in message.elements:
            if hasattr(el, "mime") and el.mime and el.mime.startswith("image/"):
                try:
                    path = Path(el.path) if el.path else None
                    if path and path.exists():
                        raw = path.read_bytes()
                        b64 = base64.b64encode(raw).decode()
                        images.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": el.mime,
                                "data": b64,
                            },
                        })
                except Exception as exc:
                    log.warning("Could not read image attachment: %s", exc)

    await agent.handle_message(message.content, images=images if images else None)


# ── Session end ────────────────────────────────────────────────────────────────

@cl.on_stop
async def on_stop():
    session_id = cl.user_session.get("session_id")
    store: ConversationStore = cl.user_session.get("store")
    loader: ContextLoader = cl.user_session.get("loader")

    if loader:
        loader.stop_watching()

    if store and session_id:
        store.close_session(session_id, metadata={"model": _MODEL})


# ── Order staging action callback ──────────────────────────────────────────────

@cl.action_callback("stage_order")
async def on_stage_order(action: cl.Action):
    """Called when the user clicks 'Stage this order' on an order proposal."""
    from claudia.order_flow import execute_staged_order
    session_id = cl.user_session.get("session_id")
    store: ConversationStore = cl.user_session.get("store")
    await execute_staged_order(action, session_id=session_id, store=store)


@cl.action_callback("cancel_proposal")
async def on_cancel_proposal(action: cl.Action):
    await cl.Message(content="Order proposal cancelled.", author="ClaudIA").send()
    await action.remove()
