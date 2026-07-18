# IBKR Soft-Timeout Silent Recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the IBKR gateway's brokerage session soft-times-out (idle >5-6 min without a
`/tickle`), silently re-establish it via the documented `POST /iserver/auth/ssodh/init`
endpoint — no browser, no 2FA — instead of forcing the user through a manual re-login every
time this specific, recoverable state occurs.

**Architecture:** All changes live in `claudia/status.py`'s `ConnectivityChecker` — no changes
to `ibkr_core_mcp` (it already has an unused, deliberately-not-wired `reauthenticate()` for the
*deprecated* endpoint; this plan does not touch it). The recovery attempt is gated by a single
narrow condition — the previous poll was `OK` **and** the current poll shows the exact
documented soft-timeout signature (`connected:true, authenticated:false`) — so it can never fire
during the fragile first-seconds-after-login window (that transition starts from `UNKNOWN`, not
`OK`) and never on a hard disconnect (`connected:false`). `compete` is hardcoded `false` always:
this must never force-evict a concurrent IBKR Mobile/TWS session. This does **not** touch
`claudia/agent.py`'s hardcoded order-execution safety block — it is session-liveness code only,
nowhere near `place_order`/`modify_order`/`cancel_order`.

**Tech Stack:** Python, `requests`, `pytest` + `pytest-asyncio`, `unittest.mock`.

**Sources (verified 2026-07-17, scraped via Firecrawl):**
- Soft-timeout signature + `ssodh/init` recovery: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ssodh-init
- `compete` param semantics ("disconnects other brokerage sessions"): same page, "Initialize Brokerage Session" section
- `/iserver/reauthenticate` deprecated, superseded by `ssodh/init`: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#reauthenticate
- Full breakdown already in `docs/connectivity.md` § Session lifecycle

---

## File Structure

- Modify: `claudia/status.py` — add `_last_ibkr_auth_status` state, capture it in `check_ibkr()`,
  add `_attempt_soft_recovery()`, wire it into `_run_checks()`
- Modify: `tests/test_status.py` — new unit + integration tests for the above
- Modify: `docs/connectivity.md` — flip the existing "not yet implemented" note to "implemented"
- Modify: `docs/project-status.md`'s `## Live Test Log` section — log the live-test result (Task 6, after Task 5 actually runs)

No new files, no new dependencies, no `ibkr_core_mcp` changes.

---

### Task 1: Capture the full auth-status detail during `check_ibkr()`

`check_ibkr()` currently discards everything except the `authenticated AND connected` boolean.
The recovery logic in Task 3 needs the individual `connected`/`authenticated` values from the
*same* `/tickle` call — not a second HTTP round-trip — so stash them as instance state.

**Files:**
- Modify: `claudia/status.py:74-79` (`__init__`), `claudia/status.py:87-106` (`check_ibkr`)
- Test: `tests/test_status.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_status.py` near the other `check_ibkr` tests (after line 68, before the
`check_gdrive` section):

```python
def test_check_ibkr_ok_stashes_auth_status(checker):
    with patch("claudia.status.requests.get", return_value=_ibkr_ok_response()):
        checker.check_ibkr()
    assert checker._last_ibkr_auth_status == {"authenticated": True, "connected": True}


def test_check_ibkr_soft_timeout_stashes_auth_status(checker):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    with patch("claudia.status.requests.get", return_value=m):
        assert checker.check_ibkr() is False
    assert checker._last_ibkr_auth_status == {"authenticated": False, "connected": True}


def test_check_ibkr_non_200_clears_auth_status(checker):
    checker._last_ibkr_auth_status = {"authenticated": True, "connected": True}
    m = MagicMock()
    m.status_code = 401
    with patch("claudia.status.requests.get", return_value=m):
        checker.check_ibkr()
    assert checker._last_ibkr_auth_status == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_status.py -k "stashes_auth_status or clears_auth_status" -v`
Expected: FAIL with `AttributeError: 'ConnectivityChecker' object has no attribute '_last_ibkr_auth_status'`

- [ ] **Step 3: Add the instance attribute**

In `claudia/status.py`, inside `__init__` (after the existing `self._status = {...}` block,
before `self._task: asyncio.Task | None = None`):

```python
        self._last_ibkr_auth_status: dict = {}
```

