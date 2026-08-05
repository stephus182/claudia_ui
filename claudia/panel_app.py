"""Panel entry point for ClaudIA — the sole entry point since the Phase 11 cutover
removed the Chainlit app (claudia/app.py) and the chainlit dependency.

Owns the whole session lifecycle: background init, Drive document download +
versioning, opening status, document hot-reload alerts, process-wide backend
singletons, per-session connectivity indicators and chat-alert delivery, the
TradingView bridge, the external candlestick chart pane, the live dashboard
(KPI strip + Positions/P&L tabs), screenshot upload, background Flex sync, and
session-end cleanup.

Also owns the app's single `pn.extension(...)` call and `pn.config` settings — see
the block below `load_dotenv`. That call is shared with the deferred restyle track,
so it is changed deliberately and in one place, never as a side effect.

Served by Panel's own first-class Tornado server via pn.serve(callable) — the
native-serving principle (2026-07-24): Panel-native serving, no workarounds.
pn.serve calls _build_session_root once per browser session; module-level
singletons stay process-wide.

**Bound to loopback only** (`address="127.0.0.1"` in main()). There is no auth layer,
so that bind is the security boundary — see SECURITY.md §8 before changing it.

Run with:  python -m claudia.panel_app
"""

import asyncio
import base64
import io
import logging
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import panel as pn
from dotenv import load_dotenv
from ibkr_core_mcp import (
    BrowserCookieAuth,
    ClaudeToolkit,
    Config,
    GDriveCache,
    IBKRClient,
    SQLiteStore,
)
from ibkr_core_mcp.gateway import GatewayManager

from claudia.agent import ClaudIAAgent, warn_if_model_lacks_operator_channel
from claudia.context_loader import ContextLoader
from claudia.conversation_store import ConversationStore
from claudia.dashboard_poller import DashboardPoller
from claudia.execution_listener import ExecutionListener
from claudia.flex_sync import dataset_fingerprint, validate_dataset
from claudia.gateway_preflight import (
    EXIT_BORROWED,
    EXIT_READY,
    gateway_url,
    read_state,
    verdict,
    warn_if_session_borrowed,
)
from claudia.gdrive_sync import GDriveSync
from claudia.install_check import warn_if_stale
from claudia.opening_status import build_trade_lines, gather_session_state
from claudia.panel_chart import build_chart_pane
from claudia.panel_dashboard import build_dashboard
from claudia.panel_markdown import safe_markdown
from claudia.panel_sink import PanelMessageSink
from claudia.session_reporter import generate_session_report
from claudia.status import ConnectivityChecker, ServiceStatus
from claudia.tradingview import TradingViewBridge, check_cdp_running, launch_tradingview

log = logging.getLogger(__name__)

load_dotenv(override=False)

# The one and only pn.extension() call in this app, at import time so it runs before any
# session Document exists (Panel collects the extension's JS into the page template).
#
# `tabulator` is required — without it the dashboard's positions table renders as a blank
# rectangle. An unrecognised name does NOT raise: panel logs "<name> extension not
# recognized and will be skipped" via param.warning and carries on (panel/config.py), so
# a typo yields a silently non-functional table. tests/test_panel_app.py therefore
# asserts on `pn.extension._loaded_extensions`, never on the argument string.
#
# `notifications=True` is not cosmetic: it is what `pn.config.reconnect` needs in order
# to tell the user anything. Without it `pn.state.notifications` is None and a dropped
# socket reconnects — or fails to — in silence.
#
# `design=` / `theme=` are deliberately absent. They belong to the deferred restyle
# track (docs/panel/ui-design-reference.md §8.3), and this call is shared with it: adding
# a table must not silently pre-empt those decisions.
pn.extension("tabulator", notifications=True)

# Automatic WebSocket reconnect with exponential backoff (1/2/4/8/16/32s). Requires
# panel >= 1.8 and bokeh >= 3.8, both floored in pyproject.toml. A trading session that
# silently loses its socket keeps showing the last numbers it received forever — the
# dashboard's staleness line covers the poller failing, but not the browser being
# disconnected from a poller that is still running happily.
pn.config.reconnect = True

_MODEL = os.environ.get("CLAUDIA_MODEL", "claude-opus-4-8")
_DOCS_PATH = Path(os.environ.get("CLAUDIA_DOCS_PATH", "docs"))
_VERSIONS_PATH = _DOCS_PATH / "versions"
_DB_PATH = Path(os.environ.get("CLAUDIA_DB_PATH", "data/claudia.db"))
_PANEL_PORT = int(os.environ.get("CLAUDIA_PANEL_PORT", "8001"))

# ── Module state & process-level singletons ───────────────────────────────────

_toolkit: ClaudeToolkit | None = None
_conv_store: ConversationStore | None = None
_gdrive_sync: GDriveSync | None = None
_connectivity_checker: ConnectivityChecker | None = None
_execution_listener: ExecutionListener | None = None
# Process-wide, exactly like the checker above: one 15s poll serves every browser
# session, and each session's 5s repaint reads its cache with no I/O at all.
_dashboard_poller: DashboardPoller | None = None

# Process-level TradingViewBridge singleton (Phase 9). Originally mirrored from the old
# claudia/app.py; app.py was removed in the Phase 11 cutover, so this is now the only copy.
_tv_bridge: TradingViewBridge | None = None
_tv_bridge_lock = asyncio.Lock()

# Strong references to destroy-hook cleanup tasks — the loop only weak-refs
# tasks, so a bare create_task could be GC'd mid-cleanup (ruff RUF006).
# Task[str]: _run_session_cleanup returns the status string, logged on the
# destroy path by the done callback.
_cleanup_tasks: set[asyncio.Task[str]] = set()

# Strong references to fire-and-forget background Flex-sync tasks — same
# GC-protection rationale as _cleanup_tasks (ruff RUF006).
_background_tasks: set[asyncio.Task[None]] = set()

# Serializes the check-download-first-store-open section of _init_session across
# concurrently-initializing sessions — see the comment at its acquire site.
_init_lock = asyncio.Lock()


def _get_toolkit() -> ClaudeToolkit:
    """Return the process-level ClaudeToolkit singleton, building it on first call.

    Shared by every session: one ClaudeToolkit, and therefore one IBKRClient, cache, and
    SQLiteStore, for the whole process. Session isolation covers UI state only — the IBKR
    account behind it is process-global, which is sound because ClaudIA is single-user by
    design (SECURITY.md §8).

    Originally duplicated from the old claudia/app.py; app.py was removed in the Phase 11
    cutover, so this is now the only copy.
    """
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
    """Return the process-level ConversationStore singleton, opening it on first call.

    Shared across sessions, like _get_toolkit's. The store opens a connection per
    operation, so concurrent sessions do not contend on a single handle.
    """
    global _conv_store
    if _conv_store is None:
        _conv_store = ConversationStore(_DB_PATH)
    return _conv_store


