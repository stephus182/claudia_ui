"""Panel entry point for ClaudIA (Phase 5: session lifecycle — immediate render,
background per-session init, input gated on init completion).

Standalone FastAPI app, mounted via panel.io.fastapi.add_application — deliberately
its own process (a distinct dev port), not importing claudia/app.py's Chainlit
FastAPI instance, so this can be built and tested fully on the side per the kickoff
prompt's isolation instruction. Phase 11 (cutover) is where this becomes the sole
entry point.

Run with:  uvicorn claudia.panel_app:app --port 8001 --reload
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import panel as pn
from dotenv import load_dotenv
from fastapi import FastAPI
from ibkr_core_mcp import (
    BrowserCookieAuth,
    ClaudeToolkit,
    Config,
    GDriveCache,
    IBKRClient,
    SQLiteStore,
)

# panel/io/__init__.py deliberately does not eagerly import its fastapi submodule (fastapi
# is only an optional panel[fastapi] extra, so the base package stays importable without
# it) — confirmed by inspecting the installed 1.9.3 package directly: `import panel as pn`
# alone leaves `pn.io.fastapi` unresolved (AttributeError). Importing add_application
# directly from its defining module is the correct fix, not an attribute-chain off `pn`.
from panel.io.fastapi import add_application

from claudia.agent import ClaudIAAgent
from claudia.context_loader import ContextLoader
from claudia.conversation_store import ConversationStore
from claudia.gdrive_sync import GDriveSync
from claudia.panel_sink import PanelMessageSink

log = logging.getLogger(__name__)

load_dotenv(override=False)

_MODEL = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
_DOCS_PATH = Path(os.environ.get("CLAUDIA_DOCS_PATH", "docs"))
_VERSIONS_PATH = _DOCS_PATH / "versions"
_DB_PATH = Path(os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db"))
_PANEL_PORT = int(os.environ.get("CLAUDIA_PANEL_PORT", "8001"))

_toolkit: ClaudeToolkit | None = None
_conv_store: ConversationStore | None = None
_gdrive_sync: GDriveSync | None = None

# Serializes the check-download-first-store-open section of _init_session across
# concurrently-initializing sessions — see the comment at its acquire site.
_init_lock = asyncio.Lock()


def _get_toolkit() -> ClaudeToolkit:
    """Process-level ClaudeToolkit singleton — identical pattern to claudia/app.py's
    _get_toolkit(), duplicated rather than imported to keep this module fully
    independent of the Chainlit entry point during the transition (see module
    docstring)."""
    global _toolkit
    if _toolkit is None:
        config = Config.from_env()
        ibkr = IBKRClient(
            config=config,
            auth=BrowserCookieAuth(os.environ.get("IBKR_AUTH_BROWSER", "chrome")),
        )
        cache = GDriveCache(config)
        store = SQLiteStore(config)
        _toolkit = ClaudeToolkit(client=ibkr, cache=cache, store=store, config=config)
    return _toolkit


def _get_store() -> ConversationStore:
    global _conv_store
    if _conv_store is None:
        _conv_store = ConversationStore(_DB_PATH)
    return _conv_store


# Duplicated VERBATIM from claudia/app.py's _write_version_snapshot (using this
# module's own _VERSIONS_PATH) — deliberate duplication-for-independence, same
# rationale as _get_toolkit's docstring: panel_app must never import claudia.app,
# which imports chainlit.
def _write_version_snapshot(version: str, context_text: str, principles_text: str) -> None:
    """Write human-readable snapshot to docs/versions/{version}/. No-op if already exists."""
    try:
        version_dir = _VERSIONS_PATH / version
        ctx_file = version_dir / "context.md"
        pri_file = version_dir / "principles.md"
        if ctx_file.exists() and pri_file.exists():
            return
        version_dir.mkdir(parents=True, exist_ok=True)
        ctx_file.write_text(context_text, encoding="utf-8")
        pri_file.write_text(principles_text, encoding="utf-8")
        log.info("Written version snapshot: docs/versions/%s/", version)
    except Exception as exc:
        log.warning("Could not write version snapshot for %s: %s", version, exc)


def _build_chat_app() -> pn.chat.ChatInterface:
    """Per-session factory: called fresh for each new browser session by Bokeh's
    _eval_panel (confirmed live against Panel 1.9.3 — see Phase 2 header note).

    Phase 5 design (see 'Phase 5 design decisions' in the migration plan): only the
    chat surface is built synchronously — everything else (GDrive download, store,
    loader, agent) runs in a background _init_session task on the session's own event
    loop, with user input gated on an asyncio.Event so an early message waits for
    init instead of racing it or erroring.
    """
    session_id = str(uuid.uuid4())
    chat = pn.chat.ChatInterface()

    # store/loader are written (not yet read here) for Tasks 5.6/5.7's session-end
    # cleanup consumers; init_task keeps a strong reference to the background task.
    _session: dict[str, Any] = {
        "agent": None,
        "error": None,
        "store": None,
        "loader": None,
        "init_task": None,
    }
    _init_done = asyncio.Event()

    async def _on_user_input(contents: str, user: str, instance: pn.chat.ChatInterface) -> None:
        await _init_done.wait()
        agent = _session["agent"]
        if agent is None:
            error = _session["error"]
            # "Setup required" errors already carry their own label — re-prefixing
            # "Session init failed:" would double-label the same problem.
            label = "" if str(error).startswith("Setup required") else "**Session init failed:** "
            chat.send(
                f"{label}{error} — check the server logs and reload the page.",
                user="System",
                respond=False,
            )
            return
        try:
            await agent.handle_message(contents)
        except Exception:
            log.exception("Error handling message (session %s)", session_id)
            raise  # Panel's callback_exception="summary" still renders the friendly message

    chat.callback = _on_user_input
    chat.send(
        "**ClaudIA is ready** — gathering your account status…",  # status block lands in Task 5.3
        user="ClaudIA",
        respond=False,
    )

    async def _init_session() -> None:
        global _gdrive_sync
        try:
            # GDrive DB download — MUST complete before ConversationStore first opens
            # the DB file (design D1). Unlike app.py, whose download is synchronous and
            # therefore accidentally atomic (no await, no interleaving possible), the
            # asyncio.to_thread below opens a yield window: without the lock, session A
            # could set _gdrive_sync and await the download while session B's init sees
            # _gdrive_sync already set, skips the branch WITHOUT waiting, and opens
            # ConversationStore on the old DB file the download thread is about to
            # atomically replace — B's sqlite connection would hold the unlinked inode
            # and its writes would be silently lost. The lock serializes
            # check + download + first-store-open, so B blocks until A's download
            # finishes and then opens the fresh file.
            async with _init_lock:
                if _gdrive_sync is None and os.environ.get("GOOGLE_DRIVE_FOLDER_ID"):
                    # Deliberately OUTSIDE the Drive try below: a Config failure is
                    # env-wide (toolkit construction needs the same Config later), so
                    # swallowing it as "continuing without Drive sync" would mislead —
                    # and init would then fail identically on the toolkit anyway.
                    cfg = Config.from_env()
                    try:
                        _gdrive_sync = GDriveSync(cfg)
                        if _conv_store is None:
                            await asyncio.to_thread(_gdrive_sync.download_db, _DB_PATH)
                    except Exception as exc:
                        log.warning(
                            "GDriveSync setup failed: %s — continuing without Drive sync", exc
                        )

                toolkit = _get_toolkit()
                store = _get_store()

                # Read context/principles from Drive every session so each session picks
                # up the latest version (app.py:256-262 parity; read_text falls back to
                # the local file itself when Drive is unreachable or the file is absent).
                # Deliberately INSIDE _init_lock: googleapiclient binds a single
                # AuthorizedHttp/httplib2.Http to the built Drive service, shared by
                # every .execute(), and httplib2.Http is not thread-safe — concurrent
                # session inits would run read_text on that one connection from two
                # worker threads (worst case: interleaved socket reads that still parse,
                # handing a session the wrong document content silently). Serializing
                # the per-session reads costs ~nothing for a single-user app.
                drive_context: str | None = None
                drive_principles: str | None = None
                if _gdrive_sync is not None:
                    drive_context = await asyncio.to_thread(
                        _gdrive_sync.read_text,
                        "context.md",
                        local_path=_DOCS_PATH / "context.md",
                    )
                    drive_principles = await asyncio.to_thread(
                        _gdrive_sync.read_text,
                        "principles.md",
                        local_path=_DOCS_PATH / "principles.md",
                    )

            loader = ContextLoader(
                _DOCS_PATH, context_text=drive_context, principles_text=drive_principles
            )
            try:
                loader.load_system_prompt()  # validate docs exist before proceeding
            except FileNotFoundError as exc:
                _session["error"] = f"Setup required: {exc}"
                chat.send(
                    f"**Setup required:** {exc}\n\nCreate the missing file and reload.",
                    user="System",
                    respond=False,
                )
                return

            # Register document version (idempotent) + snapshot + hash-change alert
            context_text, principles_text = loader.get_effective_texts()
            current_hash = loader.compute_hash()
            version_label = store.register_doc_version_if_new(
                current_hash, context_text, principles_text
            )
            log.info("Active document version: %s", version_label)
            _write_version_snapshot(version_label, context_text, principles_text)

            # Must run BEFORE this session's create_session below — get_last_context_hash
            # reads the newest session row, so inserting ours first would make it see its
            # own hash and the hash-change warning would never fire again.
            prev_hash = store.get_last_context_hash()
            if prev_hash is not None and prev_hash != current_hash:
                prev_version = store.get_version_label(prev_hash) or f"unknown ({prev_hash[:8]})"
                chat.send(
                    f"**WARNING: context.md / principles.md changed: "
                    f"{prev_version} → {version_label}.**\n"
                    "Please verify the content before continuing.",
                    user="System",
                    respond=False,
                )

            store.create_session(
                session_id, context_hash=current_hash, doc_version=version_label
            )

            sink = PanelMessageSink(chat=chat, session_id=session_id, store=store)
            _session["store"] = store
            _session["loader"] = loader
            _session["agent"] = ClaudIAAgent(
                toolkit=toolkit,
                store=store,
                context_loader=loader,
                session_id=session_id,
                sink=sink,
                model=_MODEL,
                doc_version=version_label,
            )
        except Exception as exc:
            log.exception("Session init failed (session %s)", session_id)
            _session["error"] = str(exc)
            chat.send(
                f"**Session init failed:** {exc} — check the server logs and reload the page.",
                user="System",
                respond=False,
            )
        finally:
            _init_done.set()

    # Safe here: _build_chat_app runs synchronously ON the session's live event loop
    # (verified empirically — see the Phase 5 'Resolved' note), so create_task schedules
    # onto the correct loop with no thread-crossing bridge. The task reference is kept
    # in _session (alive as long as chat holds the callback closure) — the loop itself
    # only weak-refs tasks, so a bare create_task could be GC'd mid-init (ruff RUF006).
    _session["init_task"] = asyncio.create_task(_init_session())
    return chat


app = FastAPI()


@add_application("/", app=app, title="ClaudIA (Panel preview)")
def _serve_chat_app() -> pn.chat.ChatInterface:
    return _build_chat_app()
