"""Panel-native PineScript copy/inject buttons.

Mirrors panel_order_flow.py's separation: a pure detector (extract_pine_blocks)
plus a Panel-specific renderer (render_pinescript_blocks) that drops a Copy/Inject
button row beneath each ```pine block ClaudIA emits. The whole feature lives in the
Panel sink path — safety-critical agent.py is UNTOUCHED. The agent emits PineScript
as plain ```pine markdown (proposal blocks are stripped, ```pine is not); detection
happens after, on the already-rendered text.

Copy is a REAL client-side clipboard write (js_on_click +
navigator.clipboard.writeText) — the concrete Panel-over-Chainlit win; localhost is
a secure context so writeText is permitted, and a click supplies the required
transient activation. The pine code is passed as a Bokeh-serialized js arg
(args={"code": code}) referenced by name — NOT string-interpolated into the JS — so
quotes/backticks/newlines in the source cannot break out (injection-safe).

Inject calls the tradingview-mcp `pine_set_source` tool through the live
TradingViewBridge. Contract verified against the local sidecar source
(~/.tradingview-mcp/src/tools/pine.js:11): input {source: string}. The bridge is
resolved lazily via an injected getter so a TradingView launch that happens AFTER
the message rendered is still picked up at click time.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import panel as pn

if TYPE_CHECKING:
    from collections.abc import Callable

    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)


def _pine_inject_succeeded(result: str) -> bool:
    """True only on an explicit success signal from pine_set_source. Fail-safe:
    a non-JSON result (e.g. "TradingView is not connected.", a "…failed." string)
    or success != True reads as FAILURE — never claim an inject that didn't happen
    (same polarity as order_flow's _is_ibkr_rejection).

    TradingViewBridge.execute (tradingview.py:336-362) returns a STRING that is
    either the sidecar's jsonResult JSON ({"success": true, ...} /
    {"success": false, "error": ...}) or a literal status string on the
    no-session / exception paths — so the result must be classified, not trusted.
    """
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("success") is True


def _pine_error_detail(result: str) -> str:
    """Human-facing error text from a failed pine_set_source result: the sidecar's
    `error` field when the result is JSON with one, else the raw result string
    (covers the literal status-string paths). Keeps the failure message honest
    without dumping raw JSON braces at the user."""
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return result


def extract_pine_blocks(text: str) -> list[str]:
    """Return the code of every ```pine fenced block in text, each stripped.

    Regex verified in the Task 9.2 research doc: `\\b` avoids matching a longer
    fence tag (e.g. ```pinescript), `[^\\n]*` tolerates an info string after
    `pine`, and multiple blocks per message are all captured.
    """
    return [block.strip() for block in re.findall(r"```pine\b[^\n]*\n(.*?)```", text, re.DOTALL)]


async def render_pinescript_blocks(
    chat: pn.chat.ChatInterface,
    text: str,
    tv_bridge_getter: Callable[[], TradingViewBridge | None] | None,
) -> None:
    """Send a Copy/Inject button row beneath each ```pine block in text."""
    for code in extract_pine_blocks(text):
        _render_pine_block(chat, code, tv_bridge_getter)


def _render_pine_block(
    chat: pn.chat.ChatInterface,
    code: str,
    tv_bridge_getter: Callable[[], TradingViewBridge | None] | None,
) -> None:
    """Send one Copy/Inject row for a single pine code block.

    Factored out of the loop so each block's async inject handler closes over its
    OWN `code` — a shared loop variable would give every handler the last block's
    source.
    """
    copy_btn = pn.widgets.Button(label="Copy PineScript", color="light")
    # Bokeh serializes `code` into a JS variable of the same name — it is NOT
    # concatenated into the `code=` JS string, so the pine source is injection-safe.
    copy_btn.js_on_click(args={"code": code}, code="navigator.clipboard.writeText(code)")

    inject_btn = pn.widgets.Button(label="Inject into TradingView", color="primary")

    async def _on_inject(event) -> None:
        # disable-first closes the double-click window during the await, but — unlike
        # panel_order_flow (safety-critical, one live order) — PineScript inject is
        # idempotent (it just sets the editor source), so every FAILURE path re-enables
        # the button: the commonest failure is "TV not launched yet", which is
        # recoverable on the same button once the user runs the debug-launch helper.
        inject_btn.disabled = True
        try:
            bridge = tv_bridge_getter() if tv_bridge_getter is not None else None
            if bridge is None:
                chat.send(
                    "TradingView is not connected — launch it first, or copy the script manually.",
                    user="System",
                    respond=False,
                )
                inject_btn.disabled = False  # recoverable: launch TV, then retry this button
                return
            result = await bridge.execute("pine_set_source", {"source": code})
            # Classify — execute() returns the sidecar's own {"success": ...} JSON (or a
            # literal status string). Reporting "Injected" on a {"success": false} result
            # would be a false-success (same class as the field-8089 order bug); the
            # project's data-integrity standard forbids claiming success on failure.
            if _pine_inject_succeeded(result):
                chat.send("✅ Injected into the TradingView Pine Editor.", user="ClaudIA", respond=False)
            else:
                chat.send(
                    f"✕ Injection did not complete — copy the script manually.\n\n"
                    f"{_pine_error_detail(result)}",
                    user="System",
                    respond=False,
                )
                inject_btn.disabled = False  # recoverable (e.g. CDP dropped) — allow retry
        except Exception:
            # No raise — mirror panel_order_flow's honest-degradation handlers. execute()
            # itself never raises (tradingview.py:339), but the getter / chat.send could.
            log.exception("PineScript injection failed")
            chat.send("Injection failed — copy the script manually.", user="System", respond=False)
            inject_btn.disabled = False

    inject_btn.on_click(_on_inject)

    chat.send(pn.Row(copy_btn, inject_btn), user="ClaudIA — PineScript", respond=False)
