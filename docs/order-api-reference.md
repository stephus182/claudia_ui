# Order API Reference

Full detail behind the summary in CLAUDE.md § Order Staging. Pull this in when actually
touching `order_flow.py`, `claudia/agent.py`'s order-proposal handling, or debugging an
order-related issue.

## Order proposal format

```json
{
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 100.00,
  "stop_price": null,
  "tif": "GTC",
  "sec_type": "STK",
  "conid": null,
  "reason": "one-line rationale"
}
```

`sec_type` values: `STK` (default), `FUT`, `OPT`, `FOP`, `CASH`.
`order_type` values: `MKT`, `LMT`, `STP`, `STOP_LIMIT`, `MIDPRICE`, `TRAIL`, `TRAILLMT`.
`tif` values: `DAY`, `GTC`, `OPG`, `IOC`.
`conid` (optional): a pre-resolved IBKR contract ID. **Required** for `FOP` (options-chain
conid resolution isn't inferable from symbol alone); accepted as an override for any
`sec_type` — when set, it skips `search_contract()`/`get_futures()` resolution entirely.

## Order body field spec (from IBKR CP API docs, verified 2026-07-02)

Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#place-order

| Field | Type | Required? | Notes |
|---|---|---|---|
| `conid` | int | yes* | *or `conidex`; SMART-routes when set. `order_flow.py` resolves it from `symbol` per instrument (below) unless the proposal's own `conid` field overrides resolution |
| `orderType` | str | yes | `LMT` \| `MKT` \| `STP` \| `STOP_LIMIT` \| `MIDPRICE` \| `TRAIL` \| `TRAILLMT` |
| `side` | str | yes | `"BUY"` \| `"SELL"` |
| `tif` | str | yes | `DAY` \| `GTC` \| `OPG` \| `IOC` \| `PAX` (crypto) |
| `quantity` | int | yes | whole shares/contracts only |
| `price` | float | LMT / STOP_LIMIT | limit price |
| `auxPrice` | float | STOP_LIMIT / TRAILLMT | stop price |
| `acctId` | str | no | defaults to first account |
| `ticker` | str | no | underlying symbol — valid IBKR field, not stripped |
| `cOID` | str | no | customer order ID; max 64 chars; unique per 24h |
| `listingExchange` | str | no | default: SMART routing |
| `outsideRTH` | bool | no | allow execution outside regular trading hours |
| `manualIndicator` | bool | **FUT/FOP** | CME Rule 536-B — required since May 1, 2025 |
| `extOperator` | str | **FUT/FOP** | CME Rule 536-B — identifies submitting system |

Display-only fields use `_` prefix (`_companyName`, `_multiplier`) — stripped by `client.py`
before the API call. `ticker` is **not** stripped (valid IBKR field).

## Instrument-specific paths

`execute_staged_order()` in `order_flow.py` resolves `conid` in this order: **(1)** the
proposal's own `conid` field, if set, always wins — no further lookup; **(2)** otherwise,
routing depends on `sec_type`:

**Equities (STK):**
- Conid resolved via `IBKRClient.search_contract()` → `/iserver/secdef/search`
- `manualIndicator` / `extOperator` omitted (equity orders; would cause 400 if included)

**Futures (FUT):**
- Conid resolved via `IBKRClient.get_futures()` → `/trsrv/futures`, front month picked by lowest `expirationDate`
- `/iserver/secdef/search` does **not** support FUT — do not use it for futures conid resolution
- `manualIndicator: True` and `extOperator: "ClaudIA"` added automatically (CME Rule 536-B, mandatory since May 1, 2025)
- Contract multiplier fetched from `/trsrv/futures` response and passed as `_multiplier` display field
- Gate 2 dialog shows correct notional: `price × qty × multiplier`

**Futures Options (FOP):**
- `/iserver/secdef/search` does not document FOP either, and FOP conid can't be derived from
  symbol alone (needs expiry + strike + put/call) — a proposal without `conid` set is
  **rejected with a chat message** directing the user to have ClaudIA call
  `get_option_chain` first and re-issue the proposal with `conid` filled in
- Once `conid` is set, resolution is a pass-through (no `search_contract`/`get_futures` call)
- Same `manualIndicator: True` + `extOperator: "ClaudIA"` requirement as FUT (CME Rule 536-B applies to FOP too)

Source (536-B requirement): https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/

## Order Cancellation

Mirrors the placement flow exactly: ClaudIA emits an `order-cancel-proposal` JSON block →
`order_flow.render_cancel_proposal()` shows a "Cancel this order" / "Keep order" button pair →
`execute_cancel_order()` calls `IBKRClient.cancel_order(account_id, order_id)` behind the same
Gate 1 (Touch ID) + Gate 2 (AppKit dialog) pair used by placement — the gates fire inside
`cancel_order()` itself, not in `claudia_ui`. No reply chain to resolve (a single `DELETE` call).

```json
{
  "order_id": "242538143",
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 100.00,
  "tif": "GTC",
  "reason": "Closing out the disposable test order"
}
```

`order_id` is required; the rest are display-only fields ClaudIA copies verbatim from a real
`get_live_orders`/`get_order_status`/`diagnose_orders` call earlier in the conversation — never
invented. A successful cancel logs `decision_type="trade_cancelled"` to `ConversationStore`.

**Live-verified 2026-07-10**: button click → Touch ID → Gate 2 → `cancel_order` fired on a
disposable AAPL order (orderId `567317535`), confirmed gone from `get_live_orders` on the next
check. STK cancellation works end to end.

**Known gap (FUT/FOP):** IBKR's documented Cancel Order endpoint requires `manualIndicator`/`extOperator`
**query params** for FUT/FOP (CME Rule 536-B), but `ibkr_core_mcp.IBKRClient.cancel_order()`'s
signature (`account_id, order_id`) has no way to pass them — FUT/FOP cancellation may be
rejected by IBKR until that's added upstream in `ibkr_core_mcp`. STK cancellation is unaffected.
Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#cancel-order

**Gate 2 shows full order detail on cancel (fixed 2026-07-10):** `confirm_cancel_dialog(order_id,
account_id, order=None)` in `ibkr_core_mcp/order_confirm.py` takes an optional `order` param —
when provided, the dialog displays the same symbol/side/qty/order type/price/TIF detail the place
and modify Gate 2 dialogs already showed. `cancel_order()` gained a matching optional
`order_details` param; `order_flow.py`'s `execute_cancel_order()` passes its in-hand `proposal`
through (`ibkr.cancel_order(account_id, order_id, order_details=proposal)`). See the resolved
Known Gaps entry in `docs/project-status.md` for commit references and two flagged (non-blocking)
residuals.

## Order Modification

Same button-then-gates pattern, with one important difference: **the request body must be the
full original order, not a partial diff** — verified directly against the primary source
(fetched live 2026-07-08, matches an existing 2026-07-02 scrape word-for-word): the body
content of the modify order endpoint follows the same structure as the standard
`/iserver/account/{accountId}/orders` endpoint, mirroring the original order's content.
Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#modify-order

```json
{
  "order_id": "242538143",
  "conid": 265598,
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 105.00,
  "stop_price": null,
  "tif": "GTC",
  "sec_type": "STK",
  "reason": "Bumping the limit a few dollars, staying unfillable",
  "_changed_fields": ["limit_price"],
  "_previous_values": {"limit_price": 100.00}
}
```

`order_id` and `conid` are both required — **no fallback resolution** for `conid` (re-resolving
from `symbol` risks silently picking a different contract). A modify proposal requires ClaudIA
to have called `get_order_status(order_id)` first — richer detail than `get_live_orders`
exposes, including `conid`. `_changed_fields`/`_previous_values` are display-only, used by the
Gate 2 dialog to show a before/after diff.

**Field-casing gotcha (verified live 2026-07-08 against the CP API reference):** `get_order_status`'s
response uses **snake_case** (`order_id`, `order_type`, `order_status`, `tif`, `conid`, `sec_type`,
`size`, `total_size`, `order_not_editable`, `cannot_cancel_order`) — a different convention from
`get_live_orders`'s response, which is **camelCase** (`orderId`, `orderType`, `secType`,
`timeInForce`, `status`, `remainingQuantity`). Neither matches the modify/place request body's
own camelCase field names (`orderType`, `tif`, `quantity`, `price`, `auxPrice`). `execute_modify_order()`
therefore builds a **fresh** order body from the proposal's typed fields (mirroring
`execute_staged_order()`) rather than forwarding anything from `get_order_status` verbatim —
`modify_order()` does no `_`-prefix stripping (unlike `place_order()`), so display-only proposal
fields (`_changed_fields`, `_previous_values`, `reason`) must never reach the request body.
Sources: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#order-status ,
https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#live-orders

