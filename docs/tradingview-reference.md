# TradingView Integration Reference

## Screenshot analysis (always available)

Drag or paste any TradingView chart screenshot into the chat. ClaudIA receives it as a
Claude vision content block and analyzes indicators, patterns, and price action.

## Live integration (requires TradingView Desktop)

The sidecar is [`tradesdontlie/tradingview-mcp`](https://github.com/tradesdontlie/tradingview-mcp)
(84 MCP tools + `tv` CLI, 4.1k stars) at upstream commit `55534aa` of 2026-07-06, which is
what both `~/.tradingview-mcp` and `vendor/` hold. It was 78 tools at `4795784a` (2026-04-03),
the commit vendored until 2026-07-31; `vendor/tradingview-mcp/ARCHIVE_INFO` is the record of
what is actually vendored and outranks this line. ClaudIA exposes a curated
17-tool subset by default to control token cost; the full set is available via `bridge.get_all_tools()`.

## Normal startup — no manual terminal commands needed

1. Run `./start-claudia.sh` (or `python -m claudia.panel_app`).
2. The **TradingView** button in the action bar under the chat is grey (or red) while
   TradingView Desktop is not connected (until 2026-09-03: a "Launch TradingView" button in
   the welcome message).
3. Click it — ClaudIA calls `launch_tradingview()` which runs
   `open -a "TradingView" --args --remote-debugging-port=9222`, waits up to 10 s for a
   TradingView **process** to exist, then up to 30 s for CDP port 9222, then reconnects the
   MCP sidecar. TV tools become available without a page reload. Progress and outcome go to
   the System log.
4. Three failure outcomes, told apart since 2026-09-04 (`launch_tradingview` docstring):
   - **"TradingView Desktop never started"** — `open` accepted the launch but no process
     appeared. **Measured 2026-09-04: this is what a ClaudIA server started from a Claude Code
     tool shell does** — `open -a` returns 0 and launches nothing there, not even Calculator.
     Launch TradingView from a real Terminal (`./scripts/launch-tradingview-debug.sh`) and
     click again: with the port already up the button skips the launch and only connects the
     sidecar. Better: start ClaudIA itself from Terminal (`./start-claudia.sh`).
   - **"already running without the remote debug port"** — the flag can only be set at
     launch; run the quit+relaunch helper. ClaudIA never quits the user's app.
   - **"did not open its debug port within 30s"** — the process exists but no port: the same
     helper, or the app build dropped the flag (`docs/tradingview-mcp-recovery.md`).
   A non-zero `open` exit is raised with its stderr (it used to go to `/dev/null`).

## Sidecar behavior when TradingView Desktop is not running

The sidecar starts and lists tools even when TV Desktop is not running, but tool calls fail
at the CDP layer — ClaudIA falls back to screenshot mode (drag/paste a chart screenshot into
chat).

> **Historical note:** a Python 3.14 / anyio `AsyncIOTaskInfo.__init__` (`current_task()`
> returning `None`) compat patch once lived in the Chainlit `app.py`. The project now targets
> Python 3.11 (`requires-python >=3.11,<3.14`) and `app.py` was removed in the Phase 11 Panel
> cutover, so that patch no longer applies.

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

## Curated 17-tool subset (`_CURATED_TOOLS` in `claudia/tradingview.py`)

All 16 names re-verified present against sidecar `55534aa` on 2026-07-31 (via `list_tools()`,
which needs no CDP); last verified against a *live* TradingView Desktop 2026-06-30. The 17th,
`data_get_indicator`, was curated on 2026-08-11 and **is the best-verified name in the set**:
registered at `~/.tradingview-mcp/src/tools/data.js:14`, reported by `list_tools()` the same
evening (`84 total tools, 17 curated` in the startup log), and **exercised against a live
TradingView Desktop** that night — it returned the real RSI input ids, which the model then used
to complete a change its first guess had silently no-opped. Tool
descriptions are provided by the sidecar
at runtime via MCP `list_tools()` — they appear in the Anthropic `tools=` parameter and
are the only documentation ClaudIA receives about what each tool does.

| Category | Tools |
|---|---|
| Chart reading | `chart_get_state`, `quote_get`, `data_get_ohlcv`, `data_get_study_values` |
| Chart control | `chart_set_symbol`, `chart_set_timeframe`, `indicator_set_inputs`, `data_get_indicator` (reads the input ids the setter needs) |
| Pine Script IDE | `pine_set_source`, `pine_smart_compile`, `pine_get_errors`, `pine_get_source` |
| Strategy results | `data_get_strategy_results`, `data_get_equity` (equity curve), `data_get_trades` |
| Utility | `tv_health_check`, `capture_screenshot` |

## Result post-processing (`_post_process` in `claudia/tradingview.py`)

**Every sidecar result crosses `TradingViewBridge.execute()`, and since 2026-08-11 it is
transformed there before the model sees it.** So a payload in `claudia.db` will not always match
what the sidecar emitted. Three transforms run, in a `_TRANSFORMS` tuple; each takes the tool name
and the parsed payload and returns a payload. All three came out of the 2026-08-11 live batch.

| Transform | What you will see | Why |
| --- | --- | --- |
| `_annotate_epochs` | a `<key>_utc` sibling beside `time` / `timestamp` / `from` / `to` — e.g. `"time": 1786455000, "time_utc": "2026-08-11T13:30:00Z"` | the payload carries bare epoch seconds and no date string, so the model was converting them in its head. It printed 2026-08-11 as "May 12", every row wrong, while every price round-tripped exactly |
| `_flag_empty_result` | a `claudia_warning` key on a tool that reported success while its own payload shows a no-op | `indicator_set_inputs` returns `success: true` with `updated_inputs: {}` when no override key matched a real input id, and the model announced the change as done |
| `_trim_blobs` | `"text": "<omitted: 2422 chars>"` in place of an oversized value under a `text` key | Pine studies carry obfuscated source there. It was 5,280 of 8,641 chars (61%) of a live `data_get_study_values` result |

Four properties worth knowing before changing anything here:

- **The seam is inert unless it fires.** 12 of the 16 payloads captured on 2026-08-11 come back
  byte-identical. `indent=2` and the default `ensure_ascii=True` are what preserve that — the
  sidecar already emits indent-2 JSON. **Do not set `ensure_ascii=False`**: JS escapes a lone
  surrogate to ASCII, and re-emitting it raw produces a `str` that cannot be UTF-8 encoded, which
  crashes the `conversation_store` insert. That was tried and reverted the same day.
- **It fails open.** Anything that is not a JSON object, and anything a transform raises on, is
  returned untouched. A transform that swallowed a tool result — or turned a succeeded call into a
  reported failure — would be worse than the defects it fixes.
- **`claudia_warning` is a reserved key**, dropped from the incoming payload before any transform
  runs. It is our channel for telling the model to distrust a payload, so it cannot be sourced
  from the thing being judged. Note this is the *opposite* resolution from `_utc`, where a
  sidecar-supplied sibling legitimately wins — that one is data, this one is a trust statement.
- **Epoch annotation is UTC, never exchange-local.** The exchange is not knowable from the payload
  and guessing it would be an instrument-specific rule. Expect ClaudIA's times to differ from the
  chart's clock by the exchange offset — on 2026-08-11 the chart read `16:00` UTC-4 for the bar
  annotated `20:00:00Z`. Both correct; not a regression.

Live-verified 2026-08-11 (evening): dates matched the payload 20/20, and a deliberately wrong
input key produced the warning, which the model read and recovered from by calling
`data_get_indicator`. **The blob trim has not fired live** — it is verified by replaying the
captured payload only.

## Upgrading the sidecar

**Upgraded 2026-07-31 to `55534aa`** (was `46ec2d3`, 60 commits behind). What it took, because
the next upgrade will hit the same three things:

**1. The local commit was dropped, not merged.** `46ec2d3` restored `CHROME_REMOTE_DEBUG_PORT`
support in `src/connection.js`; upstream had meanwhile rewritten that lookup to read
`TV_CDP_HOST`/`TV_CDP_PORT` (falling back to `CDP_HOST`/`CDP_PORT`) — strictly better, and it
does **not** read the old name. Upstream's version wins, so the local commit was preserved as
branch `local-cdp-patch-46ec2d3` + tag `pre-upgrade-2026-07-31` in `~/.tradingview-mcp` and the
checkout reset to `origin/main`.

**2. That rename was a silent break on our side, and it is now fixed.**
[`tradingview.py`](../claudia/tradingview.py) set only `CHROME_REMOTE_DEBUG_PORT`, so after the
pull a non-default `TRADINGVIEW_DEBUG_PORT` would have been ignored and the sidecar would have
used 9222 — no error, no log line. This is security-audit-2026-06-12 **M-1 returning under a new
spelling.** Proven, not assumed: with an HTTP listener on 127.0.0.1:9333, `CHROME_REMOTE_DEBUG_PORT=9333`
produced **0** requests while `TV_CDP_PORT=9333` produced **5** `/json/list` requests. ClaudIA
now sets **all three names**, so both upstream and the older `vendor/` snapshot honour the
override; a regression test asserts all three carry the configured port.

**3. `npm audit` needed a separate pass.** The pull alone left the vulnerability count unchanged
(the advisories are in transitive deps of `@modelcontextprotocol/sdk` and `eslint`, not in
sidecar code). Plain `npm audit fix` — no `--force` — cleared all six: **0 vulnerabilities**,
prod and dev. Cost: `~/.tradingview-mcp/package-lock.json` now deviates from upstream's. Expect
that file to show as modified until upstream bumps its own lockfile; do not "restore" it.

Verification run: sidecar unit tests **152/152 pass** (`npm run test:unit`), lint 0 errors /
4 upstream warnings, **84 tools** exposed (up from 78) with **all 16 curated tools present** —
no renames, so `_CURATED_TOOLS` was not touched. `tests/e2e.test.js` fails without TradingView
Desktop running, which is expected. `vendor/` re-snapshotted to `55534aa`.

**Still unproven *as of that 2026-07-31 upgrade*:** every tool *call* against live TradingView.
`list_tools()` needs no CDP, so the tool-name check above is solid, but schema drift inside a
tool is not auto-detected — the 16 curated tools had not been exercised against a live desktop
at that point. **Closed since:** the full T0–T8 batch ran 2026-08-11 against this sidecar and
completed 2026-08-12; the curated set is now **17** (`data_get_indicator` added and
live-verified). See § Result post-processing and `docs/project-status.md` § Live Test Log.

The 68 non-curated tools now include `tv_update` (sidecar self-update), `alert_create`/`_list`/
`_delete`, the overhauled `watchlist_*` set and `tv_launch`. **Whether any of those should join
the curated 16 is an open product question — not decided here.**

**Partly answered 2026-08-11:** one of those 68, `data_get_indicator`, joined the set — because
`indicator_set_inputs` takes input ids the model cannot guess and an unmatched key is a silent
no-op reported as success. The rest of the question stands. The ceiling on the set is
deliberate: every curated tool costs a schema in every request, so an addition needs evidence of
a specific failure, not plausibility.

```bash
git -C ~/.tradingview-mcp pull
npm -C ~/.tradingview-mcp install
npm -C ~/.tradingview-mcp audit fix        # no --force; re-check with: npm audit
npm -C ~/.tradingview-mcp run test:unit    # e2e needs TradingView Desktop running
# Restart ClaudIA — startup log will show commit and warn of any renamed tools:
#   INFO  tradingview-mcp sidecar: .../server.js (commit abc1234)
#   INFO  tradingview-mcp connected: 84 total tools, 17 curated
#   WARNING  curated tools not found in sidecar: {data_get_equity_curve}  ← rename detected
# If a WARNING appears, update _CURATED_TOOLS in claudia/tradingview.py, then:
./scripts/archive-tv-mcp.sh    # snapshot the new working version to vendor/
```

⚠ **Check `src/connection.js` for env-var renames on every upgrade.** It has now happened twice.
The failure mode is silence, so nothing in the startup log will tell you.

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
