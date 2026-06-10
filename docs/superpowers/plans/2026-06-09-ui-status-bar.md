# ClaudIA UI Redesign — Status Bar & Dark Theme

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed top status bar with live green/red connectivity lights (GDrive / IBKR / TradingView), ClaudIA logo top-left, disconnect alerts in chat, and a Claude-like dark theme — all via CSS/JS injection without touching Chainlit internals.

**Architecture:** `claudia/status.py` runs a 60s asyncio poll loop that checks connectivity and pushes `cl.Message` alerts on state transitions. A `/api/status` route on the Chainlit server returns the cached status dict instantly. `public/custom.js` injects a `<div id="claudia-status-bar">` into the DOM on load and polls `/api/status` every 60s to update dot colors. `public/custom.css` styles the bar and overrides Chainlit's theme.

**Tech Stack:** Python asyncio + requests (IBKR ping), Chainlit Starlette server extension, vanilla JS (no dependencies), CSS custom properties + Chainlit 2.11.0 MUI selectors.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `claudia/status.py` | `ConnectivityChecker`: poll loop, cached status dict, alert push |
| Create | `tests/test_status.py` | Unit tests for all three check methods + state transitions |
| Create | `public/custom.css` | Status bar layout + dark Claude-like theme |
| Create | `public/custom.js` | DOM injection + 60s polling + dot color updates |
| Place | `public/claudia-logo.png` | ClaudIA logo image (user places once) |
| Modify | `claudia/app.py` | Add `/api/status` route; start/stop `ConnectivityChecker` |
| Modify | `.chainlit/config.toml` | Enable custom_css, custom_js, logo, name, dark theme |

---

## Task 1: `claudia/status.py` — ConnectivityChecker

**Files:**
- Create: `claudia/status.py`
- Create: `tests/test_status.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_status.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as req

from claudia.status import ConnectivityChecker, ServiceStatus


@pytest.fixture
def checker(tmp_path):
    return ConnectivityChecker(
        gateway_url="https://localhost:5055/v1/api",
        gdrive_token_file=tmp_path / "token.json",
    )


def test_check_ibkr_ok(checker):
    with patch("claudia.status.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        assert checker.check_ibkr() is True
        mock_get.assert_called_once_with(
            "https://localhost:5055/v1/api/tickle",
            timeout=3,
            verify=False,
        )


def test_check_ibkr_non_200(checker):
    with patch("claudia.status.requests.get") as mock_get:
        mock_get.return_value.status_code = 401
        assert checker.check_ibkr() is False


def test_check_ibkr_connection_error(checker):
    with patch("claudia.status.requests.get", side_effect=ConnectionError("refused")):
        assert checker.check_ibkr() is False


def test_check_ibkr_timeout(checker):
    with patch("claudia.status.requests.get", side_effect=req.Timeout("timeout")):
        assert checker.check_ibkr() is False


def test_check_gdrive_file_exists(checker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    checker._gdrive_token_file = token
    assert checker.check_gdrive() is True


def test_check_gdrive_file_missing(checker, tmp_path):
    checker._gdrive_token_file = tmp_path / "missing.json"
    assert checker.check_gdrive() is False


def test_check_tradingview_no_bridge(checker):
    assert checker.check_tradingview() is False


def test_check_tradingview_process_running(checker):
    bridge = MagicMock()
    bridge._process = MagicMock()
    bridge._process.poll.return_value = None   # None = still running
    checker._tv_bridge = bridge
    assert checker.check_tradingview() is True


def test_check_tradingview_process_exited(checker):
    bridge = MagicMock()
    bridge._process = MagicMock()
    bridge._process.poll.return_value = 1      # non-None = exited
    checker._tv_bridge = bridge
    assert checker.check_tradingview() is False


def test_check_tradingview_no_process_attr(checker):
    bridge = MagicMock(spec=[])                # no _process attribute
    checker._tv_bridge = bridge
    assert checker.check_tradingview() is False


def test_get_status_initial(checker):
    s = checker.get_status()
    assert s == {"ibkr": "unknown", "gdrive": "unknown", "tv": "unknown"}


def test_get_status_returns_copy(checker):
    s1 = checker.get_status()
    s1["ibkr"] = "tampered"
    assert checker.get_status()["ibkr"] == "unknown"  # original unchanged
```

