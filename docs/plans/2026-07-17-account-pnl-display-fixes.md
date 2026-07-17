# Account P&L Display Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two confirmed, live-reproduced bugs where ClaudIA shows no/wrong P&L despite IBKR having correct data available, and diagnose a third before deciding its fix.

**Architecture:** Task 1 removes two dead fields from an `ibkr_core_mcp` tool formatter that read keys an IBKR endpoint never returns (proven via live call + official docs). Task 2 is diagnostic-only — root-cause why a different IBKR endpoint returns empty, no fix until the cause is known. Task 3 adds a ledger-based fallback in `claudia_ui` for the case where ClaudIA's reactive P&L cache was never populated (e.g. the user traded before this ClaudIA session started), via one new shared helper used by both call sites.

**Tech Stack:** Python 3.11, pytest, IBKR Client Portal Web API (`ibkr_core_mcp`), Chainlit (`claudia_ui`)

---

## Context — how this was found

Live test session 2026-07-17 (`docs/plans/2026-07-18-live-test-session.md`, Phase 1/2). User traded ES futures same day (round-trip: bought 2, sold 2, flat by end — see `client.get_trades()` live output, execution at 2026-07-17 14:16:17 UTC, `"position": "0"` after the closing sale). IBKR Mobile correctly showed the resulting realized P&L. ClaudIA's opening status card showed:

```
Account Summary: Unrealized P&L: — | Realized P&L: —
Account P&L: Live P&L not yet available — no trade execution has been recorded yet,
or the execution listener may still be connecting.
```

Both are wrong — IBKR has this data right now. Three live REST calls during the session (read-only, no side effects) isolated exactly where each number lives:

| Call | Result | Verdict |
|---|---|---|
| `client.get_account_summary('U1675699')` (`/portfolio/{accountId}/summary`) | ~90 keys returned, **no** `unrealizedpnl`/`realizedpnl` key present | Confirmed via official docs too — this endpoint's documented response never includes these fields |
| `client.get_pnl()` (`/iserver/account/pnl/partitioned`) | `{"upnl": {}}` — completely empty | Root cause unknown — see Task 2 |
| `client.get_account_ledger('U1675699')` (`/portfolio/{accountId}/ledger`) | `realizedpnl: 461.56`, `unrealizedpnl: -8243.1` — correct, live, matches what the user sees on Mobile | This is the reliable source |

Docs pulled via Firecrawl keyless scrape of `https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/` (this session, 2026-07-17) confirm: the `/portfolio/{accountId}/summary` response schema has no P&L fields at all; the `/portfolio/{accountId}/ledger` response schema documents `unrealizedpnl` and `realizedpnl` explicitly, per-currency.

---

### Task 1: Remove dead P&L fields from `_get_account_summary`

**Files:**
- Modify: `ibkr_core_mcp/ibkr_core_mcp/claude_tools.py:134-141` (tool description), `:1208-1233` (`_get_account_summary`)
- Test: `ibkr_core_mcp/tests/claude_tools/test_account.py`

- [ ] **Step 1: Write the failing test**

Add to `ibkr_core_mcp/tests/claude_tools/test_account.py`, right after `test_execute_get_account_summary` (line 15):

```python
def test_get_account_summary_omits_pnl_fields(toolkit):
    """/portfolio/{accountId}/summary never returns unrealizedpnl/realizedpnl keys
    (confirmed live 2026-07-17 + official IBKR docs) — the formatter must not
    claim to show them. Real P&L data comes from get_ledger/get_pnl instead."""
    toolkit._client.get_accounts.return_value = [{"accountId": "U123"}]
    toolkit._client.get_account_summary.return_value = {
        "netliquidation": {"amount": 100000},
        "totalcashvalue": {"amount": 50000},
        "grosspositionvalue": {"amount": 20000},
        "buyingpower": {"amount": 80000},
    }
    text, fig = toolkit.execute("get_account_summary", {})
    assert fig is None
    assert "Unrealized P&L" not in text
    assert "Realized P&L" not in text
    assert "Net Liquidation" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/steph/Claude_Projects/ibkr_core_mcp && .venv/bin/pytest tests/claude_tools/test_account.py::test_get_account_summary_omits_pnl_fields -v`
