# Documentation Architecture Cleanup — Design

## Problem

`docs/` mixes three kinds of material with no structural distinction: living reference docs
read on demand (14), design specs (`docs/superpowers/specs/`, 7 files), implementation plans
(`docs/superpowers/plans/`, 11 files), and dated point-in-time investigation records — security
audits, a bug-finding sprint report, an architecture review, and its evidence file — sitting
loose at the `docs/` root (6 files) alongside the reference docs. There is no `docs/README.md`
catalog, so discoverability depends entirely on `CLAUDE.md`'s hand-curated Pointers section (9
links) — anything not listed there is `ls`/`grep`-only.

This mirrors the exact problem `ibkr_core_mcp` fixed on 2026-07-13
(`ibkr_core_mcp/docs/plans/2026-07-13-docs-reorg-design.md`). This design applies the same
three-category structure to `claudia_ui`, adapted to what's actually in this repo — no
`docs/*-design.md` root files or `audit-evidence/` directory here, and a filename-collision
situation that doesn't arise (see Category 2).

`CLAUDE.md` is not touched structurally — it was already split from 937 to 146 lines on
2026-07-10 (`docs/plans/2026-07-10-claude-md-delink-imports.md`, moved here by this same plan)
specifically to fix context bloat. This cleanup only adds one line to its existing Pointers
section and repoints two links whose targets move.

## Decisions

### Category 1 — Reference (stays flat in `docs/`, hand-indexed)

Living documentation describing current behavior, read on demand, updated in place as the code
changes. Stays exactly where it is — no physical move. Indexed in a new `docs/README.md` with
one line + description each:

| File | Description |
| --- | --- |
| `api-reference.md` | Anthropic/IBKR/Drive/Chainlit source-of-truth URLs; scraped-evidence convention |
| `connectivity.md` | IBKR / GDrive / TradingView check logic, reconnection flows |
| `context-loading-reference.md` | `context.md`/`principles.md` loading, hot-reload, versioning, prompt-cache mechanics |
| `conversation-memory-reference.md` | `claudia.db` schema — sessions, messages, decisions, doc_versions, FTS5 |
| `env-vars-reference.md` | Full environment variable reference |
| `flex-query-setup.md` | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| `gdrive-sync-reference.md` | GDrive sync — folder layout, credential flow, error handling |
| `market-calendar-reference.md` | 20-exchange market calendar, futures schedules |
| `order-api-reference.md` | Full order-staging spec (Gate 1/2, immutability rule, FUT/FOP conid) |
| `project-status.md` | Living status doc — feature timeline, known gaps, live test log/plan |
| `prompt-caching-upgrade.md` | Prompt-caching implementation note — status, verified numbers, findings |
| `startup-flow.md` | Every phase of ClaudIA startup in order |
| `trading-data-reference.md` | Trade data architecture, Flex vs live API, P&L/execution listener |
| `tradingview-mcp-recovery.md` | TradingView sidecar break patterns and recovery steps |
| `tradingview-reference.md` | TradingView integration — curated tools, screenshot mode |
| `windows-compatibility.md` | Windows-specific platform issues found during macOS development |

`context.md`, `principles.md`, and `versions/` are git-ignored personal/hot-reloaded files —
not part of this reorg, not listed in the `docs/README.md` table, mentioned only in prose.

`prompt-caching-upgrade.md` is classified Reference, not Plan, despite living at the repo root
without a date prefix and describing one feature: unlike the immutable plan documents (Category
2), it was edited in place after initial writing (its own "Implementation findings (2026-07-03)"
section was appended post-implementation, and its `**Status:**` line is updated as work
completes) — the same living-document treatment as the rest of Category 1, just for a narrower
topic.

### Category 2 — Design specs & Plans → consolidate into `docs/plans/`

