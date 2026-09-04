# ClaudIA UI customisation reference

**What the UI's adjustable settings are, where each one lives, and how to change it.** Living
document — one job: the record of the customisation track (Track D,
[`docs/project-status.md`](../project-status.md) gap #23). Phase 1 shipped 2026-09-02.

Companions: [`ui-design-reference.md`](ui-design-reference.md) is the *styling surface* Panel
offers (shadow DOM, designs, tokens, templates) and the long-form proposal; this document is
what was actually chosen and how to operate it. `panel-reference.md` is how the Panel layer
works today.

Two rules govern this doc (same as its companion):

1. **Every Panel claim is scraped and cited.** The official pages in §6 were fetched with
   Firecrawl on **2026-09-02** (site docs at that date: **panel v1.9.4**; installed: **1.9.3**,
   `pip show panel`). The 1.9.4 delta (§6, releases page) is mostly Tabulator/ESM/OAuth fixes;
   the three entries that touch this work are "Fix `Feed` rendering all objects at once"
   (#8667 — `ChatFeed` renders its log in a `layout.Feed`, `feed.py:241`) and two Fast-theme fixes (#8704, #8652), none
   of which changes a parameter used here. Where the docs are silent, the installed source is cited by
   path and line, and the finding was executed, not read. **Our own code is cited by symbol**, not by
   line — every line anchor in this doc rotted twice on its first day; `grep -n` finds a symbol.
2. **Simple first.** Phase 1 uses Panel parameters only — no CSS, no template, no custom
   component. Anything needing more is listed in §4/§5 with its real cost, not built.

---

## 1. What phase 1 changed

The user's brief (2026-09-02): dark mode, ClaudIA's own avatar, a wider input box, no reaction
hearts, and a menu of other easy changes. Decisions taken in the same session:

| Setting | Value | Where | Env var | Revert |
|---|---|---|---|---|
| Theme default | light unless `CLAUDIA_THEME=dark` | `panel_theme.apply_session_theme`, called in `panel_app._build_session_root` | `CLAUDIA_THEME` | unset the var |
| Theme per tab | `?theme=dark` / `?theme=default` on the URL wins over the env var | `panel_theme.resolve_theme` | — | drop the query parameter |
| ClaudIA's avatar | `claudia/assets/claudia-avatar.png`, registered in `ChatMessage.default_avatars` | `panel_theme.register_claudia_avatar`, called at import in `panel_app` right after `pn.extension(...)` | — | delete the file (letter fallback + one warning) |
| Human's author label (typed messages and screenshot uploads alike) | `CLAUDIA_USER_NAME`, else `User` | `panel_theme.user_display_name`; applied as `user=` in `panel_app._build_chat_app` | `CLAUDIA_USER_NAME` | unset the var |
| Footer buttons | Send only (Stop replaces it while a reply streams) | `show_rerun/undo/clear=False` in `panel_app._build_chat_app` | — | delete the three lines |
| Input box | `ChatAreaInput`, 3 rows growing to 10, placeholder "Ask ClaudIA…", Enter sends | `widgets=` in `panel_app._build_chat_app` | — | delete `widgets=` |
| Reaction icons | off on every message | `panel_app._build_chat_app`: `message_params={"show_reaction_icons": False}` | — | delete the line |
| Pinned layout (later on 2026-09-02, by user request) | the chat pane is bounded by the viewport: `ChatInterface(sizing_mode="stretch_both")` and `sizing_mode="stretch_both"` on the column holding the dots row + chat. Panel's chat then scrolls its own feed (`.chat-feed-log`, `max-height: calc(100% - 75px)`) and keeps the input row at the bottom; the page never scrolls, so the KPI strip stays at the top for free. **Measured** with 40 long messages at 1500×950 and 1280×680: document height == viewport, feed `scrollHeight` 9,179 px inside a 626 px pane, input row fully inside the viewport | `panel_app._build_chat_app` (`sizing_mode=`) and `_build_session_root` (the inner `pn.Column`) | — | remove both `sizing_mode` arguments (the page grows with the messages again) |
| System log (2026-09-03) | a collapsed `pn.Card` titled "System log (n)" under the chat input, holding a read-only timestamped `ChatFeed`; every **session-level** event lands there (§2.6 has the routing rule) and `warning`/`error` entries also raise a toast. The chat keeps only the conversation and the per-turn feedback that answers something you just did | `panel_system_log.SystemLog`; every call site is `syslog.say(...)` in `panel_app` (`_build_action_bar`, `_maybe_background_flex_sync`, `_init_session`, `_make_alert_subscriber`) | — | route a call back to `chat.send(..., user="System")` — the source-scan test `test_no_session_level_system_send_reaches_the_chat_feed` will name it |
| Action bar (2026-09-03) | `[IBKR] [TradingView] [Drive] [End Session]` at the bottom of the chat column, replacing the three status dots **and** the "System" message that used to carry the buttons. **Colour is the light**: `success` green = up, `danger` red = down, `light` grey = not configured / not checked yet. **A click reconnects; it never merely checks** (user rule). Repainted every 5 s from the checker's cache, and again right after a reconnect | `panel_action_bar.ActionBar` (`SERVICE_LABELS` for order and labels, `_COLOR` for the mapping); the three reconnect coroutines in `panel_app._build_action_bar` | — | there is no "revert" short of restoring the dots from git — the bar *is* the status display now |
| Intro card (added later on 2026-09-02) | the opening bubble is the standing portrait over "ClaudIA is ready — loading…"; `_init_session` updates the **same message** in place to "— connected to IBKR." / "— IBKR not connected." (or "session init failed"), **no earlier than 3 s after the message was built** (`_INTRO_MIN_SECONDS`, measured server-side in the factory, so on-screen time is that minus page-load latency, ~0.5 s on localhost — an offline IBKR answers in ~1 s and the portrait would only flicker; init itself is never delayed) | `panel_theme.intro_card`, `claudia/assets/claudia-standing.jpg` (320×480, 37 KB, embedded once per session); `_settle_intro` in `panel_app._build_chat_app` | — | delete the asset (plain text, one warning) |

### 1.1 Live smoke, 2026-09-02 (IBKR offline, TradingView not running)

Served with `CLAUDIA_THEME=dark`, opened with Playwright, captures in
`docs/panel/screenshots/2026-09-02-phase1-*.png` (git-ignored, no account data — the KPI
tiles read `—`):

| Check | Result |
|---|---|
| No URL parameter → dark (env default) | ✅ page background, bubbles, tiles, tabs all dark |
| `?theme=default` on the same process → light | ✅ |
| `?theme=dark` explicit | ✅ |
| `?theme=bogus` | ✅ one WARNING `Ignoring ?theme='bogus' — not one of default/dark` (the smoke run printed `?theme==` — a label typo fixed the same day, before commit), rendered **dark** (fell back to the env default, not to light) |
| Image avatar on every `ClaudIA` message, gear kept on `System` | ✅ (smoked with a 128 px copy of the logo as the placeholder file) |
| Footer: Send only; placeholder "Ask ClaudIA…"; 3-row box | ✅ |
| No reaction icon under any bubble, incl. the action-button row | ✅ (copy icon + timestamp remain, by decision) |
| KPI `Number` tiles inherit the theme colour | ✅ |
| `Tabulator` under dark | ⚠ **light skin on a dark page** — a white block; header text legible, body empty (offline). Not changed in phase 1; `theme="midnight"` is the one-line fix in §4, to be judged with data in the table |
| Chart pane under dark | **not verified** — cache miss (`AAPL_1D_6M_2026-09-02`, IBKR offline). Source says `pn.pane.HoloViews` applies Bokeh's dark theme (§3.1); confirm at the next connected session |
| Intro card (second smoke, light theme) | ✅ portrait over the loading line at ~1 s (`…-intro-loading.png`); same bubble reads "— IBKR not connected." after init (`…-intro-settled.png`). The first cut had no minimum display time and the card had already settled at the first capture (`…-intro-settled-no-min.png`) — hence the 3 s floor |
| Pinned layout (third smoke, scratch harness on :8002 serving the real root under the test patches, 40 messages) | ✅ page does not scroll at 1500×950 or 1280×680; input row at the bottom of the viewport; feed scrolls internally and auto-scrolls to the newest message; Positions tab fits at 680 px (`…-layout-pinned-*.png`) |
| Transient: an empty bubble with a blank avatar ~1s after load | Expected — the opening-status message streaming before its text arrives; it fills in (`…-dark-initialising.png` vs `…-dark.png`) |

### 1.2 Live smoke, 2026-09-03 (gateway up, real account — System log + action bar, commit `b3de83d`)

| Check | Result |
|---|---|
| Left column order: chat → input → `System log (n)` collapsed → Choose File → `[IBKR] [TradingView] [Drive] [End Session]` | ✅ (`2026-09-03-system-log-collapsed-1500x950.png`) |
| Chat shows no `System` bubble | ✅ only ClaudIA's two opening bubbles; the startup "IBKR Gateway disconnected / reconnected" pair that used to be two chat bubbles is now entries 1–2 of the log |
| Lights | at first paint IBKR **red**, TradingView grey, Drive green; IBKR **green by 20:17:37** after the checker's second poll. The red start is the checker's first poll racing the session owner's first read — the same latency the dots had; a candidate in §4 |
| Expand the card | ✅ two timestamped entries, gear avatar, no reaction/copy icons (`…-system-log-expanded-1500x950.png`) |
| Click IBKR while live | ✅ log: "Reading the session before touching anything…", "✔ Already authenticated — not opening the login page.", "✅ Session is live"; nothing opened, light stayed green, button re-enabled |
| Click Drive | ✅ log: "▶ Reconnecting Google Drive…", "✅ Drive connected."; light green, button re-enabled (`…-system-log-after-reconnects-1500x950.png`, 11 entries) |
| Click TradingView | **not smoked** — it launches TradingView Desktop on the machine; the path is the pre-existing launch handler moved verbatim, covered by the three click tests |
| Page height == viewport, log expanded | ✅ 1500×950 and 1280×680; End Session bottom at 675 px of 680 (`…-system-log-expanded-1280x680.png`). At 680 px with the log **expanded** the chat feed is squeezed to one bubble — collapse the card to read the chat; `_FEED_HEIGHT` (240 px) is the knob if that bites |
| Toast on the disconnect alert | not captured — Panel toasts sit outside the Playwright screenshot timing; the `notifications.warning` call is pinned by `test_warning_and_error_toast_when_notifications_exist` |
| Page width | still 1,802 px at a 1,500 px window — the chart controls row (`panel_chart.py`, four default-width widgets); pre-existing, separate fix |

**Review record, 2026-09-02 (self-review + an independent reviewer on the same diff).** Found
and fixed the same day: the avatar skipped every suffixed ClaudIA label (proposals,
PineScript); screenshot uploads still signed as `User`; a non-UTF-8 `?theme=%FF` would have
crashed session build; the theme test pinned Panel's global slot rather than the session slot
(now asserted under a real `Document`); a checkout path containing `{`/`}` would have broken
every message (the avatar is registered as bytes now); two deferred settles could race; the
3 s floor was described as on-screen time when it is measured server-side; the 1.9.4 note was
too strong; `rows` default is 1 in the installed package, not 2; and every line-number citation
had rotted — replaced by symbol citations.

**Light is the chosen default** (user, 2026-09-02: dark is "secondary for now", to be used
once it looks better — the tables still wear the light skin under dark, §1.1). Nothing to
set: an unset `CLAUDIA_THEME` is light.

**Deliberately not on `pn.extension(...)`:** `theme=`. See §3.1 — a global theme would
silence the URL override. The guard is
`tests/test_panel_app.py::test_extension_call_does_not_pre_empt_the_restyle_track`.

Tests: `tests/test_panel_theme.py` (resolver precedence, invalid values, avatar registration
and fallback, oversized-file warning, the intro card and its plain-text fallback) and the "UI
customisation phase 1" block in `tests/test_panel_app.py` (the composed chat surface, the
per-session theme call, the import-time avatar hook, and the opening bubble: portrait before
init, settled online / offline / on init failure, the minimum display time, and a failure
that lands after a success settle was already scheduled). Mutation-checked before shipping:
flipping `show_rerun` back, dropping the `apply_session_theme()` call, and moving the avatar
call above `pn.extension` each fail exactly one test; removing the success-path settle fails
two.

---

## 2. How to

### 2.1 Switch the theme

```bash
# default for every session
CLAUDIA_THEME=dark          # in .env; "default" (light) when unset

# one tab, regardless of the default
open "http://localhost:8001/?theme=dark"
open "http://localhost:8001/?theme=default"
```

A bad value at either level (`?theme=bogus`, `CLAUDIA_THEME=Bogus`) is logged once and skipped,
falling through to the next level — so a typo in the URL lands on the configured default, not
on light. Values are case- and whitespace-insensitive.

**Why there is no switch in the UI.** Panel's only built-in switch is
`FastListTemplate.theme_toggle`, and it is a page reload:

```js
// panel/template/fast/js/fast_template.js:39-49 (installed 1.9.3)
function toggleLightDarkTheme(theme) { … href = updateURLParameter(href, 'theme', theme); window.location.href = href }
```

In ClaudIA a reload is a **new session**: the on-screen conversation is gone, the destroy hook
runs the session-end cleanup (report, Drive upload — `panel_app.py` `_run_session_cleanup`),
and the new session re-downloads `claudia.db` and re-initialises (`_init_session`). Offering
that as a "toggle" would be a trap. Choose the theme before starting a conversation; the URL
parameter is the per-tab override.

### 2.2 Replace ClaudIA's avatar

Drop a **small** PNG at `claudia/assets/claudia-avatar.png`. The committed file is the
user's chatbot portrait (source 1254×1254, resized to 160 px on 2026-09-02 — the smoke
earlier that day ran on a temporary copy of the logo). Panel embeds a local path as
base64 in every message model (`panel/chat/utils.py` `build_avatar_pane` →
`pn.pane.Image`), so a 1 MB file adds ~1.3 MB (base64) to every message. `register_claudia_avatar`
registers the file's **bytes** (read once) rather than the path: Panel `.format()`s a string avatar,
so a checkout path containing `{` or `}` would break every message. `register_claudia_avatar` warns above
200 KB. To shrink a source image on macOS:

