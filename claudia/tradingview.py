"""TradingView integration for ClaudIA.

Phase 1 (this module):
  - Spawns the tradingview-mcp Node.js sidecar process on startup.
  - Connects to it via MCP stdio transport using the `mcp` Python client.
  - Merges a curated subset of tradingview-mcp tools into the Anthropic tools= list.
  - PineScript rendering (copy / inject buttons) lives in claudia/panel_pinescript.py, not here.
  - Falls back gracefully when TradingView Desktop is not running.

Phase 1 fallback (always available):
  - Screenshot analysis via Claude vision — user drags image into chat.
  - Handled in panel_app.py / agent.py; no code in this module required.

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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

_TV_DEBUG_PORT = int(os.environ.get("TRADINGVIEW_DEBUG_PORT", "9222"))


def _find_tv_mcp_bin() -> str | None:
    """Find the tradingview-mcp entry point, in priority order:
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

# 17-tool curated subset exposed to Claude by default.
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
    # …and the getter it depends on: input ids are not guessable (a built-in RSI uses
    # in_0..in_7, an EMA on the same chart uses "length"), and an unmatched key is a
    # silent no-op reported as success. Curating the setter alone made that a false
    # success on 2026-08-11. Added with the guard in _EMPTY_RESULT_GUARDS.
    "data_get_indicator",
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


# ── result post-processing ────────────────────────────────────────────────────
#
# Every tradingview-mcp result crosses TradingViewBridge.execute(). These transforms
# run there, on the parsed payload, before the model sees it. Added 2026-08-11 after
# the live batch found the model inventing dates from bare epoch fields (it printed
# 2026-08-11 as "May 12", every row wrong, while every price round-tripped exactly).

# A parsed sidecar payload. Every transform takes the tool name and one of these, and
# returns one, so they compose in _TRANSFORMS in a single line without any of them having
# to re-narrow the top-level type. Transforms that ignore the name still declare it: one
# shape for all of them is what keeps the loop a loop instead of a set of special cases.
Payload = dict[str, object]

# Epoch seconds only, and only in fields that are actually timestamps. `time_index` in
# data_get_trades is a BAR NUMBER (782, not a date) — annotating it would manufacture the
# very defect this exists to remove, so the match is on exact key names, backed by a range
# check. `from`/`to` are two generic words that earn their place: data_get_ohlcv's
# summary=true mode returns `period: {from: first.time, to: last.time}` — real epoch seconds
# in the same payload as an annotated `last_5_bars[].time`, so leaving them bare would
# actively imply they are not dates. Source: ~/.tradingview-mcp/src/core/data.js:170.
_EPOCH_KEYS = frozenset({"time", "timestamp", "from", "to"})

# The floor separates an epoch from a small integer that happens to share one of those key
# names. It is NOT what rejects a bar index — exact-key matching does that job — so it does
# not need to sit above any bar count. The honest cost of any floor is a silent gap: below
# it, a real timestamp gets no `time_utc`, and a missing sibling is indistinguishable from
# "not a timestamp". This floor puts that gap before 1973, and data_get_ohlcv serves at most
# 500 bars (MAX_OHLCV_BARS, data.js:7), which even monthly only reaches the mid-1980s.
_EPOCH_MIN = 100_000_000  # 1973-03-03
_EPOCH_MAX = 4_000_000_000  # 2096-10-02


def _annotate_epochs(_name: str, payload: Payload) -> Payload:
    """Add a `<key>_utc` ISO-8601 sibling beside every epoch-seconds field, at any depth.

    Applies to every tool, so the tool name is unused — it is in the signature because
    every transform shares one shape, the same reason `_post_process` carries a `name`.
    """
    return _annotate_dict(payload)


def _annotate_dict(payload: Payload) -> Payload:
    """One dict's worth of the annotation above, recursing through its values.

    UTC rather than exchange-local: the exchange is not knowable from the payload and
    guessing it would be an instrument-specific rule. The original value is kept.
    """
    out: Payload = {}
    for key, value in payload.items():
        out[key] = _walk_epochs(value)
        if (
            key in _EPOCH_KEYS
            and isinstance(value, (int, float))
            # bool is a subclass of int, so True reaches this branch. The range check
            # happens to reject it too while _EPOCH_MIN sits above 1, but that is where
            # the floor is today, not a guarantee — this guard is what stops a lowered
            # floor from stamping True as 1970-01-01T00:00:01Z.
            and not isinstance(value, bool)
            and _EPOCH_MIN <= value <= _EPOCH_MAX
            # Never clobber a sibling the sidecar already sent, and never let key order
            # decide whose value survives.
            and f"{key}_utc" not in payload
        ):
            stamped = datetime.fromtimestamp(value, tz=UTC)
            out[f"{key}_utc"] = stamped.isoformat().replace("+00:00", "Z")
    return out


