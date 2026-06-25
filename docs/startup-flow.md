# ClaudIA Startup Flow

Documents every phase of `on_chat_start` (in `claudia/app.py`) in order.
Use this to diagnose startup failures: each phase is labeled with where to look.

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
- If a file is missing, the loader falls back to the example template or empty string

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
across Chainlit sessions. It polls every 15 seconds.

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
every 15s prevents IBKR auto-logout (session times out after ~5-10 minutes
without a tickle call).

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

## IBKR reconnection sequence (after reboot or session timeout)

1. ClaudIA starts → `ping()` returns False → "Start IBKR Gateway" button shown
2. User clicks button → Docker Desktop launched (if not running)
3. Gateway container started (existing container removed first for clean state)
4. Wait up to 120s for Java process to be reachable
5. `https://localhost:5055` opened in browser
6. User completes IBKR login + mobile 2FA
7. ConnectivityChecker polls within 15s → detects `authenticated=true, connected=true`
8. "IBKR Gateway reconnected" alert sent in chat

**Common issues:**
- **"competing" session**: Another TWS/mobile session is holding the token. Log out
  from all other IBKR sessions, then re-authenticate via the gateway URL.
- **Gateway starts but session drops immediately**: The gateway may have been
  restarted mid-session by another process. Click "Start IBKR Gateway" again to
  get a clean container.
- **Login page shows "connected / close this window" but session drops**: IBKR mobile
  2FA bug — restart the gateway container via the button to get a fresh auth state.