Expected: FAIL — `assert "Unrealized P&L" not in text` fails because the current formatter always emits that line.

- [ ] **Step 3: Remove the two dead lines and update the tool description**

In `ibkr_core_mcp/ibkr_core_mcp/claude_tools.py`, change `_get_account_summary` (currently lines 1224-1232):

```python
        lines = [
            f"Account:             {summary.get('accountcode', {}).get('value', account_id)}",
            f"Net Liquidation:     {_fmt('netliquidation')}",
            f"Cash:                {_fmt('totalcashvalue')}",
            f"Gross Position Val:  {_fmt('grosspositionvalue')}",
            f"Buying Power:        {_fmt('buyingpower')}",
        ]
        return "\n".join(lines), None
```

And update the tool description (currently lines 134-141):

```python
    {
        "name": "get_account_summary",
        "description": (
            "Retrieve account net liquidation value, cash balance, gross position "
            "value, and buying power from IBKR — a single aggregate snapshot for "
            "the account. This endpoint does not carry P&L fields — for realized/"
            "unrealized P&L use get_ledger (per-currency) or get_pnl (per account "
            "partition, no realized figure); for per-position detail use get_positions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/steph/Claude_Projects/ibkr_core_mcp && .venv/bin/pytest tests/claude_tools/test_account.py -v`
Expected: PASS, all tests in the file green (this also covers the pre-existing `test_execute_get_account_summary`, which only asserts non-empty text so it isn't affected by the removed lines).

- [ ] **Step 5: Run the full unit suite**

Run: `cd /Users/steph/Claude_Projects/ibkr_core_mcp && .venv/bin/pytest -m "not integration and not live"`
Expected: PASS, same count as before minus zero (no tests removed) plus one new.

- [ ] **Step 6: Update `docs/tools-reference.md`'s `get_account_summary` entry**

In `ibkr_core_mcp/docs/tools-reference.md`, find the `get_account_summary` section and remove any claim that it returns P&L, pointing to `get_ledger`/`get_pnl` instead — mirror the new tool description text from Step 3.

- [ ] **Step 7: Commit**

```bash
cd /Users/steph/Claude_Projects/ibkr_core_mcp
git add ibkr_core_mcp/claude_tools.py tests/claude_tools/test_account.py docs/tools-reference.md
git commit -m "fix: get_account_summary no longer claims P&L fields /portfolio/summary never returns

Live-verified 2026-07-17: /portfolio/{accountId}/summary's ~90-key response has
no unrealizedpnl/realizedpnl field (confirmed against official IBKR docs too) —
_get_account_summary was reading two keys that can never be present, always
rendering '—'. Real P&L data lives in get_ledger (realized+unrealized,
per-currency) and get_pnl (daily+unrealized, per account partition)."
```

---

### Task 2: Diagnose why `get_pnl` (`/iserver/account/pnl/partitioned`) returns empty

**This is diagnostic-first, matching this repo's established pattern (see Task 5 of
`docs/plans/2026-07-10-live-test-bugfixes.md`). Do not write a fix until the cause is
confirmed — the symptom might be a one-off session quirk, a documented-but-easy-to-miss
precondition, or a real bug.**

**Files:** none yet — this task only produces findings, written into `docs/project-status.md`'s Known Gaps.

- [ ] **Step 1: Reproduce with a fresh call**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
.venv/bin/python3 -c "
import json
from ibkr_core_mcp.client import IBKRClient
from ibkr_core_mcp.config import Config
client = IBKRClient(config=Config.from_env())
print(json.dumps(client.get_pnl(), indent=2))
"
```
Expected (per this session's finding): `{"upnl": {}}`. If it now returns real data unprompted, note the timestamp and treat as transient — still do Step 2 below at least once to rule out a warm-up dependency before closing this out as "self-resolved, cause unknown."

- [ ] **Step 2: Test the WebSocket-priming hypothesis**

The design doc `docs/plans/2026-07-07-execution-triggered-pnl-design.md` notes `ExecutionListener` transiently subscribes to the `spl` WebSocket topic to capture P&L — this is suspicious because IBKR's `/iserver/marketdata/snapshot` is documented to need an analogous "warm-up" subscription before it returns real data (see `ibkr_core_mcp/docs/tools-reference.md:334-335`). Test whether `/iserver/account/pnl/partitioned` has the same undocumented dependency:

```bash
cd /Users/steph/Claude_Projects/claudia_ui
.venv/bin/python3 -c "
import asyncio, os, json
import requests
from ibkr_core_mcp.auth import BrowserCookieAuth
from ibkr_core_mcp.streaming import IBKRWebSocket
from ibkr_core_mcp.client import IBKRClient
from ibkr_core_mcp.config import Config

