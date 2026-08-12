# ClaudIA UI Documentation

This directory holds three kinds of documentation. See `CLAUDE.md`'s Pointers section for the
most commonly needed links; this file is the full catalog.

## Reference

Living documentation describing current behavior — read on demand, updated in place as the
code changes.

Grouped by domain — a flat list (seventeen entries at the time) made it hard to see which document
owns a question. Within each group, the file that answers "where do I start" is listed first.

**IBKR — the broker connection, orders and trade data**

| File | Description |
| --- | --- |
| [`ibkr-gateway.md`](ibkr-gateway.md) | **The gateway session, start here for anything IBKR-connection.** Phases, who may touch the session and from which runtime, the login runbook, borrowed-session and IB Key failures, the container image trap, and the incident record |
| [`order-api-reference.md`](order-api-reference.md) | Full order-staging spec — Gate 1/2, the parameter-immutability rule, the `conid` requirement |
| [`trading-data-reference.md`](trading-data-reference.md) | Trade data architecture, Flex vs live API, P&L and the execution listener |
| [`flex-query-setup.md`](flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`market-calendar-reference.md`](market-calendar-reference.md) | 20-exchange market calendar and futures schedules |

**Running ClaudIA — startup, connectivity, configuration**

| File | Description |
| --- | --- |
| [`startup-flow.md`](startup-flow.md) | Every phase of ClaudIA startup in order — the first place to look at a failed launch |
| [`connectivity.md`](connectivity.md) | Status dots, **Google Drive and TradingView** check logic and reconnection. IBKR moved to `ibkr-gateway.md` on 2026-08-06 |
| [`env-vars-reference.md`](env-vars-reference.md) | Full environment variable reference |
| [`gdrive-sync-reference.md`](gdrive-sync-reference.md) | GDrive sync — folder layout, credential flow, error handling |
| [`windows-compatibility.md`](windows-compatibility.md) | Windows-specific platform issues found during macOS development |

**The agent — context, memory, prompt construction**

| File | Description |
| --- | --- |
| [`agent-behavior-reference.md`](agent-behavior-reference.md) | **How ClaudIA is stopped from asserting what it did not do — start here for anything about model behavior.** The three enforcement layers and why a rule in the wrong one is not a control, `_SAFETY_BLOCK`'s nine sections, the four claim detectors (*trigger textual, verdict evidence*), the operator channel, the frozen precision measurement, the Anthropic technique-by-technique map, and the known limits |
| [`context-loading-reference.md`](context-loading-reference.md) | `context.md`/`principles.md` loading, hot-reload, versioning, prompt-cache mechanics |
| [`conversation-memory-reference.md`](conversation-memory-reference.md) | `claudia.db` schema — sessions, messages, decisions, doc_versions, FTS5 |
| [`prompt-caching-upgrade.md`](prompt-caching-upgrade.md) | Prompt-caching implementation note — status, verified numbers, findings |

**TradingView**

| File | Description |
| --- | --- |
| [`tradingview-reference.md`](tradingview-reference.md) | TradingView integration — sidecar, curated tools, screenshot mode |
| [`tradingview-mcp-recovery.md`](tradingview-mcp-recovery.md) | Sidecar break patterns and recovery steps |

**Project-wide**

| File | Description |
| --- | --- |
| [`project-status.md`](project-status.md) | Living status — milestone history, test coverage, live testing (index/outstanding/log), work plan, known gaps |
| [`api-reference.md`](api-reference.md) | Anthropic / IBKR / Drive / Panel source-of-truth URLs; the scraped-evidence convention |

`context.md`, `principles.md`, and `versions/` are personal, git-ignored, hot-reloaded files —
not part of this catalog. See `context-loading-reference.md` for how they're loaded.

**Lives in the other repo, but ClaudIA depends on it:** the four web tools ClaudIA can call
(`fetch_page`, `crawl_site`, `search_site`, `firecrawl_search`) are documented in
`ibkr_core_mcp/docs/web-scraper-reference.md` — paywalled-site logins, what a blocked page looks
like, and the mandatory live-test procedure. Two behaviors surprise people from this side: the
tools need the `[scraper]` extra and every import is lazy, so a missing extra fails at tool-call
time rather than at startup; and a fetch of a domain with a saved login profile opens a **real
browser window** and is **serialised per profile**, both required rather than incidental.

## Panel framework reference (`docs/panel/`)

Living reference for the Panel UI (the framework since the 2026-07-24 Chainlit→Panel
cutover) — each claim backed by a `file:line`, the installed package, or a scraped URL. Start
at [`panel/README.md`](panel/README.md), which indexes:

| File | Description |
| --- | --- |
| [`panel/panel-reference.md`](panel/panel-reference.md) | How ClaudIA uses Panel — serving model, session lifecycle, layout tree, `MessageSink` seam, widget gotchas, status dots, chart pane, headless button testing, dependency state |
| [`panel/ui-design-reference.md`](panel/ui-design-reference.md) | UI design & styling — the no-styling baseline, the shadow-DOM constraint, Panel's scraped styling surface (designs/themes/tokens/templates), open design questions, a proposed Track D direction, official-source index |
| [`panel/component-model-reference.md`](panel/component-model-reference.md) | How a Panel component is built, parameterised and composed — the model behind the two references above |
| [`panel/data-surfaces-reference.md`](panel/data-surfaces-reference.md) | Tabulator / Number / ECharts, the `pn.extension()` gate, side windows, stream/patch + connectivity, and 27 measured gotchas (16 onwards found live against the account) — the reference the live dashboard was built from |

Plus two dated research docs (candlestick chart pane, PineScript/actionable buttons) and the
migration's smoke screenshots. The post-migration restyle project draws from here.

## Probes (`docs/probes/`)

Runnable verification scripts committed verbatim as they were run (D4 thread→session
bridge, D7 session-destroy, `pn.serve` behavior, watchdog) — the executable evidence behind
load-bearing claims in the migration plan and `docs/panel/`. See
[`probes/README.md`](probes/README.md).

## Plans (`docs/plans/` — git-ignored, local + Google Drive only)

**All plans live here, and the whole directory is git-ignored** (user rule 2026-07-24:
plans are personal working documents, kept local and on Drive, never committed). Any
`docs/plans/...` path mentioned elsewhere in this repo is a pointer into this local
archive, not a repo file. Designs (`*-design.md`), implementation plans, and
workflow-executed plans alike (never in a separate `docs/superpowers/` directory). Filenames
carry a `YYYY-MM-DD-<topic>` prefix, so a filename sort gives chronological order. A design
spec captures the why/what, a plan captures the how. Workflow-executed plans (e.g.
`2026-07-22-panel-migration.md`, the complete
Chainlit→Panel migration record) are living documents *during* execution — task notes and
review outcomes appended in place — and freeze once their project completes. All others are
point-in-time records: once written they are not edited to reflect later changes, including
later file moves; a later revisit gets a new dated file. Browse the directory directly
rather than looking for an index entry here.

## Audits (`docs/audits/`)

Point-in-time investigation and verification records — security audits, code audits (e.g. the
agent information-handling architecture review), a bug-finding sprint report, and the
accumulated live-test log ([`live-test-log.md`](audits/live-test-log.md)). Same treatment as
Plans: dated filenames, not retroactively edited. Browse directly rather than looking for an
index entry here.