def _walk_epochs(node: object) -> object:
    """Recurse into containers; every dict reached is handed to _annotate_dict."""
    if isinstance(node, dict):
        return _annotate_dict(node)
    if isinstance(node, list):
        return [_walk_epochs(item) for item in node]
    return node


# Our channel for telling the model to distrust the payload the key sits in — so it is ours
# alone. A sidecar-supplied one is dropped in _post_process before any transform runs: never
# relayed, never deferred to. That is the OPPOSITE resolution from the `_utc` siblings above,
# and deliberately so — a sidecar `time_utc` is data ABOUT the payload and legitimately wins,
# while this is a statement about the payload's TRUSTWORTHINESS and cannot be sourced from
# the thing being judged. Reserved once, for every present and future transform, rather than
# defended inside any one of them.
#
# Latent rather than live: the sidecar builds its responses from fixed keys and none of them
# is this one. It is reserved because it is an injection surface into a trusted channel.
_RESERVED_KEY = "claudia_warning"


# A tool whose success flag is meaningless unless a named field came back non-empty.
# Keyed by tool name -> (field that must be non-empty, what to tell the model).
#
# indicator_set_inputs is the measured case (2026-08-11): asked to set the RSI length to 21
# it returned success:true, updated_inputs:{} and changed nothing. The sidecar matches
# override keys against the study's real input ids, drops the ones that miss, and reports
# success either way (~/.tradingview-mcp/src/core/indicators.js:171-192). The tool is
# unvalidated, not broken — retried with in_0 it worked.
#
# CHOOSING A FIELD: emptiness here is plain falsiness, so `0`, `False`, `""` and `[]` all
# read as a no-op. Only list a field whose empty value is unambiguously "nothing happened".
# A field where `0` or `""` is a legitimate result needs a predicate, not this table.
# (updated_inputs is safe: indicators.js:178 initialises `var updatedKeys = {}` and returns
# it unconditionally, so it is always an object.)
#
# Every message here is OUR constant, and on the transform path it is the only claudia_warning
# that can reach the model: _post_process drops any the sidecar sent before the transforms run.
# The reservation stops there, and the gap is worth naming rather than rounding off. On the three
# fail-open paths — non-JSON, a non-object payload, or a transform raising — _post_process returns
# the sidecar's bytes untouched, so a claudia_warning inside them survives. Measured, not assumed:
# a top-level array carrying one comes through. That is what fail-open means and the trade is
# deliberate; passing a tool result through unread beats swallowing it. Do not "fix" it by
# re-serialising those paths.
_EMPTY_RESULT_GUARDS: dict[str, tuple[str, str]] = {
    "indicator_set_inputs": (
        "updated_inputs",
        "NOTHING WAS CHANGED. None of the requested keys matched this study's input "
        "ids, and the sidecar reports success either way. Input ids are not guessable "
        "-- the built-in RSI uses in_0..in_7 while an EMA on the same chart uses "
        "'length'. Call data_get_indicator on this entity_id to read the real ids, "
        "then retry. Do not report this change as done.",
    ),
}


def _flag_empty_result(name: str, payload: Payload) -> Payload:
    """Attach a warning when a tool claims success but its own payload shows a no-op.

    The sidecar's own `success` field is left exactly as it sent it: this reports what the
    tool said alongside what its payload shows, it does not rewrite the tool's answer.
    """
    guard = _EMPTY_RESULT_GUARDS.get(name)
    if guard is None:
        return payload
    field, message = guard
    if field in payload and not payload[field]:
        payload = dict(payload)
        payload[_RESERVED_KEY] = message
    return payload


# Pine studies carry their obfuscated source in inputs.text. Measured 2026-08-11 on a live
# data_get_study_values result: two base64 blobs under that key were 5,280 of the payload's
# 8,641 chars (61%), roughly 1,320 tokens on a tool that fires on any "what are my
# indicators" question, and they push the real values further from the model's attention.
# The sidecar already applies this rule in getIndicator (~/.tradingview-mcp/src/core/data.js:202)
# and omits it in getStudyValues. Applying it to both closes an upstream inconsistency; the rule
# itself is the sidecar's, not one invented here.
_BLOB_KEY = "text"

