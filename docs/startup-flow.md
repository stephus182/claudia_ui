# ClaudIA Startup Flow

Documents every phase of startup in order: `start-claudia.sh` (pre-UI) then session init.
Use this to diagnose startup failures: each phase is labeled with where to look.

> **Post-cutover mapping (2026-07-24, corrected 2026-08-05):** the UI moved from Chainlit to
> Panel. What this doc calls `on_chat_start` in the removed Chainlit app.py is now `_init_session` inside
> `claudia/panel_app.py` (panel_app was built as a faithful port, so the phase sequence below
> is preserved).
>
> This note used to promise that "the equivalent Panel code carries `# app.py:NNN` parity
> comments". **It does not, and the 2026-08-05 doc audit checked** — `panel_app.py` carries
> ten parity comments, all reading "parity with the removed app.py" with **no line numbers**,
> so every `app.py:NNN` reference this doc used to carry pointed at nothing a reader could
> match. Every one has now been **re-anchored to `claudia/panel_app.py` by function name**,
> with the old app.py location kept as parenthetical history.
>
> **By function, not by line, deliberately:** a line range is a claim that rots on the next
> refactor with nothing to detect it — which is how this doc broke in the first place. A
> function name breaks loudly (`tests/test_docs_claims.py` fails on a path that stops
> existing) and survives edits above it.

---

## Phase -1 — IBKR gateway pre-flight (`start-claudia.sh`)

**File:** `start-claudia.sh` → `ibkr_core_mcp/gateway/manager.py` → `GatewayManager.startup()`

Runs before the Panel app starts. Two paths:

**Fast path — container already running and authenticated:**
```
▶ Ensuring Docker is running...
  ✔ IBKR gateway already running and authenticated — skipping startup.
```
The existing IBKR session is preserved. This is the normal path when restarting
ClaudIA without touching IB. No container restart, no login prompt.

**Full path — first start or session lost:**
```
▶ Ensuring Docker is running...
▶ Starting IBKR gateway container...
▶ Waiting for gateway to be reachable...
▶ Opening IBKR login page in browser...
  [user completes login + 2FA]
▶ Verifying IBKR session...
  ✔ IBKR session active and ready.
```
The existing container (if any) is removed and a fresh one is started.
Login is required.

**Decision logic** (`GatewayManager.startup()`):
1. Ensure Docker Desktop is running
2. Check `is_running() AND is_authenticated()` — if both true → fast path, return
3. Otherwise → full path: remove container, start fresh, prompt for login

**Why remove-and-recreate on the full path:** the IBKR gateway container holds session state. Reusing a stale container after a timeout produces unpredictable auth errors. A fresh container always starts clean.

**caffeinate:** macOS sleep prevention is started before the gateway check. `caffeinate -i -w $$` runs for the lifetime of the script, preventing idle sleep from disconnecting IBKR mid-session.

---

## Phase 0 — GDrive DB download

**File:** `claudia/gdrive_sync.py` → `GDriveSync.download_db()`

On the very first session of the process (not on reconnects), ClaudIA downloads
`claudia.db` from Google Drive before opening the local DB. This ensures
conversation history is current if another machine uploaded a newer copy.

- Controlled by `GOOGLE_DRIVE_FOLDER_ID` in `.env`
- If Drive is unreachable, the existing local `claudia.db` is used (non-fatal)
- Only runs once per process — subsequent sessions skip this

---

## Phase 1 — Context / Principles loading

**File:** `claudia/context_loader.py`

Loads `docs/context.md` and `docs/principles.md` (or their Drive equivalents).
These form the core of ClaudIA's system prompt.

- Drive texts are fetched once per session start (may override local files)
- A `watchdog` file observer is started to detect live edits mid-session
- If a file is missing, `ContextLoader._read_required()` raises `FileNotFoundError` and
  `on_chat_start` aborts the session with a "Setup required" chat message — there is no
  template or empty-string fallback (`claudia/context_loader.py:142-149`, and `claudia/panel_app.py`'s
  `FileNotFoundError` handler in `_build_chat_app`)

---

## Phase 2 — Document versioning

**File:** `claudia/conversation_store.py` → `register_doc_version_if_new()`

SHA-256 hash of `context.md` + `principles.md` is computed. If the hash is new,
a new version label (`v1`, `v2`, …) is registered and a snapshot is written to
`docs/versions/{label}/`.

- If the hash changed since last session → security warning shown in chat
- The active version label is injected into ClaudIA's system prompt each turn

---

## Phase 3 — TradingView sidecar

**File:** `claudia/tradingview.py` → `_get_tv_bridge()`

ClaudIA attempts to connect to the `tradingview-mcp` Node.js sidecar via stdio MCP.

Binary discovery order:
1. `TRADINGVIEW_MCP_PATH` env var
2. `tradingview-mcp` on PATH
3. `~/.tradingview-mcp/src/server.js`
4. `~/.tradingview-mcp/build/index.js`
5. `vendor/tradingview-mcp/src/server.js`
6. `vendor/tradingview-mcp/index.js`

