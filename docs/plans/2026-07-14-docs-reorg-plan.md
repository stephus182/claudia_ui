# Documentation Architecture Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `claudia_ui/docs/` per `docs/plans/2026-07-14-docs-reorg-design.md` — 16
reference files stay flat, 18 design/plan files consolidate from
`docs/superpowers/{specs,plans}/` into `docs/plans/`, 6 audit/investigation files consolidate
from `docs/` root into `docs/audits/`, a new `docs/README.md` catalog is created, and every
living-document reference to a moved path is repointed.

**Architecture:** This is a file-move task, not a code-feature task — there is no code under
test beyond two docstring lines. "Verification" steps use `git status`/`ls`/`grep` instead of
`pytest` to confirm each move landed correctly and nothing references a now-stale path, plus one
`pytest` safety-net run since Task 3 touches a `.py` docstring. All relocations use `git mv` so
history follows the file, except one untracked file (`mv` + `git add`, since it was never
committed at its old path). Internal cross-references between immutable point-in-time
Plan/Audit documents are deliberately **not** fixed (see design doc) — only living Reference
docs, `CLAUDE.md`, root `README.md`, `SECURITY.md`, and code comments get their links repointed.

**Tech Stack:** Plain `git mv`/`mv`, `grep`, `sed`, and manual markdown editing. No build step.

---

## Pre-flight

- [ ] **Step 1: Confirm `docs/` working tree is clean**

The repo has an unrelated pending change (`.claude/settings.json`) outside `docs/` — leave that
untouched; this check is scoped to `docs/` only.

```bash
cd /Users/steph/Claude_Projects/claudia_ui
git status --porcelain docs/
```

Expected: exactly one line, `?? docs/superpowers/plans/2026-07-10-claude-md-delink-imports.md`
(untracked — created this week, handled specially in Task 2). If anything else appears, stop and
ask the user before proceeding.

- [ ] **Step 2: Snapshot the current file inventory for later diffing**

```bash
git ls-files docs/ | sort > /tmp/claudia-docs-before.txt
wc -l /tmp/claudia-docs-before.txt
find docs/superpowers -type f | wc -l
```

Expected: `git ls-files` reports 41 tracked files (baseline — exact count isn't the point, this
is a diff baseline for later); `docs/superpowers` has 18 files (7 specs + 11 plans, 10 of the 11
plans tracked, 1 untracked per Step 1).

---

### Task 1: Move Audits into `docs/audits/`

**Files:**
- Move: `docs/security-audit-2026-06-12.md` → `docs/audits/security-audit-2026-06-12.md`
- Move: `docs/security-audit-2026-06-25.md` → `docs/audits/security-audit-2026-06-25.md`
- Move: `docs/test-coverage-findings-2026-06-15.md` → `docs/audits/test-coverage-findings-2026-06-15.md`
- Move: `docs/2026-07-03-agent-info-architecture-review.md` → `docs/audits/2026-07-03-agent-info-architecture-review.md`
- Move: `docs/2026-07-03-llm-best-practices-sources.md` → `docs/audits/2026-07-03-llm-best-practices-sources.md`
- Move: `docs/live-test-log.md` → `docs/audits/live-test-log.md`

`git mv` creates the destination directory automatically — no separate `mkdir` step.