`get_order_status` also returns `order_not_editable`/`cannot_cancel_order` booleans — ClaudIA's
system prompt requires checking these before proposing a modify/cancel and explaining to the
user if either blocks the action, rather than proposing it anyway.

Calls `IBKRClient.modify_order_and_confirm(account_id, order_id, order_body)` — the reply-chain-aware
variant (same loop as `place_order_and_confirm()`). **Live-verified 2026-07-10**: a clean,
button-click-only send → modify → cancel cycle on a disposable AAPL order (orderId `567317535`,
limit $100.00 → $105.00), zero manual reply-chain intervention at any step — see Live Test Log
in `docs/project-status.md`. A successful modify logs `decision_type="trade_modified"` to
`ConversationStore`.

**Order-origin labeling fixed (2026-07-10):** `get_live_orders`/`diagnose_orders` now check
`order_ref` (IBKR's actual Live Orders field, snake_case) first, with `orderRef`/`cOID`/
`clientOrderId` kept only as fallbacks. Before the fix, both checked the fallback keys only, so
every order — including ClaudIA's own — fell through to an unreliable `clientId` check and was
mislabeled `EXTERNAL`; this made ClaudIA correctly refuse to auto-propose a modify on its own
just-placed order per its hard rule, requiring a manual gate confirmation instead of an autonomous
proposal. Empirically the mislabel itself was cosmetic (IBKR accepted the modify regardless), but
the usability regression was real. See the resolved Known Gaps entry in `docs/project-status.md`
for commit references and a known residual edge case.