- If TradingView Desktop is not running (CDP port 9222 unreachable): sidecar starts
  but TV tools return errors. Status: UNKNOWN (neutral TradingView button).
- If sidecar binary is missing entirely: TV tools unavailable. The TradingView button in
  the action bar under the chat launches and connects it (since 2026-09-03; before that a
  "Launch TradingView" button in the welcome message).
- TV offline is non-fatal — screenshot mode (Claude vision) is always available.

---

## Phase 4 — Connectivity monitor

**File:** `claudia/status.py` → `ConnectivityChecker`

The connectivity checker is a **process-level singleton** — created once and reused
across Panel sessions. It polls every 60 seconds (`POLL_INTERVAL` in `claudia/status.py`).

| Service | Check method | Condition for OK |
|---|---|---|
| IBKR | GET `/tickle` | `authenticated=true AND connected=true` in `iserver.authStatus` |
| GDrive | `GDriveSync.ping()` or token file exists | Live API round-trip succeeds |
| TradingView | TCP connect to port 9222 | Connection accepted within 1s |

**State transitions that send a chat alert:**
- Any service: UNKNOWN/OK → ERROR = disconnected message
- Any service: ERROR → OK = reconnected message
- UNKNOWN → OK at startup = silent (expected)

**IBKR: competing session detection**
If `authStatus.competing=true` appears in the `/tickle` response, a warning is
logged. This means another TWS or gateway session is active and may be holding
the authentication token. Symptom: auth completes on mobile but the session
immediately drops.

**Side effect of `/tickle`:** resets the IBKR session keepalive timer. Polling
every 60s prevents IBKR auto-logout (session times out after ~5-6 minutes
without a tickle call, per IBKR's official FAQ — see
[`docs/connectivity.md` § Session lifecycle](connectivity.md#session-lifecycle-verified-against-official-docs-2026-07-17)
for the full breakdown including the unavoidable 24h/midnight absolute session
cap and the `ssodh/init` soft-recovery path, implemented 2026-07-17, not yet
live-verified).

---

## Phase 4.5 — Execution listener

**File:** `claudia/execution_listener.py` → `ExecutionListener`

Like `ConnectivityChecker`, this is a **process-level singleton** — constructed and started
right after the connectivity checker (`claudia/panel_app.py`, `_init_session` step 8), before the
IBKR ping check in Phase 5.

- Subscribes to IBKR's execution WebSocket feed (any order origin, not just ClaudIA's own)
- On each trade execution, triggers a one-shot P&L snapshot check. This used to drive an
  "Account P&L" line in the welcome message; that line was retired on 2026-08-05 (the
  dashboard's KPI strip polls it live). The snapshot itself still runs and is still what
  `get_live_pnl` reads
- Connection failures retry on a backoff schedule (5s, 10s, 30s, 60s) rather than failing the
  session — a listener outage only affects the auto-triggered P&L snapshot after a fill, not
  order placement/modify/cancel (`get_live_pnl` still works by reading the last stored snapshot)

---

## Phase 5 — IBKR gateway check

**File:** `claudia/panel_app.py` → `_send_opening_status()` decides `ibkr_offline`; the
reconnect itself lives in `_build_action_bar()` (until 2026-09-03 `_send_action_buttons()`,
was app.py ~463–480, pre-cutover)

`toolkit.client.ping()` checks `iserver/auth/status`:
- Returns `True` only when `authenticated=true`
- Retries once with a tickle to handle IBKR's first-call quirk (gateway returns
  `authenticated=false` on the very first request of a new session even when fully
  logged in)
- Returns `False` on any network error, 401, or non-authenticated state

**If ping returns False:** a second, independent question is asked —
`opening_status.account_readable`, which probes `/portfolio/*` via `get_accounts`. IBKR's two
halves fail separately, and inferring one from the other put a "gateway not connected" line
beside a dashboard drawing live balances (2026-08-04, `docs/connectivity.md`).

- `ibkr_offline = True` either way — it means *the brokerage session* is down, which is what
  the flag's consumers act on: it defers the Flex sync and words the opening line (the IBKR
  button in the action bar exists regardless — its colour carries the state)
- account endpoints answering → `BROKERAGE_SESSION_DOWN`: the dashboard keeps drawing, and the
  chat names the half that is missing
- neither answering → `OFFLINE_STATUS`, and the dashboard blanks to match

**If ping returns True:** chat says nothing about the account at all.

- Flex sync staleness check runs
- Market calendar context is injected into system prompt

**No account figure is fetched here (2026-08-05).** Account summary, live orders and positions
were fetched in parallel and rendered into the welcome message until the live dashboard took
over: it polls all of them every 15s, so a startup copy was a second, immediately-stale set of
the same numbers. On the healthy path the whole phase is now one `ping()`. See
`claudia/opening_status.py`'s module docstring.

---

## Phase 6 — Flex trade sync