- [ ] **Step 1: Move the 6 files**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
git mv docs/security-audit-2026-06-12.md docs/audits/security-audit-2026-06-12.md
git mv docs/security-audit-2026-06-25.md docs/audits/security-audit-2026-06-25.md
git mv docs/test-coverage-findings-2026-06-15.md docs/audits/test-coverage-findings-2026-06-15.md
git mv docs/2026-07-03-agent-info-architecture-review.md docs/audits/2026-07-03-agent-info-architecture-review.md
git mv docs/2026-07-03-llm-best-practices-sources.md docs/audits/2026-07-03-llm-best-practices-sources.md
git mv docs/live-test-log.md docs/audits/live-test-log.md
```

- [ ] **Step 2: Verify the moves**

```bash
ls docs/audits/ | sort
```

Expected: exactly 6 files —
```
2026-07-03-agent-info-architecture-review.md
2026-07-03-llm-best-practices-sources.md
live-test-log.md
security-audit-2026-06-12.md
security-audit-2026-06-25.md
test-coverage-findings-2026-06-15.md
```

- [ ] **Step 3: Commit**

```bash
git add -A docs/audits
git commit -m "docs: consolidate audits and investigation records into docs/audits/"
```

---

### Task 2: Move Design specs & Plans into `docs/plans/`

**Files:**
- Move: 7 `docs/superpowers/specs/*.md` files → `docs/plans/` (no rename — no collision exists)
- Move: 10 tracked `docs/superpowers/plans/*.md` files → `docs/plans/` (no rename)
- Move: 1 untracked `docs/superpowers/plans/2026-07-10-claude-md-delink-imports.md` → `docs/plans/2026-07-10-claude-md-delink-imports.md` (via `mv` + `git add`, not `git mv`)

`docs/plans/2026-07-14-docs-reorg-design.md` and this file already live at the target directory
— nothing to do for those.

- [ ] **Step 1: Move the 7 spec files**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
git mv docs/superpowers/specs/2026-06-09-claudia-ui-redesign-design.md docs/plans/2026-06-09-claudia-ui-redesign-design.md
git mv docs/superpowers/specs/2026-06-11-gdrive-sync-design.md docs/plans/2026-06-11-gdrive-sync-design.md
git mv docs/superpowers/specs/2026-07-01-scraping-rag-pipeline-design.md docs/plans/2026-07-01-scraping-rag-pipeline-design.md
git mv docs/superpowers/specs/2026-07-06-live-pnl-streaming-design.md docs/plans/2026-07-06-live-pnl-streaming-design.md
git mv docs/superpowers/specs/2026-07-07-execution-triggered-pnl-design.md docs/plans/2026-07-07-execution-triggered-pnl-design.md
git mv docs/superpowers/specs/2026-07-10-gdrive-auth-dedup-design.md docs/plans/2026-07-10-gdrive-auth-dedup-design.md
git mv docs/superpowers/specs/2026-07-10-gdrive-oauth-client-migration-design.md docs/plans/2026-07-10-gdrive-oauth-client-migration-design.md
```

- [ ] **Step 2: Move the 10 tracked plan files**

```bash
git mv docs/superpowers/plans/2026-06-09-ui-status-bar.md docs/plans/2026-06-09-ui-status-bar.md
git mv docs/superpowers/plans/2026-06-11-gdrive-sync.md docs/plans/2026-06-11-gdrive-sync.md
git mv docs/superpowers/plans/2026-06-12-test-coverage.md docs/plans/2026-06-12-test-coverage.md
git mv docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md docs/plans/2026-07-03-prompt-caching-upgrade.md
git mv docs/superpowers/plans/2026-07-06-live-pnl-streaming.md docs/plans/2026-07-06-live-pnl-streaming.md
git mv docs/superpowers/plans/2026-07-07-execution-triggered-pnl.md docs/plans/2026-07-07-execution-triggered-pnl.md
git mv docs/superpowers/plans/2026-07-08-order-cancel-modify.md docs/plans/2026-07-08-order-cancel-modify.md
git mv docs/superpowers/plans/2026-07-10-gdrive-auth-dedup.md docs/plans/2026-07-10-gdrive-auth-dedup.md
git mv docs/superpowers/plans/2026-07-10-live-test-bugfixes-handoff.md docs/plans/2026-07-10-live-test-bugfixes-handoff.md
git mv docs/superpowers/plans/2026-07-10-live-test-bugfixes.md docs/plans/2026-07-10-live-test-bugfixes.md
```

- [ ] **Step 3: Move the 1 untracked plan file**

```bash
mv docs/superpowers/plans/2026-07-10-claude-md-delink-imports.md docs/plans/2026-07-10-claude-md-delink-imports.md
git add docs/plans/2026-07-10-claude-md-delink-imports.md
```

- [ ] **Step 4: Verify the count and that no filename collided**

```bash
ls docs/plans/*.md | wc -l
find docs/superpowers -type f -not -name ".DS_Store"
```

Expected: **20** — 18 moved in Steps 1–3 + `2026-07-14-docs-reorg-design.md` +
`2026-07-14-docs-reorg-plan.md` (already in place before this task ran). The second command
prints nothing — `docs/superpowers/` holds no files left except possibly `.DS_Store`.

- [ ] **Step 5: Remove the now-empty `docs/superpowers/` tree**

```bash
git status --porcelain docs/superpowers
find docs/superpowers -type d -empty -delete 2>/dev/null
rm -f docs/superpowers/.DS_Store
find docs/superpowers -mindepth 0 2>/dev/null
```

Expected: `git status --porcelain docs/superpowers` prints nothing (any stray `.DS_Store` was
already untracked). The final `find` prints nothing — `docs/superpowers` itself is gone.

- [ ] **Step 6: Commit**

```bash
git add -A docs/plans docs/superpowers
git commit -m "docs: consolidate design specs and plans into docs/plans/"
```

---

### Task 3: Repoint living-document references to moved files

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/gdrive-sync-reference.md`
- Modify: `docs/project-status.md`
- Modify: `docs/prompt-caching-upgrade.md`
- Modify: `docs/trading-data-reference.md`
- Modify: `docs/context-loading-reference.md`
- Modify: `docs/api-reference.md`
- Modify: `claudia/execution_listener.py`

Per the design doc, internal cross-references *between* Plan/Audit documents are deliberately
left as-is (immutable point-in-time records). Only living Reference docs, the two root-level
project docs, and code comments get fixed here.

- [ ] **Step 1: Apply the substitutions**

```bash
cd /Users/steph/Claude_Projects/claudia_ui

files=(
  CLAUDE.md
  README.md
  SECURITY.md
  docs/gdrive-sync-reference.md
  docs/project-status.md
  docs/prompt-caching-upgrade.md
  docs/trading-data-reference.md
  docs/context-loading-reference.md
  docs/api-reference.md
  claudia/execution_listener.py
)

for f in "${files[@]}"; do
  sed -i '' \
    -e 's#docs/security-audit-2026-06-12\.md#docs/audits/security-audit-2026-06-12.md#g' \
    -e 's#docs/security-audit-2026-06-25\.md#docs/audits/security-audit-2026-06-25.md#g' \
    -e 's#docs/2026-07-03-agent-info-architecture-review\.md#docs/audits/2026-07-03-agent-info-architecture-review.md#g' \
    -e 's#docs/2026-07-03-llm-best-practices-sources\.md#docs/audits/2026-07-03-llm-best-practices-sources.md#g' \
    -e 's#docs/live-test-log\.md#docs/audits/live-test-log.md#g' \
    -e 's#superpowers/plans/#plans/#g' \
    -e 's#superpowers/specs/#plans/#g' \
    "$f"
done
```

Note: `sed -i ''` (empty string after `-i`) is the BSD/macOS form — do not drop the `''`, it
would otherwise treat the next `-e` as the backup-suffix argument and corrupt the edit.

- [ ] **Step 2: Fix the one bare relative link the blanket substitution can't catch**

`docs/prompt-caching-upgrade.md` links to `live-test-log.md` with a same-directory relative
link (no `docs/` prefix), which Step 1's `docs/live-test-log\.md` pattern doesn't match.

Current (verify before editing):
```bash
grep -n "live-test-log.md#run" docs/prompt-caching-upgrade.md
```
Expected: `([2026-07-03 run 1](live-test-log.md#run-2026-07-03-1))` on line 6.

Change:
```
([2026-07-03 run 1](live-test-log.md#run-2026-07-03-1))
```
to:
```
([2026-07-03 run 1](audits/live-test-log.md#run-2026-07-03-1))
```

- [ ] **Step 3: Verify no stale references remain in the 10 fixed files**

```bash
grep -n -E "docs/security-audit-2026-0[16]-2[56]\.md|docs/2026-07-03-agent-info-architecture-review\.md|docs/2026-07-03-llm-best-practices-sources\.md|docs/live-test-log\.md|superpowers/(plans|specs)/" \
  CLAUDE.md README.md SECURITY.md docs/gdrive-sync-reference.md docs/project-status.md \
  docs/prompt-caching-upgrade.md docs/trading-data-reference.md docs/context-loading-reference.md \
  docs/api-reference.md claudia/execution_listener.py
```

Expected: no output. If anything prints, a substitution was missed — fix it before continuing.

- [ ] **Step 4: Spot-check a few rewritten lines**

```bash
grep -n "docs/audits/2026-07-03-agent-info-architecture-review.md" README.md
grep -n "docs/plans/2026-07-03-prompt-caching-upgrade.md" CLAUDE.md
grep -n "docs/audits/security-audit-2026-06-12.md" SECURITY.md
grep -n "docs/plans/2026-07-10-gdrive-auth-dedup-design.md" docs/gdrive-sync-reference.md
grep -n "plans/2026-07-06-live-pnl-streaming-design.md" claudia/execution_listener.py
grep -n "audits/live-test-log.md" docs/prompt-caching-upgrade.md docs/context-loading-reference.md
```

Expected: one match each, all pointing at the new paths.

- [ ] **Step 5: Confirm the docstring-only code edit didn't touch behavior**

```bash
pytest -m "not integration" -q
```

Expected: same pass count the repo had before this plan started — these edits only touched
comment text in `claudia/execution_listener.py`, never executable strings.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md SECURITY.md docs/gdrive-sync-reference.md docs/project-status.md docs/prompt-caching-upgrade.md docs/trading-data-reference.md docs/context-loading-reference.md docs/api-reference.md claudia/execution_listener.py
git commit -m "docs: repoint living-document references to moved plans/audits"
```

---

### Task 4: Create `docs/README.md`

**Files:**
- Create: `docs/README.md`

- [ ] **Step 1: Write the file**

```markdown
# ClaudIA UI Documentation

This directory holds three kinds of documentation. See `CLAUDE.md`'s Pointers section for the
most commonly needed links; this file is the full catalog.

## Reference

Living documentation describing current behavior — read on demand, updated in place as the
code changes.

| File | Description |
| --- | --- |
| [`api-reference.md`](api-reference.md) | Anthropic/IBKR/Drive/Chainlit source-of-truth URLs; scraped-evidence convention |
| [`connectivity.md`](connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows |
| [`context-loading-reference.md`](context-loading-reference.md) | `context.md`/`principles.md` loading, hot-reload, versioning, prompt-cache mechanics |
| [`conversation-memory-reference.md`](conversation-memory-reference.md) | `claudia.db` schema — sessions, messages, decisions, doc_versions, FTS5 |
| [`env-vars-reference.md`](env-vars-reference.md) | Full environment variable reference |
| [`flex-query-setup.md`](flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`gdrive-sync-reference.md`](gdrive-sync-reference.md) | GDrive sync — folder layout, credential flow, error handling |
| [`market-calendar-reference.md`](market-calendar-reference.md) | 20-exchange market calendar, futures schedules |
| [`order-api-reference.md`](order-api-reference.md) | Full order-staging spec (Gate 1/2, immutability rule, FUT/FOP conid) |
| [`project-status.md`](project-status.md) | Living status doc — feature timeline, known gaps, live test log/plan |
| [`prompt-caching-upgrade.md`](prompt-caching-upgrade.md) | Prompt-caching implementation note — status, verified numbers, findings |
| [`startup-flow.md`](startup-flow.md) | Every phase of ClaudIA startup in order |
| [`trading-data-reference.md`](trading-data-reference.md) | Trade data architecture, Flex vs live API, P&L/execution listener |
| [`tradingview-mcp-recovery.md`](tradingview-mcp-recovery.md) | TradingView sidecar break patterns and recovery steps |
| [`tradingview-reference.md`](tradingview-reference.md) | TradingView integration — curated tools, screenshot mode |
| [`windows-compatibility.md`](windows-compatibility.md) | Windows-specific platform issues found during macOS development |

`context.md`, `principles.md`, and `versions/` are personal, git-ignored, hot-reloaded files —
not part of this catalog. See `context-loading-reference.md` for how they're loaded.

## Plans (`docs/plans/`)

Point-in-time records of what was decided and how — a design spec (`*-design.md`) captures the
why/what, a plan (plain topic name) captures the how, for both features and fixes. Filenames
carry a `YYYY-MM-DD-<topic>` prefix, so a filename sort gives chronological order. These are not
living documents — once written they are not edited to reflect later changes, including later
file moves; a later revisit gets a new dated file. Browse the directory directly rather than
looking for an index entry here.

## Audits (`docs/audits/`)

Point-in-time investigation and verification records — security audits, code audits (e.g. the
agent information-handling architecture review), a bug-finding sprint report, and the
accumulated live-test log ([`live-test-log.md`](audits/live-test-log.md)). Same treatment as
Plans: dated filenames, not retroactively edited. Browse directly rather than looking for an
index entry here.
```

- [ ] **Step 2: Verify all Reference-category links resolve**

```bash
for f in api-reference.md connectivity.md context-loading-reference.md conversation-memory-reference.md env-vars-reference.md flex-query-setup.md gdrive-sync-reference.md market-calendar-reference.md order-api-reference.md project-status.md prompt-caching-upgrade.md startup-flow.md trading-data-reference.md tradingview-mcp-recovery.md tradingview-reference.md windows-compatibility.md docs/audits/live-test-log.md; do
  path="docs/$f"
  [ "$f" = "docs/audits/live-test-log.md" ] && path="$f"
  test -f "$path" && echo "OK: $path" || echo "MISSING: $path"
done
```

Expected: every line says `OK:`.

- [ ] **Step 3: Commit**

```bash
git add docs/README.md
git commit -m "docs: add docs/README.md catalog"
```

---

### Task 5: Cross-link the catalog from root `README.md` and `CLAUDE.md`

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a row to root `README.md`'s Documentation table**

Current (after Task 3's path fixes):
```markdown
## Documentation

| File | Contents |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Developer guide: setup, env vars, architecture, hard rules |
| [`SECURITY.md`](SECURITY.md) | Security model: order barriers, threat model, audit checklist |
| [`docs/flex-query-setup.md`](docs/flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) | TradingView break patterns, recovery steps, CDP fallback |
| [`docs/connectivity.md`](docs/connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows, live test results |
| [`docs/project-status.md`](docs/project-status.md) | Feature timeline, test coverage, live test plan and log |
```

Change to (new first row after the header):
```markdown
## Documentation

| File | Contents |
|---|---|
| [`docs/README.md`](docs/README.md) | Full documentation catalog — every doc in `docs/`, categorized |
| [`CLAUDE.md`](CLAUDE.md) | Developer guide: setup, env vars, architecture, hard rules |
| [`SECURITY.md`](SECURITY.md) | Security model: order barriers, threat model, audit checklist |
| [`docs/flex-query-setup.md`](docs/flex-query-setup.md) | IBKR Flex Query setup: token, query config, backfill, ongoing sync |
| [`docs/tradingview-mcp-recovery.md`](docs/tradingview-mcp-recovery.md) | TradingView break patterns, recovery steps, CDP fallback |
| [`docs/connectivity.md`](docs/connectivity.md) | IBKR / GDrive / TradingView check logic, reconnection flows, live test results |
| [`docs/project-status.md`](docs/project-status.md) | Feature timeline, test coverage, live test plan and log |
```

- [ ] **Step 2: Add a bullet to `CLAUDE.md`'s Pointers section**

Current last line of the Pointers section:
```
- Known gaps, live test log, project status: `docs/project-status.md`
```

Change to:
```
- Known gaps, live test log, project status: `docs/project-status.md`
- Full documentation catalog (every doc in `docs/`, categorized): `docs/README.md`
```

- [ ] **Step 3: Verify**

```bash
grep -n "docs/README.md" README.md CLAUDE.md
```

Expected: one match in each file.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: link docs/README.md catalog from root README and CLAUDE.md"
```

---

### Task 6: Final verification

- [ ] **Step 1: Confirm no stale `docs/superpowers/` or moved-root-file references remain repo-wide**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
grep -rn --include="*.md" --include="*.py" "docs/superpowers/" . --exclude-dir=.git
```

Expected: no output.

- [ ] **Step 2: Confirm the two intentionally-untouched dead links are still exactly as documented (not accidentally fixed or broken further)**

```bash
grep -n "future-doc-scraper" docs/project-status.md
grep -n "ibkr_core_mcp/docs/live-test-log.md" docs/audits/live-test-log.md
```

Expected: both still print — these are pre-existing, unrelated, out-of-scope per the design
doc's "Not fixed, deliberately" section, not touched by this plan.

- [ ] **Step 3: Full test suite one more time**

```bash
pytest -m "not integration" -q
```

Expected: all green, matching the baseline pass count from Task 3 Step 5.

- [ ] **Step 4: Review the full commit sequence**

```bash
git log --oneline -6
git diff --stat HEAD~6 HEAD -- docs/ README.md CLAUDE.md SECURITY.md claudia/
```

Confirm the diffstat shows renames for every moved file (not delete+add pairs) — proof `git mv`
preserved history for the 24 tracked moves (6 audits + 18 plans, minus the 1 untracked file
which shows as a plain add).

- [ ] **Step 5: Report to user**

Summarize: 18 files → `docs/plans/`, 6 files → `docs/audits/`, 16 files unmoved, `docs/README.md`
created, 10 files got reference fixes (9 docs-level + 1 code file), root `README.md` and
`CLAUDE.md` both link the new catalog, `docs/superpowers/` removed, test suite green. Do not
push — leave that decision to the user.