```bash
sips -Z 128 path/to/source.png --out claudia/assets/claudia-avatar.png
```

The registration is Panel's documented in-place update of `ChatMessage.default_avatars`
(ChatMessage reference: "You can modify, but not replace the dictionary"). Keys are matched
after `to_alpha_numeric` (`panel/chat/utils.py:23-29` — `\W` stripped, so underscores stay,
lower-cased), so `"ClaudIA"` covers `user="ClaudIA"` in any case — **but each suffixed label
is its own key**: `"ClaudIA — Order Proposal"`, `"— Cancel Proposal"`, `"— Modify Proposal"`
and `"— PineScript"` (the proposal and PineScript renders) are registered one by one from
`panel_theme.CLAUDIA_AUTHORS`, and a source-scan test fails if a new `user="ClaudIA — …"`
literal appears anywhere in `claudia/` without an entry. (The first cut registered `"claudia"`
only and the proposal bubbles fell back to the letter — caught in the same-day review.) This
is why no send site passes `avatar=`: `PanelMessageSink.send_message` (`panel_sink.py`),
the opening messages and the renders all inherit it from the author label.

The 1.4 MB `claudia/assets/claudia-logo.png` (1254×1254, replaced 2026-09-02 with the user's
new logo) is **not** the avatar; it is reserved for a phase-2 header (§5) and must be
resized before a template embeds it.

