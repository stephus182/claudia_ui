# Update Project Status + Order Operations Live Test Plan

## Context

Two things prompted this: (1) `ibkr_core_mcp` shipped a wave of `claude_tools.py` fixes and a full test-suite reorganization on 2026-07-08, none of which are reflected in `claudia_ui/docs/project-status.md` (last updated 2026-07-03); (2) the IBKR gateway is now up, making live order testing possible again, and the user wants to prioritize testing **order operations — send, modify, cancel, reply** — which have never been fully live-verified end-to-end.

Research surfaced two important facts that reshape the test plan, not just refresh the docs:

1. **`project-status.md` is stale in several places independent of the new ibkr_core_mcp work**: it claims 180 unit tests (actual: 233), is missing two whole test modules from its table, still shows §5 Order Staging as "not yet re-tested" even though a 2026-07-06 fix (`b012f6c` in claudia_ui, paired with `place_order_and_confirm`/`modify_order_and_confirm` in ibkr_core_mcp) already addressed and live-verified the placement reply-chain bug, and has a duplicate `§4b Price Alerts` heading.
2. **claudia_ui has no UI for modifying or cancelling an already-placed order** — only new-order placement exists (`order_flow.py`'s `render_order_proposal`/`execute_staged_order`). The underlying `IBKRClient.modify_order`/`cancel_order`/`modify_order_and_confirm` are already built and gated (Touch ID + AppKit dialog) in `ibkr_core_mcp`, just never wired into `claudia_ui`. The user has decided (confirmed via AskUserQuestion) to **build this wiring now**, mirroring the existing proposal-block pattern, then live-test it — rather than testing only at the raw client level.

**Correction:** research surfaced a note (`ibkr_core_mcp/docs/claude-tools-audit-2026-07.md`) claiming a resting order from the 2026-07-06 test (orderId `242538143`) was still live. **This is stale/false** — the user manually cancelled that order as part of routine EoD cleanup, outside of ClaudIA entirely (i.e. not via `cancel_order`/Gate 1/Gate 2, so it does **not** count as a "cancel via ClaudIA" live test). The account currently has no test orders resting. This plan does not depend on that order existing — Batch 1 below uses a single fresh disposable order for the full send → modify → cancel lifecycle instead.

This plan has three parts: **(A)** concrete edits to `docs/project-status.md` reflecting both repos' recent state, **(B)** the design for building order cancel/modify UI wiring in `claudia_ui` (TDD), and **(C)** the batched live test plan itself, orders first.

---

## Part A — `docs/project-status.md` updates

File: `/Users/steph/Claude_Projects/claudia_ui/docs/project-status.md` (409 lines, read in full).

1. **Header** (line 4): `Last updated: 2026-07-03` → `2026-07-08`.

2. **Feature Timeline** (after line 73): append rows for work shipped since 2026-07-03 that the timeline currently omits:
   - 2026-07-06 `b012f6c` / `0fb1fba` — `order_flow.py` now calls `place_order_and_confirm` to follow IBKR's full reply-confirmation chain instead of declaring success after the first response; distinct error message for a mid-chain reply decline.
   - 2026-07-06 (mcp) — `place_order_and_confirm`/`modify_order_and_confirm` added to `client.py`; live-verified via a real 3-chained-reply AAPL order (orderId `242538143`); that order was later manually cancelled by the user outside ClaudIA as routine EoD cleanup — it does not count as a live "cancel via ClaudIA" test (see Part C).
   - 2026-07-06 (mcp) — `preview_order` gains `STOP_LIMIT`/`MIDPRICE` order types + `stop_price` + `sec_type` params (fixes a live HTTP 500).
   - 2026-07-07 — `ExecutionListener` replaces `PnLStreamer` for execution-triggered P&L (consolidated single entry — full detail already in CLAUDE.md; not otherwise touched by this plan).
   - 2026-07-08 (mcp) — **claude_tools test suite reorganized**: the 2,373-line/177-test `tests/test_claude_tools.py` monolith deleted, replaced by `tests/claude_tools/` (11 files by domain, 181 tests, `TEST_INDEX.md`, new pytest markers `orders`/`flex`/`alerts`/`market_data`/`account`/`trades`/`pa_analytics`/`backtest_pinescript`/`web_scraping`/`errors`/`integration`). Repo-wide: **757 tests total — 673 unit + 84 integration**.
   - 2026-07-08 (mcp) — docs-accuracy pass: Touch ID policy corrected (biometric with system-password fallback, not biometric-only as previously documented), `CLAUDE.md` package-structure diagram +7 modules, market-calendar exchange-count fix, stale version-pin fix.

3. **Test Coverage section** (lines 77-94):
   - Line 79: `180 tests` → `233 tests`.
   - Table (lines 81-91): fix `order_flow.py` row `30` → `31`; add missing rows `test_execution_listener.py` (23) and `test_session_reporter.py` (15); fix `gdrive_sync.py` `14` → `17` and `context_loader.py` `14` → `15` (per the actual collected counts found during research).
   - Lines 93-94 (ibkr_core_mcp dependency note): replace with current, structured info — *"757 tests total — 673 unit (`pytest -m 'not integration'`) + 84 integration (`pytest -m integration`). Test suite reorganized 2026-07-08: `claude_tools.py` tests split from a single monolith into `tests/claude_tools/` (11 files by domain, domain-specific pytest markers, `TEST_INDEX.md`). Run targeted: `pytest tests/claude_tools/ -m orders`, etc. — see `ibkr_core_mcp/CLAUDE.md`."*
   - Add one line noting `ibkr_core_mcp/CHANGELOG.md` is stale since 2026-06-27 (predates all of the above) — flagged, not fixed here (out of this repo's scope; call out as a follow-up for whoever maintains that repo's changelog).

4. **What Has Never Been Live-Tested** (lines 98-103): replace the priority-order line with a pointer to the new batch plan in Part C (Batch 1 → 2 → 3 → 4), and note order send/modify/cancel/reply are now bundled as one batch rather than listed as a single "§5 re-test" item.

5. **§5 Order Staging** (lines 224-251):
   - Keep the existing 2026-07-01/02 checklist and bug table as historical record — do not rewrite history.
   - Update the "Approve dialog → order submitted → success" line (233) from BLOCKED to reflect the 2026-07-06 fix and live 3-chained-reply verification, **with the caveat** that verification mixed direct `IBKRClient` calls with UI per the audit doc's own phrasing — so a clean, button-click-only re-run is still open (this becomes Batch 1.1 in Part C).
   - Add a new `§5b — Order Modify / Cancel (feature does not exist yet)` subsection stating the gap plainly and pointing to Part B/Batch 1.0.
   - "Cancel live order via ClaudIA" (line 235) stays unchecked with a note: blocked on Part B's build, not just untested.

6. **Live Test Log table** (lines 293-299): add a row for 2026-07-06 — order reply-chain fix + live 3-chained-reply AAPL test, orderId `242538143`, outcome PASS with the "mixed direct-client + UI" caveat noted, plus a note that `242538143` was subsequently cancelled manually outside ClaudIA (not a ClaudIA-cancel test).

7. **Known Gaps / Tech Debt** (line 361): update the "§5 order submit not yet confirmed end-to-end" row to reflect the 2026-07-06 fix + caveat, and add a new row for "no UI wiring exists for order modify/cancel" pointing at Part B.

8. **Next Session Plan** (lines 303-336): replace entirely with the Part C batch plan (Batch 1 Orders → Batch 2 TradingView → Batch 3 Price Alerts → Batch 4 Security), keeping the existing §6/§9.3 content since it's still accurate, just re-sequenced and given batch numbers.

9. **Duplicate `§4b Price Alerts` heading**: remove the stub at lines 151-153 ("low priority — defer"), since the real dedicated batch already exists at line 191 and is scheduled as Batch 3 below. Leave a one-line pointer at the original location if useful for readers scanning top-to-bottom.

---

## Part B — Build order cancel/modify UI wiring (new feature, TDD)

Mirrors the existing new-order-placement pattern exactly (`agent.py` proposal block → `order_flow.py` render/execute → `app.py` action callback → Gate 1 Touch ID + Gate 2 AppKit dialog in `ibkr_core_mcp`). The LLM never gets a callable tool for this — Hard Rule 1 in `CLAUDE.md` already names `modify_order`/`cancel_order` explicitly and needs no change.

**Step 0 (spike, before writing modify code):** Live-call `get_order_status("242538143")` and `get_live_orders()` against the real gateway to confirm IBKR's actual field names/casing for this order (is `conid` present? what casing does `orderType` use?). Also re-attempt fetching IBKR's modify-order doc (`interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#modify-order`) through an authenticated session — an unauthenticated `WebFetch` returned 403 during research; the claim that **the full order body must be re-sent, not a partial diff,** was only confirmed via a third-party mirror and needs primary-source confirmation before implementation, per this repo's "docs first, never assume API behavior" rule.

**`claudia/agent.py`:**
- Generalize `_strip_order_proposal` into a small factory (`_make_block_stripper(tag)`) reused for two new tags: `order-cancel-proposal`, `order-modify-proposal`. Existing `_strip_order_proposal` keeps its name/behavior (tests import it directly).
- `handle_message`: chain the two new strips after the existing one; on a non-empty parse, call new `order_flow.render_cancel_proposal` / `render_modify_proposal`.
- `_SAFETY_BLOCK` additions: new "ORDER CANCEL / MODIFY RULES" section — LLM may only propose a cancel/modify for an `order_id` obtained from a real `get_live_orders`/`get_order_status`/`diagnose_orders` call *this conversation* (never invented); must check order origin first (mobile/TWS-placed orders can't be modified/cancelled via API — `get_live_orders`'s tool description already says this); modify proposals require a prior `get_order_status(order_id)` call specifically (richer detail than `get_live_orders` exposes, including `conid`). Plus a "MODIFY PARAMETER IMMUTABILITY" rule mirroring the existing order-parameter-immutability rule: every unchanged field must be copied byte-for-byte from the latest `get_order_status` result.

**Proposal JSON shapes:**
- `order-cancel-proposal`: `order_id` (required) + display-only fields (`symbol`, `action`, `quantity`, `order_type`, `limit_price`, `tif`, `reason`) copied verbatim from the sourcing tool call.
- `order-modify-proposal`: `order_id`, `conid` (required, no fallback resolution — unlike placement, re-resolving conid from a symbol risks picking a different contract), full current order fields, plus display-only `_changed_fields`/`_previous_values` for the before/after diff shown in Gate 2.

**`claudia/order_flow.py`:** new functions mirroring `_format_order_summary`/`render_order_proposal`/`execute_staged_order`:
- `_format_cancel_summary`, `render_cancel_proposal` (actions: `cancel_order` / `keep_order`)
- `execute_cancel_order` — parses payload, requires `order_id`, calls `ibkr.cancel_order(account_id, order_id)` (no reply chain — simpler than placement), same Gate 1/2 + error-pattern-matching + `store.add_decision(decision_type="trade_cancelled", ...)` + `finally: action.remove()` conventions as `execute_staged_order`.
- `_format_modify_summary`, `render_modify_proposal` (actions: `modify_order` / `discard_modify`)
- `execute_modify_order` — parses payload, requires `order_id` **and** `conid` (missing conid → clear error directing the LLM/user to call `get_order_status` first, no silent fallback), builds a **fresh** order body dict (never forwards the raw proposal — `modify_order()` in `client.py` does no `_`-prefix stripping, unlike `place_order()`), calls `ibkr.modify_order_and_confirm(account_id, order_id, order_body)` (reply-chain aware, first-ever live exercise of this path), same conventions as above with `decision_type="trade_modified"`.

**`claudia/app.py`:** four new `@cl.action_callback` handlers (`cancel_order`, `keep_order`, `modify_order`, `discard_modify`) inserted next to the existing `stage_order`/`cancel_proposal` callbacks (~line 892-914).

**Tests (TDD — write first):** ~45-50 new cases across `tests/test_agent.py` (~9: strip functions, system-prompt content, a Hard-Rule-1 regression asserting `{"place_order","modify_order","cancel_order","reply_order"} & _LOCAL_TOOL_NAMES == set()`) and `tests/test_order_flow.py` (~36-40: format/render/execute happy paths and every error path for both cancel and modify, mirroring the existing 31-case structure for placement).

**Docs:** add "Order Cancellation" and "Order Modification" subsections to `CLAUDE.md`'s "Order Staging Flow" section once the above is built and live-verified (Part C), including the field-body-completeness caveat from Step 0.

Critical files: `claudia/order_flow.py`, `claudia/agent.py`, `claudia/app.py`, `tests/test_order_flow.py`, `tests/test_agent.py`, `CLAUDE.md`.

---

## Part C — Batched live test plan (orders first)

Gateway is confirmed up. Each write action still requires live Touch ID + Gate 2 dialog confirmation in the moment — nothing here executes automatically.

### Batch 1 — Order Operations (send / modify / cancel / reply) — top priority

**1.0 Build** — Part B above, TDD, full green unit suite before any live step.

No test order currently exists in the account (see Context correction above) — Batch 1 places exactly **one** fresh disposable order and runs it through the full lifecycle, rather than juggling multiple orders. This keeps the account clean (single known order, single terminal state) and exercises send/modify/cancel/reply in one continuous, easy-to-audit sequence.

**1.1 Send — clean UI-only placement**
- Stage a fresh, deliberately unfillable order purely by clicking "Stage this order" in chat — no direct client calls, closing the "mixed verification" caveat left over from the 2026-07-06 session (e.g. `BUY 1 AAPL LMT @ <~65-70% below market> GTC`, or any liquid symbol — no need to avoid AAPL specifically now that `242538143` is gone).
- Confirm Touch ID + Gate 2 fire, any IBKR reply chain auto-resolves with zero manual intervention, success message shown.
- Verify via `get_live_orders`/`get_order_status`; record the new orderId.

**1.2 Modify — same order**
- Ask ClaudIA about the order (triggers `get_order_status`) so it can build an `order-modify-proposal` (e.g. bump `limit_price` a few dollars, staying well below market so it remains unfillable).
- Click "Modify this order" → Touch ID → Gate 2 → confirm `modify_order_and_confirm` fires and any reply chain auto-resolves — **first-ever live exercise of this path** (the client method's own docstring flags it as never live-verified).
- Verify the new price via `get_order_status(order_id)`.

**1.3 Cancel — same order**
- Propose cancelling the now-modified order.
- Click "Cancel this order" → Touch ID → Gate 2 → confirm `cancel_order` fires.
- Verify removed from `get_live_orders` / status shows Cancelled — account ends this batch with zero open test orders.

**1.4 Reply-chain verification** — not a separate click-through; graded from 1.1 and 1.2 above. Record explicitly whether IBKR raised any reply/confirmation prompt at each step and whether it auto-resolved without manual intervention.

**1.5 Docs** — update `project-status.md` §5/§5b, Live Test Log, Known Gaps, Test Coverage counts, and `CLAUDE.md`'s Order Staging Flow section with the real dates/order ID/results from 1.1-1.4.

### Batch 2 — TradingView Live Tools (§6)
Unchanged from the existing plan (lines 252-259/323-331): `chart_get_state`, `quote_get`, Pine Script generation + inject, `chart_set_symbol`/`chart_set_timeframe`, screenshot-vision fallback. Requires TradingView Desktop running with the CDP debug port.

### Batch 3 — Price Alerts (§4b)
Unchanged from the existing dedicated batch (lines 191-222): single alerts (explicit price, %loss, %gain, $loss, $gain), bulk alerts, modify, cancel/deactivate.

### Batch 4 — Security (§9.3)
Confirm `ANTHROPIC_API_KEY` never appears in chat output or Chainlit logs (line 282/335).

### Batch 5 (lower priority, parallel-track) — Pending Doc Verification
The 7 remaining "observed, not documented" items (lines 400-408: trades session-scope, `?days=7` param, PA response shapes, Flex T+1 cutoff, Flex error 1025, rate-limit/`Retry-After` policy) — doc-verification homework, not live order testing; doesn't gate Batches 1-4.

---

## Verification

- `pytest -m "not integration"` green after Part B, count grows 233 → ~278-283.
- Each Batch 1 live step confirmed manually in the running Chainlit session — Touch ID fired, correct Gate 2 dialog content, IBKR response text captured, order state cross-checked via `get_live_orders`/`get_order_status` after each action.
- No order left resting/unaccounted-for at the end of Batch 1: the single disposable test order ends in a known terminal state (Cancelled).
- `docs/project-status.md` and `CLAUDE.md` updated with real results (dates, order IDs, pass/fail) immediately after Batch 1 executes, not deferred.

---

## Handoff prompt (for a future implementation session)

This plan is intended for **later** implementation, not immediate execution. Paste the prompt below into a fresh Claude Code session in `/Users/steph/Claude_Projects/claudia_ui` to pick it up. It is self-contained — the session does not need this conversation's history, only this plan file.

> Implement the plan at `/Users/steph/.claude/plans/update-project-status-based-shimmying-charm.md` in full. Read it first — it covers three parts:
>
> **Part A** — a set of concrete doc edits to `docs/project-status.md` (stale test counts, missing rows, a duplicate heading, and outdated order-staging status). Do these first; they're low-risk and don't touch code.
>
> **Part B** — build order cancel/modify UI wiring in `claudia_ui`, which does not exist yet (only new-order placement exists today, in `claudia/order_flow.py`). Follow this repo's TDD convention (see the `superpowers:test-driven-development` skill if available — write the ~45-50 new test cases in `tests/test_agent.py`/`tests/test_order_flow.py` first, then implement against them). Two things to do **before** writing the modify-side code specifically:
>   1. Run the Step 0 spike in Part B: live-call `get_order_status(...)` and `get_live_orders()` against the real IBKR gateway (must be running — start it via `./start-claudia.sh` or the in-chat "Start IBKR Gateway" button if needed) to confirm IBKR's actual field shapes.
>   2. Re-verify, via an authenticated browser session (not a bare `WebFetch`, which 403'd during planning), IBKR's modify-order API docs at `interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#modify-order` to confirm the full-body-required claim at the primary source before finalizing `execute_modify_order`'s field list. This repo has a hard "docs first, never assume API behavior" rule (see `CLAUDE.md`'s "API Reference — Docs First" section) — do not skip this.
>
>   Non-negotiable constraint: the LLM must **never** receive a callable tool for `place_order`/`modify_order`/`cancel_order`/`reply_order` (`CLAUDE.md` Hard Rule 1). The new cancel/modify flows must follow the exact same pattern as the existing order-proposal flow — the LLM emits a JSON block in plain text, a human clicks a button, then Gate 1 (Touch ID) + Gate 2 (AppKit dialog) fire. Add the Hard-Rule-1 regression test described in Part B.
>
> **Part C** — a batched live test plan, orders first (Batch 1), then TradingView (Batch 2), price alerts (Batch 3), security (Batch 4). **Batch 1's live steps (1.1-1.4) require an interactive session on the user's machine** — Gate 1 is a real Touch ID prompt and Gate 2 is a real GUI dialog requiring a physical click, so this cannot run headless/unattended/in the background. Confirm with the user before placing, modifying, or cancelling any real order, even though the order will be deliberately unfillable. Do not proceed to Batch 1's live steps until Part B's full test suite is green.
>
> After Batch 1 completes (or if it's deferred to a later session), update `docs/project-status.md` and `CLAUDE.md` with the real results per Part C item 1.5 — don't leave the docs describing a hypothetical future state.
>
> Work through Parts A → B → C in order. Ask before starting Part B if you'd rather review the exact proposal JSON shapes and function signatures in the plan first — they're spelled out in detail and shouldn't need re-deriving.
