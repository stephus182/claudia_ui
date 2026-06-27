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

  Open TradingView Desktop with remote debugging enabled:
  open -a "Trading View" --args --remote-debugging-port=9222

tradingview-mcp repo: https://github.com/tradesdontlie/tradingview-mcp
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

# 15-tool curated subset exposed to Claude by default.
# Covers chart reading, control, Pine Script IDE, strategy results, and utility.
# Full 78-tool set is available but kept out of the Anthropic context window to
# reduce token cost and avoid tool-choice noise.
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
    "data_get_equity_curve",
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


async def launch_tradingview() -> bool:
    """
    Launch TradingView Desktop with --remote-debugging-port on macOS.
    Returns True if the CDP port becomes available within 30s.
    """
    if check_cdp_running():
        return True
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Automatic TradingView launch is only supported on macOS. "
            f"Start it manually: open -a 'Trading View' --args --remote-debugging-port={_TV_DEBUG_PORT}"
        )
    log.info("Launching TradingView Desktop with --remote-debugging-port=%d", _TV_DEBUG_PORT)
    subprocess.Popen(
        ["open", "-a", "Trading View", "--args", f"--remote-debugging-port={_TV_DEBUG_PORT}"],
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

        try:
            self._cm = stdio_client(server_params)
            read, write = await self._cm.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()

            # Discover available tools
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
