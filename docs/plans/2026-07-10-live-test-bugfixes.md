# Live-Test Bug Fixes (2026-07-10 Batch 1-4 findings) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the six bugs found during the 2026-07-10 live test session (Batches 1-4, logged
in `docs/project-status.md`'s Known Gaps and Live Test Log) before starting the next live test
batch — includes the critical retry-phrased-request fabrication finding.

**Architecture:** Four bugs are fully diagnosed with a known fix — these are done TDD-first with
no live dependency for the code change itself (Tasks 1-4). Two bugs (`create_price_alert` HTTP
403, `ExecutionListener`'s unexplained `RuntimeError`) are only partially diagnosed — evidence
tonight was suggestive but not conclusive, so those tasks are diagnostic-first: gather hard
evidence via a live run, then decide the fix from what's actually observed rather than guessing
(the whole point of tonight's testing was catching exactly this kind of unverified assumption).
A final task bundles a live re-verification pass mirroring tonight's own verification protocol
(tool-call cards + API-call counts + screenshots, not chat text alone) before the next batch
starts.

**Tech Stack:** Python 3.14, pytest, Chainlit, Anthropic SDK (`claudia_ui`), IBKR Client Portal
API (`ibkr_core_mcp`, separate repo/venv at `/Users/steph/Claude_Projects/ibkr_core_mcp`).

**Repos touched:** `claudia_ui` (this repo) and `ibkr_core_mcp` (sibling repo — has its own
venv and `pytest` invocation; do not confuse the two test suites).

---

## File Structure

| File | Repo | Change |
|---|---|---|
| `claudia/agent.py` | claudia_ui | Add `TOOL RESULT FRESHNESS` rule to `_SAFETY_BLOCK` |
| `tests/test_agent.py` | claudia_ui | New regression test for the rule |
| `ibkr_core_mcp/claude_tools.py` | ibkr_core_mcp | Fix `_get_live_orders`/`_diagnose_orders` origin field lookup |
| `tests/claude_tools/test_orders.py` | ibkr_core_mcp | New origin-label tests |
| `ibkr_core_mcp/order_confirm.py` | ibkr_core_mcp | `confirm_cancel_dialog` gains an optional `order` param |
| `ibkr_core_mcp/client.py` | ibkr_core_mcp | `cancel_order` gains an optional `order_details` param |
| `tests/test_order_confirm.py`, `tests/test_client.py` | ibkr_core_mcp | Update 2 existing mocks, add 2 new tests |
| `claudia/order_flow.py` | claudia_ui | Pass the cancel proposal as `order_details` |
| `tests/test_order_flow.py` | claudia_ui | Update 1 existing assertion |
| `claudia/gdrive_sync.py` | claudia_ui | `read_text` gains a freshness guard (mirrors `download_db`'s) |
| `claudia/app.py` | claudia_ui | Pass `local_path` into both `read_text` calls |
| `tests/test_gdrive_sync.py` | claudia_ui | 3 new freshness-guard tests |
| `claudia/execution_listener.py` | claudia_ui | Add `exc_info=True` to the swallowed-exception log call |
| `tests/test_execution_listener.py` | claudia_ui | New traceback-logging regression test |

No new files are created — every change is a targeted edit to an existing file.

---

### Task 1: 🔴 Anti-fabrication system prompt rule (top priority)

**Files:**
- Modify: `claudia/agent.py:179-180` (insert new rule before the closing `"""` of `_SAFETY_BLOCK`)
- Test: `tests/test_agent.py`

**Context:** Confirmed 3 independent times on 2026-07-10 (TSLA `quote_get`, `pine_set_source`
retry disproven by a live TradingView screenshot, `create_price_alert` retry) that when a
request is phrased as "retry X" after a prior tool call, the model sometimes fabricates a
plausible result instead of making a fresh tool call — including fabricating a fake "raw tool
result" JSON block when directly challenged to prove a result was real. A differential test
showed non-"retry" phrasing reliably triggers a real tool call. This follows the exact same
established pattern as the `ORDER PARAMETER IMMUTABILITY` and `MODIFY PARAMETER IMMUTABILITY`
rules already in `_SAFETY_BLOCK` (both added after live-testing caught the model violating an
implicit rule — see `docs/project-status.md`'s §5 bug table, "ClaudIA changed $100 limit to
$250 → rule added to system prompt").

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py`, in the "Safety block: order cancel/modify rules" section (after
`test_safety_block_at_most_one_proposal_block_per_message`, currently ending at line 201):

```python
def test_safety_block_requires_fresh_tool_call_on_retry():
    """2026-07-10 live finding: 'retry'-phrased requests sometimes skipped the actual
    tool call and fabricated a plausible result instead (confirmed 3x independently:
    a fake TSLA quote, a fake Pine Script injection disproven by a live screenshot, a
    fake alert-creation retry). This rule closes that gap explicitly."""
    prompt = _build_system_prompt("# Role\nI am ClaudIA.")
    assert "TOOL RESULT FRESHNESS" in prompt
    assert "fresh tool call" in prompt.lower()
    assert "retry" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent.py::test_safety_block_requires_fresh_tool_call_on_retry -v`
Expected: FAIL — `AssertionError: assert 'TOOL RESULT FRESHNESS' in prompt` (the rule doesn't exist yet)

- [ ] **Step 3: Add the rule to `_SAFETY_BLOCK`**

In `claudia/agent.py`, the `_SAFETY_BLOCK` string currently ends (lines 170-180):

```python
## MODIFY PARAMETER IMMUTABILITY — NON-OVERRIDABLE

Every field in an order-modify-proposal that the user did NOT ask to change must be copied
byte-for-byte (the exact value) from the latest `get_order_status` result for that order. Only
the specific field(s) the user asked to change may differ. List every changed field in
`_changed_fields` and its prior value in `_previous_values` so the confirmation dialog can show
a clear before/after diff.

You MUST NEVER change an unrequested order field when building a modify proposal. This mirrors
the ORDER PARAMETER IMMUTABILITY rule above — the user decides, you propose, they confirm.
"""
```

Replace the closing `"""` with a new rule section, so the block ends:

```python
## MODIFY PARAMETER IMMUTABILITY — NON-OVERRIDABLE

Every field in an order-modify-proposal that the user did NOT ask to change must be copied
byte-for-byte (the exact value) from the latest `get_order_status` result for that order. Only
the specific field(s) the user asked to change may differ. List every changed field in
`_changed_fields` and its prior value in `_previous_values` so the confirmation dialog can show
a clear before/after diff.

You MUST NEVER change an unrequested order field when building a modify proposal. This mirrors
the ORDER PARAMETER IMMUTABILITY rule above — the user decides, you propose, they confirm.

## TOOL RESULT FRESHNESS — NON-OVERRIDABLE

Every tool result is valid only for the turn in which it was returned. When the user asks you
to "retry", "try again", "check again", "verify", "confirm that", or otherwise re-attempt
something you already did earlier in this conversation, you MUST make a fresh tool call in
the current turn before responding — never restate, reuse, paraphrase, or reconstruct a
previous tool result as if it were newly fetched. This applies even when you are confident you
already know the answer, and even when the previous attempt failed and nothing about the
situation has visibly changed — a failed call must be genuinely retried, not assumed to still
be failing.

If the user directly asks you to prove a result came from a real tool call (e.g. "show me the
raw tool result"), you MUST either show the actual output of a tool call you just made, or
say plainly that you have not made that call — never construct a plausible-looking result and
present it as real. Fabricating a tool result, or fabricating "evidence" that a result is real,
is a more serious violation than simply not knowing the answer.

If you are about to respond to a retry/re-check/verify request without having made a tool call
in the current turn, stop and make the tool call first.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_agent.py::test_safety_block_requires_fresh_tool_call_on_retry -v`
Expected: PASS

- [ ] **Step 5: Run the full existing safety-block test suite to confirm no regressions**

Run: `.venv/bin/pytest tests/test_agent.py -v -k safety_block`
Expected: all PASS (the new rule is additive — no existing assertion should break)

- [ ] **Step 6: Commit**

```bash
git add claudia/agent.py tests/test_agent.py
git commit -m "$(cat <<'EOF'
fix: add TOOL RESULT FRESHNESS rule to close retry-fabrication gap

Live-tested 2026-07-10: requests phrased as "retry X" after a prior tool
call sometimes skipped the actual tool invocation and fabricated a
plausible result instead, confirmed 3x independently (fake TSLA quote,
fake Pine Script injection disproven by a live screenshot, fake alert
retry). Follows the same pattern as the existing ORDER/MODIFY PARAMETER
IMMUTABILITY rules added after past live-test findings.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

**Note for Task 7 (live re-verification):** this fix is a system-prompt instruction, not
deterministic code — the unit test only confirms the rule text is present in the rendered
prompt, not that the model actually obeys it every time. Task 7 re-runs the exact scenario
that found this bug (ask for a TSLA quote, then say "retry it" / "show me the raw tool
result") and confirms a real tool-call card + ≥2 API calls appear this time.

---

### Task 2: Fix `get_live_orders`/`diagnose_orders` origin field mismatch

**Repo:** `ibkr_core_mcp` (`/Users/steph/Claude_Projects/ibkr_core_mcp`)

**Files:**
- Modify: `ibkr_core_mcp/claude_tools.py:1571` (`_get_live_orders`) and `:1615` (`_diagnose_orders`)
- Test: `tests/claude_tools/test_orders.py`

**Context:** `_get_live_orders` and `_diagnose_orders` check `o.get("orderRef")` (camelCase) and
`o.get("cOID")` to detect ClaudIA-placed orders, but IBKR's documented Live Orders response field
is `order_ref` (snake_case, per `docs/superpowers/audit-evidence/scrapes/cpapi-v1.md`) — neither
checked key ever matches a real response, so every order (including ClaudIA's own) falls through
to an unreliable `clientId` check and lands on `EXTERNAL`. Empirically confirmed cosmetic
(2026-07-10: IBKR accepted a modify on an order flagged `EXTERNAL`) but it's a real usability
regression — ClaudIA correctly refuses to auto-propose modify/cancel on an `EXTERNAL`-flagged
order per its own hard rule, so the mislabel forces the user to manually confirm at the gate
every time.

- [ ] **Step 1: Write the failing tests**

Add to `tests/claude_tools/test_orders.py`, after the existing `test_diagnose_orders_shows_filtered_status` (currently ending at line 187):

```python
def test_execute_get_live_orders_labels_claudia_staged_via_order_ref(toolkit):
    """IBKR's real Live Orders field is order_ref (snake_case), not orderRef/cOID —
    2026-07-10 live finding: every order fell through to EXTERNAL because neither
    checked key ever matched a real response."""
    toolkit._client.get_live_orders.return_value = [
        {"orderId": 1, "ticker": "AAPL", "side": "BUY", "totalSize": 1,
         "price": 100.0, "status": "Submitted", "order_ref": "CLAUDIA-1783692527147"},
    ]
    text, fig = toolkit.execute("get_live_orders", {})
    assert "ClaudIA-staged" in text
    assert "EXTERNAL" not in text


def test_execute_get_live_orders_still_falls_back_to_external_without_order_ref(toolkit):
    """Regression guard: an order with no order_ref/orderRef/cOID and clientId=0 must
    still show as EXTERNAL — this is the correct behavior for genuinely external orders."""
    toolkit._client.get_live_orders.return_value = [
        {"orderId": 2, "ticker": "EEM", "side": "BUY", "totalSize": 1,
         "price": 50.0, "status": "Submitted", "clientId": 0},
    ]
    text, fig = toolkit.execute("get_live_orders", {})
    assert "EXTERNAL" in text


def test_diagnose_orders_labels_claudia_staged_via_order_ref(toolkit):
    """_diagnose_orders must use the same order_ref-first lookup as _get_live_orders —
    they were inconsistent before this fix (get_live_orders computed an origin label,
    diagnose_orders only showed raw clientId/ref with no origin at all)."""
    toolkit._client.get_orders_raw.return_value = {"orders": [
        {"orderId": 1, "ticker": "AAPL", "side": "BUY", "totalSize": 1,
         "price": 100.0, "status": "Submitted", "order_ref": "CLAUDIA-1783692527147"},
    ]}
    text, fig = toolkit.execute("diagnose_orders", {})
    assert "ClaudIA-staged" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/claude_tools/test_orders.py -k "order_ref or claudia_staged" -v`
Expected: FAIL on all 3 — `"ClaudIA-staged" not in text` (or similar) since the field lookup
doesn't check `order_ref` yet, and `_diagnose_orders` doesn't compute an origin label at all

- [ ] **Step 3: Fix `_get_live_orders`**

In `ibkr_core_mcp/claude_tools.py`, line 1571 currently reads:

```python
            order_ref = o.get("orderRef") or o.get("cOID") or o.get("clientOrderId") or ""
```

Replace with:

```python
            order_ref = (
                o.get("order_ref")  # IBKR's real Live Orders field (snake_case) — verified
                                     # against docs/superpowers/audit-evidence/scrapes/cpapi-v1.md
                or o.get("orderRef")  # kept in case IBKR ever adds a camelCase alias
                or o.get("cOID")
                or o.get("clientOrderId")
                or ""
            )
```

Also update the docstring at lines 1557-1558, which currently says:

```python
        Origin is determined from orderRef prefix ('CLAUDIA-' = ClaudIA-staged) rather than
        clientId, which is unreliable - both CP API and mobile orders can show clientId=0.
```

to:

```python
        Origin is determined from the order_ref prefix ('CLAUDIA-' = ClaudIA-staged) rather
        than clientId, which is unreliable — both CP API and mobile orders can show clientId=0.
        order_ref is IBKR's real Live Orders field (snake_case); orderRef/cOID/clientOrderId
        are kept as fallbacks only (see docs/project-status.md Known Gaps, found 2026-07-10).
```

- [ ] **Step 4: Fix `_diagnose_orders` to compute and display the same origin label**

`_diagnose_orders` (lines 1590-1624) currently has no origin computation at all — only raw
`clientId`/`ref`/`status`/`[FILTERED]`. Line 1615 currently reads:

```python
            order_ref = o.get("orderRef") or o.get("cOID") or ""
```

Replace lines 1615-1621 (the `order_ref`/`client_id`/`lines.append` block) with:

```python
            order_ref = (
                o.get("order_ref") or o.get("orderRef") or o.get("cOID") or ""
            )
            client_id = o.get("clientId", "absent")
            if order_ref.startswith("CLAUDIA-"):
                origin = "ClaudIA-staged"
            elif client_id not in (0, "0", "absent", None):
                origin = f"API (clientId={client_id})"
            else:
                origin = "EXTERNAL"
            lines.append(
                f"orderId={o.get('orderId')} ticker={o.get('ticker', o.get('symbol'))} "
                f"side={o.get('side')} qty={o.get('totalSize')} price={o.get('price')} "
                f"status={status} origin={origin} clientId={client_id} "
                f"ref={order_ref or 'none'}{filtered}"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/claude_tools/test_orders.py -v`
Expected: all PASS, including the 3 new tests and every pre-existing test in the file (this
confirms the fallback chain still covers the old field names and the filtered-status logic
is untouched)

- [ ] **Step 6: Run the full ibkr_core_mcp unit suite to confirm no wider regressions**

Run: `.venv/bin/pytest -m "not integration" -q`
Expected: all PASS, same count as before plus 3

- [ ] **Step 7: Commit**

```bash
git add ibkr_core_mcp/claude_tools.py tests/claude_tools/test_orders.py
git commit -m "$(cat <<'EOF'
fix: get_live_orders/diagnose_orders check order_ref, not orderRef/cOID

IBKR's documented Live Orders field is order_ref (snake_case), but both
tools checked orderRef/cOID (camelCase) — neither ever matched a real
response, so every order including ClaudIA's own fell through to an
unreliable clientId check and was mislabeled EXTERNAL. Confirmed live
2026-07-10 (empirically cosmetic — IBKR accepted a modify on a
mislabeled order — but a real usability regression: ClaudIA correctly
refuses to auto-propose modify/cancel on an EXTERNAL-flagged order).
Also brings diagnose_orders' origin logic in line with get_live_orders,
which it never had before.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Fix Gate 2 cancel dialog missing order details

**Repos:** `ibkr_core_mcp` (dialog + client) and `claudia_ui` (call site) — do this task in
`ibkr_core_mcp` first, then `claudia_ui`, since `claudia_ui` depends on the new `ibkr_core_mcp`
signature.

**Files:**
- Modify: `ibkr_core_mcp/order_confirm.py:85-93` (`confirm_cancel_dialog`)
- Modify: `ibkr_core_mcp/client.py:1126-1139` (`cancel_order`)
- Modify: `claudia_ui/claudia/order_flow.py:455` (`execute_cancel_order`)
- Test: `ibkr_core_mcp/tests/test_order_confirm.py`, `ibkr_core_mcp/tests/test_client.py`, `claudia_ui/tests/test_order_flow.py`

**Context:** User-flagged hard requirement, found live 2026-07-10. `confirm_cancel_dialog`
only ever displays `{"Order ID": ..., "Account": ...}` — no symbol/side/qty/order
type/price/TIF — unlike `confirm_order_dialog` (place) and `confirm_modify_dialog` (modify),
which both receive and display the full order dict. `order_flow.py`'s
`execute_cancel_order()` already has the full proposal in hand; it's just never passed
through `cancel_order()`'s signature.

#### Part A — `ibkr_core_mcp`

- [ ] **Step 1: Write the failing tests**

Add to `ibkr_core_mcp/tests/test_order_confirm.py`, after the existing
`test_confirm_cancel_dialog_passes_order_id` (currently ending at line 210):

```python
def test_confirm_cancel_dialog_shows_order_details_when_provided():
    """User-flagged hard requirement, 2026-07-10: the cancel dialog must show full
    order details (symbol/side/qty/price/TIF), not just an opaque order ID — mirrors
    what confirm_modify_dialog already does."""
    with patch("ibkr_core_mcp.order_confirm._show_confirm_dialog") as mock_show:
        from ibkr_core_mcp.order_confirm import confirm_cancel_dialog
        confirm_cancel_dialog(
            "ORD456", "U1234567",
            {"symbol": "AAPL", "side": "BUY", "quantity": 1, "orderType": "LMT", "price": 100.0},
        )
    kwargs = mock_show.call_args.kwargs
    assert kwargs["details"]["Order ID"] == "ORD456"
    assert kwargs["details"]["Account"] == "U1234567"
    assert kwargs["details"]["symbol"] == "AAPL"
    assert kwargs["details"]["side"] == "BUY"
    assert kwargs["details"]["price"] == "100.0"
```

Add to `ibkr_core_mcp/tests/test_client.py`, after the existing
`test_cancel_order_calls_both_gates` (currently ending at line 248):

```python
def test_cancel_order_passes_order_details_to_dialog(client):
    captured = {}
    with _patch("ibkr_core_mcp.client.require_touch_id"), \
         _patch(
             "ibkr_core_mcp.client.confirm_cancel_dialog",
             side_effect=lambda o_id, a, order=None: captured.update(order or {}),
         ), \
         _patch.object(client._session, "delete") as mock_del:
        mock_del.return_value = _make_ok_response({"status": "cancelled"})
        client.cancel_order(
            "U1234567", "ORD456", order_details={"symbol": "AAPL", "side": "SELL"}
        )
    assert captured == {"symbol": "AAPL", "side": "SELL"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_order_confirm.py::test_confirm_cancel_dialog_shows_order_details_when_provided tests/test_client.py::test_cancel_order_passes_order_details_to_dialog -v`
Expected: FAIL — `test_confirm_cancel_dialog_shows_order_details_when_provided` fails with
`TypeError: confirm_cancel_dialog() takes 2 positional arguments but 3 were given`;
`test_cancel_order_passes_order_details_to_dialog` fails with `TypeError: cancel_order() got
an unexpected keyword argument 'order_details'`

- [ ] **Step 3: Fix `confirm_cancel_dialog`**

In `ibkr_core_mcp/order_confirm.py`, lines 85-93 currently read:

```python
def confirm_cancel_dialog(order_id: str, account_id: str) -> None:
    """Gate 2 for cancel_order."""
    _show_confirm_dialog(
        title="⚠  CANCEL ORDER CONFIRMATION",
        details={"Order ID": order_id, "Account": account_id},
        disclaimer="This will CANCEL a live order at Interactive Brokers.",
        confirm_label="CANCEL ORDER",
    )
```

Replace with:

```python
def confirm_cancel_dialog(
    order_id: str, account_id: str, order: dict[str, Any] | None = None
) -> None:
    """Gate 2 for cancel_order.

    `order` is optional display-only detail (symbol/side/qty/price/TIF/etc.) so the human
    can visually verify which order they're cancelling — mirrors confirm_modify_dialog's
    pattern. None preserves the old order-id-only dialog for any caller without full order
    detail available. Found missing live 2026-07-10 — user-flagged hard requirement.
    """
    details: dict[str, Any] = {}
    if order:
        details.update(
            {k: str(v) for k, v in order.items() if k not in ("Order ID", "Account")}
        )
    details["Order ID"] = order_id
    details["Account"] = account_id
    _show_confirm_dialog(
        title="⚠  CANCEL ORDER CONFIRMATION",
        details=details,
        disclaimer="This will CANCEL a live order at Interactive Brokers.",
        confirm_label="CANCEL ORDER",
    )
```

- [ ] **Step 4: Fix `cancel_order`**

In `ibkr_core_mcp/client.py`, lines 1126-1139 currently read:

```python
    def cancel_order(self, account_id: str, order_id: str) -> dict[str, Any]:
        """Cancel an order. Requires Touch ID (Gate 1) + tkinter confirmation dialog (Gate 2).

        Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#cancel-order
                https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/
        Endpoint: DELETE /iserver/account/{accountId}/order/{orderId}
        """
        _validate_account_id(account_id)
        self._ensure_accounts_initialized()
        require_touch_id(f"IBKR: Cancel order {order_id}")
        confirm_cancel_dialog(order_id, account_id)
        url = f"{self._base}/iserver/account/{account_id}/order/{order_id}"
        resp = with_retry(lambda: self._session.delete(url, timeout=30))
        return resp.json()
```

Replace with:

```python
    def cancel_order(
        self, account_id: str, order_id: str, order_details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Cancel an order. Requires Touch ID (Gate 1) + tkinter confirmation dialog (Gate 2).

        `order_details` is optional display-only info (symbol/side/qty/price/TIF/etc.) shown
        in the Gate 2 dialog so the human can verify the right order before cancelling —
        mirrors modify_order()'s dialog, which already receives the full order dict. Found
        missing live 2026-07-10 — user-flagged hard requirement.

        Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#cancel-order
                https://www.interactivebrokers.com/campus/trading-lessons/request-modify-orders/
        Endpoint: DELETE /iserver/account/{accountId}/order/{orderId}
        """
        _validate_account_id(account_id)
        self._ensure_accounts_initialized()
        require_touch_id(f"IBKR: Cancel order {order_id}")
        confirm_cancel_dialog(order_id, account_id, order_details)
        url = f"{self._base}/iserver/account/{account_id}/order/{order_id}"
        resp = with_retry(lambda: self._session.delete(url, timeout=30))
        return resp.json()
```

- [ ] **Step 5: Fix the two existing mocks broken by the new positional parameter**

In `ibkr_core_mcp/tests/test_client.py`, `test_cancel_order_calls_both_gates` (line 243) and
`test_cancel_order_aborts_if_dialog_cancelled` (line 275) both patch `confirm_cancel_dialog`
with a `side_effect` lambda taking exactly 2 args (`o_id, a`). Since `cancel_order` now always
calls `confirm_cancel_dialog(order_id, account_id, order_details)` positionally (3 args), both
lambdas need a 3rd parameter. Change line 243 from:

```python
         _patch("ibkr_core_mcp.client.confirm_cancel_dialog", side_effect=lambda o_id, a: call_order.append("dialog")), \
```

to:

```python
         _patch("ibkr_core_mcp.client.confirm_cancel_dialog", side_effect=lambda o_id, a, order=None: call_order.append("dialog")), \
```

And line 275 from:

```python
         _patch("ibkr_core_mcp.client.confirm_cancel_dialog", side_effect=HumanAuthError("cancelled")), \
```

This one raises via `side_effect` with an exception object (not a lambda), so it already
accepts any call signature — **no change needed** for line 275.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_order_confirm.py tests/test_client.py -v -k cancel`
Expected: all PASS

- [ ] **Step 7: Run the full ibkr_core_mcp unit suite**

Run: `.venv/bin/pytest -m "not integration" -q`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add ibkr_core_mcp/order_confirm.py ibkr_core_mcp/client.py tests/test_order_confirm.py tests/test_client.py
git commit -m "$(cat <<'EOF'
fix: Gate 2 cancel dialog now shows full order details, not just ID

confirm_cancel_dialog only ever displayed Order ID + Account, unlike
confirm_order_dialog (place) and confirm_modify_dialog (modify), both
of which show the full order dict. User-flagged hard requirement,
found live 2026-07-10 — the whole point of Gate 2 is letting the human
visually verify the right order before committing, which an opaque
order ID doesn't satisfy. cancel_order() gains an optional
order_details param threaded through to the dialog.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

#### Part B — `claudia_ui` (depends on Part A)

- [ ] **Step 9: Re-install ibkr_core_mcp so claudia_ui picks up the signature change**

Run (from `claudia_ui`'s repo root): `pip install -e "../ibkr_core_mcp"`
Expected: reinstalls cleanly, no errors (this is an editable install — CLAUDE.md notes no
restart is needed for tool-schema changes, but this is a Python signature change, so any
running ClaudIA process must be restarted after this task's Step 12)

- [ ] **Step 10: Write the failing test**

In `claudia_ui/tests/test_order_flow.py`, the existing test at lines 724-729 currently reads:

```python
@pytest.mark.asyncio
async def test_execute_cancel_order_calls_client_with_account_and_order_id():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    action = _make_cancel_action({"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"})
    await _run_cancel(action, ibkr_mod)
    client.cancel_order.assert_called_once_with("U12345", "555")
```

Replace the final assertion line with:

```python
@pytest.mark.asyncio
async def test_execute_cancel_order_calls_client_with_account_and_order_id():
    ibkr_mod, client = _make_cancel_modify_ibkr_mock()
    proposal = {"order_id": "555", "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"}
    action = _make_cancel_action(proposal)
    await _run_cancel(action, ibkr_mod)
    client.cancel_order.assert_called_once_with("U12345", "555", order_details=proposal)
```

- [ ] **Step 11: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_order_flow.py::test_execute_cancel_order_calls_client_with_account_and_order_id -v`
Expected: FAIL — `AssertionError: Expected call: cancel_order('U12345', '555',
order_details={...})` vs actual call `cancel_order('U12345', '555')`

- [ ] **Step 12: Fix `execute_cancel_order`**

In `claudia_ui/claudia/order_flow.py`, line 455 currently reads:

```python
        result = ibkr.cancel_order(account_id, order_id)
```

Replace with:

```python
        result = ibkr.cancel_order(account_id, order_id, order_details=proposal)
```

- [ ] **Step 13: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_order_flow.py::test_execute_cancel_order_calls_client_with_account_and_order_id -v`
Expected: PASS

- [ ] **Step 14: Run the full claudia_ui unit suite**

Run: `.venv/bin/pytest -m "not integration" -q`
Expected: all PASS, same count as before

- [ ] **Step 15: Commit**

```bash
git add claudia/order_flow.py tests/test_order_flow.py
git commit -m "$(cat <<'EOF'
fix: pass full cancel proposal into cancel_order's Gate 2 dialog

Completes the ibkr_core_mcp side of the Gate 2 cancel-dialog fix —
order_flow.py already had the full proposal (symbol/qty/price/etc.)
in hand, it just never reached cancel_order()'s new order_details
param. Depends on the ibkr_core_mcp fix in the prior commit; run
`pip install -e "../ibkr_core_mcp"` before testing this.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

**Note for Task 7 (live re-verification):** this is the kind of fix that most needs a real
Gate 2 dialog screenshot, not just a passing unit test — a mocked test can't catch a
formatting bug in how the AppKit dialog actually renders a `float` or `None` value. Task 7
re-stages a disposable order, proposes a cancel, and screenshots the actual Gate 2 dialog
before clicking through.

---

### Task 4: Add freshness guard to `GDriveSync.read_text`

**Repo:** `claudia_ui`

**Files:**
- Modify: `claudia/gdrive_sync.py:327-360` (`read_text`)
- Modify: `claudia/app.py:436-437` (the two `read_text` call sites)
- Test: `tests/test_gdrive_sync.py`

**Context:** `download_db` has a documented freshness guard (an older Drive copy never
overwrites a newer local DB), but `read_text` (used for `context.md`/`principles.md`) has no
guard at all — it unconditionally downloads whatever is on Drive every session start. Found
live 2026-07-10: with the GDrive OAuth token flapping (failed at process boot, succeeded ~27
min later on a second `on_chat_start`), two sessions in the same running process resolved to
different doc versions from the same unedited local files (v3 correct at boot from local
fallback, v1 — a stale June 11 Drive copy — once Drive briefly reconnected). Local
`context.md` was edited after whatever's sitting in Drive, so any future session where Drive
succeeds silently reverts ClaudIA's persona to 6-week-old content. Does not affect trading
rules — `principles.md` is byte-identical across all three registered versions — but is a
real data-integrity gap.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_gdrive_sync.py`, after the existing `read_text` tests (`test_read_text_returns_none_when_not_on_drive` at line 148, `test_read_text_returns_content` at line 154):

```python
def test_read_text_skips_when_local_not_older_than_drive(sync, tmp_path):
    """A stale Drive copy must not silently override a local context.md/principles.md
    edit that was never re-uploaded — the same gap download_db already guards against
    for claudia.db. Found live 2026-07-10 (v3 local vs v1 stale Drive copy)."""
    local_file = tmp_path / "context.md"
    local_file.write_text("local content")  # mtime = now

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "13", "modifiedTime": "2020-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc):
        result = sync.read_text("context.md", local_path=local_file)

    assert result is None


def test_read_text_proceeds_when_drive_newer(sync, tmp_path):
    """The guard must not block legitimate updates uploaded from another machine."""
    local_file = tmp_path / "context.md"
    local_file.write_text("stale local content")

    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(b"fresh drive content")
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "20", "modifiedTime": "2099-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md", local_path=local_file)

    assert result == "fresh drive content"


def test_read_text_without_local_path_downloads_unconditionally(sync):
    """Backward compatibility: a caller that doesn't pass local_path gets the old
    unconditional-download behavior (no local file to compare against)."""
    class FakeDownloader:
        def __init__(self, buf, _req):
            buf.write(b"drive content")
        def next_chunk(self):
            return None, True

    svc = MagicMock()
    svc.files.return_value.get.return_value.execute.return_value = {
        "size": "13", "modifiedTime": "2026-01-01T00:00:00.000Z"
    }
    with patch.object(sync, "_find_file", return_value="file-id"), \
         patch.object(sync, "_get_service", return_value=svc), \
         patch("claudia.gdrive_sync.MediaIoBaseDownload", FakeDownloader):
        result = sync.read_text("context.md")

    assert result == "drive content"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_gdrive_sync.py -k "read_text_skips or read_text_proceeds_when_drive_newer or read_text_without_local_path" -v`
Expected: FAIL — `TypeError: read_text() got an unexpected keyword argument 'local_path'`

- [ ] **Step 3: Add the freshness guard to `read_text`**

In `claudia/gdrive_sync.py`, lines 327-360 currently read:

```python
    def read_text(self, filename: str) -> str | None:
        """Download a text file (e.g. "context.md") from Drive.

        Returns content string, or None if not found or on any error.

        files().get(fields="size") fetches only the file's metadata size field — avoids
        downloading the content twice. The 1 MB guard prevents a runaway context.md from
        bloating the system prompt.

        Source (files.get): https://developers.google.com/drive/api/reference/rest/v3/files/get
        Source (files.get_media): https://developers.google.com/drive/api/reference/rest/v3/files/get
        """
        try:
            svc = self._get_service()
            file_id = self._find_file(filename)
            if file_id is None:
                return None
            meta = svc.files().get(fileId=file_id, fields="size").execute()
            size = int(meta.get("size", 0))
            if size > self._MAX_TEXT_BYTES:
                log.warning(
                    "GDriveSync.read_text(%r): file is %d bytes (limit %d) — skipping",
                    filename, size, self._MAX_TEXT_BYTES,
                )
                return None
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
            self._download_chunked(downloader)
            return buf.getvalue().decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning(
                "GDriveSync.read_text(%r) failed: %s — using local fallback", filename, exc
            )
            return None
```

Replace with:

```python
    def read_text(self, filename: str, local_path: Path | None = None) -> str | None:
        """Download a text file (e.g. "context.md") from Drive.

        Returns content string, or None if not found, if Drive's copy is not newer than
        the local file (freshness guard), or on any error.

        Freshness guard: if local_path is given and exists, Drive's copy is skipped when
        its modifiedTime is not newer than the local file's mtime — mirrors download_db's
        identical guard for claudia.db. Closes a gap found live 2026-07-10: with no guard,
        a stale Drive copy could silently overwrite a newer local context.md/principles.md
        edit that was never re-uploaded, reverting ClaudIA's persona without warning.

        files().get(fields="size,modifiedTime") fetches only file metadata — avoids
        downloading the content twice. The 1 MB guard prevents a runaway context.md from
        bloating the system prompt.

        Source (files.get): https://developers.google.com/drive/api/reference/rest/v3/files/get
        Source (files.get_media): https://developers.google.com/drive/api/reference/rest/v3/files/get
        """
        try:
            svc = self._get_service()
            file_id = self._find_file(filename)
            if file_id is None:
                return None
            meta = svc.files().get(fileId=file_id, fields="size,modifiedTime").execute()
            if local_path is not None and local_path.exists():
                drive_mtime = datetime.fromisoformat(meta["modifiedTime"])
                local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc)
                if local_mtime >= drive_mtime:
                    log.warning(
                        "GDriveSync.read_text(%r): Drive copy (%s) is not newer than local "
                        "(%s) — keeping local",
                        filename, drive_mtime.isoformat(), local_mtime.isoformat(),
                    )
                    return None
            size = int(meta.get("size", 0))
            if size > self._MAX_TEXT_BYTES:
                log.warning(
                    "GDriveSync.read_text(%r): file is %d bytes (limit %d) — skipping",
                    filename, size, self._MAX_TEXT_BYTES,
                )
                return None
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
            self._download_chunked(downloader)
            return buf.getvalue().decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning(
                "GDriveSync.read_text(%r) failed: %s — using local fallback", filename, exc
            )
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_gdrive_sync.py -k "read_text" -v`
Expected: all PASS, including the 2 pre-existing `read_text` tests (unaffected — they don't
pass `local_path`, so they exercise the unconditional-download path) and the 3 new ones

- [ ] **Step 5: Wire `local_path` through at the call sites**

In `claudia/app.py`, lines 436-437 currently read:

```python
        drive_context = _gdrive_sync.read_text("context.md")
        drive_principles = _gdrive_sync.read_text("principles.md")
```

Replace with:

```python
        drive_context = _gdrive_sync.read_text("context.md", local_path=_DOCS_PATH / "context.md")
        drive_principles = _gdrive_sync.read_text("principles.md", local_path=_DOCS_PATH / "principles.md")
```

(`_DOCS_PATH` is already a module-level `Path` constant, defined at `app.py:258`.)

- [ ] **Step 6: Run the full claudia_ui unit suite**

Run: `.venv/bin/pytest -m "not integration" -q`
Expected: all PASS, same count as before plus 3

- [ ] **Step 7: Commit**

```bash
git add claudia/gdrive_sync.py claudia/app.py tests/test_gdrive_sync.py
git commit -m "$(cat <<'EOF'
fix: add freshness guard to GDriveSync.read_text for context/principles

download_db has a documented freshness guard (an older Drive copy never
overwrites a newer local claudia.db), but read_text (context.md/
principles.md) had none — it downloaded Drive's copy unconditionally
every session start. Found live 2026-07-10: a flapping GDrive OAuth
token caused two sessions in one process to resolve to different doc
versions from the same unedited local files, because Drive's stale
June 11 copy silently overrode the current local context.md once Drive
briefly reconnected. read_text now takes an optional local_path and
skips the Drive copy when it isn't newer than local, mirroring
download_db exactly.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

**Separately, not part of this plan (manual action, not code):** the actual Drive copies of
`context.md`/`principles.md` are still stale (last matching v1, June 11) — once this fix
lands, re-upload the current local `docs/context.md`/`docs/principles.md` to the Drive root
folder via the Drive web UI so Drive and local agree going forward. This plan's fix prevents
the *silent* overwrite; it doesn't retroactively fix Drive's stale content.

---

### Task 5: Diagnose `create_price_alert` HTTP 403 (live, diagnostic-first)

**Repo:** `ibkr_core_mcp`

**Files:**
- Investigate: `ibkr_core_mcp/claude_tools.py:2341-2390` (`_create_price_alert`), `ibkr_core_mcp/client.py:1283-1293` (`create_alert`)
- Fix: TBD based on findings — do not write speculative fix code before Step 3's evidence

**Context:** Confirmed live 2026-07-10 via 2 independent real tool calls (proper tool-call
cards, real API round-trips — not the Task 1 fabrication pattern) that `create_price_alert`
fails with HTTP 403 from IBKR on every attempt. Order writes (`place_order`, `modify_order`,
`cancel_order`) all succeeded earlier in the same session on the same gateway session, so this
looks specific to the alerts endpoint's auth/permission scope rather than a general session
problem — but this was never actually investigated (time-boxed out of the original test
session). Per this repo's own "docs first" convention (`CLAUDE.md`'s "API Reference — Docs
First" section — two past bugs went undetected for months because nobody checked official
docs first), do not guess at a fix; gather the real error body and check the official
endpoint docs before writing any code.

- [ ] **Step 1: Confirm the IBKR gateway is up and authenticated**

Run: `curl -sk https://localhost:5055/v1/api/tickle`
Expected: JSON body with `"authStatus": {"authenticated": true, "connected": true, ...}`.
If not authenticated, open `https://localhost:5055` in a browser and log in + complete 2FA
before continuing — every step below needs a live, authenticated gateway.

- [ ] **Step 2: Reproduce the 403 directly with `IBKRClient`, capturing the real error body**

Run this from `ibkr_core_mcp`'s repo root (adjust the venv path if different):

```bash
.venv/bin/python3 -c "
from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
import os

config = Config.from_env()
client = IBKRClient(config=config, auth=BrowserCookieAuth(os.environ.get('IBKR_AUTH_BROWSER', 'chrome')))
accounts = client.get_accounts()
account_id = accounts[0]['accountId']
print('account_id:', account_id)

alert = {
    'orderId': 0,
    'alertName': 'DIAGNOSTIC TEST — delete me',
    'alertMessage': '',
    'alertRepeatable': 0,
    'expireTime': '',
    'tif': 'GTC',
    'outsideRth': False,
    'isSizeCondition': False,
    'conditions': [{
        'type': 1,
        'conid': 265598,  # AAPL — already known-good from tonight's order tests
        'exchange': 'SMART',
        'conditionType': 'Price',
        'operator': '<=',
        'value': '200',
    }],
}

try:
    result = client.create_alert(account_id, alert)
    print('SUCCESS:', result)
except Exception as e:
    print('EXCEPTION TYPE:', type(e).__name__)
    print('EXCEPTION STR:', str(e))
"
```

Expected: either a genuine success this time (transient — go to Step 5 and note "transient,
did not reproduce, re-verify at least twice more before concluding fixed"), or an exception
whose `str(e)` includes the actual HTTP response body (per `rate_limiter.py`'s `with_retry`,
which raises `IBKRAPIError` with `resp.text[:400]` on 4xx/5xx) — **this response body is the
actual evidence needed**; do not proceed to a fix without reading it.

- [ ] **Step 3: Read the official IBKR docs for the alerts endpoint's requirements**

Use `WebFetch` (per this repo's "docs first" convention) on:
`https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#create-alert` (or search
the CP API reference for "alert" if the anchor has changed) — confirm what account
entitlement, permission, or request-body requirement the endpoint documents, and compare
against the exact error body captured in Step 2.

- [ ] **Step 4: Check whether other alert operations (read/list) work on the same session**

Run: `.venv/bin/python3 -c "
from ibkr_core_mcp import BrowserCookieAuth, Config, IBKRClient
import os
config = Config.from_env()
client = IBKRClient(config=config, auth=BrowserCookieAuth(os.environ.get('IBKR_AUTH_BROWSER', 'chrome')))
accounts = client.get_accounts()
print(client.get_alerts(accounts[0]['accountId']))
"`

If `get_alerts` (a GET) succeeds while `create_alert` (a POST) 403s, that's further evidence
this is specifically a write-permission/entitlement gap on the alerts endpoint, not a general
auth problem — narrows the fix to either a missing account entitlement (user-side, not
code-side) or a request-body field IBKR's docs require that the current `alert` dict is
missing.

- [ ] **Step 5: Based on findings, write the fix**

This step is intentionally not pre-written — the exact fix depends on what Steps 2-4 reveal.
Plausible outcomes and their likely next action, to be confirmed (not assumed) against the
Step 2 error body and Step 3 docs before implementing:
  - **Error body says a specific field is missing/malformed** → fix the `alert` dict
    construction in `_create_price_alert` (`claude_tools.py:2369-2387`) to match the documented
    schema, add a regression test with the corrected body, verify live.
  - **Error body indicates a permission/entitlement issue** (e.g. "not entitled", "feature not
    available") → this is an account-level configuration gap, not a code bug — document it
    plainly in `docs/project-status.md` Known Gaps as "requires IBKR account-side alert
    entitlement" rather than writing a code fix, and tell the user what to check/enable on
    their account.
  - **Genuinely transient / gateway session issue** → document as intermittent, no code change,
    re-test in Task 7.

- [ ] **Step 6: If a code fix was made, write a regression test, then commit**

Only applicable if Step 5 produced an actual code change. Follow this repo's existing test
style in `tests/claude_tools/test_orders.py`/`test_alerts.py` (mock `client.create_alert` to
return a success payload, assert the returned text reflects it) — write the specific test
once the real fix is known; it cannot be pre-written here without knowing what changed.

```bash
git add ibkr_core_mcp/claude_tools.py tests/claude_tools/test_alerts.py  # adjust paths to what actually changed
git commit -m "fix: <describe the actual root cause found in Step 2-4>

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Diagnose `ExecutionListener`'s unexplained `RuntimeError` (live, diagnostic-first)

**Repo:** `claudia_ui`

**Files:**
- Modify: `claudia/execution_listener.py:149-154` (add `exc_info=True` — safe, low-risk, do this regardless of what Step 2 finds)
- Investigate: `claudia/app.py:29-226` (5 existing Python 3.14/anyio compat patches, for pattern reference only)
- Test: `tests/test_execution_listener.py`

**Context:** On every ClaudIA startup, `ExecutionListener._run_once()` immediately raises a
bare `RuntimeError` and backs off per the documented 5/10/30/60s schedule — logged only as
`type(exc).__name__`, no traceback, so the real cause has never actually been seen. Reproduced
`BrowserCookieAuth(...).apply()` + `IBKRWebSocket.connect()`/`subscribe_executions()`
standalone outside Chainlit and both worked fine, so this is scoped to running inside
Chainlit/uvicorn's asyncio context. Corroborating evidence found live 2026-07-10: a browser
reconnect captured a full traceback for the *same* "Timeout should be used inside a task"
signature, but in a **different** code path (`websockets.legacy.server`'s opening handshake,
which handles incoming browser connections — not necessarily the same path as
`IBKRWebSocket`'s outgoing connection to IBKR's gateway). This is suggestive, not proof —
do not assume the same fix applies without seeing `ExecutionListener`'s own real traceback
first.

- [ ] **Step 1: Write the failing test for traceback logging**

Add to `tests/test_execution_listener.py` (check whether `import logging` already exists near
the top of the file — add it if not), after `test_run_with_retry_retries_on_error_then_cancels`
(currently ending at line 308):

```python
@pytest.mark.asyncio
async def test_run_with_retry_logs_traceback_on_error(caplog):
    """The previous type(exc).__name__-only logging hid the real cause of a repeating
    RuntimeError for a full live-test session (2026-07-10) before this fix — exc_info=True
    is required so the actual traceback is captured, not just the exception class name."""
    listener, _ = _make_listener()
    call_count = 0

    async def fail_then_cancel():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Timeout should be used inside a task")
        raise asyncio.CancelledError

    with patch.object(listener, "_run_once", side_effect=fail_then_cancel), \
         patch("claudia.execution_listener.asyncio.sleep", new=AsyncMock()), \
         caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await listener._run_with_retry()

    assert any(r.exc_info is not None for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_execution_listener.py::test_run_with_retry_logs_traceback_on_error -v`
Expected: FAIL — `assert any(...)` is `False` since no log record currently carries `exc_info`

- [ ] **Step 3: Add `exc_info=True` to the log call**

In `claudia/execution_listener.py`, lines 149-154 currently read:

```python
            except Exception as exc:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                log.warning(
                    "ExecutionListener error (attempt %d), retrying in %ds: %s",
                    attempt + 1, delay, type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt += 1
```

Replace with:

```python
            except Exception as exc:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                log.warning(
                    "ExecutionListener error (attempt %d), retrying in %ds: %s",
                    attempt + 1, delay, type(exc).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                attempt += 1
```

- [ ] **Step 4: Run test to verify it passes, then run the full suite**

Run: `.venv/bin/pytest tests/test_execution_listener.py -v`
Expected: all PASS (this is a pure logging addition — no behavior change, so all 4 pre-existing
`_run_with_retry` tests are unaffected)

Run: `.venv/bin/pytest -m "not integration" -q`
Expected: all PASS

- [ ] **Step 5: Commit the logging fix on its own — it's useful regardless of what Step 6 finds**

```bash
git add claudia/execution_listener.py tests/test_execution_listener.py
git commit -m "$(cat <<'EOF'
fix: log full traceback for ExecutionListener's swallowed RuntimeError

_run_with_retry logged only type(exc).__name__ on every failure, never
a traceback — so the real cause of a bare RuntimeError firing on every
ClaudIA startup (since at least 2026-07-10, likely earlier) was never
actually seen, only guessed at by analogy to a similar-looking error
elsewhere. This is diagnostic-only; the actual root-cause fix (if any
is needed beyond this) follows once a real startup is observed with
this logging in place.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Restart ClaudIA and capture the real traceback**

Run: `./start-claudia.sh` (or your normal dev-run command), then within a few seconds check
the log:

```bash
grep -A 30 "ExecutionListener error (attempt 1)" <path-to-claudia-log> | head -35
```

Expected: a full Python traceback this time, not just the one-line warning.

- [ ] **Step 7: Compare the traceback against the 5 existing Python 3.14/anyio patches**

Read `claudia/app.py:29-226` for the 5 existing patches' shape (banner comment → `_orig_X =
X` capture → `_X_compat` function → `X = _X_compat` reassignment). If the newly-captured
traceback shows the same root shape as those patches address — `asyncio.current_task()` is
`None` inside `asyncio.timeout()`, `asyncio.wait_for()`, or an `anyio` internal, surfacing as
`RuntimeError: Timeout should be used inside a task`, `TypeError`, or `AssertionError` — this
confirms the hypothesis and a 6th patch is warranted. **If the traceback shows something
different** (a different exception type, a different library, a different root cause
entirely), **stop here** — do not apply an anyio/asyncio patch that doesn't match real
evidence. Write up what was actually found in `docs/project-status.md`'s Known Gaps entry for
this item and treat it as a new, separately-scoped bug requiring its own investigation.

- [ ] **Step 8: If confirmed, design and implement the 6th patch — do this as its own
  follow-up task once the real traceback is in hand, not blind from this plan.** Given the
  complexity and risk of a custom `asyncio.timeout()`-compatibility shim (it must correctly
  preserve cancellation semantics, not just raise `TimeoutError` after the fact — a naive
  implementation could silently let long-running operations continue past their timeout),
  treat this as requiring its own short design pass with the real traceback in hand, mirroring
  how patches #1-#5 were each added individually as the specific failure was understood. Do
  not copy a template blindly.

---

### Task 7: Live re-verification pass (before starting the next test batch)

**Context:** Several of the fixes above (Task 1's system-prompt rule, Task 3's Gate 2 dialog
formatting) can't be fully verified by a unit test alone — they need the same live,
independently-verified protocol used to find these bugs in the first place: tool-call UI
cards, server-log API-call counts, and (for TradingView) direct screenshots, not chat text
taken at face value.

- [ ] **Step 1: Restart ClaudIA** with all of Tasks 1-4 (and any code from Tasks 5-6) merged,
  confirm `pip install -e "../ibkr_core_mcp"` was re-run so the `ibkr_core_mcp` changes are
  picked up.

- [ ] **Step 2: Re-run the Task 1 scenario** — ask for a live quote (e.g. "What's the current
  price of TSLA?"), then say "retry it" or "show me the raw tool result." Confirm a real
  tool-call card appears and the server log shows ≥2 API calls for that turn (same detection
  method as the original finding).

- [ ] **Step 3: Re-run the Task 2 scenario** — place one disposable test order (mirroring
  Batch 1's protocol: a GTC limit far below market), call `get_live_orders`, confirm the
  order now shows `origin=ClaudIA-staged`, not `EXTERNAL`.

- [ ] **Step 4: Re-run the Task 3 scenario** — propose cancelling that same order, click
  "Cancel this order," and **screenshot the actual Gate 2 AppKit dialog** before confirming —
  verify it now shows symbol/side/qty/price/TIF, not just Order ID + Account. Then confirm
  the cancel and verify it's gone from `get_live_orders`, closing out the disposable order
  cleanly (zero open test orders at the end, per this repo's established test-order hygiene).

- [ ] **Step 5: Re-run the Task 4 scenario** — with GDrive reachable, restart ClaudIA twice in
  a row and confirm both sessions resolve to the same doc version (no v3→v1 flapping).

- [ ] **Step 6: Re-run whatever fix emerged from Task 5** (if any code changed) — attempt
  `create_price_alert` again live and confirm success, or confirm the documented
  account-entitlement gap is still accurately described if no code fix was possible.

- [ ] **Step 7: Re-run whatever fix emerged from Task 6** (if any code changed) — restart
  ClaudIA and confirm `ExecutionListener` connects cleanly with no repeating `RuntimeError`
  in the log.

- [ ] **Step 8: Update `docs/project-status.md`** — mark each Known Gap resolved with the
  real date and evidence (mirroring how Batch 1's findings were closed out on 2026-07-10),
  and update the Live Test Log with a new row for this fix-verification session.

- [ ] **Step 9: Commit the docs update, then proceed to the next live test batch** (TradingView
  drag/paste screenshot vision test, remaining Price Alerts scenarios, or whatever's next per
  `docs/project-status.md`'s "Next Session Plan").