async def main():
    session = requests.Session()
    BrowserCookieAuth(os.environ.get('IBKR_AUTH_BROWSER', 'chrome')).apply(session)
    cookie = session.headers.get('Cookie', '')
    config = Config.from_env()
    ws = IBKRWebSocket(config.gateway_url, cookie)
    await ws.connect()
    await ws.subscribe_pnl()
    async for item in ws.listen():
        print('WS item:', item)
        break
    await ws.unsubscribe_pnl()
    await ws.disconnect()
    client = IBKRClient(config=config)
    print('REST get_pnl after WS spl touch:', json.dumps(client.get_pnl()))

asyncio.run(main())
"
```
- If the REST call now returns real data: the endpoint needs a `spl` WS subscription touch first, same class of quirk as market data snapshots. Proceed to Step 3a.
- If it's still empty: the cause is something else (account/session state, IBKR-side issue, wrong account context). Proceed to Step 3b.

- [ ] **Step 3a (if WS-priming confirmed): decide the fix shape, don't implement yet**

Options to weigh with the user before writing code: (a) have `_get_pnl` (`ibkr_core_mcp/claude_tools.py:2099`) open a throwaway WS connection, subscribe to `spl`, wait for one tick, then hit the REST endpoint — expensive and duplicates `ExecutionListener`'s job; (b) document the precondition and leave `get_pnl` as "only reliable after ExecutionListener has been running a moment" (since claudia_ui always runs it); (c) since Task 3 below already gives `_get_live_pnl`/opening-card a ledger fallback that doesn't need this endpoint at all, consider whether fixing `get_pnl` itself is even still worth doing, or whether to just document the quirk in `tools-reference.md` and move on.

- [ ] **Step 3b (if not WS-priming): check for account/session-context prerequisites**

WebFetch the account-pnl doc section again for any prerequisite call (the ledger/summary sections both mention "`/portfolio/accounts` or `/portfolio/subaccounts` must be called prior to this endpoint" — check whether `pnl/partitioned` has the same undocumented-in-the-scrape-we-grabbed prerequisite by searching the full scraped page saved this session, or re-scraping). Test calling `client.get_accounts()` or a `/portfolio/accounts` hit immediately before `get_pnl()` in the same session to see if that changes the result.

- [ ] **Step 4: Record the finding**

Regardless of outcome, add a row to `docs/project-status.md`'s Known Gaps table dated 2026-07-17 describing what was found, whether a fix is warranted, and if not, why (e.g. "superseded by Task 3's ledger fallback — no user-visible impact remains").

---

### Task 3: Ledger fallback for cold-start P&L display

**Problem:** `ExecutionListener` only populates `SQLiteStore.pnl_snapshots` reactively, when it
observes a live trade execution during its own process's lifetime (by design — see
`docs/plans/2026-07-07-execution-triggered-pnl-design.md`). If the user's last trade happened
before this ClaudIA process started (very common — trades happen via Mobile/TWS independent of
whether ClaudIA is open), `store.get_latest_pnl()` returns `None` forever until the next trade,
and both `_get_live_pnl` (agent.py) and the opening status card (app.py) show "Live P&L not yet
available" even though real P&L exists. `get_account_ledger` (`/portfolio/{accountId}/ledger`)
is proven live, immediate, and correct (Task 1's findings) — use it as the fallback. Both call
sites currently duplicate the same "read cache, format" logic, so this task extracts one shared
helper instead of patching each site separately.

**Files:**
- Modify: `claudia_ui/claudia/execution_listener.py` (add `get_live_pnl_text`, next to `format_pnl_snapshot`)
- Modify: `claudia_ui/claudia/agent.py:731-738` (`_get_live_pnl` calls the new helper)
- Modify: `claudia_ui/claudia/app.py:381-397` (opening status block calls the new helper)
- Test: `claudia_ui/tests/test_execution_listener.py` (new helper), `claudia_ui/tests/test_agent.py:372-389` (update existing `_get_live_pnl` tests)

- [ ] **Step 1: Write the failing tests for the new helper**

Add to `claudia_ui/tests/test_execution_listener.py`, right after the `format_pnl_snapshot` tests (after line ~460, following the existing `# ── format_pnl_snapshot ──` section):