async def _get_tv_bridge() -> TradingViewBridge:
    """Return the process-level TradingViewBridge singleton, starting it on first call.

    _tv_bridge_lock prevents a double-start race when two sessions initialise
    concurrently. _tv_bridge is only assigned after start() succeeds, so a failed start
    leaves it None and the next caller will retry.

    Originally duplicated from the old claudia/app.py; app.py was removed in the Phase 11
    cutover, so this is now the only copy.
    """
    global _tv_bridge
    async with _tv_bridge_lock:
        if _tv_bridge is None:
            bridge = TradingViewBridge()
            await bridge.start()  # only assign if start() succeeds; keeps _tv_bridge None on failure
            _tv_bridge = bridge
    return _tv_bridge


# ── Init-flow helpers (in _init_session call order) ──────────────────────────


# The canonical doc-version snapshot writer (originally duplicated from the old
# claudia/app.py during the migration to keep panel_app independent; app.py was
# removed in the Phase 11 cutover, so this is now the only copy).
def _write_version_snapshot(version: str, context_text: str, principles_text: str) -> None:
    """Write a human-readable snapshot of the private docs to docs/versions/{version}/.

    No-op if the pair already exists — snapshots are immutable once written.

    Both files are chmod 0o600 after writing. Path.write_text() honours the process umask
    (0644 on a default macOS setup), and these snapshots hold the full verbatim content of
    context.md/principles.md — ClaudIA's persona and the user's trading rules. This mirrors
    the token-file reasoning in SECURITY.md §7: creation flags only set the mode on files
    that did not already exist, so the chmod must be unconditional and after every write.
    Without it, each new doc version silently re-created world-readable copies
    (security-audit-2026-07-25.md, M-2).

    Args:
        version: Version label from ConversationStore, e.g. "v3". Never user-supplied.
        context_text: Full effective context.md content for this version.
        principles_text: Full effective principles.md content for this version.

    Never raises — a snapshot failure is logged and swallowed so it cannot block session
    init; the version row in the DB is the authoritative record, the files are a convenience.
    """
    try:
        version_dir = _VERSIONS_PATH / version
        ctx_file = version_dir / "context.md"
        pri_file = version_dir / "principles.md"
        if ctx_file.exists() and pri_file.exists():
            return
        version_dir.mkdir(parents=True, exist_ok=True)
        ctx_file.write_text(context_text, encoding="utf-8")
        pri_file.write_text(principles_text, encoding="utf-8")
        ctx_file.chmod(0o600)
        pri_file.chmod(0o600)
        log.info("Written version snapshot: docs/versions/%s/", version)
    except Exception as exc:
        log.warning("Could not write version snapshot for %s: %s", version, exc)


async def _read_context_docs() -> tuple[str | None, str | None]:
    """Read context.md/principles.md via Drive (read_text falls back to the local
    file when Drive is unreachable or the file is absent) — parity with the removed app.py.
    MUST be called while holding _init_lock: googleapiclient binds a single
    AuthorizedHttp/httplib2.Http to the built Drive service, shared by every
    .execute(), and httplib2.Http is not thread-safe — concurrent session inits
    would run read_text on that one connection from two worker threads (worst
    case: interleaved socket reads that still parse, handing a session the wrong
    document content silently). Serializing the per-session reads costs ~nothing
    for a single-user app."""
    if _gdrive_sync is None:
        return None, None
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
    return drive_context, drive_principles


def _register_doc_version(
    store: ConversationStore, loader: ContextLoader
) -> tuple[str, str, str | None]:
    """Register the current doc version (idempotent), write the human-readable
    snapshot, and detect a hash change vs the previous session. Returns
    (current_hash, version_label, hash_change_warning_or_None) — UI-free by
    design: the caller decides how to surface the warning.

    ORDERING INVARIANT: get_last_context_hash reads the newest session row, so
    this helper must run BEFORE the session's own create_session — inserting the
    new row first would make it see its own hash and the security warning would
    never fire again."""
    context_text, principles_text = loader.get_effective_texts()
    current_hash = loader.compute_hash()
    version_label = store.register_doc_version_if_new(
        current_hash, context_text, principles_text
    )
    log.info("Active document version: %s", version_label)
    _write_version_snapshot(version_label, context_text, principles_text)

    warning: str | None = None
    prev_hash = store.get_last_context_hash()
    if prev_hash is not None and prev_hash != current_hash:
        prev_version = store.get_version_label(prev_hash) or f"unknown ({prev_hash[:8]})"
        warning = (
            f"**WARNING: context.md / principles.md changed: "
            f"{prev_version} → {version_label}.**\n"
            "Please verify the content before continuing."
        )
    return current_hash, version_label, warning


async def _connect_tradingview(agent: ClaudIAAgent) -> bool:
    """Connect the tradingview-mcp sidecar and merge its tools into this session's
    agent. Returns tv_offline (True → offer the "Launch TradingView" button).

    Computes tv_offline as the removed app.py did; the agent/checker wiring mirrors
    its constructor injection (ClaudIAAgent(tv_bridge=...),
    ConnectivityChecker.set_tv_bridge) — panel_app uses set_tv_bridge
    here because TV connects AFTER the agent is built. The status STRING is not
    ported (the Panel status block renders its own TV line via
    _send_opening_status). On success — sidecar up AND TradingView Desktop's CDP
    port (9222) open — wires the bridge into the agent (tool merge) and the
    ConnectivityChecker. Every other outcome (sidecar up but CDP down, no tools,
    or ANY exception) logs and returns tv_offline=True.

    TV is OPTIONAL: a TV failure must NEVER block init, so the whole body is
    wrapped — parity with app.py's try/except that degrades silently
    (project-tradingview-robustness memory)."""
    try:
        bridge = await _get_tv_bridge()
        tools = bridge.get_tools()
        # The sidecar can start without TradingView Desktop; CDP is the separate
        # liveness check that the Desktop app is actually reachable for live tools.
        cdp_up = check_cdp_running()
        if tools and cdp_up:
            agent.set_tv_bridge(bridge, tools)
            if _connectivity_checker is not None:
                _connectivity_checker.set_tv_bridge(bridge)
            return False
        if tools and not cdp_up:
            log.warning(
                "tradingview-mcp sidecar up but TradingView Desktop not running "
                "(CDP port 9222 closed)"
            )
            return True
        return True
    except Exception as exc:
        log.warning("tradingview-mcp sidecar not available: %s", exc)
        return True