Point-in-time records of what was decided (`*-design.md`) and how it was carried out (plain
topic name, no suffix — this repo's plans never adopted a `*-plan.md` suffix the way
`ibkr_core_mcp`'s did). Not enumerated in `docs/README.md` — the index names the directory and
convention, not each file. Filenames keep their `YYYY-MM-DD-<topic>` prefix, so a filename sort
is a chronological sort.

18 files consolidate from 2 current locations into one, **with no renames** — unlike
`ibkr_core_mcp`'s reorg, no spec/plan filename pair collides here (every `*-design.md` in
`docs/superpowers/specs/` already carries the `-design` suffix, and no file in
`docs/superpowers/plans/` shares a bare name with one):

- `docs/superpowers/specs/*.md` (7 files): `2026-06-09-claudia-ui-redesign-design.md`,
  `2026-06-11-gdrive-sync-design.md`, `2026-07-01-scraping-rag-pipeline-design.md`,
  `2026-07-06-live-pnl-streaming-design.md`, `2026-07-07-execution-triggered-pnl-design.md`,
  `2026-07-10-gdrive-auth-dedup-design.md`, `2026-07-10-gdrive-oauth-client-migration-design.md`
- `docs/superpowers/plans/*.md` (11 files): `2026-06-09-ui-status-bar.md`,
  `2026-06-11-gdrive-sync.md`, `2026-06-12-test-coverage.md`,
  `2026-07-03-prompt-caching-upgrade.md`, `2026-07-06-live-pnl-streaming.md`,
  `2026-07-07-execution-triggered-pnl.md`, `2026-07-08-order-cancel-modify.md`,
  `2026-07-10-claude-md-delink-imports.md` (**untracked** — created this week, never committed;
  moved with `mv` + `git add`, not `git mv`), `2026-07-10-gdrive-auth-dedup.md`,
  `2026-07-10-live-test-bugfixes-handoff.md`, `2026-07-10-live-test-bugfixes.md`

Plus this design doc and its companion plan (`2026-07-14-docs-reorg-design.md`,
`2026-07-14-docs-reorg-plan.md`) — created directly at the new location, establishing
`docs/plans/` as the going-forward convention the same way `ibkr_core_mcp`'s reorg doc did for
that repo.

**Internal cross-references between these files are not fixed.** Several plan/spec files
reference each other by path (e.g. `execution-triggered-pnl.md` → `live-pnl-streaming-design.md`).
Per the same precedent `ibkr_core_mcp` set: these are immutable point-in-time records — once
written, not edited to reflect later changes, including later file moves. A later revisit gets a
new dated file, not a patched old one. This is deliberate, not an oversight — see Category 3 for
the one place this project draws the line differently (living docs that link *into* this
directory).

### Category 3 — Audits → consolidate into `docs/audits/`

Same point-in-time treatment as Plans. 6 files consolidate from `docs/` root:

- `security-audit-2026-06-12.md`, `security-audit-2026-06-25.md`
- `test-coverage-findings-2026-06-15.md` — bug-finding sprint report; same investigative nature
  as a security audit even though the filename doesn't say "audit"
- `2026-07-03-agent-info-architecture-review.md` — a code audit (explicit `**Method:**` line:
  "Code read of `agent.py`, `context_loader.py`, ..."), reclassified from ambiguous
  root-level doc to Audit on the same basis `ibkr_core_mcp` used for
  `2026-06-30-quote-access-matrix.md`
- `2026-07-03-llm-best-practices-sources.md` — the evidence trail backing the architecture
  review and the caching plan (claim→source table, scraped 2026-07-03); moves alongside the
  review it supports, the same relationship `ibkr_core_mcp`'s `audit-evidence/` has to
  `claude-tools-audit-2026-07.md`. No raw-evidence directory exists in this repo — the evidence
  is one markdown file, not a JSON/patch dump, so it moves as a normal file, not a subdirectory.
- `live-test-log.md` — accumulated record of live-test runs, appended to per run (see
  `docs/live-test-log.md`, note its own line 5 already cross-references
  `ibkr_core_mcp/docs/live-test-log.md`, which moved under that repo's own reorg — a pre-existing
  stale link this plan does not touch, since fixing it would mean editing an Audit-category
  immutable log entry, out of scope here). As a single accumulating file it gets one explicit
  line in `docs/README.md` rather than "browse, don't enumerate" treatment.

