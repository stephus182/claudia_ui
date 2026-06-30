"""
TradingView integration for ClaudIA.

Phase 1 (this module):
  - Spawns the tradingview-mcp Node.js sidecar process on startup.
  - Connects to it via MCP stdio transport using the `mcp` Python client.
  - Merges a curated subset of tradingview-mcp tools into the Anthropic tools= list.
  - Renders PineScript output as a formatted Chainlit message with action buttons.
  - Falls back gracefully when TradingView Desktop is not running.

Phase 1 fallback (always available):
  - Screenshot analysis via Claude vision — user drags image into chat.
  - Handled in app.py / agent.py; no code in this module required.

Prerequisites (user must install once):
  git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
  cd ~/.tradingview-mcp && npm install   # pure JS — no build step needed

  ~/.tradingview-mcp/src/server.js is auto-discovered; TRADINGVIEW_MCP_PATH
  in .env is only needed to override the default path.

  TradingView Desktop launch (no manual command needed):
    - Start ClaudIA normally. If TV Desktop is not running, the welcome message
      shows a "Launch TradingView" button — click it.
    - ClaudIA calls launch_tradingview() which runs:
        open -a "TradingView" --args --remote-debugging-port=9222
      then polls for CDP port 9222 up to 30s and reconnects the sidecar.
    - If TV is already running WITHOUT the debug port, the button shows an error.
    - Manual fallback (if needed):
        /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222

tradingview-mcp repo: https://github.com/tradesdontlie/tradingview-mcp
  78 MCP tools + tv CLI, 4.1k stars, last updated April 2026.
  CDP injection sanitization added April 3, 2026 (safeString + requireFinite guards).
  Source: https://github.com/tradesdontlie/tradingview-mcp/blob/main/README.md
  Setup guide: https://github.com/tradesdontlie/tradingview-mcp/blob/main/SETUP_GUIDE.md

Python 3.14 / anyio compatibility (fixed 2026-06-30):
  The sidecar now starts successfully regardless of whether TradingView Desktop is
  running. Previously, sidecar startup failed when CDP port 9222 was unreachable:
  anyio._MemoryObjectItemReceiver instantiation calls AsyncIOTaskInfo(current_task())
  but Python 3.14 returns None from current_task() during async generator cleanup,
  causing AttributeError: 'NoneType' object has no attribute 'get_coro'.
  Fix: AsyncIOTaskInfo.__init__ is patched in claudia/app.py to stub a TaskInfo when
  task=None (task_info is only used in __repr__ — stub is safe). anyio 4.14.1 and
  MCP 1.28.1 do not fix this upstream as of 2026-06-30.
  Residual: when TV Desktop is not running, sidecar starts and lists its 78 tools but
  tool calls fail at the CDP layer — ClaudIA falls back to screenshot mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

import chainlit as cl
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

_TV_DEBUG_PORT = int(os.environ.get("TRADINGVIEW_DEBUG_PORT", "9222"))


def _find_tv_mcp_bin() -> str | None:
    """
    Find the tradingview-mcp entry point, in priority order:
      1. TRADINGVIEW_MCP_PATH env var
      2. tradingview-mcp on PATH
      3. ~/.tradingview-mcp/src/server.js   (JS version — no build step)
      4. ~/.tradingview-mcp/build/index.js  (TypeScript build output)
      5. vendor/tradingview-mcp/src/server.js  (archived fallback, needs node_modules/)
      6. vendor/tradingview-mcp/index.js    (legacy single-bundle archived fallback)
    """
    if path := os.environ.get("TRADINGVIEW_MCP_PATH"):
        p = Path(path)
        if not p.exists():
            log.warning("TRADINGVIEW_MCP_PATH=%r does not exist — ignoring", path)
        elif not path.endswith(".js"):
            log.warning("TRADINGVIEW_MCP_PATH=%r is not a .js file — ignoring", path)
        else:
            return path
    if which := shutil.which("tradingview-mcp"):
        return which
    js_src = Path.home() / ".tradingview-mcp" / "src" / "server.js"
    if js_src.exists():
        return str(js_src)
    ts_build = Path.home() / ".tradingview-mcp" / "build" / "index.js"
    if ts_build.exists():
        return str(ts_build)
    vendor_base = Path(__file__).parent.parent / "vendor" / "tradingview-mcp"
    vendor_js = vendor_base / "src" / "server.js"
    if vendor_js.exists() and (vendor_base / "node_modules").exists():
        log.warning(
            "Using archived vendor tradingview-mcp — "
            "run scripts/archive-tv-mcp.sh after upgrading."
        )
        return str(vendor_js)
    vendor_bundle = vendor_base / "index.js"
    if vendor_bundle.exists():
        log.warning(
            "Using archived vendor tradingview-mcp build — "
            "run scripts/archive-tv-mcp.sh after upgrading. "
            "See docs/tradingview-mcp-recovery.md"
        )
        return str(vendor_bundle)
    return None


_TV_MCP_BIN = _find_tv_mcp_bin()

# 16-tool curated subset exposed to Claude by default.
# Covers chart reading, control, Pine Script IDE, strategy results, and utility.
# Full 78-tool set is available but kept out of the Anthropic context window to
# reduce token cost and avoid tool-choice noise.
# Verified against live sidecar 2026-06-30 — data_get_equity_curve renamed to
# data_get_equity; data_get_trades added (Strategy Tester trade list).
_CURATED_TOOLS = {
    # Chart reading
    "chart_get_state",
    "quote_get",
    "data_get_ohlcv",
    "data_get_study_values",
    # Chart control
    "chart_set_symbol",
    "chart_set_timeframe",
    "indicator_set_inputs",
    # Pine Script IDE
    "pine_set_source",
    "pine_smart_compile",
    "pine_get_errors",
    "pine_get_source",
    # Strategy results
    "data_get_strategy_results",
    "data_get_equity",       # renamed from data_get_equity_curve in current sidecar
    "data_get_trades",       # trade list from Strategy Tester
    # Utility
    "tv_health_check",
    "capture_screenshot",
}


# ── CDP health check + launch helpers ────────────────────────────────────────

def check_cdp_running() -> bool:
    """TCP check if TradingView Desktop's CDP debug port is accepting connections."""
    try:
        with socket.create_connection(("localhost", _TV_DEBUG_PORT), timeout=1.0):
            return True
    except OSError:
        return False


