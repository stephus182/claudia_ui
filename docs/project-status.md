# ClaudIA — Project Status

> Living document. Update after each sprint, live test session, or notable fix.
> Last updated: 2026-07-24 (full reorganization — Chainlit-era workaround detail removed;
> see this file's git history for the pre-reorg record).

**Framework note:** ClaudIA was migrated from Chainlit to Panel, completed and merged to
`main` on 2026-07-24 (`docs/plans/2026-07-22-panel-migration.md`, local archive). All
Chainlit-specific implementation history — Python 3.14/anyio compat patches, route-priority
workarounds, Chainlit CSS/status-bar wiring — is obsolete and lives only in git history.

---

## Architecture in One Paragraph

ClaudIA is a Panel chatbot served natively by `pn.serve` (Tornado) at `localhost:8001`. It
wraps an Anthropic SDK streaming loop (`claudia/agent.py`, UI-agnostic via the `MessageSink`
seam) that routes tool calls to three sources: `ibkr_core_mcp` (IBKR positions, orders,
alerts, history — direct Python import), `tradingview-mcp` (Node.js sidecar, curated tool
subset via stdio MCP), and local tools (`list_doc_versions`, `get_doc_version`,
`search_past_conversations`). Session state lives in `data/claudia.db` (SQLite).
`context.md` and `principles.md` define the persona and trading rules. GDrive syncs the DB
and docs across machines. Orders require two physical gates (Touch ID + AppKit NSAlert
colored dialog: green=BUY, red=SELL); the LLM has no order-execution tools. Order staging
supports equities (STK) and futures (FUT front-month with CME Rule 536-B fields). An
external Bokeh candlestick chart pane (`claudia/panel_chart.py`) renders cache-backed STK
history. ClaudIA surfaces user-directed trade proposals — it never makes trade decisions
autonomously.

---

## Milestone History

Condensed record of shipped work. Full per-commit detail: git history of this file
(pre-2026-07-24 revision) and `git log` in both repos.

| Date | Milestone |
| --- | --- |
| 2026-06-09 | Foundation — chat UI, agent streaming loop, all IBKR tools wired, `ConnectivityChecker` (IBKR/GDrive/TV status lights), in-chat gateway + TradingView startup buttons |
| 2026-06-11 | GDrive sync (`claudia.db` down/up, `context.md`/`principles.md` from Drive) + doc versioning (v1/v2 hash-change snapshots, `get_doc_version` tool) |
| 2026-06-12 | Security audit #1 — 8 findings resolved (env allowlist, chmod, size guard, lock, path validation, logging) |
| 2026-06-15 | Test coverage sprint — 133 unit tests across all modules |
| 2026-06-23/24 | IBKR Flex pipeline + 7-year backfill (1,029 trades, 2020-04-15 → 2026-06-22, integrity PASS); startup Flex sync; Drive `account_data/` archiving; truthful status lights (GDrive live `ping()`, IBKR auth-state parse of `/tickle`); End Session button; hot-reload bug batch fixed; `search_past_conversations` (FTS5) |
| 2026-06-25 | Full docstring audit; SSRF fix (H-1) + security audit #2 (`docs/audits/security-audit-2026-06-25.md`); security regression suite established |
| 2026-06-27 | ibkr_core_mcp v1.0 — tool count to 42 (adds `get_pa_periods`, `verify_flex_import`, `firecrawl_search`, `firecrawl_crawl`); SSRF decimal/hex-IP bypass guard ported |
| 2026-06-30 | TradingView sidecar connection fixed (78 tools discovered, 14 curated); detection + warning for TV running without the CDP debug port |
| 2026-07-01/02 | Order staging live-debugged end to end (9 bugs fixed); AppKit NSAlert Gate 2 dialog replaces tkinter; futures/FOP staging (CME 536-B fields, `/trsrv/futures` front-month conid); ORDER PARAMETER IMMUTABILITY rule added to system prompt |
| 2026-07-03 | Prompt caching — 3 breakpoints (tools → system → messages), live-verified 22,047-token static prefix cached at 0.1×; GDrive sync correctness G1–G3 (WAL-consistent upload, freshness guard, stale sidecar cleanup); SSRF guard on every redirect hop |
| 2026-07-06 | `place_order_and_confirm` full reply-confirmation chain (live-verified, orderId `242538143`) |
| 2026-07-07 | `ExecutionListener` replaces `PnLStreamer` for execution-triggered P&L |
| 2026-07-08 | Order modify/cancel proposal flows built (TDD + 3-angle review, 57 new tests); ibkr_core_mcp test suite reorganized by domain (`tests/claude_tools/`, pytest markers) |
| 2026-07-10 | Live Batch 1 order lifecycle passed (see Live Test Log); retry-fabrication finding mitigated via TOOL RESULT FRESHNESS system-prompt rule; `order_ref` origin-label fix; Gate 2 cancel dialog gains full order details; `read_text` freshness guard |
| 2026-07-15 | Python 3.11 revert — all Python 3.14 compat patching removed |
| 2026-07-17 | Always-on IBKR keepalive daemon (macOS LaunchAgent + `caffeinate`, live-verified); Account P&L / Account Summary display fixes (live-verified); `get_pnl` WebSocket self-priming fix; IBKR soft-timeout silent recovery built (`ssodh/init`, 15 safety-branch tests — live verify still pending); retry-fabrication mitigation live-re-verified |
| 2026-07-22 | Pre-migration code-quality audit — mypy 91→0, ruff 106→0, strict editable install for mypy, `mcp` pinned `<2`; all pending IBKR doc-verification items closed against scraped official docs. Report: `docs/audits/2026-07-22-code-quality-pre-migration-audit.md` |
| 2026-07-24 | **Chainlit→Panel migration complete, merged to `main`** — native `pn.serve` on :8001, `MessageSink` seam, framework-agnostic order cores, `agent.py` untouched, 100% safety coverage preserved; PineScript copy/inject revived (real client-side clipboard); external candlestick chart pane shipped (`panel_chart.py`, Bokeh, cache-backed); futures error-8089 fix + honest-success classifier live-proven (ES order `716373691` through the full gate chain) |
| 2026-07-24 | Post-migration cleanup & end-state audit — "no Chainlit workarounds" claim verified (PASSED for code: zero imports, zero workaround patterns, only Panel-first-class JS); residue purged: tracked `chainlit.yaml`, `.gitignore` Chainlit block, `.files/`, stale Chainlit/`app.py` docstrings in 8 code files + `startup-flow.md`, orphaned Chainlit deps via full venv rebuild from the documented setup; gates green on the fresh venv (451/ruff/mypy). `SECURITY.md` deferred to a dedicated audit. Record: `docs/audits/2026-07-24-post-migration-cleanup.md` |

---

## Test Coverage

### claudia_ui

**451 unit tests, 0 failures** (verified 2026-07-24: `pytest -q -m "not integration"`,
7.0s). `ruff check claudia/ tests/` and `mypy claudia/` both clean — all three are merge
gates. No test carries the `integration` marker (registered in `pyproject.toml` for future
use); live IBKR verification is manual and recorded in the Live Test Log below.

| Test file | Tests | Covers |
| --- | --- | --- |
| `test_order_flow.py` | 82 | Order/cancel/modify cores: summaries (STK/FUT labels, TIF, price formats), staging paths (STK/FUT/FOP, 536-B fields, conid override, front-month, multiplier), all error paths, `_resolve_account_id` |
| `test_agent.py` | 69 | Proposal-block stripping (order/cancel/modify), safety-block content, Hard-Rule-1 regression, system prompt (incl. TOOL RESULT FRESHNESS), history mapping, version note, local tools, decision logging, TV bridge |
| `test_panel_app.py` | 54 | Panel session lifecycle (`pn.serve` + `_init_session`), startup action buttons, status dots, session cleanup |
| `test_status.py` | 43 | IBKR/GDrive/TV connectivity checks, auth-state parsing, state transitions, soft-timeout recovery safety branches |
| `test_conversation_store.py` | 28 | Schema, CRUD, FTS5 search, decisions, doc_versions |
| `test_execution_listener.py` | 26 | Execution-triggered P&L capture, queue fan-out, shutdown drop window, retry/backoff |
| `test_security_regressions.py` | 21 | 2026-06-12 audit (9) + SSRF 2026-06-25 (11) + decimal/hex-IP bypass (1) — must stay green |
| `test_gdrive_sync.py` | 21 | Download/upload DB (RLock, WAL-safe), `read_text` freshness guard + tie boundary, size guard, chmod, `ping()` |
| `test_panel_pinescript.py` | 18 | ```pine block auto-detection, clipboard copy, TradingView inject buttons |
| `test_tradingview.py` | 17 | Binary discovery, CDP check, tool curation, env allowlist |
| `test_context_loader.py` | 17 | Load, hash, watchdog hot-reload, Drive override, version registration |
| `test_session_reporter.py` | 15 | Session report generation, tool-call/decision aggregation |
| `test_panel_chart.py` | 14 | Chart pane: fetch/cache paths, candlestick render, error/spinner paths |
| `test_panel_sink.py` | 10 | `PanelMessageSink` → `pn.chat.ChatInterface` routing |
| `test_opening_status.py` | 9 | UI-free opening-status builders |
| `test_panel_order_flow.py` | 7 | Proposal buttons → order-flow cores |

### ibkr_core_mcp (separate repo, own venv)

**835 tests — 750 unit + 85 integration** (counts verified 2026-07-24 via
`pytest --collect-only`). Run targeted by domain marker: `pytest tests/claude_tools/ -m
orders`, etc. — see `ibkr_core_mcp/CLAUDE.md`. Known flag: that repo's `CHANGELOG.md` is
stale since 2026-06-27 (out of this repo's scope — follow-up for its maintainer).

---

## Live Testing

Live tests run against a real IBKR gateway (and TradingView Desktop where relevant), driven
through the ClaudIA UI with real Touch ID + Gate 2 interaction. Standing safety rules:
`caffeinate` on for the whole session; ONE fresh gateway login up front, never re-auth
mid-session (`feedback-ibkr-session-safety`); write tests use prices far outside realistic
market context. Two deliberately resting far-out test orders (ES @ 6100, AAPL @ 150) are
currently parked on the account for next-session checks.

### Test Area Index

| # | Area | Status | Last verified | Notes |
| --- | --- | --- | --- | --- |
| 1 | Session startup | ✅ Verified | 2026-07-24 | Full startup passes 2026-06-30 (Chainlit); Panel startup re-verified in migration smoke tests (`docs/panel/`) |
| 2 | GDrive sync | ✅ Verified | 2026-07-17 | DB down/up, hot reload, freshness guards all live-verified. Outstanding: Drive-root doc fetch (Drive copies still stale at v1 — re-upload needed), manual Drive-DB inspection |
| 3 | Doc versioning | ✅ Verified | 2026-06-24 | Hash-change registration, `list_doc_versions`, `get_doc_version` |
| 4 | Core chat — IBKR tools | ✅ Verified | 2026-06-26 | Positions, P&L, live orders, account summary/ledger/allocation, snapshots, multi-turn history |
| 5 | Market data — history & analytics | ⚠️ Partial | 2026-06-27 | 3m/6m daily bars + pagination (1000-point limit) verified. Outstanding: long-lookback weekly bars, indicator/backtest chain, contract-resolution checks |
| 6 | Price alerts | ⛔ Blocked | 2026-07-17 | `create_price_alert` HTTP 403 diagnosed as **account-side entitlement gap**, not code (see Known Gaps). Lowest priority per user direction |
| 7 | Order staging — place | ✅ Verified | 2026-07-24 | STK button-only lifecycle 2026-07-10 (orderId `567317535`); FUT live-proven 2026-07-24 (ES `716373691`, 8089 fix + honest-success classifier) |
| 8 | Order modify / cancel | ✅ Verified | 2026-07-10 | STK modify ($100→$105) + cancel, zero manual reply-chain intervention. Open: FUT/FOP cancel 536-B params (upstream), Touch-ID-decline negative path never exercised |
| 9 | TradingView live tools | ✅ Verified | 2026-07-17 | `chart_get_state`/`set_symbol`/`set_timeframe`/`pine_set_source` reliable when actually invoked (screenshot-cross-checked); retry-fabrication finding mitigated + live-re-verified. Outstanding: screenshot-vision analysis test |
| 10 | Flex trade history | ✅ Verified | 2026-06-30 | `verify_flex_import` CLEAN PASS (983/983 tradeIDs reconciled); startup sync + skip logic. Outstanding: offline-start path, store-sourced queries, rate-limit path, `sync_flex_archive` |
| 11 | Conversation memory | ✅ Verified | 2026-06-24 | FTS5 recall with dated snippets |
| 12 | Security controls | ✅ Verified | 2026-07-10 | Order refusals cite principles; API-key grep of logs + transcripts clean; direct key probe refused |
| 13 | External chart pane | ⚠️ Partial | 2026-07-24 | Render proven on real cached OHLCV; live fetch→render smoke needs an authenticated gateway (Track B1) |
| 14 | IBKR soft-timeout recovery | ⚠️ Built, not live-tested | — | 15 unit tests green; needs a deliberate >6-min idle with a human present (Track B2) |
| 15 | Keepalive daemon | ✅ Verified | 2026-07-17 | Both OK↔WARN transitions live, `caffeinate` hold/release confirmed via `ps` |
| 16 | Account P&L display | ✅ Verified | 2026-07-17 | Opening card + `get_live_pnl` consistent, matched IBKR Mobile figures |

### Outstanding Live Tests

**Next authenticated-gateway session (Track B — ~45–60 min, human present):**

- [ ] **B1 — Chart live smoke**: uncached liquid STK (e.g. MSFT) 6m/1d fetch→render +
      screenshot; re-Load cache hit; eyeball `1h`/`30m` body widths (median-spacing fix
      `794d7c0` never seen live); bogus symbol → honest error, spinner clears
- [ ] **B2 — Soft-timeout recovery**: idle >6 min (overlap with B3/B4), confirm silent
      `ssodh/init` recovery with no 2FA prompt (protocol: 2026-07-17 soft-timeout plan, Task 5)
- [ ] **B3 — Gate 2 cancel-dialog screenshot** (twice-missed): disposable AAPL LMT far
      below market; pre-arm the screenshot (or screen-record) before clicking; check the
      duplicate `order_id`/`Order ID` row + unfiltered fields; clean up the order
- [ ] **B4 — Guardrail corpus capture**: fresh session, ES far-out LMT, modify request ~5
      turns in; capture the transcript either way (evidence for Track A — no live fixing)

**Backlog (any later live session):**

- [ ] Touch ID decline path → "authentication failed" message, button removed (never exercised)
- [ ] TradingView screenshot drag/paste → vision analysis (blocked twice by test tooling, needs a manual pass)
- [ ] GDrive: `context.md`/`principles.md` fetched from Drive root (after manual re-upload of current docs); manual inspection of the Drive DB copy
- [ ] Flex: IBKR-offline startup path; `get_trades source='store'` historical queries; `check_flex_coverage`; rate-limit (1001) messaging; `sync_flex_archive` dedup
- [ ] Market data: 5y weekly bars; indicator overlay → backtest → analytics chain; `search_contract`/`get_contract_info`/`get_futures` resolution checks
- [ ] Price alerts full matrix (single %/$ variants, bulk, modify, cancel/deactivate) — **blocked on the IBKR entitlement** (check Account Management / support ticket first)

### Live Test Log

| Date | Scope | Key findings | Outcome |
| --- | --- | --- | --- |
| 2026-06-23 | Startup, IBKR read tools, conversation logging | Stopped-container bug in `GatewayManager.start()` fixed | PASS |
| 2026-06-24 | GDrive sync, doc versioning, memory recall, security refusals | 6 bugs found + fixed (GDrive deadlock, IBKR auth check, hot-reload ×3, tool-dispatch gap) | PASS |
| 2026-06-25/26 | Core IBKR tools; market & account data batch | `get_watchlists` 404 + fabricated fallback caught by DATA INTEGRITY constraint; option-chain 404 (both later root-caused as wrong endpoint paths — doc verification 2026-07-22) | PASS with findings |
| 2026-06-30 | Startup ×3, Flex integrity ×2, TV sidecar | 3 TV bugs fixed (app name, no-debug-port detection, sidecar connect); `verify_flex_import` CLEAN PASS 983/983 | PASS |
| 2026-07-01/02 | Order staging §5 full flow | 9 bugs found + fixed; ORDER PARAMETER IMMUTABILITY violation caught (ClaudIA changed $100→$250 — rule added); AppKit Gate 2 built | PASS after fixes |
| 2026-07-06 | Order reply-confirmation chain | Real 3-chained-reply AAPL order `242538143`; caveat (mixed direct-client + UI verification) closed by the 2026-07-10 clean re-run | PASS |
| 2026-07-10 | **Batch 1** — clean button-click-only send→modify→cancel lifecycle, orderId `567317535` (AAPL LMT GTC, $100→$105→cancelled) | 2 bugs logged: `order_ref` EXTERNAL mislabel (cosmetic but blocked auto-modify proposals); Gate 2 cancel dialog missing order details. Both fixed in the follow-up session | PASS |
| 2026-07-10 | **Batches 2–4** — TradingView live, price alerts, security | 🔴 **Retry-fabrication finding**: "retry X"-phrased requests skipped the tool call and fabricated results (3 independent instances, proven via tool-card/API-count/direct-screenshot cross-checks). Also: `create_price_alert` real HTTP 403; TV tools genuinely reliable when actually invoked | FAIL → mitigated 2026-07-10, live-re-verified clean 2026-07-17 |
| 2026-07-10 (follow-up) | Code fixes only (no live interaction): TOOL RESULT FRESHNESS rule, `order_ref` fix, Gate 2 cancel details, `read_text` freshness guard | All TDD + dual independent review; suites green both repos | COMPLETE |
| 2026-07-17 | First live session post-3.11-revert: boot, `ExecutionListener`, re-verification of all four 2026-07-10 fixes, alert-403 diagnosis | All four fixes confirmed clean live; `ExecutionListener` connects cleanly (root cause was `streaming.py` URL-doubling, not Python 3.14); **new findings**: P&L display bugs (fixed + live-verified same day), order-proposal button silently absent on 2nd+ proposal per session, alert 403 concluded account-side entitlement | PASS with findings |
| 2026-07-17 | Keepalive daemon (`281a8d0`) — both state transitions against a real login/idle cycle | None — `caffeinate` hold/release + single-line transition logging confirmed | PASS |
| 2026-07-17 | Account P&L / Summary fixes live re-verification | None — opening cards + `get_live_pnl` consistent (Realized +$461.56 matched diagnosis figure exactly) | PASS |
| 2026-07-24 | Panel-migration live testing (order flow through the new Panel UI) | Futures error-8089 fix + honest-success classifier live-proven — ES order `716373691` accepted through the full gate chain; **proposal-block omission reproduced in a FRESH session ~5 turns in** (ES modify) — not a long-conversation-only failure, guardrail is the primary fix; upstream issues logged: `get_watchlists` nested-dict handling, `place_order` extOperator docstring | PASS with findings |

---

## Current Work Plan (2026-07-24)

Full executable detail (protocols, design questions, preconditions):
`docs/plans/2026-07-24-post-migration-work-order.md` (local archive). Pick the track by
gateway availability at session start. Priority stance per user direction: fix main
features and known bugs first; price alerts are lowest priority (blocked on an IBKR
entitlement, not engineering).

**Track A — anti-fabrication guardrail (no gateway needed; the top open defect):**

- [ ] **A1 — Design doc + user sign-off** (`docs/plans/2026-07-25-anti-fabrication-guardrail-design.md`,
      local): detection (a) proposal-intent-without-block — deterministic post-strip check on
      `display_text`, precision over recall, signals validated against the real 2026-07-17 +
      2026-07-24 transcripts in `claudia.db`; response policy = ONE corrective retry, then an
      honest System notice. Detection (b) data-question-without-tool-call is descoped to a
      later iteration. Hard constraints: safety block untouched; the guardrail never
      synthesizes or repairs a proposal block; every violation logged.
- [ ] **A2 — TDD implementation** (subagent-driven, spec + quality reviews): `agent.py`
      post-response check + bounded retry; fixtures include a verbatim real failing
      transcript plus innocent look-alikes; live acceptance (the ES-modify recipe) rides the
      next gateway session.

**Track B — authenticated-gateway live batch:** items B1–B4 above (see Outstanding Live Tests).

**Track C — small-fix batch (fill-in, independent, no gateway):** chart "No data"
IBKR-offline-vs-unknown-symbol honesty (verify `execute()`'s real return shape first);
bounded `TradingViewBridge.start()` handshake (`asyncio.wait_for` ~15s → screenshot-mode
degradation); `upload_db` → return `bool`, threaded into cleanup status lines. Cross-repo
(`ibkr_core_mcp`, own session): empty-trades staleness, `get_watchlists` nested-dict,
`place_order` extOperator docstring.

**Track D — deep restyle: LATER, own project** (brainstorm + spec with user first; draws on
`docs/panel/` research; includes split-vs-tabs, theme, and the deferred chart features).

---

## Known Gaps / Tech Debt

### Open — priority order

| # | Item | Where | Status |
| --- | --- | --- | --- |
| 1 | 🔴 **Order-proposal block silently omitted → anti-fabrication guardrail needed** | `claudia/agent.py` (agent loop) | ClaudIA's text claims a "Stage this order" button exists while the required fenced `order-proposal` block was never emitted (no button renders — **fails safe**, but blocks legitimate orders and is the top trust defect). Found 2026-07-17 (3× repro in one session, fresh session worked around it); **2026-07-24: reproduced in a FRESH session ~5 turns in** — not long-conversation-only, fresh-session workaround unreliable. Same failure class as the resolved 2026-07-10 retry-fabrication finding. Fix = Track A guardrail (spec not yet written): proposal-intent-without-block detection → one honest retry, then a System notice; never synthesize the block. |
| 2 | `create_price_alert` HTTP 403 on every attempt | IBKR account entitlement (+ `ibkr_core_mcp/claude_tools.py`) | **Diagnosed 2026-07-17 as an account-side entitlement gap, not a code bug** — 6-step elimination (docs-literal payload, browser headers, cookie dedup, `Server-Timing` shows a fast origin rejection) while order writes succeeded on the same session. Next step is administrative: check IBKR Account Management for a Price Alerts entitlement or open a support ticket. **Separate real code defect to fix whenever alerts resume** (won't fix the 403): `_create_price_alert` sends undocumented `conid`/`exchange`/`conditionType`/`orderId`/`isSizeCondition` fields instead of the documented `conidex` string + required `logicBind`/`triggerMethod`. Lowest priority per user direction. |
| 3 | `upload_db` swallows its own errors — cleanup can report Drive success on a real failure | `claudia/gdrive_sync.py` (`upload_db`), `claudia/panel_app.py` cleanup paths | Returns `None` and catches everything internally, so session-end can render "claudia.db → Drive ✅" when nothing uploaded. Pre-existing (byte-parity with Chainlit cleanup), not a migration regression. Fix (Track C): return `bool` like `download_db`, thread into status lines. |
| 4 | Empty trades table skips the first-ever startup Flex sync with a misleading "data current" log | `ibkr_core_mcp/store.py` (`get_trade_date_coverage`), `claudia/panel_app.py` | Zero-row store returns no `stale` key → a never-populated store is treated as current. Upstream fix candidate: treat `newest=None` as stale. |
| 5 | FUT/FOP cancel missing CME 536-B query params | `ibkr_core_mcp/client.py` (`cancel_order`) | IBKR documents `manualIndicator`/`extOperator` as required cancel params for FUT/FOP; the signature can't pass them. STK cancel unaffected (live-confirmed). Upstream fix. |
| 6 | MIDPRICE/TRAIL/TRAILLMT order types lack price-field handling | `claudia/order_flow.py` (place + modify) | Only LMT/STP/STOP_LIMIT populate `price`/`auxPrice`; the proposal schemas document the same four types. Needs symmetric changes across all three schemas + both execute paths. |
| 7 | Gate 2 cancel-dialog cosmetic residuals — needs a live screenshot | `ibkr_core_mcp/order_confirm.py`, `claudia/order_flow.py` | Two review-flagged residuals: raw LLM proposal dict forwarded unfiltered (modify builds a typed field set; cancel doesn't), and a likely duplicate `order_id`/`Order ID` row. Functionally verified twice; the screenshot check missed both times (native dialog closes too fast). Track B3. |
| 8 | Chart pane follow-ups | `claudia/panel_chart.py` | Live fetch→render smoke pending (Track B1); "No data" message conflates IBKR-offline vs unknown-symbol (Track C); first-cut scope limits by design (volume subplot, overlays, crosshair, zoom-sync, non-STK, theming) — deferred to the restyle project. |
| 9 | `TradingViewBridge.start()` handshake is unbounded | `claudia/tradingview.py` | A wedged sidecar could stall session init; never observed live. Fix (Track C): `asyncio.wait_for` ~15s → honest screenshot-mode degradation. |
| 10 | FOP staging requires a pre-resolved conid | `claudia/order_flow.py` | By design: FOP without `conid` → clear error directing to `get_option_chain` first. A full expiry+strike+right resolution flow is unbuilt. |
| 11 | Cross-repo `ibkr_core_mcp` follow-ups | `ibkr_core_mcp` | (1) `get_watchlists` nested-dict handling bug (live, 2026-07-24); (2) `place_order` docstring still implies `extOperator` for CME 536-B — reversed by the live-proven 8089 finding (`manualIndicator`-only); (3) Drive archive duplicate-upload dedup (`_find_file` check before upload); (4) `CHANGELOG.md` stale since 2026-06-27. All out of this repo's scope. |
| 12 | Stale Drive copies of `context.md`/`principles.md` | Google Drive root folder | The 2026-07-10 freshness guard prevents the *silent overwrite*, but Drive still holds v1 (June 11) copies — manually re-upload the current local docs so Drive and local agree. Also: the guard is verified across sessions in one process, not yet across a true process restart (mechanism identical; minor). |
| 13 | Minor test debt | `tests/` | `test_strip_order_proposal_malformed_json` doesn't assert `clean` is unchanged; env allowlist covered twice (tradingview + security_regressions). Low priority. |
| 14 | Deep restyle pending | all `panel_*` modules | Phase 7 was deliberately minimal (function-first ruling). Dedicated future project — Track D. |

### Resolved (summary — full detail in this file's git history)

| Item | Resolution |
| --- | --- |
| 🔴 Retry-phrased requests fabricated tool results | TOOL RESULT FRESHNESS system-prompt rule (2026-07-10, `738a11c`); live-re-verified clean 2026-07-17 (genuinely fresh second tool call on "retry it") |
| Futures orders rejected with error 8089 + false success reporting | extOperator removed (`manualIndicator`-only) + honest-success classifier; live-proven 2026-07-24, ES order `716373691` |
| Account P&L / Summary showed wrong or missing P&L | Dead summary fields removed + ledger fallback via shared `get_live_pnl_text()`; live-verified 2026-07-17 |
| `get_pnl` empty on a cold gateway session | Root-caused: needs one `spl` WS subscription touch; `_get_pnl` now self-primes (ibkr_core_mcp `3f81bb6`), 2026-07-17 |
| `get_live_orders`/`diagnose_orders` mislabeled every order EXTERNAL | Real field is `order_ref` (snake_case); fixed 2026-07-10 (`a887048`), live-re-verified 2026-07-17. Residual: extract a shared `_classify_order_origin()` (twin functions differ on a `clientId="0"` edge) |
| Gate 2 cancel dialog showed only Order ID + Account | Full order details threaded through (2026-07-10); cosmetic residuals pending screenshot (open item 7) |
| No freshness guard on `context.md`/`principles.md` Drive override | `read_text(local_path=)` mtime guard, tie-inclusive (2026-07-10); live-re-verified v3-stable across 3 session starts 2026-07-17 |
| `ExecutionListener` failed to connect in-process | Root cause was `IBKRWebSocket` URL-doubling (`streaming.py` `e209272`), not Python 3.14; clean connect live-verified 2026-07-17 |
| Chainlit `app.py` had zero unit tests | Resolved by the Panel cutover — `panel_app.py` built testable (54 tests) |
| Order place / modify / cancel never live-verified end-to-end | Clean button-click-only lifecycle 2026-07-10 (orderId `567317535`) |
| Watchlists + option-chain endpoints returned 404 | Wrong paths — corrected via doc verification (`/iserver/watchlists`; two-step `search` + `strikes`), 2026-07-22 |
| TV sidecar crash / Python 3.14 compat patches | Obsolete — 2026-07-15 Python 3.11 revert removed the entire patch surface |

---

## Planned Features (Not Built)

| Feature | When | Notes |
| --- | --- | --- |
| Anti-fabrication guardrail | Next (Track A) | See Known Gaps #1 — spec first, then subagent-driven TDD |
| Voice output (TTS) | Phase 2 | `edge-tts`; Panel-side audio delivery TBD (the old Chainlit `cl.Audio` approach no longer applies) |
| ML signals | Phase 3 | `ibkr_ml_client` sibling repo; pattern detection, regime signals |
| Vector RAG knowledge base | After v1.0 live tests | sqlite-vec + sentence-transformers, separate `knowledge.db`, `search_knowledge_base` tool (design chosen, on hold) |
| Scraping → RAG pipeline (layer-2 list/read/delete tools) | On hold | Design approved 2026-07-01; spec in the local plan archive |

---

## IBKR Doc Verification — Complete (2026-07-22)

All "observed, not documented" behaviors flagged in code were verified against scraped
official IBKR docs (Firecrawl keyless tier — the pages are fully public, no login gate).
Citations + quotes live in the relevant `ibkr_core_mcp` docstrings and in this file's git
history; the audit context is in `docs/audits/2026-07-22-code-quality-pre-migration-audit.md`.
(Numbering is historical; there was never an item 10.)

| # | Claim | Verdict |
| --- | --- | --- |
| 1 | `/iserver/account/trades` is session-scoped | **Corrected** — not origin-scoped; the "missing mobile fills" observation was a subscription-warmup artifact |
| 2 | `?days=7` extends lookback | **Confirmed** — documented param, 7-day max |
| 3 | `/pa/allperiods` response shape | **Confirmed** — dict keyed by account ID with per-period sub-objects |
| 4 | PA same-day fill latency | **Genuinely undocumented** — honest non-result |
| 5 | PA period strings account-specific | **Corrected** — fixed documented set (`1D`,`7D`,`MTD`,`1M`,`YTD`,`1Y`) |
| 6 | Flex T+1 cutoff time | **Genuinely undocumented** — IBKR publishes no generation time |
| 7 | Flex error 1025 not in the official table | **Confirmed** — absent from the official 20-code table (so is 1002) |
| 8 | No `Retry-After` on 429s | **Confirmed** — pacing limits documented (10 req/s, 15-min penalty box), no `Retry-After`; fixed backoff is correct |
| 9 | `/iserver/marketdata/history` bar limit | **Confirmed** — 1,000 data points; pagination implemented 2026-06-27 |
| 11 | `GET /iserver/account/watchlists` 404 | **Corrected** — wrong path; documented endpoint is `GET /iserver/watchlists` |
| 12 | `GET /trsrv/secdef/chains` 404 | **Corrected** — endpoint doesn't exist; documented flow is `/iserver/secdef/search` → `/iserver/secdef/strikes` |
