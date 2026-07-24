# Research: PineScript copy/inject + Panel actionable-button capabilities (2026-07-24)

Scraped/verified per the "API Docs First" convention (CLAUDE.md) at the user's request
("research doc, use scraping"; "research more on actionable buttons too, will be useful
in the future for: reconnect, end"). Every claim below is backed by an authoritative
source: the **installed** package (panel 1.9.3 — source of truth for our version), the
**local** tradingview-mcp sidecar source, or an official web doc (scraped via firecrawl).
Probe scripts: `scratchpad/probe_clipboard.py`, `probe_jsargs.py`, `probe_buttons.py`.

---

## Part A — PineScript copy/inject (Phase 9 Task 9.2, immediate)

**Feature revival:** the Chainlit `render_pinescript`/`copy_pinescript`/`inject_pinescript`
(`tradingview.py:377-440`) are dead code (`render_pinescript` called nowhere). User wants
the feature WORKING in Panel, auto-detecting ```pine blocks, leveraging Panel's advantages.
Memory: `project-pinescript-feature-revival`.

### A1. `pine_set_source` sidecar contract — LOCAL SOURCE (authoritative)

`~/.tradingview-mcp/src/tools/pine.js:11-14`:

```js
server.tool('pine_set_source', 'Set Pine Script source code in the editor', {
  source: z.string().describe('Pine Script source code to inject'),
}, async ({ source }) => { return jsonResult(await core.setSource({ source })); });
```

- Input: `{ "source": "<pine code>" }`. Output: JSON `{ success, source, ... }`; on error
  `{ success: false, source: "internal_api", error: <msg> }` (`pine.js:54`).
- It IS a curated tool (`claudia/tradingview.py:126`, in `_CURATED_TOOLS`), so it's already
  in the agent's TV toolset when connected. Inject call:
  `await _tv_bridge.execute("pine_set_source", {"source": code})`.
- Requires TradingView Desktop connected via CDP (bridge `_session` live); otherwise the
  bridge is None / not started → honest "TradingView not connected" message.

### A2. Real client-side clipboard — Panel `js_on_click` (INSTALLED + MDN)

Installed (`pn.widgets.Button.js_on_click`, panel 1.9.3):
`js_on_click(args: dict = {}, code: str = '') -> Callback` — "Allows defining a JS callback
triggered when the button is clicked. args: mapping of objects made available to the JS
callback. code: the JavaScript to execute."

**Injection-safe pattern (probe-verified `probe_jsargs.py`):** pass the pine code as an
`args` value (Bokeh SERIALIZES it into a JS variable — NOT string-concatenated into `code`,
so quotes/backticks/newlines are safe), then reference it by name:

```python
copy_btn = pn.widgets.Button(label="Copy PineScript", color="light")
copy_btn.js_on_click(args={"code": pine_code},
                     code="navigator.clipboard.writeText(code)")
```

**`navigator.clipboard.writeText` — MDN (authoritative,**
https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText **):** returns a
Promise; **secure-context ONLY**; throws `NotAllowedError` otherwise. **localhost is a
secure context**, so serving on `localhost:8001` works with no HTTPS. Baseline
widely-available since March 2020. A click handler provides the required transient
activation (user gesture), so writeText from `js_on_click` is permitted. This is the
concrete Panel-over-Chainlit win: Chainlit runs server-side with no clipboard API, so its
"copy" only re-displayed the code for manual selection (`tradingview.py:410-415`).

Refs: https://panel.holoviz.org/reference/widgets/Button.html (v1.9.3),
https://panel.holoviz.org/how_to/links/jslinks.html. Discourse confirms `js_on_click` and a
Python `on_click` action can coexist on one button
(https://discourse.holoviz.org/t/button-with-both-js-on-click-and-python-action/1785) — but
we use SEPARATE buttons (Copy = JS, Inject = Python async), which is simpler.

### A3. ```pine detection regex (probe-verified `probe_clipboard.py`)

`re.findall(r"```pine\b[^\n]*\n(.*?)```", text, re.DOTALL)` — captures the code of every
```pine block; `\b` avoids matching a longer fence tag, `[^\n]*` tolerates an info string
after `pine`, multiple blocks per message handled. `send_message` receives the full
`display_text` (proposal blocks are stripped, ```pine is NOT — `agent.py:666`), so
detection in the Panel sink path needs ZERO change to safety-critical `agent.py`.

