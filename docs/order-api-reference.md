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

## Order body field spec (from IBKR CP API docs, verified 2026-07-02; bracket rows 2026-09-06)

Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/place-order.md
Bracket fields (`parentId`, `isSingleGroup`, verbatim rules): https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-orders/submit-new-order.md

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
| `parentId` | str | bracket child only | must equal the parent's `cOID`; the child is held by IBKR and submitted only when the parent fills — see § Attached profit taker (scraped 2026-09-06). **Not sent by ClaudIA**: nothing in the stack can carry a second ticket |
| `isSingleGroup` | bool | OCA only | marks every ticket in the array as one OCA group. **Not** part of a bracket — the bracket example omits it; only the OCA variant sets it |
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
  passed as `_multiplier`, `_currency` and `_companyName` (e.g. `ESU6 · expires 2026-09-18`; the
  multiplier reaches the dialog's Quantity row as `1 (×50 per contract)` since 2026-09-11, gap #45)
  display fields. Since 2026-09-11 (gap #49) a **stock** gets `_companyName` and `_currency` from
  the same read (`order_flow._stock_contract_facts`, cached per conid, fail-soft): live that
  morning `SELL 1 GLD LMT 600` had reached Gate 2 as `Symbol: GLD`, `Price: 600.00` with no
  name and no currency while IBKR's status said `USD` — a share price is money. Until
  2026-09-04 this line said the multiplier came from `/trsrv/futures`:
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

### The resolved contract is named on the approval text (2026-09-10, gap #37)

A bare root in a futures proposal (`symbol: "ES"`, `sec_type: "FUT"`) keeps meaning the front
month — the lowest `expirationDate` on `/trsrv/futures`, IB's own default — and the approval
text now says which contract that is: `**Contract:** ESU6 · SEP26 · expires 2026-09-18`, from
`claudia/contract_identity.py` (one cached `GET /iserver/contract/{conid}/info` per conid:
`local_symbol`, `contract_month` → IB's `MMMYY` token, `maturity_date`, `company_name`). The
render site reads it in a thread through `order_flow.proposal_contract_label`; a stock builds no
client and any failure means no line, never a guess — Gate 2 carries the same label
independently. `ESU6` is an output only: `secdef/search?symbol=ESU6` answers "No symbol found"
(measured 2026-09-10), so nothing parses a month-coded symbol as an input. The same identity
feeds the Positions and Orders tabs (`Symbol` = local symbol, `Name` = IB's long name · month)
and the `_contract` block on `get_market_snapshot` FUT results.

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

## Attached profit taker (parent → child) orders — API support (scraped 2026-09-06, extended 2026-09-08)

The question: *sell ES at 7725 with a "profit taker" buy at 7700 that only comes live when the
sell fills* — a one-child bracket. **The Web API supports it. ClaudIA cannot send it today.**
Local copies of every page cited here are in `.firecrawl/ibkr/*-2026-09-06.md` and
`*-2026-09-08.md` (git-ignored). The 09-08 pass re-fetched the six 09-06 pages (byte-identical) and
added twenty more, including IBKR's own Web API bracket walkthrough and the newer reference site.

### What IBKR calls it

- **Profit Taker** is *"an opposite side limit order designed to close a position while it is
  profitable"*; *"For a SELL parent order, it's a low-side buy order"* with *"the same order
  quantity as the parent"*, and *"the order will be created, but will not be submitted until the
  parent order fills."* Profit Taker + Stop Loss = **Bracket**; either child can be attached on
  its own (TWS: *"Check the 'Profit Taker' box"*; the type is *"limit or relative"*).
  Sources: <https://www.ibkrguides.com/ipad/attached.htm> (updated 2026-01-27),
  <https://www.ibkrguides.com/traderworkstation/advanced-button.htm>.
- The order-types catalogue lists **Bracket** as *Platforms: Select · Regions: US & Non-US ·
  Routing: Smart, Directed* — no futures exclusion.
  Source: <https://www.interactivebrokers.com/en/trading/ordertypes.php>.

### How the Web API expresses it

`POST /iserver/account/{accountId}/orders` takes an `orders` **array**: *"Only one order ticket
object may be submitted per request, unless constructing a bracket."* The link is two fields
(both in the field table above):

| Ticket | Field | IBKR's rule (verbatim) |
|---|---|---|
| parent | `cOID` | *"Client-configurable order identifier … Should not be set for the child of a bracket order."* |
| child | `parentId` | *"If the order ticket is a child order in a bracket, the parentId field must be set equal to the cOID provided for the parent order."* |

`isSingleGroup` is **not** involved: the bracket example omits it, and IBKR adds it only for the
OCA variant (*"in addition to the standard bracket, each order will include isSingleGroup: true"*).
A bracket is therefore *one* request. The narrative page also allows a sequential form — *"Bracket
orders can be submitted sequentially using the default order_id created by Interactive Brokers"* —
with no example; the one-request form is the documented one. The whatif endpoint accepts the
same array (*"Preview the projected effects of an order ticket or bracket of orders"*), so a
bracket can be margin-previewed before the gates.
Sources: <https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-orders/submit-new-order.md>,
<https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/bracket-orders-oca-groups.md>,
<https://ibkrcampus.com/docs/web-api/trading/orders/submitting-bracket-orders.md>,
<https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-orders/preview-margin-impact.md>.

The user's example, in IBKR's documented shape (ES Sep-2026 conid 649180671 as measured
2026-09-04 — pick the contract deliberately, § Instrument-specific paths rule 5):

```json
{
  "orders": [
    {
      "acctId": "U…", "conid": 649180671, "cOID": "CLAUDIA-<ms>",
      "orderType": "LMT", "price": 7725.00, "side": "SELL", "quantity": 1, "tif": "GTC",
      "manualIndicator": true
    },
    {
      "acctId": "U…", "conid": 649180671, "parentId": "CLAUDIA-<ms>",
      "orderType": "LMT", "price": 7700.00, "side": "BUY", "quantity": 1, "tif": "GTC",
      "manualIndicator": true
    }
  ]
}
```

`manualIndicator` on **both** tickets is an inference, not a documented rule: IBKR says *"Orders
for USFUT products that do not include this field will be rejected"* per ticket and never
mentions children. Probe it on `whatif` before the first live send.

### What the 2026-09-08 scrape settled, and what stays unwritten — measure the rest

Four of the six items the 09-06 pass listed as undocumented now have a documented expectation.
An expectation is confirmed live before it is relied on; it is not re-derived.

**Documented:**

- **Response: one entry per ticket, and the reply chain is per ticket, index-aligned.** The
  reference schema names four response variants. *orderSubmitSuccess*: *"A successful submission
  of one or more order tickets."* *orderReplyMessage*: *"An array containing objects that each
  deliver the order reply messages emitted against one order ticket in the submission request's
  array. Indicies of the order reply message objects in this array correspond to the indicies of
  the order tickets in the submission request's array."* So a bracket can return a reply for the
  child at index 1 while index 0 is terminal, and `place_order_and_confirm`'s loop, which keys on
  `response[0]` only, would never answer it. IBKR's own worked example (IBKR API group, 2021, the
  only place the per-ticket keys appear) returns:

  ```json
  [{"order_id": "1763237133", "order_status": "Submitted",    "local_order_id": "66807300"},
   {"order_id": "1763237135", "order_status": "PreSubmitted", "parent_order_id": "1763237133"}]
  ```

  with the gloss *"order_id = system generated order Id(s) for each order. local_order_id = cOID.
  parent_order_id = order_Id of the parent order."* Those two keys are absent from the formal
  schema (three properties on the success object), so they are an expectation to confirm, not a
  contract.
  Sources: <https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/submit-new-order>,
  <https://www.interactivebrokers.com/campus/ibkr-quant-news/how-to-code-a-bracket-order-in-the-web-api/>.
- **The held child's status is `PreSubmitted`** in that example, defined as *"accepted by the
  system (simulated orders) or an exchange (native orders) and that this order has yet to be
  elected"*. `_CONFIRMED["place"]` in `order_flow.py` counts `PreSubmitted` as a working order;
  for a bracket child it means *held*, and a read-back must say so rather than "working".
  Source: <https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/order-status-value.md>.
- **The mechanism, in IBKR's words:** *"When an order is attached to another, the system will keep
  the child order 'on hold' until its parent fills. Once the parent order is completely filled, its
  children will automatically become active."* (TWS API page; same order model.) The sequential
  form has a documented failure the single-request form cannot have: *"it will be necessary to
  include a small delay of 50 ms or less after placing the parent order for processing, before
  placing the child order. Otherwise the error '10006: Missing parent order' will be triggered."*
  Decision D3 (one request) stands on that.
  Sources: <https://interactivebrokers.github.io/tws-api/order_submission.html#order_attach>,
  <https://ibkrcampus.com/docs/general/order-types/complex-orders/hedging.md>.
- **Read side: two candidates the 09-06 pass missed.** The Live Orders *guide* example carries
  `"order_ref": "Order123"`, a key absent from the reference's field list; if it echoes `cOID`, a
  parent is findable in the book by its `CLAUDIA-<ms>` reference. And the two `child_order_type`
  pages disagree: the reference says hedges (*"A = Attached child hedge order"*), the v1 page
  says *"A=attached, B=beta-hedge, 0=No Child"*. Whether a bracket child reads `A` is a
  measurement.
  Sources: <https://ibkrcampus.com/docs/web-api/trading/orders/monitoring-live-orders.md>,
  <https://ibkrcampus.com/docs/web-api/v1/endpoints/order-monitoring/order-status.md>.
- **The parent need not be a limit order.** IBKR staff, in the Mosaic lesson's comments: *"the
  primary order does not necessarily need to be a Limit order"*; IBKR's Web API bracket example
  uses a `MKT` parent. Each child sets `outsideRTH` for itself in every IBKR UI (the Desktop
  lesson: the profit taker asks *"if they would like the order to be active outside regular
  trading hours"*, the stop loss *"is not available outside regular trading hours"*).
  Sources: <https://www.interactivebrokers.com/campus/trading-lessons/bracket-orders-for-tws-mosaic-2/>,
  <https://www.interactivebrokers.com/campus/trading-lessons/bracket-orders-for-ibkr-desktop/>.
- **A Profit Taker alone is a first-class product** on every IBKR front end (TWS: *"Check the
  'Profit Taker' box"*; Mobile: *"Choose from Profit Taker, Stop Loss or Bracket"*), and the order
  types catalogue lists Bracket for *"Stocks, ETFs, Options, Futures, FOPs, Currencies, Warrants,
  EFPs, Combos"* on *"TWS, IBKR Desktop, and IBKR Mobile"*. The API pages still show no one-child
  example; the order model allows one.
  Source: <https://www.interactivebrokers.com/en/trading/ordertypes.php> (Bracket).

**Still unwritten anywhere IBKR publishes — measure, do not assume:**

1. **A one-child bracket through the Web API.** Every API example has two children.
2. **Mixed time-in-force** across parent and child.
3. **Cancelling the parent.** No IBKR page states what happens to a held child. The product
   pages describe only the *active* children as an OCA pair (*"When one fills, the other is
   canceled"*). Community threads say the child dies with the parent; that is not evidence.
4. **Modifying the parent.** The modify body must *"mirror the content of the original order"*
   with *"All JSON keys from the initial order submission"* present; whether that includes `cOID`,
   and whether the held child survives a parent modify, is unstated. This matters today, not
   later: `propose_modify` already exists and will be pointed at a bracketed parent.
5. **Partial fill of the parent.** *"Once the parent order is completely filled"* is the only
   statement; what a 1-of-2 fill does to the held child is not.
6. **Whether a bracket is accepted or rejected as a unit.** The two reject variants
   (`orderSubmitError`, `advancedOrderReject`) are single objects with no index, which suggests a
   whole-request verdict. Suggests. A parent accepted with its child refused is the one outcome
   the staging text must never mislabel as "not placed".
7. **`manualIndicator` on the child** (above).

After the parent fills, the child is an ordinary order: a **LMT** profit taker on ES is native to
Globex and rests around the clock; a **STP** child is IBKR-simulated and RTH-only by default —
§ Stop orders on US futures applies to the child too, so a stop-loss leg needs `outsideRTH`.

### Measured live 2026-09-10 — the single-ticket baseline (not a bracket)

A disposable ES stop (`975324503`) was placed, modified and cancelled through ClaudIA with every
response captured out of band (Live Test Log row of that date; raw captures in
`data/test-sessions/2026-09-10-captures/`, git-ignored). This is what a bracket read gets compared
against, so the expectations above do not have to be re-derived:

- **`local_order_id` is live behaviour on the single-ticket path and equals the cOID.** Placement
  *and* modify both answered `[{"order_id": "975324503", "local_order_id":
  "CLAUDIA-1789049488826", "order_status": "PreSubmitted", "encrypt_message": "1"}]`. The 2021
  worked example's key is real; `parent_order_id` on a child is still unmeasured.
- **`order_ref` on the live-orders row echoes the cOID** (`CLAUDIA-1789049488826`) — the
  book-side link the 09-08 pass called a candidate — while the order-status endpoint has no
  `order_ref` key at all (its keys were listed: 46 on the working read, `order_ref` not among
  them). A parent is findable in the book by reference; a status read needs the id.
- **`child_order_type` is not a child indicator.** The plain, unattached stop read `"3"` while
  working and `"0"` once cancelled. The v1 page's `0 = No Child` therefore described a
  *cancelled* plain order here, and a working plain order was neither `A` nor `0`. A bracket
  detector must not key on the field's presence or on `≠ 0`; whether a real child reads `A`
  stays a measurement.
- **The bare `DELETE` cancelled the future** (second measurement, § Order Cancellation) — the
  path a parent cancel would take.
- **Status values seen:** `PreSubmitted` from placement through modify (a resting stop, not a
  held child — a bracket's held child will read the same on this field, which is why the
  read-back must say *held* from the parent link, never from the status alone), then
  `Cancelled` with `cannot_cancel_order: true`.

### Why ClaudIA cannot send it today (2026-09-06, re-checked against the 2026-09-08 scrape)

Every layer carries exactly one ticket:

| Layer | Where | The one-ticket assumption |
|---|---|---|
| Proposal schema | `claudia/proposal_tools.py` | `propose_order` is 11 closed keys, no child; a strict-schema change needs the live-API probe (`live_api` marker) |
| Order body | `claudia/order_flow.py` (`order_body`) | one dict; `cOID` is set, `parentId` never |
| Client | `ibkr_core_mcp/client.py` `place_order(account_id, order: dict)` and `get_order_preview` | both wrap the single dict as `{"orders": [api_order]}` — a caller cannot pass two tickets. `place_order_and_confirm` answers replies for `response[0]` only; the reply chain is documented as one entry per ticket, index-aligned (above), so a child's reply would go unanswered and the child be dropped silently |
| Gate 2 | `ibkr_core_mcp/order_confirm.py` | renders one order; the human must see **both** legs before **SEND TO IBKR** |
| Read-back | `order_flow._read_back` | one order id; the documented bracket response carries one entry per ticket (above), so both ids are available to read back — the child's `PreSubmitted` must read as *held*, not *working* |
| Orders tab | `claudia/panel_dashboard.py` | no parent column; two candidate fields to measure before adding one — `order_ref` and `child_order_type` (above) |
| Execution reports | `claudia/execution_listener.py` | already correct: both legs of a bracket are reported since the 2026-09-04 fix |

Hard Rule 1 is unaffected by any of this: a bracket is still one physical click on a request the
human sees whole. Known Gaps #36 in `docs/project-status.md` tracks it.

**Two independent orders are NOT a substitute and must never be proposed as one — user
rule, 2026-09-07.** A standalone opposite-side limit placed *before* the parent fills is a live
order on its own: at 7700 it fills first and opens a long, the opposite of the intent, with
the "parent" still resting to double the exposure on the way back. The conditional link
(`parentId`) *is* the order type; without it there is no profit taker, only two unrelated
orders. Until the bracket path exists, the profit taker is either attached in TWS/IBKR Mobile
by hand, or proposed through ClaudIA **only after** the parent's fill has been reported by
IBKR (§ Automatic execution reports). This rule belongs in ClaudIA's safety block when the
feature is built, so the model can never "help" by splitting a bracket into two proposals.

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

**Documented but not enforced (FUT/FOP) — measured twice:** IBKR's Cancel Order page lists
`manualIndicator`/`extOperator` **query params** for FUT/FOP (CME Rule 536-B), and
`ibkr_core_mcp.IBKRClient.cancel_order()` sends the bare `DELETE` with neither. That bare call
cancelled a live ES order on 2026-07-28 (T2, Live Test Log) and again on 2026-09-10
(`975324503`: `{"msg": "Request was submitted"}`, read back `Cancelled`, confirmed gone from
`/iserver/account/orders`). So the requirement is documented and, on both days, unenforced. Do not
add the params on the page's word alone — a query param IBKR rejects on `DELETE` would break a
cancel that works; if they are ever added, probe the live endpoint first, the same rule as the
strict-schema keywords. Tracked as Known Gaps #7 in `docs/project-status.md`. The bracket plan
inherits this path: a parent cancel is the same `DELETE`.
Source: https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/cancel-order.md

**Since 2026-09-10 (gap #40, same commits):** the cancel core reads the order status once before
Touch ID and hands the dialog an IBKR-shaped display dict — side, size, type, prices, TIF,
outside-RTH, the futures label/multiplier/currency, and `Currently at IBKR` from
`order_description_with_contract`; on a failed read the proposal's own values are shown, so a
cancel is never blocked by a read. The order id appears once; nulls and the reason blob are gone
(before: screenshot 2026-09-10 10:38, gaps #27(b–e); the reworked dialog read live at 16:02).

**Gate 2 shows full order detail on cancel (fixed 2026-07-10):** `confirm_cancel_dialog(order_id,
account_id, order=None)` in `ibkr_core_mcp/order_confirm.py` takes an optional `order` param —
when provided, the dialog displays the same symbol/side/qty/order type/price/TIF detail the place
and modify Gate 2 dialogs already showed. `cancel_order()` gained a matching optional
`order_details` param; `order_flow.py`'s `_execute_cancel_order_core()` passes its in-hand `proposal`
through (`ibkr.cancel_order(account_id, order_id, order_details=proposal)`). See the resolved
Known Gaps entry in `docs/project-status.md` for commit references and two flagged (non-blocking)
residuals.

## Order Modification

**Precondition enforced in the handler (2026-09-11):** `propose_modify` is refused — with a
`tool_result` telling the model to call `get_order_status(order_id)` and copy the unchanged
fields from its result — unless `get_order_status` ran **in the same turn**. The read the
immutability rule below copies from has to be in evidence, not recalled; the refusal creates
no button. Source and measurement: `docs/agent-behavior-reference.md` §4d.

**Since 2026-09-10 (gap #40, ibkr_core_mcp `c8ff5d6` + claudia_ui `6df077a`):** the modify
dialog shows the same typed rows as the place dialog (Account / Action / Symbol with the futures
label / Quantity / Order Type / Price with currency / Stop / TIF / Outside RTH / Total) plus
`Order ID`, a `Changes` row (`stop price 7900.0 → 7895.0`, from the proposal's `changes`) and
`Currently at IBKR` (IBKR's `order_description_with_contract`, read once before Touch ID).
`_`-prefixed display keys are stripped by `modify_order` before the POST, as `place_order` always
did. Before that date the dialog was the replacement body verbatim (screenshot 2026-09-10 10:37);
the reworked dialog was read live by screenshot at 16:00 the same day.

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

## Automatic execution reports (2026-09-04)

A fill is shown the moment IBKR sends it — the user does not ask. `ExecutionListener` (already
subscribed to the `str` WebSocket topic for every execution, any origin) now has
`subscribe(callback)` like `ConnectivityChecker`, and notifies each session **before** its P&L
capture (which can block up to 10 s per round). The `ExecutionReport` it delivers is built only
from IBKR's event fields — BOUGHT/SOLD from `side`, size, `symbol` + `contract_description_1`,
price, `trade_time` (UTC) shown in ET, exchange, origin from the `CLAUDIA-` order ref — nothing
model-written and no currency claimed (the event carries none).

Per session, `panel_app._make_fill_subscriber` puts it on four surfaces:

| Surface | What | Why |
|---|---|---|
| Chat message authored **IBKR** | `**FILLED: BOUGHT 1 ES Sep18 '26 @ 7,732.00** · 12:47:05 ET · CME` + provenance line | The broker's record, not the assistant's claim — the one documented exception to "session events go to the System log": a fill must not be missed |
| System log, `warning` | the headline, with the 8 s toast | Noticed while reading something else; on record |
| Operator note (`agent.note_execution`) | the same text, prefixed "IBKR reported an execution … not your action" | The next turn already knows; the channel cannot be spoofed by model output |
| Decision row `execution_reported` | headline, symbol, the report's fields | The session report carries it; in no allowlist (it is not a ClaudIA action) |

Detached on End Session and on the destroy hook together with the alert subscription. If the
WebSocket is down there is no report and nothing is invented; the red IBKR light and the
dashboard's 15 s refresh remain.

**Rules the review of 2026-09-04 added, each with a test:** every fill is reported, including
the ones the listener's P&L capture round consumes from the same queue (a bracket's second leg
within 10 s of the first was silently dropped — reproduced with the real capture loop before
the fix); each execution id is reported once (IBKR's `str` doc does not promise a resubscribe
never re-sends); size and price keep IBKR's digits (no six-significant-figure truncation, no
rounding of a 4-dp FX price — a known broker figure must not be altered any more than an
unknown one may be guessed); a stock's description equal to its ticker is not joined twice
(IBKR's documented STK shape is `"AMD" / "AMD"`); the System log captures the session's
notifications area when it is built, because the listener task keeps the document of whichever
session started it; the fill is never handled as a user turn (`respond=False`, pinned).
Only the FUT event shape has been observed live; STK/OPT shapes rest on IBKR's documentation
until a stock fills through the listener. Not done: a P&L-after-fill line (the realised tile refreshes
within 15 s), a poller-based fallback. Live status: **code-verified 2026-09-04; the first
automatic report awaits the next real fill** (the two fills of that day, 12:47 and the SELL
after it, happened on a server that predates the feature).

## IBKR's reply chain — what it has actually sent (2026-07-06 STK, 2026-09-10 FUT)

### One Touch ID per order write — decided and shipped 2026-09-11 (ibkr_core_mcp `65cedba`…`8af6cca`)

Until that morning, every entry of the chain ran behind its own Gate 1 *and* Gate 2:
`_resolve_one_reply` called `require_touch_id` then `confirm_reply_dialog` per reply, on top of
the write's own pair. Measured 2026-09-10: a BUY ES stop drew two precautions (value limit, Stop Variant) — **three**
Touch IDs; a SELL stop-limit drew three (percentage constraint, value limit, Cap Price) — **four**,
all within seconds, in-process, for one decision.

The user's rule, after a sourced review that morning: **IBKR Mobile and TWS ask for one biometric
per placement, modification or cancellation, and ClaudIA replicates that.** The biometric and the
dialog are two controls with two jobs — Touch ID *authenticates* (a human is present and consents
to this write), the dialog *validates* (this data is read and agreed, with an explicit button).
Place, modify and cancel each keep their own Touch ID; every dialog stays, one per message — the
order, and each precaution; nothing after the fingerprint asks for it again.

What the sources say (URLs in `docs/api-reference.md` § Order authorization; quotes in the local
research note): OWASP's unit of authorization is the transaction and its per-step duty is What
You See Is What You Sign; NIST 800-63B-4 says intent is an explicit button and that a biometric
alone may not establish it, and lists *Authentication Fatigue* as a threat; CISA names push
fatigue; EU RTS 2018/389 Art. 5 is why a bank app asks once — one strong authentication per
transaction, dynamically linked, invalidated on change. Apple's Touch ID reuse window is scoped
to the device unlock and cannot merge prompts.

**What shipped (gap #47):** `place_order_and_confirm` and `modify_order_and_confirm` run Gate 1
once through `client._authorize_order_write` and pass a `human_auth.OrderWriteAuthorization`
down the chain — bound to the exact body about to be sent (`_order_write_scope`: display keys
dropped, keys sorted, SHA-256), 300 s, verified with the same `covers(scope)` at the write and
at every precaution reply, failing closed (no authorization, expired, or another write → a
prompt), never persisted, never global. Every dialog stays; the reply dialog's title names its
order (`⚠  CONFIRM ORDER REPLY — BUY 1 ES`). `cancel_order` is unchanged — one Touch ID, one
dialog. The direct `place_order` / `modify_order` / `reply_order` calls, with no authorization,
prompt exactly as before. Each grant and each covered reply is logged at INFO (`Gate 1: granted
for …`, `Gate 1: reply … covered by authorization …`), so the server log witnesses the count.
The same pass gave the dialogs bold values with regular labels, a MODIFY banner in the order's
colour, and futures price rows without a currency (index points, not money — the total keeps
the USD). `ibkr_core_mcp/SECURITY.md` now says what the code does — gap #48's biometrics-only
claim is corrected there, dated, with the reason. **Counted live the same day:** one Touch ID for
a two-precaution placement at 10:04 (three the day before), then twelve writes for twelve
fingerprints across the 10:24 coverage matrix, user-counted and log-witnessed (`Gate 1:
granted for …` once per write, `covered by authorization` once per precaution). **Refusals
(gap #50, same day):** DO NOT SEND at Gate 2 and DO NOT REPLY at a precaution had left the
store with `trade_proposed` and nothing else — the declined precaution's `confirmed: false`
entry was discarded with the chain. Every outcome after the button now writes a row:
`trade_refused` (stage `gate2` / `reply` / `timeout` / `touch_id`, the reason, the reply log as
far as it got), `trade_rejected` (IBKR's payload), `trade_dispatched_unverified`; the same for
modify and cancel. A human's no logs at INFO, not as an ERROR traceback.

`place_order_and_confirm` loops over `{id, message, messageOptions}` entries, each behind Gate 1
(*Python is trying to confirm an IBKR order reply <id>.*) and the CONFIRM ORDER REPLY dialog, and
returns only the terminal `[{order_id, local_order_id, order_status}]` entry. Chains observed:

- **2026-07-06, an AAPL limit:** three sequential replies before the terminal response (the
  `reply_order` docstring's record) — the loop exists because of it.
- **2026-09-10 10:34, `BUY 1 ES STP 7900 GTC`** (one contract, 395,000 USD notional): **two**
  replies, both screenshotted — (1) *"value estimate of 395,000 USD exceeds the Total Value Limit
  of 100,000 USD. Are you sure you want to submit this order?"*, the account's precautionary
  setting; (2) *Stop Variant Order Confirmation*, IBKR's stop-order disclosure, whose text carries
  literal `&nbsp;` entities (Known Gaps #39). The identical order at 10:11 the same day left no
  record of whether it was asked: the store keeps the terminal entry only (Known Gaps #38).

For a bracket the chain is per ticket and index-aligned (§ Attached profit taker), so both gaps
compound there: a child's reply would be neither answered by today's loop nor recorded.

**Persisted since 2026-09-10 (gap #38 closed in code, ibkr_core_mcp `882231a` + claudia_ui
`d732c44`):** `place_order_and_confirm` / `modify_order_and_confirm` take `reply_log=`, a
caller-owned list that receives one record per reply — `reply_id`, raw `message`, `message_text`
(tags stripped, entities unescaped by `order_confirm.reply_message_text`, which also fixed the
raw `&nbsp;` of gap #39), `message_options`, `confirmed`, UTC `at` — appended before the gates so a
decline or a Touch ID failure is recorded too. `order_flow` stores it as `ibkr_replies` in the
`trade_staged` / `trade_modified` decision metadata and lists the confirmed precautions in the
chat (first line each); a decline names the declined prompt. Live-verified 2026-09-10 16:00:
decision row 72 carried both replies of a one-lot ES stop, confirmed and time-stamped.

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