### New file — `docs/README.md`

The categorized catalog. Three sections matching the categories above: Reference hand-listed
with descriptions; Plans and Audits described as directories/conventions, not enumerated.
GitHub auto-renders this as the `docs/` folder landing page.

### Cross-links

- Root `README.md`'s existing `## Documentation` table (4 entries: `CLAUDE.md`, `SECURITY.md`,
  `flex-query-setup.md`, `tradingview-mcp-recovery.md`, `connectivity.md`, `project-status.md`)
  gets one new row pointing at `docs/README.md` as the full catalog.
- `CLAUDE.md`'s existing Pointers section gets one new bullet, same treatment.

### Living-doc reference fixes (the one place internal links ARE fixed)

Unlike Category 2/3's internal point-in-time cross-references, **Category 1 (Reference) docs
that link into the directories being moved get their links fixed**, because Reference docs are
living — read on demand, expected to be accurate right now, not a historical record. Same
principle `ibkr_core_mcp` applied in its Task 5 (`test-coverage.md` → moved `live-test-log.md`).

Scoped to files found (during design, via repo-wide grep) referencing a path this plan moves:
`CLAUDE.md`, root `README.md`, `SECURITY.md`, `docs/gdrive-sync-reference.md`,
`docs/project-status.md`, `docs/prompt-caching-upgrade.md`, `docs/trading-data-reference.md`,
`docs/context-loading-reference.md`, `docs/api-reference.md`, and two code docstrings
(`claudia/execution_listener.py`). `claudia/agent.py`'s one docs-path comment
(`docs/prompt-caching-upgrade.md`) needs no change — that target isn't moving.

One reference is a same-directory bare relative link, not a `docs/`-prefixed path, so the
blanket substitution below won't catch it: `docs/prompt-caching-upgrade.md`'s
`[2026-07-03 run 1](live-test-log.md#run-2026-07-03-1)` must become
`(audits/live-test-log.md#run-2026-07-03-1)` by hand.

**Not fixed, deliberately:** `docs/project-status.md:481`'s reference to
`ibkr_core_mcp/docs/future-doc-scraper.md` is already a dead link — that file was deleted in
`ibkr_core_mcp`'s own 2026-07-13 reorg (Task 3 there). Pre-existing, unrelated to this plan,
cross-repo — flagged here for the record, not fixed, to keep this plan's scope to `claudia_ui`'s
own structure.

## Industry-practice grounding

Same grounding `ibkr_core_mcp`'s reorg already established and verified against live sources
2026-07-13 — not re-scraped here since the sources (Anthropic memory docs, Diátaxis, ADRs) are
unchanged and the reasoning transfers directly: capped index + on-demand topic files mirrors
Claude Code's own memory system; Diátaxis validates the Reference/Plans split; ADR practice
validates point-in-time immutability for Plans/Audits.

## Out of scope

- Any `CLAUDE.md` restructuring beyond the one line + two link fixes.
- Fixing internal Plan-to-Plan or Plan-to-Audit cross-references (Category 2/3 principle above).
- Fixing the pre-existing dead `ibkr_core_mcp/docs/future-doc-scraper.md` link in
  `project-status.md` (cross-repo, unrelated).
- Fixing the pre-existing dead `ibkr_core_mcp/docs/live-test-log.md` reference inside
  `docs/live-test-log.md` itself (Audit-category immutable log entry).
- Renaming any moved file (no collisions exist, so no rename is needed).

## Implementation notes

- Use `git mv` for every tracked-file relocation so history follows the file; the one untracked
  file (`2026-07-10-claude-md-delink-imports.md`) uses `mv` + `git add` since it was never
  committed at its old path.
- 18 files → `docs/plans/`, 6 files → `docs/audits/`, 16 files unmoved, 1 file created
  (`docs/README.md`), 2 files created (this design + its plan), 10 files get reference fixes
  (9 docs/README-level files + 1 code file), 1 bare relative link fixed by hand.
