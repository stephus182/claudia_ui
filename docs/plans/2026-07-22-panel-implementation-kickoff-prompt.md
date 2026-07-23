# ClaudIA UI Migration — Panel Implementation Kickoff Prompt

Portable prompt for starting the Chainlit → Panel migration as a separate, parallel
workstream — copy the block below into a fresh Claude Code session (or hand to an
Agent/subagent) to kick it off. Self-contained: written to need no memory of the
research conversation that produced it.

---

## Prompt

I'm migrating ClaudIA (a Chainlit-based trading assistant chatbot in `claudia_ui`,
connecting to Interactive Brokers via a local `ibkr_core_mcp` package) from Chainlit
to **Panel** (HoloViz) as the UI framework. This decision is final — a full comparison
of 6 candidates (NiceGUI, Panel, Reflex, Gradio, Mesop, custom FastAPI+HTMX) was
already completed and Panel was selected. **Do not re-litigate the framework choice**
or re-run that comparison — go straight to planning and building the migration.

### Do this first: isolate the work

`claudia_ui`'s `main` branch has ongoing, unfinished Chainlit-based development —
this migration must not disrupt it. Before touching any code:

1. Use the `using-git-worktrees` skill (or `git worktree add`) to create an isolated
   worktree/branch for this migration off `main`.
2. **Copy these three research files into the new worktree manually** — they are
   gitignored in the main repo (kept local + Google Drive only, deliberately not in
   git history), so a fresh worktree will *not* have them automatically:
   - `docs/plans/2026-07-19-ui-framework-research.md` — the full comparison, all
     citations, and the live-tested Shadow-DOM findings (see below)
   - `docs/plans/2026-07-19-ui-framework-research.html` — same content, rendered
   - `docs/plans/2026-07-22-panel-shadow-dom-live-test.md` — raw methodology/results
     of the DOM verification test
   - If those files are missing from the source `claudia_ui` checkout for any reason,
     they're also archived in Google Drive under `web_docs/` (via `ibkr_core_mcp`'s
     `WebDocsStore`) and published as a Claude Artifact — ask the user for the link
     if needed rather than re-deriving the research from scratch.
3. Read `CLAUDE.md` in full before writing any code — it defines Hard Rules that
   are framework-agnostic and must carry forward unchanged (see below).

### Then: plan before building

This is a multi-step migration with a real spec already in hand (the research doc).
Invoke the `writing-plans` skill next to turn this into a phased implementation plan
— do not start writing application code before that plan exists. No prototype/spike
is needed first; the research phase (including live DOM verification) is already
complete and the plan can go straight to full implementation.

### Non-negotiable constraints carried forward regardless of UI framework

These are from `claudia_ui/CLAUDE.md`'s Hard Rules for Developers — re-verify each
still holds after the migration, don't just assume Panel makes them automatic:

1. **Never add a tool that calls `place_order`, `modify_order`, `cancel_order`, or
   `reply_order`.** Order staging stays a UI-layer action triggered by a physical
   button click, never an LLM tool call — this is a UI concern to rebuild in Panel,
   not a backend concern to touch.
2. **Never log or expose `ANTHROPIC_API_KEY`** in any output, logs, or error messages.
3. **Never modify or weaken the hardcoded safety block** in the agent's streaming
   loop (currently `claudia/agent.py`).
4. **Never inject conversation history directly into the system prompt** — history
   must be added as `role: user/assistant` message objects.
5. **`ibkr_core_mcp` stays read-only from the UI's perspective** — never bypass
   `ClaudeToolkit` to call `IBKRClient` directly from a tool handler.
6. **Order parameters are immutable** — exact user values (symbol, action, quantity,
   price, type, TIF), no silent adjustment, ever.
7. **Gate 1 (Touch ID) and Gate 2 (native AppKit confirmation dialog) stay exactly
   as they are** — they're OS-native and already framework-agnostic (they live in
   `ibkr_core_mcp/human_auth.py` and `ibkr_core_mcp/order_confirm.py`, entirely
   outside the browser process). The Panel migration only needs to reproduce the
   pattern that triggers them: a chat message with 1-2 labeled buttons → a named
   server-side callback carrying a JSON payload → the button removed/disabled after
   click. Do not touch the gates themselves.

### Confirmed technical findings to build from (already researched and, where noted, live-tested — don't re-derive)

- **FastAPI mounting**: `panel.io.fastapi.add_application`/`add_applications`
  (Panel 1.5+) mounts directly onto an existing `FastAPI()` app — use this to extend
  the current app's ASGI object the same way `_fix_route_priority()` currently adds
  custom asset routes, rather than standing up a separate process.
- **Per-session state + background push**: each browser tab is a live server-side
  Bokeh document. Cross-thread/background-task push into a specific session uses
  `doc.add_next_tick_callback()` (thread-safe by design) — this replaces the current
  `ContextLoader` pattern (`contextvars.copy_context()` +
  `loop.call_soon_threadsafe(...create_task(..., context=_cl_ctx))`) that bridges the
  file-watcher thread, the Flex-sync task, and the doc-change notifier into the
  Chainlit event loop today. All three of those call sites need the Panel-native
  equivalent.
- **Message-with-buttons (the order-staging pattern)**: `ChatMessage.object` accepts
  an arbitrary Panel object — embed `pn.Row(pn.widgets.Button(...), pn.widgets.Button(...))`
  directly, bind normal `on_click` callbacks carrying any closure payload, and disable
  or remove via `button.disabled = True` / reassigning `message.object` after click.
  This is imperative (a direct Python object reference), not declarative — preserve
  that property, it's what makes the safety-critical remove-after-click step
  auditable.