_TV_APP_NAME = "TradingView"


def _tv_already_running_without_debug() -> bool:
    """True if TradingView process is running but CDP port is not open."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", _TV_APP_NAME],
            capture_output=True, text=True
        )
        return result.returncode == 0 and not check_cdp_running()
    except OSError:
        return False


async def launch_tradingview() -> bool:
    """
    Launch TradingView Desktop with --remote-debugging-port on macOS.

    If TradingView is already running without the debug port, raises RuntimeError
    with instructions to quit and relaunch — the process cannot be relaunched while
    running without restarting it from scratch.

    Returns True if the CDP port becomes available within 30s.

    The official SETUP_GUIDE.md recommends the `tv_launch` MCP tool or the direct
    binary path. ClaudIA uses `open -a "TradingView"` which is equivalent on macOS
    and handles app relocation automatically (no hardcoded binary path).

    Source: https://github.com/tradesdontlie/tradingview-mcp/blob/main/SETUP_GUIDE.md
    """
    if check_cdp_running():
        return True
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Automatic TradingView launch is only supported on macOS. "
            f"Start it manually: open -a '{_TV_APP_NAME}' --args --remote-debugging-port={_TV_DEBUG_PORT}"
        )
    if _tv_already_running_without_debug():
        raise RuntimeError(
            f"TradingView is already running without the remote debug port. "
            f"Quit TradingView, then relaunch it:\n"
            f"  open -a '{_TV_APP_NAME}' --args --remote-debugging-port={_TV_DEBUG_PORT}"
        )
    log.info("Launching TradingView Desktop with --remote-debugging-port=%d", _TV_DEBUG_PORT)
    subprocess.Popen(
        ["open", "-a", _TV_APP_NAME, "--args", f"--remote-debugging-port={_TV_DEBUG_PORT}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if check_cdp_running():
            log.info("TradingView CDP port %d is ready", _TV_DEBUG_PORT)
            return True
    log.warning("TradingView Desktop did not open CDP port %d within 30s", _TV_DEBUG_PORT)
    return False


# ── TradingViewBridge ─────────────────────────────────────────────────────────

class TradingViewBridge:
    """
    Manages the tradingview-mcp sidecar and exposes its tools to ClaudIA.

    Lifecycle:
      await bridge.start()        — spawn sidecar, list available tools
      bridge.get_tools()          — returns curated tool definitions for Anthropic SDK
      await bridge.execute(name, inputs)  — call a tradingview-mcp tool
      await bridge.stop()         — shut down sidecar gracefully
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._curated_tools: list[dict] = []
        self._cm = None  # async context manager for stdio_client

    async def start(self) -> None:
        """Spawn the tradingview-mcp sidecar and connect via MCP stdio.

        Only selected env vars are forwarded to the Node subprocess — never the
        full process env — to prevent ANTHROPIC_API_KEY and other secrets from
        leaking to an external process.

        Raises RuntimeError if the binary cannot be found.
        """
        bin_path = _TV_MCP_BIN or _find_tv_mcp_bin()
        if not bin_path:
            raise RuntimeError(
                "tradingview-mcp binary not found. "
                "Clone with: git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp "
                "&& cd ~/.tradingview-mcp && npm install  (no build step needed — pure JS). "
                "Or set TRADINGVIEW_MCP_PATH in .env to override the discovery path."
            )
        log.info("tradingview-mcp binary: %s", bin_path)

        # Pass only the vars the sidecar actually needs — never the full process env,
        # which would leak ANTHROPIC_API_KEY and all other secrets to the Node subprocess.
        # CHROME_REMOTE_DEBUG_PORT tells the sidecar which CDP port to connect to
        # (default 9222, overridable via TRADINGVIEW_DEBUG_PORT env var).
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP",
                      "NODE_PATH", "NODE_ENV", "XDG_RUNTIME_DIR")
            if k in os.environ
        }
        env["CHROME_REMOTE_DEBUG_PORT"] = str(_TV_DEBUG_PORT)

        # node path/to/index.js for a built .js file; direct binary otherwise
        if bin_path.endswith(".js"):
            cmd = "node"
            args = [bin_path]
        else:
            cmd = bin_path
            args = []

        server_params = StdioServerParameters(
            command=cmd,
            args=args,
            env=env,
        )

        # Log sidecar git commit for version diagnostics (best-effort — vendor/ has no .git)
        sidecar_dir = str(Path(bin_path).parent.parent)
        try:
            result = subprocess.run(
                ["git", "-C", sidecar_dir, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            sidecar_commit = result.stdout.strip() if result.returncode == 0 else "unknown"
        except Exception:
            sidecar_commit = "unknown"
        log.info("tradingview-mcp sidecar: %s (commit %s)", bin_path, sidecar_commit)

        try:
            self._cm = stdio_client(server_params)
            read, write = await self._cm.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()

            # Discover available tools from sidecar — descriptions and schemas come from here,
            # not from the ClaudIA codebase. This is the only documentation ClaudIA receives
            # about what each tool does.
            response = await self._session.list_tools()
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}, "required": []},
                }
                for t in response.tools
            ]
            self._curated_tools = [t for t in self._tools if t["name"] in _CURATED_TOOLS]
            sidecar_names = {t["name"] for t in self._tools}
            if missing_curated := _CURATED_TOOLS - sidecar_names:
                log.warning(
                    "tradingview-mcp: curated tools not found in sidecar (sidecar may have renamed them) — %s",
                    ", ".join(sorted(missing_curated)),
                )
            log.info(
                "tradingview-mcp connected: %d total tools, %d curated",
                len(self._tools),
                len(self._curated_tools),
            )

        except Exception as exc:
            log.warning("tradingview-mcp sidecar failed to start: %s", exc)
            self._tools = []
            raise

    def get_tools(self) -> list[dict]:
        """Return the curated subset of tools for the Anthropic tools= list."""
        return list(self._curated_tools)

    def get_all_tools(self) -> list[dict]:
        """Return all available tools (bypasses the curated filter)."""
        return list(self._tools)

    async def execute(self, name: str, inputs: dict) -> str:
        """Call a tradingview-mcp tool via the MCP stdio session. Returns a string result.

        Never raises — on any error returns a user-facing error string so the agent
        loop can include it in the next assistant message without crashing.

        Source: https://github.com/tradesdontlie/tradingview-mcp
        """
        if not self._session:
            return "TradingView is not connected."
        try:
            result = await self._session.call_tool(name, inputs)
            # Extract text from result content
            parts = []
            for item in (result.content or []):
                if hasattr(item, "text"):
                    parts.append(item.text)
                elif isinstance(item, dict) and "text" in item:
                    parts.append(item["text"])
            return "\n".join(parts) if parts else json.dumps(result.content)
        except Exception as exc:
            log.error("tradingview-mcp tool '%s' failed: %s", name, exc)
            return f"TradingView tool '{name}' failed."

    async def stop(self) -> None:
        """Tear down the MCP stdio session. Errors are silently discarded — stop must not raise."""
        try:
            if self._session:
                await self._session.__aexit__(None, None, None)
            if self._cm:
                await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None
        self._tools = []
        self._curated_tools = []