**File:** `claudia/panel_app.py` → `_maybe_background_flex_sync()` (was app.py's `_background_flex_sync`)

Runs as a background asyncio task (non-blocking) after the welcome message is sent.

Sync is **skipped** when any of:
1. `store.db` is fresh — newest trade date == last NYSE trading day (calendar-aware)
2. Last sync attempt was < 4 hours ago (prevents IBKR API lockout on rapid retries)
3. `IBKR_FLEX_TOKEN` or `IBKR_FLEX_QUERY_ID` not configured

On sync success: `store.db` is backed up to Drive `account_data/`.

---

## Phase 7 — Market calendar

**File:** `ibkr_core_mcp/store.py` → `get_market_calendar_context()`

Injects trading-day awareness into ClaudIA's system prompt:
- Today's date, whether it's a NYSE trading day
- Last and next NYSE trading days
- Holiday lists for 20 exchanges (current + next year)
- Futures vs securities schedule distinction (CME vs NYSE hours)
- CME product group schedule (grains close at 1:20 PM CT, not 4 PM)

**Performance:** ~3.4s cold (numpy array load for 20 exchanges), 0.01ms warm
(process-level date-keyed cache). Cache auto-invalidates at midnight.

---

## Phase 8 — Welcome message

**File:** `claudia/panel_app.py` → `_send_opening_status()` (was app.py ~573–620, pre-cutover)

The welcome message includes:
- Account summary (positions, unrealized P&L, cash balance) — if IBKR online
- Live orders summary — if IBKR online
- Flex trade coverage info (date range, integrity status)
- Market calendar block (today, trading day status)
- (Until 2026-09-03 the welcome message also carried the action buttons. They now live in
  the **action bar** under the chat, always present: IBKR, TradingView, Drive — colour =
  state, click = reconnect — and End Session. Session-level lines such as connectivity
  alerts and the Flex sync result go to the **System log** card, not the chat.
  `docs/panel/ui-customisation-reference.md` §2.6.)

---

## Startup failure diagnosis

| Symptom | Where to look |
|---|---|
| Login prompt appears on every ClaudIA restart | Phase -1: gateway pre-flight not running (`is_running()` or `is_authenticated()` returns false) — check container with `docker ps` |
| Container restarted unexpectedly, session lost | Phase -1: only happens on full path (session was gone). If it was authenticated, check for competing sessions |
| DB not found / empty history | Phase 0: GDrive download failed; check `GOOGLE_DRIVE_FOLDER_ID` and token file |
| `context.md` not loading | Phase 1: file path, permissions (`chmod 600`), or Drive not configured |
| Version warning at startup | Phase 2: file changed since last session — intentional, verify content |
| TradingView unavailable | Phase 3: sidecar binary path, Node.js version, `vendor/` fallback |
| Action-bar buttons all red | Phase 4: network issue, gateway container stopped, Drive unreachable |
| IBKR button red | Phase 5: gateway not running or session expired — click it to reconnect (pre-flight first, login only if needed) |
| "IBKR Gateway disconnected" after login | Phase 4+5: competing session or session not fully synced — restart gateway |
| No Flex data / stale trades | Phase 6: Flex token/query ID not set, or rate limit hit |
| Market calendar missing from system prompt | Phase 7: exchange-calendars library issue |

---

## IBKR reconnection flows

### Restarting ClaudIA (IB stays connected)

```
./start-claudia.sh
  → Phase -1: is_running=true, is_authenticated=true → fast path
  → Panel app starts
  → Phase 5: ping() returns True → account summary fetched, no button shown
```

No container restart. No login. Session uninterrupted.

### First start or session lost

```
./start-claudia.sh
  → Phase -1: container missing or not authenticated → full path
  → Docker launched, fresh container started
  → Login page opened
  → User completes IBKR login + mobile 2FA
  → Phase 5 in the Panel app: ping() returns True
  → ConnectivityChecker: "IBKR Gateway reconnected" alert
```

### Session lost while ClaudIA is running (in-chat recovery)

1. ConnectivityChecker detects the session is no longer live → "IBKR Gateway disconnected"
   line in the System log (a warning toast too), IBKR button turns red
2. User clicks the **IBKR** button → `_build_action_bar()`'s reconnect hands a
   `GatewayManager` to `GatewaySession.establish()`, the one shared session owner: it
   **pre-flights first** (read-only), leaves a working session alone, starts the container
   only if it is not running, and opens the login page only from `FREE`. Progress lines
   stream into the System log. (Until 2026-08-06 this path called `GatewayManager.start()`
   unconditionally, recreating the container before any pre-flight — see
   `docs/ibkr-gateway.md`.)
3. ConnectivityChecker detects recovery → "IBKR Gateway reconnected" line, button green

**Common issues:**
- **Login prompt on every restart**: Check `docker ps` — if the container is not running between restarts, the session is being lost before ClaudIA starts. Likely cause: Mac sleep (caffeinate should prevent this) or Docker Desktop stopping.
- **Competing session**: Another TWS/mobile session holds the token. Log out from all other IBKR sessions, then re-authenticate via the gateway URL.
- **Gateway starts but session drops immediately**: Competing session or IBKR 2FA timing issue. Click the IBKR button again — the pre-flight names a borrowed session rather than retrying it.
- **Container present but not authenticated**: Session timed out. Full path runs — remove/recreate/login.
