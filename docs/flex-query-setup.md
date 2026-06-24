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

**Startup sync logic:**
- Skip if `days_since_newest <= 1` — data is fully current (Flex can't give anything newer)
- Skip if last sync attempt was < 4 hours ago — avoid API lockout from repeated restarts
- Sync otherwise — pulls last 30 days, upserts idempotently, logs the event

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

## Step 6 — Verify Coverage

```
check_flex_coverage
```

No gaps > 5 calendar days = complete. Any gap reported = missing import for that period.

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

## XML Tag Reference (for developers extending flex_query.py)

| Section | XML element | Key attributes |
|---|---|---|
| Trades | `<Trade>` | tradeID, symbol, buySell, quantity, tradePrice, dateTime, ibCommission, accountId |
| Cash Transactions | `<CashTransaction>` | transactionID, type, amount, dateTime, symbol, currency |
| Open Positions | `<OpenPosition>` | reportDate, symbol, position, costBasisMoney, fifoPnlUnrealized |
| Corporate Actions | `<CorporateAction>` | type, dateTime, symbol, quantity, value |
| Change in NAV | `<ChangeInNAV>` | reportDate, startingValue, endingValue, commissions, dividends |
| Statement of Funds | `<StatementOfFunds>` | reportDate, activityDescription, debit, credit, balance |
| Forex Balances | `<FxPosition>` | reportDate, currency, quantity, costBasisMoney, fifoPnlUnrealized |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Flex SendRequest HTTP 401` | Invalid token | Re-copy token from IBKR → Flex Web Service |
| `Flex SendRequest unexpected response: status='Warn'` | Query not found | Verify Query ID in IBKR UI |
| `Flex statement not ready after 5 attempts` | IBKR server slow | Increase `_MAX_POLL_RETRIES` in `flex_query.py` or retry later |
| `Unexpected Flex dateTime format` | Wrong date format set | Re-check Date Format = `yyyyMMdd;HHmmss` in query config |
| Empty trades list | Date range too narrow | Widen the period in the query config |
| No data for a section | Section not enabled | Edit query and enable the section |