```python
# ── get_live_pnl_text ──────────────────────────────────────────────────────────

def test_get_live_pnl_text_uses_cache_when_populated():
    from unittest.mock import MagicMock
    from claudia.execution_listener import get_live_pnl_text
    toolkit = MagicMock()
    toolkit._store.get_latest_pnl.return_value = {
        "account": "U1675699.Core", "dpl": 12.5, "nl": 10000.0,
        "upl": 3.0, "uel": 9000.0, "mv": 5000.0,
    }
    result = get_live_pnl_text(toolkit)
    assert "U1675699.Core" in result
    toolkit.execute.assert_not_called()


def test_get_live_pnl_text_falls_back_to_ledger_when_cache_empty():
    from unittest.mock import MagicMock
    from claudia.execution_listener import get_live_pnl_text
    toolkit = MagicMock()
    toolkit._store.get_latest_pnl.return_value = None
    toolkit.execute.return_value = ("Account Ledger (USD):\n  Realized P&L : +461.56", None)
    result = get_live_pnl_text(toolkit)
    assert "Realized P&L" in result
    assert "+461.56" in result
    toolkit.execute.assert_called_once_with("get_ledger", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/steph/Claude_Projects/claudia_ui && .venv/bin/pytest tests/test_execution_listener.py -k get_live_pnl_text -v`
Expected: FAIL — `ImportError: cannot import name 'get_live_pnl_text'`.

- [ ] **Step 3: Add the helper**

In `claudia/execution_listener.py`, immediately after `format_pnl_snapshot` (after line 85):

```python
def get_live_pnl_text(toolkit: Any) -> str:
    """Best-available live P&L text for display: the ExecutionListener's last
    captured snapshot if this process observed a trade execution, otherwise a
    live ledger pull.

    The reactive cache (SQLiteStore.pnl_snapshots) is empty whenever no
    execution has been observed during this process's lifetime — e.g. the
    user's last trade happened before ClaudIA started, or in an earlier
    session. get_account_ledger (/portfolio/{accountId}/ledger) has no such
    dependency: it returns correct realized/unrealized P&L on every call,
    live-verified 2026-07-17 (docs/plans/2026-07-17-account-pnl-display-fixes.md).
    """
    latest = toolkit._store.get_latest_pnl()
    if latest is not None:
        return format_pnl_snapshot(latest)
    text, _ = toolkit.execute("get_ledger", {})
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/steph/Claude_Projects/claudia_ui && .venv/bin/pytest tests/test_execution_listener.py -k get_live_pnl_text -v`
Expected: PASS.

- [ ] **Step 5: Update `agent.py` to use the helper, update its tests**

In `claudia/agent.py`, replace `_get_live_pnl` (lines 731-738):

```python
    def _get_live_pnl(self) -> str:
        """Best-available live P&L text — see execution_listener.get_live_pnl_text
        for the cache-then-ledger-fallback logic. Never raises."""
        from claudia.execution_listener import get_live_pnl_text
        return get_live_pnl_text(self._toolkit)
```

In `claudia_ui/tests/test_agent.py`, replace `test_handle_local_tool_get_live_pnl_none` (lines 372-376) — it asserted the old no-fallback "not yet available" behavior, which is no longer correct once cache-miss falls through to ledger:

```python
def test_handle_local_tool_get_live_pnl_none_falls_back_to_ledger():
    agent = _make_agent()
    agent._toolkit._store.get_latest_pnl.return_value = None
    agent._toolkit.execute.return_value = ("Account Ledger (USD):\n  Realized P&L : +461.56", None)
    result = agent._handle_local_tool("get_live_pnl", {})
    assert "Realized P&L" in result
    agent._toolkit.execute.assert_called_once_with("get_ledger", {})
```

