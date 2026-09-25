# IBKR Flex Query Setup for ClaudIA

This guide covers how to build the Flex Query that powers ClaudIA's full historical data
access. One comprehensive Activity Statement query covers all current and planned features.

---

## Trade Data Architecture

ClaudIA uses two complementary data sources — each covers what the other cannot:

| Source | Tool | Coverage | Latency |
|---|---|---|---|
| IBKR Flex Web Service | `sync_flex_trades`, `get_trades source='store'` | Full history (years) — settled trades only | T+1 lag — yesterday at best |
| IBKR Client Portal REST API | `get_trades source='live'` | Last 6 days — includes today's intraday | Real-time |

**Key rule:** Flex never has today's trades. The most current Flex data is always yesterday's settled activity. Today's intraday executions are only available via the live API.

### When a day's statement becomes available (researched 2026-09-25, gap #72)

**IBKR publishes what a day's statement *includes*, not when it can be pulled.** From the
Client Portal statements guide (https://www.ibkrguides.com/clientportal/performanceandstatements/statements.htm,
read 2026-09-25): *"The statement cutoff time for commodities is generally 5:15 PM EST, and the
statement cutoff time for securities is generally 8:20 PM EST."* and *"Any trades executed for
those asset classes before the cutoff times will be reflected in your statement for the day."*
No IBKR page we could reach states a time at which the finished statement can be retrieved:
not the Web API Flex pages (`introduction`, `using-flex-web-service`, `generate-the-report`,
`retrieve-the-report`, `error-codes`, `send-request`, `get-statement`, all fetched as `.md`),
not the Client Portal Flex Web Service v3 guide, not the Campus glossary, not the ibkrguides
Flex Queries or Reporting pages (raw copies in `.firecrawl/flex-availability/`, git-ignored).
A search-engine summary attributed *"updated once daily at close of business"* to IBKR; that
sentence appears verbatim on none of those pages, so it is not cited here. What the Flex pages
do state is the pacing: *"1 request per second; maximum 10 requests per minute per token"*
(`send-request`; error 1018 says the same).

**Measured on our own pull log** (`store.db` `session_log`, event `flex_sync`, 43 pulls
2026-06-24 → 2026-09-25, each with its UTC time and the newest trade date it brought back):

| Observation | Evidence |
| --- | --- |
| The previous trading day's trades were present on **every** morning pull, the earliest at **08:18 ET** (2026-08-05 → newest 08-04); also 09:30, 09:55, 09:57, 10:04, 10:07, 10:29, 10:40, 10:41 ET on other days | lower bound: by 08:18 ET the statement is out |
| A day's own trades were present on **no** evening pull, the latest at **22:07 ET** (2026-07-01 → newest 06-30) and 21:01 ET (2026-09-24 → newest 09-23) | upper bound: not before ~22:07 ET the same day |
| The window **22:07 ET → 08:18 ET is unobserved** | do not put a clock time in code or in user-facing text |

Two cautions when reading that log: `newest` is the newest *trade* date in the store, so a pull
that reports an older date may mean "no trades that day" rather than "not yet available" (the
five coverage gaps are genuine inactivity — memory `project-flex-gap-audit`); and the June
pulls with `trades_fetched: 0` were failures of the early integration, not evidence.

**Consequence for a pull policy:** an evening pull never brings the day's own trades, a morning
pull after ~08:18 ET does, and a pull that finds nothing new is a no-op behind the fingerprint
gate. Spending a Flex call to find out is cheap (one `SendRequest`, ten a minute allowed); what
must never happen is a clock rule that claims to know when IBKR publishes.


