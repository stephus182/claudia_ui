# Trading Principles & Strategy Guidelines

<!--
  ClaudIA reads this document before every response and must verify all
  proposed actions are consistent with these principles.

  This file is personal — do not commit it to version control.
  These principles are a living document — they can be refined over time,
  but in-session overrides do not constitute a permanent rule change.

  Behavioral rules are covered in the separate CLAUDIA_CONTEXT.md file.
-->

---

## 1. Core Philosophy

- This is **trading, not investing**. Fundamental analysis and long-hold
  strategies are not the objective.
- The objective is to **lock in gains and generate consistent returns** —
  more gains than losses, compounded over time.
- **Discipline is the edge.** Consistency beats brilliance. Small profitable
  ugly trades are as valuable as big beautiful ones — this is a money game,
  not an art game.
- **Humility is non-negotiable.** When wrong, accept the mistake, take the
  loss, and move on. Never be stubborn about a position.
- **Patience is a position.** If an entry is missed, wait. Do not chase.
- **Focus over breadth.** A small number of well-understood positions beats a
  scattered portfolio. Too many open positions is a distraction and a risk.
- Money is hard to make and easy to lose. Every trade must have more expected
  upside than downside — think in terms of expected returns, not hope.

---

## 2. Risk Management

### Hard limits

| Rule | Limit |
|---|---|
| Maximum loss per trade | $10,000 (or equivalent in EUR, CHF, GBP, JPY) |
| Maximum loss per day | $5,000 (or equivalent in EUR, CHF, GBP, JPY) |
| Maximum open trades | 4 to 5 — a L/S pair counts as one trade |

### Position sizing

- Size must be consistent with the max loss limit above. Before entering,
  confirm that the worst-case exit scenario does not breach $10,000.
- Never size up into a losing position to average down the entry price.
  This is a hard rule, not a guideline.

### Gain protection (locking unrealised profits)

- When a position reaches **$1,000 in unrealised gains**, ClaudIA will note
  the threshold has been reached.
- At approximately **$1,200 unrealised gain**, ClaudIA will offer to stage
  a stop order at the $1,000 level — locking in a minimum profit on pullback.
- The "locking unrealised gains" approach should be treated as a discipline
  reminder: a minimum guaranteed profit is worth securing even if the position
  could go further.
- Preferred use of stops is to **protect profits**, not to cap losses.
  This distinction matters — see Section 4 on stops.

### Weekend short exposure

- Being short into a weekend carries specific gap risk. ClaudIA will always
  flag an open short position before Friday close and ask for explicit
  confirmation to hold it over the weekend.

### Consecutive loss pause

- After a losing trade, **one recovery trade is permitted** during the same
  day. If that trade also results in a loss, trading stops for the day.
- ClaudIA will flag when a recovery trade is being entered and will not
  propose further entries after two intraday losses without an explicit
  override and acknowledgment.
- This rule exists to prevent emotional escalation. Two losses in a day is
  a signal the market is not offering good conditions — not a challenge to
  fight back against.

### Expected return discipline

- No trade should be entered where the downside scenario exceeds the
  upside scenario. If the math doesn't work, the trade doesn't work.
- ClaudIA should always present an expected return framing alongside any
  trade proposal — not just price targets.

---

## 3. Order Types & Execution

### Entries — limit orders required

- **All new positions must have a defined limit level at entry.** The level
  is chosen in advance, based on technical analysis.
- If the level is hit, the position is initiated. If not, wait or revisit —
  never chase with a market order.
- Time in force for entry limit orders: **GTC "Day+"** — to capture
  favorable liquidity outside regular hours.

### Exits — scaling and limit orders

- Closing a position should use a limit order at the target level,
  based on technical analysis.
- Time in force for exit limit orders: **GTC "Day+"**.

**Scaling out by instrument:**

- **Equities (larger positions):** scale out of winning positions in
  tranches. Do not exit the full position at once — take partial profits
  at intermediate levels and let the remainder run to the full target.
- **Futures (1–2 lots):** full exit at once is standard given the small
  lot size. Scaling is not practical at this size.

### Stops — profit protection, not loss control

- **The primary use of stop orders is to lock in unrealised gains**,
  not to define a fixed loss exit.
- The stop-loss model has known adverse behavior in volatile, thin-liquidity
  conditions: algorithmic participants are specifically designed to trigger
  stops on quick moves that fully recover. Stops set too close to spot price
  will be hit on noise, not signal.
- Stops can be used as a **maximum notional loss protection** — set far
  from current price, acting as a circuit breaker rather than a tactical exit.
- Time in force for stop orders: **GTC "Day"** — to avoid execution in thin
  after-hours markets where liquidity gaps are worst.

---

## 4. Technical Analysis Framework

### Preferred indicators

- Support and resistance levels (horizontal)
- Moving averages: **25, 50, 100, 200** time-unit MAs
- Standard deviation bands around a moving average
- Volume (used to confirm or challenge a directional move)

### Preferred chart timeframes

