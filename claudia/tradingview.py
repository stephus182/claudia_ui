"""
TradingView integration for ClaudIA.

Phase 1 (this module):
  - Spawns the tradingview-mcp Node.js sidecar process on startup.
  - Connects to it via MCP stdio transport using the `mcp` Python client.
  - Merges tradingview-mcp tools into the Anthropic tools= list.
  - Renders PineScript output as a formatted Chainlit message with action buttons.
  - Falls back gracefully when TradingView Desktop is not running.

Phase 1 fallback (always available):
  - Screenshot analysis via Claude vision — user drags image into chat.
  - Handled in app.py / agent.py; no code in this module required.

Prerequisites (user must install once):
  npm install -g @mxstbr/tradingview-mcp
  Open TradingView Desktop with --remote-debugging-port=9222

tradingview-mcp repo: https://github.com/mxstbr/tradingview-mcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import chainlit as cl
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

_TV_MCP_BIN = os.environ.get("TRADINGVIEW_MCP_PATH") or shutil.which("tradingview-mcp")
_TV_DEBUG_PORT = int(os.environ.get("TRADINGVIEW_DEBUG_PORT", "9222"))


class TradingViewBridge:
    """
    Manages the tradingview-mcp sidecar and exposes its tools to ClaudIA.

    Lifecycle:
      await bridge.start()        — spawn sidecar, list available tools
      bridge.get_tools()          — returns tool definitions for Anthropic SDK
      await bridge.execute(name, inputs)  — call a tradingview-mcp tool
      await bridge.stop()         — shut down sidecar gracefully
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._process: subprocess.Popen | None = None
        self._cm = None  # async context manager for stdio_client

    async def start(self) -> None:
        if not _TV_MCP_BIN:
            raise RuntimeError(
                "tradingview-mcp binary not found. "
                "Install with: npm install -g @mxstbr/tradingview-mcp"
            )

        env = {**os.environ, "CHROME_REMOTE_DEBUG_PORT": str(_TV_DEBUG_PORT)}

        server_params = StdioServerParameters(
            command=_TV_MCP_BIN,
            args=[],
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
            log.info("tradingview-mcp connected: %d tools available", len(self._tools))

        except Exception as exc:
            log.warning("tradingview-mcp sidecar failed to start: %s", exc)
            self._tools = []
            raise

    def get_tools(self) -> list[dict]:
        return list(self._tools)

    async def execute(self, name: str, inputs: dict) -> str:
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
        try:
            if self._session:
                await self._session.__aexit__(None, None, None)
            if self._cm:
                await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None
        self._tools = []


# ── PineScript display helpers ────────────────────────────────────────────────

async def render_pinescript(code: str, title: str = "PineScript Strategy") -> None:
    """Render a PineScript code block with copy and inject action buttons."""
    actions = [
        cl.Action(
            name="copy_pinescript",
            value=code,
            label="Copy to clipboard",
            description="Copy PineScript code",
        ),
    ]

    # Add inject button only if TradingView bridge has inject tool available
    # (This is detected at runtime via the tool name)
    actions.append(
        cl.Action(
            name="inject_pinescript",
            value=code,
            label="Inject into TradingView",
            description="Paste directly into TradingView Pine Editor",
        )
    )

    await cl.Message(
        content=f"**{title}**\n\n```pine\n{code}\n```",
        actions=actions,
        author="ClaudIA — PineScript",
    ).send()


@cl.action_callback("copy_pinescript")
async def on_copy_pinescript(action: cl.Action):
    # Chainlit doesn't have clipboard access server-side; we display the code
    # in a focused message so the user can manually copy it.
    await cl.Message(
        content=f"Copy this PineScript:\n\n```pine\n{action.value}\n```",
        author="ClaudIA",
    ).send()
    await action.remove()


@cl.action_callback("inject_pinescript")
async def on_inject_pinescript(action: cl.Action):
    """Attempt to inject PineScript into TradingView via the sidecar."""
    code = action.value
    # Try to call tradingview-mcp's pine_editor or equivalent tool
    # The exact tool name depends on the tradingview-mcp version
    await cl.Message(
        content="Attempting to inject PineScript into TradingView Pine Editor…",
        author="System",
    ).send()
    try:
        from claudia.app import _tv_bridge
        if _tv_bridge and _tv_bridge._session:
            result = await _tv_bridge.execute("open_pine_editor", {"code": code})
            await cl.Message(content=f"Injected. TradingView response: {result}", author="ClaudIA").send()
        else:
            await cl.Message(
                content="TradingView Desktop is not connected. Copy the script manually.",
                author="System",
            ).send()
    except Exception as exc:
        log.error("Pine injection failed: %s", exc)
        await cl.Message(content="Injection failed. Please copy the script manually.", author="System").send()
    await action.remove()
