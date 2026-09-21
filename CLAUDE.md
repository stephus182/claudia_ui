# ClaudIA UI — Developer Guide

ClaudIA is a Panel-based trading assistant chatbot that connects to Interactive Brokers via `ibkr_core_mcp`. It provides conversational access to IBKR data, backtesting, technical analysis, TradingView integration, an external candlestick chart pane, a live account dashboard (KPI strip · positions · working orders · realised P&L), and human-confirmed order staging. (It was migrated from Chainlit to Panel — see `docs/plans/2026-07-22-panel-migration.md`.)

---

## Architecture

```
Panel UI (localhost:8001 — native pn.serve Tornado, no FastAPI/uvicorn)
    ↓
claudia/panel_app.py        — pn.serve entry: session lifecycle, the reconnect coroutines, layout root
claudia/panel_system_log.py — System log: collapsed card + read-only feed for session-level events (not the chat)
claudia/panel_action_bar.py — action bar: IBKR/TradingView/Drive reconnect buttons lit by ConnectivityChecker
claudia/panel_sink.py       — PanelMessageSink: agent output → pn.chat.ChatInterface (MessageSink protocol)
claudia/panel_order_flow.py — order/cancel/modify proposal buttons → order_flow.py cores
claudia/panel_pinescript.py — Pine-fence copy (real clipboard) / inject buttons
                              (matches pine | pinescript | pine-script, any case)
claudia/panel_chart.py      — external HoloViews candlestick chart pane (STK, cache-backed)
claudia/panel_theme.py      — session theme (CLAUDIA_THEME + ?theme=), user label, ClaudIA's avatar
claudia/dashboard_data.py   — live dashboard: pure data (ledger, positions, realised windows), no panel import
claudia/dashboard_poller.py — live dashboard: process-wide 15s poller caching one DashboardSnapshot
claudia/panel_dashboard.py  — live dashboard: KPI strip + Tabs(Chart/Positions/Orders/P&L), no IBKR and no SQL
claudia/agent.py            — Anthropic SDK streaming loop, tool routing, prompt caching (UI-agnostic via MessageSink)
claudia/proposal_tools.py   — strict-schema propose_order/propose_cancel/propose_modify declarations (no execution)
claudia/message_sink.py     — MessageSink / ToolStepHandle protocols (the UI-decoupling seam)
claudia/order_flow.py       — framework-agnostic order-execution cores → ibkr_core_mcp biometric gates
claudia/opening_status.py   — UI-free opening-status builders (session state + trade line; no account figures)
claudia/briefing.py         — startup briefing: expiring positions + today's exchange closures,
                              pure builders, no network (section state is Ready/Degraded/Unavailable
                              so a failed read can never render as "nothing today")
claudia/flex_sync.py        — session-start dataset validation + the "did this pull change anything" gate
claudia/context_loader.py   — docs/context.md + docs/principles.md → system prompt
claudia/conversation_store.py — SQLite: sessions, messages, decisions, doc_versions
claudia/execution_listener.py — WebSocket execution/P&L capture, live-ledger fallback; since
                              2026-09-04 also the fill subscription: every execution reaches each
                              session as an IBKR-authored chat message + log toast + operator note
claudia/gdrive_sync.py      — GDriveSync: download claudia.db at start / upload at stop
claudia/session_reporter.py — auto-generated Markdown session report (tool calls, decisions)
claudia/status.py           — ConnectivityChecker: IBKR/GDrive/TV polling, TCP health
claudia/tradingview.py      — tradingview-mcp sidecar + CDP health + TradingViewBridge
                              (execute() post-processes every result before the model sees it)
    ↓                               ↓
ibkr_core_mcp               tradingview-mcp (Node.js, stdio)
(local editable install)            ↓
    ↓                       TradingView Desktop (CDP, localhost:9222)
IBKR Client Portal Gateway
(Docker, localhost:5055)
```

`ibkr_core_mcp` is a direct Python import, not an MCP server — `ClaudeToolkit` tools drop
straight into the Anthropic SDK `tools=` parameter. TradingView tools come from a Node.js
sidecar. Full tool catalog: `ibkr_core_mcp/docs/tools-reference.md` (44 tools).

---

## Dev Setup

