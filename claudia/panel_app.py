"""Panel entry point for ClaudIA (Phase 2: walking skeleton).

Standalone FastAPI app, mounted via panel.io.fastapi.add_application — deliberately
its own process (a distinct dev port), not importing claudia/app.py's Chainlit
FastAPI instance, so this can be built and tested fully on the side per the kickoff
prompt's isolation instruction. Phase 11 (cutover) is where this becomes the sole
entry point.

Run with:  uvicorn claudia.panel_app:app --port 8001 --reload
"""

import logging
import os
import uuid

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
from claudia.panel_sink import PanelMessageSink

log = logging.getLogger(__name__)

load_dotenv(override=False)

_MODEL = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
_DOCS_PATH = os.environ.get("CLAUDIA_DOCS_PATH", "docs")
_DB_PATH = os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db")
_PANEL_PORT = int(os.environ.get("CLAUDIA_PANEL_PORT", "8001"))

_toolkit: ClaudeToolkit | None = None
_conv_store: ConversationStore | None = None


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


def _build_chat_app() -> pn.chat.ChatInterface:
    """Per-session factory: called fresh for each new browser session by Bokeh's
    _eval_panel (confirmed live against Panel 1.9.3 — see Phase 2 header note),
    so a plain local ClaudIAAgent + PanelMessageSink here already gives correct
    per-session isolation with no extra session registry needed."""
    session_id = str(uuid.uuid4())
    toolkit = _get_toolkit()
    store = _get_store()
    store.create_session(session_id)

    loader = ContextLoader(_DOCS_PATH)
    loader.load_system_prompt()  # validates docs exist before proceeding

    chat = pn.chat.ChatInterface()
    sink = PanelMessageSink(chat=chat, session_id=session_id, store=store)
    agent = ClaudIAAgent(
        toolkit=toolkit,
        store=store,
        context_loader=loader,
        session_id=session_id,
        sink=sink,
        model=_MODEL,
    )

    async def _on_user_input(contents: str, user: str, instance: pn.chat.ChatInterface) -> None:
        try:
            await agent.handle_message(contents)
        except Exception:
            log.exception("Error handling message (session %s)", session_id)
            raise  # Panel's callback_exception="summary" still renders the friendly message

    chat.callback = _on_user_input
    chat.send(
        "**ClaudIA (Panel preview) is ready.** Ask me anything about your portfolio, "
        "markets, or strategy.",
        user="ClaudIA",
        respond=False,
    )
    return chat


app = FastAPI()


@add_application("/", app=app, title="ClaudIA (Panel preview)")
def _serve_chat_app() -> pn.chat.ChatInterface:
    return _build_chat_app()
