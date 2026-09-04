# Order API Reference

Full detail behind the summary in CLAUDE.md § Order Staging. Pull this in when actually
touching `order_flow.py`, `claudia/proposal_tools.py`, `claudia/agent.py`'s proposal-tool
handling, or debugging an order-related issue.

## How a proposal is made

A proposal is a **tool call**, not text. ClaudIA calls `propose_order`, `propose_cancel`, or
`propose_modify` — declared in `claudia/proposal_tools.py` with `strict: true`, so the
Anthropic API validates `tool_use.input` against the schema before the handler in
`claudia/agent.py` ever sees it. The handler records the input and returns a `tool_result`;
it reaches no IBKR API. There is no fenced text format — the `order-proposal` /
`order-cancel-proposal` blocks and their hand-written validator (`order_proposal_schema.py`)
were retired 2026-07-27.

Two enforcement layers, and the split matters:

| Layer | Enforces |
|---|---|
| `strict: true` JSON Schema (API boundary) | Types, `enum` membership, every `required` key present, `additionalProperties: false`, `minItems: 1` on `changes` |
| `agent.py:_proposal_defect()` | The four terms a strict schema cannot express: `quantity > 0`, non-blank `symbol`, non-blank `order_id` (cancel/modify), no duplicate `changes` entries |

Neither layer repairs a value. A defect rejects the whole proposal, creates no button, and
returns a `REJECTED — <reason>` `tool_result` that says so — order parameters are immutable
(CLAUDE.md § Order Staging). At most one proposal is accepted per turn.

`exclusiveMinimum` and free-form `additionalProperties: true` maps are hard 400s on the tools
endpoint (probed 2026-07-27); `proposal_tools.py`'s module docstring is the single record of
what the API actually accepts. Do not add a schema keyword without probing it — an
unsupported keyword fails **every** request, not just a malformed one.

