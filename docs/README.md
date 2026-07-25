# ClaudIA UI Documentation

This directory holds three kinds of documentation. See `CLAUDE.md`'s Pointers section for the
most commonly needed links; this file is the full catalog.

## Reference

Living documentation describing current behavior — read on demand, updated in place as the
code changes.

| File | Description |
| --- | --- |
| [`api-reference.md`](api-reference.md) | Anthropic/IBKR/Drive/Panel source-of-truth URLs; scraped-evidence convention |
| [`connectivity.md`](connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows |
| [`context-loading-reference.md`](context-loading-reference.md) | `context.md`/`principles.md` loading, hot-reload, versioning, prompt-cache mechanics |
| [`conversation-memory-reference.md`](conversation-memory-reference.md) | `claudia.db` schema — sessions, messages, decisions, doc_versions, FTS5 |
| [`env-vars-reference.md`](env-vars-reference.md) | Full environment variable reference |
| [`flex-query-setup.md`](flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`gdrive-sync-reference.md`](gdrive-sync-reference.md) | GDrive sync — folder layout, credential flow, error handling |
| [`market-calendar-reference.md`](market-calendar-reference.md) | 20-exchange market calendar, futures schedules |
| [`order-api-reference.md`](order-api-reference.md) | Full order-staging spec (Gate 1/2, immutability rule, FUT/FOP conid) |
| [`project-status.md`](project-status.md) | Living status doc — milestone history, test coverage, live testing (index/outstanding/log), work plan, known gaps |
| [`prompt-caching-upgrade.md`](prompt-caching-upgrade.md) | Prompt-caching implementation note — status, verified numbers, findings |
| [`startup-flow.md`](startup-flow.md) | Every phase of ClaudIA startup in order |
| [`trading-data-reference.md`](trading-data-reference.md) | Trade data architecture, Flex vs live API, P&L/execution listener |
| [`tradingview-mcp-recovery.md`](tradingview-mcp-recovery.md) | TradingView sidecar break patterns and recovery steps |
| [`tradingview-reference.md`](tradingview-reference.md) | TradingView integration — curated tools, screenshot mode |
| [`windows-compatibility.md`](windows-compatibility.md) | Windows-specific platform issues found during macOS development |

`context.md`, `principles.md`, and `versions/` are personal, git-ignored, hot-reloaded files —
not part of this catalog. See `context-loading-reference.md` for how they're loaded.

## Panel framework reference (`docs/panel/`)

Living reference for the Panel UI (the framework since the 2026-07-24 Chainlit→Panel
cutover): verified `pn.serve`/widget/chart/clipboard findings — each backed by the installed
package or a scraped URL — plus the migration's smoke screenshots. Start at
[`panel/README.md`](panel/README.md); the post-migration restyle plan draws from here.

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
