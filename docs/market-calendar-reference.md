# Market Calendar Reference

`SQLiteStore.get_market_calendar_context()` injects trading-day awareness into ClaudIA's
system prompt at every session start. No API calls — pure pre-built library data.

## 20 exchanges covered — full G20 + Eurex (current year + next year, past and future holidays)

Excludes Russia (XMOS — IBKR suspended most Russian securities since 2022 sanctions) and
Argentina (XBUE — capital controls, very limited IBKR access). Saudi Arabia (XSAU) trades
Sun–Thu; Fridays appear as "closed" from a Mon–Fri perspective — correct, not a data error.

| Code | Exchange | Region | Why it matters |
|---|---|---|---|
| `XNYS` | NYSE | US | Primary staleness reference, equity order timing |
| `CME` | CME Futures | US | ES, CL, GC — different hours and holiday set vs NYSE |
| `XLON` | LSE London | Europe | European open/close effects on US pre-market |
| `XETR` | Xetra Frankfurt | Europe | EU macro events, German/EU equity flows |
| `XEUR` | Eurex | Europe | DAX futures, EURO STOXX 50 — EU derivatives benchmark |
| `XPAR` | Euronext Paris | Europe | CAC 40, EU large-cap equities |
| `XMIL` | Borsa Italiana | Europe | FTSE MIB, EU peripheral spreads |
| `XTKS` | TSE Tokyo | Asia | Nikkei, yen carry — first major session after US close |
| `XHKG` | HKEX Hong Kong | Asia | China proxy, Hang Seng, dim sum flows |
| `XSHG` | SSE Shanghai | Asia | China A-shares, direct macro signal |
| `XBOM` | BSE Mumbai | Asia | India — fastest-growing G20 equity market |
| `XKRX` | KRX Seoul | Asia | Samsung, TSMC proxy, semiconductor bellwether |
| `XASX` | ASX Sydney | Asia-Pacific | Iron ore, copper — first market to open globally |
| `XTSE` | TSX Toronto | Americas | Oil sands, gold miners |
| `BVMF` | B3 São Paulo | Americas | Brazilian commodities, EM sentiment |
| `XMEX` | BMV Mexico City | Americas | Nearshoring flows, peso/USD dynamics |
| `XJSE` | JSE Johannesburg | Africa | Mining, platinum group metals |
| `XSAU` | Tadawul | Middle East | Oil policy signal, Aramco flows (Sun–Thu week) |
| `XIDX` | IDX Jakarta | SE Asia | Commodities, EM Asia |
| `XIST` | Borsa Istanbul | EMEA | Macro volatility signal, lira dynamics |

## What ClaudIA receives in the system prompt

- Today's date and whether it is a trading day (NYSE reference)
- Last and next trading day
- Full holiday list for all 20 exchanges — proactive context for "why is volume low today?"
- **Futures vs Securities distinction** — explicitly injected so ClaudIA never confuses CME and equity schedules:
  - Most CME Globex products trade ~23h/day (Sun 5 PM CT → Fri 4 PM CT), daily 1h maintenance break 4–5 PM CT
  - IBKR routes all CME products via Globex (electronic only — no pit sessions)
  - **CME open when NYSE is closed**: MLK Day, Presidents Day, Memorial Day, Juneteenth, Labor Day, etc. — dynamically computed from exchange_calendars each session

## What the calendar is NOT: a source of trade dates (measured 2026-09-24)

The calendar is **market context** — sessions, closures, hours — for trading and
calculations. It never assigns a fill's trade date: that comes from the Flex statement alone
(`docs/trading-data-reference.md`, gap #69). Measured against CME's own holiday notices
(cmegroup.com holiday calendar + per-holiday PDFs):

- `exchange_calendars` 4.13.2 `CMES` (what `get_calendar("CME")` returns) is **one calendar
  for every CME product**. It gets the ordinary 18:00 ET evening roll right. All three holiday
  sessions tested — Labor Day, the Sunday before Memorial Day, Juneteenth 2026 — it dates **on
  the holiday itself**, where CME assigns holiday trading to the **next** trade date: for Labor
  Day 2026 CME wrote that orders "on Sunday, September 6th are for trade date Tuesday,
  September 8th". CME's notices say the same for MLK Day, Presidents' Day and Thanksgiving;
  those were not run against the library.
- Its 2026 CME closures are New Year's Day, Good Friday and Christmas. **Good Friday 2026 was
  not a uniform closure:** it was a jobs-report day, so per CME's notice FX, crypto and
  interest-rate products had "unique settlements derived on trade date April 3rd", while all
  other products had their settlements "copy/pasted from April 2nd". A single CME calendar
  cannot express a closure that varies by product.
- `pandas_market_calendars` 5.4.0 (a superset: it mirrors every `exchange_calendars` calendar
  and adds per-product CME calendars) gets the Good Friday split right, but it too dates
  the tested holiday sessions on the holiday. A possible future move to it is logged,
  **deferred**, in the core register (F18).

So "CME open when NYSE is closed" in the market context means Globex trades on that day, and,
per CME's holiday notices, those trades belong to the next trade date.

## CME product group schedule (`_FUTURES_SCHEDULE` in `store.py`)

| Group | Exchange | Globex Hours (CT) | Key products |
|---|---|---|---|
| Equity Index | CME | Sun 5 PM – Fri 4 PM (~23h) | ES, NQ, RTY, YM |
| Energy | NYMEX | Sun 5 PM – Fri 4 PM (~23h) | CL, NG, RB, HO |
| Metals | COMEX | Sun 5 PM – Fri 4 PM (~23h) | GC, SI, HG |
| Foreign Currency | CME | Sun 5 PM – Fri 4 PM (~23h) | 6E, 6J, 6B, 6A |
| Interest Rates | CBOT | Sun 5 PM – Fri 4 PM (~23h) | ZN, ZB, ZF, ZT |
| Agriculture/Grains | CBOT | Sun 7 PM – Fri 1:20 PM (~17h) | ZC, ZS, ZW — closes at 1:20 PM CT, **not 4 PM** |
| Softs/Livestock | CME/CBOT | Varies — shorter than financials | LE, GF, HE, CC |

## Performance (designed for zero marginal cost)

| Call | Time |
|---|---|
| First call per process (cold) | ~3.4s — exchange_calendars loads numpy arrays for 20 exchanges once |
| Subsequent calls same day | 0.01ms — process-level date-keyed cache hit |
| Next day / process restart | Recomputes fresh — cache key includes today's date |

Cache lives in `_market_calendar_cache` (module-level dict in `ibkr_core_mcp/store.py`). Key:
`(date_str, tuple(exchange_codes))` — auto-invalidates at midnight, no manual expiry needed.

**Staleness check** also uses the NYSE calendar: `stale = newest < penultimate_trading_day`
(the trading day *before* the last trading day, not the last trading day itself) — Flex
publishes yesterday's trades today, so `newest == last_trading_day` is always normal lag, not
staleness; only 2+ trading days behind counts as stale. Falls back to `days_since_newest > 2`
if `exchange_calendars` import fails. This correctly handles weekends and holidays — no false
stale on Saturdays, no missed sync after a holiday Monday.