- [ ] **Step 1.2: Run tests — confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_status.py -v
```

Expected: `ModuleNotFoundError: No module named 'claudia.status'`

- [ ] **Step 1.3: Create `claudia/status.py`**

```python
"""
Connectivity monitor for ClaudIA.

Polls IBKR gateway, GDrive token file, and TradingView sidecar every 60s.
Caches status in memory (instant reads for /api/status endpoint).
Pushes cl.Message alerts to chat on state transitions.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from claudia.tradingview import TradingViewBridge

log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds


class ServiceStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


_DISCONNECT_MESSAGES = {
    "ibkr":   "⚠️ **IBKR Gateway disconnected** — check the Client Portal and log in.",
    "gdrive": "⚠️ **Google Drive disconnected** — credentials file not found.",
    "tv":     "⚠️ **TradingView sidecar stopped** — TradingView tools unavailable.",
}
_RECONNECT_MESSAGES = {
    "ibkr":   "✅ **IBKR Gateway reconnected.**",
    "gdrive": "✅ **Google Drive reconnected.**",
    "tv":     "✅ **TradingView reconnected.**",
}


class ConnectivityChecker:
    def __init__(
        self,
        gateway_url: str,
        gdrive_token_file: Path,
        tv_bridge: Optional["TradingViewBridge"] = None,
    ) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._gdrive_token_file = Path(gdrive_token_file)
        self._tv_bridge = tv_bridge
        self._status: dict[str, str] = {
            "ibkr":   ServiceStatus.UNKNOWN,
            "gdrive": ServiceStatus.UNKNOWN,
            "tv":     ServiceStatus.UNKNOWN,
        }
        self._task: asyncio.Task | None = None

    def get_status(self) -> dict[str, str]:
        return dict(self._status)

    # ── Individual checks (synchronous, cheap) ──────────────────────────────

    def check_ibkr(self) -> bool:
        try:
            resp = requests.get(
                f"{self._gateway_url}/tickle",
                timeout=3,
                verify=False,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def check_gdrive(self) -> bool:
        return self._gdrive_token_file.exists()

    def check_tradingview(self) -> bool:
        bridge = self._tv_bridge
        if bridge is None:
            return False
        proc = getattr(bridge, "_process", None)
        if proc is None:
            return False
        return proc.poll() is None  # None = process still alive

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            log.info("ConnectivityChecker started (interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("ConnectivityChecker stopped")

    # ── Internal ────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        await self._run_checks()          # run once immediately on start
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self._run_checks()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ConnectivityChecker poll error: %s", exc)

    async def _run_checks(self) -> None:
        new = {
            "ibkr":   ServiceStatus.OK if await asyncio.to_thread(self.check_ibkr) else ServiceStatus.ERROR,
            "gdrive": ServiceStatus.OK if self.check_gdrive() else ServiceStatus.ERROR,
            "tv":     ServiceStatus.OK if self.check_tradingview() else ServiceStatus.ERROR,
        }
        for service, new_state in new.items():
            prev_state = self._status[service]
            if prev_state != new_state:
                self._status[service] = new_state
                await self._send_alert(service, prev_state, new_state)

    async def _send_alert(self, service: str, prev: str, new: str) -> None:
        import chainlit as cl
        if new == ServiceStatus.ERROR:
            msg = _DISCONNECT_MESSAGES.get(service, f"⚠️ {service} disconnected.")
        elif new == ServiceStatus.OK and prev == ServiceStatus.ERROR:
            msg = _RECONNECT_MESSAGES.get(service, f"✅ {service} reconnected.")
        else:
            return  # UNKNOWN → OK at startup: silent
        try:
            await cl.Message(content=msg, author="System").send()
        except Exception as exc:
            log.warning("Could not push connectivity alert: %s", exc)
```

- [ ] **Step 1.4: Run tests — confirm they pass**

```bash
.venv/bin/python -m pytest tests/test_status.py -v
```

Expected: `12 passed`

- [ ] **Step 1.5: Commit**

```bash
git add claudia/status.py tests/test_status.py
git commit -m "feat: add ConnectivityChecker for IBKR/GDrive/TradingView monitoring"
```

---

## Task 2: `/api/status` route + wire ConnectivityChecker into `app.py`

**Files:**
- Modify: `claudia/app.py`

- [ ] **Step 2.1: Add imports and module-level route at the top of `app.py`**

Add after the existing imports block (after `from claudia.tradingview import TradingViewBridge`):

```python
from claudia.status import ConnectivityChecker
from chainlit.server import app as _server_app
from starlette.responses import JSONResponse

_connectivity_checker: ConnectivityChecker | None = None


@_server_app.get("/api/status")
async def api_status():
    """Returns cached connectivity status — instant, non-blocking."""
    if _connectivity_checker:
        return JSONResponse(_connectivity_checker.get_status())
    return JSONResponse({"ibkr": "unknown", "gdrive": "unknown", "tv": "unknown"})
```

- [ ] **Step 2.2: Start ConnectivityChecker in `on_chat_start`**

In `on_chat_start`, after the `tv_bridge` block (after the `except Exception as exc: log.warning(...)` block for TradingView), add:

```python
    # Start connectivity monitor (singleton — persists across sessions)
    global _connectivity_checker
    if _connectivity_checker is None:
        cfg = _config or Config.from_env()
        _connectivity_checker = ConnectivityChecker(
            gateway_url=cfg.gateway_url,
            gdrive_token_file=cfg.gdrive_token_file,
            tv_bridge=_tv_bridge,
        )
    _connectivity_checker.start()
```

- [ ] **Step 2.3: Verify the route is reachable**

Start the app, then in a separate terminal:

```bash
curl -s http://localhost:8000/api/status
```

Expected: `{"ibkr":"ok","gdrive":"ok","tv":"unknown"}` (or similar — depends on what's running)

- [ ] **Step 2.4: Commit**

```bash
git add claudia/app.py
git commit -m "feat: add /api/status route and wire ConnectivityChecker into session start"
```

---

## Task 3: Update `.chainlit/config.toml`

**Files:**
- Modify: `.chainlit/config.toml`

- [ ] **Step 3.1: Update the `[UI]` section**

Find the `[UI]` section (currently around line 96) and replace the existing `name = "Assistant"` line plus the commented-out `default_theme`, `custom_css`, `custom_js` lines with:

```toml
[UI]
name = "ClaudIA"
default_theme = "dark"
custom_css = "/public/custom.css"
custom_js = "/public/custom.js"
logo_file_url = "/public/claudia-logo.png"
cot = "tool_call"
confirm_new_chat = true
```

Keep all other existing `[UI]` settings (alert_style, etc.) unchanged.

- [ ] **Step 3.2: Commit**

```bash
git add .chainlit/config.toml
git commit -m "config: enable dark theme, custom CSS/JS, and ClaudIA logo"
```

---

## Task 4: `public/custom.css` — Dark theme + status bar

**Files:**
- Create: `public/custom.css`

- [ ] **Step 4.1: Create `public/custom.css`**

```css
/*
  ClaudIA custom theme — Chainlit 2.11.x
  Targets Chainlit's MUI/React structure. If a selector stops working after
  a Chainlit upgrade, inspect the element in DevTools and update the selector.
*/

/* ── Design tokens ───────────────────────────────────────────── */
:root {
  --cl-accent:        #e11d48;
  --cl-accent-dim:    rgba(225, 29, 72, 0.15);
  --cl-bg:            #0f0f0f;
  --cl-surface:       #161616;
  --cl-surface-2:     #1e1e1e;
  --cl-border:        #2a2a2a;
  --cl-text:          #e5e7eb;
  --cl-muted:         #9ca3af;
  --cl-green:         #22c55e;
  --cl-red:           #ef4444;
  --cl-status-bar-h:  36px;
}

/* ── Status bar spacer — push everything below our fixed bar ─── */
body {
  padding-top: var(--cl-status-bar-h) !important;
  background-color: var(--cl-bg) !important;
}

/* ── Chainlit header: shift down so it sits below our bar ─────── */
header {
  top: var(--cl-status-bar-h) !important;
  background-color: var(--cl-surface) !important;
  border-bottom: 1px solid var(--cl-border) !important;
  box-shadow: none !important;
}

/* ── Page background ─────────────────────────────────────────── */
#root,
[class*="MuiBox-root"] {
  background-color: var(--cl-bg);
}

/* ── Sidebar ─────────────────────────────────────────────────── */
nav,
[data-testid="thread-history"] {
  background-color: var(--cl-surface) !important;
  border-right: 1px solid var(--cl-border) !important;
}

/* ── Message column — Claude-style centered width ────────────── */
[class*="message-container"],
[data-testid="messages-container"],
.cl-chat-messages {
  max-width: 760px !important;
  margin-left: auto !important;
  margin-right: auto !important;
  padding-left: 16px !important;
  padding-right: 16px !important;
}

/* ── User message — subtle dark card ────────────────────────── */
[data-testid="user-message"],
[class*="userMessage"] {
  background-color: var(--cl-surface-2) !important;
  border-radius: 12px !important;
  border: 1px solid var(--cl-border) !important;
  padding: 12px 16px !important;
}

/* ── Assistant message — no bubble, clean text ───────────────── */
[data-testid="assistant-message"],
[class*="assistantMessage"] {
  background-color: transparent !important;
  border: none !important;
}

/* ── Tool steps — red left accent ───────────────────────────── */
[data-testid="step"],
.step {
  border-left: 2px solid var(--cl-accent) !important;
  padding-left: 12px !important;
  margin: 4px 0 8px 0 !important;
  background-color: var(--cl-surface) !important;
  border-radius: 0 6px 6px 0 !important;
}

/* ── Chat input area ─────────────────────────────────────────── */
[data-testid="chat-input"],
[class*="chatInput"],
form[class*="chat"] {
  background-color: var(--cl-surface) !important;
  border-top: 1px solid var(--cl-border) !important;
  padding: 12px 16px !important;
}

[data-testid="chat-input"] textarea,
[data-testid="chat-input"] input[type="text"],
[class*="chatInput"] textarea {
  background-color: var(--cl-surface-2) !important;
  border-radius: 24px !important;
  border: 1px solid var(--cl-border) !important;
  color: var(--cl-text) !important;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif !important;
  padding: 10px 18px !important;
  transition: border-color 0.2s ease !important;
}

[data-testid="chat-input"] textarea:focus,
[data-testid="chat-input"] input[type="text"]:focus {
  border-color: var(--cl-accent) !important;
  outline: none !important;
  box-shadow: 0 0 0 2px var(--cl-accent-dim) !important;
}

/* ── Send button ─────────────────────────────────────────────── */
button[data-testid="send-button"],
[class*="sendButton"] {
  background-color: var(--cl-accent) !important;
  color: #fff !important;
  border-radius: 50% !important;
}

button[data-testid="send-button"]:hover {
  background-color: #be1238 !important;
}

/* ── Typography ──────────────────────────────────────────────── */
body, p, span, div {
  font-family: system-ui, -apple-system, BlinkMacSystemFont,
               "Segoe UI", Roboto, sans-serif;
  color: var(--cl-text);
}

/* ── Scrollbar ───────────────────────────────────────────────── */
::-webkit-scrollbar        { width: 5px; height: 5px; }
::-webkit-scrollbar-track  { background: var(--cl-bg); }
::-webkit-scrollbar-thumb  { background: var(--cl-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--cl-accent); }

/* ── Status bar (DOM injected by custom.js) ──────────────────── */
#claudia-status-bar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: var(--cl-status-bar-h);
  background-color: #111111;
  border-bottom: 1px solid var(--cl-accent);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  z-index: 99999;
  box-sizing: border-box;
}

#claudia-status-bar .cl-logo {
  height: 28px;
  width: auto;
  object-fit: contain;
  display: block;
}

#claudia-status-bar .cl-services {
  display: flex;
  align-items: center;
  gap: 20px;
}

#claudia-status-bar .cl-service {
  display: flex;
  align-items: center;
  gap: 6px;
}

#claudia-status-bar .cl-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background-color: #6b7280;   /* gray = unknown/checking */
  flex-shrink: 0;
  transition: background-color 0.4s ease;
}

#claudia-status-bar .cl-dot.ok    { background-color: #22c55e; }
#claudia-status-bar .cl-dot.error {
  background-color: #ef4444;
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.6);
}

#claudia-status-bar .cl-label {
  font-size: 11px;
  font-family: system-ui, -apple-system, sans-serif;
  color: #9ca3af;
  letter-spacing: 0.03em;
  white-space: nowrap;
  user-select: none;
}
```

- [ ] **Step 4.2: Commit**

```bash
git add public/custom.css
git commit -m "feat: add dark Claude-like theme and status bar CSS"
```

---

## Task 5: `public/custom.js` — Status bar injection + polling

**Files:**
- Create: `public/custom.js`

- [ ] **Step 5.1: Create `public/custom.js`**

```javascript
/**
 * ClaudIA UI — status bar injector
 * Injects a fixed top bar with live connectivity dots.
 * Polls /api/status every 60s (matches backend poll interval).
 */
(function () {
  'use strict';

  var POLL_MS = 60000;
  var SERVICES = [
    { key: 'gdrive', label: 'GDrive' },
    { key: 'ibkr',   label: 'IBKR' },
    { key: 'tv',     label: 'TradingView' },
  ];

  function createBar() {
    var bar = document.createElement('div');
    bar.id = 'claudia-status-bar';

    // Logo
    var logo = document.createElement('img');
    logo.src = '/public/claudia-logo.png';
    logo.className = 'cl-logo';
    logo.alt = 'ClaudIA';
    bar.appendChild(logo);

    // Services
    var svcWrap = document.createElement('div');
    svcWrap.className = 'cl-services';

    SERVICES.forEach(function (svc) {
      var item = document.createElement('div');
      item.className = 'cl-service';

      var dot = document.createElement('span');
      dot.className = 'cl-dot';
      dot.id = 'cl-dot-' + svc.key;
      dot.title = svc.label + ': checking…';

      var lbl = document.createElement('span');
      lbl.className = 'cl-label';
      lbl.textContent = svc.label;

      item.appendChild(dot);
      item.appendChild(lbl);
      svcWrap.appendChild(item);
    });

    bar.appendChild(svcWrap);
    document.body.prepend(bar);
  }

  function updateDots(status) {
    SERVICES.forEach(function (svc) {
      var dot = document.getElementById('cl-dot-' + svc.key);
      if (!dot) return;
      var state = (status && status[svc.key]) || 'unknown';
      dot.className = 'cl-dot';                     // reset
      if (state === 'ok')    dot.classList.add('ok');
      if (state === 'error') dot.classList.add('error');
      dot.title = svc.label + ': ' + state;
    });
  }

  function poll() {
    fetch('/api/status', { cache: 'no-store' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) { if (data) updateDots(data); })
      .catch(function () { /* network error — keep current dots */ });
  }

  function init() {
    createBar();
    poll();                            // immediate first check
    setInterval(poll, POLL_MS);        // then every 60s
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
```

- [ ] **Step 5.2: Commit**

```bash
git add public/custom.js
git commit -m "feat: inject status bar and start 60s polling via custom.js"
```

---

## Task 6: Place the ClaudIA logo

**Files:**
- Place: `public/claudia-logo.png`

- [ ] **Step 6.1: Copy the logo file into `public/`**

The logo image (the red/black ClaudIA character) must be saved as `public/claudia-logo.png` in the project root. It is referenced in both:
- `.chainlit/config.toml` → `logo_file_url = "/public/claudia-logo.png"` (Chainlit native header)
- `public/custom.js` → `img.src = '/public/claudia-logo.png'` (status bar)

Copy or export the file from wherever it is stored and place it at:
```
/Users/steph/Claude_Projects/claudia_ui/public/claudia-logo.png
```

- [ ] **Step 6.2: Confirm file exists**

```bash
ls -lh public/claudia-logo.png
```

Expected: file present, size > 0

- [ ] **Step 6.3: Commit**

```bash
git add public/claudia-logo.png
git commit -m "assets: add ClaudIA logo to public/"
```

---

## Task 7: Full end-to-end verification

- [ ] **Step 7.1: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ -v -m "not integration"
```

Expected: all tests pass (at minimum the 12 new status tests + 26 existing = 38 total)

- [ ] **Step 7.2: Start the app**

```bash
chainlit run claudia/app.py
```

Open `http://localhost:8000` in a browser.

Expected:
- Status bar appears as a 36px dark strip at the very top
- ClaudIA logo visible top-left
- Three dots visible: GDrive / IBKR / TradingView
- All dots start gray, then update to green/red within ~2s (first poll completes)
- Chat content appears below the bar, not overlapping it

- [ ] **Step 7.3: Verify disconnect alert — IBKR**

Stop the IBKR Client Portal Gateway. Wait up to 60s.

Expected:
- IBKR dot turns red
- Chat window receives: `⚠️ **IBKR Gateway disconnected** — check the Client Portal and log in.`

- [ ] **Step 7.4: Verify reconnect alert — IBKR**

Restart the IBKR Client Portal Gateway. Wait up to 60s.

Expected:
- IBKR dot turns green
- Chat window receives: `✅ **IBKR Gateway reconnected.**`

- [ ] **Step 7.5: Verify GDrive disconnect**

```bash
mv ~/.ibkr_core/token.json ~/.ibkr_core/token.json.bak
```

Wait up to 60s.

Expected: GDrive dot turns red + alert in chat.

```bash
mv ~/.ibkr_core/token.json.bak ~/.ibkr_core/token.json
```

- [ ] **Step 7.6: Verify scroll behavior**

Send 10+ messages. Scroll up through the history.

Expected: status bar stays fixed at top throughout. Chat scrolls normally underneath it.

- [ ] **Step 7.7: Final commit**

```bash
git add -A
git push origin main
```

---

## CSS Selector Troubleshooting

Chainlit uses MUI (Material UI) with emotion-generated class names. If a theme override looks wrong after a Chainlit upgrade, inspect the element in Chrome DevTools (⌘⌥I → Elements), find the element, and update the selector in `public/custom.css`.

The most stable selectors are `data-testid` attributes (used in Steps 4.1). Class names like `[class*="userMessage"]` are fallbacks — they match partial class names and are less likely to break.

The status bar itself (`#claudia-status-bar`) is fully self-contained with a fixed ID — it will never be affected by Chainlit updates.