async def _send_opening_status(
    chat: pn.chat.ChatInterface, toolkit: ClaudeToolkit, tv_offline: bool
) -> tuple[str | None, bool]:
    """Send the second chat message — what is *not* working, plus the local dataset — and
    return (trade_context, ibkr_offline): the trade/calendar context for the caller to
    stamp on agent._trade_context, and the offline flag so _init_session can
    decide whether to offer the Start-Gateway button (Task 5.3/5.6b —
    parity with the removed app.py). tv_offline (from _connect_tradingview) drives the
    TradingView status line. Effectively non-raising: both builders catch their
    own IBKR/store failures internally and degrade to offline/fallback text; an
    unexpected escape is caught by _init_session's generic handler.

    The IBKR half is a caveat, not a report: `gather_session_state` returns `""` when both
    halves of the gateway answer, and the empty part is dropped rather than sent as a
    leading blank line. Account figures and the order book live in the dashboard and are
    no longer echoed here (2026-08-05) — so on a healthy start this message is two lines:
    the trade dataset and TradingView."""
    ibkr_caveat, ibkr_offline = await gather_session_state(toolkit)
    trade_status, trade_context = await asyncio.to_thread(
        build_trade_lines, toolkit, ibkr_offline
    )
    tv_line = (
        "_TradingView: not connected — click Launch below._"
        if tv_offline
        else "_TradingView: connected._"
    )
    parts = ([ibkr_caveat] if ibkr_caveat else []) + [f"_{trade_status}_", tv_line]
    chat.send("\n\n".join(parts), user="ClaudIA", respond=False)
    return trade_context, ibkr_offline


_INDICATOR_LABELS = {"ibkr": "IBKR", "gdrive": "GDrive", "tv": "TradingView"}


def _make_status_indicators() -> dict[str, pn.indicators.BooleanStatus]:
    """One BooleanStatus per service, keyed like ConnectivityChecker.get_status().
    UNKNOWN at build: gray dot for not-yet-checked, not an error. label= (not
    name=) — Widget.name raises PendingDeprecationWarning on Panel 1.9.3
    (probe-verified, same as the Task 5.6b Button finding)."""
    return {
        key: pn.indicators.BooleanStatus(value=False, color="dark", label=label)
        for key, label in _INDICATOR_LABELS.items()
    }


def _apply_status(
    indicators: dict[str, pn.indicators.BooleanStatus], status: dict[str, ServiceStatus]
) -> None:
    """ServiceStatus → (value, color): OK → lit green, ERROR → lit red,
    UNKNOWN → unlit gray (matches _run_checks()' not-configured-is-not-an-error
    rule). Unknown keys in either direction are ignored."""
    # Literal-typed so mypy accepts assignment into BooleanStatus.color's
    # Literal['primary', ..., 'dark'] param type.
    mapping: dict[ServiceStatus, tuple[bool, Literal["success", "danger", "dark"]]] = {
        ServiceStatus.OK: (True, "success"),
        ServiceStatus.ERROR: (True, "danger"),
        ServiceStatus.UNKNOWN: (False, "dark"),
    }
    for key, ind in indicators.items():
        if key in status:
            value, color = mapping[status[key]]
            ind.value, ind.color = value, color


def _make_alert_subscriber(chat: pn.chat.ChatInterface) -> Callable[[str], Awaitable[None]]:
    """Async subscriber for ConnectivityChecker alerts — texts arrive
    pre-formatted (_DISCONNECT_MESSAGES/_RECONNECT_MESSAGES). Runs inside the
    checker's poll task on the same process-wide loop as the session (V2/V3
    probe basis), so a direct chat.send is safe; a closed session's send is a
    harmless no-op (V4)."""
    async def _on_alert(text: str) -> None:
        """Deliver one connectivity alert into this session's chat feed."""
        chat.send(text, user="System", respond=False)

    return _on_alert