## `propose_order` input

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
  "outside_rth": null,
  "reason": "one-line rationale"
}
```

All eleven keys are `required` and no others are accepted — nullable fields carry an explicit
`null` rather than being omitted.

`outside_rth` (added 2026-09-04, gap #33): nullable boolean, IBKR's `outsideRTH` attribute.
`null` = the user did not say → nothing is sent and IBKR's default applies; `true`/`false` are
sent verbatim, on `propose_modify` too (a modify resends the whole order, so a replacement
without it would silently drop the attribute). It decides **when a stop on a US future can
trigger** — see § Stop orders on US futures below. The field's description is the only text
that reaches the model and carries the immutability rule ("set true only when the user asks
… never assume"). Rendered on every human surface: the approval text (always for a futures
stop, otherwise only when set), the Gate 2 dialog ("Outside RTH: Yes/No" when the body
carries it — `ibkr_core_mcp/order_confirm.py`), and the dashboard's Orders tab as
Yes / No / `—` (`—` = IBKR did not report it; measured `None` on a resting ES limit).

`sec_type` values: `STK`, `FUT`, `OPT`, `FOP`, `CASH`.
`order_type` values: `MKT`, `LMT`, `STP`, `STOP_LIMIT`. This is deliberately **narrower than
the IBKR request body** (below), which also accepts `MIDPRICE`, `TRAIL` and `TRAILLMT`:
`order_flow.py` populates `price`/`auxPrice` only for `LMT`/`STP`/`STOP_LIMIT`, so widening
the enum without widening both execute paths would send a trailing order with no price. See
Known Gaps #6 in `docs/project-status.md`.
`tif` values: `DAY`, `GTC`, `IOC`, `OPG`.
`quantity` is `"type": "integer"` — a fractional value is rejected at the API boundary rather
than silently truncated by `int(qty)` in `order_flow.py`. Positivity is *not* schema-enforced
(`exclusiveMinimum` is a 400); `_proposal_defect()` carries it.
`conid`: a pre-resolved IBKR contract ID, nullable here. **Required non-null** for `FOP`
(options-chain conid resolution isn't inferable from symbol alone) — enforced by
`order_flow.py`, not by the schema; accepted as an override for any `sec_type`, and when set
it skips `search_contract()`/`get_futures()` resolution entirely.

## Order body field spec (from IBKR CP API docs, verified 2026-07-02)

Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/place-order.md

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
| `outsideRTH` | bool | no | allow execution outside regular trading hours — sent when the proposal's `outside_rth` is not `null` (2026-09-04) |
| `manualIndicator` | bool | **FUT/FOP** | CME Rule 536-B — required since May 1, 2025 |
| `extOperator` | str | **FUT/FOP** | CME Rule 536-B — identifies submitting system. **Not sent by ClaudIA**: IBKR rejects any non-empty value as undocumented field 8089 on this account class (whatif isolation, 2026-07-23); `manualIndicator` alone is accepted |

Display-only fields use `_` prefix (`_companyName`, `_multiplier`) — stripped by `client.py`
before the API call. `ticker` is **not** stripped (valid IBKR field).

## Instrument-specific paths

`_execute_staged_order_core()` in `order_flow.py` resolves `conid` in this order: **(1)** the
proposal's own `conid` field, if set, always wins — no further lookup; **(2)** otherwise,
routing depends on `sec_type`:

**Equities (STK):**
- Conid resolved via `IBKRClient.search_contract()` → `/iserver/secdef/search`
- `manualIndicator` / `extOperator` omitted (equity orders; would cause 400 if included)

**Futures (FUT):**
- Conid resolved via `IBKRClient.get_futures()` → `/trsrv/futures`, front month picked by lowest `expirationDate`
- `/iserver/secdef/search` does **not** support FUT — do not use it for futures conid resolution
- `manualIndicator: True` added automatically (CME Rule 536-B, mandatory since May 1, 2025). `extOperator` is
  **not** sent: IBKR rejects any non-empty value as undocumented field 8089 on this account class — proven by
  whatif isolation 2026-07-23 (`order_flow.py`, the field-spec comment). This line said "added automatically"
  until 2026-09-04; the code had stopped sending it on 2026-07-23.
- **Multiplier, currency and contract label come from `/iserver/contract/{conid}/info`**
  (`order_flow._futures_contract_facts`, on every futures path — conid supplied or resolved),
  passed as `_multiplier`, `_currency` and `_companyName` (e.g. `ESU6 · expires 2026-09-18 · x50`)
  display fields. Until 2026-09-04 this line said the multiplier came from `/trsrv/futures`:
  **it never did** — those rows carry only `conid`, `expirationDate`, `ltd`, the cut-offs,
  `symbol` and `underlyingConid` (measured), the tests had invented a `multiplier` key, and
  on the conid-supplied path the lookup was skipped entirely. Found live the same day: Gate 2
  printed `Total (est.): 7,735.00` for one ES contract standing for 386,750 USD.
  Source: <https://ibkrcampus.com/docs/web-api/v1/endpoints/contract/contract-information-by-contract-id.md>
- Gate 2 dialog shows the notional as `price × qty × multiplier` with the ISO currency; when the
  multiplier could not be learned it prints `— (contract multiplier unknown; not price × quantity)`
  rather than a number wrong by the multiplier (`_multiplier_unknown`, ibkr_core_mcp `e12b6fd`).
  The symbol line carries the local symbol and expiry, so the contract month is visible before
  the send — the proposal text still shows only `ES [FUT]`.
- **Read side of a resting stop**: IBKR reports it with `price` `''` and the stop in
  `auxPrice` / `stop_price` (`orderType` `Stop`, measured on order 853170745); the dashboard's
  order book has a `Stop` column for it since 2026-09-04.

### Stop orders on US futures — what IBKR does with them (scraped 2026-09-04)

Established before a live ES buy-stop test, from IBKR's own pages (local copies in
`.firecrawl/ibkr/`, git-ignored):

1. **A plain `STP` carries its stop in `price`, not `auxPrice`, on the Web API.** IBKR's Web
   API lesson: *"To create a Stop order, we will change the order type from LMT to STP … We
   will still use our price field to designate our stop price."* The TWS API is different
   (`order.auxPrice = stopPrice`) — do not "fix" ours to match it. `order_flow.py` sends
   `STP → price`, `STOP_LIMIT → price` (limit) + `auxPrice` (stop). ✔ matches the source.
   Source: <https://www.interactivebrokers.com/campus/trading-lessons/placing-orders/>
2. **Stops on US futures are simulated by IBKR and, by default, trigger only during RTH.**
   *"Interactive Brokers provides customers with simulated stop orders … simulated stop orders in
   U.S. futures contracts other than single stock futures will only be triggered during regular
   trading hours unless you specify otherwise."* CME/Globex: stop-**limit** orders configured to
   trigger outside RTH are native to Globex, with the constraint buy limit ≥ stop / sell limit
   ≤ stop. Source: <https://www.interactivebrokers.com/en/trading/us-futures-stop-order.php>
3. **"Specify otherwise" is the `outsideRTH` order attribute.** IBKR staff, on the outside-RTH
   lesson: MKT/LMT orders on a CME future *"are active throughout the 24 hour trading day … and
   do not require the Outside RTH attribute"* — stops are not in that list, and where the
   attribute is not applicable it is *"grayed out"*. Source (article + the staff replies in the
   comments): <https://www.interactivebrokers.com/campus/trading-lessons/trading-outside-regular-trading-hours-rth/>.
   The Web API field is `outsideRTH: bool` (place-order example shows it on a GTC TRAILLMT):
   <https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/place-order.md>
4. **ClaudIA can set it since 2026-09-04** (`outside_rth`, above; gap #33). Until then
   `propose_order` had no such field and the body never sent `outsideRTH`, so a GTC stop on ES
   placed through ClaudIA rested overnight but could trigger only in the RTH session. The
   schema change was probed against the live API (accepted), the attribute is shown in the
   approval text, the Gate 2 dialog and the Orders tab, and the read side was measured first:
   `/iserver/account/orders` returns `outsideRTH` although its doc does not list it — `False`
   on a resting AAPL GTC limit, `None` on a resting ES Sep-26 GTC limit — hence three states
   on screen. `/iserver/account/order/{id}` (order status) was measured too: `outside_rth`
   (snake_case, bool) on the stock order, **no such key at all** on the futures order. So
   the modify read-back compares it when IBKR reports a boolean and otherwise appends an
   explicit "could not be verified" caveat (like the price), and `propose_modify`'s
   description tells the model the value for a futures order comes from the user, not from
   a status field that is not there. Review record 2026-09-04 (independent, adversarial):
   seven findings, all addressed before the live test — no `bool()` coercion (only a real
   boolean is sent; `_proposal_defect` check #5 rejects anything else), a stated `false`
   reads "no (stated)" while `null` reads "not set", the modify summary warns that `null`
   resends the order without the attribute, a boolean `previous_value` is expressible in
   `changes`, Gate 2 ignores a present-but-`None` key, and `get_live_orders` renders
   `outsideRTH=yes|no|not-reported`. Live verification: the user's ES buy stop, recorded in
   the Live Test Log.
5. **Which contract a FUT proposal lands on.** Without `conid`, the resolver picks the lowest
   `expirationDate` from `/trsrv/futures`: for ES on 2026-09-04 that is the **2026-09-18**
   contract (conid 649180671), two weeks from expiry — a GTC order on it ends at expiry. A
   proposal that carries `conid` wins outright (rule (1) above), so a further-out contract *is*
   expressible: have ClaudIA read `get_futures` and put the wanted contract's conid in the
   proposal. Measured the same day: ES last 7715.00 at 11:25 ET; the full chain lists Sep-2026
   through Jun-2031.

**Futures Options (FOP):**
- `/iserver/secdef/search` does not document FOP either, and FOP conid can't be derived from
  symbol alone (needs expiry + strike + put/call) — a proposal without `conid` set is
  **rejected with a chat message** directing the user to have ClaudIA call
  `get_option_chain` first and re-issue the proposal with `conid` filled in
- Once `conid` is set, resolution is a pass-through (no `search_contract`/`get_futures` call)
- Same `manualIndicator: True` as FUT, and likewise **no** `extOperator` (CME Rule 536-B applies to FOP too; the
  8089 rejection is per account class, not per instrument). Multiplier, currency and label from contract info,
  as for FUT — an FOP has never been placed live through ClaudIA, so that path is code-verified only.

Source (536-B requirement): https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-changelog/

## Order Cancellation

Mirrors the placement flow exactly: ClaudIA calls `propose_cancel` →
`panel_order_flow.render_cancel_proposal()` shows a "Cancel this order" / "Keep order" button pair →
`_execute_cancel_order_core()` calls `IBKRClient.cancel_order(account_id, order_id)` behind the same
Gate 1 (Touch ID) + Gate 2 (AppKit dialog) pair used by placement — the gates fire inside
`cancel_order()` itself, not in `claudia_ui`. No reply chain to resolve (a single `DELETE` call).

```json
{
  "order_id": "1234567890",
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 100.00,
  "stop_price": null,
  "tif": "GTC",
  "reason": "Closing out the disposable test order"
}
```

All nine keys are `required` and no others are accepted — note there is no `sec_type` or
`conid` on cancel, since `cancel_order()` takes only `(account_id, order_id)`.

`order_id` drives the call; the rest are display fields ClaudIA copies verbatim from a real
`get_live_orders`/`get_order_status`/`diagnose_orders` call earlier in the conversation — never
invented (enforced by the ORDER CANCEL / MODIFY RULES section of `_SAFETY_BLOCK`, and by
`_proposal_defect()`'s non-blank `order_id` check). Every dispatched cancel logs
`decision_type="trade_cancelled"` to `ConversationStore`, carrying the state the read-back
observed (see § Post-dispatch read-back) — `CANCELLED:` in the summary only when
`get_order_status` actually read back `Cancelled`.

**Live-verified 2026-07-10**: button click → Touch ID → Gate 2 → `cancel_order` fired on a
disposable AAPL order (orderId `567317535`), confirmed gone from `get_live_orders` on the next
check. STK cancellation works end to end.

**Known gap (FUT/FOP):** IBKR's documented Cancel Order endpoint requires `manualIndicator`/`extOperator`
**query params** for FUT/FOP (CME Rule 536-B), but `ibkr_core_mcp.IBKRClient.cancel_order()`'s
signature (`account_id, order_id`) has no way to pass them — FUT/FOP cancellation may be
rejected by IBKR until that's added upstream in `ibkr_core_mcp`. STK cancellation is unaffected.
Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/cancel-order.md

**Gate 2 shows full order detail on cancel (fixed 2026-07-10):** `confirm_cancel_dialog(order_id,
account_id, order=None)` in `ibkr_core_mcp/order_confirm.py` takes an optional `order` param —
when provided, the dialog displays the same symbol/side/qty/order type/price/TIF detail the place
and modify Gate 2 dialogs already showed. `cancel_order()` gained a matching optional
`order_details` param; `order_flow.py`'s `_execute_cancel_order_core()` passes its in-hand `proposal`
through (`ibkr.cancel_order(account_id, order_id, order_details=proposal)`). See the resolved
Known Gaps entry in `docs/project-status.md` for commit references and two flagged (non-blocking)
residuals.

## Order Modification

Same button-then-gates pattern, with one important difference: **the request body must be the
full original order, not a partial diff** — verified directly against the primary source
(fetched live 2026-07-08, matches an existing 2026-07-02 scrape word-for-word): the body
content of the modify order endpoint follows the same structure as the standard
`/iserver/account/{accountId}/orders` endpoint, mirroring the original order's content.
Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/modify-order.md

```json
{
  "order_id": "1234567890",
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
  "changes": [{"field": "limit_price", "previous_value": 100.00}]
}
```

All twelve keys are `required` and no others are accepted. The top-level fields carry the
**full replacement order**; `changes` carries only the prior values, for display.

`order_id` and `conid` are both required, and `conid` is the file's one deliberately
non-nullable `conid` — **no fallback resolution** (re-resolving from `symbol` risks silently
picking a different contract). A modify proposal requires ClaudIA to have called
`get_order_status(order_id)` first — richer detail than `get_live_orders` exposes, including
`conid`.

**`changes` replaced the `_changed_fields` / `_previous_values` pair (2026-07-27).** It is an
array of `{field, previous_value}` objects, `minItems: 1`, where `field` is an `enum` of the
five modifiable fields — `limit_price`, `stop_price`, `quantity`, `order_type`, `tif` — so the
model cannot invent a field name. One array rather than two parallel structures, because two
structures describing one fact can disagree; and a free-form `previous_values` map is
inexpressible anyway (strict mode's mandatory `additionalProperties: false` makes a closed
object with no declared properties hold nothing).

`order_flow._format_modify_summary()` reads `changes` directly and renders each entry as
`field: <previous_value> → <proposal[field]>`. **There is no adapter**: the dict that reaches
the render path, the execution core and the `decisions` table is byte-identical to what the
model emitted — reshaping it in the handler would put a mutation of an order proposal on the
path to Gate 2. Two consequences worth knowing:

- The "before" column is an LLM-authored **claim**, not a verified read of the resting order.
  Gate 2 re-renders the actual order and is the authoritative view.
- `_format_modify_summary()` is total by construction — a malformed entry renders as
  `(malformed change entry: …)` rather than raising. A render that dies is exactly how a
  proposal once vanished while the model went on to describe a button that never existed.

`_proposal_defect()` rejects duplicate `field` entries (`uniqueItems` is unsupported), which
would otherwise render a contradictory before/after diff.

**Field-casing gotcha (verified live 2026-07-08 against the CP API reference):** `get_order_status`'s
response uses **snake_case** (`order_id`, `order_type`, `order_status`, `tif`, `conid`, `sec_type`,
`size`, `total_size`, `order_not_editable`, `cannot_cancel_order`) — a different convention from
`get_live_orders`'s response, which is **camelCase** (`orderId`, `orderType`, `secType`,
`timeInForce`, `status`, `remainingQuantity`). Neither matches the modify/place request body's
own camelCase field names (`orderType`, `tif`, `quantity`, `price`, `auxPrice`). `_execute_modify_order_core()`
therefore builds a **fresh** order body from the proposal's typed fields (mirroring
`_execute_staged_order_core()`) rather than forwarding anything from `get_order_status` verbatim.
`modify_order()` does no `_`-prefix stripping (unlike `place_order()`), so the body is an explicit
whitelist — `conid`, `orderType`, `side`, `tif`, `quantity`, `ticker`, plus `price`/`auxPrice` by
order type and `manualIndicator` for FUT/FOP. The display-only proposal fields (`changes`,
`reason`) are never copied in, so they cannot reach the request body.
Sources: https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/order-status.md ,
https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/live-orders.md

`get_order_status` also returns `order_not_editable`/`cannot_cancel_order` booleans — ClaudIA's
system prompt requires checking these before proposing a modify/cancel and explaining to the
user if either blocks the action, rather than proposing it anyway.

Calls `IBKRClient.modify_order_and_confirm(account_id, order_id, order_body)` — the reply-chain-aware
variant (same loop as `place_order_and_confirm()`). **Live-verified 2026-07-10**: a clean,
button-click-only send → modify → cancel cycle on a disposable AAPL order (orderId `567317535`,
limit $100.00 → $105.00), zero manual reply-chain intervention at any step — see Live Test Log
in `docs/project-status.md`. Every dispatched modify logs `decision_type="trade_modified"` to
`ConversationStore`, carrying the state the read-back observed (see § Post-dispatch read-back) —
`MODIFIED:` in the summary only when the status read back as working **and** the read-back's
fields matched the request.

**Order-origin labeling fixed (2026-07-10):** `get_live_orders`/`diagnose_orders` now check
`order_ref` (IBKR's actual Live Orders field, snake_case) first, with `orderRef`/`cOID`/
`clientOrderId` kept only as fallbacks. Before the fix, both checked the fallback keys only, so
every order — including ClaudIA's own — fell through to an unreliable `clientId` check and was
mislabeled `EXTERNAL`; this made ClaudIA correctly refuse to auto-propose a modify on its own
just-placed order per its hard rule, requiring a manual gate confirmation instead of an autonomous
proposal. Empirically the mislabel itself was cosmetic (IBKR accepted the modify regardless), but
the usability regression was real. See the resolved Known Gaps entry in `docs/project-status.md`
for commit references and a known residual edge case.

## Post-dispatch read-back (L2)

**Evidence is the only source of truth for orders, no assumptions.** A dispatch response
proves the request was *received* and nothing more, so no core claims an outcome from it.
Added 2026-07-27 (`_read_back` in `claudia/order_flow.py`).

IBKR says this itself for cancels: the `{"msg": "Request was submitted"}` body "indicates our
request to cancel order 987654 was received, **but not that the order ticket itself has been
canceled**"
(<https://ibkrcampus.com/docs/web-api/trading/orders/canceling-orders.md>). Before this change
`_execute_cancel_order_core` printed `**Order cancelled:** order {id}` having observed nothing
about the order's actual state.

Each core now emits two separate things:

1. **What is known** — `Dispatch accepted by IBKR — order {id}. Verifying live state…`, with
   the raw response. Never "successfully"; that word no longer appears on any of these paths.
2. **What is observed** — one `get_order_status` read after a single fixed `_READBACK_DELAY_S`
   (2.0 s, above `client.py`'s 1 s subscription warmup). Deliberately **not** a poll loop: a
   retry state machine is complexity that can itself fail.

Confirmation sets, from IBKR's documented `order_status` values
(<https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/order-status-value.md>):

| Action | Confirms on | Notably excluded |
|---|---|---|
| place / modify | `Submitted`, `PreSubmitted`, `Filled` | `PendingSubmit` ("have not yet received confirmation that it has been accepted by the order destination"), `Inactive`, `WarnState` |
| cancel | `Cancelled` ("the balance of your order has been confirmed canceled") | `PendingCancel`, `PreCancelled` — reported with IBKR's own warning that you may still receive an execution while a cancellation request is pending |

`ApiCancelled` is deliberately **not** in the cancel set: `client.py` lists it in
`_TERMINAL_STATUSES` for filtering the live-orders feed, but it is not a documented value of
this endpoint's `order_status` — an undocumented state is never treated as proof.

Absence from `get_live_orders` is not usable as evidence either: `_TERMINAL_STATUSES` filters
`Cancelled` out, so a cancelled order and one that never existed look identical.

**A failed read is an absence of evidence, never a confirmation.** `get_order_status` returns
**503 by design** for orders cancelled or filled before the active session, and for FA/linked
accounts without an account switch
(<https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/order-status.md>).
That path reports "could not be verified … do not assume this order is working", and so does a
placement whose response carries no order id.

**Modify additionally compares fields**, because a modify that silently did not apply still
reads `Submitted` — the status only proves the order exists. `_compare_modify_readback` checks
`quantity`→`total_size` (not `size`, which is only the unfilled remainder), `orderType`→
`order_type`, `tif`→`tif`, `side`→`side`, numerically first so IBKR returning `"3.0"` for a
requested `3` is a match. A missing or blank read-back field counts as *not comparable*, never
as disagreement — a false alarm is what teaches a user to ignore the real ones.
**Prices are verified from `limit_price` / `stop_price` since 2026-09-04.** The *documented*
order-status response has no discrete price field (`average_price` is the average price of
*execution*), and until that day every price modify was reported "could not be verified" on
that basis. Measured live on three resting orders, the response does carry `limit_price` on a
limit (`'150.00'`, `'7660.00'`) and `stop_price` on a stop (`'7732.00'`, `limit_price` `''`) —
undocumented, like `outside_rth`. `_price_readback_fields` maps the request's `price` /
`auxPrice` to those by order type (`_compare_modify_readback`); a response that lacks the field
still gets the plain "could not be verified" caveat with IBKR's own `order_description` quoted,
never a value parsed out of that string.

`_is_ibkr_rejection` is retained and its role narrowed: it can no longer authorise a success
claim, only a failure one. It remains the sole detector of a dispatch that never became an
order, and its evidence — the POST body's error text — is unrecoverable afterwards, since a
rejected order has no id to read back.

An exception raised *after* the dispatch call returns (surfacing the result, the read-back,
writing the decision row) reports "**The order WAS dispatched to IBKR** — this failure happened
afterwards"; "Order not placed" is reserved for failures that occur before the write reaches
IBKR.

Both human gates are untouched: Gate 1 (Touch ID) and Gate 2 (AppKit dialog) run in
`ibkr_core_mcp` before any write, and the read-back happens strictly after a dispatch that
already passed both. The read runs on the already-blocked event loop via `asyncio.to_thread` —
one ~2 s call, not a loop (Known Gap #15 is unchanged and out of scope).