### 2.3 Rename the human

```bash
CLAUDIA_USER_NAME=Steph     # author label on your messages; avatar is the first letter
```

Blank or unset → `User`. The value is a label only; nothing downstream (store, agent) reads it.

### 2.4 Change the input box

`ChatAreaInput` parameters (ChatAreaInput reference, §6): `rows` (default **1** in the installed
`panel/chat/input.py:55` — the reference page says 2), `max_rows`
(default 10, with `auto_grow=True`), `placeholder`, `enter_sends` (`False` → Ctrl-Enter
sends), `resizable`. Edit the `widgets=` line in
`panel_app._build_chat_app`. Passing our own instance keeps
Enter-to-send because `ChatInterface` wires auto-send by widget type
(`panel/chat/interface.py:296-304`).

### 2.5 Bubble chrome and footer buttons — each one parameter

| Piece | Parameter | Where it goes |
|---|---|---|
| Reaction icons | `show_reaction_icons` | `message_params={...}` on the `ChatInterface` — forwarded to every message the feed builds (ChatFeed reference), including `ChatStep`s and proposal `Column`s |
| Copy icon | `show_copy_icon` | `message_params` |
| Timestamp | `show_timestamp`, `timestamp_format` | `message_params` |
| Author name / avatar | `show_user`, `show_avatar` | `message_params` |
| Streaming dot | `show_activity_dot` | `ChatInterface` |
| Send / Stop / Rerun / Undo / Clear | `show_send`, `show_stop`, `show_rerun`, `show_undo`, `show_clear` | `ChatInterface` |
| Button text vs icon-only | `show_button_name` (icon-only shrinks each to 45 px) | `ChatInterface` |
| Button tooltips | `show_button_tooltips` | `ChatInterface` |