def _send_action_buttons(
    chat: pn.chat.ChatInterface,
    _session: dict[str, Any],
    session_id: str,
    ibkr_offline: bool,
    tv_offline: bool,
) -> None:
    """End Session (always) + Start IBKR Gateway (when IBKR offline) + Launch
    TradingView (when TV offline) — app.py action-button parity, Phase 3 widget
    pattern (disable-first async handlers)."""
    end_btn = pn.widgets.Button(label="End Session", color="light")
    buttons: list[pn.widgets.Button] = [end_btn]

    async def _on_end(event: Any) -> None:
        """End the session from the button: disable controls, unsubscribe, run cleanup.

        Idempotent — the `_session["closed"]` guard means clicking End Session and then
        closing the tab (which fires the destroy hook) cleans up exactly once.
        """
        for b in buttons:
            b.disabled = True
        if _session["closed"]:
            return
        unsub = _session["unsubscribe"]
        if unsub is not None:
            unsub()
            _session["unsubscribe"] = None
        _session["closed"] = True
        chat.send("Saving session…", user="System", respond=False)
        status = await _run_session_cleanup(session_id, _session["store"], _session["loader"])
        chat.send(
            f"**Session ended.** {status}\n\nSafe to close this tab.",
            user="System", respond=False,
        )

    end_btn.on_click(_on_end)

    if ibkr_offline:
        gw_btn = pn.widgets.Button(label="Start IBKR Gateway", color="primary")
        buttons.append(gw_btn)

        async def _on_start_gateway(event: Any) -> None:
            """Start the IBKR gateway from chat: Docker → container → wait → login page.

            Runs the blocking GatewayManager calls on worker threads, waits up to 120s for
            reachability, opens the browser login page, then forces a connectivity re-check
            so the status dot reflects reality without waiting for the next 60s poll.
            Every branch, including each failure, reports honestly into the chat feed.
            """
            gw_btn.disabled = True
            gm = GatewayManager()
            try:
                chat.send("▶ Ensuring Docker is running…", user="System", respond=False)
                await asyncio.to_thread(gm.ensure_docker_running)
                chat.send("▶ Starting IBKR gateway container…", user="System", respond=False)
                await asyncio.to_thread(gm.start)
                chat.send("▶ Waiting for gateway to be reachable (up to 120s)…",
                          user="System", respond=False)
                reachable = await asyncio.to_thread(gm.wait_for_gateway)
                if not reachable:
                    chat.send("✕ Gateway did not start within timeout. Check Docker logs.",
                              user="System", respond=False)
                    return

                # Pre-flight BEFORE sending anyone to the login page. Reachable only means
                # the process answers; it says nothing about whose session it holds. Two
                # states must not lead to a login: one that already works (re-authenticating
                # it for nothing is what escalates into the IB Key challenge/response) and
                # one held by another IBKR app (which cannot be authenticated here at all,
                # so every retry is wasted and makes the next attempt worse).
                state = await asyncio.to_thread(read_state, gateway_url())
                code, headline, guidance = verdict(state)
                if code == EXIT_READY:
                    chat.send(f"✅ **{headline}** — {guidance}", user="System", respond=False)
                    return
                if code == EXIT_BORROWED:
                    chat.send(
                        f"⚠️ **LOG OUT OF YOUR IBKR APP FIRST — {headline}**\n\n{guidance}\n\n"
                        "The login page has **not** been opened: doing so now would fail and "
                        "count as another attempt. Log out, then click **Start IBKR Gateway** "
                        "again.",
                        user="System", respond=False,
                    )
                    return

                await asyncio.to_thread(gm.open_login_page)
                if _connectivity_checker is not None:
                    await _connectivity_checker._run_checks()
                chat.send(
                    "✅ IBKR Gateway is reachable. **https://localhost:5055** opened in "
                    "your browser.\n\nComplete the IBKR login and 2FA. ClaudIA will "
                    "notify you here once the session is authenticated.",
                    user="System", respond=False,
                )
            except Exception as exc:
                log.error("Gateway startup failed: %s", exc)
                chat.send(f"✕ Gateway startup failed: {exc}", user="System", respond=False)

        gw_btn.on_click(_on_start_gateway)

    if tv_offline:
        tv_btn = pn.widgets.Button(label="Launch TradingView", color="warning")
        buttons.append(tv_btn)

        async def _on_launch_tv(event: Any) -> None:
            """Launch TradingView Desktop with CDP debugging, then rebuild the
            sidecar bridge and merge its tools into this session's agent —
            Parity with the removed app.py. Every failure path is an honest chat message,
            never a crash (project-tradingview-robustness memory)."""
            global _tv_bridge
            tv_btn.disabled = True
            try:
                chat.send("▶ Launching TradingView Desktop with remote debugging…",
                          user="System", respond=False)
                launched = await launch_tradingview()
                if not launched:
                    chat.send(
                        "✕ TradingView Desktop did not open its debug port within 30s.\n\n"
                        "If it's already running without the debug port, it can't be "
                        "fixed in place — run the one-command quit+relaunch helper:\n"
                        "```\n./scripts/launch-tradingview-debug.sh\n```",
                        user="System", respond=False,
                    )
                    return
                chat.send("▶ Connecting tradingview-mcp sidecar…",
                          user="System", respond=False)
                async with _tv_bridge_lock:
                    if _tv_bridge is not None:
                        await _tv_bridge.stop()
                        _tv_bridge = None
                bridge = await _get_tv_bridge()  # creates a fresh bridge under its own lock
                if _connectivity_checker is not None:
                    _connectivity_checker.set_tv_bridge(bridge)
                tv_tools = bridge.get_tools()
                agent = _session.get("agent")
                if agent is not None:
                    agent.set_tv_bridge(bridge, tv_tools)
                chat.send(
                    f"✅ TradingView connected ({len(tv_tools)} tools available).",
                    user="System", respond=False,
                )
            except Exception as exc:
                log.error("TradingView launch failed: %s", exc)
                chat.send(f"✕ TradingView launch failed: {exc}",
                          user="System", respond=False)

        tv_btn.on_click(_on_launch_tv)

    chat.send(pn.Row(*buttons), user="System", respond=False)


async def _maybe_background_flex_sync(
    chat: pn.chat.ChatInterface, toolkit: ClaudeToolkit, ibkr_offline: bool
) -> None:
    """Startup Flex sync decision + background sync (parity with the removed app.py).

    Decision (fast sqlite, threaded) runs inline; only the actual work — Flex API call,
    conditional Drive backup, re-validation — is spawned as a background task. Logic:

    1. **Coverage** check first (SQLite, no API): if the data is current, skip. This is
       `get_trade_date_coverage`, which is an *activity report* — it counts trades and
       finds date gaps. It is deliberately **not** called an integrity check here: that
       mislabel is what let "integrity validated" sit unearned in the opening line for
       months. The real checks are `flex_sync.validate_dataset`.
    2. Only if stale: check the logs for a recent attempt (<4h) — avoid hammering the
       rate-limited Flex API on restarts.
    3. Only if stale AND no recent attempt: call the Flex API.

    **Where validation happens, and why not here.** Every session start validates the
    dataset through `opening_status.build_trade_lines`, which runs whether or not a sync
    was due — this function returns early when Flex is unconfigured or IBKR is offline,
    so hanging validation off it would skip exactly the sessions where a stale or damaged
    store matters most. It re-validates after a *successful* sync because a pull is the
    one thing in a session that can change the answer.
    """
    cfg = toolkit._config
    if not (cfg and cfg.flex_token and cfg.flex_query_id) or ibkr_offline:
        return

    should_sync = False
    skip_reason = ""
    try:
        cov = await asyncio.to_thread(toolkit._store.get_trade_date_coverage)
        if not cov.get("stale"):
            skip_reason = (
                f"data current (newest: {cov['newest']}, "
                f"last trading day: {cov.get('last_trading_day')})"
            )
        else:
            last_attempts = await asyncio.to_thread(
                toolkit._store.get_log, n=1, event="flex_sync"
            )
            if last_attempts:
                last_ts = datetime.fromisoformat(last_attempts[0]["ts"]).replace(tzinfo=UTC)
                hours_since = (datetime.now(UTC) - last_ts).total_seconds() / 3600
                if hours_since < 4:
                    skip_reason = (
                        f"already attempted {hours_since:.1f}h ago (newest: {cov['newest']})"
                    )
                else:
                    should_sync = True
            else:
                should_sync = True  # never synced
    except Exception:
        should_sync = True  # on any check failure, attempt sync

    if skip_reason:
        log.info("Startup Flex sync skipped — %s", skip_reason)
        return
    if not should_sync:
        return

    async def _background_flex_sync() -> None:
        """Run the Flex trade sync off the critical path, then back up store.db to Drive.

        Fire-and-forget: **never raises**. A sync failure falls back to reporting
        `check_flex_coverage` so the user learns what data is actually present rather than
        just that something broke.

        The backup is **conditional on the data having moved** (2026-08-05). Flex is T+1,
        so one pull per session is the entire refresh budget and an unchanged 53 MB file
        costs 21 measured seconds to re-send for nothing. `dataset_fingerprint` decides;
        an unreadable fingerprint uploads, because unknown is not "unchanged".
        """
        try:
            before = await asyncio.to_thread(dataset_fingerprint, cfg.sqlite_path)
            result, _ = await asyncio.to_thread(toolkit.execute, "sync_flex_trades", {})
            chat.send(f"✅ {result}", user="System", respond=False)
            after = await asyncio.to_thread(dataset_fingerprint, cfg.sqlite_path)
            # Back up the updated store.db to Drive account_data/.
            # upload_account_sqlite, not upload_account_file: store.db runs in WAL mode, so
            # a raw byte read would miss commits still sitting in store.db-wal and could tear
            # mid-checkpoint. Same consistent-snapshot guarantee GDriveSync.upload_db gives
            # claudia.db.
            if before is not None and after is not None and before == after:
                log.info("store.db unchanged by this pull — Drive backup left as is")
            else:
                try:
                    await asyncio.to_thread(
                        toolkit._cache.upload_account_sqlite, cfg.sqlite_path, "store.db"
                    )
                    log.info("store.db backed up to Drive account_data/")
                except Exception as backup_exc:
                    log.warning("store.db Drive backup failed: %s", backup_exc)
            # A backup is only worth having if what it holds is sound, and the dashboard's
            # realised windows are computed from exactly these rows — so re-check after a
            # pull, and say so on screen rather than only in a log nobody is reading.
            validity = await asyncio.to_thread(validate_dataset, cfg.sqlite_path)
            if not validity.ok:
                log.error("Flex dataset validation FAILED after sync — %s", validity.summary)
                chat.send(
                    f"⚠ Trade dataset failed validation after the sync — {validity.summary}. "
                    f"The realised P&L figures on the dashboard are computed from these rows; "
                    f"treat them as unverified until this is resolved.",
                    user="System", respond=False,
                )
        except Exception as exc:
            log.warning("Background Flex sync failed: %s", exc)
            # Sync failed — still run integrity check so data status is known
            try:
                cov_result, _ = await asyncio.to_thread(
                    toolkit.execute, "check_flex_coverage", {}
                )
                chat.send(
                    f"⚠ Sync failed: {exc}. Run `sync_flex_trades` manually.\n\n{cov_result}",
                    user="System", respond=False,
                )
            except Exception:
                chat.send(
                    f"⚠ Trade data sync failed: {exc}. Run `sync_flex_trades` manually.",
                    user="System", respond=False,
                )

    task = asyncio.get_running_loop().create_task(_background_flex_sync())
    _background_tasks.add(task)

    def _log_flex_done(t: asyncio.Task[None]) -> None:
        """Drop the strong task reference (RUF006) and log a task that died."""
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("Background Flex sync task died", exc_info=t.exception())

    task.add_done_callback(_log_flex_done)