---

## Part B — Actionable buttons (reference for FUTURE: reconnect, end)

Enumerated authoritatively from the installed `pn.widgets.Button` (1.9.3, `probe_buttons.py`)
plus the scraped Notifications reference. This is the toolkit for the richer
reconnect / end-session / launch actions the user flagged for later.

### B1. `pn.widgets.Button` params (all present in 1.9.3)

| Param | Values / type | Use for reconnect/end |
|---|---|---|
| `label` | str | button text (`name` is the deprecated alias — use `label`) |
| `color` | default/primary/success/warning/danger/**light** | semantic color; `color=` is an alias that sets `button_type` |
| `button_type` | same set | same as `color` |
| `button_style` | **solid** / **outline** | outline for secondary actions (e.g. a low-emphasis "End") |
| `icon` | str (tabler icon name, e.g. `"refresh"`, `"power"`) | icon-labeled actions |
| `icon_size` | str (e.g. `"1.2em"`) | |
| `description` | str/tooltip | hover tooltip (replaces Chainlit's `tooltip=`) |
| `disabled` | bool | disable-first-on-click (already the Phase 3 pattern) |
| **`loading`** | bool | **spinner while an async action runs** — ideal for reconnect/launch (multi-second CDP/gateway waits); set True at click, False in `finally` |
| `css_classes` | list[str] | restyle-plan hook (target specific buttons in CSS) |
| `width`/`height` | int | |

Actions: `on_click(callback)` (Python; callback may be async — awaited on the session
loop), `js_on_click(args, code)` (pure client-side JS), `jscallback(args, **events)`. They
compose (JS + Python can both fire).

### B2. Notifications / toasts — `pn.state.notifications` (scraped reference)

https://panel.holoviz.org/reference/global/Notifications.html — enable via
`pn.extension(notifications=True)` (or a template with notifications on). API:

```python
pn.state.notifications.success("Reconnected.")                 # default 3s
pn.state.notifications.error("Reconnect failed: …", duration=0)  # 0 = sticky until dismissed
pn.state.notifications.info("…", duration=2000)
pn.state.notifications.warning("…", duration=4000)
n = pn.state.notifications.success("…", duration=0); n.destroy()  # programmatic dismiss
pn.state.notifications.clear()                                    # clear all
```

**Why it matters for reconnect/end:** today those actions stream progress as chat
`System` messages (functional, but clutters the feed). Toasts give transient,
non-intrusive feedback that doesn't pollute conversation history — a natural restyle-era
upgrade. Sticky (`duration=0`) toasts suit "reconnecting…" states cleared on completion.
NOTE: `pn.state.notifications` is None unless notifications are enabled at extension/serve
time — a `pn.serve`/`pn.extension` wiring change (verify before use; deferred to when the
feature lands, per the function-first ruling).

### B3. Confirm-before-destructive — template modal (probe-verified present)

`pn.template.BootstrapTemplate` (and others) expose a `modal` area — usable for an "End
session? This uploads and closes." confirm dialog before the irreversible cleanup, instead
of a bare button. Deferred: templates are a Phase 7 (restyle) concern; recorded here so the
restyle plan has the mechanism. For now End Session stays a direct button (Phase 5).

### B4. Restyle-plan carry-forward (do NOT act during migration)

- `loading` spinner on Start Gateway / Launch TV / (future) Reconnect — removes the
  "did my click register?" ambiguity during the multi-second waits.
- `icon=` + `description=` tooltips for a denser, clearer action row.
- Toasts for action outcomes; modal for destructive confirms.
- `css_classes` on each action button as styling hooks.
These are collected for the dedicated post-migration restyle plan
(`project-migration-styling-deferred`), not built now.
