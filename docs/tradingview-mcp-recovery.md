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

**Fix:** Update tradingview-mcp to the latest commit, which will have updated the internal CDP paths.
```bash
cd ~/.tradingview-mcp
git pull
npm install
npm run build
# Restart ClaudIA
```
Then archive the new working build (see "Archiving a working version" below).

---

### 2. tradingview-mcp tool names changed (breaking npm/git update)

**Trigger:** The package renamed tools (e.g. `chart_get_state` → `get_chart_state`) in a breaking update.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp connected: 82 total tools, 0 curated
```
Zero curated tools means none of the names in `_CURATED_TOOLS` matched the new tool list.

**Symptoms in ClaudIA chat:** TradingView shows as connected but Claude never calls any TV tools.

**Fix:**
1. Restore the archived build (see below) so you stay on the working version.
2. Then update `_CURATED_TOOLS` in `claudia/tradingview.py` to match the new names:
   ```python
   # Check what names the new sidecar actually exposes:
   # In Python: bridge = TradingViewBridge(); await bridge.start(); print([t["name"] for t in bridge.get_all_tools()])
   ```
3. Re-archive once verified.

---

### 3. Node.js version incompatibility after system upgrade

**Trigger:** `brew upgrade node` or macOS update installs a new Node.js major version incompatible with the built sidecar.

**Symptoms in ClaudIA logs:**
```
tradingview-mcp sidecar failed to start:
  Error [ERR_REQUIRE_ESM]: require() of ES Module not supported
  SyntaxError: Unexpected token '?'
  Error: Cannot find module '...'
```

**Fix:**
```bash
cd ~/.tradingview-mcp
npm install          # rebuilds native deps for current Node
npm run build        # re-bundles
```
If the build still fails, check the Node version required:
```bash
cat ~/.tradingview-mcp/package.json | grep '"node"'
node --version
```
Install the required version via `nvm` or `brew install node@XX`.

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

The script:
- Copies `build/index.js` to `vendor/tradingview-mcp/index.js`
- Records the git commit, date, and Node version in `vendor/tradingview-mcp/ARCHIVE_INFO`

The archive is gitignored (binary artifact) but lives in the repo directory so it survives `git pull` on claudia_ui.

---

## Restoring the archived version

```bash
# Option A: point env var at the archive (no file changes needed)
export TRADINGVIEW_MCP_PATH=/path/to/claudia_ui/vendor/tradingview-mcp/index.js

# Option B: copy archive over the broken build
cp vendor/tradingview-mcp/index.js ~/.tradingview-mcp/build/index.js
```

`_find_tv_mcp_bin()` in `tradingview.py` automatically falls back to `vendor/tradingview-mcp/index.js`
if the env var is unset and `~/.tradingview-mcp/build/index.js` does not exist.

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
but still subject to change. The `_study_id` needed for Pine injection requires navigating
TradingView's internal study model, which is where most maintenance effort goes.

---

## Staying current without surprises

```bash
# Before any upgrade, check what changed
git -C ~/.tradingview-mcp log HEAD..origin/main --oneline

# Upgrade + rebuild
cd ~/.tradingview-mcp && git pull && npm install && npm run build

# Quick smoke test (TradingView Desktop must be open)
cd /path/to/claudia_ui
python - <<'EOF'
import asyncio
from claudia.tradingview import TradingViewBridge, check_cdp_running
async def test():
    assert check_cdp_running(), "TradingView CDP port not open"
    b = TradingViewBridge()
    await b.start()
    tools = b.get_tools()
    print(f"OK: {len(tools)} curated tools: {[t['name'] for t in tools]}")
    await b.stop()
asyncio.run(test())
EOF

# Archive the verified build
./scripts/archive-tv-mcp.sh
```