# ── Session-end cleanup ───────────────────────────────────────────────────────


async def _run_session_cleanup(
    session_id: str | None,
    store: ConversationStore | None,
    loader: ContextLoader | None,
) -> str:
    """Close session, generate report, upload DB (parity with the removed app.py).
    Returns a one-line status string. The slow calls (report generation, Drive
    upload) are offloaded via asyncio.to_thread; the sqlite row ops and
    stop_watching are ms-scale and run inline (app.py parity) — the destroy-hook
    path runs this on the shared loop, where blocking would freeze every live
    session (V4 probe). NO UI calls: on the destroy path the chat's Document is
    already gutted."""
    if loader:
        loader.stop_watching()

    if store and session_id:
        store.close_session(session_id, metadata={"model": _MODEL})
        connectivity = (
            {k: v.value for k, v in _connectivity_checker.get_status().items()}
            if _connectivity_checker else {}
        )
        session_meta = store.get_session(session_id) or {}
        await asyncio.to_thread(
            generate_session_report,
            session_id, store, connectivity, session_meta.get("doc_version"),
        )
        msg_count = store.count_messages(session_id)
    else:
        msg_count = 0

    drive_note = ""
    if _gdrive_sync is not None:
        try:
            await asyncio.to_thread(_gdrive_sync.upload_db, _DB_PATH)
            drive_note = " · claudia.db → Drive ✅"
        except Exception as exc:
            log.warning("End-session Drive upload failed: %s", exc)
            drive_note = " · Drive upload failed ⚠️"

    return f"{msg_count} messages saved{drive_note}"