```bash
# 1. Clone and enter the project
cd /Users/steph/Claude_Projects/claudia_ui

# 2. Create venv
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Install claudia_ui (ibkr_core_mcp comes with it, from PyPI)
pip install -e ".[dev]"
git config core.hooksPath .githooks   # the four CI gates as a pre-push hook (see Testing)
#
# ibkr_core_mcp is a DECLARED DEPENDENCY since 2026-09-19 — `ibkr-core-mcp>=2.0.1,<3` in
# pyproject.toml, resolved from https://pypi.org/project/ibkr-core-mcp/ (first release
# 2.0.1, 2026-09-19, Trusted Publishing). It is no longer a sibling checkout you install by
# hand, and CI no longer checks it out. The one release this repository is *supported*
# against is `core-ref.txt`; the floor above is a compatibility range, a different claim,
# and `tests/security/test_cross_repo_contract.py` holds the two together.
#
# THE DEVELOPER OVERRIDE — working on both repositories at once. Add, after the line above:
#
#   pip install -e "../ibkr_core_mcp[scraper]" --config-settings editable_mode=strict
#
# It is the same distribution name, so pip uninstalls the PyPI wheel and installs the
# editable tree in its place; `python -c "import ibkr_core_mcp, pathlib;
# print(pathlib.Path(ibkr_core_mcp.__file__))"` then prints a path under
# ../ibkr_core_mcp/build/__editable__…/ instead of one under .venv/lib/…/site-packages/
# (both measured 2026-09-19). Re-running `pip install -e ".[dev]"` afterwards does NOT undo
# it: the editable version satisfies the floor, and pip does not upgrade a satisfied
# requirement. To go back, `pip install --force-reinstall "ibkr-core-mcp==$(grep -v '^#'
# core-ref.txt | grep -v '^$' | head -1)"`.
#
# While the override is in place, one assertion — "the installed core is the supported
# release" — compares the CHECKOUT's declared version with the pin. It passes while the two
# agree (both 2.0.1 on 2026-09-19) and fails the moment the core bumps its version, which is
# the signal working as intended rather than a problem to work around. Set
# `CLAUDIA_CORE_UNPINNED=1` to skip that one assertion — the whole rest of the contract still
# runs — exactly as the forward-compat CI lane does, unconditionally, so that lane cannot go
# red on the day the core bumps.
#
# Everything below applies to the OVERRIDE ONLY. A plain PyPI install is a wheel: it cannot
# go stale, needs no strict mode, and `stale_modules()` returns [] for it by construction.
#
# [scraper] is NOT optional in practice — it is what installs crawl4ai, and without it all
# four web tools (fetch_page, crawl_site, search_site, firecrawl_search) are dark. Every
# scraper import is lazy, so ClaudIA starts perfectly and each tool fails only when the
# model calls it. This exact omission already shipped once: it was found in the environment
# on 2026-07-28, fixed there, and left in these instructions, so every clean setup since
# would have reintroduced it. Corrected 2026-07-30. Fixing the environment is not fixing
# the bug — the instructions are what the next install actually runs.
#
# strict mode required for mypy — the default "lazy" editable install registers a
# meta-path finder mypy's static import resolution can't see. Re-confirmed 2026-07-30
# against mypy 2.3.0: a non-strict install produces 14 "Cannot find implementation or
# library stub for module named ibkr_core_mcp" errors, despite the package shipping
# py.typed and importing fine at runtime. So strict is not optional.
#
# THE COST, and it has bitten three times: strict mode snapshots a symlink farm under
# build/__editable__…/ of the modules that existed AT INSTALL TIME. Add, rename or
# delete a module in ibkr_core_mcp and this project keeps resolving the old set.
# Because every scraper import is lazy, ClaudIA starts perfectly and fails only when
# the affected tool is called. RE-RUN THIS COMMAND after any module is added, renamed
# or removed — not just after a new tool is added.
#
# Guarded since 2026-07-30: claudia/install_check.py compares the snapshot against the
# real source tree. panel_app logs a loud ERROR naming the modules and this command at
# startup, and tests/test_install_check.py fails in the ordinary pytest run. You should
# never have to diagnose this from a bare ModuleNotFoundError again.
#
# WHICH install is in this venv? `python -m claudia.install_check` says so — `editable`
# (the override), `index` (the PyPI release), `directory` (a frozen copy) or `unknown`.
# Read from PEP 610 metadata, not from the version, because the checkout and the release
# can declare the SAME version (both 2.0.1 on 2026-09-19, nine commits apart). Add
# `--require-editable` to make it exit non-zero on anything but the override; that is what
# CI's forward-compat lane runs, so a silently failed override cannot leave it testing the
# released core while reporting forward compatibility.

# 4. Copy and fill in env vars
cp .env.example .env
# Edit .env — minimum required: ANTHROPIC_API_KEY. Full var reference: @docs/env-vars-reference.md

# 5. Personal documents (git-ignored; define ClaudIA's persona + trading rules)
# If GOOGLE_DRIVE_FOLDER_ID is set, both download from Drive automatically at session
# start — nothing to create. Otherwise create them by hand:
touch docs/context.md docs/principles.md   # then write persona / trading rules
chmod 600 docs/context.md docs/principles.md
# Loading/versioning mechanics: docs/context-loading-reference.md

# 6. TradingView sidecar (optional — one-time install)
git clone https://github.com/tradesdontlie/tradingview-mcp ~/.tradingview-mcp
cd ~/.tradingview-mcp && npm install && cd -   # pure JS — no build step
./scripts/archive-tv-mcp.sh   # snapshot the working version to vendor/
# Full sidecar details, curated tool list, recovery: @docs/tradingview-reference.md

# 7. Run ClaudIA
./start-claudia.sh            # recommended: IBKR gateway + ClaudIA
# or:
python -m claudia.panel_app   # ClaudIA only (the IBKR button under the chat starts the gateway)
# → Open http://localhost:8001
```

## Testing

```bash
source .venv/bin/activate   # every command below needs it — a bare `pytest` resolves to
                            # system Python and dies on `ModuleNotFoundError: panel`
pytest        # full suite — all unit, no IBKR gateway needed (2,052 collected 2026-09-20
              # in the main checkout; `pytest --collect-only -q | tail -1` reports it)
pytest tests/security   # the structural invariants alone, ~3s (also part of the full run)
ruff check . && ruff format --check . && mypy   # lint, format, type gates — all must be clean
# The ruff rule set (`[tool.ruff.lint]` in pyproject.toml) is identical to ibkr_core_mcp's,
# aligned 2026-09-08 — change it in both repos or in neither.
#
# No unit test opens a socket, resolves a name, or sees a real secret: pytest-socket is armed
# in pytest_configure and `load_dotenv` is neutralised there too, because importing panel_app
# loads `.env` at module scope and a fixture would be far too late. Two exemption lists in
# tests/conftest.py, each with its reason, each held against collection by a staleness test.