**Startup sync logic** (rewritten 2026-09-25, gap #72):
- Skip if IBKR is offline (logged — the sync needs the gateway for the account id)
- Skip if a pull already **changed the store since the most recent midnight ET** — read from
  the store's own `flex_sync` log (`claudia/flex_sync.py`: `last_fruitful_pull`, `pull_due`);
  a pull that changed nothing is not evidence and does not count
- Sync otherwise — pulls last 30 days, upserts idempotently, logs the event. No retry window
  and no staleness definition: over-checking on a weekend or a no-trade day costs one request
  out of the ten a minute IBKR allows; under-checking is what the core's flag did on 2026-09-24

## Why Flex Queries

The IBKR Client Portal REST API returns at most **6 days** of trade history. Everything
beyond that requires a Flex Query via the IBKR Flex Web Service. ClaudIA's
`sync_flex_trades` tool calls this service to populate `~/.ibkr_core/store.db` with
the full execution history, then `get_trades source='store'` queries that store with
no date limit.

Beyond trades, planned future features (cash flow analysis, dividend income, position
history, NAV curve) all require additional Flex sections that we configure now so the
data is ready when the code is extended.

---

## Step 0 — Get Your Flex Web Service Token

The token is account-level and shared across all queries.

1. Log in to **Client Portal** → top-right menu → **Settings** → **User Settings**
2. Under **Reporting**, click **Flex Web Service** (or search "Flex token")
3. If not yet activated, click **Configure** and enable it
4. Copy the **Current Token** — this is your `IBKR_FLEX_TOKEN`

> The token is long-lived but can be regenerated at any time. Store it in `.env`.

---

## Step 1 — Navigate to Flex Queries

**Performance & Reports → Flex Queries**

Or: top-left hamburger menu → **Reporting** → **Flex Queries**

---

## Step 2 — Create the Query: Complete Checklist

Navigate to **Performance & Reports → Flex Queries → "+" next to Activity Flex Query**.

Work through this list top to bottom without stopping. Every answer is pre-decided.

### Top-level fields

| Field | Value |
|---|---|
| Query Name | `ClaudIA Full Activity` |
| Format | `XML` |
| Period | `Last 30 Calendar Days` ¹ |
| Date Format | `yyyyMMdd;HHmmss` |
| Date Separator | `;` (semicolon) |
| Include Offsetting Trade | **Yes** |
| Include Currency Rates | **Yes** |
| Include Audit Trail Fields | **No** |
| Display Account Alias Instead of ID | **No** |
| Breakout by Day | **No** |

> ¹ Full history (2020–present) is in SQLite from the one-time archive import.
> Ongoing sync only needs recent activity — 30 days covers any missed sessions.

---

### Sections — enable exactly these 7, skip everything else

For each section: **Select All fields and all subsections** unless a specific sub-option
is noted below.

| # | Section | Notes |
|---|---|---|
| 1 | **Trades** | Level of Detail = **Execution** |
| 2 | **Cash Transactions** | Select All |
| 3 | **Open Positions** | Select All |
| 4 | **Corporate Actions** | Select All |
| 5 | **Change in NAV** | Sub-option = **Mark to Market** |
| 6 | **Statement of Funds** | Sub-option = **Order Summary** |
| 7 | **Forex Balances** | Select All |

Skip all other sections (SLB, Soft Dollars, Bill Receivables, Account Notes,
Interest Accruals, Complex Positions, Model Portfolio, Order Summary standalone, etc.)

## Step 3 — Save and Get the Query ID

Click **Save**. The query appears in your query list. IBKR assigns it a numeric ID visible
in the list (hover the query row or check the URL when editing).

```
Query ID: 123456789    ← this is your IBKR_FLEX_QUERY_ID
```

---

## Step 4 — Configure `.env`

```bash
IBKR_FLEX_TOKEN=your_token_from_step_0
IBKR_FLEX_QUERY_ID=123456789
```

---

## Step 5 — Historical Backfill (one-time, manual)

The IBKR website lets you run the query with a custom date range up to 365 days wide.
Run year by year, download each XML, save to `~/.ibkr_core/flex_archive/`.

| File | Date range |
|---|---|
| `flex_2020.xml` | first → last trading day of 2020 |
| `flex_2021.xml` | first → last trading day of 2021 |
| `flex_2022.xml` | first → last trading day of 2022 |
| `flex_2023.xml` | first → last trading day of 2023 |
| `flex_2024.xml` | first → last trading day of 2024 |
| `flex_2025.xml` | first → last trading day of 2025 |
| `flex_2026.xml` | first trading day of 2026 → today |

Use the first/last selectable (non-grey) date in the IBKR date picker — holidays and
weekends are greyed out automatically.

Then import each file in ClaudIA:
```
import_flex_file path=~/.ibkr_core/flex_archive/flex_2020.xml
```

**Gap — this backfill is not covered by the integrity check:** `import_flex_file` only upserts
into local SQLite; it never uploads anything to Drive. `verify_flex_import` (Step 6's underlying
check) only scans Drive's `account_data/` folder, matching either the manual-archive pattern
(`ClaudIA_Full_Activity_*.xml`) or the auto-synced pattern (`flex_U*.xml`) — see
`docs/trading-data-reference.md`. Files named `flex_2020.xml` etc. living only in
`~/.ibkr_core/flex_archive/` match neither pattern and aren't in Drive at all, so the years of
history imported this way are invisible to that check. If you want this backfill covered, also
upload each file to Drive's `account_data/` folder, named to match the manual-archive pattern
(e.g. `ClaudIA_Full_Activity_2020.xml`).

## Step 6 — Verify Coverage

```
check_flex_coverage
```

Reports periods of 45+ calendar days with no recorded trade executions. These may be
genuine inactivity (holding a position with no new trades) or missing XML imports —
only you can tell by recalling your trading activity during that period.

A clean import of all year XMLs does not guarantee zero gaps: if you held a position
for 2+ months without executing, that will appear as a gap in the trade history.
That is correct data, not a coverage hole.

## Step 7 — Ongoing Sync

Keep the query period at **Last 30 Calendar Days** (updated after initial archive import).
Run daily or on-demand in ClaudIA:
```
sync_flex_trades
```

Currently parses Trades only. The other 6 sections are present in the XML and will be
parsed as each feature is implemented in code.

---

## What Each Section Feeds in ClaudIA

| Section | Current use | Planned use |
|---|---|---|
| Trades | `sync_flex_trades`, `get_trades source='store'` | Realized P&L breakdowns, trade analytics |
| Cash Transactions | — | Dividend income, interest analysis, fee audit |
| Open Positions | — | Historical position snapshots, drawdown by position |
| Corporate Actions | — | Adjust price history for splits before backtesting |
| Change in NAV | — | Portfolio equity curve, benchmark comparison |
| Statement of Funds | — | Full cash flow audit, running balance |
| Forex Balances | — | FX exposure, multi-currency cash reconciliation |

---

## XML Tag Reference (for developers extending the Flex import)

> **Corrected 2026-08-04.** This table previously listed 8 attributes for `<Trade>`.
> A `<Trade>` element carries **85**, and every one of them was present in every
> statement in the archive. The old list was not a summary — it was the exact set the
> parser kept, and the other 75 were silently discarded for months. Do not hand-maintain
> a field list here again.

**The authoritative inventory is generated from the statements themselves:**

```bash
python scripts/audit_flex_xml.py --src ~/.ibkr_core/flex_archive
#   → ibkr_core_mcp/docs/flex-xml-structure-audit.md   (human-readable, per-attribute)
#   → ibkr_core_mcp/docs/flex-xml-structure.json       (machine-readable)
#   → ibkr_core_mcp/ibkr_core_mcp/flex_schema.py       (generated table/column spec)
```

Measured across the 21 archived statements (2020-01-02 → 2026-08-03):

| XML element | Rows | Attributes | Stored as |
|---|---:|---:|---|
| `<ConversionRate>` | 90,909 | 4 | `flex_conversion_rate` |
| `<StatementOfFundsLine>` | 6,117 | 56 | `flex_statement_of_funds_line` |
| `<Trade>` | 2,247 | 85 | `flex_trade` |
| `<UnbundledCommissionDetail>` | 2,232 | 49 | `flex_unbundled_commission_detail` |
| `<Order>` | 2,152 | 85 | `flex_order` |
| `<Lot>` | 1,371 | 85 | `flex_lot` |
| `<CashTransaction>` | 776 | 46 | `flex_cash_transaction` |
| `<WashSale>` | 443 | 85 | `flex_wash_sale` |
| `<SymbolSummary>` | 279 | 85 | `flex_symbol_summary` |
| `<SecurityInfo>` | 175 | 33 | `flex_security_info` |
| `<OpenPosition>` | 75 | 50 | `flex_open_position` |
| `<AssetSummary>` | 35 | 85 | `flex_asset_summary` |
| `<AccountInformation>` | 19 | 37 | `flex_account_information` |
| `<ChangeInNAV>` | 19 | 58 | `flex_change_in_nav` |

**`<Trade>`, `<Lot>`, `<Order>`, `<WashSale>`, `<SymbolSummary>` and `<AssetSummary>` are
attribute-identical** — one 85-attribute shape distinguished by `levelOfDetail`
(`EXECUTION` / `CLOSED_LOT` / `ORDER` / `WASH_SALE` / `SYMBOL_SUMMARY` / `ASSET_SUMMARY`).
They are siblings under `<Trades>`, **not** nested inside `<Trade>`.

### The three fields most easily got wrong

| Field | What it actually is |
|---|---|
| `fifoPnlRealized` on `<Trade>` | **Realised P/L — the authoritative figure**, net of wash-sale adjustment, exactly as IBKR reports it. |
| `fifoPnlRealized` on `<Lot>` | Tax-lot detail *before* the wash-sale adjustment. Summing it overstates losses. |
| `fifoPnlRealized` on `<WashSale>` | The disallowed loss IBKR adds back. |
| `mtmPnl` | Mark-to-market P/L — a **different accounting methodology**, not another view of the same number. Positions are not matched and commissions are not netted. |
| `tradePnl` | **Does not exist.** The old parser read it first and always fell through to `fifoPnlRealized`. |

**Realised P&L = `SUM(fifo_pnl_realized)` over ALL trades — no open/close filter.** Verified
against IBKR's own `SymbolSummary` totals in **20 of 20** archived statements, to the cent.
Filtering on `openCloseIndicator` is wrong: a buy that closes a short and opens a long is
flagged `O` and still carries realised P&L.

The exact relationship, holding in every statement:

```
Trade.fifoPnlRealized  ==  Lot.fifoPnlRealized  +  WashSale.fifoPnlRealized
```

per IBKR: *"For wash sales, the Realized P/L column will contain the net realized amount,
including loss disallowed."* This is why some equity closes report **exactly 0.00** — the
position was re-bought inside 30 days and the whole loss was disallowed. Correct tax
accounting, not a bug.

`Lot.transactionID` refers to the **opening** transaction; `Lot.tradeDate` is the
**closing** date. Joining lots to trades on `transactionID` matches the open, not the close.

Codes in `notes` are semicolon-delimited and documented at
`ibkrguides.com/reportingreference/reportguide/codes.htm` — mirrored in
`ibkr_core_mcp.flex_import.STATEMENT_CODES`. Present in this account's history:
`P` (partial execution), `IA` (executed against an IB affiliate), `L` (**ordered by IB —
margin violation**), `R` (dividend reinvestment).

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Flex SendRequest HTTP 401` | Invalid token | Re-copy token from IBKR → Flex Web Service |
| `Flex error 1014: Query is invalid.` | Query ID not found | Verify Query ID in IBKR UI (Reports → Flex Queries) |
| `Flex statement not ready after 5 attempts` | IBKR server slow | Increase `_MAX_POLL_RETRIES` in `flex_query.py` or retry later |
| `Unexpected Flex dateTime format` | Wrong date format set | Re-check Date Format = `yyyyMMdd;HHmmss` in query config |
| Empty trades list | Date range too narrow | Widen the period in the query config |
| No data for a section | Section not enabled | Edit query and enable the section |