# ── Per-session factory ───────────────────────────────────────────────────────


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
    # renderers= routes every string sent through this feed via safe_markdown, closing the
    # raw-HTML/run_scripts execution path (security-audit-2026-07-25.md, H-1). Panel objects
    # (ChatStep, Column, image panes) bypass renderers and are unaffected. Note this must be
    # the top-level `renderers` param — passing it inside message_params raises TypeError,
    # since ChatFeed._build_message already forwards self.renderers explicitly.
    chat = pn.chat.ChatInterface(renderers=[safe_markdown])

    # store/loader are read by the session-end cleanup consumers (End Session
    # button + destroy hook); init_task keeps a strong reference to the
    # background task; closed guards against double cleanup (End Session then
    # tab close — app.py's session_closed parity).
    _session: dict[str, Any] = {
        "agent": None,
        "error": None,
        "store": None,
        "loader": None,
        "init_task": None,
        "closed": False,
        "unsubscribe": None,
    }
    _init_done = asyncio.Event()

    def _on_session_destroyed(session_context: Any) -> None:
        """V4 contract: sync, fires 15-32s after disconnect on the shared loop
        with pn.state.curdoc None — no UI calls; schedule async cleanup and
        return immediately (blocking here freezes every live session)."""
        if _session["closed"]:
            return
        unsub = _session["unsubscribe"]
        if unsub is not None:
            unsub()
            _session["unsubscribe"] = None
        _session["closed"] = True
        task = asyncio.get_running_loop().create_task(
            _run_session_cleanup(session_id, _session["store"], _session["loader"])
        )
        _cleanup_tasks.add(task)

        def _log_cleanup_done(t: asyncio.Task[str]) -> None:
            """Drop the strong task reference (RUF006) and log the cleanup outcome."""
            _cleanup_tasks.discard(t)
            try:
                log.info("Destroy-path cleanup (session %s): %s", session_id, t.result())
            except Exception:
                log.exception("Destroy-path cleanup failed (session %s)", session_id)

        task.add_done_callback(_log_cleanup_done)

    # Registered BEFORE the input callback wiring / init task, while curdoc is
    # set (i.e. on the session Document) — so even a session whose init later
    # fails still gets its cleanup on destroy.
    pn.state.on_session_destroyed(_on_session_destroyed)

    async def _on_user_input(contents: str, user: str, instance: pn.chat.ChatInterface) -> None:
        """ChatInterface callback — the primary chat entry point.

        Blocks on `_init_done` first, so a message typed during startup is answered once
        the agent exists rather than dropped. If init failed there is no agent, and the
        stored error is reported instead of the message being silently swallowed.
        """
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
        # Was "gathering your account status…", which stopped being true on 2026-08-05:
        # the dashboard gathers it, continuously, and this message no longer waits on any
        # account call. What still follows via _send_opening_status is the local dataset
        # line, TradingView, and a caveat if either half of IBKR is down.
        "**ClaudIA is ready** — loading your trade history…",
        user="ClaudIA",
        respond=False,
    )

    # Task 8.1: standalone screenshot upload. ChatInterface's native file tab
    # unpacks its upload wrapper before the callback sees it (mime/filename
    # lost, bare BytesIO delivered — see the ⚠ CORRECTION 2026-07-24 block in
    # the migration plan's Task 8.1 section), so screenshots arrive via this
    # widget instead: FileInput's own public value/mime_type/filename params
    # carry full metadata. accept= is a client-side hint only — the watcher
    # re-checks the mime type server-side.
    file_input = pn.widgets.FileInput(accept="image/*")

    async def _on_file_upload(event: Any) -> None:
        """Send an uploaded screenshot to the agent's vision path.

        All three widget params (`value`, `mime_type`, `filename`) are snapshotted at
        watcher entry, **before** the init await. `FileInput` is a single shared widget, so
        a second upload arriving during the init window would overwrite them mid-flight and
        cross-wire one image with another's metadata.
        """
        if not event.new:
            return  # our own post-processing reset re-fires the watcher with None
        # Snapshot all three params at watcher entry, BEFORE the init await.
        # event.new already snapshots `value`; mime_type/filename must be
        # captured now too, because a second upload arriving during the init
        # window would overwrite them on this shared widget — cross-wiring this
        # upload's bytes with the next one's metadata, i.e. a mislabeled
        # media_type to the vision API. Reading them post-await is the only
        # window (init typically completes in seconds), but a mislabeled
        # screenshot is a data-integrity defect, so close it unconditionally.
        data = event.new
        mime = file_input.mime_type or ""
        filename = file_input.filename
        await _init_done.wait()
        try:
            agent = _session["agent"]
            if agent is None:
                # Mirrors _on_user_input's init-failure branch.
                error = _session["error"]
                label = (
                    "" if str(error).startswith("Setup required") else "**Session init failed:** "
                )
                chat.send(
                    f"{label}{error} — check the server logs and reload the page.",
                    user="System",
                    respond=False,
                )
                return
            if not (isinstance(mime, str) and mime.startswith("image/")):
                chat.send(
                    "Only image attachments are supported (TradingView screenshots).",
                    user="System", respond=False,
                )
                return
            # Echo the screenshot into the feed (the standalone widget renders
            # no message of its own), then hand the agent the Anthropic vision
            # block — parity with the removed app.py.
            chat.send(pn.pane.Image(io.BytesIO(data), width=400), user="User", respond=False)
            block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode(),
                },
            }
            await agent.handle_message(
                f"(screenshot attached: {filename})", images=[block]
            )
        except Exception as exc:
            # Unlike the chat callback, no Panel exception renderer sits above a
            # param watcher — raising would vanish into the async executor, so
            # log AND tell the user honestly.
            log.exception("Screenshot upload failed (session %s)", session_id)
            chat.send(f"**Screenshot upload failed:** {exc}", user="System", respond=False)
        finally:
            # Reset both sides so re-uploading the same file re-fires the
            # watcher (param drops equal-value events): clear() only clears the
            # CLIENT widget (sends ClearInput; no server param change —
            # source-verified, panel/widgets/input.py FileInput.clear), and
            # value/mime_type never sync server→client, so the param update is
            # the server-side reset (its value=None re-fire is caught by the
            # falsy guard above).
            file_input.clear()
            file_input.param.update(value=None, mime_type=None, filename=None)

    file_input.param.watch(_on_file_upload, "value")
    # Handoff for _build_session_root's composition: a plain Python attribute
    # on our own object (not a Panel private). Read back via
    # _screenshot_file_input — the typed accessor keeps mypy's attr checking
    # everywhere else.
    chat._claudia_file_input = file_input  # type: ignore[attr-defined]

    async def _init_session() -> None:
        """Bring one session fully online, in order, as a background task.

        Drive DB download → store open → doc load + versioning → agent construction →
        connectivity checker → execution listener → opening status → TradingView connect →
        background Flex sync. Sets `_init_done` when finished; `_on_user_input` waits on it.

        The Drive download runs under the process-wide `_init_lock`. `asyncio.to_thread`
        opens a yield window the old synchronous path did not have, so without the lock a
        second session could open the DB file while the first is replacing it — leaving the
        first holding an unlinked inode and silently writing to a file nobody will read.

        Never raises: any failure is captured into `_session["error"]` and surfaced on the
        next user message, so a broken startup degrades to an explained UI rather than a
        dead one.
        """
        global _gdrive_sync, _connectivity_checker, _execution_listener, _dashboard_poller
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
                    drive_cfg = Config.from_env()
                    try:
                        _gdrive_sync = GDriveSync(drive_cfg)
                        if _conv_store is None:
                            await asyncio.to_thread(_gdrive_sync.download_db, _DB_PATH)
                    except Exception as exc:
                        log.warning(
                            "GDriveSync setup failed: %s — continuing without Drive sync", exc
                        )

                toolkit = _get_toolkit()
                store = _get_store()

                # Drive reads must stay under the lock — see _read_context_docs.
                drive_context, drive_principles = await _read_context_docs()

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

            # Hot-reload alert (parity with the removed app.py). The watchdog fires in a
            # plain OS thread; the D4-verified loop bridge serializes the entire
            # chat.send onto this session's event loop (see the D4 RESOLVED note
            # in the migration plan — probe + official Panel docs in agreement).
            loop = asyncio.get_running_loop()

            def _on_doc_change(filename: str, new_prompt: str) -> None:
                """Announce a hot-reloaded document — called from the watchdog thread.

                Hops back to the session's event loop via `call_soon_threadsafe`, since
                Panel widgets must not be touched off-loop. A `RuntimeError` means the loop
                already closed (tab gone); that is expected and ignored.
                """
                try:
                    loop.call_soon_threadsafe(
                        partial(
                            chat.send,
                            f"**Document updated:** `{filename}` reloaded. "
                            "Principles apply from your next message.",
                            user="System",
                            respond=False,
                        )
                    )
                except RuntimeError:  # loop closed — session gone, alert moot
                    log.debug("Dropped doc-change alert for closed session %s", session_id)

            loader.start_watching(_on_doc_change)

            # Must run BEFORE this session's create_session below (see the
            # ordering invariant in _register_doc_version's docstring).
            current_hash, version_label, warning = _register_doc_version(store, loader)
            if warning is not None:
                chat.send(warning, user="System", respond=False)

            store.create_session(
                session_id, context_hash=current_hash, doc_version=version_label
            )

            # Backend singletons (design D6, parity with the removed app.py). The
            # checker's 60s /tickle poll is the IBKR session KEEPALIVE — live-
            # session protection, not cosmetics. Constructed once per process,
            # started unconditionally each session (start() is idempotent and
            # restarts a cancelled task). Each session then subscribes its own
            # alert closure (Phase 6) — unsubscribed on End Session AND on the
            # destroy hook. Construction is synchronous
            # (no await between the None-check and assignment on this single-
            # threaded loop), so no lock is needed — same reasoning as
            # Parity with the removed app.py. tv_bridge stays None until Phase 9 (D5).
            cfg = toolkit._config
            if _connectivity_checker is None:
                _connectivity_checker = ConnectivityChecker(
                    gateway_url=cfg.gateway_url,
                    gdrive_token_file=cfg.gdrive_token_file,
                    tv_bridge=None,
                    gdrive_sync=_gdrive_sync,
                )
            _connectivity_checker.start()
            _session["unsubscribe"] = _connectivity_checker.subscribe(
                _make_alert_subscriber(chat)
            )
            if _session["closed"]:
                # Session was destroyed while init was still running — undo the
                # subscription we just made into a dead session.
                _session["unsubscribe"]()
                _session["unsubscribe"] = None
            if _execution_listener is None:
                _execution_listener = ExecutionListener(cfg.gateway_url, toolkit._store)
            _execution_listener.start()
            # Same construct-once/start-each-session shape as the checker above; start() is
            # idempotent and restarts a cancelled task. The poller reads `toolkit.client`
            # directly rather than going through ClaudeToolkit.execute(), which returns
            # rendered markdown rather than data — see claudia/dashboard_data.py.
            if _dashboard_poller is None:
                _dashboard_poller = DashboardPoller(toolkit.client, cfg.sqlite_path)
            _dashboard_poller.start()

            # tv_bridge_getter reads panel_app's live _tv_bridge module global at click
            # time, so a ```pine Inject button reflects a TradingView launched later.
            sink = PanelMessageSink(
                chat=chat,
                session_id=session_id,
                store=store,
                tv_bridge_getter=lambda: _tv_bridge,
            )
            _session["store"] = store
            _session["loader"] = loader
            agent = ClaudIAAgent(
                toolkit=toolkit,
                store=store,
                context_loader=loader,
                session_id=session_id,
                sink=sink,
                model=_MODEL,
                doc_version=version_label,
            )
            # TradingView connect + tool merge (parity with the removed app.py, deferred by
            # D5 into Phase 9). Runs before the opening status so the TV status line
            # reflects the real connection state, and before the buttons so tv_offline
            # decides whether to offer "Launch TradingView". Wrapped internally so a
            # TV failure NEVER blocks init — TV is optional.
            tv_offline = await _connect_tradingview(agent)
            # Stamp trade context BEFORE publishing the agent: an agent visible
            # to the input gate without _trade_context would silently answer
            # without trade-history grounding.
            agent._trade_context, ibkr_offline = await _send_opening_status(
                chat, toolkit, tv_offline
            )
            _session["agent"] = agent
            _send_action_buttons(chat, _session, session_id, ibkr_offline, tv_offline)
            await _maybe_background_flex_sync(chat, toolkit, ibkr_offline)
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