# The threshold is the sidecar's own (data.js:202, read 2026-08-11). It is not a length
# heuristic standing in for "looks encoded": the KEY selects the field, and this only separates
# a blob from a genuine short value sent under that same key.
#
# Why the key and nothing else. The live payload carries RSI's in_3, whose value object
# includes {"v": "SMA", "t": "text"} — an input whose TYPE is text and whose value is a real
# one, so matching on the type would mangle it. Matching on length alone would reach any long
# field whatever it holds, which is why the sidecar's SECOND getIndicator rule (drop any
# string input over 500 chars, data.js:203) is deliberately NOT mirrored here.
#
# CHARACTERS, not bytes, while the cost being reduced is wire bytes. 150 astral-plane
# characters are 600 UTF-8 bytes and ~1,800 chars once ensure_ascii escapes them into the
# emitted JSON, yet they pass; 201 ASCII chars are trimmed. About a 12x spread in the very
# quantity this exists to reduce. Left as is on purpose: base64 Pine source is ASCII by
# construction so the spread is unreachable for the measured case, and String.length is what
# the sidecar's own rule counts — matching it keeps one definition of "oversized", not two.
_BLOB_MAX_CHARS = 200


def _trim_blobs(_name: str, payload: Payload) -> Payload:
    """Replace oversized `text` values with a marker naming how much was withheld.

    The KEY selects the field, at any dict depth: a `text` key whose value is an oversized
    string is replaced. An oversized string that is merely reachable from one — an element of
    `{"text": [...]}` — is not, because nothing marks it as the same kind of content.

    Applies to every tool, so the tool name is unused — it is in the signature because
    every transform shares one shape, the same reason `_post_process` carries a `name`.
    """
    return _trim_dict(payload)


def _trim_dict(payload: Payload) -> Payload:
    """One dict's worth of the trim above, recursing through its values.

    Replaced, never deleted: a study with no `text` field at all tells the model nothing,
    while a marker tells it something was withheld and how big it was.

    The marker states the size and NOTHING about what was withheld. The transform is
    unscoped to any tool list and the key alone is no evidence of encoding, so a
    plain-English `text` value over the threshold would be trimmed too — calling it
    "encoded source" would be asserting a provenance nothing here checked.

    Unlike _RESERVED_KEY, the marker is NOT defended against a sidecar payload containing
    an identical string: within a <=200-char `text` value it passes through verbatim and is
    indistinguishable from ours. Accepted, not overlooked. There is no honest way to
    separate them — Pine sources are third-party content that may contain any text — and
    the impact is bounded: the model is told something was withheld when nothing was,
    which costs it a value, not a wrong action. A key can be reserved; a string cannot.
    """
    out: Payload = {}
    for key, value in payload.items():
        if key == _BLOB_KEY and isinstance(value, str) and len(value) > _BLOB_MAX_CHARS:
            out[key] = f"<omitted: {len(value)} chars>"
        else:
            out[key] = _walk_blobs(value)
    return out


def _walk_blobs(node: object) -> object:
    """Recurse into containers; every dict reached is handed to _trim_dict.

    Preserves falsiness everywhere, which is what makes this transform order-independent
    against _flag_empty_result — see the note on _TRANSFORMS. The marker branch fires only
    on a string already over the threshold (necessarily truthy) and writes a non-empty
    string; every other value is passed through, no key is removed, and no container's
    length changes. So no value can cross between empty and non-empty in either direction.
    """
    if isinstance(node, dict):
        return _trim_dict(node)
    if isinstance(node, list):
        return [_walk_blobs(item) for item in node]
    return node


# Order is not arbitrary, but nothing today depends on it: these three touch disjoint keys,
# and all six orderings were run over the live 2026-08-11 tool-result batch and produced
# byte-identical output. The invariant that WOULD break: a transform that can empty or fill a
# field named in _EMPTY_RESULT_GUARDS is order-coupled to _flag_empty_result, since it decides
# what that guard sees. _trim_blobs cannot, because it PRESERVES FALSINESS everywhere: it
# writes a non-empty marker only over an already-truthy string, passes every other value
# through, removes no key and changes no container's length. `{"payload": {}}` comes out
# `{"payload": {}}`. The guard reads plain falsiness, so a transform that cannot move a value
# across that line cannot change its verdict — whatever the guard table lists. That property,
# not the marker, is what a future transform here would have to preserve.
# So this stays a note, not a test — write the test when one violates it.
_TRANSFORMS: tuple[Callable[[str, Payload], Payload], ...] = (
    _annotate_epochs,
    _flag_empty_result,
    _trim_blobs,
)


