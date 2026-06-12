# TradingView MCP — Break Recovery Guide

## Architecture recap

```
ClaudIA (Python)
    ↓  MCP stdio pipe
tradingview-mcp (Node.js — tradesdontlie/tradingview-mcp)
    ↓  Chrome DevTools Protocol (CDP) — localhost:9222
TradingView Desktop (Electron)
```

The Node.js sidecar translates MCP tool calls into CDP commands against
TradingView's internal Electron renderer. Both the MCP tool names and the CDP
paths inside TradingView are internal APIs — they can change with any update.

## Binary layout (current version)

This is a **pure JavaScript** package — no TypeScript compilation step.

```
~/.tradingview-mcp/
  src/
    server.js          ← MCP stdio entry point (node src/server.js)
    connection.js      ← CDP connection (hardcoded port 9222)
    tools/             ← 78 tool implementations
  package.json
  node_modules/        ← npm install (only @modelcontextprotocol/sdk + chrome-remote-interface)
```

`_find_tv_mcp_bin()` in `tradingview.py` resolves the entry point in this order:
1. `TRADINGVIEW_MCP_PATH` env var
2. `tradingview-mcp` on PATH
3. `~/.tradingview-mcp/src/server.js` ← normal install location
4. `~/.tradingview-mcp/build/index.js` ← legacy TypeScript build output
5. `vendor/tradingview-mcp/src/server.js` (needs `node_modules/` present)
6. `vendor/tradingview-mcp/index.js` (legacy single-bundle archive)

---

## Prerequisites

TradingView Desktop must be launched with the CDP debug port open:

```bash
# Quit existing instance first, then:
open -a "Trading View" --args --remote-debugging-port=9222
```

Verify the port is open:
```bash
nc -zv localhost 9222   # should print "succeeded"
```

Without this flag, `check_cdp_running()` returns `False` and ClaudIA shows the
"Launch TradingView" button in the welcome message.

---

## Known break patterns

### 1. TradingView Desktop update breaks CDP frame structure

**Trigger:** TradingView Desktop auto-updates, changing internal JS context IDs or frame hierarchy.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp tool 'chart_get_state' failed:
  McpError: Tool execution failed: Cannot find context with specified id
  McpError: Runtime.evaluate failed: Session closed. Most likely the page has been closed.
  McpError: No target with given id found
```

**Symptoms in ClaudIA chat:** Tool calls return `"TradingView tool 'X' failed."` with no data.

**Fix:** Update tradingview-mcp to the latest commit and reinstall:
```bash
cd ~/.tradingview-mcp
git pull
npm install
# Restart ClaudIA — no build step needed
```
Then archive the working version (see below).

---

### 2. tradingview-mcp tool names changed (breaking update)

**Trigger:** The package renamed tools (e.g. `chart_get_state` → `get_chart_state`) in a breaking update.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp connected: 82 total tools, 0 curated
```
Zero curated tools means none of the names in `_CURATED_TOOLS` matched the new tool list.

**Symptoms in ClaudIA chat:** TradingView shows as connected but Claude never calls any TV tools.

**Fix:**
1. Restore the archived build (see below) to stay on the working version.
2. Then update `_CURATED_TOOLS` in `claudia/tradingview.py` to match the new names:
   ```python
   # Check what names the new sidecar actually exposes:
   # In Python: bridge = TradingViewBridge(); await bridge.start(); print([t["name"] for t in bridge.get_all_tools()])
   ```
3. Re-archive once verified.

---

### 3. Node.js version incompatibility after system upgrade

**Trigger:** `brew upgrade node` or macOS update installs a new Node.js major version.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp sidecar failed to start:
  Error [ERR_REQUIRE_ESM]: require() of ES Module not supported
  SyntaxError: Unexpected token '?'
  Error: Cannot find module '...'
```

**Fix** (pure JS — no build step):
```bash
cd ~/.tradingview-mcp
npm install   # rebuilds native deps for current Node version
# Restart ClaudIA
```
If it still fails, check the engines field:
```bash
node -e "const p=require('./package.json'); console.log(p.engines)"
node --version
```

---

### 4. CDP port no longer accepted by TradingView Desktop

**Trigger:** TradingView removes `--remote-debugging-port` support from their Electron build.

**Symptoms:** `check_cdp_running()` always returns `False` even after launching TradingView with the flag. Port 9222 never opens.

**Diagnosis:**
```bash
open -a "Trading View" --args --remote-debugging-port=9222
sleep 5
nc -zv localhost 9222    # should print "succeeded" if port is open
```

**Fix:** If the flag is permanently removed, the MCP sidecar can no longer function.
Fall back to direct CDP from Python — see "Fallback: direct CDP from Python" below.

---

### 5. MCP protocol version mismatch

**Trigger:** The `mcp` Python package (claudia_ui dependency) and the Node.js `tradingview-mcp` sidecar are built against incompatible MCP spec versions.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp sidecar failed to start:
  mcp.exceptions.McpError: Unsupported protocol version
  RuntimeError: MCP initialize failed
```