`renderers` must stay a **top-level** argument — inside `message_params` it raises
(`panel_app.py`, comment above the constructor).

### 2.6 System log and action bar (2026-09-03)

**The routing rule, decided by cause** (the way IDE assistants do it): what explains an answer
stays with the answer; what happens *to the session* goes to the log.

| Stays in the chat (per-turn) | Moves to the System log (session-level) |
|---|---|
| tool-call `ChatStep`s (`panel_sink.tool_step`) | connectivity alerts (`ConnectivityChecker.subscribe` → `_make_alert_subscriber`) — `warning` |
| "Response truncated" (`send_max_tokens_warning`) | Flex sync result (`info`), validation / coverage warnings (`warning`) |
| upload rejected / upload failed / the echoed screenshot | document reloaded (`info`), doc-version warning (`warning`) |
| Pine inject outcomes (`panel_pinescript`) | gateway and TradingView reconnect progress and outcome |
| the honest "Setup required / Session init failed" **reply** to a message typed after a failed init | "Saving session…", "Session ended.", "Session init failed", "Setup required" at init time (`error`) |

Levels: `info` = entry only; `warning` = entry + 8 s toast; `error` = entry + sticky toast
(`panel_system_log._TOAST_DURATION_MS`). The count in the card title is the whole "unread"
mechanism — deliberately no badge, no auto-expand.