# ── PineScript display helpers ────────────────────────────────────────────────

async def render_pinescript(code: str, title: str = "PineScript Strategy") -> None:
    """Render a PineScript code block with copy and inject action buttons."""
    actions = [
        cl.Action(
            name="copy_pinescript",
            payload={"code": code},
            label="Copy to clipboard",
            tooltip="Copy PineScript code",
        ),
        cl.Action(
            name="inject_pinescript",
            payload={"code": code},
            label="Inject into TradingView",
            tooltip="Paste directly into TradingView Pine Editor",
        ),
    ]

    await cl.Message(
        content=f"**{title}**\n\n```pine\n{code}\n```",
        actions=actions,
        author="ClaudIA — PineScript",
    ).send()


@cl.action_callback("copy_pinescript")
async def on_copy_pinescript(action: cl.Action):
    """Display PineScript code for manual copy.

    Chainlit runs server-side — there is no clipboard API available. The code block
    is re-sent as a message so the user can select and copy it in the browser.

    Source: https://docs.chainlit.io/api-reference/action
    """
    # Chainlit doesn't have clipboard access server-side; display for manual copy.
    code = action.payload["code"]
    await cl.Message(
        content=f"Copy this PineScript:\n\n```pine\n{code}\n```",
        author="ClaudIA",
    ).send()
    await action.remove()


@cl.action_callback("inject_pinescript")
async def on_inject_pinescript(action: cl.Action):
    """Inject PineScript into TradingView Pine Editor via pine_set_source."""
    code = action.payload["code"]
    await cl.Message(
        content="Injecting PineScript into TradingView Pine Editor…",
        author="System",
    ).send()
    try:
        from claudia.app import _tv_bridge
        if _tv_bridge and _tv_bridge._session:
            result = await _tv_bridge.execute("pine_set_source", {"source": code})
            await cl.Message(content=f"Injected. Response: {result}", author="ClaudIA").send()
        else:
            await cl.Message(
                content="TradingView Desktop is not connected. Copy the script manually.",
                author="System",
            ).send()
    except Exception as exc:
        log.error("Pine injection failed: %s", exc)
        await cl.Message(content="Injection failed. Please copy the script manually.", author="System").send()
    await action.remove()