Leave `test_handle_local_tool_get_live_pnl_populated` and
`test_handle_local_tool_get_live_pnl_partial_fields_format_as_na` (lines 360-369, 379-389)
unchanged — they test the cache-populated path, which `get_live_pnl_text` still delegates to
`format_pnl_snapshot` unchanged.

- [ ] **Step 6: Run agent tests**

Run: `cd /Users/steph/Claude_Projects/claudia_ui && .venv/bin/pytest tests/test_agent.py -k get_live_pnl -v`
Expected: PASS, 3 tests (2 unchanged + 1 renamed/rewritten).

- [ ] **Step 7: Update the opening status block in `app.py`**

In `claudia/app.py`, replace lines 381-387:

```python
        (opening_text, _), (orders_text, _), (positions_text, _) = await asyncio.gather(
            cl.make_async(toolkit.execute)("get_account_summary", {}),
            cl.make_async(toolkit.execute)("get_live_orders", {}),
            cl.make_async(toolkit.execute)("get_positions", {}),
        )
        from claudia.execution_listener import get_live_pnl_text
        pnl_text = await cl.make_async(get_live_pnl_text)(toolkit)
        status_block = (
            f"**Account Summary**\n{opening_text}\n\n"
            f"**Open Positions**\n{positions_text}\n\n"
            f"**Account P&L**\n{pnl_text}\n\n"
            f"**Live Orders**\n{orders_text}"
        )
```

(This drops `get_latest_pnl` from the `asyncio.gather` tuple since `get_live_pnl_text` now
owns that call internally — check for an unused `format_pnl_snapshot` import at the top of
`app.py` afterward and remove it if this was the only use.)

- [ ] **Step 8: Run the full unit suite**

Run: `cd /Users/steph/Claude_Projects/claudia_ui && .venv/bin/pytest -m "not integration"`
Expected: PASS, 296 + 2 new = 298.

- [ ] **Step 9: Commit**

```bash
cd /Users/steph/Claude_Projects/claudia_ui
git add claudia/execution_listener.py claudia/agent.py claudia/app.py tests/test_execution_listener.py tests/test_agent.py
git commit -m "fix: fall back to live ledger P&L when the reactive execution cache is empty

ExecutionListener's pnl_snapshots cache only populates when it observes a trade
execution during this process's lifetime — a trade placed before ClaudIA started
(the common case for Mobile/TWS trades) left both get_live_pnl and the opening
status card stuck on 'not yet available' forever, even with real P&L sitting in
IBKR. get_account_ledger has no such dependency (live-verified 2026-07-17) — new
shared helper get_live_pnl_text() tries the cache first, falls back to a live
ledger call otherwise. Found live during the 2026-07-17 test session
(docs/plans/2026-07-17-account-pnl-display-fixes.md)."
```

- [ ] **Step 10: Live re-verification (needs a running ClaudIA + gateway session)**

Restart ClaudIA fresh (no trade yet this process) and confirm the opening status card's
"Account P&L" section now shows real ledger data instead of "not yet available". Then ask
ClaudIA "what's my P&L" in chat and confirm `get_live_pnl` returns the same.

---

## Self-Review

**Spec coverage:** Task 1 covers the confirmed-dead `_get_account_summary` fields. Task 2 covers
the unexplained empty `get_pnl` response (diagnostic, not a blind fix — appropriate given the
cause isn't known yet). Task 3 covers the cold-start reactive-cache gap the user actually hit
this session, via one shared fallback rather than duplicating fix logic across `agent.py` and
`app.py`.

**Placeholder scan:** No TBD/"add error handling"/vague steps — every code step shows the exact
diff or full new function.

**Type consistency:** `get_live_pnl_text(toolkit: Any) -> str` is used identically in both
`agent.py` and `app.py`; `toolkit.execute("get_ledger", {})` return shape `tuple[str, Any]`
matches the existing convention used everywhere else in `claude_tools.py`/`agent.py`.
