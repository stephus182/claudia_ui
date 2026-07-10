# Handoff prompt — Tasks 5-7 of the 2026-07-10 live-test bugfix plan

Paste this to start the next session.

---

Continue the plan at `docs/superpowers/plans/2026-07-10-live-test-bugfixes.md` in the
claudia_ui repo (`/Users/steph/Claude_Projects/claudia_ui`, branch `main`). Tasks 1-4 are
done, reviewed, and committed:

- claudia_ui: `738a11c` (Task 1), `e23be54` (Task 3 part B), `192774a` + `52dfa94` (Task 4)
- ibkr_core_mcp (`/Users/steph/Claude_Projects/ibkr_core_mcp`, branch `main`): `a887048`
  (Task 2), `b9b23cf` (Task 3 part A)

Remaining: **Task 5** (diagnose `create_price_alert` HTTP 403), **Task 6** (diagnose
`ExecutionListener`'s `RuntimeError`), **Task 7** (live re-verification pass). Load
`superpowers:subagent-driven-development` (or `superpowers:executing-plans`) and work
these in order, same as before — work directly on `main` in both repos (already
established for this plan; no worktree needed).

## Before doing anything else

Check the gateway is authenticated:

```bash
curl -sk --max-time 5 -w "\nHTTP_STATUS:%{http_code}\n" https://localhost:5055/v1/api/tickle
```

As of this handoff it returns **401** (container running, session not logged in). If still
401, stop and ask the user to open https://localhost:5055 in a browser and complete login +
2FA before continuing — Tasks 5 and 6 cannot proceed without this, and Task 7 needs it too.

## Task 5 and 6 — diagnostic-first, not pre-written fixes

Both are intentionally NOT pre-written fixes — read the plan's Task 5 and Task 6 sections in
full (`docs/superpowers/plans/2026-07-10-live-test-bugfixes.md` lines 907-1187) before
touching code. Gather real evidence (actual HTTP error bodies, actual tracebacks, official
docs) and only write a fix once the evidence supports it — this is explicitly the point of
these two tasks (the whole plan exists because a past session found bugs by refusing to guess
at fixes). Do not invent a plausible-looking fix.

- **Task 5** (`ibkr_core_mcp`): reproduce `create_price_alert`'s HTTP 403 directly via
  `IBKRClient.create_alert()`, capture the real error body, check the official IBKR CP API
  docs for the alerts endpoint's requirements via WebFetch, check whether `get_alerts` (GET)
  works while `create_alert` (POST) doesn't (narrows to a permission/entitlement gap vs a
  code bug). Fix only if the evidence points to a code-side field/schema issue; otherwise
  document it as an account-entitlement gap.

- **Task 6** (`claudia_ui`): Step 1-5 (add `exc_info=True` to the swallowed-exception log
  call, write a regression test, commit) are safe and mechanical — do these regardless.
  Steps 6-8 require restarting ClaudIA live and reading the real traceback that appears for
  `ExecutionListener`'s repeating `RuntimeError` — only then compare it against the 5 existing
  Python 3.14/anyio compat patches in `claudia/app.py:29-226` to see if a 6th patch is
  warranted. If the traceback shows something different from the hypothesized
  `asyncio.current_task() is None` shape, stop and treat it as a new, separately-scoped bug —
  do not force-fit the anyio patch pattern.

## Task 7 — requires a physically-present human

This step cannot be completed non-interactively: it needs a live ClaudIA Chainlit session, a
live IBKR gateway, a live TradingView Desktop connection, and — critically — a human to click
through Touch ID + a 60-second-timeout AppKit confirmation dialog (Gate 2) and take a
screenshot of it. If running unattended, stop after Task 6 and ask the user to run Task 7
interactively.

Two things from code review during Tasks 1-4 are worth specifically checking during Task 7's
Step 4 (screenshotting the Gate 2 cancel dialog), flagged by the code-quality reviewer but not
fixed (both trace back to code the plan itself specified verbatim, not implementer
deviations — reviewer explicitly did not block merge on either):

1. `execute_cancel_order()` in `order_flow.py` forwards the *entire* raw LLM-authored cancel
   proposal dict as `order_details` into the Gate 2 dialog, rather than extracting a typed
   field set the way `execute_modify_order()` explicitly does (and the way
   `_format_cancel_summary()` one function above it already does). Low risk today against the
   documented cancel-proposal schema, but nothing schema-validates the LLM's JSON block.
2. `confirm_cancel_dialog`'s exclude-key filter strips literal `"Order ID"`/`"Account"`
   (Title Case with a space) but real proposals use `order_id` (snake_case) — so a real
   cancel proposal will likely render **both** an `order_id: 555` line and the canonical
   `Order ID: 555` line in the same dialog. This is inherited unchanged from
   `confirm_modify_dialog`'s identical filter (not a regression), but it's exactly the kind of
   thing a live screenshot should catch. If you see the duplicate row, that confirms the
   finding — flag it as a fast-follow rather than blocking Task 7.

Report task-by-task progress and stop for a checkpoint after Task 5 and Task 6 specifically —
their outcomes aren't known in advance, that's the point.
