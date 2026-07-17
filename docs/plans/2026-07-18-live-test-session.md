# Live Test Session Plan — 2026-07-18

> Self-contained handoff for the next live test session. Paste this to start cold.

**Context:** the last live test session was 2026-07-10 (Batches 1-4 of
`docs/plans/2026-07-08-order-cancel-modify.md`'s Part C). Since then, the single biggest
change to the stack is the **2026-07-15 Python 3.11 revert**
(`docs/plans/2026-07-15-python-3.11-pin.md`): all 6 Python-3.14 anyio/asyncio monkeypatches
were removed from `claudia/app.py`, `.venv` was rebuilt on 3.11.15, and guardrails
(`requires-python = ">=3.11,<3.14"`, `.python-version`) were added. That revert was verified
only at the **boot level** (a bare `chainlit run` + the unit suite) — it has never been
exercised through a real browser session, a live IBKR gateway, or a live TradingView Desktop
connection. This session is the first live proof (or disproof) that the revert didn't
regress anything, layered on top of finishing the carried-over work from 2026-07-10.

## Status snapshot (2026-07-17, gateway offline while writing this)

| Check | Result |
|---|---|
| claudia_ui venv | Python 3.11.15, **296/296** unit tests pass (295 + 1 new, see below) |
| ibkr_core_mcp venv | Python 3.11, **740/740** unit tests pass, ruff clean, mypy clean |
| `websockets` | 16.1 importable — base dependency now (was silently missing behind `[server]` before) |
| `IBKRWebSocket` URL-doubling fix | Confirmed in `ibkr_core_mcp/streaming.py` (commit `e209272`) |
| IBKR gateway | Down (Docker daemon itself not running) — expected, this is prep, not the live session |

**Pre-work already done today (2026-07-17), uncommitted, ready for review:**
Task 6 Steps 1-5 of `docs/plans/2026-07-10-live-test-bugfixes.md` — added `exc_info=True` to
`ExecutionListener._run_with_retry`'s swallowed-exception log call
(`claudia/execution_listener.py`), plus a new regression test
(`test_run_with_retry_logs_traceback_on_error` in `tests/test_execution_listener.py`) asserting
a log record carries `exc_info`. TDD (watched RED, then GREEN), full suite still 296/296, ruff
clean on both touched files. **Not committed** — commit only if asked.

This means when `ExecutionListener` is exercised live tonight, a real traceback will appear in
the log instead of just `RuntimeError` with no context — whether or not the mystery is already
fixed by the `streaming.py` URL-doubling patch (leading suspect, unconfirmed) will finally be
visible directly rather than inferred.

## Prerequisites before starting

1. `git status` in both `claudia_ui` and `ibkr_core_mcp` — confirm the exc_info logging change
   above is still present (or committed) before proceeding.
2. Start Docker + the IBKR gateway, log in + complete 2FA in a browser at
   `https://localhost:5055` until `curl -sk https://localhost:5055/v1/api/tickle` shows
   `"authenticated": true, "connected": true`.
3. Start TradingView Desktop with `--remote-debugging-port=9222` (needed for Phase 3 step 1
   and the Batch 2 backlog item in Phase 5).
4. `./start-claudia.sh` and open `http://localhost:8000` in a real browser — Phase 1 below
   starts here.
5. This session needs a **physically present human** for Touch ID + the AppKit Gate 2 dialog
   (Phase 3, order tests) — same requirement as every prior order-staging live test.

## Phase 1 — Boot verification under Python 3.11 (do this first, new this session)

Confirm, watching the actual terminal log and the chat UI, not just that it starts:
- [ ] Status lights: IBKR ✓, GDrive ✓, TV ✓ (or a graceful fallback if TV Desktop isn't up)
- [ ] No anyio/asyncio errors of any kind in the log — there is no compat patch left to catch
  one silently; this session is the real test of whether 3.11 needed one in the first place
- [ ] GDrive: `claudia.db` downloads from Drive on start (check log)
- [ ] TradingView sidecar connects if TV Desktop + CDP port 9222 are up — this exercises the
  MCP stdio loop that the now-removed `AsyncIOTaskInfo.__init__` patch used to cover; if it
  fails with the *same* symptom as before (`'NoneType' object has no attribute 'get_coro'`),
  that would mean the bug isn't actually Python-3.14-specific — treat as a new finding, not
  something to re-patch reflexively

## Phase 2 — `ExecutionListener` verdict (Task 6 Steps 6-8)

- [ ] Watch the log within the first ~10-15s after startup for `"ExecutionListener started"`
  followed by either nothing further (connected cleanly) or a warning
- [ ] **If clean:** this closes Task 6 — the URL-doubling fix in `ibkr_core_mcp/streaming.py`
  was very likely the actual root cause all along, not a 3.14-only issue. Mark it resolved in
  `docs/project-status.md`'s Known Gaps with this session's date as the evidence.
- [ ] **If it still errors:** the `exc_info=True` fix means a full traceback is now in the log.
  ```bash
  grep -A 30 "ExecutionListener error (attempt 1)" <path-to-claudia-log> | head -35
  ```
  Compare the traceback's shape against what the removed patches used to address
  (`asyncio.current_task()` returning `None` inside `asyncio.timeout()`/`wait_for()`/an anyio
  internal). If it matches that shape, this is surprising post-revert (3.11 shouldn't have the
  3.14 regression) — investigate fresh, don't assume the old patch still applies verbatim. If
  it's a *different* exception entirely, treat it as a new, separately-scoped bug per the
  original plan's Step 7 guidance — do not force-fit a fix that doesn't match the evidence.

## Phase 3 — Task 7: re-verify the four 2026-07-10 fixes live

Full context for each fix: `docs/project-status.md` Known Gaps table (search for "RESOLVED
2026-07-10"). Use the same verification discipline as the session that found these bugs —
tool-call UI cards, server-log API-call counts, and screenshots where noted, not chat text
alone.

- [ ] **Retry-fabrication rule** — ask for a live quote (e.g. "What's the current price of
  TSLA?"), then say "retry it" or "show me the raw tool result." Confirm a real tool-call card
  appears and the server log shows ≥2 `messages.stream()` API calls for that turn. This is a
  system-prompt instruction, not deterministic code — a single clean pass doesn't prove it
  never fails, but a repeat of the original fabrication would be a clear regression.
- [ ] **`order_ref` origin label** — stage one disposable order (small GTC limit far below
  market, mirroring the 2026-07-10 Batch 1 protocol), call `get_live_orders`, confirm it shows
  `origin=ClaudIA-staged`, not `EXTERNAL`.
- [ ] **Gate 2 cancel dialog** — propose cancelling that same order, click "Cancel this order,"
  **screenshot the actual AppKit dialog** before confirming. Confirm it now shows
  symbol/side/qty/price/TIF, not just Order ID + Account. While looking at the screenshot,
  specifically check for the two known (non-blocking) residuals flagged by code review:
  1. a duplicate row — both `order_id: ...` (snake_case, from the raw forwarded proposal) and
     the canonical `Order ID: ...` line
  2. anything obviously malformed from a raw `float`/`None` proposal field rendering unfiltered
  Then confirm the cancel and verify the order is gone from `get_live_orders` — end this phase
  with **zero open ClaudIA test orders**, per this repo's established hygiene.
- [ ] **GDrive `read_text` freshness guard** — with GDrive reachable, restart ClaudIA twice in a
  row, confirm both sessions resolve to the same doc version (no v3→v1-style flapping).

## Phase 4 — Task 5: diagnose `create_price_alert`'s HTTP 403 (diagnostic-first)

Full step-by-step (exact reproduction script, WebFetch target, decision tree) is written out
already in `docs/plans/2026-07-10-live-test-bugfixes.md`, Task 5 (search for "Diagnose
`create_price_alert`"). Summary:

1. Confirm gateway auth (`curl .../v1/api/tickle`).
2. Reproduce the 403 directly via `IBKRClient.create_alert()`, capturing the actual response
   body (not just the status code) — `IBKRAPIError` should carry `resp.text[:400]`.
3. `WebFetch` the official CP API alerts endpoint docs — check what the documented error body
   actually means before writing any code (this repo's "docs first" convention — two past bugs
   went undetected for months because this step was skipped).
4. Check whether `get_alerts` (GET) succeeds on the same session while `create_alert` (POST)
   403s — narrows "account-entitlement gap" vs. "code bug."
5. Only write a fix if the evidence supports one; otherwise document as an account-side gap in
   `docs/project-status.md` Known Gaps, plainly, with what the user should check/enable.

This blocks all of Batch 3 (Price Alerts) below until resolved one way or the other.

## Phase 5 — resume the 2026-07-10 backlog once Phases 1-4 are clean

- [ ] **Batch 2 remainder:** drag/paste a TradingView screenshot into chat for vision analysis
  — blocked last time by a Playwright automation gap (no native file-chooser modal fired), not
  a known app bug; do this one manually, not automated.
- [ ] **Batch 3 (only if Phase 4 resolves the 403):** % loss/gain and $ loss/gain math
  verification, bulk alerts across all positions, modify (price/TIF/extended-hours), cancel/
  deactivate, confirm alerts appear in the IBKR mobile app. Full scenario checklist already
  written in `docs/project-status.md` §4b.

## Closing out

- [ ] Update `docs/project-status.md`: check off items above, update the Live Test Log table
  with a new dated row, move any newly-resolved Known Gaps into their `~~struck-through~~`
  resolved form (mirroring how the 2026-07-10 follow-up entries are formatted).
- [ ] Commit the `docs/project-status.md` update.
- [ ] If Task 6 (Phase 2) or Task 5 (Phase 4) produced a code fix, commit that separately first,
  following the TDD + review pattern already established for Tasks 1-4 (see
  `docs/plans/2026-07-10-live-test-bugfixes.md` for the exact style).
