# Futures order path — two bugs found in first-ever live FUT test (2026-07-23)

**Context:** First live test of the futures order-staging path (STK BUY/MODIFY had been
validated; FUT/FOP never had been). Done on the Panel app against the live IBKR gateway.
Both bugs are in `claudia/order_flow.py`, which is **shared code** (Chainlit + Panel) — not
Panel-migration-specific. The staging/UI/gate chain itself worked perfectly (proposal
rendered with conid + sec_type FUT, Gate 1 Touch ID + Gate 2 SEND TO IBKR both fired,
`place_order` was called). The failures are in the order **body** and the **result
handling**.

Test order: `BUY 1 ES SEP2026 LMT 6000 GTC` (conid 649180671, ~19% below market — a safe
non-fillable resting test). IBKR **rejected** it; **no order was placed** (verified on
gateway: no ES @ 6000 working order exists; IBKR response had `order_id: "0"`).

---

## Bug A — FUT/FOP orders rejected: `"Can not contain field # 8089"`

### Symptom
IBKR response to the ES order:
```json
{"error": "\"BUY 1 ES SEP'26 @ 6000.00\"\nCan not contain field # 8089",
 "cqe": {"post_payload": {"rejections": ["Can not contain field # 8089"],
                          "sec_type": "FUT", "conid": "649180671", "exchange": "CME",
                          "order_id": "0"}},
 "action": "order_submit_issue"}
```
Reproduced twice (initial stage + "proceed" re-stage). `order_id: "0"` = not placed.

### Root cause (confirmed via authoritative source, per "API Docs First")
- Official IBKR place-order docs (scraped 2026-07-23 via `ibkr_core_mcp.web_scraper`):
  `manualIndicator` (bool) and `extOperator` (string) **are the correct, required fields**
  for FUT/FOP under CME Rule 536-B. So the fields themselves are right.
- IBKR field **#8089 is undocumented.** A Stack Overflow answer from someone who hit the
  identical error (https://stackoverflow.com/questions/79659438/ibkr-place-order-issue-can-not-contain-field-8089,
  answered 2025-07-06) identifies it: **IBKR rejects `manualIndicator`/`extOperator` when it
  classifies the order as _non-futures_.** Their fix — add those fields only for futures — is
  what `order_flow.py:277-282` **already does.**
- **So our case is one level deeper:** our order *is* a genuine future (ES SEP2026) and we
  *do* only add the fields for FUT/FOP, yet IBKR still rejected them as if the order were
  non-futures. **IBKR is not classifying our ES order as a future during validation.**

### Leading hypothesis (NOT yet confirmed — must verify before fixing)
The order body we send (`order_flow.py:266-284`) contains a bare `conid` and **no `secType`
and no `conidex`**:
```
conid, orderType, side, tif, quantity, ticker, acctId, cOID, manualIndicator, extOperator, price
```
The official docs' order example includes **both** `"conidex": "<conid>@<exchange>"` and
`"secType"`. Our STK orders work without them (bare conid is enough for equities), but a FUT
order may need `secType: "FUT"` and/or `conidex: "<conid>@CME"` for IBKR to apply the 536-B
futures validation path and accept the compliance fields. **This is a hypothesis** — confirm
before changing safety-critical order code.

### Verification plan (do FIRST, before any code change)
1. Re-scrape the place-order endpoint doc for the `secType` and `conidex` field definitions
   and any FUT-specific order-body requirements (via `ibkr_core_mcp.web_scraper`, key in
   `.env`). Confirm the exact required format (`"FUT"` vs `"<conid>@FUT"`, exchange in
   `conidex`).
2. Optionally confirm empirically with the IBKR **whatif/preview** endpoint
   (`POST /iserver/account/{acctId}/orders/whatif`) — validates an order body WITHOUT placing
   it. Preview the ES body with vs without `secType`/`conidex` and see which clears the 8089
   rejection. Safe (no order placed), and turns the hypothesis into a certainty.

### Fix (after verification) — `claudia/order_flow.py`
Add the correct futures classification field(s) to the FUT/FOP branch at
`order_flow.py:277-284` (and mirror in the modify body at `:641-644`). Exact field per step-1
verification. TDD: add a test asserting the FUT order body includes the classification field;
this path currently has no such test (which is why it shipped un-caught — "we never
live-checked futures").

---

## Bug B — a rejected order is reported as "Order staged successfully" (ALL instruments)

### Symptom
Even though IBKR rejected the order, the chat showed **"Order staged successfully: BUY 1 ES
(LMT)"** followed by the raw error payload. Misleading — a trader could believe a rejected
order is working.

### Root cause
`order_flow.py:302-306` builds the success message **unconditionally** after
`ibkr.place_order()` returns:
```python
success_text = (
    f"**Order staged successfully:** {action_str} {qty} {symbol} ({otype})\n"
    f"IBKR response: {json.dumps(result, indent=2)}"
)
await send_status(success_text, "ClaudIA")
```
The code's only failure path is `place_order` *raising*. But IBKR returns a **200 response
with an error payload** (`action: "order_submit_issue"`, `order_id: "0"`, `rejections: [...]`,
`error: ...`) — no exception. So the rejection sails through and is labeled success. This is
**not FUT-specific** — any 200-with-rejection response for any instrument would be mislabeled.

### Fix — `claudia/order_flow.py`
Before declaring success, inspect `result` for rejection markers and report failure instead:
- `result[0].get("action") == "order_submit_issue"`, or
- presence of `result[0].get("error")` / `result[0]["cqe"]["post_payload"]["rejections"]`, or
- `order_id in ("0", 0, None)` with no `order_status`.
Only report "staged successfully" when a real order id / `order_status: "Submitted"` (or the
reply-chain terminal state) is present. Also fix the analogous cancel/modify success messages
(`:464-468` and the modify equivalent) if they share the unconditional pattern. TDD: feed a
rejection payload fixture and assert the message says failure, not success, and that no
`trade_executed` decision is logged.

---

## Severity / disposition
- Both fail **safe on the account** (no wrong order placed — IBKR rejected it).
- Bug B is a **data-integrity/trust** issue (mislabels rejections as success) and is
  instrument-general — arguably the higher priority.
- Both touch the safety-critical order path → fix via proper TDD + independent review, not a
  live hot-patch. The live gateway is available now for the whatif verification (Bug A step 1)
  and for a final live re-test of the FUT order once fixed.
- Recorded in the migration plan (Risks & open issues, item 10) and memory. Fix is its own
  task/spec, not part of any current Panel phase.