# Opt-in only — bills real Anthropic API calls, skipped by default (4 tests):
CLAUDIA_LIVE_SCHEMA_CHECK=1 pytest -m live_api
```

CI runs those four and **two more** since 2026-09-14: `pip-audit` over the resolved tree
(with `ibkr-core-mcp[scraper]` at the pinned release, because that is what a real install
carries, and because 19
packages here — the whole Panel/Bokeh/Tornado stack — are audited by no other repository) and
`gitleaks` over the pushed range. Both block. A no-fix dependency finding goes in
`security/pip-audit-ignores.txt` with a reason and a re-check date; a finding with a fix bumps
the floor instead. **If the secret scan is red, read the log before assuming a leak** — a
failure to run the scanner looks identical to a finding, and that is exactly what happened on
its first run.

**`ibkr_core_mcp` is resolved from `core-ref.txt`, and CI has two lanes** (2026-09-14;
PyPI since 2026-09-19). That file holds one exact released version — the release this
repository is *supported* against — and it is the only file here allowed to name one
(enforced by `tests/security/test_cross_repo_contract.py`, which also asserts that the
**installed** distribution version equals it). The blocking `test` and `dependency-audit`
jobs install exactly that release from PyPI, so a green commit is reproducible and a core
release published between two pushes here cannot silently become the tested core. A separate
`forward-compat` job still **checks out** core `main` — that is the one lane a checkout is
right for, since its whole subject is unreleased changes — installs it editable over the
PyPI copy, and runs the seam tests only (`tests/security/`, `tests/test_order_flow.py`,
`tests/test_install_check.py`) with `CLAUDIA_CORE_UNPINNED=1`, which switches off the
supported-release assertion and nothing else. It is **informational** (`continue-on-error`)
on purpose — a push in the other repository must not be able to make this one un-mergeable,
and the value is seeing the drift days before an upgrade. To move the supported release:
change the version, check the published artifact against its git tag (core-ref.txt records
how), run the whole gate line locally against it, and say in the commit message what changed
and why the bump is safe. **The file held a commit SHA until 2026-09-19**; the reasoning for
the change, and why a PyPI version is at least as immutable as a SHA, is in its own comment
block.

The original four gates are also `.github/workflows/ci.yml`, step for step the same file as
ibkr_core_mcp's (aligned 2026-09-08): every push and PR to `main` runs them on Ubuntu for
Python 3.11 and 3.12, with ibkr_core_mcp installed from PyPI at the release in
`core-ref.txt`. `mypy` runs in **strict mode** over `tests/` as well as `claudia/`
(both since 2026-09-08, the same configuration as ibkr_core_mcp; the fetchers in
`dashboard_data` take read-only Protocols, so a test double type-checks without casts).

**Run the whole line locally before pushing, in CI's order.** CI stops at its first failing
step, so a red `ruff format --check` hides whatever mypy or pytest would have said — that is
how ibkr_core_mcp run 34082479743 masked two real mypy errors on 2026-09-07. `ruff format` is
a gate since that date; the whole repo was reformatted in one dedicated commit first. The
same four commands are `.githooks/pre-push`, which refuses a push that would go red — enable
it once per clone with `git config core.hooksPath .githooks` (Dev Setup step 3); `git push
--no-verify` bypasses it on purpose. Branch protection cannot do this job for a direct-push
workflow: a required status check rejects every push whose commit has not already passed CI,
which a direct push never has. CI skips
more tests than a local run does (29 against 4 on 2026-09-08): the extra skips are the suites
keyed on git-ignored account fixtures and the local conversation corpus, absent from a fresh
clone by design — not a regression. Three git-ignored personal documents are absent there
too, which is why the docs gate treats a git-ignored pointer as a local one.
**A `git worktree` has the same gap, for the same reason** — `git worktree add` does not copy
git-ignored files, so a suite run inside one silently skips those suites. Measured 2026-09-15
on the startup-briefing branch: **32 skips in the worktree against 7 in the main checkout**,
from an identical 2,027 collected. Those 25 tests only ran, and passed, after the merge.
**Verify a merge from the main checkout, never from the worktree the work was built in.**

**The `live_api` marker exists because local validation cannot prove API acceptance.** During
the 2026-07-27 guardrail work, three separate defects passed a docs read *and* a green suite
and would each have returned a 400 on every request: `exclusiveMinimum` and
`additionalProperties: true` in the tool schemas, and a `role: "system"` message placed after
an assistant turn. `jsonschema` validates that our schema is valid JSON Schema; it says
nothing about what the endpoint accepts. **Probe the live API before adding any JSON Schema
keyword or changing a message-role placement** — the published support list has been wrong in
both directions (`minLength` is accepted despite being documented as unsupported). Evidence
table: `claudia/proposal_tools.py` module docstring.

No test carries the `integration` marker (registered in `pyproject.toml`, meaning "live IBKR
gateway") — live IBKR verification is done manually and recorded in
`docs/project-status.md` § Live Test Log. ibkr_core_mcp's own integration suite lives in
that repo.

---

## Conventions

- **API Docs First**: never assume endpoint behavior, error codes, or field names from
  memory. Always `WebFetch` the official doc before writing any error message, fix, or
  diagnosis. Cite the source URL in the error string and commit message. This rule exists
  because two production bugs went undetected for months and were caught instantly once
  docs were checked. Full source table: `docs/api-reference.md`
- **All plans live in `docs/plans/`, and the directory is git-ignored** (dated
  `YYYY-MM-DD-<topic>.md` filenames — designs, implementation plans, and workflow-executed
  plans alike). Plans are personal working documents: kept local + Google Drive, never
  committed (user rule 2026-07-24). Never create a `docs/superpowers/` directory — this
  overrides any skill's default plan location. (The 2026-07-14 docs reorg dissolved
  `docs/superpowers/` into `docs/plans/`; a skill default recreated it on 2026-07-22 and it
  was re-dissolved on 2026-07-24, same day the directory went git-ignored.) `docs/plans/...`
  paths in tracked docs are pointers into the local archive, not repo files.
- `context.md` / `principles.md` define ClaudIA's persona and trading rules. Hot-reloaded
  mid-session, never commit either file. Loading/versioning mechanics: `docs/context-loading-reference.md`
- Prompt caching uses 3 breakpoints (tools → system → messages). Mechanics and live-verified
  numbers: `docs/context-loading-reference.md`. Design rationale and the three-round
  consistency review: `docs/plans/2026-07-03-prompt-caching-upgrade.md`

---

## Hard Rules for Developers

These rules must never be violated when extending ClaudIA:

1. **Never add a tool that calls `place_order`, `modify_order`, `cancel_order`, or
   `reply_order`.** *Proposing* is a tool call (`propose_order` / `propose_cancel` /
   `propose_modify` — schema-validated, local handlers, no IBKR reachability). *Staging* is
   a UI-layer action triggered by a physical button click. The rule forbids the second as an
   LLM capability, not the first.
2. **Never log or expose `ANTHROPIC_API_KEY`** in UI output, logs, or error messages.
3. **Never modify the hardcoded safety block** in `claudia/agent.py` to weaken constraints.
4. **Never inject conversation history directly into the system prompt.** History must be
   added as `role: user/assistant` message objects to prevent prompt injection.
5. **ibkr_core_mcp is read-only from claudia_ui's perspective.** Never bypass `ClaudeToolkit`
   to call `IBKRClient` directly from within an LLM tool handler.

---

## Order Staging (safety-critical — summary only, full spec: `docs/order-api-reference.md`)

ClaudIA **cannot** place, modify, or cancel orders autonomously:
1. ClaudIA calls `propose_order` (or `propose_cancel` / `propose_modify`) — a `strict: true`
   tool declared in `claudia/proposal_tools.py`. It reaches nothing: the handler records the
   proposal and returns a `tool_result`. There is no text format for a proposal.
2. `agent.py` hands the recorded `tool_use.input` to the `MessageSink`
   (`send_order_proposal`); `PanelMessageSink` routes it to
   `panel_order_flow.render_order_proposal()` → a Panel message with a
   **"Stage this order"** button.
3. Click → `panel_order_flow`'s handler → `order_flow._execute_staged_order_core()` →
   **Gate 1** (Touch ID — **once per order write**, as IBKR Mobile and TWS ask once per
   placement, modification or cancellation; IBKR's precaution replies validate through their
   own dialogs on the write's `OrderWriteAuthorization`, 2026-09-11) → **Gate 2** (AppKit
   dialog, values in bold, green/red banner by side,
   **SEND TO IBKR** button, 60s auto-cancel, Return key disabled).
4. `IBKRClient.place_order()` fires only after both gates pass.

- **Order parameters are immutable**: ClaudIA must use the user's exact values (symbol,
  action, quantity, price, order type, TIF). No rounding or "helpful" adjustment. A risky
  parameter gets a text warning, never a silent change — changing a parameter requires
  explicit user approval in a follow-up message. Enforced in `claudia/agent.py` system
  prompt and in memory (`feedback-order-parameter-immutability.md`).
- **`outside_rth`** (2026-09-04): IBKR simulates stops on US futures and triggers them only in
  regular trading hours unless the `outsideRTH` attribute is set. The proposal carries it as a
  nullable boolean — `null` sends nothing, the user's `true`/`false` goes verbatim — on
  `propose_modify` too, and it is shown in the approval text, the Gate 2 dialog and the Orders
  tab (`—` there = not reported by IBKR, never "No"). Sources: `docs/order-api-reference.md`
  § Stop orders on US futures.
- **Display-only keys** (2026-09-10): `_`-prefixed keys on an order body (`_companyName`,
  `_multiplier`, `_multiplier_unknown`, `_currency`, `_changes`, `_current_description`) exist
  for Gate 2 only and are stripped by `client.py` before the POST on place **and** modify. The
  three dialogs draw from one typed row builder (`order_confirm._order_rows`); the reply chain
  IBKR sends before accepting a write is persisted as `ibkr_replies` on the decision row — the
  store no longer keeps only the terminal response. Since 2026-09-11 a **stock** proposal
  carries `_companyName` and `_currency` too (gap #49: a share price is money, so it shows its
  ISO code; a future's price is index points and shows none), and **every outcome after the
  button leaves a decision row** — `trade_refused` / `modify_refused` / `cancel_refused` with
  the stage, the reason and the reply log as far as it got, `*_rejected` with IBKR's payload,
  `*_dispatched_unverified` when the write landed and the reporting failed (gap #50: a DO NOT
  SEND used to leave only `trade_proposed`, and a declined precaution lost its record).
- **Attached profit taker / bracket orders (2026-09-06, reviewed 2026-09-08): supported by the
  Web API, not yet expressible here.** IBKR takes a bracket as one request — an `orders` array
  where the parent carries `cOID` and each child `parentId` equal to it — and holds the child
  until the parent fills. Every layer of this stack carries one ticket (proposal schema, order
  body, the ibkr_core_mcp client, Gate 2, read-back), so it is Known Gaps #36 with a plan in
  `docs/plans/` (build deferred). Documented 2026-09-08: the response is one entry per ticket and
  the reply chain is per ticket, **index-aligned** — `place_order_and_confirm` answers the first
  entry only, which is right for one ticket and would drop a bracket child's reply. **Never
  approximate it with two independent proposals** (user rule 2026-09-07): a standalone
  opposite-side limit is live immediately and can open the wrong position. Finding and sources:
  `docs/order-api-reference.md` § Attached profit taker.
- Modify requests require the **full original order**, not a diff (IBKR API requirement).
  `propose_modify` carries the replacement order in its top-level fields plus a `changes`
  array of `{field, previous_value}` objects, used only to render the before/after diff.
- **Placement resolves no symbols. Every sec_type except FUT requires a `conid` already in
  the proposal** (`order_flow._needs_conid_text`, 2026-08-05). The order path used to fall
  back to `search_contract` → `contracts[0]`, which is `/iserver/secdef/search`: no `isUS`,
  no currency, and an undocumented result order, so `contracts[0]` for IGV is the *Mexican*
  listing — the defect ibkr_core_mcp had already removed from every read path. FUT is the
  one exception, resolved by front month via `get_futures` — the earliest contract **still
  tradeable**, decided by `ltd` (gap #58, 2026-09-20). "Lowest `expirationDate`" was the rule
  until then and was not safe: IBKR keeps returning a contract after its last trade date, so a
  bare root resolved to an expired one for days after each roll.
  This costs nothing: the model gets its conid from `get_market_snapshot`/`preview_order`,
  which route through the authoritative resolver, and **every real placement proposal since
  2026-07-10 already carried one** (measured over the full order history 2026-08-05). Do not
  "restore" symbol resolution here — re-implementing it would put a second, drifting
  definition next to the authoritative one.
- `strict: true` enforces types, `enum`s, required keys and closed objects at the API
  boundary. **Seven** guarantees it cannot express are checked by `_proposal_defect()` in
  `claudia/agent.py`: positive quantity, non-blank `symbol`, non-blank `order_id`, no
  duplicate `changes` entries, `outside_rth` strictly boolean-or-null, every present price a
  finite non-zero number, and a priced `order_type` carrying the price it is priced by. A
  defective proposal is **rejected whole and never repaired**: the model gets an honest
  `tool_result` saying no button was created. The last two were added 2026-09-14 — a schema
  can say "number or null" but not "null only when the type is MKT", and `number` admits NaN
  and zero, which are not prices. Negatives stay legal: crude has printed below zero.
- **Narrated actions (2026-09-11):** a turn that ran no tool and still claims a tool result
  or an order action is withdrawn before display and retried once (`_RETRIES_PER_TURN`,
  `tool_choice: any` on the first request where the model is probed for it); a contradicted
  turn is never replayed as the model's words again (`message_withdrawals`); `propose_modify`
  is refused unless `get_order_status` ran in the same turn; refused / rejected / unverified
  clicks replay on the operator channel. Mechanism and measurements:
  `docs/agent-behavior-reference.md` §4d.

---

## ibkr_core_mcp Dependency

A PyPI dependency (`ibkr-core-mcp>=2.0.1,<3`), pinned for CI to the release in
`core-ref.txt`; `pip install -e ".[dev]"` brings it in. Dev Setup step 3 above documents the
editable override for working on both repositories at once, and the re-install that override
needs after ibkr_core_mcp adds, renames or removes a module — a PyPI wheel never needs it. No
Panel restart needed for tool definition changes; restart required for Python module
changes. Full tool catalog (40 core + 4 optional web-scraper = 44 total, verified against
`TOOL_DEFINITIONS` 2026-07-30): `ibkr_core_mcp/docs/tools-reference.md` — check there
before adding/debugging a tool. Recent additions log: `ibkr_core_mcp/CHANGELOG.md`.

`self._all_tools` in `claudia/agent.py` is **not** just that catalog: it is the toolkit's 44,
plus the TradingView extras when the sidecar is up (17 curated), plus 5 local utility tools
(`_LOCAL_TOOLS`) and the 3 `PROPOSAL_TOOLS`, both declared in claudia_ui. The proposal tools
are appended last so the tools cache breakpoint on the final entry stays stable.

No extras (e.g. `[server]`) are needed for the install above; `[scraper]` is the one that
matters, and it is not pulled in by the dependency — see Dev Setup step 3. `websockets` — the sole
runtime dependency of `IBKRWebSocket`, which `claudia/execution_listener.py` uses
unconditionally for live P&L/execution tracking — is a base dependency of ibkr_core_mcp, not
gated behind an extra. (It briefly wasn't: a bare install used to leave `websockets` missing
and `ExecutionListener` would silently retry-loop forever on `ModuleNotFoundError`. Fixed
ibkr_core_mcp-side by moving `websockets` out of `[server]` into base `dependencies`, since
`IBKRWebSocket`/`AlertManager` are core public API, not server-only.)

### Release batching — core changes accumulate into ONE release (window opened 2026-09-21)

**The core is deliberately unreleased ahead of PyPI right now.** `main` in `../ibkr_core_mcp`
is 21 commits past its `v2.0.1` tag (measured 2026-09-21), all pushed, none released on
purpose. They ship as a single `2.1.0` once ClaudIA's current round of work settles — not
incrementally, because small changes driven from this repository would otherwise cost a
release each. This is Keep a Changelog's `[Unreleased]` section used for exactly what it is
for — "Keep an `Unreleased` section at the top to track upcoming changes … At release time,
you can move the `Unreleased` section changes into a new release version section"
(https://keepachangelog.com/en/1.1.0/) — and the core's CHANGELOG already declares that format.

**Which core you are actually running.** The developer override (Dev Setup step 3) is in
place, so this venv imports core `main`, not 2.0.1. `pip show ibkr-core-mcp` reports `2.0.1`,
which is the *checkout's declared version*, not the code — do not read it as the answer. Ask
`python -m claudia.install_check`, which reads PEP 610 provenance and prints
`ibkr-core-mcp install origin: editable` (measured 2026-09-21); that is precisely the case it
was built for. The supported-release assertion passes unmodified in this state
(`pytest tests/security/test_cross_repo_contract.py` → 18 passed, 2026-09-21), so
**`CLAUDIA_CORE_UNPINNED` is not needed and must not be set.**

**The one API that will bite.** The blocking `test` lane installs the release named in
`core-ref.txt` from PyPI while you develop against `main`. **Six** public names exist on main and
not in 2.0.1, all of them the bracket seam (Known Gaps #36) —
**`IBKRClient.get_bracket_preview`**, **`IBKRClient.place_bracket_and_confirm`**,
**`order_confirm.confirm_bracket_dialog`**, and from the 2026-09-21 live session
**`client.pair_bracket_response`**, **`client.BracketPairing`** and **`BracketPairing.ok`** —
re-measured 2026-09-21 by walking every top-level function, class and public method in
`ibkr_core_mcp/` at tag `v2.0.1` and at `main`: 317 public names against 323, **none removed**.
`__all__` is unchanged: the `IBKRClient` methods ride on a class 2.0.1 already exported, and
neither `confirm_bracket_dialog` nor the pairing pair is exported from
`ibkr_core_mcp/__init__.py` — import them from `ibkr_core_mcp.client` / `.order_confirm`.
`TOOL_DEFINITIONS` is unchanged at **44** entries — the bracket seam is reachable from the UI
layer and from no tool the model can call — and no module was added (29 `.py` files at the tag,
29 on main), so the strict editable install does **not** need re-running for any of them.
**Corrected 2026-09-21: this sentence said 48**, which never matched the 44 stated in § Pointers
below; both counts are now measured (`TOOL_DEFINITIONS` is a list literal — count it at each ref
with AST, not by hand). The "unchanged" half was right. Everything else alters the *behaviour* of APIs
2.0.1 already has — the front-month `ltd` rule and the `extOperator` removal from
`preview_order` — so those shapes are safe to call, but the 2.0.1 lane will not carry the fixed
behaviour. Code here that calls **any of these six** names **passes locally and fails the
blocking lane, and that is the lane working correctly.** Keep such work on a branch, do not merge it, and report it as blocked on core
2.1.0. Re-measure rather than incrementing this number: the command is in
`docs/plans/2026-09-07-attached-profit-taker-bracket-plan.md` § Phase 1, and a count edited by
hand is how the test-count claim drifted twice.

**While the window is open, do not:**

- change `core-ref.txt` — it stays `2.0.1` until 2.1.0 is live on PyPI;
- tag, release, or bump the version of `../ibkr_core_mcp`;
- loosen `ibkr-core-mcp>=2.0.1,<3`, add `--pre`, or repoint the dependency at a git ref;
- add `continue-on-error` to the `test` job, set `CLAUDIA_CORE_UNPINNED=1`, or skip
  `tests/security/test_cross_repo_contract.py`;
- merge to `main` with CI red.

Nothing here can publish by accident. `publish.yml` triggers only on `release: [published]`
and `workflow_dispatch` (TestPyPI only), and the `pypi` environment requires the owner's
manual approval (`required reviewer = the owner; deployment tag rule v*`). A push, a merge,
even a pushed tag, publishes nothing.

**When ClaudIA needs a core change:** make it on core `main`; add an entry under
`## [Unreleased]` in the core's CHANGELOG — that entry *is* the 2.1.0 release note, written
while you still remember why; run the core's four gates **bare, unpiped, as four separate
commands** (`ruff check .`, `ruff format --check .`, `mypy`, `pytest -m "not integration"`);
commit and push. Do not tag. If you added, renamed or removed a module, re-run the strict
editable install from Dev Setup step 3 or this project keeps resolving the old set.