def _screenshot_file_input(chat: pn.chat.ChatInterface) -> pn.widgets.FileInput:
    """Typed accessor for the plain-attribute handoff from _build_chat_app.
    mypy can't see ad-hoc attributes on ChatInterface (panel ships py.typed)
    and ruff B009 bans the literal-getattr spelling, so the single ignore
    lives here instead of at every read site."""
    return chat._claudia_file_input  # type: ignore[attr-defined, no-any-return]


def _build_session_root() -> pn.Column:
    """pn.serve target: the KPI strip across the top, chat left, dashboard tabs right.

    Layout (the plan's chosen shape, taken over three columns, floating FloatPanels and a
    second pn.serve route — all three costed and still available):

        Column( KPI strip
                Row( Column( status dots + screenshot upload, chat ),
                     Tabs( Chart · Positions · Orders · P&L ) ) )

    The KPI strip is a Column, not a Row, at the top level so account state is glanceable
    from any tab without switching. The candlestick pane moved from a bare right-hand
    column into the Chart tab; it is still driven by its own Load button and still
    decoupled from the conversation.

    **The root type changed from pn.Row to pn.Column here.** Composition tests that index
    into the root by position were updated with it — a new pane at the top necessarily
    shifts every positional index below it.

    Periodic refresh is session-scoped with automatic cleanup
    (pn.state.add_periodic_callback registers against this session's Document —
    source-verified, see the Phase 6 design note in the migration plan). Both the status
    dots and the dashboard ride the same 5-second callback: one timer, two synchronous
    cache reads, no I/O on the session's event loop.
    """
    chat = _build_chat_app()
    indicators = _make_status_indicators()
    dashboard = build_dashboard(chart_pane=build_chart_pane())

    def _refresh() -> None:
        """Repaint the status dots and the dashboard from their cached snapshots.

        Both reads are synchronous and I/O-free by construction — the checker and the
        poller each maintain their own background task. `DashboardView.refresh` swallows
        its own exceptions (a repaint that raises inside a periodic callback would take
        the timer down and freeze the dots too), so this function cannot break the dots
        by failing on the dashboard.
        """
        if _connectivity_checker is not None:
            _apply_status(indicators, _connectivity_checker.get_status())
        if _dashboard_poller is not None:
            dashboard.refresh(_dashboard_poller.snapshot())

    # start=False + onload: starting at build time registers the callback
    # doc-side before the ServerSession exists, and the held SessionCallbackAdded
    # event is then replayed at unhold on top of the session-init sweep — a
    # double delivery that raises a per-session bokeh ValueError ("A callback of
    # the same type has already been added with this ID"); deferring the start
    # to onload (post-session) delivers it exactly once. The cb still registers
    # in pn.state._periodic[curdoc], so session auto-cleanup is preserved.
    cb = pn.state.add_periodic_callback(_refresh, period=5000, start=False)
    pn.state.onload(cb.start)
    return pn.Column(
        dashboard.kpi_strip,
        pn.Row(
            pn.Column(
                pn.Row(*indicators.values(), _screenshot_file_input(chat)),
                chat,
            ),
            dashboard.tabs,
            sizing_mode="stretch_both",
        ),
        sizing_mode="stretch_both",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Install a root log handler so `log.info` is actually emitted.

    Nothing else in the process does this. `pn.serve` does not configure logging —
    only bokeh's `bokeh serve` / `panel serve` CLI calls `bokeh.util.logconfig.basicConfig`,
    and ClaudIA serves natively (Panel-native serving rule, 2026-07-24). With no root
    handler, Python falls back to `logging.lastResort`, which emits WARNING and above
    only, so every `log.info` in this package was silently discarded — including
    `_log_thinking_usage`'s thinking-token line and `_log_cache_usage`'s prompt-cache
    numbers, i.e. exactly the telemetry those functions exist to produce.

    Third-party loggers stay at WARNING. That keeps httpx/anthropic per-request lines
    out of the log, which also means no request URL or header can carry a credential
    into it (Hard Rule 2 — never log ANTHROPIC_API_KEY).

    Called only from main(); importing this module configures nothing, so tests and
    embedders keep control of their own logging.
    """
    level = os.environ.get("CLAUDIA_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "googleapiclient",
                  "google_auth_httplib2", "bokeh", "tornado", "markdown_it"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _port_is_free(port: int, address: str = "127.0.0.1") -> bool:
    """Whether `port` can be bound on `address` right now.

    Checked against the same loopback address `pn.serve` binds — a port free on one
    interface and taken on another is a real state, and testing the wrong one would give
    a confident wrong answer.

    Inherently racy: something could take the port between this call and `pn.serve`. That
    is acceptable because the failure it prevents is the *common* one (a second ClaudIA
    started by hand), and losing the race merely restores today's behaviour — an
    EADDRINUSE traceback — rather than introducing a new failure.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((address, port))
        except OSError:
            return False
    return True


def main() -> None:
    """Serve ClaudIA on Panel's native Tornado server (design principle 2026-07-24:
    Panel-native serving, no workarounds — pn.serve(callable) invokes
    _build_session_root once per browser session while module singletons stay
    process-wide; verified by the pnserve probe, see the migration plan's
    re-verification note).

    Blocks until SIGINT (Ctrl-C): Panel installs its own SIGINT handler that stops
    the IO loop and returns from pn.serve. SIGTERM is translated to SIGINT so
    launchd/scripts reach the same clean-stop path; the final claudia.db upload
    runs after pn.serve returns — V5 contract.
    """
    _configure_logging()
    # Before the "server is up" line, so a stale install is the FIRST thing in the log
    # rather than something scrolled past. Every scraper import in claude_tools.py is
    # lazy, so a drifted editable install lets ClaudIA start perfectly and fail only when
    # a web tool is called — see claudia/install_check.py for the three times that cost
    # real debugging. Warns, never refuses: it breaks the web tools, not the trading ones.
    warn_if_stale()
    # Same shape and the same reason: a misconfiguration that lets ClaudIA start cleanly
    # and fail much later, where nobody would connect the symptom to the setting.
    warn_if_model_lacks_operator_channel(_MODEL)
    # Third of the same family: provable-only, and it names the app holding the
    # session so the fix is one action rather than a retry loop on the 2FA code.
    warn_if_session_borrowed()
    # The port has to be checked BEFORE the banner. `pn.serve` blocks until shutdown, so
    # there is no "after" in which to announce success — and until 2026-08-05 the banner
    # was simply logged first, which meant a start that then died on EADDRINUSE printed
    # "ClaudIA serving on …" and a stack trace. That is the precise inverse of the
    # guarantee the line exists for: it made a *failed* start look like a good one.
    if not _port_is_free(_PANEL_PORT):
        log.error(
            "PORT %d IS ALREADY IN USE — ClaudIA is very likely already running, and this "
            "process would have died with EADDRINUSE. Find the owner with: "
            "lsof -nP -iTCP:%d -sTCP:LISTEN   then stop it by PID (pkill -f "
            "'claudia.panel_app' does NOT match — the binary is named Python).",
            _PANEL_PORT, _PANEL_PORT,
        )
        return
    # The only "server is up" signal. pn.serve(show=False) opens no browser, and
    # _configure_logging pins bokeh to WARNING, which suppresses its own
    # "Bokeh app running at" banner — so without this line a correctly-started
    # ClaudIA looks identical to one that failed to start. Logged by us rather than
    # left to bokeh so it cannot be silenced by a third-party logger setting.
    log.info("ClaudIA serving on http://localhost:%d (Ctrl-C to stop)", _PANEL_PORT)
    # Panel installs its SIGINT handler inside pn.serve; translating SIGTERM to
    # SIGINT routes both through the same io_loop.stop() → serve-returns path.
    # Empirically verified in this task's smoke step (V5 proved only SIGINT).
    signal.signal(signal.SIGTERM, lambda *_: signal.raise_signal(signal.SIGINT))
    try:
        pn.serve(
            _build_session_root,
            port=_PANEL_PORT,
            show=False,
            title="ClaudIA",
            # Loopback ONLY. Without address=, Panel passes address=None to bokeh, which
            # calls tornado bind_sockets(port, None) — "None to listen on all available
            # interfaces" — so the server binds *:8001 on both IPv4 and IPv6 and is
            # reachable from the LAN. Confirmed live 2026-07-25 via lsof before the fix
            # (security-audit-2026-07-25.md, H-3).
            #
            # websocket_origin below is NOT a substitute and must never be treated as one:
            #   1. It gates the websocket upgrade only — GET / (bokeh DocHandler) is not
            #      origin-checked at all, and serving it creates a full ClaudIA session
            #      (Drive download, IBKR calls, DB row, possible Flex sync).
            #   2. Tornado skips check_origin entirely when the request carries no Origin
            #      header ("we assume it did not come from a browser" — tornado/websocket.py),
            #      so any non-browser client just omits it.
            # There is no auth layer, so the bind address is the actual boundary.
            # Source: https://www.tornadoweb.org/en/stable/netutil.html#tornado.netutil.bind_sockets
            address="127.0.0.1",
            # Default allowlist is localhost:<port> only; 127.0.0.1 access would get a
            # 403 websocket refusal without this (probe-verified). Defence-in-depth against
            # cross-origin browser attacks (incl. DNS rebinding), layered on the bind above.
            websocket_origin=[f"localhost:{_PANEL_PORT}", f"127.0.0.1:{_PANEL_PORT}"],
        )
    finally:
        # Loop is stopped here — synchronous blocking upload is fine (V5).
        if _gdrive_sync is not None:
            try:
                _gdrive_sync.upload_db(_DB_PATH)
                log.info("Final claudia.db upload complete")
            except Exception as exc:
                log.warning("Final Drive upload failed: %s — local DB preserved", exc)


if __name__ == "__main__":
    main()