- **Styling — live-tested via Playwright against a real running `ChatInterface`
  (Panel 1.9.3), not assumed:**
  - Message content sits nested ~7 Shadow DOM levels deep. A shared page-level
    stylesheet with class/tag selectors — the pattern `claudia/assets/custom.css`
    currently uses for Chainlit — **does not reach through and will not work.**
    Do not port that file's approach forward as-is.
  - **Inline `style="..."` attributes in generated message HTML work correctly**
    through all shadow levels — confirmed for red/green text coloring (e.g. P&L).
    This is how ClaudIA should emit colored/styled numbers going forward: inline
    styles per element, generated server-side, not a CSS class + stylesheet.
  - **Panel's own `stylesheets=[...]` parameter, passed to the Python component
    object itself, using `:host`/`:host *` selectors, works correctly** through all
    shadow levels — confirmed for font-family. Use this mechanism for broader
    theming (fonts, base colors), applied at the component level in Python, not via
    a shared external CSS file.
  - GFM/HTML tables render correctly as real `<table>` markup (not escaped) via
    Panel's markdown/HTML rendering — same inline-style rule applies for any styling
    beyond browser defaults.
  - Panel's markdown renderer does not escape raw HTML the way Chainlit's does —
    an actual improvement being migrated toward, not just parity.
- **Candlestick/OHLC charting**: `df.hvplot.ohlc()` is a one-line native candlestick
  chart with `pos_color`/`neg_color`/`line_color`/`bar_width` styling built for
  up/down candles, rendering through the caller's choice of Bokeh, Matplotlib, or
  Plotly — same call, switch backend by parameter. Recommended split: **Bokeh** for
  any live-updating dashboard pane (efficient incremental `ColumnDataSource` patches
  on tick updates, native to Panel's own rendering layer), **Matplotlib** for any
  static chart embedded directly in a chat message (no interactivity needed, cleanest
  "sober/professional" look). Don't build two separate charting code paths — same
  `hvplot.ohlc()` call, different backend argument.
- **Dashboard layout**: Panel's template system (`FastListTemplate`/`MaterialTemplate`,
  or the newer `panel.ui`/panel-material-ui namespace) supports a chat pane next to a
  live data/chart pane in one app — this is what a "chatbot + dashboard window"
  layout should be built on. **Target the modern `panel.ui`/panel-material-ui
  namespace, not legacy widgets** — Panel 2.0 (dual legacy/modern API) is targeted
  for Q2 2026 and Panel 3.0 (legacy removal) for 2027; panel-material-ui has already
  reached what the Panel team calls production maturity.
- **File upload**: `FileInput` (click + native drag-and-drop) or `FileDropper` for
  larger files — replaces Chainlit's `spontaneous_file_upload` mechanism used today
  for TradingView screenshot attachments.

### Known, already-scoped risk (informational — doesn't block starting)

Panel's chat *sub-components* specifically (`ChatInterface`/`ChatMessage`, not the
app shell) are the newest, least-proven part of the library:

- Open issue [holoviz/panel#6291](https://github.com/holoviz/panel/issues/6291)
  tracks chrome-level gaps: a combined file-upload+textarea input, keyboard-shortcut
  handling, and — relevant here — a "Status" component for showing agent/tool
  intermediate steps, which is the direct equivalent of Chainlit's `cl.Step` (used
  today for the tool-call collapsible display in `agent.py`). **This component may
  need to be hand-built** rather than assumed to exist off the shelf — check current
  Panel docs/changelog first, since this was open as of the 2026-07-19/22 research
  pass and may have shipped since.
- No independent third-party production case study was found for Panel-as-a-chatbot
  specifically (strong official examples exist — `panel-chat-examples`, HoloViz's own
  blog tutorials — but no outside "we run this in production" account either way).
  Panel-as-a-dashboard-framework has abundant, long-running production evidence; the
  chat surface specifically is comparatively newer ground.

Neither of these blocks starting — they're context for where to expect friction,
not open questions to resolve before beginning.

### Source reference

A fork of Panel exists at **[github.com/stephus182/panel](https://github.com/stephus182/panel)**
— kept as a read-only reference, and specifically so a fix for the missing
Status/tool-step component (issue #6291 above) could be sent upstream as a PR
someday if it comes to that. It is not a pinned dependency or a patched fork to
install from — the actual runtime dependency should still be installed normally
from PyPI (`pip install panel`) unless a specific reason to diverge comes up.

For actually reading Panel's internals *during* implementation (e.g. understanding
`ChatInterface`/`ChatMessage`, or how `panel-material-ui` wires up its Shadow DOM
components while building the Status/tool-step component), **clone the fork locally
next to the implementation worktree** — a local clone is what the implementing
session can actually grep/read directly, the same way it reads any other file in
this project, rather than browsing GitHub:

```bash
git clone https://github.com/stephus182/panel.git ../panel-source-reference
```

Add upstream and pull periodically to keep it current, since a fork left unsynced
drifts stale while upstream Panel keeps shipping:

```bash
cd ../panel-source-reference
git remote add upstream https://github.com/holoviz/panel.git
git fetch upstream && git merge upstream/main
```

### Scope reminder

This is being built **on the side** of the still-unfinished main Chainlit app, not
as an urgent replacement — there's no deadline pressure implied here. Take the
plan-first approach seriously given that: get the phased plan right via
`writing-plans` before writing implementation code, since this is a full UI
framework migration touching session lifecycle, streaming, order-staging safety
logic, and the frontend layer all at once.

---

*Source: `docs/plans/2026-07-19-ui-framework-research.md` (framework comparison,
recommendation, and citations) and `docs/plans/2026-07-22-panel-shadow-dom-live-test.md`
(DOM verification methodology and results), both from the same research effort.*