- **5-minute** candles — intraday entries and scalp timing
- **Hourly** candles — intraday trend and structure
- **Daily** candles — multi-day trend and key levels

Analysis should generally be conducted top-down: daily to hourly to 5-minute.

---

## 5. Strategies

### Strategy 1 — Trend Following

**Core idea**: Identify the local trend (a directional channel), trade with
it, and use technical levels for entry and exit.

- In an **uptrend**: go long. Buy support, sell resistance or target.
  Do not short an uptrend.
- In a **downtrend**: go short. Sell resistance, cover at support or target.
  Do not go long in a downtrend.
- If an exit is missed, the position direction aligns with the trend —
  there remains a possibility of recovery. Do not panic.
- Identifying the direction correctly is the most important step. This
  comes before any sizing or order discussion.

### Strategy 2 — Horizontal Range (Support & Resistance)

**Core idea**: Certain price levels act as magnets — revisited multiple
times as tests. When levels are clearly identified, a position can be
initiated long or short.

- Long from support, short from resistance, with targets at the
  opposing level.
- Quality of the level matters: more touches = stronger level.
- If a level breaks cleanly, reassess the setup — do not defend it.

### Strategy 3 — Mean Reversion

**Core idea**: After a sharp price move that is statistically extended
(measured in standard deviations from a moving average) with fading
volume, a reversion trade can be initiated.

- Short after a large price gain with fading volume.
- Long after a sharp price drop with fading volume.
- Reversion trades are fast — use preset entry and exit levels.
- Key risk: distinguishing reversion from momentum continuation.
  Strong volume into the extension signals momentum acceleration, not
  reversion. Do not fade strong-volume moves.
- ClaudIA should always include the standard deviation reading and
  volume assessment when proposing a reversion trade.

---

## 6. Markets & Instruments

### Futures

- Highly leveraged. Margin deposit is a small fraction of notional.
- Intended for **short-term trading** — typically a few days, not long hold.
- Typically traded in 1–2 lots. Full exit at once is standard.
- Apply trend, horizontal, and reversion strategies.
- Apply the weekend short rule with extra caution given leverage.

### US Equities & ETFs — L/S Pair Trading

- Intended for **medium-term holding** — can hold several weeks or months.
- Primary strategy: **Long/Short pair trades**.
- Larger position sizes allow scaling — always exit equities in tranches.

**L/S pair trade process (in order):**

1. **Quantitative study first.** Before entering any L/S pair, run a
   correlation analysis and ratio study. Understand the historical
   relationship between the two names. No entry without this step.
2. **Enter by tranches.** Initiate the pair at partial size — do not
   put on the full position at once.
3. **Add size only if the trade improves.** If the ratio moves in the
   expected direction after entry, additional tranches can be added.
   Never add size if the ratio is moving against the position.
4. **Do not chase.** If the ratio has already moved significantly before
   entry, the opportunity is missed. Wait for the next setup.
5. Track the **ratio** as the primary signal — not the individual legs.
   The ratio action determines whether the trade is working.

- A L/S pair (both legs combined) counts as **one trade** against the
  4–5 open trade maximum.
- Single stocks and ETFs are both eligible — no instrument restrictions
  within US equities.

---

## 7. ClaudIA Interaction Rules

- **Always show expected return framing** before proposing a trade for
  staging — not just price targets.
- **Always show the order preview** (instrument, side, size, entry,
  stop if applicable, target, notional exposure) before staging.
- **Verify a limit level is defined** before staging any new entry.
  A market order entry is a principle violation.
- **Flag every principle conflict** explicitly and visibly. Do not silently
  adjust parameters to work around a rule.
- **Principle conflict = warning, not block.** ClaudIA raises the conflict
  clearly, identifies which principle is being violated, and proceeds only
  after explicit user acknowledgment. The override is logged.
- **Never fabricate data.** If ClaudIA lacks sufficient information to give
  a confident view, it says so and identifies what is missing.
- **When proposing a mean reversion trade**, always include the standard
  deviation reading and volume assessment.
- **When requesting a backtest**, always include: Sharpe ratio, Sortino
  ratio, max drawdown, win rate, expectancy, and profit factor. Flag any
  concerns about sample size or overfitting.
- **Remind the user of the "locking gains" option** when unrealised profit
  thresholds are reached — don't wait to be asked.
- **Flag weekend short exposure** proactively before Friday close.
- **Enforce the consecutive loss rule**: flag the recovery trade when it
  is entered; do not propose further trades after two intraday losses
  without explicit override.
- **For L/S equity pairs**: require confirmation that a quantitative study
  has been completed before staging the first tranche. Flag any attempt
  to add size when the ratio is moving against the position.
- If a trade idea has **merit but violates a principle**, say so explicitly:
  state the merit, state the conflict, and let the user decide.

---

*This file is read by ClaudIA at every session start.*
*Behavioral and coaching rules are in CLAUDIA_CONTEXT.md.*
*Principles evolve — update this file when rules change deliberately.*
*In-session overrides are logged but do not modify this document.*