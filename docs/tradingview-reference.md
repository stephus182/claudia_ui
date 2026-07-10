# TradingView Integration Reference

## Screenshot analysis (always available)

Drag or paste any TradingView chart screenshot into the chat. ClaudIA receives it as a
Claude vision content block and analyzes indicators, patterns, and price action.

## Live integration (requires TradingView Desktop)

The sidecar is [`tradesdontlie/tradingview-mcp`](https://github.com/tradesdontlie/tradingview-mcp)
(78 MCP tools + `tv` CLI, 4.1k stars, last updated April 2026). ClaudIA exposes a curated
16-tool subset by default to control token cost; the full set is available via `bridge.get_all_tools()`.

## Normal startup — no manual terminal commands needed

1. Run `./start-claudia.sh` (or `chainlit run claudia/app.py`).
2. If TradingView Desktop is not running, the welcome message shows a **"Launch TradingView"** button.
3. Click it — ClaudIA calls `launch_tradingview()` which runs
   `open -a "TradingView" --args --remote-debugging-port=9222`, polls for CDP port 9222
   up to 30s, then reconnects the MCP sidecar. TV tools become available without a page reload.
4. If TV is already running **without** the debug port, the button shows an error with
   instructions to quit TV and relaunch — ClaudIA cannot inject the debug flag into a running process.

## Python 3.14 compatibility note

The sidecar now starts successfully even when TV Desktop is not running (fixed 2026-06-30:
`AsyncIOTaskInfo.__init__` patched in `app.py` to handle `current_task()` returning `None` —
the 5th Python 3.14/anyio compat patch). When TV Desktop is not running: sidecar starts, tools
are listed, but tool calls fail at the CDP layer. ClaudIA falls back to screenshot mode
(drag/paste a chart screenshot into chat). The anyio upstream bug (`_MemoryObjectItemReceiver`
+ `get_current_task`) is unfixed in anyio 4.14.1 and MCP 1.28.1 as of 2026-06-30.

## Binary discovery order (`_find_tv_mcp_bin()`)

1. `TRADINGVIEW_MCP_PATH` env var (validated: file must exist and end in `.js`)
2. `tradingview-mcp` on PATH
3. `~/.tradingview-mcp/src/server.js` (pure JS layout — current)
4. `~/.tradingview-mcp/build/index.js` (TypeScript build output — legacy)
5. `vendor/tradingview-mcp/src/server.js` (archived fallback, needs `node_modules/`)
6. `vendor/tradingview-mcp/index.js` (legacy single-bundle archive)

## PineScript

ClaudIA generates PineScript v5 directly. Use the **"Inject into TradingView"**
button to paste it into the Pine Editor via the `pine_set_source` MCP tool.

## Curated 16-tool subset (`_CURATED_TOOLS` in `claudia/tradingview.py`)

Verified against live sidecar 2026-06-30. Tool descriptions are provided by the sidecar
at runtime via MCP `list_tools()` — they appear in the Anthropic `tools=` parameter and
are the only documentation ClaudIA receives about what each tool does.

| Category | Tools |
|---|---|
| Chart reading | `chart_get_state`, `quote_get`, `data_get_ohlcv`, `data_get_study_values` |
| Chart control | `chart_set_symbol`, `chart_set_timeframe`, `indicator_set_inputs` |
| Pine Script IDE | `pine_set_source`, `pine_smart_compile`, `pine_get_errors`, `pine_get_source` |
| Strategy results | `data_get_strategy_results`, `data_get_equity` (equity curve), `data_get_trades` |
| Utility | `tv_health_check`, `capture_screenshot` |

## Upgrading the sidecar

```bash
git -C ~/.tradingview-mcp pull
npm -C ~/.tradingview-mcp install
# Restart ClaudIA — startup log will show commit and warn of any renamed tools:
#   INFO  tradingview-mcp sidecar: .../server.js (commit abc1234)
#   INFO  tradingview-mcp connected: 78 total tools, 16 curated
#   WARNING  curated tools not found in sidecar: {data_get_equity_curve}  ← rename detected
# If a WARNING appears, update _CURATED_TOOLS in claudia/tradingview.py, then:
./scripts/archive-tv-mcp.sh    # snapshot the new working version to vendor/
```

## Version detection at startup (`claudia/tradingview.py → TradingViewBridge.start()`)

- Logs sidecar binary path + git commit (best-effort; `unknown` if running from vendor/)
- Logs total tool count and curated count
- Emits a `WARNING` if any name in `_CURATED_TOOLS` is absent from the sidecar — detects
  silent tool renames between sidecar versions (e.g. `data_get_equity_curve` → `data_get_equity`)
- Tool descriptions and input schemas come from the sidecar's `list_tools()` — ClaudIA has
  no hardcoded schema; what the sidecar reports is what Claude receives in `tools=`
- Schema drift (a tool exists but its parameters changed) is not auto-detected — check the
  sidecar changelog (https://github.com/tradesdontlie/tradingview-mcp) after any `git pull`

## Break recovery

If the sidecar breaks after a TradingView or npm update, see
`docs/tradingview-mcp-recovery.md` for the error signature catalog and recovery steps.

## Vendor archive

Run `./scripts/archive-tv-mcp.sh` after every verified install to snapshot the working
version to `vendor/tradingview-mcp/`. For the JS layout it copies `src/` + installs prod
deps; for legacy TS it copies the single bundle. ClaudIA automatically falls back to this
archive if the live install at `~/.tradingview-mcp/` is missing or broken.