def _post_process(name: str, raw: str) -> str:
    """Run every registered transform over a parsed sidecar payload before the model sees it.

    Fails open by design: anything that is not a JSON object is returned untouched, and so
    is anything a transform raises on. A transform that could swallow a tool result — or
    turn a succeeded call into a reported failure — would be worse than the defects it fixes.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    payload.pop(_RESERVED_KEY, None)
    try:
        for transform in _TRANSFORMS:
            payload = transform(name, payload)
    except Exception as exc:
        log.warning("post-processing of tradingview-mcp '%s' failed, passing raw: %s", name, exc)
        return raw
    # ensure_ascii stays at its default True: JS JSON.stringify escapes a lone surrogate,
    # so it arrives as ASCII `\ud800`, and re-emitting it raw produces a str that cannot be
    # UTF-8 encoded — which crashes the conversation_store insert and the tool_result body.
    # pine_get_source/pine_get_errors carry arbitrary user-authored text. Measured 2026-08-11.
    return json.dumps(payload, indent=2)


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
    """Launch TradingView Desktop with --remote-debugging-port on macOS.

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
            "TradingView is already running without the remote debug port "
            "(it can only be set at launch). Run the one-command quit+relaunch "
            "helper:\n"
            "  ./scripts/launch-tradingview-debug.sh\n"
            f"(equivalent: quit TradingView, then "
            f"open -a '{_TV_APP_NAME}' --args --remote-debugging-port={_TV_DEBUG_PORT})"
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
    """Manages the tradingview-mcp sidecar and exposes its tools to ClaudIA.

    Lifecycle:
      await bridge.start()        — spawn sidecar, list available tools
      bridge.get_tools()          — returns curated tool definitions for Anthropic SDK
      await bridge.execute(name, inputs)  — call a tradingview-mcp tool
      await bridge.stop()         — shut down sidecar gracefully
    """

    def __init__(self) -> None:
        """Create an unstarted bridge. Nothing is spawned until `start()`.

        Holds four pieces of state: the MCP `ClientSession`, the sidecar's full tool list,
        the curated subset actually exposed to the LLM, and `_cm` — the retained
        `stdio_client` context manager, which `stop()` needs in order to shut the
        subprocess down cleanly.
        """
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._curated_tools: list[dict] = []
        self._cm: AbstractAsyncContextManager[Any] | None = None  # stdio_client's context manager

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
        # Which CDP port the sidecar connects to (default 9222, overridable in .env via
        # TRADINGVIEW_DEBUG_PORT). THREE names are set because the sidecar renamed the
        # variable: upstream's src/connection.js now reads TV_CDP_PORT / CDP_PORT, while
        # CHROME_REMOTE_DEBUG_PORT is what older sidecars — including the vendor/
        # fallback snapshot — read. Setting only one name makes the override silently
        # do nothing on the other, which is exactly security-audit-2026-06-12 M-1
        # ("CHROME_REMOTE_DEBUG_PORT env var silently ignored by sidecar") returning
        # under a new spelling. The default is 9222 on both sides, so the failure only
        # appears for a non-default port — the quietest possible break.
        # Verified 2026-07-31 against sidecar commit 55534aa.
        env["CHROME_REMOTE_DEBUG_PORT"] = str(_TV_DEBUG_PORT)
        env["TV_CDP_PORT"] = str(_TV_DEBUG_PORT)
        env["CDP_PORT"] = str(_TV_DEBUG_PORT)

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
                # Unreachable per the mcp SDK's declared content types (TextContent etc., never
                # plain dict) — kept as defense-in-depth against the tradingview-mcp sidecar's
                # documented fragility (external Node.js process, not always SDK-conformant in
                # practice). See project-tradingview-robustness memory.
                elif isinstance(item, dict) and "text" in item:  # type: ignore[unreachable]
                    parts.append(item["text"])  # type: ignore[unreachable]
            raw = "\n".join(parts) if parts else json.dumps(result.content)
            return _post_process(name, raw)
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