**Closing the window is the operator's step, in this order:** core `[Unreleased]` →
`## [2.1.0] — <date>`, `pyproject.toml` version, four gates, tag, GitHub Release, approve the
`pypi` environment, verify the install in a fresh venv, then move `core-ref.txt` here with a
commit message saying what changed in the core and why the bump is safe.


## Pointers

Plain file references below, not `@import`s — read on demand via normal file tools, not
loaded into every session's context automatically. Compliant with the official Claude Code
memory docs (verified 2026-07-10, https://code.claude.com/docs/en/memory): a bare `@path` is
a real import ("expanded and loaded into context at launch"); backtick-wrapping keeps it a
literal path instead. See `docs/plans/2026-07-10-claude-md-delink-imports.md` for
the fix that established this (75,480 → 2,910 tokens/session).

- **Security architecture** (the living design — read before changing anything a safety
  property rests on): `docs/security-architecture.md`. Principals and what each is *not*
  trusted for, the trust-boundary map, the **thirteen invariants** with the test that fails when
  each stops being true and an honest BUILT/PARTIAL/OPEN status per row, a dated decision log,
  and the known limits stated plainly rather than implied. The control inventory and
  vulnerability reporting are `SECURITY.md`; point-in-time evidence is `docs/audits/`.
  **Facts the core owns are pointed at, never restated here** — a copied gate policy was wrong
  for three days after ibkr_core_mcp changed, and the 2026-09-13 audit found six such stale
  claims. The change recipes in § 10 are the short version of what a new tool, a new UI
  surface, a new button or a new core import each have to do.
  **One item there is a running-the-app rule, not a coding one** (§ 9, 2026-09-14): web
  research and live account data are kept in **separate ClaudIA sessions** — each browser tab
  on `localhost:8001` is an independent session — because a hostile page can steer the model
  into fetching a URL that carries account figures in its query string, and every public host
  is allowed by design. Nothing enforces the separation; § 9 records the three ways it leaks
  anyway (a fill reaches *every* open session) and the two gates that were designed and
  deliberately not built.
- Connectivity (IBKR/GDrive/TV status lights, check logic, reconnection flows): `docs/connectivity.md`.
  Since 2026-09-03 the lights are the colours of the action bar's buttons under the chat, and a
  click **reconnects** (IBKR through the session owner's read-only pre-flight, never a forced
  re-login); session-level events go to the collapsed **System log** card, not the chat —
  routing rule and button table in `docs/panel/ui-customisation-reference.md` §2.6.
  **Before opening the IBKR login page — from a script, a button, or by hand — run
  `python -m claudia.gateway_preflight`** (read-only: two GETs, never a write). Only one
  brokerage session exists per username across Client Portal, TWS and IBKR Mobile, so a
  needless re-login is what escalates into the IB Key challenge/response, and some login
  failures *cannot* be fixed by retrying. The case that cost days on 2026-08-05: the gateway
  held an SSO session issued to **IBKR Mobile**, so it could not authenticate as itself —
  visible only in `/sso/validate`'s `CLIENT_APP`, while `/tickle` showed `userId` populated,
  `ssoExpires` renewing and `competing` *false*. `POST /logout` could not clear it (three
  ticklers renewed it every 60s); `docker restart` could. Recovery: `./scripts/gateway-reset.sh`,
  which refuses to run against a healthy session. Full diagnosis + verdict table:
  `docs/connectivity.md` § A borrowed session / § Runbook
- Panel implementation (serving model, session lifecycle, MessageSink seam, widget gotchas,
  headless button testing): `docs/panel/panel-reference.md`
- **UI customisation** (what is set and how to change it — theme default + per-tab URL
  override, ClaudIA's avatar, user label, Send-only footer, no reaction icons; the costed menu
  of next easy changes; phase-2 candidates): `docs/panel/ui-customisation-reference.md`.
  The theme is set **per session**, never on `pn.extension()` — a global theme silences the
  `?theme=` override (Panel reads the global slot first). Panel's only built-in theme switch
  (Fast template) is a page reload, i.e. a new ClaudIA session — do not add it as a "toggle".
- Panel UI design & styling (no-styling baseline, shadow-DOM constraint, scraped styling
  surface, proposed restyle direction): `docs/panel/ui-design-reference.md`
- Panel component model (object taxonomy, the real class hierarchy, the Param foundation,
  the four interactivity APIs and how they rank, and the four routes to building a component
  of our own): `docs/panel/component-model-reference.md`
- Panel data surfaces — Tabulator/Number/ECharts, the `pn.extension()` gate, side windows,
  stream/patch + connectivity, and 27 measured gotchas (16 onwards found live against the
  account): `docs/panel/data-surfaces-reference.md`
- **Live dashboard** (KPI strip · Positions · P&L, shipped 2026-08-04): the three modules in
  the diagram above. Read `claudia/dashboard_data.py`'s module docstring first — it carries the
  realised-P&L rule, the T+1 gap, and the source table for every figure. Two invariants that
  must not be relaxed: a failed poll republishes the previous `as_of` (so staleness stays
  visible instead of being masked by a fresh timestamp), and **every** `Tabulator` —
  positions and the working-order book — is `disabled=True` with **no** click/edit handler
  bound (Hard Rule 1, asserted over all of them in tests, not over a fixed one). The order
  book is where that matters most: it is the one surface where a click could plausibly be
  wired to "cancel this", and cancelling stays behind `propose_cancel` and both gates.
  `DashboardSnapshot.orders` is `tuple | None` because `()` ("nothing resting") and a
  failed lookup are opposite claims — never render an empty book for an unknown one.
  **The two realised figures on that screen are different quantities — never add them or
  "fix" one to match the other.** Ledger `realizedpnl` is today only; the week/month/YTD
  windows are Flex, which is T+1 and never includes today. They also use different day
  boundaries: Flex on IBKR's session date, the ledger on IBKR's **accounting** roll —
  measured 2026-08-05 as late-ET-evening **at an hour that varies**, and **not midnight**
  in ET or UTC, so late in the evening the "Realised today" tile can already be on
  tomorrow. (It was documented as a calendar day until that measurement.) Midnight ET,
  midnight UTC and a fixed clock hour were each killed by a reading, the last by a
  37-read watch that ended exactly on the surviving bracket's bound with the field
  unmoved. **Do not put a specific time in user-facing text** — a claim the user can
  check against the clock is worse than a vague one if it is wrong. Those two differences
  are why they disagree on screen. Futures realised there is **entry → exit at the traded prices, not
  settlement-relative** — a lot opened at 80.84 and closed the next trade date across a
  75.77 settlement still reports against 80.84.
  They are additionally defined on different cost bases — ledger on IBKR's real-time
  `avgCost`, Flex on the statement basis — but **that divergence was measured on 2026-08-05
  and did not appear**: the CRM close read −2,810.47 on both, to the cent, and the earlier
  ≈−252.60 counter-figure was a projection, not a measurement. Do not restate it. Evidence
  at `dashboard_data.REALISED_LEDGER_WINDOW` and `RealisedWindow`.
  The positions table leads with **"Avg entry"** — the average price of
  the open lots, FIFO over the account's own fills (`dashboard_data.economic_entries`) —
  and shows IBKR's basis beside it: a basis is a fiscal figure, and a trader sizing an exit
  needs the level actually traded at. The reconstruction publishes a number **only when it
  independently reproduces IBKR's own position quantity**, and a blank cell means it declined.
  That check was called the whole safety argument until **2026-08-10 disproved it**: CL closed
  both open lots and reopened two more inside one session, so a book that stopped at the last
  statement reproduced IBKR's `2` exactly and certified 77.185 for lots bought at 82.05 — and
  the pane turned the gap into a **+9,734.72 USD** claim that the unrealised P&L was "basis
  rather than market". Same quantity, different lots; no quantity comparison can see it. The
  input is what closes it: the book is Flex through `flex_coverage().through` plus
  `/iserver/account/trades` after it, keyed on **conid** (never symbol), and **fills that
  could not be read (`None`, as against `()`) blank the column rather than certify the stored
  history**
- **Futures contract identity (2026-09-10, gap #37):** a bare root (`ES`) keeps meaning the
  front month, as IB does, and the resolved contract is named in IB's own strings on every
  surface: `get_market_snapshot` FUT results carry `_contract` (`local_symbol ESU6`, `month
  SEP26`, `expires`, `name`), `get_futures` is sorted by expiry with `front_month: true`, the
  approval text carries a `Contract: ESU6 · SEP26 · expires 2026-09-18` line, and on the
  Positions and Orders tabs `Symbol` shows the local symbol once contract info is read while
  `Name` carries IB's long name with the month (`E-mini S&P 500 · Sep18'26`). One definition:
  `claudia/contract_identity.py` (cached per conid, never a guess). `ESU6` is an output only —
  IB's search rejects it as an input (measured 2026-09-10).
- Panel folder hub (both references + dated research + smoke screenshots): `docs/panel/README.md`
- Startup flow, phase by phase (diagnose startup failures): `docs/startup-flow.md`
- Trade data sync (Flex vs live API, integrity checks): `docs/flex-query-setup.md` and
  `docs/trading-data-reference.md`. **Realised P&L = `SUM(flex_trade.fifo_pnl_realized)` over
  ALL trades — no open/close filter** (settled 2026-08-04 against IBKR's own annual statements,
  6/6 years exact). `flex_lot` is pre-wash-sale detail and must not be summed instead;
  `Trade == Lot + WashSale`. Both traps were live in this repo and are gated now.
- Market calendar (20 exchanges, futures schedules): `docs/market-calendar-reference.md`
- GDrive sync (folder layout, error handling): `docs/gdrive-sync-reference.md`
- TradingView integration (sidecar, curated tools, recovery): `docs/tradingview-reference.md` and
  `docs/tradingview-mcp-recovery.md`. **Since 2026-08-11 `execute()` post-processes every sidecar
  result**, so a payload in `claudia.db` need not match what the sidecar emitted: epoch fields gain
  a `<key>_utc` ISO sibling, a tool reporting success over its own empty result gains a
  `claudia_warning`, and oversized Pine `text` blobs become `<omitted: N chars>`. The seam is inert
  otherwise — 12 of 16 real payloads pass through byte-identical — and it fails open on anything it
  cannot parse. **Do not set `ensure_ascii=False` there**: it turns a JS-escaped lone surrogate into
  a string that cannot be UTF-8 encoded and crashes the `conversation_store` insert (tried and
  reverted 2026-08-11). Full table and rationale: `docs/tradingview-reference.md` § Result
  post-processing
- Web scraping — the 4 tools ClaudIA can call (`fetch_page`, `crawl_site`, `search_site`,
  `firecrawl_search`), paywalled-site logins, and what a blocked page looks like:
  `ibkr_core_mcp/docs/web-scraper-reference.md`. Two things that bite from ClaudIA's side:
  the tools need the `[scraper]` extra (`pip install "ibkr_core_mcp[scraper]"`) and fail only
  at call time without it, and **a fetch of a domain with a saved login profile opens a real
  browser window** — required, not a bug (§6 has the evidence).
- Environment variables (full reference): `docs/env-vars-reference.md`
- Conversation memory schema: `docs/conversation-memory-reference.md`
- API source-of-truth URLs (IBKR, Anthropic, Drive, Panel, libraries): `docs/api-reference.md`
- Known gaps, live test log, project status: `docs/project-status.md`
- Full documentation catalog (every doc in `docs/`, categorized): `docs/README.md`
