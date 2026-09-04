# ClaudIA — The IBKR Gateway

**Scope:** the IBKR brokerage session — its states, who is allowed to touch it, how a login
is performed, and what to do when one will not take. Google Drive and TradingView
connectivity live in `docs/connectivity.md`; this file was split out of it on 2026-08-06.

## Why this is a document of its own

The other two services answer one question — *is it up?* The IBKR gateway is a state machine
with eight phases, a suspend protocol spanning four runtimes, a single brokerage session
shared with TWS and IBKR Mobile, and a login that can fail in ways no retry fixes. Those are
different kinds of fact and they drift at different rates: of the fourteen stale claims found
in `connectivity.md` on 2026-08-06, **every one was an IBKR claim.**

## What is here and what is not — read this before adding to either

Invariants live in **docstrings**, beside the code that enforces them. This document carries
what a human needs and code cannot state. The rule matters because two descriptions of one
rule, kept in step by nobody, is the exact defect class the 2026-08-06 review was convened to
remove — `gateway_preflight.verdict` and `gateway_session.classify` are now pinned to each
other by a test over all 512 readings rather than by a comment promising they agree.

| Question | Where the answer lives |
|---|---|
| What are the phases, and what does each mean? | `claudia/gateway_session.py` module docstring |
| Why does `LIVE` need a data call, not a flag? | ibid. |
| Why does recovery not call `POST /logout`? | `gateway_session._recover` docstring |
| What can `ssodh/init` actually do? | `gateway_session.attempt_soft_recovery` docstring |
| What can the pre-flight **not** see? | `claudia/gateway_preflight.py` module docstring |
| Who may write, and what enforces it? | `tests/test_gateway_ownership.py` |
| **Which actor renews the session, in which runtime** | here — §Actors |
| **How do I get logged in when it will not take** | here — §Runbook |
| **Why did my change to the container do nothing** | here — §The image trap |
| **What happened before, and what it cost** | here — §Incidents |

---

## Actors — who can touch the session, and from where