**To log a new session-level event:** call `syslog.say(text, level)` wherever `syslog` is in
scope in `panel_app` (`_build_chat_app`'s closures, or pass it in as `_maybe_background_flex_sync`
does). Do **not** `chat.send(..., user="System")` — the source-scan test names the four per-turn
sends that are allowed and fails on a fifth.

**The buttons.** `SERVICE_LABELS` in `panel_action_bar` fixes the order and the labels;
`ConnectivityChecker.get_status()` keys them, and a test pins the two key sets against each
other. Each click awaits the reconnect coroutine injected by `panel_app._build_action_bar`,
disable-first with the `loading` spinner, always re-enabled in `finally`; a raising reconnect
becomes one `error` line ("✕ IBKR reconnect failed: …"). What each click does:

| Button | Click |
|---|---|
| IBKR | `get_session().establish(GatewayManager(), emit=syslog.say)` on a worker thread — the one shared session owner runs the **read-only pre-flight first**, so a live session is left alone ("Already authenticated — not opening the login page") and only a session that needs it gets a container start / login page. Never a forced re-login |
| TradingView | launch Desktop with the debug port if it is not up, rebuild the sidecar bridge, hand the tools to the agent, then re-poll the checker |
| Drive | `GDriveSync.reconnect()` (drops the cached service and re-authenticates via `load_or_refresh_credentials`, never a browser), then re-poll. Disabled with a tooltip when Drive was never configured (`UNKNOWN`), because there is nothing to reconnect to |
| End Session | unchanged: unsubscribe, save, report, upload; the bar disables itself first |

**Why the log card is below the input, not between feed and input:** the feed and the input
row are one `ChatInterface`; putting anything between them means reaching into its internals.
Below the input is also where VS Code puts its panel relative to the editor, and the action bar
is the status-bar analogue at the very bottom.

**The "+" attachment button, deferred 2026-09-03:** Panel's chat input has no attachment
slot; two `widgets=` render as a tab strip, not a "+"; and a button that opens the OS picker
needs a browser-side workaround. The file picker keeps its widget and moved down instead.

---

## 3. Research findings the implementation rests on

Each was executed against the installed package, not inferred from prose.

### 3.1 The theme is session-scoped when set inside the session factory

