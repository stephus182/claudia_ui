# CLAUDE.md: de-link `@docs/...` references (fix eager-import bug)

## Context

`ibkr_core_mcp`'s CLAUDE.md was recently split into a lean index + `docs/*.md` reference
files, linked with bare `@docs/foo.md` syntax on the assumption that these were lazy
"pull in when relevant" pointers. That assumption was wrong: per Anthropic's current docs
(`code.claude.com/docs/en/memory`), a bare `@path` reference **is a real import** —
"imported files are expanded and loaded into context at launch," and explicitly:
"splitting into `@path` imports helps organization but does not reduce context, since
imported files load at launch." It was fixed there by backtick-wrapping every `docs/*.md`
mention so they become plain file references instead of imports — Claude then reads them
on demand via normal file tools when a task actually touches that area.

`claudia_ui`'s CLAUDE.md has the exact same bug, already present from an earlier split
session. It's structurally fine (already a lean 146-line index pointing at `docs/*.md`) —
the only problem is the same bare-`@` syntax. But the **impact here is far larger** than
in `ibkr_core_mcp`.

## Measured impact (2026-07-10, via `anthropic` SDK `count_tokens`, model `claude-opus-4-8`)

| | tokens |
|---|---|
| `CLAUDE.md` alone (raw file) | 2,910 |
| Sum of all 13 `@`-imported docs (currently loaded eagerly every session) | 72,570 |
| **Actual current per-session load** | **75,480** |
| What it should be once fixed | 2,910 |

Two files alone account for most of the waste:

| File | Tokens |
|---|---|
| `docs/project-status.md` | 32,345 |
| `docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md` | 13,352 |
| `docs/order-api-reference.md` | 4,388 |
| `docs/tradingview-mcp-recovery.md` | 4,094 |
| `docs/flex-query-setup.md` | 3,293 |
| `docs/trading-data-reference.md` | 3,676 |
| `docs/market-calendar-reference.md` | 2,035 |
| `docs/gdrive-sync-reference.md` | 1,997 |
| `docs/context-loading-reference.md` | 1,800 |
| `docs/api-reference.md` | 1,747 |
| `docs/tradingview-reference.md` | 2,158 |
| `docs/env-vars-reference.md` | 1,178 |
| `docs/conversation-memory-reference.md` | 507 |

Checked for compounding: none of these 13 files contain further bare `@` references
(Claude Code imports recurse up to 4 hops deep), so 75,480 is the actual total, not a
lower bound.

## The fix

Backtick-wrap every bare `@docs/...` reference in `CLAUDE.md` so it reads as a plain file
path, not an import. No content changes, no restructuring — this is a pure syntax fix.

**13 lines to fix** (confirmed via `grep -n '@docs' CLAUDE.md`):

```
84:    Full source table: @docs/api-reference.md
86:    Loading/versioning mechanics: @docs/context-loading-reference.md
88:    numbers: @docs/context-loading-reference.md. Design rationale and the three-round
89:    consistency review: @docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md
108:  ## Order Staging (safety-critical — summary only, full spec: @docs/order-api-reference.md)
139:  - Trade data sync (Flex vs live API, integrity checks): @docs/flex-query-setup.md and @docs/trading-data-reference.md
140:  - Market calendar (20 exchanges, futures schedules): @docs/market-calendar-reference.md
141:  - GDrive sync (folder layout, error handling): @docs/gdrive-sync-reference.md
142:  - TradingView integration (sidecar, curated tools, recovery): @docs/tradingview-reference.md and @docs/tradingview-mcp-recovery.md
143:  - Environment variables (full reference): @docs/env-vars-reference.md
144:  - Conversation memory schema: @docs/conversation-memory-reference.md
145:  - API source-of-truth URLs (IBKR, Anthropic, Drive, Chainlit, libraries): @docs/api-reference.md
146:  - Known gaps, live test log, project status: @docs/project-status.md
```

Replace each `@docs/foo.md` with `` `docs/foo.md` `` (backticks, no `@`) in place. Same
pattern already used correctly elsewhere in this same file at lines 30 and 134-135
(`` `ibkr_core_mcp/docs/tools-reference.md` ``, `` `ibkr_core_mcp/CHANGELOG.md` ``) — use
those as the reference style.

**Not in scope / do not touch:** lines 49 and 60 also contain `@docs/env-vars-reference.md`
and `@docs/tradingview-reference.md`, but both sit inside the `` ```bash ``` `` fenced code
block spanning lines 36–67 (Dev Setup section). Claude Code's import parser skips fenced
code blocks, so these two are already inert — not part of the bug. Optionally backtick-wrap
them too for defense-in-depth (so a future edit that moves this text out of the code fence
doesn't silently reintroduce the bug), but it's not required to fix the actual problem.

Consider adding a short note near the "## Pointers" heading (line 137) stating these are
plain file references, not imports — same clarifying line was added in `ibkr_core_mcp`'s
CLAUDE.md and makes the intent explicit for future editors. Optional, not required.

## Verification steps

1. `grep -n '@docs' CLAUDE.md` — after the fix, only the two inert code-fence lines (49,
   60) should remain, if left unwrapped; zero remaining if those are wrapped too.
2. Run the test suite (`pytest -m "not integration"`) — this is a docs-only change, but
   confirm nothing else regressed.
3. Recount tokens with the same `count_tokens` script used above — `CLAUDE.md` alone
   should be ~2,910 tokens (± the note-line addition), and that number is now the *real*
   per-session load since the 13 docs no longer auto-import.
4. `git status --short` — only `CLAUDE.md` should be modified.
5. Report before/after: 75,480 → ~2,910 tokens per session (~96% reduction) — this is the
   number that matters, since unlike `ibkr_core_mcp` no other content is being moved
   around, it's a pure bug fix recovering tokens that were always meant to be on-demand.

## Out of scope

- No restructuring of `CLAUDE.md`'s content or the `docs/*.md` files themselves — this
  repo's CLAUDE.md is already a lean, well-organized index. This is a syntax-only fix.
- No changes to `ibkr_core_mcp/docs/tools-reference.md` or `ibkr_core_mcp/CHANGELOG.md`
  references (lines 30, 134-135) — already correct plain references, not imports.