**Fix:**
1. Restore archived build (keeps the Node sidecar at the last known-compatible version).
2. If the Python `mcp` package was upgraded, pin it back:
   ```bash
   pip install "mcp==<last-working-version>"
   ```
3. Check `pyproject.toml` — the `mcp` version constraint should match what the archived build was tested against.

---

## Archiving a working version

Run after every successful update to snapshot the working build:

```bash
./scripts/archive-tv-mcp.sh
```

The script detects the layout automatically:
- **JS layout** (`src/server.js`): copies `src/` + `package.json` + installs prod deps into `vendor/`
- **TS layout** (`build/index.js`): copies the single bundle to `vendor/tradingview-mcp/index.js`

The archive lives in the repo directory — `src/` and `node_modules/` are gitignored, but `ARCHIVE_INFO` (metadata) is committed so you can see what version was archived.

---

## Restoring the archived version

**JS layout (current):**
```bash
# Option A: point env var at the archive
export TRADINGVIEW_MCP_PATH=/path/to/claudia_ui/vendor/tradingview-mcp/src/server.js

# Option B: copy archive over the broken install
cp -r vendor/tradingview-mcp/src ~/.tradingview-mcp/src
cp vendor/tradingview-mcp/package.json ~/.tradingview-mcp/package.json
cd ~/.tradingview-mcp && npm install
```

**Legacy TS bundle:**
```bash
# Option A: point env var at the archive
export TRADINGVIEW_MCP_PATH=/path/to/claudia_ui/vendor/tradingview-mcp/index.js

# Option B: copy archive over the broken build
cp vendor/tradingview-mcp/index.js ~/.tradingview-mcp/build/index.js
```

`_find_tv_mcp_bin()` in `tradingview.py` automatically falls back to the vendor directory
if the env var is unset and `~/.tradingview-mcp/src/server.js` does not exist.

---

## Fallback: direct CDP from Python

If the tradesdontlie sidecar becomes unmaintained, blocked by TradingView, or
otherwise unrecoverable, `TradingViewBridge` can be replaced with a direct
Playwright / pycdp implementation. No other file in claudia_ui needs to change —
only the internals of `claudia/tradingview.py`.

**Minimal skeleton** (replace `TradingViewBridge.start()` body):

```python
# pip install playwright && playwright install chromium
from playwright.async_api import async_playwright

class TradingViewBridge:
    async def start(self) -> None:
        if not check_cdp_running():
            await launch_tradingview()
        self._playwright = await async_playwright().start()
        # Attach to the already-running TradingView Desktop Electron process
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://localhost:{_TV_DEBUG_PORT}"
        )
        # TradingView uses multiple contexts; the main chart is typically the
        # first page whose URL contains "chart"
        self._page = next(
            (p for ctx in self._browser.contexts for p in ctx.pages
             if "chart" in p.url),
            self._browser.contexts[0].pages[0],
        )
        # Build your own tool dispatch table here:
        self._tools = _DIRECT_CDP_TOOLS   # list[dict] matching Anthropic schema

    async def execute(self, name: str, inputs: dict) -> str:
        handler = _CDP_HANDLERS.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        return await handler(self._page, inputs)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
```

The CDP API paths for common operations:
- **Get current symbol**: `page.evaluate("() => window.tvWidget?.activeChart()?.symbol()")`
- **Set symbol**: `page.evaluate(f"() => window.tvWidget?.activeChart()?.setSymbol('{symbol}')")`
- **Get indicators**: `page.evaluate("() => window.tvWidget?.activeChart()?.getAllStudies()")`
- **Set Pine source**: `page.evaluate(f"() => window.tvWidget?.activeChart()?.getStudyById(...).applyOverrides(...)")`

These paths are TradingView's semi-public widget API — more stable than CDP internal IDs
but still subject to change.

---

## Staying current without surprises

```bash
# Before any upgrade, check what changed
git -C ~/.tradingview-mcp log HEAD..origin/main --oneline

# Upgrade (no build step for pure JS version)
cd ~/.tradingview-mcp && git pull && npm install

# Quick smoke test (TradingView Desktop must be open with --remote-debugging-port=9222)
cd /path/to/claudia_ui
source .venv/bin/activate
python - <<'EOF'
import asyncio
from claudia.tradingview import TradingViewBridge, check_cdp_running
async def test():
    assert check_cdp_running(), "TradingView CDP port not open — relaunch with --remote-debugging-port=9222"
    b = TradingViewBridge()
    await b.start()
    tools = b.get_tools()
    print(f"OK: {len(tools)} curated tools: {[t['name'] for t in tools]}")
    await b.stop()
asyncio.run(test())
EOF

# Archive the verified version
./scripts/archive-tv-mcp.sh
```