`panel/config.py` (1.9.3): `theme` is **not** in `_config._globals`, so
`pn.config.theme = x` while `state.curdoc` is set writes `_session_config[curdoc]`
(`__setattr__`, lines 436-462), and the `theme` getter (`def theme`, line 675) reads that slot first. The
getter's order is: session slot → global slot (`_theme_`, set by `pn.extension(theme=…)` /
`pn.config.theme` at import) → `?theme=` in `session_args` → `"default"`. **A global default
therefore beats the URL parameter**, which is why ClaudIA resolves the precedence itself and
sets the session slot. The themes how-to documents both `pn.config.theme = 'dark'` and the
`?theme=dark` URL parameter but is silent on their precedence, and makes **no claim** that a
standalone served app follows the browser's `prefers-color-scheme` — it does not.

What follows the theme automatically: the page background (`panel/_templates/base.html:32`,
`html { background-color: #121212 }` when dark), Panel widgets and the chat, the KPI `Number`
tiles (`default_color="inherit"`, `panel_dashboard.py`), and `pn.pane.HoloViews`, which applies
Bokeh's dark theme from the design (`panel/pane/holoviews.py:574-575`). What does **not**:
`Tabulator` — its themes are its own list (`panel/models/tabulator.py:26-30`; the dark skin is
`"midnight"`) and must be passed explicitly. Whether the positions/orders tables need it is
settled by looking, not by assuming (see the phase-1 smoke note in §1's status line once run).

### 3.2 Avatars

ChatMessage reference: `avatar` accepts "a single character text, an emoji, or anything
supported by `pn.pane.Image`"; `default_avatars` is a class-level dict, modify-in-place.
Installed: `build_avatar_pane` (`panel/chat/utils.py:59-81`) tries `Image(avatar)` for anything
longer than one character and falls back to `HTML` (emoji). `ChatInterface(avatar=…)` is the
**human's** avatar, and `callback_avatar` applies only to values *returned* by the callback —
ClaudIA's replies come through the sink's `chat.send(..., user="ClaudIA")`, so neither reaches
them; `default_avatars` does.

### 3.3 Footer buttons and the input row

`ChatInterface` composes the row as `Row(widget, *buttons)`
(`panel/chat/interface.py:354-361`) — there is no parameter that stacks the buttons under the
box. The row's `stylesheets` are Panel's own CDN sheet (`_stylesheets`, line 169), not the
instance's, and it is a nested shadow root; the 2026-07-22 live test proved only *inherited*
properties (font) reach that deep from a `ChatInterface` stylesheet, and `flex-direction` is
not inherited. "Buttons under the box" is therefore a probe, not a parameter (§5). Hiding
Rerun/Undo/Clear leaves the box `stretch_width` beside one 90 px Send button — the requested
width without CSS.

### 3.4 Reaction icons

`reaction_icons` defaults to `{"favorite": "heart"}` and `show_reaction_icons` toggles
rendering (ChatMessage reference). `ChatFeed.message_params` "Parameters to pass to each
ChatMessage" — verified in `test_chat_interface_phase1_surface`: messages already in the feed
carry `show_reaction_icons is False`.

### 3.5 Input widget

The default input is already `ChatAreaInput` (`panel/chat/interface.py:170,175`, placeholder
"Send a message"); the docs' statement that `widgets` "defaults to `[TextInput]`" is stale
against the installed source. `ChatAreaInput` inherits `TextAreaInput`; `value` syncs only on
send, `value_input` holds the draft (ChatAreaInput reference).

### 3.6 Templates, for phase 2

`FastListTemplate` (reference): areas `header`/`sidebar`/`main`/`right_sidebar`/`modal`;
`logo` and `favicon` accept a local file (**base64-encoded into the page** — another reason to
keep image files small); `theme` accepts `"default"`/`"dark"` and `?theme=` overrides it
"unless explicitly declared"; `theme_toggle` default `True` (the reload, §2.1);
`main_layout="card"` by default wraps `main` in a card (`""` to disable); `sidebar_width` 330;
`collapsed_sidebar`. Serving: return the template from the `pn.serve` factory exactly like a
layout (`panel/io/application.py:79` special-cases `BaseTemplate`). Static files, if the logo
is ever served rather than embedded: `pn.serve(..., static_dirs={'assets': './assets'})`; the
`/static` route is reserved (static-files how-to).

---

## 4. Menu of other easy changes — not built, costed

Cost: **S** = one parameter or line, **M** = one function plus a test, **L** = its own design.

| Change | Cost | How | Note |
|---|---|---|---|
| ~~Label the three status dots~~ | — | **Superseded 2026-09-03:** the dots became labelled buttons (§2.6) | — |
| Faster first IBKR light | S | the checker's first poll runs at start, before the session owner's first read, so the IBKR button starts red for up to 60 s on a healthy gateway (§1.2). Either delay the first poll until `GatewaySession` has read once, or poll at 5 s until the first `LIVE` | pre-existing with the dots; visible now that the log records the false "disconnected" |
| System log feed height | S | `panel_system_log._FEED_HEIGHT` (240 px) — at a 680 px window an expanded log leaves one chat bubble visible | §1.2 |
| Hide timestamp / copy icon | S | `message_params` (§2.5) | copy icon is arguably useful; decide by use |
| Icon-only footer, tooltips | S | `show_button_name=False`, `show_button_tooltips=True` | moot with Send only |
| Ctrl-Enter to send | S | `enter_sends=False` on the `ChatAreaInput` | multi-line drafts without accidental sends |
| Dark skin on the tables | S | `Tabulator(theme="midnight")` when the resolved theme is dark | thread the theme into `build_dashboard(...)`; only if the light skin is unreadable on dark |
| KPI tile sizes | S | `font_size="19pt"`, `title_size="10pt"` in `panel_dashboard.py` `Number(...)` | |
| One palette for chart + dashboard | M | `#26a69a` / `#ef5350` are duplicated in `panel_chart.py` and `panel_dashboard.py`; move to one module | prerequisite for theme-matched candles |
| Chat vs dashboard width | M | the `pn.Row` at the root has no ratio; `width`/`sizing_mode` per column | `ui-design-reference.md` §8.6 — needs a live look, not a number on paper |
| Page title | S | `pn.serve(title=...)` — already `"ClaudIA"` | favicon needs a template (§3.6) |
| A design system (Bootstrap / Material) | S to set, M to re-smoke | `pn.extension(design="material")` | changes every widget's look at once; `Button(color=)` names are design-level, verify |
| Fonts, bubble colours | L | `stylesheets=[":host …"]` on `ChatInterface` | the one area where the 7-level shadow DOM makes iteration slow (`ui-design-reference.md` §2, §8.7) |

---

## 5. Phase 2 candidates, with their real cost

**Shipped the same evening (2026-09-02) — see the §1 table row; the analysis below was right:**

- ~~**Chat input pinned to the bottom of the chat pane**~~ (VS Code style) and **the KPI strip
  pinned at the top** while the conversation scrolls. Today the page itself grows with the
  messages, so the input row drifts down and the strip scrolls away. Panel's `ChatInterface`
  already scrolls its feed internally (`chat_interface.css`: `.chat-feed-log { max-height:
  calc(100% - 75px) }`) and keeps the input below it — **but only when the component has a
  bounded height**. The likely fix is sizing, not CSS: give the chat `sizing_mode="stretch_both"`
  inside a column that is itself bounded by the viewport (the root is already `stretch_both`;
  the intermediate `pn.Column` around dots + chat is not), so the feed scrolls inside the pane,
  the input stays put, and the page never scrolls — which pins the strip for free. Needs a live
  look at the intermediate Column and at the dashboard tabs' height on a short window. Cost: M.

1. **A page template** (`FastListTemplate`): header with logo + title, a sidebar for the status
   dots and the screenshot upload (pulling non-conversation UI out of the chat column), a
   modal for destructive confirms. Cost: the layout root becomes a template, the positional
   root tests and `_find_chat` (walks `.objects`; start it from `template.main`) are updated,
   every screen is re-smoked, and `main_layout=""` is set so the app is not wrapped in a
   card. **Its theme switch reloads the page** (§2.1) — either hide it (`theme_toggle=False`)
   or accept that a flip ends the session and say so in the UI.
2. **Buttons under the box** — a live CSS probe against the nested input row (§3.3). If it
   fails, the Panel-native alternative is our own `pn.Row` of buttons *below* the
   `ChatInterface` with `show_send=False` (Enter still sends). **Partly moot since
   2026-09-03:** the action bar is exactly such a `pn.Row` below the `ChatInterface`; only
   Send itself remains in the nested input row.
3. **Saved presets** ("templates" in the user's words): a small settings file (`TOML`) the env
   vars in §1 read from, so a look can be named and switched. Only worth it once there are
   more than a handful of knobs — today there are two env vars.
4. **Screenshot gallery** — before/after captures per phase in `docs/panel/screenshots/`
   (git-ignored; register each in its README with the account-data column filled honestly).
5. ~~**Status dot labels**~~ **Superseded 2026-09-03** — the dots are now the three labelled
   reconnect buttons of the action bar (§2.6).
7. **"+" attachment button** in the input row (deferred 2026-09-03, see §2.6 for why): the
   Panel-native shape is a `button_properties` icon button beside Send whose click *reveals* a
   small picker row under the box; opening the OS picker directly is a browser-side workaround.
   Cost: S for the reveal version, plus a live look at the input row's stylesheet scope.
6. ~~**Intro / "connected" card**~~ **Shipped 2026-09-02, same day** — see the §1 table row.
   Original note, kept for the reasoning: show the standing portrait
   (`~/Documents/Claudia_docs/claudia_standing.png`, to be resized to ~480 px tall and stored as
   `claudia/assets/claudia-standing.png` — shipped as a 37 KB `.jpg`) once per session. Recommended shape: the opening
   "ClaudIA is ready…" bubble becomes a `Column(Image, Markdown)` and its `object` is updated
   in place to the connected text when account data goes live (ChatMessage reference: a sent
   message's value can be updated). **Not** on every IBKR reconnect — the dot flips at most
   startups (gap #26) and mid-session, and a picture each time is noise. A timed swap (session
   periodic callback) or a template modal are the alternatives, both costlier. Cost: M.

---

## 6. Sources

Fetched with Firecrawl on **2026-09-02**; local copies in `.firecrawl/panel/` (git-ignored).
Site version at fetch time: panel v1.9.4.

| Topic | URL |
|---|---|
| `ChatInterface` (buttons, `widgets`, `avatar`, `button_properties`) | https://panel.holoviz.org/reference/chat/ChatInterface.html |
| `ChatMessage` (avatars, `default_avatars`, reactions, CSS class targets) | https://panel.holoviz.org/reference/chat/ChatMessage.html |
| `ChatFeed` (`message_params`, `placeholder_params`) | https://panel.holoviz.org/reference/chat/ChatFeed.html |
| `ChatAreaInput` (`rows`, `max_rows`, `auto_grow`, `enter_sends`) | https://panel.holoviz.org/reference/chat/ChatAreaInput.html |
| Toggling themes (`pn.config.theme`, `?theme=`) | https://panel.holoviz.org/how_to/styling/themes.html |
| Apply a design | https://panel.holoviz.org/how_to/styling/design.html |
| Design variables (`--design-*`, `global_css`) | https://panel.holoviz.org/how_to/styling/design_variables.html |
| Apply CSS (`stylesheets`, shadow DOM) | https://panel.holoviz.org/how_to/styling/apply_css.html |
| Customize template theme | https://panel.holoviz.org/how_to/templates/template_theme.html |
| Set a template | https://panel.holoviz.org/how_to/templates/template_set.html |
| `FastListTemplate` reference (`theme_toggle`, `logo`, `main_layout`) | https://panel.holoviz.org/reference/templates/FastListTemplate.html |
| Static files (`static_dirs`) | https://panel.holoviz.org/how_to/server/static_files.html |
| Releases (1.9.4 delta vs installed 1.9.3) | https://panel.holoviz.org/about/releases.html |

Installed-source citations (`.venv/lib/python3.11/site-packages/panel/`, 1.9.3): `config.py`
`_config.__setattr__` / `theme`; `chat/interface.py` `_init_widgets`; `chat/utils.py`
`to_alpha_numeric`, `build_avatar_pane`; `template/fast/js/fast_template.js`
`toggleLightDarkTheme`; `models/tabulator.py` `TABULATOR_THEMES`; `pane/holoviews.py`
theme application; `io/application.py` template handling; `_templates/base.html` dark
background.
