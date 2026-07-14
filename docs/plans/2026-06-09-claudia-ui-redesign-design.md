# ClaudIA UI Redesign — Design Spec

**Date:** 2026-06-09  
**Status:** Approved  
**Scope:** Custom CSS/JS overlay on Chainlit — Claude-like dark chat + fixed connectivity status bar with live lights and disconnect alerts.

---

## Goals

1. Make the chat feel like Claude.ai — centered, clean, dark, distraction-free.
2. Show live green/red connectivity lights for Google Drive, IBKR, and TradingView in a fixed bar that is always visible regardless of scroll position.
3. Display ClaudIA's logo (red/black character) top-left.
4. Push a chat-window alert message when any service disconnects or reconnects.
5. Keep resource usage minimal — single local user, polling at 60s intervals, all checks are near-zero cost.

---

## Architecture

### New files

| File | Purpose |
|---|---|
| `public/custom.css` | Dark Claude-like theme + fixed status bar styles |
| `public/custom.js` | Injects status bar DOM; polls `/api/status` every 60s; updates dot colors |
| `public/claudia-logo.png` | ClaudIA logo — user places this file once |
| `claudia/status.py` | `ConnectivityChecker`: asyncio background task, in-memory status cache, disconnect alert push |

### Modified files

| File | Change |
|---|---|
| `.chainlit/config.toml` | Enable `custom_css`, `custom_js`; set `name = "ClaudIA"`; set `logo_file_url` |
| `claudia/app.py` | Mount `/api/status` HTTP endpoint; start `ConnectivityChecker` on `on_chat_start` |

### Data flow

```
Browser JS (every 60s)
  → GET /api/status
  → claudia/app.py route handler
  → returns cached dict (instant, no blocking)
  → JS updates dot colors in DOM

claudia/status.py ConnectivityChecker (every 60s, asyncio background task)
  → check_ibkr()       — GET /v1/api/tickle to localhost
  → check_gdrive()     — os.path.exists(token_file)
  → check_tradingview() — subprocess.poll() on sidecar PID
  → update in-memory cache dict
  → on state transition OK→ERROR or ERROR→OK: push cl.Message to chat
```

---

## Status Bar — Visual Spec

```
┌─────────────────────────────────────────────────────────────┐
│  [ClaudIA logo]        ● GDrive   ● IBKR   ● TradingView    │  ← 36px fixed
└─────────────────────────────────────────────────────────────┘
```

- **Height:** 36px, `position: fixed`, `top: 0`, `z-index: 9999`
- **Background:** `#111111` with a `1px` bottom border in `#e11d48` (red accent)
- **Logo:** 28px height, 12px left padding
- **Dot size:** 10px circle
- **Dot colors:**
  - `#22c55e` — connected (green)
  - `#ef4444` — disconnected (red)
  - `#6b7280` — checking / unknown (gray)
- **Labels:** 11px, color `#9ca3af`, positioned right of each dot with 4px gap
- **Layout:** logo left, lights right with `gap: 16px` between each service

**Initial load:** all dots gray ("checking") until first poll completes (~1s).

---

## Connectivity Checks — Implementation

### IBKR
```python
GET {IBKR_GATEWAY_URL}/tickle   # keepalive ping, no auth
# ok if HTTP 200; error if ConnectionError, timeout, or non-200
# timeout: 3s
```

### Google Drive
```python
os.path.exists(config.gdrive_token_file)
# ok if token file exists; error if missing
# Rationale: if token file is gone, all GDrive ops will fail.
# A full API call is not worth the overhead for a monitor light.
```

### TradingView
```python
bridge._process.poll() if bridge._process else None
# ok if poll() returns None (process still running)
# error if process has exited or bridge was never started
```

### Poll interval
60 seconds. Both the backend checker and JS frontend use the same 60s interval so the JS always reflects the latest backend state without unnecessary fetch overhead.

---

## Disconnect Alert — Behavior

A state machine per service with three states: `UNKNOWN`, `OK`, `ERROR`.

**Alert rules:**
- `OK → ERROR`: push warning message to chat (red/warning style)
- `ERROR → OK`: push recovery message to chat (green/info style)
- Repeated `ERROR → ERROR`: no alert (suppress noise)
- `UNKNOWN → OK` at startup: no alert (normal startup)
- `UNKNOWN → ERROR` at startup: push one startup alert

**Message format:**
```
⚠️  IBKR Gateway disconnected — check the Client Portal and log in.
⚠️  Google Drive disconnected — check credentials file.
⚠️  TradingView sidecar stopped — TradingView tools unavailable.

✅  IBKR Gateway reconnected.
✅  Google Drive reconnected.
✅  TradingView reconnected.
```

Alert is pushed via `cl.Message(..., author="System")` using `cl.run_sync` — same pattern as `alert_manager.py`.

The `ConnectivityChecker` holds a reference to the Chainlit session via a shared `asyncio.Queue` or direct session reference set in `on_chat_start`. Since this is a single-user local app, a module-level reference to the active session is safe and simple.

---

## Claude-like Chat Restyling — CSS Spec

| Property | Value | Rationale |
|---|---|---|
| Page background | `#0f0f0f` | Near-black, like Claude dark mode |
| Message container | max-width `720px`, centered | Matches Claude.ai column width |
| Assistant message | No background bubble; text on dark bg | Clean, Claude-style |
| User message | `#1e1e1e` card, rounded `12px` | Subtle distinction |
| Input bar | `#1a1a1a`, `border-radius: 24px`, bottom-pinned | Pill input like Claude |
| Accent color | `#e11d48` | Matches ClaudIA logo red |
| Focus rings / send button | `#e11d48` | Consistent accent |
| Font | `system-ui, -apple-system, sans-serif` | Platform-native, same feel as Claude |
| Body top padding | `36px` | Offset for fixed status bar |
| Tool step border | `2px left border #e11d48` | Makes tool calls visually distinct |

Chainlit's existing auto-scroll behavior is preserved — CSS does not interfere with it.

---

## Chainlit Config Changes

In `.chainlit/config.toml`:

```toml
[UI]
name = "ClaudIA"
default_theme = "dark"
logo_file_url = "/public/claudia-logo.png"
custom_css = "/public/custom.css"
custom_js = "/public/custom.js"
cot = "tool_call"   # show tool names only, not full JSON — cleaner for trading use
```

---

## Logo

The ClaudIA logo image (`claudia-logo.png`) is placed in `public/` once by the user.  
It is referenced at two points:
1. Chainlit native header via `logo_file_url` in config (shown in Chainlit's sidebar/header)
2. Directly in the CSS/JS-injected status bar via `<img src="/public/claudia-logo.png">`

---

## Out of Scope

- No authentication or access control changes
- No changes to the agent loop, tools, or order flow
- No server-side WebSocket push for status updates (polling at 60s is sufficient for a monitor light)
- Voice output (Phase 2, separate spec)
- Mobile/responsive layout (single desktop user)

---

## Testing

1. Start app: `chainlit run claudia/app.py` — status bar appears with all dots gray, then green within ~2s
2. Stop IBKR gateway → dot turns red within 60s + alert message appears in chat
3. Restart IBKR gateway → dot turns green within 60s + recovery message appears in chat
4. Remove GDrive token file → GDrive dot turns red
5. Kill tradingview-mcp sidecar → TradingView dot turns red
6. Chat scroll: send 20+ messages, verify status bar remains visible at top throughout
7. Logo: confirm ClaudIA character appears top-left at correct size
