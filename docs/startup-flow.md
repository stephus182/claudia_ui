# ClaudIA Startup Flow

Documents every phase of startup in order: `start-claudia.sh` (pre-Chainlit) then
`on_chat_start` (in `claudia/app.py`).
Use this to diagnose startup failures: each phase is labeled with where to look.

---

## Phase -1 — IBKR gateway pre-flight (`start-claudia.sh`)

**File:** `start-claudia.sh` → `ibkr_core_mcp/gateway/manager.py` → `GatewayManager.startup()`

Runs before Chainlit starts. Two paths:

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
  template or empty-string fallback (`claudia/context_loader.py:142-149`,
  `claudia/app.py:440-448`)

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
  but TV tools return errors. Status: UNKNOWN (gray dot).
- If sidecar binary is missing entirely: TV tools unavailable. "Launch TradingView"
  button shown in welcome message.
- TV offline is non-fatal — screenshot mode (Claude vision) is always available.

---

## Phase 4 — Connectivity monitor

**File:** `claudia/status.py` → `ConnectivityChecker`

The connectivity checker is a **process-level singleton** — created once and reused
across Chainlit sessions. It polls every 60 seconds (`POLL_INTERVAL` in `claudia/status.py`).

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
right after the connectivity checker (`claudia/app.py`, `on_chat_start` step 8), before the
IBKR ping check in Phase 5.

- Subscribes to IBKR's execution WebSocket feed (any order origin, not just ClaudIA's own)
- On each trade execution, triggers a one-shot P&L snapshot check, which drives the
  "Account P&L" line shown in the welcome message
- Connection failures retry on a backoff schedule (5s, 10s, 30s, 60s) rather than failing the
  session — a listener outage only affects the auto-triggered P&L snapshot after a fill, not
  order placement/modify/cancel (`get_live_pnl` still works by reading the last stored snapshot)

---

## Phase 5 — IBKR gateway check

**File:** `claudia/app.py` lines ~463–480

`toolkit.client.ping()` checks `iserver/auth/status`:
- Returns `True` only when `authenticated=true`
- Retries once with a tickle to handle IBKR's first-call quirk (gateway returns
  `authenticated=false` on the very first request of a new session even when fully
  logged in)
- Returns `False` on any network error, 401, or non-authenticated state

**If ping returns False:**
- `ibkr_offline = True`
- Account summary, live orders, and positions are skipped (would fail anyway)
- "Start IBKR Gateway" action button is added to the welcome message

**If ping returns True:**
- Account summary, live orders, and positions are fetched in parallel
- Flex sync staleness check runs
- Market calendar context is injected into system prompt

---

## Phase 6 — Flex trade sync

**File:** `claudia/app.py` → `_background_flex_sync()`

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

**File:** `claudia/app.py` lines ~573–620

The welcome message includes:
- Account summary (positions, unrealized P&L, cash balance) — if IBKR online
- Live orders summary — if IBKR online
- Flex trade coverage info (date range, integrity status)
- Market calendar block (today, trading day status)
- Action buttons (one or more of):
  - "Start IBKR Gateway" — only if IBKR offline
  - "Launch TradingView" — only if TV sidecar not available
  - "End Session" — always present

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
| Status dots all red | Phase 4: network issue, gateway container stopped, Drive unreachable |
| "Start IBKR Gateway" button appears | Phase 5: gateway not running or session expired — click to start |
| "IBKR Gateway disconnected" after login | Phase 4+5: competing session or session not fully synced — restart gateway |
| No Flex data / stale trades | Phase 6: Flex token/query ID not set, or rate limit hit |
| Market calendar missing from system prompt | Phase 7: exchange-calendars library issue |

---

## IBKR reconnection flows

### Restarting ClaudIA (IB stays connected)

```
./start-claudia.sh
  → Phase -1: is_running=true, is_authenticated=true → fast path
  → Chainlit starts
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
  → Phase 5 in Chainlit: ping() returns True
  → ConnectivityChecker: "IBKR Gateway reconnected" alert
```

### Session lost while ClaudIA is running (in-chat recovery)

1. ConnectivityChecker detects `authenticated=false` → "IBKR Gateway disconnected" alert in chat
2. "Start IBKR Gateway" button appears (or was already in the welcome message)
3. User clicks → the `start_gateway` action callback (`claudia/app.py`'s `on_start_gateway`) runs
   directly — **not** `GatewayManager.startup()`, and there is no "skip if already connected"
   check on this path. It calls `ensure_docker_running()` then `start()`, which
   **unconditionally** removes and recreates the gateway container every time, then
   `wait_for_gateway()` and `open_login_page()`. (The fast-path skip-if-authenticated logic
   in `startup()` is only used by `start-claudia.sh`'s pre-Chainlit Phase -1, not this in-chat
   button.)
4. ConnectivityChecker detects recovery → "IBKR Gateway reconnected" alert

**Common issues:**
- **Login prompt on every restart**: Check `docker ps` — if the container is not running between restarts, the session is being lost before ClaudIA starts. Likely cause: Mac sleep (caffeinate should prevent this) or Docker Desktop stopping.
- **Competing session**: Another TWS/mobile session holds the token. Log out from all other IBKR sessions, then re-authenticate via the gateway URL.
- **Gateway starts but session drops immediately**: Competing session or IBKR 2FA timing issue. Click "Start IBKR Gateway" again for a clean state.
- **Container present but not authenticated**: Session timed out. Full path runs — remove/recreate/login.