The single most useful table in this file. IBKR renews a session on **any** request, not just
`/tickle` (https://ibkrcampus.com/docs/web-api/v1/endpoints/session/ping-the-server.md), so
every row is a renewer whether or not that is its purpose. Verified 2026-08-06.

| Actor | Runtime | Interval | Silenced during a login by |
|---|---|---|---|
| `GatewaySession._poll_once` | ClaudIA process | 60s | its own phase — it skips while suspended |
| `DashboardPoller` | ClaudIA process | 15s | gates on `is_live()`; a login is not `LIVE` |
| `ExecutionListener` WebSocket | ClaudIA process | reconnect 5s→60s | gates on `is_live()` |
| Agent tools (~44) | ClaudIA process | on demand | `may_call_ibkr()` |
| `scripts/ibkr-keepalive.sh` | launchd, **outside every Python process** | 55s | **the file lock** — `~/.ibkr_core/session.suspend` |
| ~~`gateway/tickler.sh`~~ | ~~container~~ | ~~60s~~ | **removed 2026-08-06 — it could not be silenced at all** |

The lock is a PID-stamped file rather than an in-process flag because the launchd renewer
runs in a different runtime and a `curl` loop has to be able to test it in one line. It
**fails open**: a missing, malformed or dead-PID lock means "not suspended", because a lock
outliving its owner would silence every renewer forever and the symptom — sessions quietly
timing out — looks nothing like the cause.

⚠ **The lock reaches the launchd keepalive; it does not reach a second Python process.**
`GatewaySession` gates on its own in-memory phase, so a separate ClaudIA process polls
straight through a login driven by the CLI. Measured 2026-08-06: holding the lock for 82
seconds, the gateway still served 25 requests. In the normal flow the owner sets both the
phase and the lock together, so this is only reachable when two processes are running — but
it is real, and the fix during a manual login is to stop ClaudIA first.

---
## The IBKR light is about the **brokerage session**, not about account data

`/portfolio/*` and `/iserver/*` are not one switch, and the red light only speaks for the
second. Measured live 2026-08-04, all three at the same moment:

```
client.ping()                 -> False        # brokerage session
/portfolio/{id}/ledger        -> live         # netliq 59,118.00, unrealised -10,101.02
/iserver/account/orders       -> HTTP 400 {"error": "Bad Request: no bridge"}
```

"no bridge" is IBKR naming exactly what is missing. Account data is served from the SSO
session and keeps working without a brokerage session, which is why the live dashboard
went on updating while the chat announced *"IBKR gateway not connected"* — a contradiction
on one screen, and the reason `opening_status.account_readable` now asks the second
question separately. Three states, not two:

| `ping()` | account reads | what the user is told |
|---|---|---|
| up | up | nothing — no caveat to give, and every figure lives in the dashboard |
| down | **up** | account figures, plus what is unavailable and why (`BROKERAGE_SESSION_DOWN`) |
| down | down | `OFFLINE_STATUS` — and the dashboard blanks to match |

The dashboard follows the same rule: past `STALE_AFTER` the account figures are **not
drawn at all** rather than left on screen under a warning, because a number that is
minutes old looks exactly like one that is current. The Flex-derived realised windows
keep rendering throughout — they are read from local SQLite and never depended on the
gateway.

---

### How a reading is taken

`GatewaySession.read_now()` issues **two GETs** — `/tickle` (is *a* session authenticated)
and `/sso/validate` (**whose** it is) — via `gateway_preflight.read_state`, then, only when
the brokerage session claims authenticated, one `GET /portfolio/accounts` to confirm the
data half is actually serving. That third call is what separates `LIVE` from `DEGRADED`: an
authenticated flag alone never turns the dot green. It is also IBKR's documented
prerequisite for the portfolio endpoints, so one call discharges both obligations.

`ConnectivityChecker.check_ibkr()` is a cached lookup of that result and issues no HTTP,
which is why the IBKR light (the action bar's button since 2026-09-03) and the dashboard KPI tiles can no longer disagree about
whether IBKR is up. The gateway answers HTTP 200 regardless of auth state, so the body is
parsed: both `iserver.authStatus.authenticated` and `.connected` must be true.

*Why `LIVE` requires a data call rather than a flag, and what each phase means:
`claudia/gateway_session.py` module docstring. Not repeated here.*

### Session lifecycle (verified against official docs, 2026-07-17)

Source: [IBKR Client Portal API — session lifecycle FAQ](https://ibkrcampus.com/docs/web-api/v1/endpoints/session/ping-the-server.md)
(scraped via Firecrawl — `interactivebrokers.com` 403s a direct `WebFetch`).

Two independent, non-overlapping timeout mechanisms:

| Mechanism | Threshold | Prevented by |
|---|---|---|
| Inactivity timeout | ~5–6 min without **any** request — not just `/tickle` | `GatewaySession`'s 60s poll while ClaudIA runs, the launchd keepalive at 55s when it does not, and in practice every other request too (the dashboard poller alone, at 15s, is sufficient) |
| **Absolute session cap** | **24h, resets at midnight NY/Zug/HK** (whichever region the gateway connects to) | **Nothing — unavoidable.** A fresh browser + 2FA login is required at least once every 24h no matter how well the inactivity timer is serviced. Accepted as a known, permanent constraint — not a bug to chase. |

Daily IBKR server maintenance can also force a disconnect earlier than the 24h mark; IBKR's own
guidance is to restart the gateway after the maintenance window rather than expect continuity
through it.

**Soft-timeout recovery.** When the inactivity timer lapses, the gateway reports
`connected: true, authenticated: false` — distinct from a hard disconnect.
`GatewaySession._poll_once()` recognises that exact signature and only from a
previously-`LIVE` session, then calls `attempt_soft_recovery()`
(`POST /iserver/auth/ssodh/init`). Success is invisible to the user; failure falls back to
the ordinary browser + 2FA flow with one disconnect alert.

- ⚠ **`compete` is hardcoded `false` and must never be changed.** `true` force-evicts a
  concurrent IBKR Mobile or TWS session.
- ⚠ **`POST /iserver/reauthenticate` is deprecated by IBKR and banned from proactive use** —
  it disrupts fresh logins. Different endpoint; unaffected by the above.
- **Status: not live-verified.** Needs a session deliberately idled past ~6 minutes.
  7 tests cover it, all in `tests/test_gateway_session.py`.

*What `ssodh/init` can and cannot do — measured, with the evidence:
`gateway_session.attempt_soft_recovery` docstring. Not repeated here.*

**Competing sessions:** IBKR's own gateway walkthrough states you *"cannot be logged into the
account you are authenticating with anywhere else before you authenticate"* and that merely
closing another IBKR window/app (instead of using its "Log Out") *"may cause a stale login
session"* — confirming `check_ibkr()`'s `authStatus.competing` warning
(now `gateway_session.classify` → `SessionPhase.CONTESTED`) reflects a real,
IBKR-documented failure mode: opening IBKR
Mobile/TWS/another browser tab during a live ClaudIA session can force-kick the gateway session.
Source: [Launching and Authenticating the Gateway](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/).

### A borrowed session — the login that could not succeed (diagnosed 2026-08-05)

For days, the gateway login failed with a correctly-formatted 8-digit IB Key response and
the right username, while IBKR Mobile logged in on demand. The cause was not 2FA at all.

**`/tickle` alone could not see it, and every field it does expose pointed the wrong way:**

| Signal | Reading | What it suggested | Why it was wrong |
|---|---|---|---|
| `userId` | populated | a good session | SSO was valid — but not *ours* |
| `ssoExpires` | renewing | session alive | kept alive by our own ticklers |
| `competing` | **false** | uncontested | the gateway never got far enough to register a claim; the flag describes a fight, and there was no fight |
| `authStatus` | `authenticated:false, connected:false` | just log in again | the retry was doomed before it started |

**`GET /sso/validate` answered it in one field:**

```text
CLIENT_APP : IBKRMOBILE_000.a-000      ← the phone's session, held by the gateway
USER_NAME  : ibkruser
RESULT     : True
```

Only one brokerage session exists per username across Client Portal, TWS and IBKR Mobile
([multiple sessions](https://ibkrcampus.com/docs/web-api/authentication/multiple-sessions.md)).
The gateway was holding a session **issued to the phone**, which it cannot authenticate as
— so the login page rejected a correct code however often it was retried.

Two follow-ons, both measured rather than reasoned about:

- **`POST /iserver/auth/ssodh/init` cannot rescue this.** Twice, it moved `connected`
  False→True and left `authenticated` False. It raises the bridge; it cannot supply an
  authentication that never happened.
- **`POST /logout` returned `{"status": true}` and the session came straight back**, with a
  full 10-minute window. Three independent ticklers renew it every ~60s — the container's
  own `tickler.sh`, the host launchd keepalive, and `ConnectivityChecker` (the last of
  which no longer tickles — the 60s read is now `GatewaySession`'s). **The keepalive
  built to protect a good session cannot tell a good session from a borrowed one**, and had
  been preserving an unusable one all day.

**What resolved it: `docker restart`.** The session is held in the gateway's local process
memory, not re-served from IBKR, so a restart drops it with nothing to race. Login then
succeeded first time, and `/sso/validate` came back `CLIENT_APP: None` — the session
belonged to the gateway itself. Four theories died before this one: a stale gateway build
(SHA-256 identical to a fresh download), a wrong `ip2loc` (IBKR's own shipped default), an
IB Key registered to another username (same username, user-confirmed), and 2FA itself.

### Runbook: a login that will not take

```bash
python -m claudia.gateway_preflight     # ALWAYS first — read-only, two GETs, no writes
```

| Verdict | Exit | What it means | Do this |
|---|---|---|---|
| `[OK] Session is LIVE` | 0 | already authenticated | **Nothing.** A needless re-login is what escalates into the IB Key challenge |
| `[DOWN] Gateway is NOT answering` | 1 | no usable HTTP response | start the container |
| `[FREE] …` | 2 | nothing holds the slot | log in now, through to *"Client login succeeds"* |
| `[BUSY] Another IBKR client holds the session` | 3 | `competing`/`collision` set | fully close the other client, re-check |
| `[BORROWED] SSO session belongs to X` | 4 | the gateway holds another app's session | log out of X **from its Log Out menu item**, then `./scripts/gateway-reset.sh` |

Closing or swiping an app away is **not** logging out — IBKR's own gateway walkthrough
warns it *"may cause a stale login session"*, which is precisely the state above.

`./scripts/gateway-reset.sh` restarts the container and re-checks. It **refuses to run
against a healthy session** (`--force` overrides) — a tool built to fix a login must not be
able to break one.

**HTTP 401 is not a failure.** It is the gateway answering that it holds no session — a
freshly started or freshly logged-out gateway, and the best possible moment to log in.
`read_state` treats it as `reachable=True`; folding it into `reachable=False` once produced
*"Gateway is NOT answering. Start it first"* about a gateway that was running perfectly and
waiting (fixed 2026-08-05, pinned by `test_a_401_means_alive_and_ready_not_down`, with a
sibling test proving the carve-out still reports a genuine outage as DOWN).

### Where the check runs by itself

- **Startup** — `warn_if_session_borrowed()` in `panel_app.main()`, beside
  `install_check.warn_if_stale()` and `agent.warn_if_model_lacks_operator_channel()`. It
  fires only on positive proof: valid SSO **and** a named `CLIENT_APP` **and** an
  unauthenticated gateway. Never on absence — a warning that fires when it need not is one
  that gets ignored when it must not.
- **The IBKR button** (in the action bar under the chat since 2026-09-03; "Start IBKR Gateway" in the welcome message before) — hands a `GatewayManager` to
  `GatewaySession.establish()`, which reads the session *before* anything touches the
  container and opens a login page only from `FREE`. It used to inline its own sequence and
  call `GatewayManager.start()` — which removes any existing container — **before** the
  pre-flight, so the two verdicts the pre-flight existed for were computed against a session
  that had already been destroyed. Fixed 2026-08-06; `tests/test_gateway_ownership.py`
  now fails if any module outside the owner drives a manager again.

### ibkr_core_mcp ping

`IBKRClient.ping()` (used by tools, not the UI) uses a different endpoint:
`GET /iserver/auth/status`. It checks only `authenticated` (not `connected`) and
has a one-retry logic for the first-request IBKR quirk — the gateway returns
`authenticated: false` on the very first request of a new session even when fully
logged in. It calls `tickle()` and retries once after a 1-second pause.

The two pings serve different purposes:
- `ConnectivityChecker.check_ibkr()` — UI light; a cached read of `GatewaySession`, no HTTP
  and therefore **no keepalive side effect** since 2026-08-06
- `IBKRClient.ping()` — pre-tool guard, runs on demand, handles startup quirk

### Reconnection process

**Automatic (session timeout):**
1. Status light turns red; in-chat alert: *"⚠️ IBKR Gateway disconnected"*
2. Open `https://localhost:5055` in your browser
3. Complete IBKR login + 2FA
4. `GatewaySession` polls within 60s → reads `authenticated: true` and confirms
   `/portfolio/accounts` → phase `LIVE` → light turns green → in-chat alert:
   *"✅ IBKR Gateway reconnected"*

**Gateway container stopped:**
1. Status light turns red (connection refused, not HTTP 200)
2. Click the red **IBKR** button in the action bar under the chat
   — or run `./start-claudia.sh` in a new terminal
3. Container starts; gateway Java process comes up (~30s)
4. Browser opens `https://localhost:5055` automatically
5. Complete login + 2FA; `GatewaySession` detects the reconnect and the dot follows it

**Docker Desktop not running:**
Same as above but step 2 also launches Docker Desktop automatically (macOS only).

### Always-on keepalive daemon (shipped 2026-07-17)

`GatewaySession`'s 60s poll and `start-claudia.sh`'s `caffeinate` only protect the
session while ClaudIA's own process is running — the gap between stopping ClaudIA (e.g. a dev
restart) and starting it again was previously unprotected unless someone remembered to run
`scripts/ibkr-keepalive.sh` manually in a separate terminal.

`scripts/install-ibkr-keepalive-daemon.sh` installs `scripts/ibkr-keepalive.sh` as a macOS
LaunchAgent (`~/Library/LaunchAgents/com.claudia-ui.ibkr-keepalive.plist`, `RunAtLoad` +
`KeepAlive`), so the gateway is tickled every 55s and the Mac is kept awake **independent of
ClaudIA, terminals, or dev restarts** — install once, it survives logouts/crashes/reboots.
It only holds the `caffeinate -i` sleep-prevention assertion while the gateway actually responds
to `/tickle`, and releases it the moment the container goes unreachable, so it doesn't keep the
Mac permanently awake when nothing needs protecting.

```bash
./scripts/install-ibkr-keepalive-daemon.sh              # install + load
./scripts/install-ibkr-keepalive-daemon.sh --uninstall   # unload + remove
```

Logs: `~/Library/Logs/claudia-ui/ibkr-keepalive.log` (+ `.err.log`). Only logs on OK/WARN state
transitions, not every tick, to keep the log bounded over a long-running install.

Redundant with `GatewaySession`'s own 60s read when ClaudIA is running (both are idempotent
`GET /tickle` calls, well inside IBKR's `1 req/sec` pacing limit for that endpoint) — that's
intentional defense in depth, not a conflict.

**It is also the only renewer the owner can silence.** During a login or a recovery,
`SuspendLock` (`~/.ibkr_core/session.suspend`, PID-stamped, fails open) is what stops it;
`tests/test_gateway_ownership.py` fails if that check is ever removed from the script.

---


---

## The image trap — why your change to the container did nothing

**A container restart does not pick up a changed `run_gateway.sh`, `conf.yaml` or
Dockerfile.** `GatewayManager.start()` builds only when the image is *absent*, so a
restart re-runs the old image forever.

```bash
docker rmi ibkr-core-gateway     # then start normally; the next start rebuilds
```

⚠ **This costs a fresh 2FA login.** Plan it; do not discover it.

This is not hypothetical. `tickler.sh` was removed from the image on 2026-08-06 and the
container went on running it for the rest of the day — the one renewer that no suspend flag
could reach was still live while every test was green. Compounding it, `build_image()`
**could not have worked either**: its build context was `Path(__file__).parent`, which under
this project's mandated strict editable install is a symlink farm, and Docker will not
follow a symlink out of a build context. Both fixed 2026-08-06; the build context is gated
by `tests/test_install_check.py::test_the_gateway_build_context_is_readable_by_docker`.

---

## When the login itself is rejected — IB Key

The pre-flight can tell you the slot is free. **It cannot tell you the login will succeed**,
and no gateway-side engineering can: whether another IBKR app holds the SLS session is not
exposed by any endpoint. Work the list in order.

1. **Is the slot actually free?** `python -m claudia.gateway_preflight`. If it names another
   `CLIENT_APP`, stop — that is the borrowed session above, and no retry fixes it.
2. **Log out of IBKR Mobile from its Log Out menu item.** Closing or swiping leaves the
   server-side session alive.
3. **Close every `localhost:5055` tab.** A reloaded login page issues a **new** challenge; a
   response computed from an older one is permanently invalid.
4. **Stop ClaudIA** if you are logging in from the CLI — its poller is a separate process and
   does not read the suspend lock (§Actors).
5. **Have the phone already on the IB Key two-factor screen before submitting the username**,
   and enter the response within seconds.

Step 5 is not fussiness. Measured 2026-08-06 on an empty gateway, same container, same
credentials: an attempt that sat **2m 13s** on the challenge was rejected; the retry,
answered promptly, reached `LIVE` first time. A rejected response code with a free slot and
a logged-out phone is most often an expired challenge.

If a promptly-entered code is still rejected, the IB Key seed is desynced — that is
reactivation with IBKR, not something to retry:
[Reactivating IBKR Mobile - IB Key](https://ibkrguides.com/securelogin/sls/reactivating-ibkr-mobile-authentication.htm)
· [Challenge-Response mode](https://ibkrguides.com/securelogin/sls/notification-not-received.htm)

**Do not keep retrying.** Repeated logins are what escalate IB Key pressure in the first place.

---

## Incidents — what happened, and what it cost

| Date | Symptom | Root cause | Now guarded by |
|---|---|---|---|
| 2026-08-05 | Correct 2FA code rejected for days | Gateway held **IBKR Mobile's** SSO session; visible only in `/sso/validate` `CLIENT_APP` | `warn_if_session_borrowed()` at startup; `SessionPhase.BORROWED` |
| 2026-08-05 | *"Gateway is NOT answering"* about a healthy gateway | HTTP 401 folded into `reachable=False` | `test_a_401_means_alive_and_ready_not_down` |
| 2026-08-06 | Working sessions destroyed on launch | `GatewayManager.start()` ran **before** the pre-flight | `test_only_the_owner_mutates_the_container` (AST-based) |
| 2026-08-06 | Pollers hammered a gateway mid-login | Launcher did not block | `test_the_launcher_blocks_until_the_session_is_resolved` |
| 2026-08-06 | `POST /logout` could not clear a session | Three renewers, one un-silenceable | Container tickler removed; `SuspendLock` |
| 2026-08-06 | Container fix had no effect for a day | Image never rebuilt, **and could not be** | §The image trap |

Full diagnosis of the 2026-08-05 borrowed session and the six 2026-08-06 gaps:
`docs/plans/2026-08-06-gateway-session-lifecycle-owner.md` (local, git-ignored).

---

## Implementation reference

```
claudia/gateway_session.py   — the session owner. Every read, the phase, the suspend lock,
                               and every session-affecting write. Start here.
claudia/gateway_preflight.py — the reader: two GETs -> GatewayState, plus verdicts and the CLI
claudia/gateway_launch.py    — CLI: `python -m claudia.gateway_launch [--diagnose]`
claudia/status.py            — ConnectivityChecker; check_ibkr() is a cached lookup, not a probe
scripts/ibkr-keepalive.sh    — launchd renewer; honours the suspend lock
scripts/gateway-reset.sh     — standalone recovery; refuses to run against a healthy session
ibkr_core_mcp/gateway/       — GatewayManager (Docker only) + the image that runs the gateway
tests/test_gateway_ownership.py — the repo-level guards that keep all of the above true
```

**The boundary, in one sentence:** `gateway_session` owns the session and every decision
about it; `gateway_preflight` reads; `gateway_launch` presents; `ibkr_core_mcp` owns the
container and the transport, never a session opinion.

Related: `docs/connectivity.md` (Google Drive, TradingView, the status lights) ·
`docs/startup-flow.md` (where the gateway sits in the launch sequence) ·
`docs/api-reference.md` (IBKR source-of-truth URLs).