- [ ] **Step 4: Capture it in `check_ibkr()`**

Replace the body of `check_ibkr()` (`claudia/status.py:93-106`):

```python
        try:
            resp = requests.get(
                f"{self._gateway_url}/tickle",
                timeout=3,
                verify=False,  # IBKR gateway uses a self-signed cert on localhost
            )
            if resp.status_code != 200:
                self._last_ibkr_auth_status = {}
                return False
            auth = resp.json().get("iserver", {}).get("authStatus", {})
            self._last_ibkr_auth_status = auth
            if auth.get("competing"):
                log.warning("IBKR: competing session detected — another TWS/gateway session is active")
            return bool(auth.get("authenticated") and auth.get("connected"))
        except Exception:
            self._last_ibkr_auth_status = {}
            return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_status.py -v`
Expected: all PASS, including the 3 new tests and every pre-existing `check_ibkr`/`_run_checks` test (no regressions — the public `bool` contract is unchanged).

- [ ] **Step 6: Commit**

```bash
git add claudia/status.py tests/test_status.py
git commit -m "feat: stash full IBKR auth-status detail during check_ibkr()"
```

---

### Task 2: `_attempt_soft_recovery()` — the `ssodh/init` call itself

A standalone, directly-testable method. It does not decide *when* to run (that's Task 3) — it
only performs the recovery POST and reports success/failure.

**Files:**
- Modify: `claudia/status.py` (new method, place after `check_tradingview`, before the
  `# ── Lifecycle ──` section marker)
- Test: `tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_status.py`:

```python
def test_attempt_soft_recovery_success(checker):
    m = MagicMock()
    m.status_code = 200
    with patch("claudia.status.requests.post", return_value=m) as mock_post:
        assert checker._attempt_soft_recovery() is True
    mock_post.assert_called_once_with(
        "https://localhost:5055/v1/api/iserver/auth/ssodh/init",
        json={"publish": True, "compete": False},
        timeout=5,
        verify=False,
    )


def test_attempt_soft_recovery_non_200_returns_false(checker):
    m = MagicMock()
    m.status_code = 500
    with patch("claudia.status.requests.post", return_value=m):
        assert checker._attempt_soft_recovery() is False


def test_attempt_soft_recovery_exception_returns_false(checker):
    with patch("claudia.status.requests.post", side_effect=req.ConnectionError()):
        assert checker._attempt_soft_recovery() is False


def test_attempt_soft_recovery_never_sets_compete_true(checker):
    """Regression guard: compete must never be true — it would force-evict a
    concurrent IBKR Mobile/TWS session."""
    m = MagicMock()
    m.status_code = 200
    with patch("claudia.status.requests.post", return_value=m) as mock_post:
        checker._attempt_soft_recovery()
    assert mock_post.call_args.kwargs["json"]["compete"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_status.py -k "attempt_soft_recovery" -v`
Expected: FAIL with `AttributeError: 'ConnectivityChecker' object has no attribute '_attempt_soft_recovery'`

- [ ] **Step 3: Implement `_attempt_soft_recovery()`**

Add to `claudia/status.py`, after `check_tradingview()` and before the
`# ── Lifecycle ──────` section:

```python
    def _attempt_soft_recovery(self) -> bool:
        """Silently re-establish a soft-timed-out brokerage session.

        Only ever called from _run_checks() when the previous poll was OK and the
        current poll shows IBKR's documented soft-timeout signature
        (connected=true, authenticated=false) — never on a fresh/settling login
        (that transition starts from UNKNOWN) and never on a hard disconnect
        (connected=false). `compete` is hardcoded False: it must never force-evict
        a concurrent IBKR Mobile/TWS session — if a real competing session is the
        actual cause, this call fails harmlessly and the normal disconnect alert
        fires instead.

        Source: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#ssodh-init
        Endpoint: POST /iserver/auth/ssodh/init
        """
        try:
            resp = requests.post(
                f"{self._gateway_url}/iserver/auth/ssodh/init",
                json={"publish": True, "compete": False},
                timeout=5,
                verify=False,
            )
            return resp.status_code == 200
        except Exception:
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_status.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add claudia/status.py tests/test_status.py
git commit -m "feat: add _attempt_soft_recovery() for IBKR ssodh/init"
```

---

### Task 3: Wire recovery into `_run_checks()`

This is the only place the narrow "was OK, now soft-timed-out" condition is evaluated. If
recovery succeeds, `ibkr_ok` flips back to `True` **before** the transition-alert logic runs —
so from the user's perspective nothing ever visibly disconnected.

**Files:**
- Modify: `claudia/status.py:173-176` (`_run_checks`)
- Test: `tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_status.py`, near the other `_run_checks` state-transition tests:

```python
@pytest.mark.asyncio
async def test_run_checks_recovers_silently_from_soft_timeout(checker_with_token):
    """OK -> soft-timeout -> ssodh/init succeeds -> stays OK, no alert."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    recovery_resp = MagicMock()
    recovery_resp.status_code = 200

    with patch(
        "claudia.status.requests.get",
        side_effect=[soft_timeout_resp, _ibkr_ok_response()],
    ), patch(
        "claudia.status.requests.post", return_value=recovery_resp
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ) as mock_alert:
        await checker_with_token._run_checks()

    mock_post.assert_called_once()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.OK
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert ibkr_calls == []  # never visibly disconnected


@pytest.mark.asyncio
async def test_run_checks_soft_recovery_failure_falls_back_to_disconnect_alert(checker_with_token):
    """OK -> soft-timeout -> ssodh/init fails -> normal ERROR alert, same as today."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }
    failed_recovery = MagicMock()
    failed_recovery.status_code = 500

    with patch(
        "claudia.status.requests.get", return_value=soft_timeout_resp
    ), patch(
        "claudia.status.requests.post", return_value=failed_recovery
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ) as mock_alert:
        await checker_with_token._run_checks()

    mock_post.assert_called_once()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
    ibkr_calls = [c for c in mock_alert.call_args_list if c.args[0] == "ibkr"]
    assert len(ibkr_calls) == 1
    assert ibkr_calls[0].args[1] == ServiceStatus.OK
    assert ibkr_calls[0].args[2] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_no_recovery_attempt_from_unknown_state(checker_with_token):
    """UNKNOWN -> soft-timeout-shaped response: never attempt recovery — this is the
    fresh/settling-login window, exactly what the existing no-proactive-reauth rule
    protects. Must go straight to a normal ERROR, untouched."""
    soft_timeout_resp = MagicMock()
    soft_timeout_resp.status_code = 200
    soft_timeout_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": True}}
    }

    with patch(
        "claudia.status.requests.get", return_value=soft_timeout_resp
    ), patch(
        "claudia.status.requests.post"
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ):
        await checker_with_token._run_checks()

    mock_post.assert_not_called()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR


@pytest.mark.asyncio
async def test_run_checks_no_recovery_attempt_on_hard_disconnect(checker_with_token):
    """OK -> connected:false (hard disconnect, e.g. competing session or container
    down): never attempt recovery — ssodh/init cannot fix this, only a real
    browser+2FA login can."""
    checker_with_token._status["ibkr"] = ServiceStatus.OK

    hard_disconnect_resp = MagicMock()
    hard_disconnect_resp.status_code = 200
    hard_disconnect_resp.json.return_value = {
        "iserver": {"authStatus": {"authenticated": False, "connected": False}}
    }

    with patch(
        "claudia.status.requests.get", return_value=hard_disconnect_resp
    ), patch(
        "claudia.status.requests.post"
    ) as mock_post, patch.object(
        checker_with_token, "_send_alert", new_callable=AsyncMock
    ):
        await checker_with_token._run_checks()

    mock_post.assert_not_called()
    assert checker_with_token.get_status()["ibkr"] == ServiceStatus.ERROR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_status.py -k "recover" -v`
Expected: `test_run_checks_recovers_silently_from_soft_timeout` and
`test_run_checks_soft_recovery_failure_falls_back_to_disconnect_alert` FAIL (status ends up
`ERROR` instead of the expected outcome, since recovery isn't wired in yet). The two
"no_recovery_attempt" tests currently PASS by accident (nothing calls `requests.post` yet at
all) — that's fine, they'll stay green through Step 3 and are there to catch a future regression.

- [ ] **Step 3: Wire recovery into `_run_checks()`**

Replace the first line of `_run_checks()` (`claudia/status.py:174`):

```python
    async def _run_checks(self) -> None:
        ibkr_ok = await asyncio.to_thread(self.check_ibkr)
        if not ibkr_ok and self._status["ibkr"] == ServiceStatus.OK:
            auth = self._last_ibkr_auth_status
            if auth.get("connected") and not auth.get("authenticated"):
                if await asyncio.to_thread(self._attempt_soft_recovery):
                    log.info("IBKR: soft-timeout recovered silently via ssodh/init")
                    ibkr_ok = await asyncio.to_thread(self.check_ibkr)
        gdrive_ok = await asyncio.to_thread(self.check_gdrive)
```

(This replaces the old first two lines — `ibkr_ok = ...` immediately followed by
`gdrive_ok = ...` — with the block above. The rest of `_run_checks()` from `tv_ok = ...` onward
is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_status.py -v`
Expected: all PASS — every new test in Tasks 1-3, and every pre-existing test in the file (no
regressions).

- [ ] **Step 5: Run the full non-integration suite**

Run: `pytest -m "not integration"`
Expected: PASS, same count as before this plan plus the ~10 new tests.

- [ ] **Step 6: Commit**

```bash
git add claudia/status.py tests/test_status.py
git commit -m "feat: silently recover IBKR soft-timeout via ssodh/init before alerting"
```

---

### Task 4: Update `docs/connectivity.md`

The "Soft-timeout recovery (not yet implemented)" note (added in the prior session, commit
`281a8d0`) is now stale.

**Files:**
- Modify: `docs/connectivity.md`

- [ ] **Step 1: Replace the note**

Find the paragraph starting `**Soft-timeout recovery (not yet implemented):**` and replace it
with:

```markdown
**Soft-timeout recovery (shipped YYYY-MM-DD):** when the inactivity timer lapses,
`/iserver/auth/status` returns `connected:true, authenticated:false` — a state distinct from a
hard disconnect. `ConnectivityChecker._run_checks()` detects this exact signature, but only on a
transition from a previously-confirmed `OK` state (never from `UNKNOWN`, which covers the
fragile first-seconds-after-login window) — and calls `POST /iserver/auth/ssodh/init`
(`publish:true, compete:false`) to silently re-establish the session. If recovery succeeds, the
disconnect is invisible to the user — no alert fires, since the service never visibly left `OK`.
If it fails, behavior falls back exactly to the pre-existing manual browser+2FA flow. `compete`
is hardcoded `false` and must never be changed — `true` would force-evict a concurrent IBKR
Mobile/TWS session. Implementation: `claudia/status.py` `_attempt_soft_recovery()` +
`_run_checks()`. `POST /iserver/reauthenticate` remains **Deprecated** and banned from proactive
use, unaffected by this change — it is a different endpoint.
```

(Fill in the actual date when this task is executed.)

- [ ] **Step 2: Commit**

```bash
git add docs/connectivity.md
git commit -m "docs: mark IBKR soft-timeout recovery as implemented"
```

---

### Task 5: Live-test protocol (human-in-the-loop — not automatable, do not delegate to a subagent)

**Why this is safe to run:** by the time this test is performed, the soft-timeout has *already*
happened naturally (idle >6 min) — the test only checks whether the *already-degraded* session
can be silently repaired. Worst case if `ssodh/init` doesn't work as documented is identical to
today's status quo: a normal browser + 2FA re-login, which would have been needed anyway. This
test creates no additional risk beyond what naturally occurs from letting a session idle.

**Do not run this until Tasks 1-4 are committed and the unit-test suite is green.**

- [ ] **Step 1: Get a live authenticated session**

Log into the gateway normally (`https://localhost:5055`, browser + 2FA) as part of any planned
live-testing session — do not log in solely to run this test; piggyback on a session that's
happening anyway.

- [ ] **Step 2: Temporarily stop all keepalive so the session can actually idle past ~6 minutes**

```bash
./scripts/install-ibkr-keepalive-daemon.sh --uninstall
```

If ClaudIA is running, stop it too (`Ctrl-C` in its terminal) — otherwise `ConnectivityChecker`'s
own 60s tickle will keep the session alive indefinitely and the soft-timeout will never occur.

- [ ] **Step 3: Wait ~7 minutes, then confirm the soft-timeout signature**

```bash
sleep 420
curl -sk "https://localhost:5055/v1/api/iserver/auth/status" | python3 -m json.tool
```

Expected: `"connected": true, "authenticated": false`. **If instead you see
`"connected": false`**, the session hard-disconnected rather than soft-timed-out — this specific
recovery path does not apply to that state; stop here, log the finding in
`docs/project-status.md`'s Live Test Log (Task 6) as "hard disconnect observed instead of soft
timeout, ssodh/init recovery not exercised", and do a normal browser re-login. This is not a
failure of the code — it's new information about actual gateway behavior under this exact idle
window.

- [ ] **Step 4: Test the recovery call directly**

```bash
curl -sk -X POST "https://localhost:5055/v1/api/iserver/auth/ssodh/init" \
  -H "Content-Type: application/json" \
  -d '{"publish": true, "compete": false}' | python3 -m json.tool
```

- [ ] **Step 5: Confirm recovery**

```bash
curl -sk "https://localhost:5055/v1/api/iserver/auth/status" | python3 -m json.tool
```

Expected: `"authenticated": true` again, with **no browser interaction and no 2FA prompt**. If
this is true, the documented recovery path works exactly as IBKR describes it.

- [ ] **Step 6: Re-install the keepalive daemon**

```bash
./scripts/install-ibkr-keepalive-daemon.sh
```

- [ ] **Step 7: Restart ClaudIA and confirm `ConnectivityChecker` behaves the same way end-to-end**

Repeat steps 2-5 with ClaudIA running instead of raw `curl`, watching
`~/Library/Logs` / ClaudIA's own logs for the `"IBKR: soft-timeout recovered silently via
ssodh/init"` log line from Task 3, and confirm **no** "⚠️ IBKR Gateway disconnected" chat message
appears.

---

### Task 6: Log the live-test result

**Files:**
- Modify: `docs/project-status.md` — add one row to the `## Live Test Log` table (5 columns:
  Date | Session report | Items tested | Issues found | Outcome — see the existing rows for the
  exact format, e.g. the `281a8d0` keepalive-daemon row added 2026-07-17) **and** one row to the
  `## Feature Timeline` table if this task lands as its own commit separate from Tasks 1-4.

Note: `docs/audits/live-test-log.md` is a **different** file with a different convention
(anchored `<a id="run-...">` entries) scoped to scripted/machine-executed tests against the
Anthropic API, Google Drive, and the TradingView sidecar specifically — it has never been used
for IBKR gateway live tests in this project's history. Do not use it here; every IBKR-related
live test to date (order staging, P&L, the keepalive daemon, etc.) has been logged in
`docs/project-status.md`'s `## Live Test Log` section instead.

- [ ] **Step 1: Add an entry** to `docs/project-status.md`'s `## Live Test Log` table, matching
its existing 5-column row format, recording: date, whether the soft-timeout signature actually
occurred as documented (Step 3 outcome), whether `ssodh/init` recovered without 2FA (Step 5
outcome), and whether the full `ConnectivityChecker` integration behaved correctly (Step 7
outcome). Verify the row parses as valid markdown before committing — count raw `|` characters
per row and compare against a neighboring row (`awk -F'|' 'NR==<line> {print NF}' docs/project-status.md`);
a 5-column row should show 7 fields including the leading/trailing empty ones.

- [ ] **Step 2: Commit**

```bash
git add docs/project-status.md
git commit -m "docs: log IBKR soft-timeout recovery live-test result"
```

---

## Self-Review Notes

- **Spec coverage:** narrow-condition recovery (Task 3), `compete:false` hardcoded + regression
  test (Task 2), no `ibkr_core_mcp` changes (confirmed — nothing in Tasks 1-4 touches that repo),
  docs updated with real implementation status (Task 4), safe live-test protocol that creates no
  new risk (Task 5), result logged per project convention (Task 6). No gaps against the plan's
  own Goal/Architecture.
- **Placeholder scan:** no TBD/TODO markers; every step has complete, runnable code.
- **Type consistency:** `_attempt_soft_recovery() -> bool` used identically in Task 2's tests and
  Task 3's `_run_checks()` call site. `_last_ibkr_auth_status: dict` matches the shape returned
  by `resp.json().get("iserver", {}).get("authStatus", {})` used in both `check_ibkr()` and the
  new tests throughout.
